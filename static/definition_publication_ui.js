import * as approvalUi from './definition_registry_approvals.js';
import { definitionViewOperation } from './definition_view_scope.js';

export async function publishRecord(record, { actor, active, setStatus }) {
  const operation = definitionViewOperation(setStatus);
  const report = operation.status;
  const request = operation.request;
  let approvalContext;
  try {
    approvalContext = await approvalUi.publicationGate(record, actor);
    if (!operation.current()) return;
  } catch (error) {
    report(`Publication assessment failed: ${error.message}`);
    return;
  }
  if (approvalContext.blockedMessage) {
    report(approvalContext.blockedMessage);
    return;
  }
  const { assessment, independentApprovals } = approvalContext;
  let impactCount = 0;
  if (active) {
    try {
      const usage = await request(
        `/api/definitions/${encodeURIComponent(active.record_id)}/usage`,
      );
      impactCount = Number(usage.count || 0);
    } catch (_) {
      impactCount = -1;
    }
  }
  if (!operation.current()) return;
  const reason = window.prompt(
    `Publication reason for ${record.definition_id} r${record.revision}:`,
    "",
  );
  if (reason === null) return;
  const approvalRaw = window.prompt(
    "Approval metadata as JSON object (optional):",
    "{}",
  );
  if (approvalRaw === null) return;
  let approvalMetadata;
  try {
    approvalMetadata = JSON.parse(approvalRaw || "{}");
    if (!approvalMetadata || Array.isArray(approvalMetadata) || typeof approvalMetadata !== "object") throw new Error("object required");
  } catch (error) {
    report(`Approval metadata must be a JSON object: ${error.message}`);
    return;
  }
  const impact = active
    ? ` Active r${active.revision} will be superseded; ${impactCount < 0 ? "usage impact could not be loaded" : `${impactCount} tenant-visible usage reference(s) currently point to it`}.`
    : " No active revision currently occupies this canonical slot.";
  const gate = approvalUi.publicationGateSummary(
    assessment,
    independentApprovals,
  );
  if (!window.confirm(
    `Publish ${record.kind}:${record.definition_id} r${record.revision}?${impact}${gate} Publication changes canonical runtime definition resolution; code-owned security invariants are unchanged.`,
  )) return;
  try {
    await request(
      `/api/definitions/${encodeURIComponent(record.record_id)}/publish`,
      {
        method: "POST",
        body: JSON.stringify({
          reason: reason.trim() || null,
          expected_active_revision: active?.revision ?? null,
          approval_metadata: approvalMetadata,
        }),
      },
    );
    report(`Published ${record.definition_id} r${record.revision}.`);
    operation.refresh();
  } catch (error) {
    report(`Definition publication failed: ${error.message}`);
  }
}

