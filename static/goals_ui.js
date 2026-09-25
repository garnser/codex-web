import { request } from './api_client.js';

const state = {
  goals: [],
  selectedGoalId: '',
  detail: null,
  revisions: [],
  events: [],
  proposals: [],
  decompositionEvents: [],
  completion: null,
  executionBindings: [],
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

function fmtValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function ensureShell() {
  if (document.querySelector('#goals-dialog')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = 'static/goals_ui.css';
  document.head.appendChild(link);

  const button = document.createElement('button');
  button.id = 'goals-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Goals';
  button.title = 'Open Goal workspace';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'goals-dialog';
  dialog.className = 'goals-dialog';
  dialog.innerHTML = `
    <div class="goals-shell">
      <header class="goals-header">
        <div>
          <h2>Goal workspace</h2>
          <p>Outcome, decomposition, authoritative work, completion evidence and provenance.</p>
        </div>
        <button type="button" class="icon-button goals-close" aria-label="Close">×</button>
      </header>
      <div class="goals-toolbar">
        <button type="button" class="ghost-button goals-refresh">Refresh</button>
        <span class="goals-status" aria-live="polite"></span>
      </div>
      <div class="goals-layout">
        <aside class="goals-list" aria-label="Goals"></aside>
        <main class="goal-detail">
          <div class="goal-empty">Select a Goal to inspect canonical outcome state.</div>
        </main>
      </div>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    dialog.showModal();
    await refreshAll();
  });
  dialog.querySelector('.goals-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.goals-refresh').addEventListener('click', refreshAll);
}

function setStatus(message, isError = false) {
  const target = document.querySelector('.goals-status');
  if (!target) return;
  target.textContent = message || '';
  target.classList.toggle('error', Boolean(isError));
}

async function refreshAll() {
  setStatus('Loading…');
  try {
    const payload = await request('/api/goals');
    state.goals = Array.isArray(payload?.items) ? payload.items : [];
    if (!state.selectedGoalId || !state.goals.some((row) => row.goal?.id === state.selectedGoalId)) {
      state.selectedGoalId = state.goals[0]?.goal?.id || '';
    }
    renderGoalList();
    if (state.selectedGoalId) {
      await loadGoal(state.selectedGoalId);
    } else {
      document.querySelector('.goal-detail').innerHTML = '<div class="goal-empty">No Goals exist in this workspace.</div>';
    }
    setStatus('Up to date');
  } catch (error) {
    setStatus(error.message || 'Failed to load Goals', true);
  }
}

function renderGoalList() {
  const list = document.querySelector('.goals-list');
  if (!list) return;
  if (!state.goals.length) {
    list.innerHTML = '<div class="goal-empty">No Goals.</div>';
    return;
  }
  list.innerHTML = state.goals.map((snapshot) => {
    const goal = snapshot.goal || {};
    const health = snapshot.health || {};
    const progress = snapshot.progress || {};
    const selected = goal.id === state.selectedGoalId ? 'selected' : '';
    return `
      <button type="button" class="goal-row ${selected}" data-goal-id="${esc(goal.id)}">
        <strong>${esc(goal.title || goal.id)}</strong>
        <span>${esc(goal.status)} · ${esc(health.health || 'unknown')}</span>
        <small>${esc(progress.completed ?? 0)}/${esc(progress.work_item_count ?? 0)} work items completed</small>
      </button>`;
  }).join('');
  list.querySelectorAll('.goal-row').forEach((row) => row.addEventListener('click', async () => {
    state.selectedGoalId = row.dataset.goalId;
    renderGoalList();
    await loadGoal(state.selectedGoalId);
  }));
}

async function loadGoal(goalId) {
  const detail = document.querySelector('.goal-detail');
  if (!detail) return;
  detail.innerHTML = '<div class="goal-empty">Loading Goal detail…</div>';
  try {
    const encoded = encodeURIComponent(goalId);
    const [
      detailPayload,
      revisions,
      events,
      proposals,
      decompositionEvents,
      completion,
      executionBindings,
    ] = await Promise.all([
      request(`/api/goals/${encoded}`),
      request(`/api/goals/${encoded}/revisions`),
      request(`/api/goals/events?goal_id=${encoded}`),
      request(`/api/goals/${encoded}/decompositions`),
      request(`/api/goals/${encoded}/decompositions/events`),
      request(`/api/goals/${encoded}/completion-evaluation`),
      request(`/api/goals/${encoded}/execution-bindings`),
    ]);
    state.detail = detailPayload?.snapshot || null;
    state.revisions = revisions?.items || [];
    state.events = events?.items || [];
    state.proposals = proposals?.items || [];
    state.decompositionEvents = decompositionEvents?.items || [];
    state.completion = completion?.item || null;
    state.executionBindings = executionBindings?.items || [];
    renderGoalDetail();
  } catch (error) {
    detail.innerHTML = `<div class="goal-error">${esc(error.message || 'Failed to load Goal')}</div>`;
  }
}

function keyValues(values) {
  return Object.entries(values).map(([key, value]) => `
    <div><span>${esc(key.replaceAll('_', ' '))}</span><strong>${esc(fmtValue(value))}</strong></div>
  `).join('');
}

function renderCriteria(goal) {
  const rows = goal.success_criteria || [];
  if (!rows.length) return '<p class="goal-muted">No success criteria recorded.</p>';
  return rows.map((item) => {
    const metricRef = item.metric_id || item.metric_key;
    const metricDetail = metricRef
      ? ` · ${esc(metricRef)} ${esc(item.operator)} ${esc(item.target_value)} ${esc(item.unit || '')}`
      : '';
    const snapshot = item.metric_snapshot_id
      ? ` · snapshot ${esc(item.metric_snapshot_id)}`
      : '';
    return `
      <div class="goal-subcard">
        <strong>${esc(item.description)}</strong>
        <small>${esc(item.kind)}${metricDetail}${snapshot} · ${item.required ? 'required' : 'optional'}</small>
      </div>
    `;
  }).join('');
}

function renderMetadataList(rows, emptyText, formatter) {
  if (!rows?.length) return `<p class="goal-muted">${esc(emptyText)}</p>`;
  return rows.map(formatter).join('');
}

function renderBindings(goal) {
  return renderMetadataList(
    goal.work_graph_bindings,
    'No Work Graph bindings yet.',
    (binding) => `
      <div class="goal-subcard">
        <strong>Project ${esc(binding.project_id)}</strong>
        <small>Roots: ${esc((binding.root_work_item_refs || []).join(', ') || 'entire project graph')}</small>
      </div>`,
  );
}

function renderProposal(proposal) {
  const items = proposal.items || [];
  const commits = proposal.commit_items || [];
  const status = proposal.status || 'unknown';
  const actions = [];
  if (status === 'proposed') {
    actions.push('<button type="button" class="ghost-button goal-proposal-accept">Accept</button>');
    actions.push('<button type="button" class="ghost-button goal-proposal-reject">Reject</button>');
    actions.push('<button type="button" class="ghost-button goal-proposal-revise">Revise</button>');
  } else if (status === 'rejected') {
    actions.push('<button type="button" class="ghost-button goal-proposal-revise">Revise</button>');
  } else if (status === 'accepted') {
    actions.push('<button type="button" class="primary-button goal-proposal-commit">Commit accepted work</button>');
  } else if (status === 'committing') {
    actions.push('<button type="button" class="ghost-button goal-proposal-reconcile">Reconcile commit</button>');
  }

  return `
    <article class="goal-proposal" data-proposal-id="${esc(proposal.id)}">
      <div class="goal-proposal-heading">
        <div>
          <strong>${esc(proposal.id)}</strong>
          <small>status ${esc(status)} · revision ${esc(proposal.revision)} · Goal r${esc(proposal.goal_revision)}</small>
          <small>model invocation: ${esc(proposal.model_invocation_id || 'deterministic/manual proposal')}</small>
        </div>
        <div class="goal-action-row">${actions.join('')}</div>
      </div>
      ${proposal.commit_error ? `<div class="goal-error">${esc(proposal.commit_error)}</div>` : ''}
      <div class="goal-proposal-items">
        ${items.map((item) => `
          <div class="goal-subcard">
            <strong>${esc(item.title)}</strong>
            <small>${esc(item.project_id)} · id ${esc(item.id)}</small>
            <p>${esc(item.description)}</p>
            <small>Parent: ${esc(item.parent_item_id || '—')} · Blocked by: ${esc((item.blocked_by_item_ids || []).join(', ') || '—')}</small>
            <small>Expected: ${esc(item.expected_result || '—')}</small>
          </div>
        `).join('')}
      </div>
      ${commits.length ? `
        <div class="goal-commit-grid">
          ${commits.map((item) => `
            <div class="goal-commit-row">
              <strong>${esc(item.proposal_item_id)}</strong>
              <span>${esc(item.state)}</span>
              <small>ActionIntent: ${esc(item.intent_id || '—')}</small>
              <small>Work Item: ${esc(item.work_item_ref || '—')}</small>
              ${item.last_error ? `<small class="goal-danger">${esc(item.last_error)}</small>` : ''}
            </div>
          `).join('')}
        </div>
      ` : ''}
    </article>`;
}

function renderCompletion(goal) {
  const evaluation = state.completion;
  const criteria = goal.success_criteria || [];
  const evaluationCriteria = Object.fromEntries((evaluation?.criteria || []).map((item) => [item.criterion_id, item]));
  const observationInputs = criteria.map((criterion) => {
    const current = evaluationCriteria[criterion.id] || {};
    const control = criterion.kind === 'manual'
      ? `<label class="goal-check"><input type="checkbox" class="goal-observation-verified" ${current.passed ? 'checked' : ''}/> Verified</label>`
      : `<label>Observed value <input class="goal-observation-value" value="${esc(current.observed_value ?? '')}" /></label>`;
    return `
      <div class="goal-observation" data-criterion-id="${esc(criterion.id)}" data-kind="${esc(criterion.kind)}">
        <strong>${esc(criterion.description)}</strong>
        <small>Target: ${esc(criterion.metric_key ? `${criterion.metric_key} ${criterion.operator} ${criterion.target_value}` : 'manual verification')}</small>
        ${control}
        <label>Source <input class="goal-observation-source" value="${esc(current.source || 'operator:goal-workspace')}" /></label>
        <label>Reference <input class="goal-observation-reference" value="${esc(current.reference || '')}" /></label>
        ${current.findings?.length ? `<small class="goal-danger">${esc(current.findings.join(' · '))}</small>` : ''}
      </div>`;
  }).join('');

  const blockers = evaluation?.blockers || [];
  const workRows = evaluation?.work_items || [];

  return `
    <section class="goal-card">
      <div class="goal-section-heading">
        <div><h3>Completion verification</h3><small>Deterministic criterion and bound-work evaluation.</small></div>
        <div class="goal-action-row">
          <button type="button" class="ghost-button goal-evaluate-completion">Evaluate</button>
          <button type="button" class="primary-button goal-complete" ${evaluation?.eligible && goal.status === 'active' ? '' : 'disabled'}>Complete Goal</button>
        </div>
      </div>
      ${evaluation ? `
        <div class="goal-evaluation-summary ${evaluation.eligible ? 'pass' : 'blocked'}">
          <strong>${evaluation.eligible ? 'Eligible for completion' : 'Completion blocked'}</strong>
          <small>Evaluation ${esc(evaluation.id)} · Goal r${esc(evaluation.goal_revision)} · ${esc(fmtTime(evaluation.evaluated_at))}</small>
          <small>By ${esc(evaluation.evaluated_by)} · ${esc(evaluation.reason)}</small>
        </div>
      ` : '<p class="goal-muted">No current-revision completion evaluation.</p>'}
      ${blockers.length ? `<div class="goal-blockers">${blockers.map((item) => `<div>${esc(item)}</div>`).join('')}</div>` : ''}
      <div class="goal-observation-grid">${observationInputs || '<p class="goal-muted">No criteria observations required.</p>'}</div>
      ${workRows.length ? `
        <details>
          <summary>Bound work completion checks</summary>
          <div class="goal-commit-grid">
            ${workRows.map((item) => `
              <div class="goal-commit-row">
                <strong>${esc(item.work_item_ref)}</strong>
                <span>${item.passed ? 'passed' : 'blocked'}</span>
                <small>${esc(item.project_id)} · outcome ${esc(item.terminal_outcome || 'active')}</small>
                ${item.findings?.length ? `<small class="goal-danger">${esc(item.findings.join(' · '))}</small>` : ''}
              </div>
            `).join('')}
          </div>
        </details>
      ` : ''}
    </section>`;
}

function renderTimeline() {
  const rows = [
    ...state.events.map((event) => ({
      at: event.occurred_at,
      type: event.event_type,
      actor: event.actor_id,
      reason: event.reason,
      source: 'goal',
    })),
    ...state.decompositionEvents.map((event) => ({
      at: event.occurred_at,
      type: event.event_type,
      actor: event.actor_id,
      reason: event.reason,
      source: 'decomposition',
    })),
  ].sort((a, b) => Number(b.at || 0) - Number(a.at || 0));
  if (!rows.length) return '<p class="goal-muted">No Goal/decomposition events.</p>';
  return rows.map((event) => `
    <div class="goal-timeline-row">
      <strong>${esc(event.type)}</strong>
      <span>${esc(event.actor || 'system')} · ${esc(event.source)}</span>
      <small>${esc(event.reason || '')} · ${esc(fmtTime(event.at))}</small>
    </div>
  `).join('');
}

function renderExecutionBindings() {
  if (!state.executionBindings.length) {
    return '<p class="goal-muted">No runtime execution is bound to this canonical Goal.</p>';
  }
  return state.executionBindings.map((item) => `
    <div class="goal-subcard goal-execution-binding" data-binding-id="${esc(item.id)}">
      <strong>Runtime: ${esc(item.status || 'unknown')}</strong>
      <span>Canonical Goal remains authoritative · revision ${esc(item.goal_revision)}</span>
      <small>${esc(item.provider_id)}/${esc(item.runtime_id)} · session ${esc(item.agent_session_id)} · owner ${esc(item.execution_owner_id)}</small>
      <small>native objective: ${esc(item.provider_native_objective_id || (item.native_objective_supported ? 'not assigned' : 'unsupported'))}</small>
      <small>cursor: ${esc(item.cursor_ref || '—')} · checkpoint: ${esc(item.checkpoint_ref || '—')}</small>
      <small>last turn: ${esc(item.last_turn_id || '—')} · last execution: ${esc(item.last_execution_id || '—')} · activity: ${esc(fmtTime(item.heartbeat_at || item.updated_at))}</small>
      <small>stop/block reason: ${esc(item.stop_reason || '—')}</small>
    </div>`
  ).join('');
}

function renderGoalDetail() {
  const target = document.querySelector('.goal-detail');
  const snapshot = state.detail || {};
  const goal = snapshot.goal || {};
  const progress = snapshot.progress || {};
  const health = snapshot.health || {};
  if (!target || !goal.id) return;

  const constraints = renderMetadataList(
    goal.constraints,
    'No constraints recorded.',
    (item) => `<div class="goal-subcard"><strong>${esc(item.kind)}</strong><span>${esc(item.description)}</span><small>${esc(item.reference || '')}</small></div>`,
  );
  const risks = renderMetadataList(
    goal.risks,
    'No risks recorded.',
    (item) => `<div class="goal-subcard"><strong>${esc(item.level)} · ${esc(item.category)}</strong><span>${esc(item.description)}</span><small>${esc(item.mitigation || '')}</small></div>`,
  );
  const approvals = renderMetadataList(
    goal.approval_requirements,
    'No approval requirements recorded.',
    (item) => `<div class="goal-subcard"><strong>${esc(item.trigger)}</strong><span>${esc(item.description)}</span><small>role: ${esc(item.required_role || 'any authorized approver')}</small></div>`,
  );

  target.innerHTML = `
    <section class="goal-title">
      <div>
        <small>${esc(goal.id)} · revision ${esc(goal.revision)}</small>
        <h2>${esc(goal.title)}</h2>
        <p>${esc(goal.description)}</p>
      </div>
      <div class="goal-action-row">
        ${goal.status === 'draft' ? '<button type="button" class="ghost-button goal-activate">Activate</button>' : ''}
        ${goal.status === 'active' ? '<button type="button" class="ghost-button goal-pause">Pause</button>' : ''}
        ${goal.status === 'paused' ? '<button type="button" class="ghost-button goal-activate">Resume</button>' : ''}
      </div>
    </section>

    <div class="goal-summary-grid">
      <section class="goal-card">
        <h3>Canonical state</h3>
        <div class="goal-kv">${keyValues({
          status: goal.status,
          owner: goal.owner_identity_id,
          priority: goal.priority,
          target_date: fmtTime(goal.target_date),
          health: health.health,
          completion: `${progress.completed ?? 0}/${progress.work_item_count ?? 0}`,
          completion_fraction: progress.completion_fraction,
          blocked: progress.blocked,
          runnable: progress.runnable,
        })}</div>
        <div class="goal-reasons">${(health.reasons || []).map((item) => `<span>${esc(item)}</span>`).join('')}</div>
      </section>
      <section class="goal-card">
        <h3>Reasoning budget</h3>
        <div class="goal-kv">${keyValues(goal.budget || {})}</div>
      </section>
    </div>

    <div class="goal-summary-grid">
      <section class="goal-card"><h3>Success criteria</h3>${renderCriteria(goal)}</section>
      <section class="goal-card"><h3>Work Graph traceability</h3>${renderBindings(goal)}</section>
    </div>

    <section class="goal-card">
      <div class="goal-section-heading">
        <div><h3>Runtime execution</h3><small>Provider-neutral execution projection; canonical Goal lifecycle and completion remain authoritative.</small></div>
      </div>
      ${renderExecutionBindings()}
    </section>

    <div class="goal-summary-grid">
      <section class="goal-card"><h3>Constraints</h3>${constraints}</section>
      <section class="goal-card"><h3>Risks</h3>${risks}</section>
      <section class="goal-card"><h3>Approvals</h3>${approvals}</section>
    </div>

    <section class="goal-card">
      <div class="goal-section-heading">
        <div><h3>Decomposition</h3><small>Reviewable bounded planning; generation runs only on explicit action.</small></div>
        <div class="goal-generation-controls">
          <input class="goal-generate-projects" placeholder="Project IDs, comma separated" value="${esc((goal.work_graph_bindings || []).map((item) => item.project_id).join(', '))}" />
          <input class="goal-generate-reason" placeholder="Generation reason" value="operator requested bounded decomposition" />
          <button type="button" class="primary-button goal-generate">Generate proposal</button>
        </div>
      </div>
      <div class="goal-proposal-list">
        ${state.proposals.length ? state.proposals.map(renderProposal).join('') : '<p class="goal-muted">No decomposition proposals.</p>'}
      </div>
    </section>

    ${renderCompletion(goal)}

    <div class="goal-summary-grid">
      <section class="goal-card">
        <h3>Revision history</h3>
        <div class="goal-timeline">
          ${state.revisions.length ? state.revisions.slice().reverse().map((item) => `
            <div class="goal-timeline-row">
              <strong>Revision ${esc(item.revision)}</strong>
              <span>${esc(item.revised_by)}</span>
              <small>${esc(item.reason)} · ${esc(fmtTime(item.revised_at))}</small>
            </div>
          `).join('') : '<p class="goal-muted">No revisions.</p>'}
        </div>
      </section>
      <section class="goal-card">
        <h3>Goal & decomposition timeline</h3>
        <div class="goal-timeline">${renderTimeline()}</div>
      </section>
    </div>`;

  wireGoalActions();
}

function currentGoal() {
  return state.detail?.goal || null;
}

async function mutate(path, body, statusText) {
  setStatus(statusText || 'Applying…');
  try {
    await request(path, {
      method: 'POST',
      body: JSON.stringify(body),
    });
    await refreshAll();
  } catch (error) {
    setStatus(error.message || 'Operation failed', true);
    throw error;
  }
}

function proposalById(id) {
  return state.proposals.find((item) => item.id === id);
}

function wireGoalActions() {
  const goal = currentGoal();
  if (!goal) return;
  const encodedGoal = encodeURIComponent(goal.id);
  const detail = document.querySelector('.goal-detail');

  detail.querySelector('.goal-generate')?.addEventListener('click', async () => {
    const projectIds = detail.querySelector('.goal-generate-projects').value
      .split(',').map((item) => item.trim()).filter(Boolean);
    const reason = detail.querySelector('.goal-generate-reason').value.trim();
    if (!reason) {
      setStatus('Generation reason is required', true);
      return;
    }
    await mutate(
      `/api/goals/${encodedGoal}/decompositions/generate`,
      {
        project_ids: projectIds,
        limits: { max_depth: 3, max_items: 20 },
        reason,
      },
      'Generating bounded proposal…',
    );
  });

  detail.querySelectorAll('.goal-proposal').forEach((node) => {
    const id = node.dataset.proposalId;
    const proposal = proposalById(id);
    const encodedProposal = encodeURIComponent(id);
    node.querySelector('.goal-proposal-accept')?.addEventListener('click', () => mutate(
      `/api/goals/${encodedGoal}/decompositions/${encodedProposal}/review`,
      { decision: 'accept', reason: 'operator accepted reviewed decomposition' },
      'Accepting proposal…',
    ));
    node.querySelector('.goal-proposal-reject')?.addEventListener('click', () => mutate(
      `/api/goals/${encodedGoal}/decompositions/${encodedProposal}/review`,
      { decision: 'reject', reason: 'operator rejected reviewed decomposition' },
      'Rejecting proposal…',
    ));
    node.querySelector('.goal-proposal-revise')?.addEventListener('click', async () => {
      const reason = window.prompt('Revision reason', 'operator revised decomposition');
      if (!reason) return;
      await mutate(
        `/api/goals/${encodedGoal}/decompositions/${encodedProposal}/revise`,
        {
          items: proposal?.items || [],
          limits: proposal?.limits || null,
          reason,
          model_invocation_id: proposal?.model_invocation_id || null,
        },
        'Revising proposal…',
      );
    });
    node.querySelector('.goal-proposal-commit')?.addEventListener('click', () => mutate(
      `/api/goals/${encodedGoal}/decompositions/${encodedProposal}/commit`,
      { reason: 'operator committed accepted Goal work' },
      'Queueing authoritative Work Item creation…',
    ));
    node.querySelector('.goal-proposal-reconcile')?.addEventListener('click', () => mutate(
      `/api/goals/${encodedGoal}/decompositions/${encodedProposal}/commit/reconcile`,
      { reason: 'operator reconciled Goal commit state' },
      'Reconciling commit state…',
    ));
  });

  detail.querySelector('.goal-evaluate-completion')?.addEventListener('click', async () => {
    const observations = [...detail.querySelectorAll('.goal-observation')].map((row) => {
      const kind = row.dataset.kind;
      const source = row.querySelector('.goal-observation-source')?.value.trim();
      const reference = row.querySelector('.goal-observation-reference')?.value.trim() || null;
      const base = {
        criterion_id: row.dataset.criterionId,
        source: source || 'operator:goal-workspace',
        reference,
      };
      if (kind === 'manual') {
        return {
          ...base,
          verified: Boolean(row.querySelector('.goal-observation-verified')?.checked),
        };
      }
      const raw = row.querySelector('.goal-observation-value')?.value.trim() || '';
      const numeric = Number(raw);
      return {
        ...base,
        observed_value: raw !== '' && Number.isFinite(numeric) ? numeric : raw,
      };
    });
    await mutate(
      `/api/goals/${encodedGoal}/completion-evaluations`,
      { observations, reason: 'operator evaluated Goal completion' },
      'Evaluating completion…',
    );
  });

  detail.querySelector('.goal-complete')?.addEventListener('click', async () => {
    if (!state.completion?.id) return;
    await mutate(
      `/api/goals/${encodedGoal}/transition`,
      {
        status: 'completed',
        reason: 'current completion evaluation passed',
        completion_evaluation_id: state.completion.id,
      },
      'Completing Goal…',
    );
  });

  detail.querySelector('.goal-activate')?.addEventListener('click', () => mutate(
    `/api/goals/${encodedGoal}/transition`,
    { status: 'active', reason: goal.status === 'paused' ? 'operator resumed Goal' : 'operator activated Goal' },
    goal.status === 'paused' ? 'Resuming Goal…' : 'Activating Goal…',
  ));
  detail.querySelector('.goal-pause')?.addEventListener('click', () => mutate(
    `/api/goals/${encodedGoal}/transition`,
    { status: 'paused', reason: 'operator paused Goal' },
    'Pausing Goal…',
  ));
}

window.addEventListener('codex:open-goal', async (event) => {
  const goalId = String(event.detail?.goalId || '').trim();
  if (!goalId) return;
  ensureShell();
  const dialog = document.querySelector('#goals-dialog');
  if (!dialog.open) dialog.showModal();
  await refreshAll();
  if (state.goals.some((row) => row.goal?.id === goalId)) {
    state.selectedGoalId = goalId;
    renderGoalList();
    await loadGoal(goalId);
  } else {
    setStatus(`Goal not found in current tenant: ${goalId}`, true);
  }
});

ensureShell();
