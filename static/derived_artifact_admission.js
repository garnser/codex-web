import { request } from './api_client.js';
import { confirmAction } from './action_confirmation.js';
import { referenceLink } from './reference_navigation.js';
import { captureProjectView, currentProjectId } from './project_view_scope.js';
import { canAdmitDerivedArtifact, derivedArtifactRequest } from './derived_artifact_request.js';
let actor = null;
function bind() {
  const host = document.getElementById('artifact-evidence-admin-panel');
  if (!host || document.getElementById('derived-artifact-form')) return;
  const section = document.createElement('details');
  section.innerHTML = `<summary>Admit approved input as a scoped Artifact</summary><form id="derived-artifact-form">
    <p>Sources remain immutable. The server inherits source governance; this grants no approval, QA pass or worker access to unscoped Evidence. No content bytes are uploaded here.</p>
    <label>Project <input name="project_id" required></label>
    <label>Target Work Item <input name="work_item_ref" required></label>
    <label>Target resource IDs <input name="resource_ids" required></label>
    <label>Parent Evidence IDs (same Project, at most 64) <textarea name="source_evidence_ids" required></textarea></label>
    <label>Artifact name <input name="name" required></label>
    <label>Exact approved document SHA-256 <input name="digest" pattern="[a-fA-F0-9]{64}" required></label>
    <label>Stable idempotency key <input name="idempotency_key" maxlength="200" required></label>
    <button type="submit" disabled>Review scoped admission</button><div role="status" data-derived-status></div></form>`;
  host.append(section);
  const form = section.querySelector('form'), button = form.querySelector('button');
  const status = form.querySelector('[data-derived-status]');
  const update = () => { button.disabled = !canAdmitDerivedArtifact(actor); };
  window.addEventListener('codex:artifact-evidence-state-rendered', event => {
    actor = event.detail?.actor || null; update();
    if (!form.elements.project_id.value) form.elements.project_id.value = currentProjectId();
  });
  for (const type of ['codex:project-changed', 'codex:identity-scope-changed']) window.addEventListener(type, () => {
    actor = null; update(); status.textContent = 'Scope changed. Refresh before admission.';
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!canAdmitDerivedArtifact(actor)) return;
    const scope = captureProjectView();
    try {
      const body = derivedArtifactRequest(Object.fromEntries(new FormData(form)));
      if (currentProjectId() && body.artifact.project_id !== currentProjectId()) throw new Error('Target Project differs from the selected Project.');
      status.innerHTML = body.source_evidence_ids.map(id => referenceLink('evidence', id, {projectId: body.artifact.project_id})).join(', ');
      if (!await confirmAction({action: 'Admit derived Artifact', target: `${body.artifact.work_item_ref} · ${body.artifact.resource_ids.join(', ')}`, risk: 'high',
        consequence: `Parent Evidence: ${body.source_evidence_ids.join(', ')}. SHA-256: ${body.artifact.digest.value}. Current source restrictions are inherited.`, recovery: 'Sources are preserved. Inspect the resulting Artifact and governance before use.'})) return;
      if (!scope.current()) return;
      button.disabled = true;
      const result = await request('/api/artifacts/derived', {method: 'POST', body: JSON.stringify(body)});
      if (!scope.current()) return;
      status.innerHTML = referenceLink('artifact', result.item.id, {projectId: result.item.project_id});
      document.getElementById('refresh-artifact-evidence')?.click();
    } catch (error) { if (scope.current()) status.textContent = `Admission unavailable: ${error.message}`; }
    finally { update(); }
  });
}
if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', bind, {once: true});
else bind();
