const esc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function api(path, options = {}) {
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
      // Keep HTTP status text.
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

function formatTime(value) {
  if (!value) return "—";
  return new Date(Number(value) * 1000).toLocaleString();
}

function installStyles() {
  if (document.getElementById("approval-request-ui-styles")) return;
  const style = document.createElement("style");
  style.id = "approval-request-ui-styles";
  style.textContent = `
    .approval-launch { position: relative; }
    .approval-count {
      display: inline-flex; min-width: 1.2rem; height: 1.2rem; padding: 0 .3rem;
      border-radius: 999px; align-items: center; justify-content: center; font-size: .72rem;
      margin-left: .25rem; background: color-mix(in srgb, currentColor 12%, transparent);
    }
    .approval-dialog { width: min(980px, calc(100vw - 2rem)); max-height: calc(100vh - 2rem); padding: 0; }
    .approval-dialog::backdrop { background: rgba(0, 0, 0, .42); }
    .approval-shell { display: grid; grid-template-rows: auto auto 1fr; max-height: calc(100vh - 2rem); }
    .approval-head, .approval-toolbar {
      display: flex; align-items: center; justify-content: space-between; gap: .75rem; padding: .8rem 1rem;
      border-bottom: 1px solid color-mix(in srgb, currentColor 14%, transparent);
    }
    .approval-toolbar { justify-content: flex-start; flex-wrap: wrap; }
    .approval-toolbar select { min-width: 170px; }
    .approval-status { margin-left: auto; font-size: .82rem; opacity: .78; }
    .approval-status[data-error="true"] { color: var(--danger, #b42318); opacity: 1; }
    .approval-list { overflow: auto; padding: 1rem; display: grid; gap: .8rem; }
    .approval-card {
      border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
      border-radius: .65rem; padding: .85rem; background: color-mix(in srgb, currentColor 3%, transparent);
    }
    .approval-card-head { display: flex; justify-content: space-between; gap: .75rem; flex-wrap: wrap; }
    .approval-card code { overflow-wrap: anywhere; }
    .approval-meta { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: .35rem .9rem; margin-top: .55rem; }
    .approval-target, .approval-decisions { margin-top: .7rem; padding-top: .6rem; border-top: 1px solid color-mix(in srgb, currentColor 12%, transparent); }
    .approval-actions { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .75rem; }
    .approval-pill {
      display: inline-flex; align-items: center; padding: .12rem .5rem; border-radius: 999px;
      border: 1px solid color-mix(in srgb, currentColor 20%, transparent); font-size: .76rem;
    }
    .approval-empty { opacity: .72; padding: 1rem; }
    @media (max-width: 700px) {
      .approval-dialog { width: calc(100vw - .75rem); max-height: calc(100vh - .75rem); }
      .approval-shell { max-height: calc(100vh - .75rem); }
      .approval-head, .approval-toolbar, .approval-list { padding-left: .65rem; padding-right: .65rem; }
    }
  `;
  document.head.appendChild(style);
}

function decisionMarkup(decision) {
  return `
    <div>
      <span class="approval-pill">${esc(decision.outcome)}</span>
      <strong>${esc(decision.identity_id)}</strong>
      · assurance ${esc(decision.assurance)}
      · ${esc(formatTime(decision.decided_at))}
      ${decision.reason ? `<div>${esc(decision.reason)}</div>` : ""}
    </div>
  `;
}

function requestMarkup(item) {
  const req = item.requirement || {};
  const target = item.target || {};
  const canDecide = ["pending", "partially_approved"].includes(item.status);
  const decisions = item.decisions || [];
  const approved = decisions.filter((entry) => entry.outcome === "approve").length;
  return `
    <article class="approval-card" data-approval-id="${esc(item.id)}">
      <div class="approval-card-head">
        <div>
          <strong>${esc(item.reason)}</strong>
          <div><code>${esc(item.id)}</code></div>
        </div>
        <span class="approval-pill">${esc(item.status)}</span>
      </div>

      <div class="approval-meta">
        <span><strong>Scope:</strong> ${esc(item.organization_id)} / ${esc(item.workspace_id)}</span>
        <span><strong>Requester:</strong> ${esc(item.requester_identity_id)}</span>
        <span><strong>Quorum:</strong> ${approved}/${esc(req.quorum ?? 1)}</span>
        <span><strong>Assurance:</strong> ${esc(req.required_assurance || "—")}</span>
        <span><strong>Expires:</strong> ${esc(formatTime(item.expires_at))}</span>
        <span><strong>Policy:</strong> ${esc(item.policy_source || "—")}</span>
        <span><strong>Authority:</strong> ${esc(item.authority_source || "—")}</span>
        <span><strong>Revision:</strong> ${esc(item.revision ?? "—")}</span>
      </div>

      <div class="approval-target">
        <strong>Reviewed target</strong>
        <div class="approval-meta">
          <span><strong>Operation:</strong> ${esc(target.operation)}</span>
          <span><strong>Object:</strong> ${esc(target.object_type)} / ${esc(target.object_id)}</span>
          <span><strong>Version:</strong> ${esc(target.target_version || "—")}</span>
          <span><strong>Digest:</strong> <code>${esc(target.target_digest || "—")}</code></span>
        </div>
        ${target.resource_ids?.length ? `<div><strong>Resources:</strong> ${target.resource_ids.map(esc).join(", ")}</div>` : ""}
      </div>

      <div class="approval-decisions">
        <strong>Decisions</strong>
        ${decisions.length ? decisions.map(decisionMarkup).join("") : '<div class="approval-empty">No decisions yet.</div>'}
      </div>

      ${item.resulting_operation_reference ? `<div><strong>Result:</strong> ${esc(item.resulting_operation_reference)}</div>` : ""}
      ${item.invalidation_reason ? `<div><strong>Invalidated:</strong> ${esc(item.invalidation_reason)}</div>` : ""}

      ${canDecide ? `
        <div class="approval-actions">
          <button type="button" class="ghost-button" data-approval-decision="approve">Approve</button>
          <button type="button" class="ghost-button" data-approval-decision="reject">Reject</button>
          <a href="/api/approval-requests/${encodeURIComponent(item.id)}" target="_blank" rel="noreferrer">Raw canonical record</a>
        </div>
      ` : `
        <div class="approval-actions">
          <a href="/api/approval-requests/${encodeURIComponent(item.id)}" target="_blank" rel="noreferrer">Raw canonical record</a>
        </div>
      `}
    </article>
  `;
}

function filterItems(items, filter) {
  if (filter === "all") return items;
  if (filter === "actionable") {
    return items.filter((item) => ["pending", "partially_approved", "approved"].includes(item.status));
  }
  return items.filter((item) => item.status === filter);
}

async function submitDecision(dialog, itemId, outcome) {
  const status = dialog.querySelector("[data-approval-status]");
  const reason = window.prompt(`${outcome === "approve" ? "Approval" : "Rejection"} reason (optional)`, "") ?? "";
  status.dataset.error = "false";
  status.textContent = `Submitting ${outcome}…`;
  try {
    await api(`/api/approval-requests/${encodeURIComponent(itemId)}/decisions`, {
      method: "POST",
      body: JSON.stringify({
        outcome,
        reason: reason.trim() || null,
        idempotency_key: `ui:${itemId}:${outcome}:${Date.now()}`,
      }),
    });
    await load(dialog);
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `Decision failed: ${error.message}`;
  }
}

async function load(dialog) {
  const status = dialog.querySelector("[data-approval-status]");
  const list = dialog.querySelector("[data-approval-list]");
  const filter = dialog.querySelector("[data-approval-filter]").value;
  status.dataset.error = "false";
  status.textContent = "Loading canonical approvals…";
  try {
    const payload = await api("/api/approval-requests");
    const all = payload.approval_requests || [];
    const items = filterItems(all, filter);
    list.innerHTML = items.length ? items.map(requestMarkup).join("") : '<div class="approval-empty">No matching ApprovalRequests.</div>';
    list.querySelectorAll("[data-approval-decision]").forEach((button) => {
      button.addEventListener("click", () => {
        const card = button.closest("[data-approval-id]");
        submitDecision(dialog, card.dataset.approvalId, button.dataset.approvalDecision);
      });
    });
    const pending = all.filter((item) => ["pending", "partially_approved"].includes(item.status)).length;
    document.querySelector("[data-approval-count]").textContent = String(pending);
    document.querySelector("[data-approval-count]").hidden = pending === 0;
    status.textContent = `${all.length} total · ${pending} awaiting decision · canonical refresh only`;
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `Approvals unavailable: ${error.message}`;
    list.innerHTML = '<div class="approval-empty">Canonical ApprovalRequest state could not be loaded.</div>';
  }
}

function install() {
  if (document.querySelector("[data-approval-launch]")) return;
  installStyles();
  const controls = document.querySelector(".topbar .controls");
  if (!controls) return;

  const button = document.createElement("button");
  button.type = "button";
  button.className = "ghost-button approval-launch";
  button.dataset.approvalLaunch = "true";
  button.innerHTML = 'Approvals <span class="approval-count" data-approval-count hidden>0</span>';

  const dialog = document.createElement("dialog");
  dialog.className = "approval-dialog";
  dialog.innerHTML = `
    <div class="approval-shell">
      <div class="approval-head">
        <div>
          <strong>Approval Requests</strong>
          <div><small>Canonical scope, target binding, quorum, assurance and audit state</small></div>
        </div>
        <button type="button" class="ghost-button" data-approval-close>Close</button>
      </div>
      <div class="approval-toolbar">
        <label>Status
          <select data-approval-filter>
            <option value="actionable">Actionable</option>
            <option value="all">All</option>
            <option value="pending">Pending</option>
            <option value="partially_approved">Partially approved</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="expired">Expired</option>
            <option value="consumed">Consumed</option>
            <option value="invalidated">Invalidated</option>
          </select>
        </label>
        <button type="button" class="ghost-button" data-approval-refresh>Refresh</button>
        <span class="approval-status" data-approval-status aria-live="polite"></span>
      </div>
      <div class="approval-list" data-approval-list></div>
    </div>
  `;

  controls.insertBefore(button, controls.firstChild);
  document.body.appendChild(dialog);

  button.addEventListener("click", () => {
    dialog.showModal();
    load(dialog);
  });
  dialog.querySelector("[data-approval-close]").addEventListener("click", () => dialog.close());
  dialog.querySelector("[data-approval-refresh]").addEventListener("click", () => load(dialog));
  dialog.querySelector("[data-approval-filter]").addEventListener("change", () => load(dialog));
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  // Badge refresh is deterministic API I/O only; no model invocation.
  api("/api/approval-requests").then((payload) => {
    const pending = (payload.approval_requests || []).filter((item) => ["pending", "partially_approved"].includes(item.status)).length;
    const count = button.querySelector("[data-approval-count]");
    count.textContent = String(pending);
    count.hidden = pending === 0;
  }).catch(() => {});
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", install, { once: true });
} else {
  install();
}
