import {
  csv,
  esc,
  dateTimeValue,
  field,
  input,
  lifecycleSelect,
  numberOrNull,
  textarea,
} from './definition_typed_editor_shared.js';

function grantHtml(grant, roleIndex, grantIndex) {
  return '<div class="comm-entry typed-authority-grant" data-role-index="' + roleIndex
    + '" data-grant-index="' + grantIndex + '">'
    + '<div class="section-title"><strong>Grant ' + esc(grant.id) + '</strong>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-grant">Remove grant</button></div>'
    + '<div class="route-test">'
    + field('Grant ID', input('typed-grant-id', grant.id))
    + field('Capability', input('typed-grant-capability', grant.capability))
    + field('Authority level', '<select class="typed-grant-level">'
      + ['read', 'recommend', 'prepare', 'execute', 'approve'].map((item) => (
        '<option value="' + item + '"' + (item === grant.level ? ' selected' : '') + '>' + item + '</option>'
      )).join('') + '</select>')
    + field('Projects', input('typed-grant-projects', (grant.project_ids || []).join(', ')))
    + field('Resource IDs', input('typed-grant-resource-ids', (grant.resource_ids || []).join(', ')))
    + field('Resource types', input('typed-grant-resource-types', (grant.resource_types || []).join(', ')))
    + field('Resource risks', input('typed-grant-resource-risks', (grant.resource_risks || []).join(', ')))
    + field('Sensitivities', input('typed-grant-sensitivities', (grant.resource_sensitivities || []).join(', ')))
    + field('Environments', input('typed-grant-environments', (grant.environments || []).join(', ')))
    + field('Max amount USD', input('typed-grant-amount', grant.max_amount_usd ?? '', 'type="number" min="0" step="0.01"'))
    + field('Max input tokens', input('typed-grant-input-tokens', grant.max_input_tokens ?? '', 'type="number" min="0"'))
    + field('Max output tokens', input('typed-grant-output-tokens', grant.max_output_tokens ?? '', 'type="number" min="0"'))
    + field('Max model calls', input('typed-grant-model-calls', grant.max_model_calls ?? '', 'type="number" min="0"'))
    + field('Autonomy risk', '<select class="typed-grant-autonomy">'
      + ['low', 'medium', 'high', 'critical'].map((item) => (
        '<option value="' + item + '"' + (item === (grant.max_autonomous_risk || 'low') ? ' selected' : '') + '>'
        + item + '</option>'
      )).join('') + '</select>')
    + field('Required approvals', input('typed-grant-approval-count', grant.approvals?.count ?? 0, 'type="number" min="0" max="20"'))
    + field('Approver roles', input('typed-grant-approval-roles', (grant.approvals?.role_ids || []).join(', ')))
    + '</div></div>';
}

function roleHtml(role, roleIndex) {
  return '<div class="comm-entry typed-authority-role" data-role-index="' + roleIndex + '">'
    + '<div class="section-title"><strong>' + esc(role.name || role.id) + '</strong>'
    + '<div class="developer-toolbar">'
    + '<button type="button" class="ghost-button" data-typed-action="add-grant">Add grant</button>'
    + '<button type="button" class="ghost-button" data-typed-action="clone-role">Clone role</button>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-role">Remove role</button>'
    + '</div></div>'
    + '<div class="route-test">'
    + field('Role ID', input('typed-role-id', role.id))
    + field('Name', input('typed-role-name', role.name))
    + field('Lifecycle', lifecycleSelect(role.lifecycle || 'active'))
    + field('Inherits', input('typed-role-inherits', (role.inherits || []).join(', ')))
    + '</div>'
    + field('Description', textarea('typed-role-description', role.description, 2))
    + '<div class="typed-grant-list">'
    + (role.grants || []).map((grant, grantIndex) => grantHtml(grant, roleIndex, grantIndex)).join('')
    + '</div></div>';
}

function bindingHtml(binding, index) {
  return '<div class="comm-entry typed-authority-binding" data-binding-index="' + index + '">'
    + '<div class="section-title"><strong>Binding ' + esc(binding.id) + '</strong>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-binding">Remove</button></div>'
    + '<div class="route-test">'
    + field('Binding ID', input('typed-binding-id', binding.id))
    + field('Role ID', input('typed-binding-role', binding.role_id))
    + field('Subject kind', '<select class="typed-binding-kind"><option value="identity"'
      + (binding.subject_kind === 'identity' ? ' selected' : '') + '>identity</option><option value="team"'
      + (binding.subject_kind === 'team' ? ' selected' : '') + '>team</option></select>')
    + field('Subject ID', input('typed-binding-subject', binding.subject_id))
    + field('Organization', input('typed-binding-org', binding.organization_id || ''))
    + field('Workspace', input('typed-binding-workspace', binding.workspace_id || ''))
    + field('Projects', input('typed-binding-projects', (binding.project_ids || []).join(', ')))
    + '</div></div>';
}

function delegationHtml(item, index) {
  return '<div class="comm-entry typed-authority-delegation" data-delegation-index="' + index + '">'
    + '<div class="section-title"><strong>Delegation ' + esc(item.id) + '</strong>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-delegation">Remove</button></div>'
    + '<div class="route-test">'
    + field('Delegation ID', input('typed-delegation-id', item.id))
    + field('Role ID', input('typed-delegation-role', item.role_id))
    + field('Delegate identity', input('typed-delegation-identity', item.delegate_identity_id))
    + field('Delegated by', input('typed-delegation-by', item.delegated_by_identity_id))
    + field('Organization', input('typed-delegation-org', item.organization_id || ''))
    + field('Workspace', input('typed-delegation-workspace', item.workspace_id || ''))
    + field('Projects', input('typed-delegation-projects', (item.project_ids || []).join(', ')))
    + field('Expires', input('typed-delegation-expires', dateTimeValue(item.expires_at), 'type="datetime-local"'))
    + '</div>'
    + field('Reason', textarea('typed-delegation-reason', item.reason, 2))
    + '</div>';
}

export function renderAuthority(payload, host) {
  host.innerHTML = '<div class="section-title"><span>Operational Roles</span></div>'
    + '<div id="definition-typed-role-list">'
    + (payload.roles || []).map(roleHtml).join('')
    + '</div>'
    + '<div class="section-title"><span>Identity / Team bindings</span></div>'
    + '<div id="definition-typed-binding-list">'
    + (payload.bindings || []).map(bindingHtml).join('')
    + '</div>'
    + '<div class="section-title"><span>Delegated authority</span></div>'
    + '<div id="definition-typed-delegation-list">'
    + (payload.delegations || []).map(delegationHtml).join('')
    + '</div>';
}

function readGrant(node) {
  return {
    id: node.querySelector('.typed-grant-id').value.trim(),
    capability: node.querySelector('.typed-grant-capability').value.trim(),
    level: node.querySelector('.typed-grant-level').value,
    project_ids: csv(node.querySelector('.typed-grant-projects').value),
    resource_ids: csv(node.querySelector('.typed-grant-resource-ids').value),
    resource_types: csv(node.querySelector('.typed-grant-resource-types').value),
    resource_risks: csv(node.querySelector('.typed-grant-resource-risks').value),
    resource_sensitivities: csv(node.querySelector('.typed-grant-sensitivities').value),
    environments: csv(node.querySelector('.typed-grant-environments').value),
    max_amount_usd: numberOrNull(node.querySelector('.typed-grant-amount').value),
    max_input_tokens: numberOrNull(node.querySelector('.typed-grant-input-tokens').value),
    max_output_tokens: numberOrNull(node.querySelector('.typed-grant-output-tokens').value),
    max_model_calls: numberOrNull(node.querySelector('.typed-grant-model-calls').value),
    max_autonomous_risk: node.querySelector('.typed-grant-autonomy').value,
    approvals: {
      count: Number(node.querySelector('.typed-grant-approval-count').value || 0),
      role_ids: csv(node.querySelector('.typed-grant-approval-roles').value),
    },
  };
}

export function captureAuthority() {
  const roles = [...document.querySelectorAll('.typed-authority-role')].map((node) => ({
    id: node.querySelector('.typed-role-id').value.trim(),
    name: node.querySelector('.typed-role-name').value.trim(),
    description: node.querySelector('.typed-role-description').value.trim(),
    lifecycle: node.querySelector('.typed-role-lifecycle').value,
    inherits: csv(node.querySelector('.typed-role-inherits').value),
    grants: [...node.querySelectorAll('.typed-authority-grant')].map(readGrant),
  }));
  const bindings = [...document.querySelectorAll('.typed-authority-binding')].map((node) => ({
    id: node.querySelector('.typed-binding-id').value.trim(),
    role_id: node.querySelector('.typed-binding-role').value.trim(),
    subject_kind: node.querySelector('.typed-binding-kind').value,
    subject_id: node.querySelector('.typed-binding-subject').value.trim(),
    organization_id: node.querySelector('.typed-binding-org').value.trim() || null,
    workspace_id: node.querySelector('.typed-binding-workspace').value.trim() || null,
    project_ids: csv(node.querySelector('.typed-binding-projects').value),
  }));
  const delegations = [...document.querySelectorAll('.typed-authority-delegation')].map((node) => {
    const expiry = node.querySelector('.typed-delegation-expires').value;
    return {
      id: node.querySelector('.typed-delegation-id').value.trim(),
      role_id: node.querySelector('.typed-delegation-role').value.trim(),
      delegate_identity_id: node.querySelector('.typed-delegation-identity').value.trim(),
      delegated_by_identity_id: node.querySelector('.typed-delegation-by').value.trim(),
      organization_id: node.querySelector('.typed-delegation-org').value.trim() || null,
      workspace_id: node.querySelector('.typed-delegation-workspace').value.trim() || null,
      project_ids: csv(node.querySelector('.typed-delegation-projects').value),
      expires_at: expiry ? new Date(expiry).getTime() / 1000 : 0,
      reason: node.querySelector('.typed-delegation-reason').value.trim(),
    };
  });
  return { roles, bindings, delegations };
}

export function newRole(id) {
  return {
    id,
    name: 'New Role',
    description: 'Describe this operational Role.',
    lifecycle: 'active',
    inherits: [],
    grants: [],
  };
}

export function newGrant(role, index) {
  return {
    id: role.id + '.grant-' + index,
    capability: 'resource.read',
    level: 'read',
    project_ids: [],
    resource_ids: [],
    resource_types: [],
    resource_risks: [],
    resource_sensitivities: [],
    environments: [],
    max_amount_usd: null,
    max_input_tokens: null,
    max_output_tokens: null,
    max_model_calls: null,
    max_autonomous_risk: 'low',
    approvals: { count: 0, role_ids: [] },
  };
}

export function newBinding(id, roleId, source) {
  return {
    id,
    role_id: roleId,
    subject_kind: 'identity',
    subject_id: '',
    organization_id: source.scope_type === 'organization' ? source.scope_id : null,
    workspace_id: source.scope_type === 'workspace' ? source.scope_id : null,
    project_ids: source.scope_type === 'project' ? [source.scope_id] : [],
  };
}

export function newDelegation(id, roleId) {
  return {
    id,
    role_id: roleId,
    delegate_identity_id: '',
    delegated_by_identity_id: '',
    organization_id: null,
    workspace_id: null,
    project_ids: [],
    expires_at: Date.now() / 1000 + 86400,
    reason: 'Temporary delegated authority.',
  };
}
