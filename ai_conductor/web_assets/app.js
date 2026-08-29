const $ = (selector) => document.querySelector(selector);
const state = {
  chats: [], budgets: {}, boardEvents: [], activeId: null, defaultCwd: "",
  draftCwd: "", csrfToken: "", view: "chat",
};
let pollTimer = null;

function activeChat() { return state.chats.find((chat) => chat.id === state.activeId); }
function pendingMessage(chat = activeChat()) { return chat?.messages?.find((message) => message.pending); }
function activeRunning() { return Boolean(pendingMessage()); }
function anyRunning() { return state.chats.some((chat) => pendingMessage(chat)); }

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const controlHeaders = method === "GET" || !state.csrfToken
    ? {} : { "X-Conductor-CSRF": state.csrfToken };
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...controlHeaders, ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function markdown(value) {
  const source = String(value || "").replace(/\r/g, "");
  const chunks = source.split(/```/);
  return chunks.map((chunk, index) => {
    if (index % 2) {
      const code = chunk.replace(/^[\w+-]+\n/, "");
      return `<pre><code>${escapeHtml(code.trim())}</code></pre>`;
    }
    return chunk.split(/\n{2,}/).filter(Boolean).map((paragraph) =>
      `<p>${inlineMarkdown(paragraph).replace(/\n/g, "<br>")}</p>`
    ).join("");
  }).join("");
}

function providerLabel(provider) {
  return ({ qwen: "Qwen", claude: "Claude Code", codex: "Codex", auto: "Qwen decides" })[provider] || provider;
}

function relativeTime(timestamp) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function renderChats() {
  const list = $("#chatList");
  list.innerHTML = state.chats.map((chat) => `
    <button class="chat-item ${chat.id === state.activeId ? "active" : ""}" data-chat="${chat.id}">
      <div class="chat-item-title">${escapeHtml(chat.title)}</div>
      <div class="chat-item-meta"><span>${escapeHtml(providerLabel(chat.provider || chat.requested_provider))}</span><span>${relativeTime(chat.updated_at)}</span></div>
    </button>`).join("");
  list.querySelectorAll("[data-chat]").forEach((button) => button.addEventListener("click", () => {
    state.view = "chat";
    state.activeId = button.dataset.chat;
    state.draftCwd = activeChat().cwd;
    render();
    $("#sidebar").classList.remove("open");
  }));
}

function titleCase(value) {
  return String(value).replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function actorLabel(value) {
  return value === "chris" ? "Operator (legacy)" : titleCase(value);
}

function renderBoard() {
  const actor = $("#boardActorFilter").value;
  const kind = $("#boardKindFilter").value;
  const acknowledged = new Set(state.boardEvents
    .filter((event) => event.kind === "acknowledgement")
    .map((event) => event.related_event_id));
  const visible = state.boardEvents.filter((event) =>
    (!actor || event.actor === actor) && (!kind || event.kind === kind));
  const container = $("#boardEvents");
  if (!visible.length) {
    container.innerHTML = '<div class="board-empty">No board messages match this view.</div>';
    return;
  }
  container.innerHTML = visible.map((event) => {
    const security = event.status === "quarantined" || event.kind === "security_report";
    const canAcknowledge = (event.status === "quarantined" || event.status === "open")
      && !acknowledged.has(event.id);
    const acknowledgedLabel = acknowledged.has(event.id)
      ? '<span class="event-acknowledged">Acknowledged</span>' : "";
    const relation = event.related_event_id
      ? `<span title="${escapeHtml(event.related_event_id)}">Related event · ${escapeHtml(event.related_event_id.slice(0, 8))}</span>`
      : event.related_run_id
        ? `<span title="${escapeHtml(event.related_run_id)}">Verified run · ${escapeHtml(event.related_run_id.slice(0, 8))}</span>` : "";
    return `<article class="board-event ${security ? "security" : ""}">
      <div class="board-event-head">
        <span class="event-actor actor-${escapeHtml(event.actor)}">${escapeHtml(actorLabel(event.actor))}</span>
        <span class="event-kind">${escapeHtml(titleCase(event.kind))}</span>
        <span class="event-status status-${escapeHtml(event.status)}">${escapeHtml(titleCase(event.status))}</span>
        <time datetime="${escapeHtml(event.created_at)}">${escapeHtml(new Date(event.created_at).toLocaleString())}</time>
      </div>
      <div class="board-event-content">${escapeHtml(event.content).replace(/\n/g, "<br>")}</div>
      <div class="board-event-foot">
        <span>${escapeHtml(titleCase(event.source))} · ${escapeHtml(event.id.slice(0, 8))}</span>
        ${relation}${acknowledgedLabel}
        ${canAcknowledge ? `<button type="button" data-acknowledge="${escapeHtml(event.id)}">Acknowledge</button>` : ""}
      </div>
    </article>`;
  }).join("");
  container.querySelectorAll("[data-acknowledge]").forEach((button) =>
    button.addEventListener("click", () => acknowledgeBoard(button.dataset.acknowledge)));
}

async function refreshBoard() {
  const board = await api("/api/board?limit=500");
  state.boardEvents = board.events;
  renderBoard();
}

async function postBoardMessage(event) {
  event.preventDefault();
  const content = $("#boardContent").value.trim();
  if (!content) return;
  const button = $("#postBoard");
  button.disabled = true;
  try {
    const created = await api("/api/board/events", {
      method: "POST",
      body: JSON.stringify({ kind: $("#boardKind").value, content }),
    });
    $("#boardContent").value = "";
    $("#boardCount").textContent = "0";
    await refreshBoard();
    toast(created.status === "quarantined"
      ? "Post quarantined and security event recorded." : "Posted to the message board.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function acknowledgeBoard(eventId) {
  try {
    await api(`/api/board/events/${eventId}/acknowledge`, { method: "POST", body: "{}" });
    await refreshBoard();
    toast("Acknowledgement recorded in the audit log.");
  } catch (error) {
    toast(error.message);
  }
}

async function showBoard() {
  state.view = "board";
  render();
  $("#sidebar").classList.remove("open");
  try { await refreshBoard(); }
  catch (error) { toast(error.message); }
}

// "offline" reads as a provider outage and sends people to check their account.
// Name the actual cause instead -- a missing CLI and a signed-out CLI need
// completely different fixes, and neither is the provider being down.
const STATUS_TEXT = {
  cli_missing: "CLI not found",
  signed_out: "signed out",
  auth_unverified: "auth unverified",
};

function budgetText(budget) {
  if (!budget.available) return STATUS_TEXT[budget.status] || "unavailable";
  if (budget.provider === "qwen") return "local";
  if (budget.window) return `${Math.round(budget.window.remaining_percent)}%`;
  return "ready";
}

function renderBudgets() {
  $("#quotaList").innerHTML = ["qwen", "claude", "codex"].map((name) => {
    const budget = state.budgets[name] || { provider: name, available: false, status: "unknown" };
    const remaining = budget.window ? Math.max(0, budget.window.remaining_percent) : (budget.available ? 100 : 0);
    const title = escapeHtml(budget.note || `${providerLabel(name)} is available`);
    return `<div class="quota-row" title="${title}">
      <span class="quota-dot ${budget.available ? "available" : ""}"></span>
      <span>${escapeHtml(providerLabel(name))}</span>
      <span class="quota-track"><span class="quota-fill" style="width:${remaining}%"></span></span>
      <span>${budgetText(budget)}</span>
    </div>`;
  }).join("");
}

function renderMessages() {
  const chat = activeChat();
  const messages = chat?.messages || [];
  $("#welcome").classList.toggle("hidden", messages.length > 0);
  $("#messages").innerHTML = messages.map((message) => {
    const assistant = message.role === "assistant";
    const name = assistant ? providerLabel(message.provider || chat.provider || "qwen") : "You";
    const route = assistant && message.routed_by_qwen && message.route_reason
      ? `<span class="route-chip">Qwen routed here · ${escapeHtml(message.route_reason)}</span>` : "";
    const body = message.pending
      ? '<span class="thinking"><i></i><i></i><i></i></span>'
      : markdown(message.content);
    const publishable = assistant && message.run_id && message.provider
      && message.exit_code === 0 && !message.pending && !message.error
      && !message.cancelled && !message.interrupted;
    const publication = publishable
      ? message.board_event_id
        ? `<span class="published-label">Published · ${escapeHtml(message.board_event_id.slice(0, 8))}</span>`
        : `<button class="publish-result" type="button" data-publish-chat="${escapeHtml(chat.id)}" data-publish-message="${escapeHtml(message.id)}">Publish exact result to board</button>`
      : "";
    return `<article class="message ${message.role} ${message.error ? "error" : ""}" data-provider="${message.provider || ""}">
      <div class="avatar">${assistant ? "C" : "Y"}</div>
      <div><div class="message-head"><span class="message-name">${escapeHtml(name)}</span>${route}</div>
      <div class="message-content">${body}</div>
      ${publication ? `<div class="message-tools">${publication}</div>` : ""}</div>
    </article>`;
  }).join("");
  $("#messages").querySelectorAll("[data-publish-message]").forEach((button) =>
    button.addEventListener("click", () => publishChatResult(
      button.dataset.publishChat, button.dataset.publishMessage, button,
    )));
}

async function publishChatResult(chatId, messageId, button) {
  button.disabled = true;
  try {
    const publication = await api(`/api/chats/${chatId}/messages/${messageId}/publish`, {
      method: "POST", body: JSON.stringify({ kind: "result" }),
    });
    const chat = state.chats.find((item) => item.id === chatId);
    const message = chat?.messages.find((item) => item.id === messageId);
    if (message) message.board_event_id = publication.event.id;
    renderMessages();
    toast(publication.created
      ? "Published the verified provider result to the board."
      : "That provider result was already on the board.");
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}

function renderHeader() {
  const board = state.view === "board";
  const chat = activeChat();
  $("#chatTitle").textContent = board ? "Message board" : chat?.title || "New conversation";
  $("#projectButton").textContent = board ? "Passive local collaboration log"
    : state.draftCwd || chat?.cwd || state.defaultCwd;
  $("#projectButton").disabled = board;
  const badge = $("#activeProvider");
  if (!board && chat?.provider) {
    badge.textContent = providerLabel(chat.provider);
    badge.className = `provider-badge ${chat.provider}`;
  } else {
    badge.className = "provider-badge hidden";
  }
  $("#deleteChat").classList.toggle("hidden", board);
  const select = $("#providerSelect");
  // The select is the user's routing preference. The badge above is the actual
  // provider selected for this conversation; an auto route must not rewrite it.
  select.value = chat?.requested_provider || "auto";
  select.disabled = activeRunning();
}

function render() {
  renderChats();
  renderBudgets();
  const board = state.view === "board";
  if (board) renderBoard(); else renderMessages();
  renderHeader();
  $("#conversation").classList.toggle("hidden", board);
  $("#composerWrap").classList.toggle("hidden", board);
  $("#boardView").classList.toggle("hidden", !board);
  $("#openBoard").classList.toggle("active", board);
  const running = activeRunning();
  $("#sendButton").classList.toggle("hidden", running);
  $("#sendButton").disabled = running || !$("#prompt").value.trim();
  $("#cancelButton").classList.toggle("hidden", !running);
  $("#cancelButton").disabled = Boolean(pendingMessage()?.cancel_requested);
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2500);
}

async function createChat() {
  state.view = "chat";
  const chat = await api("/api/chats", {
    method: "POST",
    body: JSON.stringify({ cwd: state.draftCwd || state.defaultCwd, provider: "auto" }),
  });
  state.chats.unshift(chat);
  state.activeId = chat.id;
  state.draftCwd = chat.cwd;
  $("#prompt").value = "";
  resizePrompt();
  render();
  $("#prompt").focus();
}

async function sendMessage(event) {
  event.preventDefault();
  const content = $("#prompt").value.trim();
  if (!content || activeRunning()) return;
  if (!activeChat()) await createChat();
  const chat = activeChat();
  const selectedProvider = $("#providerSelect").value;
  const optimisticUser = { id: `local-${Date.now()}`, role: "user", content, created_at: Date.now() / 1000 };
  const optimisticAssistant = { id: `pending-${Date.now()}`, role: "assistant", content: "", pending: true };
  chat.messages.push(optimisticUser, optimisticAssistant);
  if (chat.title === "New conversation") chat.title = content.replace(/\s+/g, " ").slice(0, 54);
  $("#prompt").value = "";
  resizePrompt();
  render();
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
  try {
    const updated = await api(`/api/chats/${chat.id}/messages`, {
      method: "POST",
      body: JSON.stringify({ content, provider: selectedProvider, cwd: state.draftCwd }),
    });
    state.chats = state.chats.map((item) => item.id === updated.id ? updated : item);
    state.chats.sort((a, b) => b.updated_at - a.updated_at);
    schedulePoll();
    api("/api/budgets").then((budgets) => { state.budgets = budgets; renderBudgets(); })
      .catch(() => {});
  } catch (error) {
    // The POST may have reached Conductor even if its response was lost. Re-read
    // server state before declaring failure, avoiding duplicate turns and locks.
    try {
      await refreshState();
      const reachedServer = !activeChat()?.messages?.some((message) => message.id === optimisticUser.id);
      if (!reachedServer) throw error;
      toast(pendingMessage(activeChat())
        ? "Connection recovered; response is still running."
        : "Connection recovered; response completed.");
      schedulePoll();
    } catch (refreshError) {
      chat.messages = chat.messages.filter((message) => message !== optimisticAssistant);
      chat.messages.push({ role: "assistant", content: error.message, error: true });
      toast(error.message);
    }
  } finally {
    render();
    $("#conversation").scrollTop = $("#conversation").scrollHeight;
    $("#prompt").focus();
  }
}

function resizePrompt() {
  const prompt = $("#prompt");
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 180)}px`;
  $("#sendButton").disabled = activeRunning() || !prompt.value.trim();
}

async function deleteChat() {
  const chat = activeChat();
  if (!chat || activeRunning()) { if (chat) toast("Cancel the response before deleting this chat."); return; }
  if (!confirm(`Delete “${chat.title}”?`)) return;
  await api(`/api/chats/${chat.id}`, { method: "DELETE" });
  state.chats = state.chats.filter((item) => item.id !== chat.id);
  state.activeId = state.chats[0]?.id || null;
  if (!state.activeId) return createChat();
  state.draftCwd = activeChat().cwd;
  render();
}

async function init() {
  try {
    const initial = await api("/api/state");
    Object.assign(state, initial);
    state.csrfToken = initial.csrf_token || "";
    state.activeId = state.chats[0]?.id || null;
    state.draftCwd = activeChat()?.cwd || state.defaultCwd;
    if (!state.activeId) await createChat();
    render();
    schedulePoll();
    api("/api/budgets").then((budgets) => { state.budgets = budgets; renderBudgets(); })
      .catch((error) => toast(error.message));
  } catch (error) {
    toast(error.message);
  }
}

function applyServerState(initial) {
  const activeId = state.activeId;
  const draftCwd = state.draftCwd;
  const requestedProvider = activeChat()?.requested_provider;
  Object.assign(state, initial);
  state.activeId = state.chats.some((chat) => chat.id === activeId)
    ? activeId : state.chats[0]?.id || null;
  state.draftCwd = draftCwd || activeChat()?.cwd || state.defaultCwd;
  // Preserve an unsent local menu change while polling some other running chat.
  if (requestedProvider && !activeRunning()) activeChat().requested_provider = requestedProvider;
}

async function refreshState() {
  applyServerState(await api("/api/state"));
  render();
}

function schedulePoll() {
  if (!anyRunning() || pollTimer !== null) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    try { await refreshState(); }
    catch (error) { toast(error.message); }
    finally { if (anyRunning()) schedulePoll(); }
  }, 750);
}

async function cancelMessage() {
  const chat = activeChat();
  if (!chat || !activeRunning()) return;
  $("#cancelButton").disabled = true;
  try {
    const updated = await api(`/api/chats/${chat.id}/cancel`, {
      method: "POST", body: "{}",
    });
    state.chats = state.chats.map((item) => item.id === updated.id ? updated : item);
    render();
    schedulePoll();
  } catch (error) {
    toast(error.message);
    await refreshState().catch(() => {});
  }
}

$("#composer").addEventListener("submit", sendMessage);
$("#prompt").addEventListener("input", resizePrompt);
$("#prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#composer").requestSubmit(); }
});
$("#newChat").addEventListener("click", createChat);
$("#openBoard").addEventListener("click", showBoard);
$("#deleteChat").addEventListener("click", deleteChat);
$("#cancelButton").addEventListener("click", cancelMessage);
$("#providerSelect").addEventListener("change", () => {
  const chat = activeChat();
  if (chat) chat.requested_provider = $("#providerSelect").value;
});
$("#refreshBudgets").addEventListener("click", async () => {
  $("#refreshBudgets").textContent = "…";
  try { state.budgets = await api("/api/budgets"); renderBudgets(); }
  catch (error) { toast(error.message); }
  finally { $("#refreshBudgets").textContent = "↻"; }
});
$("#projectButton").addEventListener("click", () => {
  if (activeChat()?.messages?.length) { toast("Start a new chat to change projects."); return; }
  $("#projectInput").value = state.draftCwd;
  $("#projectDialog").showModal();
});
$("#projectForm").addEventListener("submit", (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  state.draftCwd = $("#projectInput").value.trim() || state.defaultCwd;
  $("#projectDialog").close();
  renderHeader();
});
$("#openSidebar").addEventListener("click", () => $("#sidebar").classList.add("open"));
$("#closeSidebar").addEventListener("click", () => $("#sidebar").classList.remove("open"));
$("#boardComposer").addEventListener("submit", postBoardMessage);
$("#boardContent").addEventListener("input", () => {
  $("#boardCount").textContent = String($("#boardContent").value.length);
});
$("#refreshBoard").addEventListener("click", async () => {
  try { await refreshBoard(); }
  catch (error) { toast(error.message); }
});
$("#boardActorFilter").addEventListener("change", renderBoard);
$("#boardKindFilter").addEventListener("change", renderBoard);
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => {
  $("#prompt").value = button.dataset.prompt;
  resizePrompt();
  $("#prompt").focus();
}));

init();
