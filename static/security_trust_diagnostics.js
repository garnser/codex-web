(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let zones = [];
  let securityEvents = [];
  let governedRecords = [];
  let actionIntents = [];

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function setStatus(message) {
    const host = document.getElementById("security-trust-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function countBy(items, selector) {
    const counts = new Map();
    for (const item of items) {
      const key = selector(item) || "unknown";
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    return Array.from(counts.entries()).sort(([left], [right]) => left.localeCompare(right));
  }

  function renderTrustZones() {
    const host = document.getElementById("security-trust-zones");
    if (!host) return;
    const grouped = countBy(zones, (item) => item.trust_class);
    host.innerHTML = `<div class="comm-entry">
      <strong>${zones.length} canonical trust zone(s)</strong>
      <small>Classes: ${grouped.map(([name, count]) => `${escapeHtml(name)}=${count}`).join(" · ") || "none"}</small>
      <small>Only canonical_control is authority-bearing control data. User/provider/task/repository/memory/web/webhook/tool/model content remains untrusted data even when it contains instruction-like text.</small>
    </div>` + zones.map((item) => `<div class="comm-entry">
      <strong>${escapeHtml(item.zone)} · ${escapeHtml(item.trust_class)}</strong>
    </div>`).join("");
  }

  function eventSearch() {
    return (document.getElementById("security-event-search")?.value || "").trim().toLowerCase();
  }

  function eventKindFilter() {
    return document.getElementById("security-event-kind-filter")?.value || "";
  }

  function eventOutcomeFilter() {
    return document.getElementById("security-event-outcome-filter")?.value || "";
  }

  function eventVisible(item) {
    const kind = eventKindFilter();
    const outcome = eventOutcomeFilter();
    if (kind && item.violation_kind !== kind) return false;
    if (outcome && item.outcome !== outcome) return false;
    const search = eventSearch();
    if (!search) return true;
    const haystack = [
      item.id,
      item.event_type,
      item.violation_kind,
      item.outcome,
      item.actor_identity_id,
      item.work_item_ref,
      item.execution_id,
      item.action_intent_id,
      item.source_zone,
      item.reason,
      ...(item.resource_ids || []),
      ...Object.keys(item.details || {}),
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(search);
  }

  function populateEventKinds() {
    const select = document.getElementById("security-event-kind-filter");
    if (!select) return;
    const previous = select.value;
    const values = Array.from(new Set(
      securityEvents.map((item) => item.violation_kind).filter(Boolean),
    )).sort();
    select.innerHTML = '<option value="">All violation kinds</option>' + values.map((value) => (
      `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`
    )).join("");
    if (values.includes(previous)) select.value = previous;
  }

  function renderSecurityEvents() {
    const host = document.getElementById("security-event-list");
    if (!host) return;
    const visible = securityEvents.filter(eventVisible);
    host.innerHTML = visible.map((item) => {
      const detailKeys = Object.keys(item.details || {}).sort();
      return `<details class="comm-entry">
        <summary><strong>${escapeHtml(item.event_type)} · ${escapeHtml(item.outcome)}${item.violation_kind ? ` · ${escapeHtml(item.violation_kind)}` : ""}</strong></summary>
        <small>ID: ${escapeHtml(item.id)} · occurred: ${timeText(item.occurred_at)} · actor: ${escapeHtml(item.actor_identity_id || "system")}</small>
        <small>Work: ${escapeHtml(item.work_item_ref || "none")} · execution: ${escapeHtml(item.execution_id || "none")} · ActionIntent: ${escapeHtml(item.action_intent_id || "none")}</small>
        <small>Resources: ${listText(item.resource_ids)} · source zone: ${escapeHtml(item.source_zone || "none")}</small>
        <small>Reason: ${escapeHtml(item.reason || "none")}</small>
        <small>Detail fields present: ${listText(detailKeys)}. Security-event detail values are intentionally not rendered by this browser.</small>
      </details>`;
    }).join("") || '<div class="comm-entry"><strong>No security events match the current filters.</strong></div>';
  }

  function renderGovernanceSummary() {
    const host = document.getElementById("security-governance-summary");
    if (!host) return;
    const now = Date.now() / 1000;
    const classifications = countBy(governedRecords, (item) => item.classification);
    const categories = countBy(governedRecords, (item) => item.category);
    const lifecycle = countBy(governedRecords, (item) => item.lifecycle);
    const due = governedRecords.filter((item) => (
      item.lifecycle === "active"
      && item.retention_expires_at != null
      && Number(item.retention_expires_at) <= now
    ));
    const held = governedRecords.filter((item) => item.legal_hold_at != null);
    const deniedContext = governedRecords.filter((item) => item.deny_model_context);
    const secretLike = governedRecords.filter((item) => (
      item.classification === "secret" || item.category === "credential"
    ));
    host.innerHTML = `<div class="comm-entry">
      <strong>${governedRecords.length} governed data record(s)</strong>
      <small>Classifications: ${classifications.map(([name, count]) => `${escapeHtml(name)}=${count}`).join(" · ") || "none"}</small>
      <small>Lifecycle: ${lifecycle.map(([name, count]) => `${escapeHtml(name)}=${count}`).join(" · ") || "none"}</small>
      <small>Categories: ${categories.map(([name, count]) => `${escapeHtml(name)}=${count}`).join(" · ") || "none"}</small>
      <small>Retention due: ${due.length} · legal hold: ${held.length} · deny model context: ${deniedContext.length} · secret/credential-class records: ${secretLike.length}</small>
      <small>This is governance metadata only; governed object payloads are not loaded or rendered.</small>
    </div>` + due.slice(0, 50).map((item) => `<div class="comm-entry">
      <strong>Retention due · ${escapeHtml(item.category)} · ${escapeHtml(item.classification)}</strong>
      <small>Record: ${escapeHtml(item.id)} · object: ${escapeHtml(item.object_type)} / ${escapeHtml(item.object_id)} · action: ${escapeHtml(item.retention_action)}</small>
      <small>Deadline: ${timeText(item.retention_expires_at)} · legal hold: ${item.legal_hold_at ? "yes" : "no"} · residency: ${listText(item.residency_tags)}</small>
    </div>`).join("");
  }

  function decisionBlocks(intent) {
    const blocks = [];
    for (const [label, decision] of [
      ["authority", intent.authority_decision],
      ["policy", intent.policy_decision],
    ]) {
      if (decision && decision.outcome && decision.outcome !== "allow") {
        blocks.push({
          layer: label,
          outcome: decision.outcome,
          source: decision.source,
          reason: decision.reason,
          decision_id: decision.decision_id,
        });
      }
    }
    const security = intent.security_decision;
    if (security && security.outcome && security.outcome !== "allow") {
      blocks.push({
        layer: "security",
        outcome: security.outcome,
        source: security.source,
        reason: (security.reasons || []).join("; "),
        decision_id: security.id,
      });
    }
    return blocks;
  }

  function renderDisabledActions() {
    const host = document.getElementById("security-disabled-actions");
    if (!host) return;
    const rows = [];
    for (const intent of actionIntents) {
      for (const block of decisionBlocks(intent)) {
        rows.push({ intent, block });
      }
    }
    host.innerHTML = rows.map(({ intent, block }) => `<div class="comm-entry">
      <strong>${escapeHtml(intent.action_id)} · ${escapeHtml(block.layer)} ${escapeHtml(block.outcome)}</strong>
      <small>ActionIntent: ${escapeHtml(intent.id)} · provider: ${escapeHtml(intent.provider_type)}/${escapeHtml(intent.provider_instance)} · status: ${escapeHtml(intent.status)}</small>
      <small>Work: ${escapeHtml(intent.work_item_ref || "none")} · execution: ${escapeHtml(intent.execution_id || "none")} · resources: ${listText(intent.resource_ids)}</small>
      <small>Decision source: ${escapeHtml(block.source || "unknown")} · decision ID: ${escapeHtml(block.decision_id || "none")} · reason: ${escapeHtml(block.reason || "none")}</small>
      <small>This explanation is persisted canonical decision state; the browser does not infer why the action is disabled.</small>
    </div>`).join("") || '<div class="comm-entry"><strong>No persisted denied authority/policy/security decisions in current ActionIntents.</strong><small>An unavailable capability with no persisted decision is not labeled denied by this browser.</small></div>';
  }

  function renderSummary() {
    const violations = securityEvents.filter((item) => item.violation_kind != null).length;
    const denied = securityEvents.filter((item) => item.outcome === "deny").length;
    const actionBlocks = actionIntents.reduce((count, item) => count + decisionBlocks(item).length, 0);
    setStatus(`${zones.length} trust zone(s) · ${securityEvents.length} security event(s), ${violations} violation-classified, ${denied} denied · ${governedRecords.length} governed record(s) · ${actionBlocks} persisted action denial explanation(s).`);
  }

  async function refresh() {
    setStatus("Loading canonical trust boundaries, security events, governed-data metadata and action decisions...");
    try {
      const [zoneResponse, eventResponse, governanceResponse, intentResponse] = await Promise.all([
        apiRequest("/api/security/trust-zones"),
        apiRequest("/api/security/events"),
        apiRequest("/api/data-governance/records"),
        apiRequest("/api/action-intents"),
      ]);
      zones = zoneResponse.items || [];
      securityEvents = eventResponse.items || [];
      governedRecords = governanceResponse.items || [];
      actionIntents = intentResponse.items || [];
      populateEventKinds();
      renderTrustZones();
      renderSecurityEvents();
      renderGovernanceSummary();
      renderDisabledActions();
      renderSummary();
    } catch (error) {
      setStatus(`Security & Trust Diagnostics unavailable: ${error.message}`);
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-security-trust")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("security-event-search")?.addEventListener("input", renderSecurityEvents);
    document.getElementById("security-event-kind-filter")?.addEventListener("change", renderSecurityEvents);
    document.getElementById("security-event-outcome-filter")?.addEventListener("change", renderSecurityEvents);
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
