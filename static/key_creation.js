import { confirmAction } from './action_confirmation.js';
import { keyOperation } from './key_usage_ui.js';

export function keyCreator(draft, reportStatus, refresh) {
  let creating = false;
  return async function createKey() {
    if (creating) return;
    const ticket = draft.submission();
    const operation = keyOperation(reportStatus);
    const { request: apiRequest, status: setStatus } = operation;
    const backendType = document.getElementById("crypto-key-backend")?.value || "";
    const purpose = document.getElementById("crypto-key-purpose")?.value || "application_data";
    if (!backendType) {
      setStatus("Choose a healthy key backend.");
      return;
    }
    const projectId = document.getElementById("crypto-key-project-id")?.value.trim() || null;
    const resourceId = document.getElementById("crypto-key-resource-id")?.value || null;
    const scope = [projectId ? `project ${projectId}` : null, resourceId ? `resource ${resourceId}` : null].filter(Boolean).join(", ") || "workspace";
    if (!await confirmAction({ action: 'Create key reference', target: `${purpose} using ${backendType} at ${scope}`, risk: 'bounded', consequence: `Create a ${purpose} key reference using backend ${backendType} scoped to ${scope}? Cryptographic material is generated and retained only by the configured key backend.`, recovery: 'Key material remains behind the key boundary. Later revocation requires a dependency review.' })) return;
    if (!ticket.current() || creating) return;
    creating = true;
    try {
      await apiRequest("/api/crypto/keys", {
        method: "POST",
        body: JSON.stringify({
          project_id: projectId,
          resource_id: resourceId,
          purpose,
          backend_type: backendType,
        }),
      });
      if (!ticket.current()) return;
      ticket.saved();
      if (draft.dirty()) { setStatus('Managed key created. Newer metadata remains unsaved.'); return; }
      setStatus("Managed key created.");
      if (operation.current()) await refresh();
    } catch (error) {
      if (ticket.current()) setStatus(`Key creation failed: ${error.message}`);
    } finally { creating = false; }
  };
}
