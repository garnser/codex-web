import { request } from './api_client.js';
import { captureProjectView } from './project_view_scope.js';
import { referenceLink, referencePath } from './reference_navigation.js';
import { esc } from './definition_typed_editor_shared.js';
import { procedureDiscoveriesHtml } from './procedure_discovery_view.js';
import { procedureError } from './skill_procedure_ui.js';
import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { confirmAction } from './action_confirmation.js';

const refs = (kind, values, projectId) => (values || []).map(id => referenceLink(kind, id, { projectId })).join(', ') || 'none';
const owners = new WeakMap();
export function procedureRunsDirty(root) {
  return [...root.querySelectorAll('[data-procedure-run]')].some(host => Object.values(owners.get(host) || {}).some(editor => editor.dirty()));
}
export function disposeProcedureRuns(root) {
  for (const host of root.querySelectorAll('[data-procedure-run]')) {
    Object.values(owners.get(host) || {}).forEach(editor => editor.dispose()); owners.delete(host);
  }
}
export function checkpointHtml(pin, projectId) {
  if (!pin) return '<p>No playbook is pinned. Select an admitted exact revision and governed input Artifact to start. A preview alone grants no authority.</p>';
  return `<h4>Pinned playbook · ${esc(pin.status)}</h4><p>Run ${esc(pin.id || 'legacy')}</p><p>${referenceLink('definition', pin.definition.record_id, { projectId, label: `${pin.definition.definition_id} r${pin.definition.revision}` })} · checksum ${esc(pin.definition.checksum)} · checkpoint r${esc(pin.revision)}</p>
    <p>Actor ${esc(pin.actor_identity_id)} · context ${esc(pin.resource_context_checksum)} · input ${referenceLink('artifact', pin.input_snapshot_id, {projectId})} · ${esc(pin.input_snapshot_checksum)} · deadline ${esc(new Date(pin.deadline_at * 1000).toISOString())}</p>
    <p>${esc(pin.failure_code || '')} ${pin.status === 'reconciliation_required' ? 'Unknown outcome: inspect originating ActionIntent receipts and reconcile before retry or rollback. Advance cannot infer failure or duplicate an effect.' : pin.status === 'awaiting_approval' ? 'Awaiting exact canonical approval. Inspect the approval scope and decision; advance rechecks current authority.' : pin.status === 'recovery_required' ? 'Recovery required: inspect failed verification, declared recovery step and Attention before continuing.' : pin.status === 'waiting' ? 'Waiting for persisted retry time or reconciliation. No polling or background execution is started here.' : pin.status === 'completed' ? 'Verified completion. Inspect the outcome evidence before creating an inactive review draft.' : pin.status === 'recovered' ? 'Recovery recorded; this does not prove the original operation succeeded.' : 'Explicit advance rechecks lifecycle, worker lease, Resource facts, policy and evidence.'}</p>
    ${referenceLink('attention', pin.attention_id, {projectId})}
    <ol>${Object.entries(pin.steps).map(([id, step]) => `<li><strong>${esc(id)} · ${esc(step.status)}</strong><p>${esc(step.failure_code || '')} · attempts ${esc(step.attempts)} · not before ${esc(step.not_before ? new Date(step.not_before * 1000).toISOString() : 'none')}</p>
      <p>Action ${referenceLink('action_intent', step.action_intent_id, {projectId})} · approval ${referenceLink('approval', step.approval_id, {projectId})} · originating rollback receipt ${esc(step.rollback_origin_receipt_id || 'none')}</p>
      <p>Evidence ${refs('evidence', Object.values(step.evidence || {}), projectId)} · output ${referenceLink('artifact', step.output_snapshot_id, {projectId})} · checksum ${esc(step.output_snapshot_checksum || 'none')}</p><details><summary>Authority decisions and exact checkpoint</summary><pre>${esc(JSON.stringify(step, null, 2))}</pre></details></li>`).join('')}</ol>
    <details><summary>Recorded deviations (do not grant authority)</summary>${pin.deviations.map(d => `<p>${esc(d.step_id)} · ${esc(d.reason)} · actor ${esc(d.actor_identity_id)} · ${refs('evidence', d.evidence_ids, projectId)} · ${referenceLink('definition', d.authority_definition.record_id, {projectId})}</p>`).join('') || '<p>None recorded.</p>'}</details>`;
}

function outcomeHtml(result, projectId) {
  const report = result.report;
  if (!report) return `<pre>${esc(JSON.stringify(result, null, 2))}</pre>`;
  const drafts = result.drafts || (result.draft ? [{draft:result.draft, knowledge_id:result.knowledge_id, diff:result.diff}] : []);
  return `<h4>Verified outcome review</h4><p>Source run ${esc(report.run_id || 'legacy')}</p><p>${report.review_needs?.length ? `Review required: ${esc(report.review_needs.join(', '))}` : 'Observed evidence verified; root cause remains an unconfirmed hypothesis.'}</p><p>Source ${referenceLink('definition', report.prior_definition.record_id, {projectId})} · Incident ${referenceLink('incident', report.incident_id, {projectId})} · Resources ${refs('resource', report.resource_ids, projectId)}</p>
    <p>Observed proof ${refs('evidence', (report.observations || []).flatMap(step => Object.values(step.evidence || {})), projectId)} · effect intents ${refs('action_intent', (report.observations || []).map(step => step.action_intent_id).filter(Boolean), projectId)}</p>
    ${drafts.map(row => `<p>${row.draft.definitionLifecycle === 'draft' ? 'Inactive draft — publication required after review and exact-revision scan.' : esc(row.draft.definitionLifecycle)} · ${referenceLink('skill', row.draft.skillId, {projectId, revision:row.draft.revision, label:`${row.draft.skillId} r${row.draft.revision}`})} · ${referenceLink('definition', row.draft.recordId, {projectId})} · ${referenceLink('knowledge', row.knowledge_id, {projectId})}${row.newer_revision ? ' · Newer revision exists: review conflict before publication.' : ''}</p><pre>${esc(row.diff || 'No instruction differences recorded.')}</pre>`).join('')}
    <p>${esc(report.limits)}</p><details><summary>Canonical observed evidence, scope and review needs</summary><pre>${esc(JSON.stringify(report, null, 2))}</pre></details>`;
}

export function installProcedureRun(host, assignment, { incidentId = '', runId = '' } = {}) {
  if (!host || host.dataset.procedureInstalled) return;
  host.dataset.procedureInstalled = 'true'; host.dataset.procedureRun = assignment.id;
  const projectId = assignment.project_id;
  const scope = captureProjectView(), current = () => scope.current() && host.isConnected;
  const base = `/api/execution-workers/assignments/${encodeURIComponent(assignment.id)}/procedure`;
  host.innerHTML = `<h3>Playbook run</h3><p>Canonical assignment ${esc(assignment.id)} · ${referenceLink('work_item', assignment.work_item_ref, {projectId})} · Resources ${refs('resource', assignment.resource_ids, projectId)}. Inspection, instructions and mutation authority remain distinct.</p>
    ${procedureDiscoveriesHtml(assignment, projectId)}
    <label>Current and retained runs<select data-procedure-history></select></label>
    <a data-procedure-run-link>Link to this run</a><p data-procedure-history-status></p>
    <div data-procedure-checkpoint>${checkpointHtml(assignment.procedure_checkpoint, projectId)}</div>
    <form data-procedure-start-form><label>Admitted exact revision<select name="definition">${(assignment.skill_refs || []).map((ref, i) => `<option value="${i}">${esc(ref.definition_id)} r${ref.revision} · ${esc(ref.checksum)}</option>`).join('')}</select></label><label>Governed input Artifact ID<input name="input_snapshot_id"></label><button type="submit">Start pinned playbook</button></form>
    <div>${[['inspect','Refresh checkpoint'],['advance','Advance one step / resume'],['outcome','Inspect verified outcome'],['outcome-draft','Create inactive outcome draft']].map(([id,label]) => `<button type="button" data-procedure-action="${id}">${label}</button>`).join('')}</div>
    <form data-procedure-deviation><label>Step ID<input name="step_id" required></label><label>Deviation reason<textarea name="reason" required></textarea></label><label>Evidence IDs (comma separated)<input name="evidence_ids" required></label><button type="submit">Record policy checked deviation</button></form>
    <section data-procedure-draft-fields><label>Related Incident ID (optional)<input data-procedure-incident value="${esc(incidentId)}"></label><label>New draft Skill ID (optional; otherwise use canonical source)<input data-procedure-draft-id></label></section>
    <div role="status" data-procedure-run-status></div><section data-procedure-outcome></section>`;
  const editors = {
    start: trackDirtyEditor(host.querySelector('[data-procedure-start-form]'), {label:'Playbook start inputs'}),
    deviate: trackDirtyEditor(host.querySelector('[data-procedure-deviation]'), {label:'Playbook deviation'}),
    'outcome-draft': trackDirtyEditor(host.querySelector('[data-procedure-draft-fields]'), {label:'Playbook outcome draft options'}),
  };
  owners.set(host, editors);
  for (const [action, editor] of Object.entries(editors)) {
    const button = document.createElement('button'); button.type = 'button';
    button.textContent = `Discard ${action} inputs`;
    button.addEventListener('click', () => confirmDiscard(editor));
    host.querySelector(action === 'start' ? '[data-procedure-start-form]' : action === 'deviate' ? '[data-procedure-deviation]' : '[data-procedure-draft-fields]').append(button);
  }
  let busy = false, sequence = 0;
  let selectedRun = assignment.procedure_checkpoint?.id || (assignment.procedure_checkpoint ? 'legacy' : '');
  let currentRun = selectedRun;
  const route = new URLSearchParams(location.search);
  if (route.get('consumer_id') === assignment.id && route.get('procedure_run')) selectedRun = route.get('procedure_run');
  if (runId) selectedRun = runId;
  const historySelect = host.querySelector('[data-procedure-history]');
  function renderHistory(runs) {
    historySelect.innerHTML = runs.map(pin => `<option value="${esc(pin.id)}">${esc(pin.id)} · ${esc(pin.status)}${pin.id === currentRun ? ' · current' : ' · retained'}</option>`).join('');
    historySelect.value = selectedRun;
    const historical = selectedRun && selectedRun !== currentRun;
    host.querySelector('[data-procedure-history-status]').textContent = historical ? 'Retained run: inspection and outcome review only. Advancing never changes this checkpoint.' : 'Current run. A changed input starts a separate admitted run only after verified completion.';
    for (const button of host.querySelectorAll('[data-procedure-action="advance"], [data-procedure-deviation] button')) button.disabled = historical;
    const link = host.querySelector('[data-procedure-run-link]');
    const path = referencePath('assignment', assignment.id, {projectId});
    link.href = `${path}&${new URLSearchParams({procedure_run:selectedRun})}`;
  }
  renderHistory([...(assignment.procedure_history || []), assignment.procedure_checkpoint].filter(Boolean).map(pin => ({...pin,id:pin.id || 'legacy'})));
  async function perform(action, body) {
    if (busy || !current()) return;
    const visit = ++sequence, valid = () => current() && visit === sequence;
    const result = host.querySelector('[data-procedure-run-status]');
    const editor = editors[action];
    const submitted = editor?.snapshot();
    const incident = host.querySelector('[data-procedure-incident]').value.trim();
    if (action === 'outcome-draft') body = {incident_id:incident || null, skill_id:host.querySelector('[data-procedure-draft-id]').value.trim() || null, run_id:selectedRun || null};
    if (['advance','deviate'].includes(action)) {
      if (selectedRun !== currentRun) return;
      body = {...(body || {}), run_id:currentRun || null};
    }
    if (!['inspect','outcome'].includes(action) && !await confirmAction({action:`Playbook ${action}`, target:`${assignment.id} · Project ${projectId} · ${assignment.resource_ids.join(', ')}`, risk:'high',
      consequence:action === 'outcome-draft' ? 'Derive an inactive Skill draft from verified evidence. Publication and assignment require separate review.' : 'Use the existing canonical execution API. Advancing can dispatch a registered action under current authority and exact approvals.',
      recovery:'Unknown outcomes must reconcile the originating ActionIntent before retry. Rollback requires declared recovery and originating receipts.', current:valid})) return;
    if (!valid()) return;
    busy = true; historySelect.disabled = true; result.textContent = 'Loading canonical result…';
    try {
      let path = action === 'inspect' ? base : `${base}/${action}`;
      if (['inspect','outcome'].includes(action)) {
        const params = new URLSearchParams();
        if (selectedRun) params.set('run_id',selectedRun);
        if (action === 'outcome' && incident) params.set('incident_id',incident);
        if (params.size) path += `?${params}`;
      }
      const response = await request(path, ['inspect','outcome'].includes(action) ? {} : {method:'POST', body:JSON.stringify(body || {})});
      if (!valid()) return;
      if (['outcome','outcome-draft'].includes(action)) host.querySelector('[data-procedure-outcome]').innerHTML = outcomeHtml(response, projectId);
      else {
        currentRun = response.current_run_id || '';
        selectedRun = response.procedure_checkpoint?.id || (response.procedure_checkpoint ? 'legacy' : '');
        host.querySelector('[data-procedure-checkpoint]').innerHTML = checkpointHtml(response.procedure_checkpoint, projectId);
        renderHistory(response.procedure_runs || []);
      }
      if (action !== 'outcome-draft' || response.draft) editor?.markSaved(submitted);
      result.textContent = 'Canonical result loaded. Existing pins and server enforcement remain authoritative.';
    } catch (error) { if (valid()) result.textContent = `Operation blocked: ${procedureError(error)}. Edits are preserved. Inspect canonical authority, Attention, evidence and originating ActionIntent; do not repeat an uncertain effect.`; }
    finally { busy = false; historySelect.disabled = false; }
  }
  historySelect.addEventListener('change', () => { selectedRun = historySelect.value; perform('inspect'); });
  if (selectedRun !== currentRun) perform('inspect');
  host.querySelectorAll('[data-procedure-action]').forEach(button => button.addEventListener('click', () => perform(button.dataset.procedureAction)));
  host.querySelector('[data-procedure-start-form]').addEventListener('submit', event => {
    event.preventDefault(); const form = event.target;
    const definition = assignment.skill_refs?.[Number(form.elements.definition.value)];
    if (!definition) { host.querySelector('[data-procedure-run-status]').textContent = 'No admitted exact revision. Inspect assignment discovery and re-admission blockers.'; return; }
    perform('start', {definition, input_snapshot_id:form.elements.input_snapshot_id.value.trim()});
  });
  host.querySelector('[data-procedure-deviation]').addEventListener('submit', event => {
    event.preventDefault(); const form = event.target;
    perform('deviate', {step_id:form.elements.step_id.value.trim(), reason:form.elements.reason.value.trim(), evidence_ids:form.elements.evidence_ids.value.split(',').map(v => v.trim()).filter(Boolean)});
  });
}

export function installActionProcedureRuns(host, history, id) {
  const view = captureProjectView();
      const controls = document.createElement('section');
      const load = document.createElement('button');
      load.type = 'button'; load.textContent = 'Inspect pinned playbook run';
      const runHost = document.createElement('div');
      controls.append(load, runHost); host.append(controls);
      load.addEventListener('click', async () => {
        load.disabled = true;
        try {
          const response = await request('/api/execution-workers/assignments');
          if (!view.current() || !runHost.isConnected) return;
          const linked = (response.items || []).filter(item => item.project_id === history.intent?.project_id
            && [...(item.procedure_history || []), item.procedure_checkpoint].some(pin => Object.values(pin?.steps || {}).some(step => step.action_intent_id === id)));
          runHost.textContent = linked.length ? '' : 'No visible pinned step references this exact ActionIntent. Inspect discovery and assignment admission; no run is inferred from prose.';
          for (const item of linked) { const section = document.createElement('section'); runHost.append(section); installProcedureRun(section, item, {runId: [...(item.procedure_history || []), item.procedure_checkpoint].find(pin => Object.values(pin?.steps || {}).some(step => step.action_intent_id === id))?.id || 'legacy'}); }
        } catch (error) { if (view.current() && runHost.isConnected) runHost.textContent = `Pinned run unavailable: ${error.message}`; }
        finally { load.disabled = false; }
      });
}
