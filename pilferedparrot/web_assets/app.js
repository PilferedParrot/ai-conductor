const $ = (selector) => document.querySelector(selector);
const { escapeHtml, render: renderMarkdown } = globalThis.PilferedParrotMarkdown;
const state = {
  chats: [], budgets: {}, activeId: null, defaultCwd: "", draftCwd: "",
  capability: "", models: {}, model_catalog: {}, default_provider: "codex",
  model_context_windows: {}, browser_theme: { active: false }, budgetsLoaded: false,
  windowId: "main", windowProvider: "codex", providerModels: {}, authPending: {},
  authConfirmation: {}, authCodes: {}, providers: [], provider_templates: [],
  providerDraft: null, preferences: {}, modelPolls: {}, modelFeedback: {},
  initialized: false,
};
const CAPABILITY_SESSION_KEY = "pilferedparrot-dashboard-capability";
const WINDOW_ID_SESSION_KEY = "pilferedparrot-dashboard-window-id";
const ACTIVE_CHAT_SESSION_KEY = "pilferedparrot-dashboard-active-chat";
const fragment = new URLSearchParams(location.hash.slice(1));
const fragmentCapability = fragment.get("capability") || "";
const fragmentProvider = fragment.get("provider") || "";
const fragmentWindowId = fragment.get("window") || "";
const fragmentCwd = fragment.get("cwd") || "";
// Set when no inherited or configured folder was usable for this provider:
// the window still opens, and asks for a folder instead of starting a chat.
const fragmentPickProject = fragment.get("pick") === "1";
const fragmentModel = fragment.get("model") || "";
// The launcher supplies the capability fragment on every explicit app open.
// We remove it from the URL below, so a normal reload can be distinguished.
const launchedFromApp = Boolean(fragmentCapability);
try {
  state.capability = fragmentCapability || sessionStorage.getItem(CAPABILITY_SESSION_KEY) || "";
  if (fragmentCapability) sessionStorage.setItem(CAPABILITY_SESSION_KEY, fragmentCapability);
  state.windowId = fragmentWindowId || sessionStorage.getItem(WINDOW_ID_SESSION_KEY) || "main";
  if (fragmentWindowId) sessionStorage.setItem(WINDOW_ID_SESSION_KEY, fragmentWindowId);
} catch {
  state.capability = fragmentCapability;
  state.windowId = fragmentWindowId || "main";
}
if (fragmentCapability) history.replaceState(null, "", location.pathname + location.search);
let pollTimer = null;
let budgetPollTimer = null;
let budgetRefresh = null;
let themeBackgroundObjectUrl = null;
let terminalTarget = null;
let providerLogoutTarget = null;
let pendingLaunchModel = null;
let projectSubmitPending = false;
let createChatPending = false;
let selectionSavePending = false;
let draftReasoningEffort = null;
let toastTimer = null;
let notificationPermissionPending = false;
let stateRequestSequence = 0;
let stateAppliedSequence = 0;
const documentId = globalThis.crypto?.randomUUID?.()
  || `document-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const BUDGET_POLL_MS = 60_000;
const CHAT_WINDOW_WIDTH = 871;
const CHAT_WINDOW_HEIGHT = 376;
const PANE_WIDTHS_KEY = "pilferedparrot-pane-widths";
const DEFAULT_PROMPT_PLACEHOLDER = "Describe what you want done";
const FORK_PROMPT_SUGGESTION = "Help me create my own version of the Pilfered Parrot interface, then ask me what I'd like to change.";
let promptSuggestion = "";
let promptSuggestionSelections = 0;
const PANE_LIMITS = {
  sidebar: { min: 220, max: 480, variable: "--sidebar-width" },
};

function activeChat() { return state.chats.find((chat) => chat.id === state.activeId); }
function latestUsedChat(chats) {
  return [...chats].sort((a, b) =>
    (Number(b.last_used_order) || 0) - (Number(a.last_used_order) || 0)
    || (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0)
  )[0];
}
function visibleChats() {
  return state.chats.filter((chat) =>
    (chat.window_id || "main") === state.windowId
    && (chat.requested_provider || chat.provider) === state.windowProvider);
}
function pendingMessage(chat = activeChat()) { return chat?.messages?.find((message) => message.pending); }
function activeRunning() { return Boolean(pendingMessage()); }
function anyRunning() { return state.chats.some((chat) => pendingMessage(chat)); }

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

function notifyCompletion(body = "Your response is ready.", tag = "pilferedparrot-finished") {
  if (notificationPermissionDecision() !== "granted"
      || !("Notification" in globalThis) || Notification.permission !== "granted") return;
  try {
    new Notification("PilferedParrot finished", {
      body, icon: "/pilferedparrot-icon.png", tag,
    });
  } catch (_error) { /* Completion remains visible in the work session. */ }
}

async function api(path, options = {}) {
  const controlHeaders = !state.capability
    ? {} : { "X-PilferedParrot-Capability": state.capability };
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...controlHeaders, ...(options.headers || {}) },
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 404 && path.startsWith("/api/chat/")) {
    throw new Error("Restart PilferedParrot, then reload this window.");
  }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.responseReceived = true;
    error.status = response.status;
    throw error;
  }
  return data;
}

const CODE_BLOCK_LANGUAGES = new Set([
  "bash", "console", "fish", "powershell", "shell", "sh", "terminal", "zsh",
]);

function providerLabel(provider) {
  return providerInfo(provider).label;
}

function providerInfo(provider) {
  return state.providers.find((item) => item.id === provider)
    || { id: provider, label: provider || "Unknown", description: "Configured LLM provider.", auth_mode: "cli" };
}

function providerIds() {
  const ids = state.providers.map((item) => item.id).filter(Boolean);
  if (!ids.length) return Object.keys(state.model_catalog || {});
  return state.windowProvider && ids.includes(state.windowProvider)
    ? [state.windowProvider, ...ids.filter((provider) => provider !== state.windowProvider)] : ids;
}

function defaultModel(provider) { return state.model_catalog?.[provider]?.default || ""; }

function preferredModel(provider) {
  return state.preferences?.work_models?.[provider] || defaultModel(provider);
}

function modelOptionLabel(option, provider = "") {
  const value = String(option?.value || "");
  const label = String(option?.label || value);
  if (provider === "claude") return label || value;
  return label && value && label.toLowerCase() !== value.toLowerCase()
    ? `${label} · ${value}` : label || value;
}

function modelLabel(provider, model) {
  const actual = model || preferredModel(provider);
  if (!actual) return "Provider-selected model";
  const option = state.model_catalog?.[provider]?.options?.find((item) => item.value === actual);
  return modelOptionLabel(option || { value: actual, label: actual }, provider);
}

function providerModelChoice(provider) {
  if (Object.prototype.hasOwnProperty.call(state.providerModels, provider)) {
    return state.providerModels[provider];
  }
  const chat = provider === state.windowProvider ? activeChat() : null;
  return chat?.requested_model || preferredModel(provider) || "";
}

function providerModelOptions(provider) {
  const catalog = state.model_catalog?.[provider] || { default: "", options: [] };
  const selected = providerModelChoice(provider);
  const options = Array.isArray(catalog.options) ? [...catalog.options] : [];
  if (selected && !options.some((item) => item.value === selected)) {
    options.unshift({ value: selected, label: selected });
  }
  if (!options.length) return '<option value="">Provider-selected model</option>';
  const providerDefault = catalog.default ? "" :
    `<option value="" ${selected ? "" : "selected"}>Provider-selected model</option>`;
  return providerDefault + options.map((item) => `<option value="${escapeHtml(item.value)}" ${item.value === selected ? "selected" : ""}>${escapeHtml(modelOptionLabel(item, provider))}</option>`).join("");
}

function providerModelFeedback(provider, message) {
  state.modelFeedback[provider] = message;
  const node = document.querySelector(`[data-provider-model-status="${CSS.escape(provider)}"]`);
  if (node) {
    node.textContent = message;
    node.hidden = !message;
  }
  if (!$("#providerDialog").open && message !== "Checking models…") toast(message);
}

function modelRefreshFailure(provider) {
  const catalog = state.model_catalog[provider];
  return catalog?.options?.length || catalog?.default
    ? "Could not refresh models. Saved models are still available."
    : "Could not refresh models. Check the provider connection and try again.";
}

async function pollProviderModels(provider, select = null) {
  if (!provider || state.modelPolls[provider]) return state.modelPolls[provider];
  const request = (async () => {
    if (select) select.setAttribute("aria-busy", "true");
    providerModelFeedback(provider, "Checking models…");
    try {
      const catalog = await api(`/api/providers/${encodeURIComponent(provider)}/models`);
      state.model_catalog[provider] = {
        ...state.model_catalog[provider], ...catalog,
        default: catalog.default || "",
        options: Array.isArray(catalog.options) ? catalog.options : [],
      };
      state.model_context_windows[provider] = Object.fromEntries(
        state.model_catalog[provider].options
          .filter((item) => Number(item.max_context_window || item.context_window) > 0)
          .map((item) => [item.value, Number(item.max_context_window || item.context_window)]),
      );
      const selected = providerModelChoice(provider);
      if (select?.matches("[data-provider-model]")) {
        select.innerHTML = providerModelOptions(provider);
        select.value = selected;
      } else if (provider === state.windowProvider) {
        renderModelSelect(provider, activeChat()?.requested_model || selected);
      }
      providerModelFeedback(provider, catalog.warning
        ? modelRefreshFailure(provider) : "Models refreshed.");
      return catalog;
    } catch (error) {
      providerModelFeedback(provider, modelRefreshFailure(provider));
      return null;
    } finally {
      if (select?.isConnected) select.removeAttribute("aria-busy");
      delete state.modelPolls[provider];
    }
  })();
  state.modelPolls[provider] = request;
  return request;
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

function contextUsageMarkup(usage, surface) {
  if (!usage) return "";
  const used = Math.max(0, Number(usage.used_tokens) || 0);
  const limit = Math.max(1, Number(usage.limit_tokens) || 1);
  const percent = Math.max(0, Math.min(100, Number(usage.percent) || 0));
  const rounded = Math.round(percent);
  const estimate = usage.estimated ? '<em class="context-estimate">Estimate</em>' : "";
  return `<div class="context-usage-head">
      <span class="context-usage-label">Estimated context used: ${used.toLocaleString()} / ${limit.toLocaleString()} tokens (${rounded}%)</span>
      <span class="context-usage-value">${estimate}</span>
    </div>
    <div class="context-usage-bar" role="progressbar" aria-label="${escapeHtml(surface)} estimated live context used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${rounded}" title="${CONTEXT_ESTIMATE_DISCLOSURE}">
      <span style="width:${percent}%"></span>
    </div>${contextBreakdownMarkup(usage)}`;
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
  const choices = [...new Set([25, 50, 75, 100, allowance])].sort((a, b) => a - b);
  const allowanceControl = adjustable && maximumKnown ? `<label class="context-allowance">
      <span>Allow</span>
      <select data-context-percent aria-label="Allowed portion of model context">
        ${choices.map((value) => `<option value="${value}" ${value === allowance ? "selected" : ""}>${value}%</option>`).join("")}
      </select>
      <span>of ${maximum.toLocaleString()} max</span>
    </label>` : maximumKnown
    ? `<span>${allowance}% of ${maximum.toLocaleString()} max allowed</span>`
    : '<span>Select or configure a model to show its maximum</span>';
  return `<div class="context-pie ${rounded >= 100 ? "limit" : rounded >= 80 ? "near-limit" : ""}"
      style="--context-percent:${percent * 3.6}deg" role="progressbar"
      aria-label="${escapeHtml(surface)} estimated live context used" aria-valuemin="0"
      aria-valuemax="100" aria-valuenow="${rounded}" title="${CONTEXT_ESTIMATE_DISCLOSURE}">
      <div class="context-pie-center"><strong>${rounded}%</strong><span>used</span></div>
    </div>
    <div class="context-pie-copy">
      <strong>Estimated context used: ${used.toLocaleString()} / ${limit.toLocaleString()} tokens (${rounded}%)</strong>
      ${allowanceControl}
      ${contextBreakdownMarkup(usage)}
    </div>`;
}

function contextUsageForModel(usage, provider, model) {
  if (!usage) return usage;
  const selected = model || preferredModel(provider);
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
  const options = Array.isArray(catalog.options) ? [...catalog.options] : [];
  if (provider === "codex" && !options.some((item) => item.value === "gpt-5.6-sol")) {
    options.unshift({ value: "gpt-5.6-sol", label: "GPT-5.6 Sol" });
  }
  const requested = requestedModel || preferredModel(provider) || "";
  if (requested && !options.some((item) => item.value === requested)) {
    options.unshift({ value: requested, label: requested });
  }
  const providerDefault = catalog.default ? "" : '<option value="">Provider-selected model</option>';
  select.innerHTML = options.length
    ? providerDefault + options.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(modelOptionLabel(item, provider))}</option>`).join("")
    : '<option value="">Provider-selected model</option>';
  select.value = requested;
  select.disabled = !state.initialized || activeRunning() || selectionSavePending;
}


const REASONING_LABELS = { none: "None", minimal: "Minimal", low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Maximum", ultra: "Ultra" };
function reasoningOptions(provider, model) {
  if (provider !== "codex") return [];
  const option = state.model_catalog?.[provider]?.options?.find((item) => item.value === model);
  return Array.isArray(option?.reasoning_efforts) ? option.reasoning_efforts : ["low", "medium", "high"];
}
function renderReasoningSelect(provider, model, effort, disabled, chatSurface = false) {
  const select = $(chatSurface ? "#chatReasoningSelect" : "#reasoningSelect");
  const options = reasoningOptions(provider, model);
  $("#reasoningControl").hidden = !options.length;
  const catalog = state.model_catalog?.[provider] || {};
  const defaultLabel = (chatSurface ? catalog.chat_reasoning_default_label : catalog.reasoning_default_label)
    || (chatSurface ? "Chat default" : "Codex default");
  select.innerHTML = `<option value="">${escapeHtml(defaultLabel)}</option>` + options.map((value) =>
    `<option value="${escapeHtml(value)}">${escapeHtml(REASONING_LABELS[value] || value)}</option>`).join("");
  select.value = options.includes(effort) ? effort : "";
  select.disabled = disabled;
  select.title = select.value === "ultra"
    ? "Ultra reasoning may automatically delegate work to additional agents. Applies to your next message."
    : "Higher reasoning can take longer. Applies to your next message; Default uses the configured setting.";
}
function renderContextSummary(usage, status) {
  const summary = $("#contextSummary");
  summary.textContent = usage ? `~${Math.round(Math.max(0, Math.min(100, Number(usage.percent) || 0)))}% used` : "No data yet";
  summary.className = status || "";
  summary.title = "Estimated context usage. Expand for details and context settings.";
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
  list.innerHTML = visibleChats().map((chat) => `
    <button class="chat-item ${chat.id === state.activeId ? "active" : ""}" data-chat="${escapeHtml(chat.id)}">
      <div class="chat-item-title">${escapeHtml(chat.title)}</div>
      <div class="chat-item-meta"><span>${escapeHtml(providerLabel(chat.provider || chat.requested_provider))}</span><span>${chat.context_status !== "normal" ? '<i class="limit-dot" title="Near practical limit" aria-label="Near practical limit">!</i>' : ""}${relativeTime(chat.updated_at)}</span></div>
    </button>`).join("");
  list.querySelectorAll("[data-chat]").forEach((button) => button.addEventListener("click", async () => {
    const chatId = button.dataset.chat;
    state.activeId = chatId;
    try { sessionStorage.setItem(ACTIVE_CHAT_SESSION_KEY, state.activeId); } catch (_error) {}
    state.draftCwd = activeChat().cwd;
    render();
    setSidebarOpen(false);
    try {
      const updated = await api(`/api/chats/${chatId}/activate`, {
        method: "POST", body: JSON.stringify({}),
      });
      state.chats = state.chats.map((chat) => chat.id === updated.id ? updated : chat)
        .sort((a, b) => b.updated_at - a.updated_at);
      renderChats();
    } catch (_error) {
      // The selected session remains usable if its best-effort recency update
      // is interrupted by a shutdown or a transient local-server error.
    }
  }));
}

const STATUS_TEXT = {
  cli_missing: "CLI not found",
  signed_out: "Signed out",
  auth_unverified: "Sign-in status unknown",
};
const AUTH_TEXT = {
  signed_in: "Signed in",
  signed_out: "Signed out",
  auth_unknown: "Sign-in unknown",
  local_no_auth: "Local · no sign-in",
};
const REACHABILITY_TEXT = { unreachable: "Unreachable" };

function providerReachabilityText(provider, budget) {
  if (budget?.reachability === "reachable") return "";
  if (budget?.available && budget?.status === "auth_unverified") return "Access checked when used";
  // Qwen can be intentionally stopped between requests. When its configured
  // launcher is available, `available` means it is ready to start on demand,
  // not that the local endpoint is broken.
  if (provider === "qwen" && budget?.available) return "Ready on demand";
  return REACHABILITY_TEXT[budget?.reachability] || "Endpoint unavailable";
}

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

function providerUsageWindows(provider, budget) {
  if (provider === "codex") {
    const weekly = codexWeeklyWindow(budget);
    return weekly ? [weekly] : [];
  }
  return [];
}

function providerUsageUnavailableMarkup(budget) {
  // Usage availability is an adapter-owned contract. Keep this rendering
  // provider-neutral and only show a note when the backend supplied one.
  if (!["unavailable", "unsupported"].includes(budget?.usage_status)
      || !budget?.usage_note) return "";
  return `<p class="usage-unavailable-note">${escapeHtml(budget.usage_note)}</p>`;
}

function allowanceResetTime(timestamp) {
  const milliseconds = Number(timestamp) * 1000;
  const date = new Date(milliseconds);
  if (!(milliseconds > 0) || Number.isNaN(date.getTime())) {
    return { short: "Reset unavailable", exact: "Reset time unavailable", datetime: "" };
  }
  const exact = `Resets ${date.toLocaleString([], {
    weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  })}`;
  const minutes = Math.ceil((milliseconds - Date.now()) / 60_000);
  if (minutes <= 0) return { short: "Reset due", exact, datetime: date.toISOString() };
  if (minutes < 60) {
    return { short: `Resets in ${minutes}m`, exact, datetime: date.toISOString() };
  }
  if (minutes < 24 * 60) {
    const hours = Math.floor(minutes / 60);
    const remainder = minutes % 60;
    return {
      short: `Resets in ${hours}h${remainder ? ` ${remainder}m` : ""}`,
      exact,
      datetime: date.toISOString(),
    };
  }
  return {
    short: `Resets ${date.toLocaleString([], {
      weekday: "short", hour: "numeric",
    })}`,
    exact,
    datetime: date.toISOString(),
  };
}

function allowanceLabel(provider, window) {
  return window.label || "Included usage";
}

function allowanceMarkup(provider, window) {
  const remaining = Math.max(0, Math.min(100, Number(window.remaining_percent)));
  const reset = allowanceResetTime(window.resets_at);
  const label = allowanceLabel(provider, window);
  const resetMarkup = reset.datetime
    ? `<time class="allowance-reset" datetime="${escapeHtml(reset.datetime)}" title="${escapeHtml(reset.exact)}" aria-label="${escapeHtml(reset.exact)}">${escapeHtml(reset.short)}</time>`
    : `<span class="allowance-reset" title="${escapeHtml(reset.exact)}">${escapeHtml(reset.short)}</span>`;
  return `<div class="allowance-row">
    <div class="allowance-head"><span class="allowance-label">${escapeHtml(label)}</span><strong>${Math.round(remaining)}% left</strong></div>
    <div class="allowance-meter"><div class="allowance-track" role="progressbar" aria-label="${escapeHtml(label)} remaining" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(remaining)}"><span style="width:${remaining}%"></span></div>${resetMarkup}</div>
  </div>`;
}

function renderProviders() {
  const providers = [state.windowProvider];
  $("#providerList").innerHTML = providers.map((provider) => {
    const budget = state.budgets[provider];
    const label = providerLabel(provider);
    if (!budget) {
      const status = state.budgetsLoaded ? "Status unavailable" : "Checking…";
      return `<div class="provider-card" title="${escapeHtml(`Checking ${label} connection status`)}">
        <div class="provider-card-head"><span><i class="status-dot"></i><strong>${escapeHtml(label)}</strong></span><b>${status}</b></div>
        <div class="provider-model">${escapeHtml(modelLabel(provider, ""))}</div>
      </div>`;
    }
    const auth = AUTH_TEXT[budget.auth_status] || STATUS_TEXT[budget.status] || "Sign-in unavailable";
    const reachability = providerReachabilityText(provider, budget);
    const status = reachability ? `${auth} · ${reachability}` : auth;
    const allowances = providerUsageWindows(provider, budget)
      .map((window) => allowanceMarkup(provider, window)).join("");
    const usageUnavailable = providerUsageUnavailableMarkup(budget);
    const model = modelLabel(provider, "");
    return `<div class="provider-card" title="${escapeHtml(budget.note || label)}">
      <div class="provider-card-head"><span><i class="status-dot ${budget.reachability === "reachable" ? "available" : ""}"></i><strong>${escapeHtml(label)}</strong></span><b>${escapeHtml(status)}</b></div>
      ${model === "Provider-selected model" ? "" : `<div class="provider-model">${escapeHtml(model)}</div>`}
      ${allowances ? `<div class="allowances">${allowances}</div>` : ""}
      ${usageUnavailable}
    </div>`;
  }).join("");
  renderProviderConnections();
}

function renderProviderConnections() {
  const list = $("#providerConnectionList");
  if (!list) return;
  const draft = state.providerDraft ? providerDraftMarkup() : "";
  list.innerHTML = draft + providerIds().map((provider) => {
    const info = providerInfo(provider);
    const budget = state.budgets[provider];
    const missingCli = budget?.status === "cli_missing";
    const authHelp = missingCli
      ? info.install_help || `Install the ${providerLabel(provider)} CLI, then select Refresh status.`
      : info.auth_help || "Connection is managed by this provider.";
    const pending = Boolean(state.authPending[provider]) && budget?.auth_status !== "signed_in";
    const baseStatus = budget
      ? STATUS_TEXT[budget.status] || AUTH_TEXT[budget.auth_status] || "Status unavailable"
      : state.budgetsLoaded ? "Status unavailable" : "Checking…";
    const reachability = providerReachabilityText(provider, budget);
    const auth = pending ? "Complete sign-in in your browser"
      : reachability ? `${baseStatus} · ${reachability}` : baseStatus;
    const authAction = info.auth_mode !== "cli"
      ? `<span class="provider-local-note">${escapeHtml(info.auth_label || "No sign-in required")}</span>`
      : missingCli
        ? '<button type="button" class="secondary" disabled>Install CLI first</button>'
      : budget?.auth_status === "signed_in"
        ? `<button type="button" class="secondary" data-provider-logout="${provider}">Sign out</button>`
        : `<button type="button" class="secondary" data-provider-login="${provider}" ${pending ? "disabled" : ""}>${pending ? "Sign-in browser opened" : "Sign in"}</button>`;
    const confirmation = pending && state.authConfirmation[provider]
      ? `<div class="provider-auth-code">
          <label><span>Confirmation code <small>(only if Anthropic shows one)</small></span>
            <input data-provider-auth-code="${provider}" value="${escapeHtml(state.authCodes[provider] || "")}" placeholder="Paste the complete code#state value" autocomplete="one-time-code" autocapitalize="none" spellcheck="false">
          </label>
          <button type="button" data-provider-code="${provider}" ${state.authCodes[provider]?.trim() ? "" : "disabled"}>Confirm sign-in</button>
          <small>The browser usually completes sign-in automatically. Paste its code here only when it asks you to return to Claude Code.</small>
        </div>` : "";
    return `<section class="provider-connection-card">
      <div class="provider-connection-head">
        <span><i class="status-dot ${budget?.reachability === "reachable" ? "available" : ""}"></i><strong>${escapeHtml(providerLabel(provider))}</strong>${provider === state.windowProvider ? '<em class="current-provider">Current window</em>' : ""}</span>
        <small>${escapeHtml(auth)}</small>
      </div>
      <p class="provider-connection-description">${escapeHtml(info.description)}</p>
      <label class="provider-model-field">
        <span>Model for new window</span>
        <select data-provider-model="${provider}" aria-label="${escapeHtml(providerLabel(provider))} model">
          ${providerModelOptions(provider)}
        </select>
      </label>
      <p class="provider-model-status" data-provider-model-status="${provider}" role="status" ${state.modelFeedback[provider] ? "" : "hidden"}>${escapeHtml(state.modelFeedback[provider] || "")}</p>
      <p class="provider-auth-help">${escapeHtml(authHelp)}</p>
      ${confirmation}
      <div class="provider-connection-actions">
        ${authAction}
        <button type="button" data-provider-window="${provider}">${budget?.auth_status === "signed_in" || info.auth_mode !== "cli" ? "Use" : "Open"} ${escapeHtml(providerLabel(provider))}</button>
        <button type="button" class="secondary" data-provider-remove="${escapeHtml(provider)}">Remove provider</button>
      </div>
    </section>`;
  }).join("");
}

function providerTemplateInfo(id) {
  return (state.provider_templates || []).find((item) => item.id === id) || {};
}

function providerDraftMarkup() {
  const templates = Array.isArray(state.provider_templates) ? state.provider_templates : [];
  const selected = state.providerDraft?.template || templates[0]?.id || "";
  const template = providerTemplateInfo(selected);
  const draft = state.providerDraft || {};
  const options = templates.map((item) =>
    `<option value="${escapeHtml(item.id)}" ${item.id === selected ? "selected" : ""}>${escapeHtml(item.label || item.id)}</option>`
  ).join("");
  const settings = template.restorable
    ? '<p class="provider-draft-note">The complete previous configuration will be restored.</p>'
    : `<div class="provider-draft-fields">
      <label><span>Display name</span><input data-provider-draft-label maxlength="128" value="${escapeHtml(draft.label ?? template.label ?? "")}"></label>
      <label><span>Model ID <small>(blank = auto-detect)</small></span><input data-provider-draft-model maxlength="128" placeholder="Discover automatically" value="${escapeHtml(draft.model ?? template.model ?? "")}"></label>
      <label><span>Base URL</span><input data-provider-draft-base-url maxlength="512" value="${escapeHtml(draft.base_url ?? template.base_url ?? "")}"></label>
      <label><span>API-key environment variable</span><input data-provider-draft-api-key-env maxlength="128" value="${escapeHtml(draft.api_key_env ?? template.api_key_env ?? "")}"></label>
    </div>
    <p class="provider-draft-note">Secrets stay in your environment; PilferedParrot stores only the provider settings above.</p>`;
  return `<section class="provider-connection-card provider-draft-card" data-provider-draft>
    <div class="provider-connection-head"><span><strong>Add provider</strong></span><small>${escapeHtml(template.description || "Review the connection details before adding it.")}</small></div>
    <label class="provider-draft-field"><span>Provider template</span><select data-provider-draft-template>${options}</select></label>
    ${settings}
    <div class="provider-connection-actions"><button type="button" data-provider-draft-cancel class="secondary">Cancel</button><button type="button" data-provider-draft-submit>${template.restorable ? "Restore provider" : "Add provider"}</button></div>
  </section>`;
}

async function refreshBudgets(showErrors = false) {
  if (budgetRefresh) return budgetRefresh;
  budgetRefresh = api("/api/budgets")
    .then((budgets) => {
      state.budgets = budgets;
      Object.entries(budgets).forEach(([provider, budget]) => {
        if (budget?.auth_status === "signed_in") {
          delete state.authPending[provider];
          delete state.authConfirmation[provider];
          delete state.authCodes[provider];
        }
      });
      state.budgetsLoaded = true;
      renderProviders();
      return budgets;
    })
    .catch((error) => {
      state.budgetsLoaded = true;
      renderProviders();
      if (showErrors) toast(error.message);
      return null;
    })
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
  const identityState = globalThis.PilferedParrotIdentity.captureState($("#messages"));
  $("#welcome").classList.toggle("hidden", messages.length > 0);
  $("#messages").innerHTML = messages.map((message, messageIndex) => {
    const assistant = message.role === "assistant";
    const role = assistant ? "assistant" : "user";
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
      ? `<div class="pending-line"><span class="thinking" aria-label="${escapeHtml(providerLabel(provider))} is working"><i></i><i></i><i></i></span><span>${escapeHtml((activity.at(-1)?.content || `Starting ${providerLabel(provider)}…`).slice(0, 240))} · ${escapeHtml(relativeTime(message.created_at))}</span></div>`
      : renderMarkdown(message.content, {
        commandTarget: assistant && message.id ? { messageId: message.id } : null,
        shellLanguages: CODE_BLOCK_LANGUAGES,
      });
    return `<article class="message ${role} ${message.error ? "error" : ""}" data-provider="${assistant ? escapeHtml(provider) : ""}">
      <div class="avatar">${assistant ? escapeHtml(providerInfo(provider).initial || "A") : "Y"}</div>
      <div class="message-body"><div class="message-head"><span class="message-name">${escapeHtml(name)}</span>${message.cancelled ? '<span class="message-state">Cancelled</span>' : ""}</div>
      <div class="message-content">${work}${response}${assistant && !message.pending ? globalThis.PilferedParrotIdentity.render(message) : ""}</div></div>
    </article>`;
  }).join("");
  restoreWorkScroll(workScroll);
  globalThis.PilferedParrotIdentity.restoreState($("#messages"), identityState);
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
    renderContextSummary(selectedUsage, chat.context_status);
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
    renderContextSummary(null);
  }
  renderModelSelect(state.windowProvider, chat?.requested_model || "");
  renderReasoningSelect(state.windowProvider, $("#modelSelect").value,
    chat ? chat.reasoning_effort : draftReasoningEffort,
    !state.initialized || activeRunning() || selectionSavePending);
}

function render() {
  if (state.initialized) {
    const selected = activeChat();
    globalThis.PilferedParrotUpdates.check(
      api, state.windowProvider, selected?.id || "new", providerLabel(state.windowProvider),
    );
  }
  document.body.dataset.windowProvider = state.windowProvider;
  renderChats();
  renderProviders();
  renderMessages();
  renderHeader();
  const running = activeRunning();
  const ready = state.initialized;
  $("#prompt").disabled = !ready;
  $("#newWorkSession").disabled = !ready || createChatPending || selectionSavePending;
  const chatSupported = providerInfo(state.windowProvider).capabilities?.chat !== false;
  $("#openChat").disabled = !ready || !chatSupported;
  $("#openChat").title = chatSupported ? "Open Chat" : "This provider supports Work only; read-only Chat is not yet supported.";
  $("#providerWindows").disabled = !ready;
  $("#refreshBudgets").disabled = !ready;
  $("#chromeTheme").disabled = !ready;
  $("#notificationPreferences").disabled = !ready || notificationPermissionPending;
  $("#notificationPreferencesLabel").textContent = notificationPermissionLabel();
  $("#projectButton").disabled = !ready;
  $("#sendButton").classList.toggle("hidden", running);
  $("#sendButton").disabled = !ready || running || selectionSavePending || !$("#prompt").value.trim();
  $("#cancelButton").classList.toggle("hidden", !running);
  $("#cancelButton").disabled = Boolean(pendingMessage()?.cancel_requested);
}

function placeToast(node) {
  const openDialogs = document.querySelectorAll("dialog[open]");
  const activeDialog = openDialogs[openDialogs.length - 1];
  if (!node.classList.contains("show")) {
    if (node.parentElement !== document.body) document.body.append(node);
    if (typeof node.hidePopover === "function" && node.matches(":popover-open")) node.hidePopover();
    return;
  }
  if (activeDialog) {
    if (node.matches(":popover-open")) node.hidePopover();
    if (node.parentElement !== activeDialog) activeDialog.append(node);
    return;
  }
  if (node.parentElement !== document.body) document.body.append(node);
  if (typeof node.showPopover === "function" && !node.matches(":popover-open")) {
    node.showPopover();
  }
}
new MutationObserver(() => placeToast($("#toast"))).observe(document.body, {
  attributes: true, attributeFilter: ["open"], subtree: true,
});

function toast(message, variant = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.dataset.variant = variant;
  node.classList.add("show");
  placeToast(node);
  if (toastTimer !== null) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.classList.remove("show");
    if (node.parentElement !== document.body) document.body.append(node);
    if (typeof node.hidePopover === "function" && node.matches(":popover-open")) {
      node.hidePopover();
    }
    delete node.dataset.variant;
    toastTimer = null;
  }, 5200);
}

function choosePromptSuggestion() {
  const showForkSuggestion = promptSuggestionSelections === 0 || Math.random() < 1 / 15;
  promptSuggestionSelections += 1;
  const placeholder = showForkSuggestion ? FORK_PROMPT_SUGGESTION : DEFAULT_PROMPT_PLACEHOLDER;
  promptSuggestion = placeholder === FORK_PROMPT_SUGGESTION ? placeholder : "";
  $("#prompt").placeholder = placeholder;
  resizePrompt();
}

function acceptPromptSuggestion() {
  const prompt = $("#prompt");
  if (prompt.value.length || !promptSuggestion) return false;
  prompt.value = promptSuggestion;
  prompt.setSelectionRange(prompt.value.length, prompt.value.length);
  resizePrompt();
  return true;
}

function promptSuggestionEndRect() {
  if (!promptSuggestion) return null;
  const prompt = $("#prompt");
  const promptRect = prompt.getBoundingClientRect();
  const style = getComputedStyle(prompt);
  const mirror = document.createElement("div");
  Object.assign(mirror.style, {
    position: "fixed",
    visibility: "hidden",
    pointerEvents: "none",
    left: `${promptRect.left}px`,
    top: `${promptRect.top}px`,
    width: `${promptRect.width}px`,
    padding: style.padding,
    border: "0",
    boxSizing: "border-box",
    font: style.font,
    letterSpacing: style.letterSpacing,
    lineHeight: style.lineHeight,
    whiteSpace: "pre-wrap",
    overflowWrap: style.overflowWrap,
    wordBreak: style.wordBreak,
  });
  mirror.setAttribute("aria-hidden", "true");
  mirror.append(document.createTextNode(promptSuggestion));
  const marker = document.createElement("span");
  marker.textContent = "\u200b";
  mirror.append(marker);
  document.body.append(mirror);
  const markerRect = marker.getBoundingClientRect();
  mirror.remove();
  return markerRect;
}

function clickedAfterPromptSuggestion(event) {
  const end = promptSuggestionEndRect();
  if (!end) return false;
  return event.clientX >= end.left - 3 && event.clientX <= end.left + 36
    && event.clientY >= end.top - 4 && event.clientY <= end.bottom + 4;
}

async function applyBrowserTheme(theme) {
  const selected = theme?.active ? theme : { active: false };
  const root = document.documentElement;
  const body = document.body;
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
  body.classList.toggle("chrome-theme", selected.active);
  body.dataset.chromeTheme = selected.active ? `${selected.id}:${selected.version}` : "";
  $("#chromeThemeLabel").textContent = "Change theme";
  state.browser_theme = selected;
}

async function refreshBrowserTheme(notify = false) {
  const previous = document.body.dataset.chromeTheme || "";
  const theme = await api("/api/browser/theme");
  await applyBrowserTheme(theme);
  const current = document.body.dataset.chromeTheme || "";
  if (notify && current && current !== previous) {
    toast(`Applied ${theme.name || "Chrome theme"} to PilferedParrot.`);
  }
}

async function createChat(requestedModel = "") {
  if (createChatPending || selectionSavePending) return null;
  createChatPending = true;
  $("#newWorkSession").disabled = true;
  const provider = state.windowProvider;
  try {
    const chat = await api("/api/chats", {
      method: "POST",
      body: JSON.stringify({
        cwd: state.draftCwd || state.defaultCwd, provider, model: requestedModel || null,
        // The empty option is an explicit "Default" choice.  Do not revive a
        // stale draft effort when the user has selected it.
        reasoning_effort: activeChat() || $("#reasoningSelect").options.length
          ? $("#reasoningSelect").value || null : undefined,
      }),
    });
    state.chats.unshift(chat);
    state.activeId = chat.id;
    try { sessionStorage.setItem(ACTIVE_CHAT_SESSION_KEY, chat.id); } catch (_error) {}
    state.draftCwd = chat.cwd;
    $("#prompt").value = "";
    resizePrompt();
    render();
    $("#prompt").focus();
    return chat;
  } finally {
    createChatPending = false;
    if (state.initialized) $("#newWorkSession").disabled = false;
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const content = $("#prompt").value.trim();
  if (!state.initialized || !content || activeRunning() || createChatPending || selectionSavePending) return;
  if (pendingLaunchModel !== null) {
    openProjectDialog(true);
    return;
  }
  const selectedProvider = state.windowProvider;
  const selectedModel = $("#modelSelect").value;
  const reasoningEffort = $("#reasoningSelect").value || null;
  if (!activeChat()) {
    try {
      await createChat();
    } catch (error) {
      // Chat creation can fail before the normal message request (for example
      // when a provider rejects the selected workspace). Keep that failure
      // visible instead of leaving the submit promise rejected silently.
      toast(error.message);
      return;
    }
  }
  const chat = activeChat();
  const requestId = globalThis.crypto?.randomUUID?.()
    || `request-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const optimisticUser = {
    id: requestId, role: "user", content, created_at: Date.now() / 1000,
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
  choosePromptSuggestion();
  resizePrompt();
  render();
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
  try {
    const updated = await api(`/api/chats/${chat.id}/messages`, {
      method: "POST",
      body: JSON.stringify({
        content, provider: selectedProvider, model: selectedModel, cwd: state.draftCwd,
        reasoning_effort: reasoningEffort,
        request_id: requestId,
      }),
    });
    state.chats = state.chats.map((item) => item.id === updated.id ? updated : item);
    state.chats.sort((a, b) => b.updated_at - a.updated_at);
    // A Qwen request may start its local server. Update the dashboard as soon
    // as submission returns instead of waiting for the minute poll interval.
    refreshBudgets(false);
    schedulePoll();
  } catch (error) {
    try {
      if (error.responseReceived) throw error;
      await refreshState();
      const accepted = state.chats.find((item) => item.id === chat.id)?.messages
        ?.some((message) => message.id === requestId);
      if (!accepted) throw error;
      toast(pendingMessage(activeChat())
        ? "Connection recovered; the response is still running."
        : "Connection recovered; the response completed.");
      schedulePoll();
    } catch (_refreshError) {
      const current = state.chats.find((item) => item.id === chat.id) || chat;
      current.messages = current.messages.filter((message) =>
        message !== optimisticAssistant && message.id !== requestId);
      if (!$("#prompt").value) $("#prompt").value = content;
      resizePrompt();
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
  $("#sendButton").disabled = activeRunning() || selectionSavePending || !prompt.value.trim();
}

function applyServerState(initial) {
  const activeId = state.activeId;
  const draftCwd = state.draftCwd;
  const requestedProvider = activeChat()?.requested_provider;
  const requestedModel = activeChat()?.requested_model;
  Object.assign(state, initial);
  state.windowId = initial.window_id || state.windowId;
  state.windowProvider = initial.window_provider || state.windowProvider;
  const chats = visibleChats();
  state.activeId = chats.some((chat) => chat.id === activeId)
    ? activeId : chats[0]?.id || null;
  try {
    if (state.activeId) sessionStorage.setItem(ACTIVE_CHAT_SESSION_KEY, state.activeId);
  } catch (_error) {}
  state.draftCwd = draftCwd || activeChat()?.cwd || state.defaultCwd;
  if (state.activeId === activeId && requestedProvider && !activeRunning()) {
    activeChat().requested_provider = requestedProvider;
    activeChat().requested_model = requestedModel || null;
  }
}

async function refreshState() {
  const sequence = ++stateRequestSequence;
  const conversation = $("#conversation");
  const previousScrollTop = conversation.scrollTop;
  const followOutput = conversation.scrollHeight - conversation.scrollTop
    - conversation.clientHeight < 120;
  const initial = await api("/api/state");
  if (sequence < stateAppliedSequence || selectionSavePending) return;
  stateAppliedSequence = sequence;
  applyServerState(initial);
  render();
  conversation.scrollTop = followOutput ? conversation.scrollHeight : previousScrollTop;
}

function schedulePoll() {
  if (!anyRunning() || pollTimer !== null) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    const wasRunning = anyRunning();
    try {
      const runningIds = state.chats.filter((chat) => pendingMessage(chat)).map((chat) => chat.id);
      const updates = await Promise.all(runningIds.map((chatId) =>
        api(`/api/chats/${encodeURIComponent(chatId)}`)));
      const completed = updates.filter((chat) => !pendingMessage(chat));
      const changedActive = updates.some((chat) => chat.id === state.activeId);
      const byId = new Map(updates.map((chat) => [chat.id, chat]));
      state.chats = state.chats.map((chat) => byId.get(chat.id) || chat)
        .sort((a, b) => b.updated_at - a.updated_at);
      if (changedActive) render();
      else renderChats();
      // Refresh the provider card on the pending -> completed edge. This is
      // especially important for Qwen, whose auto-started endpoint may stop
      // again immediately after a turn completes.
      if (wasRunning && completed.length) {
        completed.forEach((chat) => notifyCompletion(
          `${providerLabel(chat.requested_provider || chat.provider || state.windowProvider)} finished ${chat.title || "a work session"}.`,
          `pilferedparrot-work-${chat.id}`,
        ));
        await refreshBudgets(false);
      }
    }
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

function runTerminalCommand(button) {
  const chat = activeChat();
  if (!chat) return;
  const command = button.closest(".code-block")?.querySelector("code")?.textContent || "";
  terminalTarget = {
    button,
    chatId: chat.id,
    messageId: button.dataset.messageId,
    blockIndex: Number(button.dataset.blockIndex),
  };
  $("#terminalCwd").textContent = chat.cwd;
  $("#terminalCommand").textContent = command;
  $("#terminalDialog").showModal();
}

async function confirmTerminalCommand() {
  const target = terminalTarget;
  if (!target) return;
  target.button.disabled = true;
  $("#confirmTerminal").disabled = true;
  // Restore browser focus before the native terminal opens, so closing the
  // dialog cannot subsequently bring PPI back in front of the password prompt.
  $("#terminalDialog").close();
  try {
    await api(`/api/chats/${encodeURIComponent(target.chatId)}/terminal`, {
      method: "POST",
      body: JSON.stringify({
        message_id: target.messageId,
        block_index: target.blockIndex,
      }),
    });
    toast("Opened command in a terminal.");
  } catch (error) {
    terminalTarget = target;
    $("#terminalDialog").showModal();
    toast(error.message);
  } finally {
    if (target.button.isConnected) target.button.disabled = false;
    $("#confirmTerminal").disabled = false;
  }
}

async function init() {
  try {
    restorePaneWidths();
    const initial = await api("/api/state");
    Object.assign(state, initial);
    if (fragmentCwd) state.defaultCwd = fragmentCwd;
    state.windowId = initial.window_id || state.windowId;
    state.windowProvider = initial.window_provider || fragmentProvider
      || initial.default_provider || "codex";
    await api("/api/window/open", {
      method: "POST", body: JSON.stringify({ document_id: documentId }),
    });
    state.initialized = true;
    await refreshBrowserTheme(false).catch(() => {});
    if (launchedFromApp) {
      state.activeId = null;
      state.draftCwd = fragmentCwd || state.defaultCwd;
      if (fragmentPickProject) {
        pendingLaunchModel = fragmentModel;
        openProjectDialog(true);
      } else {
        await createChat(fragmentModel);
      }
    } else {
      let savedActiveId = "";
      try { savedActiveId = sessionStorage.getItem(ACTIVE_CHAT_SESSION_KEY) || ""; } catch (_error) {}
      const chats = visibleChats();
      state.activeId = chats.some((chat) => chat.id === savedActiveId)
        ? savedActiveId : latestUsedChat(chats)?.id || null;
      state.draftCwd = activeChat()?.cwd || state.defaultCwd;
      if (!state.activeId) await createChat(fragmentModel);
    }
    render();
    schedulePoll();
    await refreshBudgets(true);
    scheduleBudgetPoll();
  } catch (error) {
    toast(error.message);
  }
}

async function openChatWindow() {
  const width = CHAT_WINDOW_WIDTH;
  const height = CHAT_WINDOW_HEIGHT;
  const left = Math.max(0, (Number(screen.availLeft) || 0) + screen.availWidth - width - 28);
  const top = Math.max(0, (Number(screen.availTop) || 0) + 28);
  const model = $("#modelSelect").value || preferredModel(state.windowProvider);
  try {
    await api("/api/chat/window", {
      method: "POST", body: JSON.stringify({
        provider: state.windowProvider, model: model,
        cwd: state.draftCwd || activeChat()?.cwd || state.defaultCwd,
        width, height, left, top,
      }),
    });
  } catch (error) { toast(error.message); }
}

async function openProviderWindow(provider, model = "") {
  const width = Math.max(900, Math.min(1280, (Number(screen.availWidth) || 1200) - 120));
  const height = Math.max(650, Math.min(900, (Number(screen.availHeight) || 800) - 100));
  const left = Math.max(0, (Number(screen.availLeft) || 0) + 54);
  const top = Math.max(0, (Number(screen.availTop) || 0) + 42);
  await api("/api/provider/window", {
    method: "POST", body: JSON.stringify({
      provider, model: model || null,
      cwd: state.draftCwd || activeChat()?.cwd || state.defaultCwd,
      width, height, left, top,
    }),
  });
  toast(`Opened ${providerLabel(provider)} in another window.`);
}

function beginProviderDraft() {
  const first = state.provider_templates?.[0];
  state.providerDraft = first ? { template: first.id } : {};
  renderProviderConnections();
}

async function submitProviderDraft() {
  const draft = state.providerDraft || {};
  const read = (name) => document.querySelector(`[data-provider-draft-${name}]`)?.value.trim() || "";
  const template = document.querySelector("[data-provider-draft-template]")?.value || draft.template || "";
  if (!template) throw new Error("Choose a provider template.");
  await api("/api/providers", { method: "POST", body: JSON.stringify({
    template, label: read("label"), model: read("model"),
    base_url: read("base-url"), api_key_env: read("api-key-env"),
  }) });
  state.providerDraft = null;
  await refreshState();
  toast("Provider added. API secrets remain in your environment.");
}

async function removeProvider(provider) {
  if (!window.confirm(`Remove ${providerLabel(provider)}? You can restore it from Add provider.`)) return;
  await api("/api/providers/remove", { method: "POST", body: JSON.stringify({ provider }) });
  await refreshState();
  toast(`Removed ${providerLabel(provider)}.`);
}

async function launchProviderLogin(provider) {
  const result = await api(`/api/providers/${provider}/login`, { method: "POST", body: "{}" });
  state.authPending[provider] = true;
  state.authConfirmation[provider] = Boolean(result.confirmation_code);
  renderProviderConnections();
  toast(result.launched === false
    ? `${providerLabel(provider)} sign-in is already open.`
    : `${providerLabel(provider)} sign-in opened in your default browser.`);
  watchProviderLogin(provider);
}

async function requestProviderLogin(provider) {
  state.authPending[provider] = true;
  state.authCodes[provider] = "";
  renderProviderConnections();
  try {
    await launchProviderLogin(provider);
  } catch (error) {
    delete state.authPending[provider];
    delete state.authConfirmation[provider];
    delete state.authCodes[provider];
    renderProviderConnections();
    throw error;
  }
}

async function submitProviderAuthCode(provider) {
  const code = (state.authCodes[provider] || "").trim();
  await api(`/api/providers/${provider}/code`, {
    method: "POST", body: JSON.stringify({ code }),
  });
  state.authCodes[provider] = "";
  renderProviderConnections();
  toast(`${providerLabel(provider)} confirmation sent. Finishing sign-in…`);
}

async function watchProviderLogin(provider, attempt = 0) {
  if (!state.authPending[provider]) return;
  if (attempt >= 200) {
    delete state.authPending[provider];
    delete state.authConfirmation[provider];
    delete state.authCodes[provider];
    renderProviderConnections();
    toast(`${providerLabel(provider)} sign-in was not detected. Select Sign in to try again.`);
    return;
  }
  await new Promise((resolve) => setTimeout(resolve, 1500));
  await refreshBudgets(false);
  if (state.budgets[provider]?.auth_status === "signed_in") {
    delete state.authPending[provider];
    delete state.authConfirmation[provider];
    delete state.authCodes[provider];
    renderProviderConnections();
    toast(`${providerLabel(provider)} is ready. Choose a model, then select Use ${providerLabel(provider)}.`);
    return;
  }
  watchProviderLogin(provider, attempt + 1);
}

function requestProviderLogout(provider) {
  providerLogoutTarget = provider;
  $("#providerLogoutNote").textContent = `This signs you out of ${providerLabel(provider)} in every PilferedParrot window that uses it. Other providers stay signed in.`;
  $("#providerLogoutDialog").showModal();
}

async function confirmProviderLogout() {
  const provider = providerLogoutTarget;
  if (!provider) return;
  $("#confirmProviderLogout").disabled = true;
  try {
    await api(`/api/providers/${provider}/logout`, { method: "POST", body: "{}" });
    $("#providerLogoutDialog").close();
    await refreshBudgets(false);
    toast(`Signed out of ${providerLabel(provider)}.`);
  } catch (error) {
    toast(error.message);
  } finally {
    $("#confirmProviderLogout").disabled = false;
  }
}

async function openChromeThemeGallery() {
  const button = $("#chromeTheme");
  button.disabled = true;
  try {
    await api("/api/browser/theme", { method: "POST", body: "{}" });
    toast("Choose Add to Chrome for this private PilferedParrot window, then return here.");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

$("#composer").addEventListener("submit", sendMessage);
$("#prompt").addEventListener("input", resizePrompt);
$("#prompt").addEventListener("click", (event) => {
  if (clickedAfterPromptSuggestion(event)) acceptPromptSuggestion();
});
$("#prompt").addEventListener("keydown", (event) => {
  if (event.isComposing) return;
  if (event.key === "ArrowRight" && acceptPromptSuggestion()) {
    event.preventDefault();
    return;
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#composer").requestSubmit();
  }
});
$("#newWorkSession").addEventListener("click", () => {
  if (pendingLaunchModel !== null) {
    openProjectDialog(true);
    return;
  }
  createChat($("#modelSelect").value || preferredModel(state.windowProvider))
    .then(choosePromptSuggestion)
    .catch((error) => toast(error.message));
});
$("#providerWindows").addEventListener("click", () => {
  renderProviderConnections();
  $("#providerDialog").showModal();
});
$("#refreshProviderDashboard").addEventListener("click", async () => {
  const button = $("#refreshProviderDashboard");
  const feedback = $("#providerDashboardStatus");
  const label = button.innerHTML;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "Checking…";
  feedback.textContent = "Checking provider status…";
  try {
    const budgets = await refreshBudgets(false);
    feedback.textContent = budgets ? "Status refreshed." : "Could not refresh status. Try again.";
  } finally {
    button.innerHTML = label;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});
$("#cancelButton").addEventListener("click", cancelMessage);
$("#openChat").addEventListener("click", openChatWindow);
$("#notificationPreferences").addEventListener("click", () => {
  manageNotificationPermission().catch((error) => toast(error.message, "error"));
});
$("#chromeTheme").addEventListener("click", openChromeThemeGallery);
setupPaneResizer("#sidebarResizer", "sidebar");
$("#messages").addEventListener("click", (event) => {
  const button = event.target.closest("[data-run-command]");
  if (button) runTerminalCommand(button);
});
async function saveWorkSelection(modelChanged = false) {
  if (selectionSavePending || activeRunning()) return;
  const chat = activeChat();
  const model = $("#modelSelect").value;
  const selected = $("#reasoningSelect").value || null;
  const effort = reasoningOptions(state.windowProvider, model).includes(selected) ? selected : null;
  const previousModel = chat?.requested_model;
  selectionSavePending = true;
  $("#modelSelect").disabled = true;
  $("#reasoningSelect").disabled = true;
  $("#sendButton").disabled = true;
  $("#newWorkSession").disabled = true;
  stateAppliedSequence = ++stateRequestSequence;
  try {
    if (modelChanged && model) {
      state.preferences = await api("/api/preferences/provider", {
        method: "POST", body: JSON.stringify({ provider: state.windowProvider, model }),
      });
    }
    if (chat) {
      const updated = await api(`/api/chats/${chat.id}/reasoning`, {
        method: "POST", body: JSON.stringify({ model, reasoning_effort: effort }),
      });
      state.chats = state.chats.map((item) => item.id === updated.id ? updated : item);
    } else {
      draftReasoningEffort = effort;
    }
    if (modelChanged && selected && !effort) toast("Reasoning reset to default for this model.");
  } catch (error) {
    if (chat) chat.requested_model = previousModel;
    toast(error.message);
  } finally {
    stateAppliedSequence = ++stateRequestSequence;
    selectionSavePending = false;
    render();
  }
}
$("#modelSelect").addEventListener("change", () => saveWorkSelection(true));
$("#reasoningSelect").addEventListener("change", () => saveWorkSelection());
$("#modelSelect").addEventListener("pointerdown", (event) => {
  pollProviderModels(state.windowProvider, event.currentTarget);
});
$("#modelSelect").addEventListener("keydown", (event) => {
  if (["Enter", " ", "ArrowDown"].includes(event.key)) {
    pollProviderModels(state.windowProvider, event.currentTarget);
  }
});
$("#refreshBudgets").addEventListener("click", async () => {
  const button = $("#refreshBudgets");
  button.classList.add("refreshing");
  button.setAttribute("aria-busy", "true");
  try { await refreshBudgets(true); }
  finally {
    button.classList.remove("refreshing");
    button.removeAttribute("aria-busy");
  }
});
function openProjectDialog(needsChoice) {
  $("#projectNotice").hidden = !needsChoice;
  $("#projectInput").value = needsChoice ? "" : state.draftCwd;
  updateProjectFolderName();
  $("#projectDialog").showModal();
}
function projectFolderName(path) {
  const value = String(path || "").trim();
  if (!value) return "No folder selected";
  const withoutTrailingSeparators = value.replace(/[\\/]+$/, "");
  if (!withoutTrailingSeparators) return value;
  const segments = withoutTrailingSeparators.split(/[\\/]/);
  return segments[segments.length - 1] || withoutTrailingSeparators;
}
function updateProjectFolderName() {
  const input = $("#projectInput");
  const name = $("#projectFolderName");
  const path = input.value.trim();
  name.textContent = projectFolderName(path);
  name.title = path;
}
$("#projectInput").addEventListener("input", updateProjectFolderName);
$("#browseProject").addEventListener("click", async () => {
  const button = $("#browseProject");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try {
    const selected = await api("/api/project/folder", {
      method: "POST",
      body: JSON.stringify({
        cwd: $("#projectInput").value.trim() || state.draftCwd || state.defaultCwd,
      }),
    });
    if (selected.path) {
      $("#projectInput").value = selected.path;
      updateProjectFolderName();
      $("#projectInput").focus();
    }
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});
$("#projectButton").addEventListener("click", () => {
  if (activeChat()?.messages?.length) {
    toast("Start a new work session to change projects.");
    return;
  }
  openProjectDialog(pendingLaunchModel !== null);
});
$("#projectForm").addEventListener("submit", async (event) => {
  if (projectSubmitPending) {
    event.preventDefault();
    return;
  }
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  const chosen = $("#projectInput").value.trim();
  // A launch that is still waiting for a folder has no default worth falling
  // back to: the inherited one is precisely what this provider refused.
  if (pendingLaunchModel !== null && !chosen) {
    toast("Enter a project folder for this provider.");
    return;
  }
  state.draftCwd = chosen || state.defaultCwd;
  if (pendingLaunchModel !== null) {
    const model = pendingLaunchModel;
    const saveButton = $("#saveProject");
    const launchDraft = $("#prompt").value;
    projectSubmitPending = true;
    saveButton.disabled = true;
    try {
      await createChat(model);
      pendingLaunchModel = null;
      if (launchDraft) {
        $("#prompt").value = launchDraft;
        resizePrompt();
        render();
      }
    } catch (error) {
      toast(error.message);
      return;
    } finally {
      projectSubmitPending = false;
      saveButton.disabled = false;
    }
  }
  $("#projectDialog").close();
  renderHeader();
});
$("#terminalForm").addEventListener("submit", (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  confirmTerminalCommand();
});
$("#terminalDialog").addEventListener("close", () => {
  if (!$("#terminalDialog").open) terminalTarget = null;
});
$("#providerConnectionList").addEventListener("click", async (event) => {
  const windowButton = event.target.closest("[data-provider-window]");
  const login = event.target.closest("[data-provider-login]");
  const code = event.target.closest("[data-provider-code]");
  const logout = event.target.closest("[data-provider-logout]");
  const remove = event.target.closest("[data-provider-remove]");
  const draftSubmit = event.target.closest("[data-provider-draft-submit]");
  const draftCancel = event.target.closest("[data-provider-draft-cancel]");
  try {
    if (windowButton) {
      const provider = windowButton.dataset.providerWindow;
      await openProviderWindow(provider, providerModelChoice(provider));
      $("#providerDialog").close();
    } else if (login) {
      await requestProviderLogin(login.dataset.providerLogin);
    } else if (code) {
      await submitProviderAuthCode(code.dataset.providerCode);
    } else if (logout) {
      requestProviderLogout(logout.dataset.providerLogout);
    } else if (remove) {
      await removeProvider(remove.dataset.providerRemove);
    } else if (draftSubmit) {
      draftSubmit.disabled = true;
      await submitProviderDraft();
    } else if (draftCancel) {
      state.providerDraft = null;
      renderProviderConnections();
    }
  } catch (error) {
    if (draftSubmit) draftSubmit.disabled = false;
    toast(error.message);
  }
});
$("#providerConnectionList").addEventListener("input", (event) => {
  const input = event.target.closest("[data-provider-auth-code]");
  if (!input) return;
  const provider = input.dataset.providerAuthCode;
  state.authCodes[provider] = input.value;
  const submit = $("#providerConnectionList").querySelector(`[data-provider-code="${provider}"]`);
  if (submit) submit.disabled = !input.value.trim();
});
$("#providerConnectionList").addEventListener("change", async (event) => {
  const select = event.target.closest("[data-provider-model]");
  if (select) {
    const provider = select.dataset.providerModel;
    state.providerModels[provider] = select.value;
    if (select.value) {
      try {
        state.preferences = await api("/api/preferences/provider", {
          method: "POST", body: JSON.stringify({ provider, model: select.value }),
        });
      } catch (error) { toast(error.message); }
    }
  }
});
$("#providerConnectionList").addEventListener("pointerdown", (event) => {
  const select = event.target.closest("[data-provider-model]");
  if (select) pollProviderModels(select.dataset.providerModel, select);
});
$("#providerConnectionList").addEventListener("keydown", (event) => {
  const select = event.target.closest("[data-provider-model]");
  if (select && ["Enter", " ", "ArrowDown"].includes(event.key)) {
    pollProviderModels(select.dataset.providerModel, select);
  }
});
$("#addProvider").addEventListener("click", beginProviderDraft);
$("#providerConnectionList").addEventListener("change", (event) => {
  const template = event.target.closest("[data-provider-draft-template]");
  if (template) {
    state.providerDraft = { template: template.value };
    renderProviderConnections();
  }
});
$("#providerLogoutForm").addEventListener("submit", (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  confirmProviderLogout();
});
$("#providerLogoutDialog").addEventListener("close", () => { providerLogoutTarget = null; });
function setSidebarOpen(open) {
  const sidebar = $("#sidebar");
  const wasOpen = sidebar.classList.contains("open");
  sidebar.classList.toggle("open", open);
  syncSidebarAccessibility();
  const currentMobile = matchMedia("(max-width: 760px)").matches;
  if (open) $("#closeSidebar").focus();
  else if (wasOpen && currentMobile) $("#openSidebar").focus();
}
function syncSidebarAccessibility() {
  const isMobile = matchMedia("(max-width: 760px)").matches;
  const sidebarOpen = $("#sidebar").classList.contains("open");
  $("#openSidebar").setAttribute("aria-expanded", String(sidebarOpen && isMobile));
  $(".main").inert = sidebarOpen && isMobile;
}
$("#openSidebar").addEventListener("click", () => setSidebarOpen(true));
$("#closeSidebar").addEventListener("click", () => setSidebarOpen(false));
window.addEventListener("resize", syncSidebarAccessibility);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("#sidebar").classList.contains("open")) {
    setSidebarOpen(false);
    $("#openSidebar").focus();
  }
});
window.addEventListener("focus", () => {
  setTimeout(() => refreshBrowserTheme(true).catch(() => {}), 250);
  setTimeout(() => refreshBrowserTheme(true).catch(() => {}), 1200);
  setTimeout(() => refreshBudgets(false), 400);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { refreshBudgets(false); scheduleBudgetPoll(); }
});
window.addEventListener("pagehide", () => {
  if (!state.capability) return;
  fetch("/api/window/close", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-PilferedParrot-Capability": state.capability,
    },
    body: JSON.stringify({ document_id: documentId }),
    keepalive: true,
  }).catch(() => {});
});
choosePromptSuggestion();
init();
