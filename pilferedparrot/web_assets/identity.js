(function installPilferedParrotIdentity(global) {
  "use strict";

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[character]));
  }

  const destinationLabels = {
    loopback: "Local machine (loopback)",
    "local-network": "Local network",
    remote: "Public network address",
    cli: "Provider CLI",
    unknown: "Hostname; location unverified",
  };

  function captureState(container) {
    const state = new Map();
    if (!container) return state;
    container.querySelectorAll(".response-identity[data-response-identity]").forEach((panel) => {
      const key = panel.dataset.responseIdentity;
      if (!key) return;
      state.set(key, {
        open: panel.open,
        focused: document.activeElement === panel.querySelector("summary"),
      });
    });
    return state;
  }

  function restoreState(container, state) {
    if (!container || !state) return;
    container.querySelectorAll(".response-identity[data-response-identity]").forEach((panel) => {
      const saved = state.get(panel.dataset.responseIdentity);
      if (!saved) return;
      panel.open = saved.open;
      if (saved.focused) panel.querySelector("summary")?.focus();
    });
  }

  function render(message) {
    if (!message || message.role !== "assistant" || message.pending || !message.response_identity) return "";
    const evidence = message.response_identity;
    if (!evidence || typeof evidence !== "object") return "";
    const requested = evidence.requested_model || message.model || "Provider-selected model";
    const kind = destinationLabels[evidence.endpoint_kind] || destinationLabels.unknown;
    const origin = evidence.endpoint_origin ? ` · ${escapeHtml(evidence.endpoint_origin)}` : "";
    const reported = Array.isArray(evidence.reported_models)
      ? evidence.reported_models.filter((model) => typeof model === "string" && model.trim()) : [];
    const report = reported.length ? reported.map(escapeHtml).join(", ") : "Not reported";
    const provider = evidence.provider ? `<div><dt>Provider</dt><dd>${escapeHtml(evidence.provider)}</dd></div>` : "";
    const destinationNote = evidence.endpoint_kind === "cli"
      ? "PPI does not capture a server-reported model from this CLI."
      : "This is where PPI sent the request. A local or network destination may forward it elsewhere, so this does not prove where the response was produced.";
    const verificationNote = reported.length
      ? "Requested and server-reported names may use different aliases or model-file paths. These identifiers alone cannot verify which model produced the response."
      : "";
    return `<details class="response-identity" data-response-identity="${escapeHtml(message.id || "")}">
      <summary>Response details</summary>
      <dl>
        ${provider}
        <div><dt>Requested model</dt><dd>${escapeHtml(requested)}</dd></div>
        <div><dt>Destination</dt><dd>${escapeHtml(kind)}${origin}</dd></div>
        <div><dt>Server-reported model${reported.length === 1 ? "" : "s"}</dt><dd>${report}</dd></div>
      </dl>
      <p class="response-identity-note">${escapeHtml(destinationNote)}</p>
      ${verificationNote ? `<p class="response-identity-note">${escapeHtml(verificationNote)}</p>` : ""}
    </details>`;
  }

  global.PilferedParrotIdentity = { captureState, render, restoreState };
})(globalThis);
