const attentionEsc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function attentionApi(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = payload?.detail?.message || payload?.detail || detail;
    } catch {
      // Preserve HTTP status text.
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

function attentionTime(value) {
  if (!value) return "—";
  return new Date(Number(value) * 1000).toLocaleString();
}

function installAttentionStyles() {
  if (document.getElementById("attention-ui-styles")) return;
  const style = document.createElement("style");
  style.id = "attention-ui-styles";
  style.textContent = `
    .attention-launch { position: relative; }
    .attention-count {
      display: inline-flex; min-width: 1.2rem; height: 1.2rem; padding: 0 .3rem;
      border-radius: 999px; align-items: center; justify-content: center; font-size: .72rem;
      margin-left: .25rem; background: color-mix(in srgb, currentColor 12%, transparent);
    }
    .attention-dialog { width: min(980px, calc(100vw - 2rem)); max-height: calc(100vh - 2rem); padding: 0; }
    .attention-dialog::backdrop { background: rgba(0, 0, 0, .42); }
    .attention-shell { display: grid; grid-template-rows: auto auto 1fr; max-height: calc(100vh - 2rem); }
    .attention-head, .attention-toolbar {
      display: flex; align-items: center; justify-content: space-between; gap: .75rem; padding: .8rem 1rem;
      border-bottom: 1px solid color-mix(in srgb, currentColor 14%, transparent);
    }
    .attention-toolbar { justify-content: flex-start; flex-wrap: wrap; }
    .attention-status { margin-left: auto; font-size: .82rem; opacity: .78; }
    .attention-status[data-error="true"] { color: var(--danger, #b42318); opacity: 1; }
    .attention-list { overflow: auto; padding: 1rem; display: grid; gap: .8rem; }
    .attention-card {
      border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
      border-radius: .65rem; padding: .85rem; background: color-mix(in srgb, currentColor 3%, transparent);
    }
    .attention-card[data-severity="critical"] { border-left: 4px solid var(--danger, #b42318); }
    .attention-card-head { display: flex; justify-content: space-between; gap: .75rem; flex-wrap: wrap; }
    .attention-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: .35rem .9rem; margin-top: .55rem; }
    .attention-actions { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; margin-top: .75rem; }
    .attention-pill {
      display: inline-flex; align-items: center; padding: .12rem .5rem; border-radius: 999px;
      border: 1px solid color-mix(in srgb, currentColor 20%, transparent); font-size: .76rem;
    }
    .attention-empty { opacity: .72; padding: 1rem; }
  `;
  document.head.appendChild(style);
}

function attentionCard(item) {
  const actionable = !["resolved", "expired", "superseded"].includes(item.status);
  const link = item.deep_link
    ? `<a href="${attentionEsc(item.deep_link)}">Open source</a>`
    : "";
  return `
    <article class="attention-card" data-attention-id="${attentionEsc(item.id)}" data-severity="${attentionEsc(item.severity)}">
      <div class="attention-card-head">
        <div>
          <strong>${attentionEsc(item.reason)}</strong>
          <div><code>${attentionEsc(item.id)}</code></div>
        </div>
        <div>
          <span class="attention-pill">${attentionEsc(item.severity)}</span>
          <span class="attention-pill">${attentionEsc(item.status)}</span>
        </div>
      </div>
      <div class="attention-meta">
        <span><strong>Type:</strong> ${attentionEsc(item.type)}</span>
        <span><strong>Source:</strong> ${attentionEsc(item.source?.object_type)} / ${attentionEsc(item.source?.object_id)}</span>
        <span><strong>Due:</strong> ${attentionEsc(attentionTime(item.due_at))}</span>
        <span><strong>Expires:</strong> ${attentionEsc(attentionTime(item.expires_at))}</span>
        <span><strong>Owner:</strong> ${attentionEsc(item.owner_identity_id || "—")}</span>
        <span><strong>Escalations:</strong> ${attentionEsc(item.escalation_count || 0)}</span>
      </div>
      <div class="attention-actions">
        ${actionable && item.status !== "acknowledged" ? '<button type="button" class="ghost-button" data-attention-action="acknowledge">Acknowledge</button>' : ""}
        ${actionable ? '<button type="button" class="ghost-button" data-attention-action="snooze">Snooze</button>' : ""}
        ${actionable ? '<button type="button" class="ghost-button" data-attention-action="resolve">Resolve</button>' : ""}
        ${link}
      </div>
    </article>
  `;
}

function filteredAttention(items, status, severity) {
  return items.filter((item) => {
    const statusMatch = status === "active"
      ? !["resolved", "expired", "superseded"].includes(item.status)
      : (status === "all" || item.status === status);
    return statusMatch && (severity === "all" || item.severity === severity);
  });
}

async function mutateAttention(dialog, itemId, action) {
  const status = dialog.querySelector("[data-attention-status]");
  status.dataset.error = "false";
  try {
    let body;
    if (action === "resolve") {
      const reason = window.prompt("Resolution reason (optional)", "") ?? "";
      body = JSON.stringify({ reason: reason.trim() || null });
    } else if (action === "snooze") {
      const minutesText = window.prompt("Snooze for how many minutes?", "30");
      if (minutesText === null) return;
      const minutes = Number(minutesText);
      if (!Number.isFinite(minutes) || minutes <= 0) throw new Error("Enter a positive number of minutes");
      body = JSON.stringify({ until: (Date.now() / 1000) + (minutes * 60) });
    }
    status.textContent = `Applying ${action}…`;
    await attentionApi(`/api/attention/${encodeURIComponent(itemId)}/${action}`, {
      method: "POST",
      ...(body ? { body } : {}),
    });
    await loadAttention(dialog);
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `Action failed: ${error.message}`;
  }
}

async function loadAttention(dialog) {
  const status = dialog.querySelector("[data-attention-status]");
  const list = dialog.querySelector("[data-attention-list]");
  status.dataset.error = "false";
  status.textContent = "Loading canonical attention…";
  try {
    const payload = await attentionApi("/api/attention");
    const all = payload.attention_items || [];
    const statusFilter = dialog.querySelector("[data-attention-filter]").value;
    const severityFilter = dialog.querySelector("[data-attention-severity]").value;
    const items = filteredAttention(all, statusFilter, severityFilter);
    list.innerHTML = items.length ? items.map(attentionCard).join("") : '<div class="attention-empty">No matching AttentionItems.</div>';
    list.querySelectorAll("[data-attention-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const card = button.closest("[data-attention-id]");
        mutateAttention(dialog, card.dataset.attentionId, button.dataset.attentionAction);
      });
    });
    const active = all.filter((item) => !["resolved", "expired", "superseded"].includes(item.status)).length;
    const badge = document.querySelector("[data-attention-count]");
    if (badge) {
      badge.textContent = String(active);
      badge.hidden = active === 0;
    }
    status.textContent = `${all.length} total · ${active} active · deterministic refresh only`;
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `Inbox unavailable: ${error.message}`;
    list.innerHTML = '<div class="attention-empty">Canonical attention state could not be loaded.</div>';
  }
}

function installAttention() {
  if (document.querySelector("[data-attention-launch]")) return;
  installAttentionStyles();
  const controls = document.querySelector(".topbar .controls");
  if (!controls) return;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost-button attention-launch";
  button.dataset.attentionLaunch = "true";
  button.innerHTML = 'Inbox <span class="attention-count" data-attention-count hidden>0</span>';

  const dialog = document.createElement("dialog");
  dialog.className = "attention-dialog";
  dialog.innerHTML = `
    <div class="attention-shell">
      <div class="attention-head">
        <div>
          <strong>Inbox / Attention</strong>
          <div><small>Canonical human-intervention queue; source domains remain authoritative</small></div>
        </div>
        <button type="button" class="ghost-button" data-attention-close>Close</button>
      </div>
      <div class="attention-toolbar">
        <label>Status
          <select data-attention-filter>
            <option value="active">Active</option>
            <option value="all">All</option>
            <option value="open">Open</option>
            <option value="acknowledged">Acknowledged</option>
            <option value="snoozed">Snoozed</option>
            <option value="escalated">Escalated</option>
            <option value="resolved">Resolved</option>
          </select>
        </label>
        <label>Severity
          <select data-attention-severity>
            <option value="all">All</option>
            <option value="critical">Critical</option>
            <option value="high">High</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
          </select>
        </label>
        <button type="button" class="ghost-button" data-attention-refresh>Refresh</button>
        <span class="attention-status" data-attention-status aria-live="polite"></span>
      </div>
      <div class="attention-list" data-attention-list></div>
    </div>
  `;

  controls.insertBefore(button, controls.firstChild);
  document.body.appendChild(dialog);

  button.addEventListener("click", () => {
    dialog.showModal();
    loadAttention(dialog);
  });
  dialog.querySelector("[data-attention-close]").addEventListener("click", () => dialog.close());
  dialog.querySelector("[data-attention-refresh]").addEventListener("click", () => loadAttention(dialog));
  dialog.querySelector("[data-attention-filter]").addEventListener("change", () => loadAttention(dialog));
  dialog.querySelector("[data-attention-severity]").addEventListener("change", () => loadAttention(dialog));

  attentionApi("/api/attention").then((payload) => {
    const active = (payload.attention_items || []).filter((item) => !["resolved", "expired", "superseded"].includes(item.status)).length;
    const badge = button.querySelector("[data-attention-count]");
    badge.textContent = String(active);
    badge.hidden = active === 0;
  }).catch(() => {});
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", installAttention, { once: true });
} else {
  installAttention();
}
