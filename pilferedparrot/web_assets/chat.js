const $ = (selector) => document.querySelector(selector);
const state = {
  chat: { messages: [], pending: false, model: "gpt-5.6-terra" },
  chat_history: [], chatViewId: null, chat_model: "gpt-5.6-terra",
  chat_model_choices: ["gpt-5.6-terra", "gpt-5.6-luna"], csrfToken: "",
  draftModel: null, model_context_windows: {},
};
let pollTimer = null;

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
  return String(value || "").replace(/\r/g, "").split(/```/).map((chunk, index) => {
    if (index % 2) {
      const language = chunk.match(/^([\w+-]+)\n/);
      const code = language ? chunk.slice(language[0].length).trim() : chunk.trim();
      return `<div class="code-block"><pre><code>${escapeHtml(code)}</code></pre></div>`;
    }
    return chunk.split(/\n{2,}/).filter(Boolean).map((paragraph) =>
      `<p>${inlineMarkdown(paragraph).replace(/\n/g, "<br>")}</p>`
    ).join("");
  }).join("");
}

function modelLabel(model) {
  return model === "gpt-5.6-luna" ? "GPT-5.6 Luna" : "GPT-5.6 Terra";
}

function chatThreads() {
  return [state.chat, ...(state.chat_history || [])].filter(Boolean);
}

function viewedChat() {
  return chatThreads().find((thread) => thread.id === state.chatViewId) || state.chat;
}

function chatRunning() { return Boolean(state.chat?.pending); }
function viewingArchivedChat() { return Boolean(viewedChat()?.archived); }

function relativeTime(timestamp) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const controlHeaders = method === "GET" || !state.csrfToken
    ? {} : { "X-PilferedParrot-CSRF": state.csrfToken };
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...controlHeaders, ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function contextPieMarkup(usage, adjustable = false) {
  if (!usage) return '<div class="context-pie-empty">No context data yet</div>';
  const used = Math.max(0, Number(usage.used_tokens) || 0);
  const limit = Math.max(1, Number(usage.limit_tokens) || 1);
  const maximumKnown = Number(usage.max_tokens) > 0;
  const maximum = maximumKnown ? Math.max(limit, Number(usage.max_tokens)) : limit;
  const allowance = Math.max(1, Math.min(100, Number(usage.allowance_percent) || 100));
  const percent = Math.max(0, Math.min(100, Number(usage.percent) || 0));
  const rounded = Math.round(percent);
  const choices = [...new Set([25, 50, 75, 100, allowance])].sort((a, b) => a - b);
  return `<div class="context-pie ${rounded >= 100 ? "limit" : rounded >= 80 ? "near-limit" : ""}"
      style="--context-percent:${percent * 3.6}deg" role="progressbar"
      aria-label="Chat visible context estimate" aria-valuemin="0" aria-valuemax="100"
      aria-valuenow="${rounded}" title="Visible-context estimate; provider limits may differ">
      <div class="context-pie-center"><strong>${rounded}%</strong><span>used</span></div>
    </div>
    <div class="context-pie-copy">
      <strong>${used.toLocaleString()} / ${limit.toLocaleString()} tokens</strong>
      <span>${usage.estimated ? "Visible transcript estimate" : "Context window"}</span>
      ${adjustable && maximumKnown ? `<label class="context-allowance">
        <span>Allow</span>
        <select data-context-percent aria-label="Allowed portion of model context">
          ${choices.map((value) => `<option value="${value}" ${value === allowance ? "selected" : ""}>${value}%</option>`).join("")}
        </select>
        <span>of ${maximum.toLocaleString()} max</span>
      </label>` : maximumKnown
        ? `<span>${allowance}% of ${maximum.toLocaleString()} max allowed</span>`
        : '<span>Provider maximum unavailable</span>'}
    </div>`;
}

function contextUsageForModel(usage, model) {
  if (!usage) return usage;
  const maximum = Number(state.model_context_windows?.codex?.[model]);
  if (!(maximum > 0)) return usage;
  const allowance = Math.max(1, Math.min(100, Number(usage.allowance_percent) || 100));
  const limit = Math.max(1, Math.floor(maximum * allowance / 100));
  const used = Math.max(0, Number(usage.used_tokens) || 0);
  return {
    ...usage,
    max_tokens: maximum,
    limit_tokens: limit,
    percent: Math.max(0, Math.min(100, used / limit * 100)),
  };
}

function renderHistory() {
  const list = $("#chatHistoryList");
  list.innerHTML = chatThreads().map((thread, index) => `
    <button class="chat-item ${thread.id === state.chatViewId ? "active" : ""}"
            data-chat-thread="${escapeHtml(thread.id)}">
      <div class="chat-item-title">${escapeHtml(thread.title || "New Chat")}</div>
      <div class="chat-item-meta"><span>${thread.archived ? "Archived" : index === 0 ? "Current" : "Chat"}</span>
        <span>${thread.context_status !== "normal" ? '<i class="limit-dot" title="Near practical limit" aria-label="Near practical limit">!</i>' : ""}${relativeTime(thread.updated_at)}</span></div>
    </button>`).join("");
  list.querySelectorAll("[data-chat-thread]").forEach((button) => {
    button.addEventListener("click", () => {
      state.chatViewId = button.dataset.chatThread;
      state.draftModel = viewedChat()?.model || state.chat_model;
      render();
    });
  });
}

function render() {
  const chat = viewedChat() || { messages: [] };
  const archived = Boolean(chat.archived);
  const messages = Array.isArray(chat.messages) ? chat.messages : [];
  const node = $("#chatMessages");
  const selectedModel = archived ? chat.model : (state.draftModel || state.chat?.model || state.chat_model);
  $("#chatThreadTitle").textContent = archived ? (chat.title || "Archived Chat") : (chat.title || "Chat");
  $("#chatIdentity").textContent = `${modelLabel(chat.model || selectedModel)} · separate read-only instance`;
  if (!messages.length && !chat.pending) {
    node.innerHTML = '<div class="chat-empty">A separate conversation for thinking, planning, and keeping track of what matters.</div>';
  } else {
    node.innerHTML = (archived ? '<div class="archive-notice">Archived · read-only</div>' : "")
      + messages.map((message) => {
        const user = message.role === "user";
        return `<article class="chat-message ${user ? "user" : "assistant"} ${message.pending ? "pending" : ""} ${message.error ? "error" : ""}">
          <div class="chat-message-head">${user ? "You" : "Chat"}</div>
          <div class="chat-message-body">${message.pending ? '<span class="thinking" aria-label="Chat is working"><i></i><i></i><i></i></span>' : markdown(message.content || "")}</div>
        </article>`;
      }).join("");
  }
  const context = $("#chatContext");
  const selectedUsage = contextUsageForModel(chat.context_usage, selectedModel);
  context.innerHTML = contextPieMarkup(
    selectedUsage, !archived && Number(selectedUsage?.max_tokens) > 0,
  );
  const allowanceSelect = context.querySelector("[data-context-percent]");
  if (allowanceSelect) {
    allowanceSelect.disabled = archived || chatRunning();
    allowanceSelect.addEventListener("change", async (event) => {
      event.target.disabled = true;
      try {
        state.chat = await api("/api/chat/context", {
          method: "POST", body: JSON.stringify({ percent: Number(event.target.value) }),
        });
        state.chatViewId = state.chat.id;
      } catch (error) {
        toast(error.message);
      } finally {
        render();
      }
    });
  }
  const modelSelect = $("#chatModelSelect");
  modelSelect.innerHTML = state.chat_model_choices.map((model) =>
    `<option value="${escapeHtml(model)}">${escapeHtml(modelLabel(model))}</option>`
  ).join("");
  modelSelect.value = selectedModel;
  modelSelect.disabled = archived || chatRunning();
  $("#chatPrompt").disabled = archived;
  $("#chatPrompt").placeholder = archived ? "Archived chat · select Current to continue" : "Ask Chat…";
  $("#resetChat").disabled = chatRunning();
  $("#cancelChat").classList.toggle("hidden", archived || !chatRunning());
  $("#sendChat").classList.toggle("hidden", !archived && chatRunning());
  $("#sendChat").disabled = archived || chatRunning() || !$("#chatPrompt").value.trim();
  renderHistory();
  if (chat.pending || archived) node.scrollTop = node.scrollHeight;
}

function applyServerState(initial) {
  const viewedId = state.chatViewId;
  const activeModel = state.chat?.model;
  state.chat = initial.chat || state.chat;
  state.chat_history = initial.chat_history || [];
  state.chat_model = initial.chat_model || state.chat_model;
  state.chat_model_choices = initial.chat_model_choices || state.chat_model_choices;
  state.model_context_windows = initial.model_context_windows || state.model_context_windows;
  state.csrfToken = initial.csrf_token || state.csrfToken;
  state.chatViewId = chatThreads().some((thread) => thread.id === viewedId)
    ? viewedId : state.chat?.id || null;
  if (!state.draftModel || state.draftModel === activeModel) {
    state.draftModel = state.chat?.model || state.chat_model;
  }
}

async function refreshState() {
  const node = $("#chatMessages");
  const follow = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
  const previous = node.scrollTop;
  applyServerState(await api("/api/state"));
  render();
  node.scrollTop = follow ? node.scrollHeight : previous;
}

function schedulePoll() {
  if (!chatRunning() || pollTimer !== null) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    try { await refreshState(); }
    catch (error) { toast(error.message); }
    finally { if (chatRunning()) schedulePoll(); }
  }, 750);
}

async function sendChatMessage(event) {
  event.preventDefault();
  const content = $("#chatPrompt").value.trim();
  if (!content || chatRunning() || viewingArchivedChat()) return;
  const model = state.draftModel || state.chat?.model || state.chat_model;
  state.chat.messages.push(
    { role: "user", content },
    { role: "assistant", content: "", pending: true, model },
  );
  state.chat.pending = true;
  state.chat.model = model;
  $("#chatPrompt").value = "";
  resizePrompt();
  render();
  try {
    const response = await api("/api/chat/messages", {
      method: "POST", body: JSON.stringify({ content, model }),
    });
    state.chat = response;
    state.chatViewId = state.chat.id;
    schedulePoll();
  } catch (error) {
    await refreshState().catch(() => {});
    toast(error.message);
  } finally {
    render();
    $("#chatPrompt").focus();
  }
}

async function resetChat(model = null, skipConfirmation = false) {
  if (state.chat?.messages?.length
      && !skipConfirmation
      && !confirm("Archive the current chat and start a new one? Its transcript will remain in Chat history.")) return;
  try {
    const body = model ? JSON.stringify({ model }) : "{}";
    const response = await api("/api/chat/reset", { method: "POST", body });
    state.chat = response.chat;
    state.chat_history = response.chat_history || state.chat_history;
    state.chatViewId = state.chat.id;
    state.draftModel = state.chat.model || state.chat_model;
    render();
    $("#chatPrompt").focus();
  } catch (error) { toast(error.message); }
}

async function selectChatModel() {
  const select = $("#chatModelSelect");
  const model = select.value;
  const previous = state.chat?.model || state.chat_model;
  if (!model || model === previous || chatRunning() || viewingArchivedChat()) return;
  if (state.chat?.messages?.length) {
    const confirmed = confirm(
      `Archive the current chat and start a new one with ${modelLabel(model)}?`,
    );
    if (!confirmed) {
      state.draftModel = previous;
      render();
      return;
    }
    state.draftModel = model;
    await resetChat(model, true);
    return;
  }
  select.disabled = true;
  state.draftModel = model;
  try {
    state.chat = await api("/api/chat/model", {
      method: "POST", body: JSON.stringify({ model }),
    });
    state.chatViewId = state.chat.id;
  } catch (error) {
    state.draftModel = previous;
    toast(error.message);
  } finally {
    render();
  }
}

async function cancelChat() {
  if (!chatRunning()) return;
  try {
    state.chat = await api("/api/chat/cancel", { method: "POST", body: "{}" });
    render();
    schedulePoll();
  } catch (error) { toast(error.message); }
}

function resizePrompt() {
  const prompt = $("#chatPrompt");
  prompt.style.height = "auto";
  prompt.style.height = `${Math.min(prompt.scrollHeight, 170)}px`;
  $("#sendChat").disabled = viewingArchivedChat() || chatRunning() || !prompt.value.trim();
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2800);
}

async function init() {
  try {
    applyServerState(await api("/api/state"));
    render();
    schedulePoll();
  } catch (error) { toast(error.message); }
}

$("#chatComposer").addEventListener("submit", sendChatMessage);
$("#chatPrompt").addEventListener("input", resizePrompt);
$("#chatPrompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chatComposer").requestSubmit();
  }
});
$("#chatModelSelect").addEventListener("change", selectChatModel);
$("#resetChat").addEventListener("click", () => resetChat());
$("#cancelChat").addEventListener("click", cancelChat);
$("#toggleChatSidebar").addEventListener("click", () => $(".chat-window-sidebar").classList.add("open"));
$("#closeChatSidebar").addEventListener("click", () => $(".chat-window-sidebar").classList.remove("open"));
window.addEventListener("focus", () => refreshState().catch(() => {}));

init();
