import { formValidation } from './form_validation.js';
import { trackDirtyEditor } from './dirty_editor.js';
import { projectPath } from './reference_navigation.js';

export const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
export const requiredProfiles = new Set(['repository-write', 'orchestration-only']);
export function profilePath(projectId, profileId = '') {
  return projectPath('definitions', { execution_profile: profileId }, projectId);
}
export function recordPath(projectId, recordId) {
  return `${profilePath(projectId)}&definition_record=${encodeURIComponent(recordId)}`;
}

export function editorShell(host) {
  host.innerHTML = `<h3>Execution Profiles</h3>
    <p>Manage this Project's execution catalog. Published revisions are immutable; saving creates a new draft.</p>
    <p data-profile-status role="status" aria-live="polite">Select a Project to load its catalog.</p>
    <div class="developer-toolbar">
      <label>Find Execution Profiles <input data-profile-search type="search"></label>
      <button type="button" data-profile-refresh class="ghost-button">Refresh profiles</button>
      <button type="button" data-profile-new class="ghost-button">New Execution Profile</button>
    </div>
    <div data-profile-source></div><div data-profile-list class="comm-log"></div>
    <form data-profile-form novalidate hidden>
      <fieldset data-profile-fields><legend>Execution Profile draft</legend>
        <div class="route-test">
          <label>Profile ID <input name="profile-id" required pattern="[a-z0-9][a-z0-9-]*"></label>
          <label>Name <input name="profile-name" required></label>
          <label>Description <textarea name="profile-description" required rows="3"></textarea></label>
          <label>Workspace <select name="profile-workspace"><option value="repository">Repository</option><option value="scratch">Scratch</option></select></label>
          <label>Repository access <select name="profile-access"><option value="mutable">Mutable</option><option value="read-only">Read only</option><option value="none">None</option></select></label>
          <label><input name="profile-default" type="checkbox"> Use as this Project's default</label>
        </div>
        <fieldset><legend>Required worker capabilities</legend>
          <label><input name="cap-command_execution" type="checkbox" checked disabled> Command execution (required)</label>
          <label><input name="cap-git" type="checkbox" disabled> Git (required for repository workspaces)</label>
          <label><input name="cap-container" type="checkbox"> Container execution</label>
          <label><input name="cap-artifact_upload" type="checkbox"> Artifact upload</label>
        </fieldset>
        <details><summary>Broker operations and platform constraints</summary>
          <label>Requested control-plane operations (one per line)<textarea name="profile-operations" rows="4"></textarea></label>
          <p>Direct network access and host mutation are disabled by platform invariants. Worker requirements and requested operations do not grant authority; canonical policy and assignment permissions still apply.</p>
        </details>
        <label>Change reason <input name="profile-reason" required></label>
        <div class="developer-toolbar">
          <button type="submit" class="ghost-button">Save new Project draft</button>
          <button type="button" data-profile-clone class="ghost-button">Clone profile</button>
          <button type="button" data-profile-remove class="ghost-button danger-button">Remove from next catalog</button>
          <button type="button" data-profile-discard class="ghost-button">Discard edits</button>
        </div>
      </fieldset>
    </form>
    <div data-profile-impact class="comm-log"></div>
    <div data-profile-draft class="comm-entry" hidden></div>
    <p>Profiles are entries in a versioned catalog. Independent profile archive/restore is not supported. Catalog quarantine and rollback remain available in the <a data-profile-history>Definition lifecycle view</a>; review usage before changing an effective catalog.</p>`;
  const form = host.querySelector('[data-profile-form]');
  const validation = formValidation(form);
  const dirty = trackDirtyEditor(form, { label: 'Execution Profile' });
  const field = name => form.elements.namedItem(name);
  function constraints() {
    const scratch = field('profile-workspace').value === 'scratch';
    if (scratch) field('profile-access').value = 'none';
    else if (field('profile-access').value === 'none') field('profile-access').value = 'read-only';
    field('profile-access').disabled = scratch;
    field('cap-git').checked = !scratch;
  }
  field('profile-workspace').addEventListener('change', constraints);
  return { form, field, dirty, validation,
    fill(profile, { fresh = false, isDefault = false } = {}) {
      validation.clear(); form.hidden = false;
      for (const [name, value] of Object.entries({ 'profile-id': profile.id, 'profile-name': profile.name,
        'profile-description': profile.description, 'profile-workspace': profile.workspace_mode,
        'profile-access': profile.repository_access, 'profile-operations': (profile.control_plane_operations || []).join('\n'),
        'profile-reason': '' })) field(name).value = value || '';
      field('profile-id').readOnly = !fresh;
      field('profile-default').checked = isDefault;
      field('profile-default').disabled = isDefault && !fresh;
      for (const capability of ['container', 'artifact_upload']) field(`cap-${capability}`).checked = (profile.required_worker_capabilities || []).includes(capability);
      constraints(); dirty.markSaved();
    },
    capture() {
      return { id: field('profile-id').value.trim(), name: field('profile-name').value.trim(),
        description: field('profile-description').value.trim(), workspace_mode: field('profile-workspace').value,
        repository_access: field('profile-access').value,
        required_worker_capabilities: ['command_execution', 'git', 'container', 'artifact_upload'].filter(cap => field(`cap-${cap}`).checked),
        control_plane_operations: field('profile-operations').value.split('\n').map(line => line.trim()).filter(Boolean),
        network_enabled: false, host_mutation: false };
    },
  };
}
