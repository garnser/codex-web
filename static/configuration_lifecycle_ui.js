import { confirmAction } from './action_confirmation.js';
import { projectViewOperation } from './project_view_scope.js';

export async function validateRecord(record, { active, setStatus }) {
  const operation = projectViewOperation(setStatus, 'refresh-configuration');
  const report = operation.status;
  const request = operation.request;
  try {
    const result = await request(`/api/configuration/${encodeURIComponent(record.id)}/validate`, { method: "POST" });
    report(`Validated ${record.key} r${record.revision} as ${result.spec?.value_kind || "unknown"}.`);
  } catch (error) {
    report(`Configuration validation failed: ${error.message}`);
  }
}
export async function publishRecord(record, { active, setStatus }) {
  const operation = projectViewOperation(setStatus, 'refresh-configuration');
  const report = operation.status;
  const request = operation.request;
  let impact = null;
  try {
    impact = await request(`/api/configuration/${encodeURIComponent(record.id)}/impact`);
  } catch (error) {
    report(`Configuration impact unavailable: ${error.message}. Retry before publishing.`);
    return;
  }
  if (!operation.current()) return;
  const reason = window.prompt(`Publication reason for ${record.key} r${record.revision}:`, record.create_reason || "");
  if (reason === null) return;
  const overrides = impact?.more_specific_overrides?.length || 0;
  const startup = impact?.startup_only ? " This is startup-only and will not hot-reload." : "";
  if (!await confirmAction({ action: 'Publish configuration', target: `${record.key} r${record.revision} at ${record.scope_type}:${record.scope_id || "default"}`, risk: 'high', consequence: `Publish ${record.key} r${record.revision}? Active revision in this exact slot: ${active?.revision ?? "none"}. ${overrides} more-specific published override(s) currently exist.${startup} Publishing changes runtime configuration but cannot grant authority.`, recovery: 'A later rollback publishes a new immutable revision after canonical validation.', current: operation.current })) return;
  try {
    await request(`/api/configuration/${encodeURIComponent(record.id)}/publish`, {
      method: "POST",
      body: JSON.stringify({
        reason: reason.trim() || null,
        expected_active_revision: active?.revision ?? null,
      }),
    });
    report(`Published ${record.key} r${record.revision}.`);
    operation.refresh();
  } catch (error) {
    report(`Configuration publication failed: ${error.message}`);
  }
}
export async function rollbackRecord(record, { active, setStatus }) {
  const operation = projectViewOperation(setStatus, 'refresh-configuration');
  const report = operation.status;
  const request = operation.request;
  if (!active || active.id === record.id) return;
  const reason = window.prompt(`Reason for rollback to ${record.key} r${record.revision}:`, "");
  if (reason === null) return;
  if (!await confirmAction({ action: 'Roll back configuration', target: `${record.key} r${record.revision} at ${record.scope_type}:${record.scope_id || "default"}`, risk: 'high', consequence: `Rollback ${record.key} from active r${active.revision} to the value of r${record.revision}? The server creates and publishes a new immutable revision; history is preserved.`, recovery: 'History is preserved; subsequent changes require a new validated revision.', current: operation.current })) return;
  try {
    const response = await request("/api/configuration/rollback", {
      method: "POST",
      body: JSON.stringify({
        key: record.key,
        scope_type: record.scope_type,
        scope_id: record.scope_id,
        target_revision: record.revision,
        reason: reason.trim() || null,
        expected_active_revision: active.revision,
      }),
    });
    report(`Rollback published as new ${record.key} r${response.record.revision}.`);
    operation.refresh();
  } catch (error) {
    report(`Configuration rollback failed: ${error.message}`);
  }
}
export async function resetRecord(record, { active, setStatus }) {
  const operation = projectViewOperation(setStatus, 'refresh-configuration');
  const report = operation.status;
  const request = operation.request;
  if (!active || active.id !== record.id) return;
  const reason = window.prompt(`Reason for reverting ${record.key} r${record.revision} to inherited/default resolution:`, "");
  if (reason === null) return;
  if (!await confirmAction({ action: 'Reset configuration override', target: `${record.key} r${record.revision} at ${record.scope_type}:${record.scope_id || "default"}`, risk: 'high', consequence: `Revert the explicit ${record.key} override at ${record.scope_type}${record.scope_id ? `:${record.scope_id}` : ""}? The active revision is preserved in history and superseded by a disabled tombstone. Resolution will fall through to the next applicable published scope or code-owned default.`, recovery: 'Restore an explicit value by publishing a new authorized revision.', current: operation.current })) return;
  try {
    const response = await request("/api/configuration/reset", {
      method: "POST",
      body: JSON.stringify({
        key: record.key,
        scope_type: record.scope_type,
        scope_id: record.scope_id,
        reason: reason.trim() || null,
        expected_active_revision: active.revision,
      }),
    });
    report(`Reverted ${record.key} with tombstone r${response.record.revision}; resolution now inherits.`);
    operation.refresh();
  } catch (error) {
    report(`Configuration reset failed: ${error.message}`);
  }
}
