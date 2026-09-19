import { request } from './api_client.js';

const state = {
  roles: [],
  catalogDefinition: null,
  activations: [],
  selectedRoleId: '',
  selectedActivationId: '',
  activation: null,
  revisions: [],
  mode: 'activation',
};

const esc = (value) => String(value ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');

function fmtTime(value) {
  if (!value) return '—';
  const date = new Date(Number(value) * 1000);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function statusBadge(value, extra = '') {
  const text = String(value || 'unknown');
  return `<span class="exec-mgmt-badge ${esc(text)} ${esc(extra)}">${esc(text)}</span>`;
}

function list(values) {
  return Array.isArray(values) ? values : [];
}

function ensureShell() {
  if (document.querySelector('#executive-management-dialog')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/executive_management_ui.css';
  document.head.appendChild(link);

  const button = document.createElement('button');
  button.id = 'executive-management-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Organization';
  button.title = 'Open canonical Executive management workspace';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'executive-management-dialog';
  dialog.className = 'exec-mgmt-dialog';
  dialog.innerHTML = `
    <div class="exec-mgmt-shell">
      <header class="exec-mgmt-header">
        <div>
          <h2>Executive / Organization workspace</h2>
          <p>Canonical roles, bounded consultations, authority decisions and resulting company objects.</p>
        </div>
        <button type="button" class="icon-button exec-mgmt-close" aria-label="Close">×</button>
      </header>
      <div class="exec-mgmt-toolbar">
        <button type="button" class="ghost-button exec-mgmt-refresh">Refresh</button>
        <button type="button" class="primary-button exec-mgmt-new">New consultation</button>
        <span class="exec-mgmt-status" aria-live="polite"></span>
      </div>
      <div class="exec-mgmt-layout">
        <aside class="exec-mgmt-sidebar">
          <section>
            <h3>Executive roles</h3>
            <div class="exec-mgmt-role-list"></div>
          </section>
          <section>
            <h3>Recent activations</h3>
            <div class="exec-mgmt-activation-list"></div>
          </section>
        </aside>
        <main class="exec-mgmt-detail">
          <div class="exec-mgmt-empty">Select a role or activation.</div>
        </main>
      </div>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    dialog.showModal();
    await refreshAll();
  });
  dialog.querySelector('.exec-mgmt-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.exec-mgmt-refresh').addEventListener('click', refreshAll);
  dialog.querySelector('.exec-mgmt-new').addEventListener('click', renderCreateForm);
}

function setStatus(message, isError = false) {
  const node = document.querySelector('.exec-mgmt-status');
  if (!node) return;
  node.textContent = message || '';
  node.classList.toggle('error', Boolean(isError));
}

async function refreshAll() {
  setStatus('Loading canonical Executive state…');
  try {
    const [roles, activations] = await Promise.all([
      request('/api/executive/roles'),
      request('/api/executive/activations?limit=100'),
    ]);
    state.roles = roles?.catalog?.roles || [];
    state.catalogDefinition = roles?.definition || null;
    state.activations = activations?.items || [];
    if (
      state.selectedActivationId
      && !state.activations.some((item) => item.id === state.selectedActivationId)
    ) {
      state.selectedActivationId = '';
    }
    if (
      state.selectedRoleId
      && !state.roles.some((item) => item.id === state.selectedRoleId)
    ) {
      state.selectedRoleId = '';
    }
    renderSidebar();
    if (state.mode === 'activation' && state.selectedActivationId) {
      await loadActivation(state.selectedActivationId);
    } else if (state.mode === 'role' && state.selectedRoleId) {
      renderRoleDetail(state.selectedRoleId);
    } else if (state.activations.length) {
      state.mode = 'activation';
      state.selectedActivationId = state.activations[0].id;
      await loadActivation(state.selectedActivationId);
    } else if (state.roles.length) {
      state.mode = 'role';
      state.selectedRoleId = state.roles[0].id;
      renderRoleDetail(state.selectedRoleId);
    } else {
      document.querySelector('.exec-mgmt-detail').innerHTML =
        '<div class="exec-mgmt-empty">No Executive role catalog is available.</div>';
    }
    setStatus('Up to date');
  } catch (error) {
    setStatus(error.message || 'Failed to load Executive management state', true);
  }
}

function renderSidebar() {
  const roleHost = document.querySelector('.exec-mgmt-role-list');
  const activationHost = document.querySelector('.exec-mgmt-activation-list');
  if (!roleHost || !activationHost) return;

  roleHost.innerHTML = state.roles.map((role) => {
    const selected = state.mode === 'role' && state.selectedRoleId === role.id ? 'selected' : '';
    return `
      <button type="button" class="exec-mgmt-row ${selected}" data-role-id="${esc(role.id)}">
        <strong>${esc(role.title)}</strong>
        <span>${esc(role.id)} · ${esc(role.lifecycle)}</span>
        <small>${esc(list(role.event_subscriptions).length)} subscriptions · ${esc(list(role.responsibilities).length)} responsibilities</small>
      </button>`;
  }).join('') || '<div class="exec-mgmt-empty">No roles.</div>';

  activationHost.innerHTML = state.activations.map((item) => {
    const selected = state.mode === 'activation' && state.selectedActivationId === item.id ? 'selected' : '';
    return `
      <button type="button" class="exec-mgmt-row ${selected}" data-activation-id="${esc(item.id)}">
        <strong>${esc(item.subject)}</strong>
        <span>${statusBadge(item.status)} · ${esc(item.trigger_kind)}</span>
        <small>${esc(list(item.selections).map((v) => v.role_id).join(', ') || 'no roles')} · ${esc(fmtTime(item.updated_at))}</small>
      </button>`;
  }).join('') || '<div class="exec-mgmt-empty">No activations.</div>';

  roleHost.querySelectorAll('[data-role-id]').forEach((button) => {
    button.addEventListener('click', () => {
      state.mode = 'role';
      state.selectedRoleId = button.dataset.roleId;
      state.selectedActivationId = '';
      renderSidebar();
      renderRoleDetail(state.selectedRoleId);
    });
  });
  activationHost.querySelectorAll('[data-activation-id]').forEach((button) => {
    button.addEventListener('click', async () => {
      state.mode = 'activation';
      state.selectedActivationId = button.dataset.activationId;
      state.selectedRoleId = '';
      renderSidebar();
      await loadActivation(state.selectedActivationId);
    });
  });
}

function roleActivations(roleId) {
  return state.activations.filter((item) =>
    list(item.selections).some((selection) => selection.role_id === roleId)
  );
}

function renderRoleDetail(roleId) {
  const host = document.querySelector('.exec-mgmt-detail');
  const role = state.roles.find((item) => item.id === roleId);
  if (!host || !role) return;

  const activations = roleActivations(roleId);
  const refs = {
    goals: new Set(),
    decisions: new Set(),
    work: new Set(),
    evidence: new Set(),
  };
  activations.forEach((item) => {
    list(item.goal_ids).forEach((id) => refs.goals.add(id));
    list(item.decision_ids).forEach((id) => refs.decisions.add(id));
    list(item.work_item_refs).forEach((id) => refs.work.add(id));
    list(item.evidence_ids).forEach((id) => refs.evidence.add(id));
  });

  host.innerHTML = `
    <section class="exec-mgmt-card exec-mgmt-summary">
      <div>
        <div class="exec-mgmt-title-line"><h3>${esc(role.title)}</h3>${statusBadge(role.lifecycle)}</div>
        <p>${esc(role.description)}</p>
      </div>
      <div class="exec-mgmt-definition">
        <span>Definition revision</span>
        <strong>${esc(state.catalogDefinition?.record_id || 'unknown')}</strong>
      </div>
    </section>

    <section class="exec-mgmt-card">
      <h4>Responsibilities</h4>
      <ul class="exec-mgmt-list">${list(role.responsibilities).map((v) => `<li>${esc(v)}</li>`).join('')}</ul>
    </section>

    <section class="exec-mgmt-card exec-mgmt-grid">
      <div><span>Observable canonical state</span><strong>${esc(list(role.observable_information).join(', ') || 'none')}</strong></div>
      <div><span>Event subscriptions</span><strong>${esc(list(role.event_subscriptions).join(', ') || 'none')}</strong></div>
      <div><span>Consultation roles</span><strong>${esc(list(role.consultation_roles).join(', ') || 'none')}</strong></div>
      <div><span>Allowed proposals</span><strong>${esc(list(role.authority?.allowed_proposal_kinds).join(', ') || 'none')}</strong></div>
      <div><span>May materialize</span><strong>${role.authority?.can_materialize ? 'yes, through canonical authority' : 'no'}</strong></div>
      <div><span>External side effects</span><strong>${role.authority?.external_side_effects ? 'allowed' : 'forbidden'}</strong></div>
    </section>

    <section class="exec-mgmt-card">
      <h4>Materialization authority capabilities</h4>
      <div class="exec-mgmt-subcards">
        ${Object.entries(role.authority?.required_materialization_capabilities || {}).map(([kind, capability]) => `
          <article><strong>${esc(kind)}</strong><small>${esc(capability)}</small></article>
        `).join('') || '<p class="exec-mgmt-muted">No materialization capabilities.</p>'}
      </div>
    </section>

    <section class="exec-mgmt-card">
      <h4>Recent canonical context for this role</h4>
      <div class="exec-mgmt-ref-groups">
        ${renderRefGroup('Goals', [...refs.goals], 'goal')}
        ${renderRefGroup('Decisions', [...refs.decisions], 'decision')}
        ${renderRefGroup('Work', [...refs.work], 'work')}
        ${renderRefGroup('Evidence', [...refs.evidence], 'evidence')}
      </div>
    </section>

    <section class="exec-mgmt-card">
      <h4>Recent activations</h4>
      <div class="exec-mgmt-subcards">
        ${activations.slice(0, 20).map((item) => `
          <button type="button" class="exec-mgmt-subcard-button" data-role-activation-id="${esc(item.id)}">
            <strong>${esc(item.subject)}</strong>
            <small>${statusBadge(item.status)} · ${esc(item.trigger_kind)} · ${esc(fmtTime(item.updated_at))}</small>
          </button>
        `).join('') || '<p class="exec-mgmt-muted">No activations have selected this role.</p>'}
      </div>
    </section>`;

  host.querySelectorAll('[data-role-activation-id]').forEach((button) => {
    button.addEventListener('click', async () => {
      state.mode = 'activation';
      state.selectedActivationId = button.dataset.roleActivationId;
      state.selectedRoleId = '';
      renderSidebar();
      await loadActivation(state.selectedActivationId);
    });
  });
  bindCanonicalLinks(host);
}

function renderRefGroup(label, values, kind) {
  if (!values.length) {
    return `<div><span>${esc(label)}</span><small>none referenced</small></div>`;
  }
  return `
    <div>
      <span>${esc(label)}</span>
      <div class="exec-mgmt-link-row">
        ${values.map((id) => canonicalButton(kind, id)).join('')}
      </div>
    </div>`;
}

function canonicalButton(kind, id, label = null) {
  return `<button type="button" class="exec-mgmt-link" data-canonical-kind="${esc(kind)}" data-canonical-id="${esc(id)}">${esc(label || id)}</button>`;
}

function openDeveloperSearch(inputId, value) {
  const panel = document.querySelector('#developer-panel');
  if (panel) panel.open = true;
  const input = document.querySelector(inputId);
  if (input) {
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
}

function openCanonical(kind, id) {
  if (kind === 'goal') {
    window.dispatchEvent(new CustomEvent('codex:open-goal', { detail: { goalId: id } }));
    return;
  }
  if (kind === 'decision') {
    window.dispatchEvent(new CustomEvent('codex:open-decision', { detail: { decisionId: id } }));
    return;
  }
  if (kind === 'evidence') {
    openDeveloperSearch('#artifact-evidence-search', id);
    return;
  }
  if (kind === 'action-intent' || kind === 'work') {
    openDeveloperSearch('#action-intent-search', id);
  }
}

function bindCanonicalLinks(host) {
  host.querySelectorAll('[data-canonical-kind]').forEach((button) => {
    button.addEventListener('click', () => {
      openCanonical(button.dataset.canonicalKind, button.dataset.canonicalId);
    });
  });
}

async function loadActivation(activationId) {
  const host = document.querySelector('.exec-mgmt-detail');
  if (!host) return;
  host.innerHTML = '<div class="exec-mgmt-empty">Loading canonical activation…</div>';
  try {
    const encoded = encodeURIComponent(activationId);
    const [detail, revisions] = await Promise.all([
      request(`/api/executive/activations/${encoded}`),
      request(`/api/executive/activations/${encoded}/revisions`),
    ]);
    state.activation = detail?.item || null;
    state.revisions = revisions?.items || [];
    renderActivation();
  } catch (error) {
    host.innerHTML = `<div class="exec-mgmt-error">${esc(error.message || 'Failed to load activation')}</div>`;
  }
}

function selectionRole(selection) {
  return state.roles.find((role) => role.id === selection.role_id) || null;
}

function renderActivation() {
  const host = document.querySelector('.exec-mgmt-detail');
  const item = state.activation;
  if (!host || !item) return;

  host.innerHTML = `
    <section class="exec-mgmt-card exec-mgmt-summary">
      <div>
        <div class="exec-mgmt-title-line"><h3>${esc(item.subject)}</h3>${statusBadge(item.status)}</div>
        <p>${esc(item.request)}</p>
      </div>
      <div class="exec-mgmt-definition">
        <span>Activation revision</span>
        <strong>r${esc(item.revision)}</strong>
      </div>
    </section>

    ${renderActivationActions(item)}

    <section class="exec-mgmt-card">
      <h4>Why was this Executive activated?</h4>
      <div class="exec-mgmt-grid">
        <div><span>Trigger</span><strong>${esc(item.trigger_kind)}</strong></div>
        <div><span>Trigger ref</span><strong>${esc(item.trigger_ref || 'manual request')}</strong></div>
        <div><span>Event type</span><strong>${esc(item.event_type || 'none')}</strong></div>
        <div><span>Initiated by</span><strong>${esc(item.initiated_by)}</strong></div>
        <div><span>Project</span><strong>${esc(item.project_id || 'workspace')}</strong></div>
        <div><span>Role catalog revision</span><strong class="exec-mgmt-break">${esc(item.role_catalog?.record_id || 'unknown')}</strong></div>
      </div>
      <div class="exec-mgmt-subcards">
        ${list(item.selections).map((selection) => {
          const role = selectionRole(selection);
          return `
            <article>
              <strong>${esc(role?.title || selection.role_id)} · score ${esc(selection.score)}</strong>
              <small>${selection.explicit ? 'explicit selection' : 'deterministic selection'} · ${esc(list(selection.reasons).join('; ') || 'no reason')}</small>
            </article>`;
        }).join('')}
      </div>
    </section>

    <section class="exec-mgmt-card">
      <h4>Canonical context</h4>
      <p class="exec-mgmt-muted">This workspace displays references already captured on the activation; it does not maintain a separate Executive task list.</p>
      <div class="exec-mgmt-ref-groups">
        ${renderContextGroup('Goals', item.context?.goals, 'goal', 'goal')}
        ${renderContextGroup('Decisions', item.context?.decisions, 'decision', 'id')}
        ${renderContextGroup('Work', item.context?.work_items, 'work', 'ref')}
        ${renderContextGroup('Evidence', item.context?.evidence, 'evidence', 'id')}
      </div>
    </section>

    <section class="exec-mgmt-card">
      <h4>Bounded consultation</h4>
      <div class="exec-mgmt-budget">
        <span>Calls ≤ ${esc(item.budget?.max_model_calls)}</span>
        <span>Input ≤ ${esc(item.budget?.max_input_tokens)}</span>
        <span>Output ≤ ${esc(item.budget?.max_output_tokens)}</span>
        <span>Cost ≤ $${esc(item.budget?.max_cost_usd)}</span>
      </div>
      <div class="exec-mgmt-consultations">
        ${list(item.consultations).map(renderConsultation).join('') || '<p class="exec-mgmt-muted">No consultation has run yet.</p>'}
      </div>
    </section>

    <section class="exec-mgmt-card">
      <h4>Synthesis, disagreement and escalation</h4>
      ${renderSynthesis(item)}
    </section>

    <section class="exec-mgmt-card">
      <h4>Advisory proposals → authorized company state</h4>
      <p class="exec-mgmt-muted">Proposals are advisory until canonical authority allows materialization. External consequences still flow through approved Decision → ActionIntent → ActionProvider.</p>
      <div class="exec-mgmt-proposals">
        ${list(item.proposals).map(renderProposal).join('') || '<p class="exec-mgmt-muted">No proposals.</p>'}
      </div>
    </section>

    <section class="exec-mgmt-card">
      <h4>Revision history</h4>
      <div class="exec-mgmt-history">
        ${state.revisions.map((revision) => `
          <div><strong>r${esc(revision.revision)}</strong><span>${esc(revision.reason)} · ${esc(revision.revised_by)} · ${esc(fmtTime(revision.revised_at))}</span></div>
        `).join('') || '<p class="exec-mgmt-muted">No revisions.</p>'}
      </div>
    </section>`;

  bindCanonicalLinks(host);
  host.querySelector('.exec-mgmt-consult')?.addEventListener('click', () => consultActivation(item.id));
  host.querySelectorAll('[data-materialize-proposal]').forEach((button) => {
    button.addEventListener('click', () => materializeProposal(item.id, button.dataset.materializeProposal));
  });
}

function renderActivationActions(item) {
  if (item.status === 'planned') {
    return `<div class="exec-mgmt-actions"><button type="button" class="primary-button exec-mgmt-consult">Run bounded consultation</button><span class="exec-mgmt-muted">No company state changes are authorized by this action.</span></div>`;
  }
  if (item.status === 'consulting') {
    return '<div class="exec-mgmt-actions"><span class="exec-mgmt-muted">Consultation is in progress.</span></div>';
  }
  return '';
}

function renderContextGroup(label, rows, kind, idKey) {
  const values = list(rows);
  if (!values.length) {
    return `<div><span>${esc(label)}</span><small>none captured</small></div>`;
  }
  return `
    <div>
      <span>${esc(label)}</span>
      <div class="exec-mgmt-link-row">
        ${values.map((row) => {
          const id = kind === 'goal' ? row?.goal?.id : row?.[idKey];
          const labelText = kind === 'goal' ? (row?.goal?.title || id) : (row?.title || row?.name || id);
          return id ? canonicalButton(kind, id, labelText) : '';
        }).join('')}
      </div>
    </div>`;
}

function renderConsultation(item) {
  const role = state.roles.find((row) => row.id === item.role_id);
  const output = item.output || {};
  return `
    <article class="exec-mgmt-consultation">
      <div class="exec-mgmt-title-line"><strong>${esc(role?.title || item.role_id)}</strong><span class="exec-mgmt-muted">${esc(item.model_invocation_id)}</span></div>
      <p>${esc(output.summary || '')}</p>
      <div class="exec-mgmt-callout"><span>Recommendation</span><strong>${esc(output.recommendation || 'none')}</strong></div>
      ${renderSmallList('Risks', output.risks)}
      ${renderSmallList('Assumptions', output.assumptions)}
      ${renderSmallList('Disagreement', output.disagreement)}
    </article>`;
}

function renderSmallList(label, values) {
  const rows = list(values);
  if (!rows.length) return '';
  return `<div class="exec-mgmt-small-list"><span>${esc(label)}</span><ul>${rows.map((value) => `<li>${esc(value)}</li>`).join('')}</ul></div>`;
}

function renderSynthesis(item) {
  const synthesis = item.synthesis;
  if (!synthesis) {
    return '<p class="exec-mgmt-muted">No synthesis recorded.</p>';
  }
  return `
    <div class="exec-mgmt-synthesis ${synthesis.escalation_required ? 'escalated' : ''}">
      <div class="exec-mgmt-title-line">
        <strong>${synthesis.escalation_required ? 'Escalation required' : 'Synthesis complete'}</strong>
        ${synthesis.escalation_required ? statusBadge('escalated') : statusBadge('completed')}
      </div>
      <p>${esc(synthesis.recommendation)}</p>
      <small>${esc(synthesis.rationale)}</small>
      ${renderSmallList('Material disagreement', synthesis.disagreement)}
      ${synthesis.escalation_reason ? `<div class="exec-mgmt-callout"><span>Escalation reason</span><strong>${esc(synthesis.escalation_reason)}</strong></div>` : ''}
    </div>`;
}

function renderProposal(item) {
  const resulting = renderResultingRef(item.resulting_ref);
  const mayMaterialize = item.status === 'proposed' && item.kind !== 'escalation';
  return `
    <article class="exec-mgmt-proposal">
      <div class="exec-mgmt-title-line"><strong>${esc(item.title)}</strong>${statusBadge(item.status, 'proposal')}</div>
      <small>${esc(item.role_id)} · ${esc(item.kind)}</small>
      <p>${esc(item.rationale)}</p>
      <div class="exec-mgmt-grid compact">
        <div><span>Authority capability</span><strong>${esc(item.authority_capability || 'not evaluated')}</strong></div>
        <div><span>Authority reasons</span><strong>${esc(list(item.authority_reasons).join('; ') || 'not evaluated')}</strong></div>
        <div><span>Materialized by</span><strong>${esc(item.materialized_by || 'none')}</strong></div>
      </div>
      ${resulting}
      ${mayMaterialize ? `<button type="button" class="primary-button" data-materialize-proposal="${esc(item.id)}">Evaluate authority & materialize</button>` : ''}
      ${item.kind === 'escalation' && item.status === 'proposed' ? '<div class="exec-mgmt-callout"><span>Advisory only</span><strong>Escalation requires canonical human/action routing and cannot be directly materialized here.</strong></div>' : ''}
    </article>`;
}

function renderResultingRef(value) {
  if (!value) return '';
  if (value.startsWith('goal:')) {
    const id = value.slice(5);
    return `<div class="exec-mgmt-result"><span>Canonical result</span>${canonicalButton('goal', id, value)}</div>`;
  }
  const workMarker = ':work:';
  if (value.startsWith('decision:') && value.includes(workMarker)) {
    const [decisionPrefix, ids] = value.split(workMarker);
    const decisionId = decisionPrefix.slice('decision:'.length);
    const intents = ids.split(',').map((v) => v.trim()).filter(Boolean);
    return `
      <div class="exec-mgmt-result">
        <span>Canonical execution path</span>
        <div class="exec-mgmt-link-row">
          ${canonicalButton('decision', decisionId, `Decision ${decisionId}`)}
          ${intents.map((id) => canonicalButton('action-intent', id, `ActionIntent ${id}`)).join('')}
        </div>
      </div>`;
  }
  if (value.startsWith('decision:')) {
    const id = value.slice('decision:'.length);
    return `<div class="exec-mgmt-result"><span>Canonical result</span>${canonicalButton('decision', id, value)}</div>`;
  }
  return `<div class="exec-mgmt-result"><span>Canonical result</span><code>${esc(value)}</code></div>`;
}

async function consultActivation(id) {
  setStatus('Running bounded Executive consultation…');
  try {
    await request(`/api/executive/activations/${encodeURIComponent(id)}/consult`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    await refreshAll();
  } catch (error) {
    setStatus(error.message || 'Executive consultation failed', true);
  }
}

async function materializeProposal(activationId, proposalId) {
  setStatus('Evaluating canonical authority…');
  try {
    await request(
      `/api/executive/activations/${encodeURIComponent(activationId)}/proposals/${encodeURIComponent(proposalId)}/materialize`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      },
    );
    await refreshAll();
  } catch (error) {
    setStatus(error.message || 'Proposal materialization was denied or failed', true);
    await loadActivation(activationId);
  }
}

function renderCreateForm() {
  state.mode = 'create';
  state.selectedActivationId = '';
  state.selectedRoleId = '';
  renderSidebar();
  const host = document.querySelector('.exec-mgmt-detail');
  if (!host) return;
  host.innerHTML = `
    <section class="exec-mgmt-card">
      <h3>New bounded Executive consultation</h3>
      <p class="exec-mgmt-muted">Creating an activation records a request and deterministic routing only. Model consultation is a separate action.</p>
      <form class="exec-mgmt-create-form">
        <label>Subject<input name="subject" required maxlength="500"></label>
        <label class="wide">Request<textarea name="request" required rows="7" maxlength="50000"></textarea></label>
        <label>Project ID<input name="project_id" placeholder="optional"></label>
        <label>Requested roles<input name="requested_role_ids" placeholder="optional comma-separated role IDs"></label>
        <label>Goal IDs<input name="goal_ids" placeholder="optional comma-separated"></label>
        <label>Decision IDs<input name="decision_ids" placeholder="optional comma-separated"></label>
        <label>Work refs<input name="work_item_refs" placeholder="optional comma-separated"></label>
        <label>Evidence IDs<input name="evidence_ids" placeholder="optional comma-separated"></label>
        <div class="wide exec-mgmt-actions">
          <button type="submit" class="primary-button">Create activation</button>
          <button type="button" class="ghost-button exec-mgmt-cancel-create">Cancel</button>
        </div>
      </form>
    </section>`;
  host.querySelector('.exec-mgmt-cancel-create').addEventListener('click', refreshAll);
  host.querySelector('.exec-mgmt-create-form').addEventListener('submit', createActivation);
}

function csv(form, name) {
  return String(new FormData(form).get(name) || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

async function createActivation(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const body = {
    subject: String(data.get('subject') || '').trim(),
    request: String(data.get('request') || '').trim(),
    project_id: String(data.get('project_id') || '').trim() || null,
    requested_role_ids: csv(form, 'requested_role_ids'),
    goal_ids: csv(form, 'goal_ids'),
    decision_ids: csv(form, 'decision_ids'),
    work_item_refs: csv(form, 'work_item_refs'),
    evidence_ids: csv(form, 'evidence_ids'),
  };
  setStatus('Creating canonical Executive activation…');
  try {
    const result = await request('/api/executive/activations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.mode = 'activation';
    state.selectedActivationId = result.item.id;
    await refreshAll();
  } catch (error) {
    setStatus(error.message || 'Failed to create Executive activation', true);
  }
}

ensureShell();
