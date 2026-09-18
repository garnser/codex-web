(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let actor = null;
  let artifacts = [];
  let evidence = [];
  let verifications = [];
  let governance = [];

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  }

  function timeText(value) {
    if (!value) return "none";
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.valueOf()) ? "unknown" : date.toLocaleString();
  }

  function listText(values) {
    return values?.length ? values.map(escapeHtml).join(", ") : "none";
  }

  function safeLink(value) {
    if (!value) return "";
    try {
      const url = new URL(value, window.location.origin);
      if (!["http:", "https:"].includes(url.protocol)) return "";
      return `<a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">open source</a>`;
    } catch (_) {
      return "";
    }
  }

  function setStatus(message) {
    const host = document.getElementById("artifact-evidence-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function setAdminStatus(message) {
    const host = document.getElementById("artifact-evidence-admin-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function governanceFor(objectId) {
    return governance.find((item) => item.object_id === objectId) || null;
  }

  function governanceHtml(record) {
    if (!record) {
      return "<small>Governance metadata: not linked/backfilled.</small>";
    }
    const hold = record.legal_hold_at
      ? `legal hold since ${timeText(record.legal_hold_at)} by ${record.legal_hold_by || "unknown"}`
      : "no legal hold";
    return `<small>Governance: ${escapeHtml(record.classification)} · ${escapeHtml(record.lifecycle)} · retention ${escapeHtml(record.retention_action)} at ${timeText(record.retention_expires_at)} · ${escapeHtml(hold)}</small>
      <small>Residency: ${listText(record.residency_tags)} · deny model context: ${record.deny_model_context ? "yes" : "no"} · governance ID: ${escapeHtml(record.id)}</small>`;
  }

  function canMutateRecord(producerId) {
    if (!actor) return false;
    if (actor.identity_id === producerId) return true;
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes("artifact-evidence:admin");
    }
    return (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function canAdmin() {
    if (!actor) return false;
    if (actor.principal_kind === "service") {
      return (actor.service_scopes || []).includes("artifact-evidence:admin");
    }
    return ["mfa", "local_trusted"].includes(actor.assurance)
      && (actor.roles || []).some((role) => ["owner", "admin"].includes(role));
  }

  function renderAdminAssurance() {
    const host = document.getElementById("artifact-evidence-admin-assurance");
    if (host && actor) {
      host.textContent = `Current actor ${actor.identity_id} · ${actor.principal_kind} · assurance ${actor.assurance}. Governance/requirements administration: ${canAdmin() ? "allowed" : "blocked"}. Producer-owned artifact/evidence invalidation remains a separate canonical permission.`;
    }
    for (const id of ["sync-artifact-governance", "expire-artifact-retention"]) {
      const button = document.getElementById(id);
      if (button) button.disabled = !canAdmin();
    }
  }

  function term() {
    return (document.getElementById("artifact-evidence-search")?.value || "").trim().toLowerCase();
  }

  function lifecycleFilter() {
    return document.getElementById("artifact-evidence-lifecycle-filter")?.value || "";
  }

  function matchesSearch(item) {
    const needle = term();
    if (!needle) return true;
    return JSON.stringify(item).toLowerCase().includes(needle);
  }

  function artifactVisible(item) {
    const state = lifecycleFilter();
    if (state && item.lifecycle !== state) return false;
    return matchesSearch(item);
  }

  function evidenceVisible(item) {
    const state = lifecycleFilter();
    if (state && item.lifecycle !== state) return false;
    return matchesSearch(item);
  }

  function verificationVisible(item) {
    return matchesSearch(item);
  }

  function digestText(digest) {
    return digest ? `${digest.algorithm}:${digest.value}` : "none";
  }

  function invalidateButton(kind, item) {
    if (!canMutateRecord(item.producer_identity_id)) return "";
    const active = kind === "artifact" ? item.lifecycle === "active" : item.lifecycle === "valid";
    if (!active) return "";
    return `<button type="button" class="ghost-button" data-artifact-evidence-invalidate="${kind}" data-record-id="${escapeHtml(item.id)}">Invalidate</button>`;
  }

  function renderArtifacts() {
    const host = document.getElementById("artifact-list");
    if (!host) return;
    const rows = artifacts.filter(artifactVisible);
    host.innerHTML = rows.map((item) => {
      const sourceLink = safeLink(item.external_url);
      return `<details class="comm-entry">
        <summary><strong>${escapeHtml(item.artifact_type)} · ${escapeHtml(item.name)} · ${escapeHtml(item.lifecycle)}</strong></summary>
        <small>ID: ${escapeHtml(item.id)} · producer: ${escapeHtml(item.producer_identity_id)} · produced: ${timeText(item.produced_at)}</small>
        <small>Work: ${escapeHtml(item.work_item_ref || "none")} · execution: ${escapeHtml(item.execution_id || "none")} · execution workspace: ${escapeHtml(item.execution_workspace_id || "none")} · project: ${escapeHtml(item.project_id || "none")}</small>
        <small>Resources: ${listText(item.resource_ids)} · provider: ${escapeHtml(item.provider || "none")} · source: ${escapeHtml(item.source || "none")} · external ID: ${escapeHtml(item.external_id || "none")} ${sourceLink}</small>
        <small>Revision: ${escapeHtml(item.revision || "none")} · digest: ${escapeHtml(digestText(item.digest))}</small>
        <small>Supersedes: ${escapeHtml(item.supersedes_id || "none")} · superseded by: ${escapeHtml(item.superseded_by_id || "none")} · retention: ${timeText(item.retention_expires_at)}</small>
        ${item.invalidation_reason ? `<small>Invalidated: ${timeText(item.invalidated_at)} · ${escapeHtml(item.invalidation_reason)}</small>` : ""}
        ${governanceHtml(governanceFor(item.id))}
        ${invalidateButton("artifact", item)}
      </details>`;
    }).join("") || '<div class="comm-entry"><strong>No artifacts match the current filters.</strong></div>';
  }

  function renderEvidence() {
    const host = document.getElementById("evidence-list");
    if (!host) return;
    const rows = evidence.filter(evidenceVisible);
    host.innerHTML = rows.map((item) => {
      const sourceLink = safeLink(item.deep_link);
      return `<details class="comm-entry">
        <summary><strong>${escapeHtml(item.evidence_type)} · ${escapeHtml(item.result)} · ${escapeHtml(item.lifecycle)}</strong></summary>
        <small>ID: ${escapeHtml(item.id)} · producer: ${escapeHtml(item.producer_identity_id)} · observed: ${timeText(item.observed_at)}</small>
        <small>Work: ${escapeHtml(item.work_item_ref || "none")} · execution: ${escapeHtml(item.execution_id || "none")} · execution workspace: ${escapeHtml(item.execution_workspace_id || "none")} · project: ${escapeHtml(item.project_id || "none")}</small>
        <small>Artifacts: ${listText(item.artifact_ids)} · provider: ${escapeHtml(item.provider || "none")} · source: ${escapeHtml(item.source || "none")} · external ID: ${escapeHtml(item.external_id || "none")} ${sourceLink}</small>
        <small>Summary: ${escapeHtml(item.summary || "none")} · digest: ${escapeHtml(digestText(item.digest))}</small>
        <small>Retention: ${timeText(item.retention_expires_at)} · governance ID: ${escapeHtml(item.governance_record_id || "none")}</small>
        ${item.invalidation_reason ? `<small>Invalidated: ${timeText(item.invalidated_at)} · ${escapeHtml(item.invalidation_reason)}</small>` : ""}
        ${governanceHtml(governanceFor(item.id))}
        ${invalidateButton("evidence", item)}
      </details>`;
    }).join("") || '<div class="comm-entry"><strong>No evidence matches the current filters.</strong></div>';
  }

  function renderVerifications() {
    const host = document.getElementById("verification-list");
    if (!host) return;
    const rows = verifications.filter(verificationVisible);
    host.innerHTML = rows.map((item) => `<details class="comm-entry">
      <summary><strong>${escapeHtml(item.result)} · ${item.independent ? "independent" : "producer-related"} · ${escapeHtml(item.method)}</strong></summary>
      <small>ID: ${escapeHtml(item.id)} · verifier: ${escapeHtml(item.verifier_identity_id)} · verified: ${timeText(item.verified_at)}</small>
      <small>Work: ${escapeHtml(item.work_item_ref || "none")} · execution: ${escapeHtml(item.execution_id || "none")}</small>
      <small>Artifacts: ${listText(item.artifact_ids)} · evidence: ${listText(item.evidence_ids)}</small>
      <small>Findings: ${listText(item.findings)} · provider: ${escapeHtml(item.provider || "none")} · source: ${escapeHtml(item.source || "none")} ${safeLink(item.deep_link)}</small>
    </details>`).join("") || '<div class="comm-entry"><strong>No verifications match the current search.</strong></div>';
  }

  function renderAll() {
    renderArtifacts();
    renderEvidence();
    renderVerifications();
    const activeArtifacts = artifacts.filter((item) => item.lifecycle === "active").length;
    const validEvidence = evidence.filter((item) => item.lifecycle === "valid").length;
    const independent = verifications.filter((item) => item.independent && item.result === "verified").length;
    setStatus(`${artifacts.length} artifact(s), ${activeArtifacts} active · ${evidence.length} evidence record(s), ${validEvidence} valid · ${verifications.length} verification(s), ${independent} independent verified. Artifact/evidence metadata is canonical; payload contents are not rendered here.`);
  }

  function renderGate(requirements, evaluation) {
    const host = document.getElementById("evidence-gate-result");
    if (!host) return;
    const requirementById = new Map((requirements || []).map((item) => [item.id, item]));
    host.innerHTML = `<div class="comm-entry">
      <strong>${evaluation.satisfied ? "Evidence gate satisfied" : "Evidence gate blocked"} · ${escapeHtml(evaluation.work_item_ref)}</strong>
      <small>Evaluated: ${timeText(evaluation.evaluated_at)} · requirements: ${requirements.length}</small>
      ${(evaluation.outcomes || []).map((outcome) => {
        const req = requirementById.get(outcome.requirement_id);
        return `<div class="comm-entry">
          <strong>${escapeHtml(outcome.requirement_id)} · ${outcome.satisfied ? "satisfied" : "missing"}</strong>
          <small>Requirement: ${escapeHtml(req?.evidence_type || "unknown")} · artifact type ${escapeHtml(req?.artifact_type || "any")} · minimum ${escapeHtml(req?.min_count || "unknown")} · accepted ${listText(req?.accepted_results)}</small>
          <small>Independent verification required: ${req?.independent_verification ? "yes" : "no"} · provider: ${escapeHtml(req?.provider || "any")} · max age: ${escapeHtml(req?.max_age_seconds ?? "none")}</small>
          <small>Matching evidence: ${listText(outcome.matching_evidence_ids)} · verifications: ${listText(outcome.verification_ids)} · reason: ${escapeHtml(outcome.reason || "satisfied")}</small>
        </div>`;
      }).join("")}
    </div>`;
  }

  async function evaluateGate() {
    const ref = document.getElementById("artifact-evidence-work-item")?.value.trim() || "";
    if (!ref) return setStatus("Enter a Work Item reference before evaluating evidence.");
    try {
      const [requirementsResponse, evaluation] = await Promise.all([
        apiRequest(`/api/evidence-requirements/work-items/${encodeURIComponent(ref)}`),
        apiRequest(`/api/evidence-evaluations/work-items/${encodeURIComponent(ref)}`, {
          method: "POST",
          body: JSON.stringify({ requirements: [] }),
        }),
      ]);
      renderGate(requirementsResponse.requirements || [], evaluation);
    } catch (error) {
      const host = document.getElementById("evidence-gate-result");
      if (host) host.innerHTML = `<div class="comm-entry"><strong>Evidence evaluation unavailable</strong><small>${escapeHtml(error.message)}</small></div>`;
    }
  }

  async function invalidate(kind, id) {
    const reason = window.prompt(`Reason for invalidating ${kind} ${id}:`, "");
    if (!reason?.trim()) return setStatus("Invalidation requires a reason.");
    const consequence = kind === "artifact"
      ? "Dependent evidence and verifications will be invalidated transitively."
      : "Dependent verifications will be invalidated transitively.";
    if (!window.confirm(`Invalidate ${kind} ${id}? ${consequence}`)) return;
    try {
      await apiRequest(`/api/${kind === "artifact" ? "artifacts" : "evidence"}/${encodeURIComponent(id)}/invalidate`, {
        method: "POST",
        body: JSON.stringify({ reason: reason.trim() }),
      });
      setStatus(`Invalidated ${kind} ${id}; dependent trust records were reconciled canonically.`);
      await refresh();
    } catch (error) {
      setStatus(`${kind} invalidation failed: ${error.message}`);
    }
  }

  async function syncGovernance() {
    if (!canAdmin()) return setAdminStatus("Governance sync requires admin + MFA/step-up or artifact-evidence:admin.");
    if (!window.confirm("Backfill missing canonical governance metadata for tenant-visible artifacts and evidence? Existing linked governance records are preserved.")) return;
    try {
      const result = await apiRequest("/api/artifact-evidence/governance/sync", { method: "POST" });
      setAdminStatus(`Governance sync complete · artifacts ${result.artifacts || 0} · evidence ${result.evidence || 0}.`);
      await refresh();
    } catch (error) {
      setAdminStatus(`Governance sync failed: ${error.message}`);
    }
  }

  async function expireRetention() {
    if (!canAdmin()) return setAdminStatus("Retention expiry requires admin + MFA/step-up or artifact-evidence:admin.");
    if (!window.confirm("Expire artifact/evidence rows whose canonical retention deadline has passed? Dependent evidence/verifications are invalidated as required.")) return;
    try {
      const result = await apiRequest("/api/artifact-evidence/expire-retention", { method: "POST" });
      setAdminStatus(`Retention expiry complete · artifacts ${result.artifacts?.length || 0} · evidence ${result.evidence?.length || 0}.`);
      await refresh();
    } catch (error) {
      setAdminStatus(`Retention expiry failed: ${error.message}`);
    }
  }

  async function refresh() {
    setStatus("Loading canonical artifacts, evidence, verifications and governance metadata...");
    try {
      const [artifactResponse, evidenceResponse, verificationResponse, governanceResponse, me] = await Promise.all([
        apiRequest("/api/artifacts"),
        apiRequest("/api/evidence"),
        apiRequest("/api/verifications"),
        apiRequest("/api/data-governance/records"),
        apiRequest("/api/identity/me"),
      ]);
      artifacts = artifactResponse.items || [];
      evidence = evidenceResponse.items || [];
      verifications = verificationResponse.items || [];
      governance = (governanceResponse.items || []).filter((item) => ["artifact", "evidence"].includes(item.object_type));
      actor = me;
      renderAdminAssurance();
      renderAll();
      window.dispatchEvent(new CustomEvent("codex:artifact-evidence-state-rendered", {
        detail: { artifacts, evidence, verifications, governance, actor },
      }));
    } catch (error) {
      setStatus(`Artifact/Evidence explorer unavailable: ${error.message}`);
    }
  }

  function bind() {
    const panel = document.getElementById("developer-panel");
    document.getElementById("refresh-artifact-evidence")?.addEventListener("click", refresh);
    document.getElementById("refresh-developer")?.addEventListener("click", refresh);
    document.getElementById("artifact-evidence-search")?.addEventListener("input", renderAll);
    document.getElementById("artifact-evidence-lifecycle-filter")?.addEventListener("change", renderAll);
    document.getElementById("evaluate-evidence-gate")?.addEventListener("click", () => evaluateGate().catch(console.error));
    document.getElementById("sync-artifact-governance")?.addEventListener("click", () => syncGovernance().catch(console.error));
    document.getElementById("expire-artifact-retention")?.addEventListener("click", () => expireRetention().catch(console.error));
    document.getElementById("artifact-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-artifact-evidence-invalidate]");
      if (button) invalidate(button.dataset.artifactEvidenceInvalidate, button.dataset.recordId).catch(console.error);
    });
    document.getElementById("evidence-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-artifact-evidence-invalidate]");
      if (button) invalidate(button.dataset.artifactEvidenceInvalidate, button.dataset.recordId).catch(console.error);
    });
    panel?.addEventListener("toggle", () => {
      if (panel.open) refresh().catch(console.error);
    });
    if (panel?.open) refresh().catch(console.error);
  }

  window.addEventListener("DOMContentLoaded", bind);
})();
