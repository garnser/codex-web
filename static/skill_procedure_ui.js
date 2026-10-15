import { request } from './api_client.js';
import { esc } from './definition_typed_editor_shared.js';
import { captureProjectView, currentProjectId } from './project_view_scope.js';
import { referenceLink } from './reference_navigation.js';
import { procedureDiscoveriesHtml } from './procedure_discovery_view.js';

const csv = value => value.split(',').map(v => v.trim()).filter(Boolean);
export const procedureError = error => typeof error.detail === 'string' ? error.detail
  : error.detail?.message || error.detail?.code || (Array.isArray(error.detail) ? error.detail.map(e => `${e.loc?.join('.')}: ${e.msg}`).join('; ') : error.message);

export function procedureSummary(item, revisions) {
  const p = item.skill?.operational_procedure;
  if (!p) return '';
  return `<section class="skill-wide" data-procedure-summary><h3>Operational playbook</h3>
    <p>${['draft', 'validated'].includes(item.definitionLifecycle) ? 'Inactive draft — review, validate and scan before explicit publication.' : esc(item.definitionLifecycle)} · ${referenceLink('definition', item.recordId, { label: 'Canonical Definition' })}</p>
    <dl><dt>Effective Definition scope</dt><dd>${esc(item.scopeType)} / ${esc(item.scopeId)}</dd><dt>Procedure owner</dt><dd>${referenceLink('identity', p.owner_identity_id)}</dd><dt>Eligibility</dt><dd>Tasks ${esc(p.task_triggers.join(', '))}; operations ${esc(p.operation_triggers.join(', '))}; environments ${esc(p.environments.join(', '))}</dd><dt>Review / expiry</dt><dd>${esc(new Date(p.review_due_at * 1000).toISOString())} / ${esc(new Date(p.expires_at * 1000).toISOString())}</dd><dt>Conflict handling</dt><dd>${esc(p.conflict_group)} · precedence ${esc(p.precedence)} · ${esc(p.on_conflict)}</dd></dl>
    <p>Requirements are independent of permission. Current facts belong to Resources; approvals and policy control each run.</p>
    <ol>${p.steps.map(step => `<li><strong>${esc(step.id)} · ${esc(step.kind)}</strong> ${esc(step.description)}<p>${step.timeout_seconds}s, ${step.max_attempts} attempt(s); failure ${esc(step.failure.outcome)} → ${referenceLink('identity', step.failure.escalation_owner_identity_id)}</p><details><summary>Inputs, outputs, evidence, impact and authority requirements</summary><pre>${esc(JSON.stringify(step, null, 2))}</pre></details></li>`).join('')}</ol>
    <button type="button" data-procedure-validate>Validate exact revision</button>
    <label>Compare revision <select data-procedure-diff-left>${revisions.filter(r => r.recordId !== item.recordId).map(r => `<option value="${esc(r.recordId)}">r${esc(r.revision)} · ${esc(r.definitionLifecycle)}</option>`).join('')}</select></label>
    <button type="button" data-procedure-diff>Show canonical diff</button><pre data-procedure-review role="status"></pre>
    <details><summary>Full validated procedure content</summary><pre>${esc(JSON.stringify(p, null, 2))}</pre></details></section>`;
}

export function installProcedurePreview(root) {
  const panel = document.createElement('section');
  panel.className = 'skill-import'; panel.dataset.procedurePreview = '';
  panel.innerHTML = `<h3>Playbook applicability preview</h3><p>Read-only canonical selection. A preview never creates an ActionIntent or authorizes an operation. Environment is read from Resources; an expected value detects stale/conflicting inputs. Include an execution ID to inspect its actual worker/provider capability evidence.</p><form>
    ${[['binding_id','ActionProvider binding'],['task_kind','Task trigger'],['resource_ids','Resource IDs (comma separated)'],['environment','Expected environment (optional)'],['operation','Registered operation ID'],['execution_id','Execution ID (optional)']].map(([key,label]) => `<label>${label}<input data-field="${key}"></label>`).join('')}
    <button type="submit">Preview applicability</button></form><div data-procedure-preview-result role="status"></div>`;
  root.append(panel);
  const form = panel.querySelector('form');
  let sequence = 0;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const scope = captureProjectView(), visit = ++sequence;
    const current = () => scope.current() && panel.isConnected && visit === sequence;
    const output = panel.querySelector('[data-procedure-preview-result]');
    const value = key => form.querySelector(`[data-field="${key}"]`).value.trim();
    output.textContent = 'Resolving canonical facts, exact revisions, capabilities and policy…';
    try {
      const actor = await request('/api/identity/me');
      if (!current()) return;
      const result = await request('/api/skills/procedure-applicability', { method: 'POST', body: JSON.stringify({
        binding_id: value('binding_id'), task: value('task_kind') || null,
        expected_environment: value('environment') || null, execution_id: value('execution_id') || null,
        request: { action_id: value('operation'), resource_ids: csv(value('resource_ids')),
          organization_id: actor.organization_id, workspace_id: actor.workspace_id, project_id: currentProjectId() || null },
      }) });
      if (!current()) return;
      output.innerHTML = `<p>Preview only · canonical environment ${esc(result.input_context?.environment || 'unresolved')} · task ${esc(result.input_context?.task || 'all')} · mutation not authorized</p><p>Current authority ${esc(result.authority?.outcome || 'unavailable')} · source ${esc(result.authority?.source || 'unknown')} · ${esc(result.authority?.reason || '')} ${esc((result.authority?.reasons || []).join(', '))}</p><p>Authority definitions ${(result.authority?.definition_refs || []).map(ref => referenceLink('definition', ref.record_id, {label:`${ref.definition_id} r${ref.revision}`})).join(', ') || 'none'}</p>` + procedureDiscoveriesHtml({procedure_discoveries: [result.discovery]}, currentProjectId());
    } catch (error) { if (current()) output.textContent = `Preview blocked: ${procedureError(error)}. Inspect Resources, provider binding, policy and capability evidence before retrying.`; }
  });
  const clear = () => { sequence += 1; form.reset(); panel.querySelector('[data-procedure-preview-result]').textContent = 'Scope changed. Preview again with current canonical inputs.'; };
  window.addEventListener('codex:project-changed', clear);
  window.addEventListener('codex:identity-scope-changed', clear);
}
