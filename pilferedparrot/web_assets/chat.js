const $ = (selector) => document.querySelector(selector);
const state = {
  chat: { messages: [], pending: false, model: "gpt-5.6-terra" },
  chat_history: [], chatViewId: null, chat_model: "gpt-5.6-terra",
  chat_model_choices: [], capability: "", windowProvider: "codex",
  model_catalog: {}, providers: [], modelPolls: {},
  draftModel: null, model_context_windows: {}, browser_theme: { active: false },
  preferences: {},
  initialized: false,
};
const CAPABILITY_SESSION_KEY = "pilferedparrot-chat-capability";
const CHAT_PANE_WIDTHS_KEY = "pilferedparrot-pane-widths";
const CHAT_SIDEBAR_LIMITS = { min: 230, max: 520, variable: "--chat-sidebar-width" };
const CHAT_CONVERSATION_MIN_WIDTH = 300;
const fragment = new URLSearchParams(location.hash.slice(1));
const fragmentCapability = fragment.get("capability") || "";
const fragmentProvider = fragment.get("provider") || "";
try {
  state.capability = fragmentCapability || sessionStorage.getItem(CAPABILITY_SESSION_KEY) || "";
  if (fragmentCapability) sessionStorage.setItem(CAPABILITY_SESSION_KEY, fragmentCapability);
} catch {
  state.capability = fragmentCapability;
}
if (fragmentCapability) history.replaceState(null, "", location.pathname + location.search);
let pollTimer = null;
let themeBackgroundObjectUrl = null;
let toastTimer = null;
let notificationPermissionPending = false;
let resetPending = false;
let stateRequestSequence = 0;
let stateAppliedSequence = 0;

function chatSidebarMaximum() {
  const shellWidth = $(".chat-window")?.getBoundingClientRect().width || window.innerWidth;
  return Math.max(CHAT_SIDEBAR_LIMITS.min, Math.min(CHAT_SIDEBAR_LIMITS.max,
    shellWidth - CHAT_CONVERSATION_MIN_WIDTH - 6));
}

function clampChatSidebarWidth(value) {
  return Math.round(Math.max(CHAT_SIDEBAR_LIMITS.min,
    Math.min(chatSidebarMaximum(), Number(value) || CHAT_SIDEBAR_LIMITS.min)));
}

function savedChatSidebarWidth() {
  try {
    const widths = JSON.parse(localStorage.getItem(CHAT_PANE_WIDTHS_KEY) || "{}");
    return widths.chatSidebar;
  } catch (_error) { return null; }
}

function setChatSidebarWidth(value, persist = false) {
  const width = clampChatSidebarWidth(value);
  const shell = $(".chat-window");
  const handle = $("#chatResizer");
  shell.style.setProperty(CHAT_SIDEBAR_LIMITS.variable, `${width}px`);
  handle.setAttribute("aria-valuemin", CHAT_SIDEBAR_LIMITS.min);
  handle.setAttribute("aria-valuemax", chatSidebarMaximum());
  handle.setAttribute("aria-valuenow", width);
  if (persist) {
    try {
      const widths = JSON.parse(localStorage.getItem(CHAT_PANE_WIDTHS_KEY) || "{}");
      widths.chatSidebar = width;
      localStorage.setItem(CHAT_PANE_WIDTHS_KEY, JSON.stringify(widths));
    } catch (_error) { /* Resizing still works when browser storage is unavailable. */ }
  }
  return width;
}

function setupChatSidebarResizer() {
  const handle = $("#chatResizer");
  const fromPointer = (event) => event.clientX - $(".chat-window").getBoundingClientRect().left;
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    handle.classList.add("dragging");
    let currentWidth = fromPointer(event);
    const move = (moveEvent) => {
      currentWidth = fromPointer(moveEvent);
      setChatSidebarWidth(currentWidth);
    };
    const finish = (upEvent) => {
      if (upEvent.type !== "pointercancel") currentWidth = fromPointer(upEvent);
      setChatSidebarWidth(currentWidth, true);
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
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const current = parseFloat(getComputedStyle($(".chat-window"))
      .getPropertyValue(CHAT_SIDEBAR_LIMITS.variable)) || CHAT_SIDEBAR_LIMITS.min;
    const next = event.key === "Home" ? CHAT_SIDEBAR_LIMITS.min
      : event.key === "End" ? chatSidebarMaximum()
        : current + (event.key === "ArrowRight" ? 16 : -16);
    setChatSidebarWidth(next, true);
  });
  const savedWidth = savedChatSidebarWidth();
  setChatSidebarWidth(savedWidth == null
    ? $(".chat-window-sidebar").getBoundingClientRect().width : savedWidth);
  window.addEventListener("resize", () => {
    const current = parseFloat(getComputedStyle($(".chat-window"))
      .getPropertyValue(CHAT_SIDEBAR_LIMITS.variable));
    setChatSidebarWidth(current);
  });
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

function providerInfo(provider) {
  return state.providers.find((item) => item.id === provider)
    || { id: provider, label: provider || "Unknown" };
}

function providerLabel(provider) { return providerInfo(provider).label; }

function modelOptionLabel(option, provider = "") {
  const value = String(option?.value || "");
  const label = String(option?.label || value);
  if (provider === "claude") return label || value;
  return label && value && label.toLowerCase() !== value.toLowerCase()
    ? `${label} · ${value}` : label || value;
}

function modelLabel(model, provider = state.windowProvider) {
  if (!model) return "Provider-selected model";
  const option = state.model_catalog?.[provider]?.options
    ?.find((item) => item.value === model);
  return modelOptionLabel(option || { value: model, label: model }, provider);
}

function chatModelOptions(selectedModel) {
  const catalog = state.model_catalog?.[state.windowProvider] || { options: [] };
  const options = Array.isArray(catalog.options) ? [...catalog.options] : [];
  if (selectedModel && !options.some((item) => item.value === selectedModel)) {
    options.unshift({ value: selectedModel, label: selectedModel });
  }
  return options;
}

function chatThreads() {
  return [state.chat, ...(state.chat_history || [])].filter(Boolean);
}

function viewedChat() {
  return chatThreads().find((thread) => thread.id === state.chatViewId) || state.chat;
}

function chatRunning() { return Boolean(state.chat?.pending); }
function viewingArchivedChat() { return Boolean(viewedChat()?.archived); }

function notificationPermissionDecision() {
  return state.preferences?.notification_permission || "unasked";
}

function notificationPermissionLabel() {
  const decision = notificationPermissionDecision();
  return {
    unasked: "Enable desktop notifications",
    granted: "Desktop notifications enabled · Reset",
    denied: "Desktop notifications denied · Reset",
    dismissed: "Desktop notifications dismissed · Reset",
    unavailable: "Desktop notifications unavailable · Reset",
  }[decision];
}

async function saveNotificationPermission(decision) {
  state.preferences = await api("/api/preferences/notifications", {
    method: "POST", body: JSON.stringify({ decision }),
  });
  render();
}

async function requestNotificationPermission() {
  if (notificationPermissionPending || notificationPermissionDecision() !== "unasked") return;
  notificationPermissionPending = true;
  render();
  try {
    if (!("Notification" in globalThis)) {
      await saveNotificationPermission("unavailable");
      return;
    }
    const current = Notification.permission;
    if (current === "granted" || current === "denied") {
      await saveNotificationPermission(current);
      return;
    }
    const result = await Notification.requestPermission();
    await saveNotificationPermission(result === "granted" || result === "denied" ? result : "dismissed");
  } catch (error) {
    try { await saveNotificationPermission("dismissed"); }
    catch (_saveError) { toast(error.message, "error"); }
  } finally {
    notificationPermissionPending = false;
    render();
  }
}

async function manageNotificationPermission() {
  if (notificationPermissionDecision() === "unasked") {
    await requestNotificationPermission();
    return;
  }
  await saveNotificationPermission("unasked");
  toast("Notification preference reset. Select Enable desktop notifications to ask again.", "success");
}

function notifyCompletion() {
  if (notificationPermissionDecision() !== "granted"
      || !("Notification" in globalThis) || Notification.permission !== "granted") return;
  try {
    new Notification("PilferedParrot Chat finished", {
      body: `${providerLabel(state.windowProvider)} finished responding.`,
      icon: "/pilferedparrot-icon.png", tag: `pilferedparrot-chat-${state.chat?.id || "current"}`,
    });
  } catch (_error) { /* The response remains visible in Chat. */ }
}

function relativeTime(timestamp) {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

const CONTEXT_ESTIMATE_DISCLOSURE = "Estimate of the next request's live context. It may change due to compaction, newly injected context, or the next user prompt.";
const CONTEXT_INCLUDED = "Includes the transcript, system and developer instructions, tool definitions and relevant tool outputs, repository and workspace context, attachments and other prompt inputs.";

function contextBreakdownMarkup(usage) {
  const transcript = Math.max(0, Number(usage.transcript_tokens) || 0);
  const reserved = Math.max(0, Number(usage.breakdown?.output_reservation) || 0);
  const reservation = reserved
    ? `<p>Response capacity reserved (not used): ${reserved.toLocaleString()} tokens</p>` : "";
  return `<details class="context-breakdown">
      <summary>Estimate details</summary>
      <p>${CONTEXT_ESTIMATE_DISCLOSURE}</p>
      <p>${CONTEXT_INCLUDED}</p>
      <p>Transcript estimate: ${transcript.toLocaleString()} tokens</p>
      ${reservation}
    </details>`;
}

async function api(path, options = {}) {
  const controlHeaders = !state.capability
    ? {} : { "X-PilferedParrot-Capability": state.capability };
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...controlHeaders, ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.responseReceived = true;
    error.status = response.status;
    throw error;
  }
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
      aria-label="Chat estimated live context used" aria-valuemin="0" aria-valuemax="100"
      aria-valuenow="${rounded}" title="${CONTEXT_ESTIMATE_DISCLOSURE}">
      <div class="context-pie-center"><strong>${rounded}%</strong><span>used</span></div>
    </div>
    <div class="context-pie-copy">
      <strong>Estimated context used: ${used.toLocaleString()} / ${limit.toLocaleString()} tokens (${rounded}%)</strong>
      ${adjustable && maximumKnown ? `<label class="context-allowance">
        <span>Allow</span>
        <select data-context-percent aria-label="Allowed portion of model context">
          ${choices.map((value) => `<option value="${value}" ${value === allowance ? "selected" : ""}>${value}%</option>`).join("")}
        </select>
        <span>of ${maximum.toLocaleString()} max</span>
      </label>` : maximumKnown
        ? `<span>${allowance}% of ${maximum.toLocaleString()} max allowed</span>`
        : '<span>Provider maximum unavailable</span>'}
      ${contextBreakdownMarkup(usage)}
    </div>`;
}

function contextUsageForModel(usage, model) {
  if (!usage) return usage;
  const maximum = Number(state.model_context_windows?.[state.windowProvider]?.[model]);
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
      setChatSidebarOpen(false);
    });
  });
}

function render() {
  const chat = viewedChat() || { messages: [] };
  const archived = Boolean(chat.archived);
  const messages = Array.isArray(chat.messages) ? chat.messages : [];
  const node = $("#chatMessages");
  const previousScrollTop = node.scrollTop;
  const followOutput = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
  const selectedModel = archived ? chat.model : (state.draftModel || state.chat?.model || state.chat_model);
  $("#notificationPreferences").disabled = !state.initialized || notificationPermissionPending;
  $("#notificationPreferencesLabel").textContent = notificationPermissionLabel();
  $("#chatThreadTitle").textContent = archived ? (chat.title || "Archived Chat") : (chat.title || "Chat");
  const displayedProvider = chat.provider || state.windowProvider;
  $("#chatIdentity").textContent = `${providerLabel(displayedProvider)} · ${modelLabel(chat.model || selectedModel, displayedProvider)} · separate read-only instance`;
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
      stateAppliedSequence = ++stateRequestSequence;
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
  modelSelect.innerHTML = chatModelOptions(selectedModel).map((option) =>
    `<option value="${escapeHtml(option.value)}">${escapeHtml(modelOptionLabel(option, state.windowProvider))}</option>`
  ).join("");
  modelSelect.value = selectedModel;
  modelSelect.disabled = !state.initialized || archived || chatRunning();
  $("#chatPrompt").disabled = !state.initialized || archived;
  $("#chatPrompt").placeholder = archived ? "Archived chat · select Current to continue" : "Ask Chat…";
  $("#resetChat").disabled = !state.initialized || chatRunning() || resetPending;
  $("#cancelChat").classList.toggle("hidden", archived || !chatRunning());
  $("#sendChat").classList.toggle("hidden", !archived && chatRunning());
  $("#sendChat").disabled = !state.initialized || archived || chatRunning() || !$("#chatPrompt").value.trim();
  renderHistory();
  node.scrollTop = chat.pending && followOutput ? node.scrollHeight : previousScrollTop;
}

function applyServerState(initial) {
  const viewedId = state.chatViewId;
  const activeModel = state.chat?.model;
  state.chat = initial.chat || state.chat;
  state.chat_history = initial.chat_history || [];
  state.chat_model = initial.chat_model || state.chat_model;
  state.chat_model_choices = initial.chat_model_choices || state.chat_model_choices;
  state.windowProvider = initial.chat_provider || fragmentProvider
    || state.chat?.provider || state.windowProvider;
  state.model_catalog = initial.model_catalog || state.model_catalog;
  state.providers = initial.providers || state.providers;
  state.model_context_windows = initial.model_context_windows || state.model_context_windows;
  state.preferences = initial.preferences || state.preferences;
  state.chatViewId = chatThreads().some((thread) => thread.id === viewedId)
    ? viewedId : state.chat?.id || null;
  if (!state.draftModel || state.draftModel === activeModel) {
    state.draftModel = state.chat?.model || state.chat_model;
  }
}

async function pollProviderModels(provider = state.windowProvider, select = null) {
  if (!provider || state.modelPolls[provider]) return state.modelPolls[provider];
  const request = (async () => {
    if (select) select.setAttribute("aria-busy", "true");
    try {
      const catalog = await api(`/api/providers/${encodeURIComponent(provider)}/models`);
      state.model_catalog[provider] = {
        default: catalog.default || "",
        options: Array.isArray(catalog.options) ? catalog.options : [],
      };
      state.model_context_windows[provider] = Object.fromEntries(
        state.model_catalog[provider].options
          .filter((item) => Number(item.max_context_window || item.context_window) > 0)
          .map((item) => [item.value, Number(item.max_context_window || item.context_window)]),
      );
      render();
      return catalog;
    } catch (error) {
      toast(`Could not refresh ${providerLabel(provider)} models: ${error.message}`);
      return null;
    } finally {
      if (select?.isConnected) select.removeAttribute("aria-busy");
      delete state.modelPolls[provider];
    }
  })();
  state.modelPolls[provider] = request;
  return request;
}

async function refreshState() {
  const sequence = ++stateRequestSequence;
  const node = $("#chatMessages");
  const follow = node.scrollHeight - node.scrollTop - node.clientHeight < 120;
  const previous = node.scrollTop;
  const initial = await api("/api/state");
  if (sequence < stateAppliedSequence) return;
  stateAppliedSequence = sequence;
  applyServerState(initial);
  render();
  node.scrollTop = follow ? node.scrollHeight : previous;
}

function schedulePoll() {
  if (!chatRunning() || pollTimer !== null) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    const wasRunning = chatRunning();
    try {
      const sequence = ++stateRequestSequence;
      const current = await api("/api/chat/current");
      if (sequence < stateAppliedSequence) return;
      stateAppliedSequence = sequence;
      state.chat = current;
      render();
      if (wasRunning && !chatRunning()) notifyCompletion();
    }
    catch (error) { toast(error.message); }
    finally { if (chatRunning()) schedulePoll(); }
  }, 750);
}

async function sendChatMessage(event) {
  event.preventDefault();
  const content = $("#chatPrompt").value.trim();
  if (!content || chatRunning() || viewingArchivedChat()) return;
  stateAppliedSequence = ++stateRequestSequence;
  const model = state.draftModel || state.chat?.model || state.chat_model;
  const requestId = globalThis.crypto?.randomUUID?.()
    || `request-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const optimisticAssistant = {
    role: "assistant", content: "", pending: true,
    provider: state.windowProvider, model,
  };
  state.chat.messages.push({ id: requestId, role: "user", content }, optimisticAssistant);
  state.chat.pending = true;
  state.chat.model = model;
  $("#chatPrompt").value = "";
  resizePrompt();
  render();
  try {
    const response = await api("/api/chat/messages", {
      method: "POST", body: JSON.stringify({ content, model, request_id: requestId }),
    });
    state.chat = response;
    state.chatViewId = state.chat.id;
    schedulePoll();
  } catch (error) {
    let accepted = false;
    if (!error.responseReceived) {
      await refreshState().catch(() => {});
      accepted = state.chat?.messages?.some((message) => message.id === requestId);
    }
    if (accepted) {
      toast(chatRunning()
        ? "Connection recovered; Chat is still responding."
        : "Connection recovered; Chat completed its response.");
      schedulePoll();
    } else {
      state.chat.messages = state.chat.messages.filter((message) =>
        message.id !== requestId && message !== optimisticAssistant);
      state.chat.pending = false;
      if (!$("#chatPrompt").value) $("#chatPrompt").value = content;
      resizePrompt();
      toast(error.message);
    }
  } finally {
    render();
    $("#chatPrompt").focus();
  }
}

async function resetChat(model = null) {
  if (resetPending) return;
  const archived = Boolean(state.chat?.messages?.length);
  try {
    stateAppliedSequence = ++stateRequestSequence;
    resetPending = true;
    render();
    const body = model ? JSON.stringify({ model }) : "{}";
    const response = await api("/api/chat/reset", { method: "POST", body });
    state.chat = response.chat;
    state.chat_history = response.chat_history || state.chat_history;
    state.chatViewId = state.chat.id;
    state.draftModel = state.chat.model || state.chat_model;
    render();
    if (archived) toast("Started a new chat. The previous transcript remains in Chat history.", "success");
    $("#chatPrompt").focus();
  } catch (error) { toast(error.message); }
  finally {
    resetPending = false;
    render();
  }
}

async function selectChatModel() {
  const select = $("#chatModelSelect");
  const model = select.value;
  const previous = state.chat?.model || state.chat_model;
  if (!model || model === previous || chatRunning() || viewingArchivedChat()) return;
  if (state.chat?.messages?.length) {
    state.draftModel = model;
    await resetChat(model);
    return;
  }
  select.disabled = true;
  stateAppliedSequence = ++stateRequestSequence;
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

function toast(message, variant = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.dataset.variant = variant;
  node.classList.add("show");
  if (toastTimer !== null) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.classList.remove("show");
    delete node.dataset.variant;
    toastTimer = null;
  }, 5200);
}

async function applyBrowserTheme(theme) {
  const selected = theme?.active ? theme : { active: false };
  const root = document.documentElement;
  const properties = {
    "--chrome-theme-frame": selected.colors?.frame,
    "--chrome-theme-toolbar": selected.colors?.toolbar,
    "--chrome-theme-text": selected.colors?.ntp_text,
    "--chrome-theme-link": selected.colors?.ntp_link,
    "--chrome-theme-section": selected.colors?.ntp_section,
  };
  Object.entries(properties).forEach(([name, value]) => {
    if (/^#[0-9a-f]{6}$/i.test(value || "")) root.style.setProperty(name, value);
    else root.style.removeProperty(name);
  });
  if (selected.background && selected.background_url) {
    try {
      const response = await fetch("/api/browser/theme/background", {
        headers: { "X-PilferedParrot-Capability": state.capability },
      });
      if (!response.ok) throw new Error(`Theme background failed (${response.status})`);
      const nextUrl = URL.createObjectURL(await response.blob());
      if (themeBackgroundObjectUrl) URL.revokeObjectURL(themeBackgroundObjectUrl);
      themeBackgroundObjectUrl = nextUrl;
      root.style.setProperty("--chrome-theme-background-image", `url("${nextUrl}")`);
      root.style.setProperty("--chrome-theme-background-position", selected.background_alignment || "center");
      root.style.setProperty("--chrome-theme-background-repeat", selected.background_repeat || "no-repeat");
    } catch (_error) {
      if (themeBackgroundObjectUrl) URL.revokeObjectURL(themeBackgroundObjectUrl);
      themeBackgroundObjectUrl = null;
      root.style.removeProperty("--chrome-theme-background-image");
    }
  } else {
    if (themeBackgroundObjectUrl) URL.revokeObjectURL(themeBackgroundObjectUrl);
    themeBackgroundObjectUrl = null;
    root.style.removeProperty("--chrome-theme-background-image");
    root.style.removeProperty("--chrome-theme-background-position");
    root.style.removeProperty("--chrome-theme-background-repeat");
  }
  document.body.classList.toggle("chrome-theme", selected.active);
  document.body.dataset.chromeTheme = selected.active ? `${selected.id}:${selected.version}` : "";
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    "content", /^#[0-9a-f]{6}$/i.test(selected.colors?.frame || "")
      ? selected.colors.frame : "#0b1017",
  );
  state.browser_theme = selected;
}

async function refreshBrowserTheme() {
  await applyBrowserTheme(await api("/api/browser/theme"));
}

async function init() {
  try {
    const initial = await api("/api/state");
    applyServerState(initial);
    const theme = await api("/api/browser/theme").catch(() => ({ active: false }));
    await applyBrowserTheme(theme);
    state.initialized = true;
    render();
    schedulePoll();
  } catch (error) { toast(error.message); }
}

$("#chatComposer").addEventListener("submit", sendChatMessage);
$("#chatPrompt").addEventListener("input", resizePrompt);
$("#chatPrompt").addEventListener("keydown", (event) => {
  if (event.isComposing) return;
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chatComposer").requestSubmit();
  }
});
$("#chatModelSelect").addEventListener("change", selectChatModel);
$("#chatModelSelect").addEventListener("pointerdown", (event) => {
  pollProviderModels(state.windowProvider, event.currentTarget);
});
$("#chatModelSelect").addEventListener("keydown", (event) => {
  if (["Enter", " ", "ArrowDown"].includes(event.key)) {
    pollProviderModels(state.windowProvider, event.currentTarget);
  }
});
$("#resetChat").addEventListener("click", () => resetChat());
$("#cancelChat").addEventListener("click", cancelChat);
$("#notificationPreferences").addEventListener("click", () => {
  manageNotificationPermission().catch((error) => toast(error.message, "error"));
});
function setChatSidebarOpen(open) {
  $(".chat-window-sidebar").classList.toggle("open", open);
  $("#toggleChatSidebar").setAttribute("aria-expanded", String(open));
  $(".chat-window-conversation").inert = open && matchMedia("(max-width: 600px)").matches;
  if (open) $("#closeChatSidebar").focus();
}
$("#toggleChatSidebar").addEventListener("click", () => setChatSidebarOpen(true));
$("#closeChatSidebar").addEventListener("click", () => setChatSidebarOpen(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $(".chat-window-sidebar").classList.contains("open")) {
    setChatSidebarOpen(false);
    $("#toggleChatSidebar").focus();
  }
});
setupChatSidebarResizer();
window.addEventListener("focus", () => {
  refreshState().catch(() => {});
  refreshBrowserTheme().catch(() => {});
});

init();
