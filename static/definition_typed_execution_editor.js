import {
  field,
  input,
  lifecycleSelect,
  lines,
  pairs,
  pairText,
  textarea,
} from './definition_typed_editor_shared.js';

export const STRUCTURAL_EXECUTION_ROLES = new Set(['orchestrator', 'quinn', 'release-manager']);

function roleHtml(role, index) {
  return '<div class="comm-entry typed-execution-role" data-role-index="' + index + '">'
    + '<div class="section-title"><strong>' + (role.name || role.id) + '</strong>'
    + '<div class="developer-toolbar">'
    + '<button type="button" class="ghost-button" data-typed-action="clone-role">Clone role</button>'
    + '<button type="button" class="ghost-button" data-typed-action="remove-role"'
    + (STRUCTURAL_EXECUTION_ROLES.has(role.id) ? ' disabled title="Required structural role"' : '') + '>Remove role</button>'
    + '</div></div>'
    + '<div class="route-test">'
    + field('Role ID', input('typed-role-id', role.id))
    + field('Name', input('typed-role-name', role.name))
    + field('Lane', input('typed-role-lane', role.lane))
    + field('Lifecycle', lifecycleSelect(role.lifecycle || 'active'))
    + field('Automatic selection', '<input class="typed-role-auto-select" type="checkbox"'
      + (role.auto_select !== false ? ' checked' : '') + ' />')
    + '</div>'
    + field('Description', textarea('typed-role-description', role.description, 2))
    + field('Expected work (one per line)', textarea('typed-role-expected', (role.expected_work || []).join('\n')))
    + field('Must refuse / reroute', textarea('typed-role-refuse', (role.must_refuse || []).join('\n')))
    + field('Required artifacts', textarea('typed-role-artifacts', (role.required_artifacts || []).join('\n')))
    + field('Handoff targets', textarea('typed-role-hands-to', (role.hands_to || []).join('\n')))
    + field('Failure conditions', textarea('typed-role-failures', (role.failure_conditions || []).join('\n')))
    + field('Routing keywords', textarea('typed-role-keywords', (role.keywords || []).join('\n')))
    + '</div>';
}

export function renderExecution(payload, host) {
  host.innerHTML = '<div class="comm-entry">'
    + field('Change classifications', textarea('typed-exec-classifications', (payload.change_classifications || []).join('\n'), 4))
    + field('Shared execution rules', textarea('typed-exec-shared-rules', (payload.shared_execution_rules || []).join('\n'), 5))
    + field('Executive defaults (key=role)', textarea('typed-exec-defaults', pairText(payload.executive_default_execution_role), 4))
    + field('Owner mappings (owner=role)', textarea('typed-exec-owner-map', pairText(payload.owner_to_execution_role), 5))
    + '</div>'
    + '<div class="section-title"><span>Execution Role contracts</span></div>'
    + '<div id="definition-typed-role-list">'
    + (payload.roles || []).map(roleHtml).join('')
    + '</div>';
}

export function captureExecution() {
  return {
    change_classifications: lines(document.querySelector('.typed-exec-classifications')?.value),
    shared_execution_rules: lines(document.querySelector('.typed-exec-shared-rules')?.value),
    executive_default_execution_role: pairs(document.querySelector('.typed-exec-defaults')?.value),
    owner_to_execution_role: pairs(document.querySelector('.typed-exec-owner-map')?.value),
    roles: [...document.querySelectorAll('.typed-execution-role')].map((node) => ({
      id: node.querySelector('.typed-role-id').value.trim(),
      name: node.querySelector('.typed-role-name').value.trim(),
      lane: node.querySelector('.typed-role-lane').value.trim(),
      description: node.querySelector('.typed-role-description').value.trim(),
      lifecycle: node.querySelector('.typed-role-lifecycle').value,
      expected_work: lines(node.querySelector('.typed-role-expected').value),
      must_refuse: lines(node.querySelector('.typed-role-refuse').value),
      required_artifacts: lines(node.querySelector('.typed-role-artifacts').value),
      hands_to: lines(node.querySelector('.typed-role-hands-to').value),
      failure_conditions: lines(node.querySelector('.typed-role-failures').value),
      keywords: lines(node.querySelector('.typed-role-keywords').value),
      auto_select: node.querySelector('.typed-role-auto-select').checked,
    })),
  };
}

export function newRole(id) {
  return {
    id,
    name: 'New Role',
    lane: 'implementation',
    description: 'Describe this execution contract.',
    lifecycle: 'active',
    expected_work: ['Define expected work.'],
    must_refuse: ['Refuse work outside this role.'],
    required_artifacts: ['Evidence appropriate to the work.'],
    hands_to: ['orchestrator'],
    failure_conditions: ['Escalate when the contract cannot be satisfied.'],
    keywords: [],
    auto_select: false,
  };
}
