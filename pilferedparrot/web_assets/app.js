const $ = (selector) => document.querySelector(selector);
const state = {
  chats: [], budgets: {}, activeId: null, defaultCwd: "", draftCwd: "",
  csrfToken: "", models: {}, model_catalog: {}, default_provider: "codex",
  model_context_windows: {},
};
let pollTimer = null;
let budgetPollTimer = null;
let budgetRefresh = null;
const BUDGET_POLL_MS = 60_000;
const PANE_WIDTHS_KEY = "pilferedparrot-pane-widths";
const PANE_LIMITS = {
  sidebar: { min: 220, max: 480, variable: "--sidebar-width" },
};

function activeChat() { return state.chats.find((chat) => chat.id === state.activeId); }
function pendingMessage(chat = activeChat()) { return chat?.messages?.find((message) => message.pending); }
function activeRunning() { return Boolean(pendingMessage()); }
function anyRunning() { return state.chats.some((chat) => pendingMessage(chat)); }

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const controlHeaders = method === "GET" || !state.csrfToken
    ? {} : { "X-PilferedParrot-CSRF": state.csrfToken };
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...controlHeaders, ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 404 && path.startsWith("/api/chat/")) {
    throw new Error("Restart PilferedParrot, then reload this window.");
  }
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

const CODE_BLOCK_LANGUAGES = new Set([
  "bash", "console", "fish", "powershell", "shell", "sh", "terminal", "zsh",
]);

function markdown(value, commandTarget = null) {
  const chunks = String(value || "").replace(/\r/g, "").split(/```/);
  let codeIndex = 0;
  return chunks.map((chunk, index) => {
    if (index % 2) {
      const currentCodeIndex = codeIndex++;
      const language = chunk.match(/^([\w+-]+)\n/);
      const code = language ? chunk.slice(language[0].length).trim() : chunk.trim();
      const shellBlock = !language || CODE_BLOCK_LANGUAGES.has(language[1].toLowerCase());
      const runButton = commandTarget && shellBlock && code && !code.includes("\n")
        ? `<button type="button" class="run-command" data-run-command data-message-id="${escapeHtml(commandTarget.messageId)}" data-block-index="${currentCodeIndex}" title="Run in terminal" aria-label="Run command in terminal">▶</button>`
        : "";
      return `<div class="code-block ${runButton ? "runnable" : ""}">${runButton}<pre><code>${escapeHtml(code)}</code></pre></div>`;
    }
    return chunk.split(/\n{2,}/).filter(Boolean).map((paragraph) =>
      `<p>${inlineMarkdown(paragraph).replace(/\n/g, "<br>")}</p>`
    ).join("");
  }).join("");
}

function providerLabel(provider) {
  return ({ qwen: "Local Qwen", codex: "OpenAI Codex", claude: "Claude Code (history)" })[provider]
    || provider || "Unknown";
}

function defaultModel(provider) { return state.model_catalog?.[provider]?.default || ""; }

function modelLabel(provider, model) {
  const actual = model || defaultModel(provider);
  if (!actual) return "provider default";
  const option = state.model_catalog?.[provider]?.options?.find((item) => item.value === actual);
  return option?.label || actual;
}

function contextUsageMarkup(usage, surface) {
  if (!usage) return "";
  const used = Math.max(0, Number(usage.used_tokens) || 0);
  const limit = Math.max(1, Number(usage.limit_tokens) || 1);
  const percent = Math.max(0, Math.min(100, Number(usage.percent) || 0));
  const rounded = Math.round(percent);
  const estimate = usage.estimated
    ? '<em class="context-estimate">Estimate</em>' : "";
  const accuracy = "Visible-context estimate; provider limits may differ";
  return `<div class="context-usage-head">
      <span class="context-usage-label">Visible context estimate</span>
      <span class="context-usage-counts">${used.toLocaleString()} / ${limit.toLocaleString()}</span>
      <span class="context-usage-value"><strong>${rounded}%</strong>${estimate}</span>
    </div>
    <div class="context-usage-bar" role="progressbar" aria-label="${escapeHtml(surface)} visible context estimate" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${rounded}" title="${accuracy}">
      <span style="width:${percent}%"></span>
    </div>`;
}

function contextPieMarkup(usage, surface, adjustable = false) {
  if (!usage) return '<div class="context-pie-empty">No context data yet</div>';
  const used = Math.max(0, Number(usage.used_tokens) || 0);
  const limit = Math.max(1, Number(usage.limit_tokens) || 1);
  const maximumKnown = Number(usage.max_tokens) > 0;
  const maximum = maximumKnown ? Math.max(limit, Number(usage.max_tokens)) : limit;
  const allowance = Math.max(1, Math.min(100, Number(usage.allowance_percent) || 100));
  const percent = Math.max(0, Math.min(100, Number(usage.percent) || 0));
  const rounded = Math.round(percent);
  const accuracy = "Visible-context estimate; provider limits may differ";
  const choices = [...new Set([25, 50, 75, 100, allowance])].sort((a, b) => a - b);
  const allowanceControl = adjustable && maximumKnown ? `<label class="context-allowance">
      <span>Allow</span>
      <select data-context-percent aria-label="Allowed portion of model context">
        ${choices.map((value) => `<option value="${value}" ${value === allowance ? "selected" : ""}>${value}%</option>`).join("")}
      </select>
      <span>of ${maximum.toLocaleString()} max</span>
    </label>` : maximumKnown
    ? `<span>${allowance}% of ${maximum.toLocaleString()} max allowed</span>`
    : '<span>Provider maximum unavailable</span>';
  return `<div class="context-pie ${rounded >= 100 ? "limit" : rounded >= 80 ? "near-limit" : ""}"
      style="--context-percent:${percent * 3.6}deg" role="progressbar"
      aria-label="${escapeHtml(surface)} visible context estimate" aria-valuemin="0"
      aria-valuemax="100" aria-valuenow="${rounded}" title="${accuracy}">
      <div class="context-pie-center"><strong>${rounded}%</strong><span>used</span></div>
    </div>
    <div class="context-pie-copy">
      <strong>${used.toLocaleString()} / ${limit.toLocaleString()} tokens</strong>
      <span>${usage.estimated ? "Visible transcript estimate" : "Context window"}</span>
      ${allowanceControl}
    </div>`;
}

function contextUsageForModel(usage, provider, model) {
  if (!usage) return usage;
  const selected = model || defaultModel(provider);
  const maximum = Number(state.model_context_windows?.[provider]?.[selected]);
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

function renderModelSelect(provider, requestedModel) {
  const select = $("#modelSelect");
  const catalog = state.model_catalog?.[provider] || { default: "", options: [] };
  const effectiveDefault = catalog.default || "";
  const options = Array.isArray(catalog.options) ? [...catalog.options] : [];
  if (provider === "codex" && !options.some((item) => item.value === "gpt-5.6-sol")) {
    options.unshift({ value: "gpt-5.6-sol", label: "GPT-5.6 Sol" });
  }
  const requested = requestedModel || "";
  if (requested && !options.some((item) => item.value === requested)) {
    options.unshift({ value: requested, label: requested });
  }
  select.innerHTML = `<option value="">Default · ${escapeHtml(modelLabel(provider, effectiveDefault))}</option>`
    + options.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
  select.value = requested;
  select.disabled = activeRunning();
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
      <div class="chat-item-meta"><span>${escapeHtml(providerLabel(chat.provider || chat.requested_provider))}</span><span>${chat.context_status !== "normal" ? '<i class="limit-dot" title="Near practical limit" aria-label="Near practical limit">!</i>' : ""}${relativeTime(chat.updated_at)}</span></div>
    </button>`).join("");
  list.querySelectorAll("[data-chat]").forEach((button) => button.addEventListener("click", () => {
    state.activeId = button.dataset.chat;
    state.draftCwd = activeChat().cwd;
    render();
    $("#sidebar").classList.remove("open");
  }));
}

const STATUS_TEXT = {
  cli_missing: "CLI not found",
  signed_out: "Signed out",
  auth_unverified: "Sign-in status unknown",
};

function budgetWindows(budget) {
  const windows = Array.isArray(budget?.windows) && budget.windows.length
    ? budget.windows : (budget?.window ? [budget.window] : []);
  return windows.filter((window) => Number.isFinite(Number(window.remaining_percent)));
}

function codexWeeklyWindow(budget) {
  const weekly = budgetWindows(budget).filter((window) =>
    window.window_minutes === 10080 || /\bweekly\b/i.test(window.label || ""));
  return weekly.find((window) => /^codex\s*[·:–-]/i.test(window.label || ""))
    || weekly[0] || null;
}

function renderProviders() {
  const budget = state.budgets.codex
    || { provider: "codex", available: false, status: "unknown" };
  const title = escapeHtml(budget.note || providerLabel("codex"));
  const weekly = codexWeeklyWindow(budget);
  const status = budget.available ? "Connected" : (STATUS_TEXT[budget.status] || "Unavailable");
  const allowance = weekly ? (() => {
    const remaining = Math.max(0, Math.min(100, Number(weekly.remaining_percent)));
    const label = "Weekly included usage";
    const reset = weekly.resets_at
      ? `Resets ${new Date(weekly.resets_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}`
      : "Reset time unavailable";
    return `<div class="allowance-row">
      <div class="allowance-head"><span>${escapeHtml(label)}</span><strong>${Math.round(remaining)}% left</strong></div>
      <div class="allowance-track" role="progressbar" aria-label="${escapeHtml(label)} remaining" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(remaining)}"><span style="width:${remaining}%"></span></div>
      <div class="allowance-reset">${escapeHtml(reset)}</div>
    </div>`;
  })() : `<div class="provider-detail">${escapeHtml(budget.note || status)}</div>`;
  $("#providerList").innerHTML = `<div class="provider-card" title="${title}">
    <div class="provider-card-head"><span><i class="status-dot ${budget.available ? "available" : ""}"></i><strong>OpenAI Codex</strong></span><b>${escapeHtml(status)}</b></div>
    <div class="provider-model">${escapeHtml(modelLabel("codex", ""))}</div>
    <div class="allowances">${allowance}</div>
  </div>`;
}

async function refreshBudgets(showErrors = false) {
  if (budgetRefresh) return budgetRefresh;
  budgetRefresh = api("/api/budgets")
    .then((budgets) => { state.budgets = budgets; renderProviders(); return budgets; })
    .catch((error) => { if (showErrors) toast(error.message); })
    .finally(() => { budgetRefresh = null; });
  return budgetRefresh;
}

function scheduleBudgetPoll() {
  if (budgetPollTimer !== null) clearTimeout(budgetPollTimer);
  budgetPollTimer = setTimeout(async () => {
    budgetPollTimer = null;
    await refreshBudgets(false);
    scheduleBudgetPoll();
  }, BUDGET_POLL_MS);
}

function workLabel(item) {
  return ({
    status: "Status", commentary: "Update", tool: "Action", tool_result: "Result", output: "Output",
  })[item.kind] || "Update";
}

function captureWorkScroll() {
  const positions = new Map();
  document.querySelectorAll(".work-items[data-work-key]").forEach((node) => {
    positions.set(node.dataset.workKey, {
      top: node.scrollTop,
      follow: node.scrollHeight - node.scrollTop - node.clientHeight < 24,
    });
  });
  return positions;
}

function restoreWorkScroll(positions) {
  document.querySelectorAll(".work-items[data-work-key]").forEach((node) => {
    const position = positions.get(node.dataset.workKey);
    if (!position) return;
    node.scrollTop = position.follow ? node.scrollHeight : position.top;
  });
}

function renderMessages() {
  const chat = activeChat();
  const messages = chat?.messages || [];
  const workScroll = captureWorkScroll();
  $("#welcome").classList.toggle("hidden", messages.length > 0);
  $("#messages").innerHTML = messages.map((message, messageIndex) => {
    const assistant = message.role === "assistant";
    const provider = message.provider || chat?.provider || chat?.requested_provider;
    const name = assistant
      ? `${providerLabel(provider)} · ${modelLabel(provider, message.model)}` : "You";
    const activity = Array.isArray(message.activity) ? message.activity : [];
    const workKey = String(message.id || `message-${messageIndex}`);
    const work = activity.length ? `<details class="work-log" ${message.pending ? "open" : ""}>
      <summary><span>${message.pending ? `${providerLabel(provider)} is working` : "Work details"}</span><small>${activity.length} update${activity.length === 1 ? "" : "s"}</small></summary>
      <div class="work-items" data-work-key="${escapeHtml(workKey)}">${activity.map((item) => `
        <div class="work-item ${escapeHtml(item.kind || "status")}">
          <span>${escapeHtml(workLabel(item))}</span>
          <div>${escapeHtml(item.content).replace(/\n/g, "<br>")}</div>
        </div>`).join("")}</div>
    </details>` : "";
    const response = message.pending
      ? `<div class="pending-line"><span class="thinking" aria-label="${escapeHtml(providerLabel(provider))} is working"><i></i><i></i><i></i></span><span>Working in ${escapeHtml(chat.cwd)}</span></div>`
      : markdown(message.content, assistant && message.id ? { messageId: message.id } : null);
    return `<article class="message ${message.role} ${message.error ? "error" : ""}" data-provider="${assistant ? escapeHtml(provider) : ""}">
      <div class="avatar">${assistant ? provider === "qwen" ? "Q" : "O" : "Y"}</div>
      <div class="message-body"><div class="message-head"><span class="message-name">${escapeHtml(name)}</span>${message.cancelled ? '<span class="message-state">Cancelled</span>' : ""}</div>
      <div class="message-content">${work}${response}</div></div>
    </article>`;
  }).join("");
  restoreWorkScroll(workScroll);
}

function clampPaneWidth(name, value) {
  const limits = PANE_LIMITS[name];
  return Math.round(Math.max(limits.min, Math.min(limits.max, Number(value) || limits.min)));
}

function savedPaneWidths() {
  try { return JSON.parse(localStorage.getItem(PANE_WIDTHS_KEY) || "{}"); }
  catch (_error) { return {}; }
}

function setPaneWidth(name, value, persist = false) {
  const width = clampPaneWidth(name, value);
  $(".shell").style.setProperty(PANE_LIMITS[name].variable, `${width}px`);
  const handle = name === "sidebar" ? $("#sidebarResizer") : $("#chatResizer");
  handle.setAttribute("aria-valuemin", PANE_LIMITS[name].min);
  handle.setAttribute("aria-valuemax", PANE_LIMITS[name].max);
  handle.setAttribute("aria-valuenow", width);
  if (persist) {
    const widths = savedPaneWidths();
    widths[name] = width;
    try { localStorage.setItem(PANE_WIDTHS_KEY, JSON.stringify(widths)); }
    catch (_error) { /* Resizing still works when browser storage is unavailable. */ }
  }
  return width;
}

function setupPaneResizer(selector, name) {
  const handle = $(selector);
  const fromPointer = (event) => name === "sidebar" ? event.clientX : window.innerWidth - event.clientX;
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("dragging");
    let currentWidth = fromPointer(event);
    const move = (moveEvent) => {
      currentWidth = fromPointer(moveEvent);
      setPaneWidth(name, currentWidth);
    };
    const finish = (upEvent) => {
      if (upEvent.type !== "pointercancel") currentWidth = fromPointer(upEvent);
      setPaneWidth(name, currentWidth, true);
      handle.classList.remove("dragging");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", finish);
      handle.removeEventListener("pointercancel", finish);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", finish);
    handle.addEventListener("pointercancel", finish);
  });
  handle.addEventListener("keydown", (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const current = parseFloat(getComputedStyle($(".shell"))
      .getPropertyValue(PANE_LIMITS[name].variable)) || PANE_LIMITS[name].min;
    const screenDelta = event.key === "ArrowRight" ? 16 : -16;
    setPaneWidth(name, current + (name === "chat" ? -screenDelta : screenDelta), true);
  });
}

function restorePaneWidths() {
  const widths = savedPaneWidths();
  Object.keys(PANE_LIMITS).forEach((name) => {
    const current = parseFloat(getComputedStyle($(".shell"))
      .getPropertyValue(PANE_LIMITS[name].variable));
    setPaneWidth(name, Number.isFinite(Number(widths[name])) ? widths[name] : current);
  });
}

function renderHeader() {
  const chat = activeChat();
  $("#chatTitle").textContent = chat?.title || "New work session";
  $("#projectButton").textContent = state.draftCwd || chat?.cwd || state.defaultCwd;
  const context = $("#technicalContext");
  if (chat?.context_usage) {
    const selectedProvider = chat.requested_provider || chat.provider;
    const selectedModel = chat.requested_model || defaultModel(selectedProvider);
    const selectedUsage = contextUsageForModel(
      chat.context_usage, selectedProvider, selectedModel,
    );
    const adjustable = selectedProvider === "codex" && Number(selectedUsage.max_tokens) > 0;
    context.innerHTML = contextPieMarkup(selectedUsage, "Work session", adjustable);
    context.className = `context-pie-card ${chat.context_status || "normal"}`;
    context.title = chat.context_status === "limit"
      ? "Near practical limit · start a new work session"
      : chat.context_status === "near_limit"
        ? "This session is long · consider starting a new one" : "";
    context.querySelector("[data-context-percent]")?.addEventListener("change", async (event) => {
      event.target.disabled = true;
      try {
        const updated = await api(`/api/chats/${chat.id}/context`, {
          method: "POST", body: JSON.stringify({ percent: Number(event.target.value) }),
        });
        state.chats = state.chats.map((item) => item.id === updated.id ? updated : item);
      } catch (error) {
        toast(error.message);
      } finally {
        render();
      }
    });
  } else {
    context.innerHTML = '<div class="context-pie-empty">No context data yet</div>';
    context.className = "context-pie-card";
  }
  const select = $("#providerSelect");
  select.value = chat?.requested_provider || state.default_provider || "codex";
  select.disabled = activeRunning();
  renderModelSelect(select.value, chat?.requested_model || "");
}

function render() {
  renderChats();
  renderProviders();
  renderMessages();
  renderHeader();
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
  setTimeout(() => node.classList.remove("show"), 2800);
}

async function createChat(provider = $("#providerSelect")?.value || state.default_provider || "codex") {
  const chat = await api("/api/chats", {
    method: "POST",
    body: JSON.stringify({ cwd: state.draftCwd || state.defaultCwd, provider }),
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
  const selectedProvider = $("#providerSelect").value;
  const selectedModel = $("#modelSelect").value;
  if (!activeChat()) await createChat(selectedProvider);
  const chat = activeChat();
  const optimisticUser = {
    id: `local-${Date.now()}`, role: "user", content, created_at: Date.now() / 1000,
  };
  const optimisticAssistant = {
    id: `pending-${Date.now()}`, role: "assistant", content: "", pending: true,
    requested_provider: selectedProvider, requested_model: selectedModel || null,
    provider: selectedProvider, model: selectedModel || defaultModel(selectedProvider) || null,
  };
  chat.messages.push(optimisticUser, optimisticAssistant);
  if (["New technical activity", "New conversation", "New work session"].includes(chat.title)) {
    chat.title = content.replace(/\s+/g, " ").slice(0, 54);
  }
  $("#prompt").value = "";
  resizePrompt();
  render();
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
  try {
    const updated = await api(`/api/chats/${chat.id}/messages`, {
      method: "POST",
      body: JSON.stringify({
        content, provider: selectedProvider, model: selectedModel, cwd: state.draftCwd,
      }),
    });
    state.chats = state.chats.map((item) => item.id === updated.id ? updated : item);
    state.chats.sort((a, b) => b.updated_at - a.updated_at);
    schedulePoll();
  } catch (error) {
    try {
      await refreshState();
      const reachedServer = !activeChat()?.messages?.some((message) => message.id === optimisticUser.id);
      if (!reachedServer) throw error;
      toast(pendingMessage(activeChat())
        ? "Connection recovered; the response is still running."
        : "Connection recovered; the response completed.");
      schedulePoll();
    } catch (_refreshError) {
      chat.messages = chat.messages.filter((message) => message !== optimisticAssistant);
      chat.messages.push({ role: "assistant", content: error.message, error: true, provider: selectedProvider });
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

function applyServerState(initial) {
  const activeId = state.activeId;
  const draftCwd = state.draftCwd;
  const requestedProvider = activeChat()?.requested_provider;
  const requestedModel = activeChat()?.requested_model;
  Object.assign(state, initial);
  state.csrfToken = initial.csrf_token || state.csrfToken;
  state.activeId = state.chats.some((chat) => chat.id === activeId)
    ? activeId : state.chats[0]?.id || null;
  state.draftCwd = draftCwd || activeChat()?.cwd || state.defaultCwd;
  if (state.activeId === activeId && requestedProvider && !activeRunning()) {
    activeChat().requested_provider = requestedProvider;
    activeChat().requested_model = requestedModel || null;
  }
}

async function refreshState() {
  const conversation = $("#conversation");
  const previousScrollTop = conversation.scrollTop;
  const followOutput = conversation.scrollHeight - conversation.scrollTop
    - conversation.clientHeight < 120;
  applyServerState(await api("/api/state"));
  render();
  conversation.scrollTop = followOutput ? conversation.scrollHeight : previousScrollTop;
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
    const updated = await api(`/api/chats/${chat.id}/cancel`, { method: "POST", body: "{}" });
    state.chats = state.chats.map((item) => item.id === updated.id ? updated : item);
    render();
    schedulePoll();
  } catch (error) {
    toast(error.message);
    await refreshState().catch(() => {});
  }
}

async function runTerminalCommand(button) {
  const chat = activeChat();
  if (!chat) return;
  const command = button.closest(".code-block")?.querySelector("code")?.textContent || "";
  if (!confirm(`Run this command as your user in ${chat.cwd}?\n\n${command}`)) return;
  button.disabled = true;
  try {
    await api(`/api/chats/${encodeURIComponent(chat.id)}/terminal`, {
      method: "POST",
      body: JSON.stringify({
        message_id: button.dataset.messageId,
        block_index: Number(button.dataset.blockIndex),
      }),
    });
    toast("Opened command in a terminal.");
  } catch (error) {
    toast(error.message);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function init() {
  try {
    restorePaneWidths();
    const initial = await api("/api/state");
    Object.assign(state, initial);
    state.csrfToken = initial.csrf_token || "";
    await api("/api/window/open", { method: "POST", body: "{}" });
    state.activeId = state.chats[0]?.id || null;
    state.draftCwd = activeChat()?.cwd || state.defaultCwd;
    if (!state.activeId) await createChat(initial.default_provider || "codex");
    render();
    schedulePoll();
    await refreshBudgets(true);
    scheduleBudgetPoll();
  } catch (error) {
    toast(error.message);
  }
}

async function openChatWindow() {
  const targetArea = (screen.availWidth * screen.availHeight) / 6;
  const aspectRatio = 16 / 9;
  // One sixth of a small desktop is narrower than Chat's full-layout
  // breakpoint. Keep the original landscape target, but give a normal window
  // enough room for both panes and the composer.
  const maxWidth = Math.max(320, screen.availWidth - 56);
  const maxHeight = Math.max(240, screen.availHeight - 84);
  const width = Math.min(maxWidth, Math.max(720, Math.round(Math.sqrt(targetArea * aspectRatio))));
  const height = Math.min(maxHeight, Math.max(480, Math.round(Math.sqrt(targetArea / aspectRatio))));
  const left = Math.max(0, (Number(screen.availLeft) || 0) + screen.availWidth - width - 28);
  const top = Math.max(0, (Number(screen.availTop) || 0) + 28);
  try {
    await api("/api/chat/window", {
      method: "POST", body: JSON.stringify({ width, height, left, top }),
    });
  } catch (error) { toast(error.message); }
}

$("#composer").addEventListener("submit", sendMessage);
$("#prompt").addEventListener("input", resizePrompt);
$("#prompt").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});
$("#newWorkSession").addEventListener("click", () => createChat().catch((error) => toast(error.message)));
$("#cancelButton").addEventListener("click", cancelMessage);
$("#openChat").addEventListener("click", openChatWindow);
setupPaneResizer("#sidebarResizer", "sidebar");
$("#messages").addEventListener("click", (event) => {
  const button = event.target.closest("[data-run-command]");
  if (button) runTerminalCommand(button);
});
$("#providerSelect").addEventListener("change", () => {
  const chat = activeChat();
  const provider = $("#providerSelect").value;
  if (chat) {
    chat.requested_provider = provider;
    chat.requested_model = null;
  }
  renderModelSelect(provider, "");
});
$("#modelSelect").addEventListener("change", () => {
  const chat = activeChat();
  if (chat) chat.requested_model = $("#modelSelect").value || null;
});
$("#refreshBudgets").addEventListener("click", async () => {
  $("#refreshBudgets").textContent = "…";
  try { await refreshBudgets(true); }
  finally { $("#refreshBudgets").textContent = "↻"; }
});
$("#projectButton").addEventListener("click", () => {
  if (activeChat()?.messages?.length) {
    toast("Start a new work session to change projects.");
    return;
  }
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
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refreshBudgets(false); scheduleBudgetPoll(); }
});
window.addEventListener("pagehide", () => {
  if (!state.csrfToken) return;
  fetch("/api/window/close", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-PilferedParrot-CSRF": state.csrfToken,
    },
    body: "{}",
    keepalive: true,
  }).catch(() => {});
});
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => {
  $("#prompt").value = button.dataset.prompt;
  resizePrompt();
  $("#prompt").focus();
}));
document.querySelectorAll("[data-provider-choice]").forEach((button) => button.addEventListener("click", () => {
  const provider = button.dataset.providerChoice;
  $("#providerSelect").value = provider;
  const chat = activeChat();
  if (chat) {
    chat.requested_provider = provider;
    chat.requested_model = null;
  }
  renderModelSelect(provider, "");
  $("#prompt").focus();
}));

init();
