(async () => {
  const BASE = window.location.pathname.startsWith("/codex") ? "/codex" : "";
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  let actor = null;
  let records = [];

  function setStatus(message) {
    const host = document.getElementById("definition-lifecycle-status");
    if (host) {
      host.hidden = false;
      host.textContent = message;
    }
  }

  function sameSlot(left, right) {
    return left.definition_id === right.definition_id
      && left.kind === right.kind
      && left.scope_type === right.scope_type
      && (left.scope_id || null) === (right.scope_id || null);
  }

  function activeFor(record) {
    return records.find((item) => sameSlot(item, record) && item.lifecycle === "published") || null;
  }

  async function publicationPreflight(record) {
    return apiRequest(
      `/api/definitions/${encodeURIComponent(record.record_id)}/publication-preflight`,
    );
  }

  function preflightSummary(assessment) {
    const classes = (assessment.change_classes || []).join(", ") || "none";
    const reasons = (assessment.reasons || []).join(" · ") || "no classified changes";
    return `classes: ${classes}; ${reasons}`;
  }

  async function approvePublication(record) {
    let preflight;
    try {
      preflight = await publicationPreflight(record);
    } catch (error) {
      setStatus(`Publication preflight failed: ${error.message}`);
      return;
    }
    const assessment = preflight.assessment || {};
    if (!assessment.requires_approval) {
      setStatus(`${record.definition_id} r${record.revision} does not require sensitive-publication approval. ${preflightSummary(assessment)}`);
      return;
    }
    if ((preflight.approvals || []).some((item) => item.approved_by === actor?.identity_id)) {
      setStatus(`Current actor already approved this exact preflight fingerprint ${assessment.fingerprint}.`);
      return;
    }
    const reason = window.prompt(
      `Approval reason for sensitive publication of ${record.definition_id} r${record.revision}:\n${preflightSummary(assessment)}`,
      "",
    );
    if (reason === null || !reason.trim()) return;
    if (!window.confirm(
      `Approve this exact sensitive-publication fingerprint? ${preflightSummary(assessment)} The approval becomes stale automatically if the active revision or candidate changes.`,
    )) return;
    try {
      const response = await apiRequest(
        `/api/definitions/${encodeURIComponent(record.record_id)}/publication-approvals`,
        {
          method: "POST",
          body: JSON.stringify({ reason: reason.trim() }),
        },
      );
      setStatus(`Recorded approval ${response.approval.id} for fingerprint ${response.approval.fingerprint}. A distinct authorized publisher can now publish while this preflight remains current.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Sensitive publication approval failed: ${error.message}`);
    }
  }

  async function publishRecord(record) {
    const active = activeFor(record);
    let preflight;
    try {
      preflight = await publicationPreflight(record);
    } catch (error) {
      setStatus(`Publication preflight failed: ${error.message}`);
      return;
    }
    const assessment = preflight.assessment || {};
    const approvals = preflight.approvals || [];
    const publicationApproval = assessment.requires_approval ? approvals[0] : null;
    if (assessment.requires_approval && !publicationApproval) {
      setStatus(`Publication blocked: sensitive authority/definition expansion requires approval for the exact current fingerprint. ${preflightSummary(assessment)}`);
      return;
    }

    let impactCount = 0;
    if (active) {
      try {
        const usage = await apiRequest(
          `/api/definitions/${encodeURIComponent(active.record_id)}/usage`,
        );
        impactCount = Number(usage.count || 0);
      } catch (_) {
        impactCount = -1;
      }
    }
    const reason = window.prompt(
      `Publication reason for ${record.definition_id} r${record.revision}:\n${preflightSummary(assessment)}`,
      "",
    );
    if (reason === null) return;
    const approvalRaw = window.prompt(
      "Additional publication metadata as JSON object (optional; this cannot replace required canonical approval evidence):",
      "{}",
    );
    if (approvalRaw === null) return;
    let approvalMetadata;
    try {
      approvalMetadata = JSON.parse(approvalRaw || "{}");
      if (!approvalMetadata || Array.isArray(approvalMetadata) || typeof approvalMetadata !== "object") {
        throw new Error("object required");
      }
    } catch (error) {
      setStatus(`Publication metadata must be a JSON object: ${error.message}`);
      return;
    }
    const impact = active
      ? ` Active r${active.revision} will be superseded; ${impactCount < 0 ? "usage impact could not be loaded" : `${impactCount} tenant-visible usage reference(s) currently point to it`}.`
      : " No active revision currently occupies this canonical slot.";
    const approvalText = publicationApproval
      ? ` Sensitive change approved by ${publicationApproval.approved_by} as ${publicationApproval.id}.`
      : " No sensitive expansion approval is required.";
    if (!window.confirm(
      `Publish ${record.kind}:${record.definition_id} r${record.revision}?${impact}${approvalText} ${preflightSummary(assessment)} Publication changes canonical runtime definition resolution; code-owned security invariants are unchanged.`,
    )) return;
    try {
      await apiRequest(
        `/api/definitions/${encodeURIComponent(record.record_id)}/publish`,
        {
          method: "POST",
          body: JSON.stringify({
            reason: reason.trim() || null,
            expected_active_revision: active?.revision ?? null,
            approval_metadata: approvalMetadata,
            publication_approval_id: publicationApproval?.id || null,
          }),
        },
      );
      setStatus(`Published ${record.definition_id} r${record.revision} using canonical publication preflight ${assessment.fingerprint}.`);
      document.getElementById("refresh-definitions")?.click();
    } catch (error) {
      setStatus(`Definition publication failed: ${error.message}`);
    }
  }

  async function handle(button) {
    const record = records.find((item) => item.record_id === button.dataset.recordId);
    if (!record) return;
    button.disabled = true;
    try {
      if (button.dataset.definitionAction === "publish") {
        await publishRecord(record);
      } else if (button.dataset.definitionAction === "approve-publication") {
        await approvePublication(record);
      }
    } finally {
      if (document.contains(button)) button.disabled = false;
    }
  }

  async function loadActor() {
    try {
      actor = await apiRequest("/api/identity/me");
    } catch (_) {
      actor = null;
    }
  }

  function bind() {
    document.getElementById("definition-registry-list")?.addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-definition-action]");
      if (
        button
        && ["publish", "approve-publication"].includes(button.dataset.definitionAction)
      ) {
        handle(button).catch(console.error);
      }
    });
    loadActor().catch(console.error);
  }

  window.addEventListener("codex:definition-registry-rendered", (event) => {
    records = event.detail?.records || [];
  });
  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", bind, { once: true });
  } else {
    bind();
  }
})();
