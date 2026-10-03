import { confirmAction } from './action_confirmation.js';
import { editorShell, esc, requiredProfiles, profilePath, recordPath } from './execution_profile_editor.js';
import { currentProjectId, projectViewOperation } from './project_view_scope.js';
import { confirmDiscard } from './dirty_editor.js';
import { publishRecord } from './definition_publication_ui.js';

const host = document.getElementById('execution-profile-management');
if (host) bind();
function bind() {
  const editor = editorShell(host);
  const node = key => host.querySelector(`[data-profile-${key}]`);
  const status = message => { node('status').textContent = message; };
  let source = null, actor = null, selected = null, draft = null;
  let generation = 0, busy = false, fingerprint = '';
  let active = /\/definitions\/?$/.test(location.pathname);
  const manageable = () => actor?.principal_kind === 'service'
    ? (actor.service_scopes || []).includes('definitions:admin')
    : actor?.principal_kind === 'human' && ['mfa', 'local_trusted'].includes(actor.assurance)
      && (actor.roles || []).some(role => ['owner', 'admin'].includes(role));
  function controls() {
    node('fields').disabled = busy || !manageable();
    node('new').disabled = busy || !source || !manageable();
    node('refresh').disabled = busy;
    node('remove').disabled = !selected || requiredProfiles.has(selected)
      || selected === source?.payload.default_profile_id;
  }
  function operation() {
    const view = projectViewOperation(status);
    const visit = generation;
    return { ...view, current: () => view.current() && visit === generation };
  }
  function renderList() {
    const search = node('search').value.trim().toLowerCase();
    node('list').innerHTML = (source?.payload.profiles || [])
      .filter(profile => `${profile.id} ${profile.name}`.toLowerCase().includes(search))
      .map(profile => `<div class="comm-entry"><button type="button" data-select-profile="${esc(profile.id)}" ${busy ? 'disabled' : ''}>${esc(profile.name)}</button>
        <small>${esc(profile.id)} · ${esc(profile.workspace_mode)} · ${esc(profile.repository_access)}${profile.id === source.payload.default_profile_id ? ' · Project default' : ''}</small></div>`).join('')
      || '<p>No Execution Profiles match this search.</p>';
  }
  function impact(usage) {
    node('impact').innerHTML = `<h4>Current consumers in this Project context</h4>
      <p>${esc(usage.count)} consumer reference(s), including ${esc(usage.restricted_count)} restricted reference(s). ${usage.truncated ? 'Display limited; count includes omitted references.' : ''}</p>
      <p>Workspace Agent Profiles are shared configurations. Queued work resolves the catalog when dispatched; existing executions retain their pinned revision.</p>
      ${(usage.items || []).map(item => `<div class="comm-entry"><strong>${esc(item.label)}</strong><small>${esc(item.object_type)} · ${esc(item.object_id)} · ${esc(item.binding)}${item.revision ? ` · r${esc(item.revision)}` : ''}</small></div>`).join('')}`;
  }
  async function usage(id, op) {
    const value = await op.request(`/api/execution-profiles/${encodeURIComponent(id)}/usage?project_id=${encodeURIComponent(currentProjectId())}`);
    if (!op.current()) throw new DOMException('Selection changed', 'AbortError');
    if (!value.available) throw new Error('Consumer impact is unavailable');
    impact(value);
    return value;
  }
  async function select(id, updateUrl = true) {
    if (busy || !confirmDiscard(editor.dirty)) return;
    const profile = source?.payload.profiles.find(item => item.id === id);
    if (!profile) { status('This Execution Profile is not in the effective catalog. Refresh or choose another profile.'); return; }
    generation += 1; selected = id; draft = null; node('draft').hidden = true;
    editor.fill(profile, { isDefault: id === source.payload.default_profile_id });
    if (updateUrl) history.pushState(history.state, '', profilePath(currentProjectId(), id));
    editor.dirty.location = location.href; editor.dirty.historyState = history.state;
    controls(); node('impact').textContent = 'Loading consumer impact…';
    const op = operation();
    try { await usage(id, op); }
    catch (error) { if (op.current()) node('impact').textContent = `Consumer impact unavailable: ${error.message}. Retry by selecting this profile again.`; }
  }
  async function load() {
    if (!active || busy || !currentProjectId() || !confirmDiscard(editor.dirty)) return;
    generation += 1; const op = operation();
    source = null; selected = null; draft = null; actor = null;
    editor.form.hidden = true; node('draft').hidden = true;
    node('list').replaceChildren(); node('impact').replaceChildren(); node('source').replaceChildren();
    controls(); status('Loading canonical Execution Profiles…');
    try {
      const [catalog, identity] = await Promise.all([
        op.request(`/api/execution-profiles?project_id=${encodeURIComponent(currentProjectId())}`),
        op.request('/api/identity/me'),
      ]);
      if (!op.current()) return;
      const response = await op.request('/api/definitions/records?kind=execution-profile-catalog');
      if (!op.current()) return;
      source = response.items.find(item => item.record_id === catalog.definition.record_id);
      if (!source) throw new Error('Effective catalog revision is unavailable');
      actor = identity.actor || identity;
      node('source').innerHTML = `<p>Effective catalog: ${esc(source.scope_type)} ${esc(source.scope_id || '')} · r${esc(source.revision)} · ${esc(source.lifecycle)}.</p>
        <p>Source checksum: <code>${esc(source.checksum)}</code>. Changes create a Project draft for ${esc(currentProjectId())}.</p>`;
      node('history').href = recordPath(currentProjectId(), source.record_id);
      renderList(); controls();
      status(manageable() ? 'Choose a profile or create a new one. Server authorization and validation apply to every mutation.' : 'Read only: managing Project definitions requires admin authority and elevated assurance, or a service definitions:admin scope.');
      const id = new URLSearchParams(location.search).get('execution_profile');
      if (id) await select(id, false);
    } catch (error) { if (op.current()) status(`Execution Profiles unavailable: ${error.message}. Use Refresh profiles to retry.`); }
  }
  function fresh(profile) {
    if (busy || !source || !manageable() || !confirmDiscard(editor.dirty)) return;
    generation += 1; selected = null; draft = null;
    node('draft').hidden = true; node('impact').textContent = 'New profile: no effective consumers until its catalog is published.';
    editor.fill(profile, { fresh: true }); controls(); editor.field('profile-id').focus();
  }
  async function save(remove = false) {
    if (busy || !source || !manageable()) return;
    const profile = editor.capture();
    const duplicate = !selected && source.payload.profiles.some(item => item.id === profile.id);
    if (!editor.validation.validate(duplicate ? [{ field: 'profile-id', message: 'Choose a unique profile ID.' }] : [])) return;
    if (remove && (!selected || requiredProfiles.has(selected) || selected === source.payload.default_profile_id)) return;
    const op = operation(); const project = currentProjectId();
    const payload = structuredClone(source.payload);
    payload.profiles = payload.profiles.filter(item => item.id !== selected);
    if (!remove) payload.profiles.push(profile);
    if (!remove && editor.field('profile-default').checked) payload.default_profile_id = profile.id;
    busy = true; controls(); renderList();
    try {
      if (selected) {
        const preview = await usage(selected, op);
        if (remove && !await confirmAction({ action: 'Remove Execution Profile from draft', target: `${selected} in Project ${project}`, risk: 'bounded', consequence: `Remove ${selected} from the next Project catalog? ${preview.count} consumer reference(s) may require reassignment; pinned executions keep their revision. Saving creates an inactive draft.`, recovery: 'The published catalog stays effective until a validated draft is published.', current: op.current })) return;
      }
      const response = await op.request('/api/definitions/drafts', { method: 'POST', body: JSON.stringify({
        definition_id: source.definition_id, kind: source.kind,
        definition_schema_version: source.definition_schema_version,
        scope_type: 'project', scope_id: project, payload,
        derived_from_record_id: source.scope_type === 'project' && source.scope_id === project ? source.record_id : null,
        reason: `${editor.field('profile-reason').value.trim()} · based on ${source.record_id} r${source.revision} checksum ${source.checksum}`,
      }) });
      if (!op.current()) return;
      draft = response.record; editor.dirty.markSaved();
      node('draft').hidden = false;
      node('draft').innerHTML = `<h4>Project draft r${esc(draft.revision)} · inactive</h4><p>Saving does not change runtime resolution. Validate and publish this immutable candidate through the canonical lifecycle.</p>
        <button type="button" data-profile-publish>Validate and publish draft</button>
        <a href="${esc(recordPath(project, draft.record_id))}">Inspect draft, approvals and lifecycle</a>`;
      status(`Saved Project draft ${draft.record_id}. Existing published profiles remain effective.`);
    } catch (error) { if (op.current()) { editor.validation.server(error); status(`Draft was not saved: ${error.message}`); } }
    finally { if (op.current()) { busy = false; controls(); renderList(); } }
  }
  async function publish() {
    if (busy || !draft || !confirmDiscard(editor.dirty)) return;
    const op = operation(); busy = true; controls();
    node('draft').querySelector('button').disabled = true;
    try {
      if (selected) await usage(selected, op);
      if (draft.payload.default_profile_id !== source.payload.default_profile_id) await usage(source.payload.default_profile_id, op);
      const validated = await op.request(`/api/definitions/${encodeURIComponent(draft.record_id)}/validate`, { method: 'POST', body: '{}' });
      if (!op.current()) return;
      const records = await op.request('/api/definitions/records?kind=execution-profile-catalog');
      if (!op.current()) return;
      const catalog = await op.request(`/api/execution-profiles?project_id=${encodeURIComponent(currentProjectId())}`);
      if (!op.current()) return;
      if (catalog.definition.record_id !== source.record_id) throw new Error('The effective catalog changed since editing began. Refresh and review its current revision');
      const current = records.items.find(item => item.definition_id === source.definition_id && item.scope_type === 'project' && item.scope_id === currentProjectId() && item.lifecycle === 'published');
      const expected = source.scope_type === 'project' ? source.revision : 0;
      if ((current?.revision || 0) !== expected) throw new Error('The Project catalog changed since editing began. Refresh and review your changes against its current revision');
      const published = await publishRecord(validated.record, { actor, active: current, setStatus: op.status });
      if (published && op.current()) { busy = false; await load(); }
    } catch (error) { if (op.current()) status(`Publication blocked: ${error.message}. Draft remains available for review.`); }
    finally {
      if (op.current()) { busy = false; controls(); node('draft').querySelector('button').disabled = false; }
    }
  }
  node('search').addEventListener('input', renderList);
  node('refresh').addEventListener('click', load);
  node('list').addEventListener('click', event => { const button = event.target.closest('[data-select-profile]'); if (button) select(button.dataset.selectProfile); });
  node('new').addEventListener('click', () => fresh({ id: '', name: '', description: '', workspace_mode: 'scratch', repository_access: 'none', required_worker_capabilities: ['command_execution'] }));
  node('clone').addEventListener('click', () => fresh({ ...editor.capture(), id: '', name: `${editor.field('profile-name').value} copy` }));
  node('discard').addEventListener('click', () => { if (confirmDiscard(editor.dirty) && selected) select(selected, false); });
  node('remove').addEventListener('click', () => save(true));
  editor.form.addEventListener('submit', event => { event.preventDefault(); save(); });
  node('draft').addEventListener('click', event => { if (event.target.closest('[data-profile-publish]')) publish(); });
  window.addEventListener('codex:project-changed', () => {
    generation += 1; busy = false; source = null; actor = null; selected = null; draft = null; fingerprint = '';
    editor.dirty.discard(); editor.form.hidden = true; node('draft').hidden = true;
    for (const key of ['source', 'list', 'impact']) node(key).replaceChildren();
    controls(); load();
  });
  window.addEventListener('codex:project-workspace-page', event => {
    active = event.detail?.workspace === 'definitions' || event.detail?.page === 'definitions';
    if (active && !source) load();
  });
  window.addEventListener('codex:definition-registry-rendered', event => {
    if (event.detail?.projectId !== currentProjectId()) return;
    const next = JSON.stringify((event.detail.records || []).filter(item => item.kind === 'execution-profile-catalog' && ['published', 'quarantined'].includes(item.lifecycle)).map(item => [item.record_id, item.lifecycle]));
    if (fingerprint && next !== fingerprint && !editor.dirty.dirty()) {
      if (busy) { fingerprint = ''; } else load();
    }
    fingerprint = next;
  });
  window.addEventListener('popstate', () => { if (active && source) select(new URLSearchParams(location.search).get('execution_profile') || source.payload.default_profile_id, false); });
  controls(); load();
}
