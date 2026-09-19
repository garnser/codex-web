import { request } from './api_client.js';

const state = {
  decisions: [],
  selectedDecisionId: '',
  detail: null,
  revisions: [],
  events: [],
  trace: null,
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

function ensureShell() {
  if (document.querySelector('#decisions-dialog')) return;

  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/static/decisions_ui.css';
  document.head.appendChild(link);

  const button = document.createElement('button');
  button.id = 'decisions-button';
  button.type = 'button';
  button.className = 'ghost-button';
  button.textContent = 'Decisions';
  button.title = 'Open durable Decision workspace';
  (document.querySelector('.topbar .controls') || document.body).prepend(button);

  const dialog = document.createElement('dialog');
  dialog.id = 'decisions-dialog';
  dialog.className = 'decisions-dialog';
  dialog.innerHTML = `
    <div class="decisions-shell">
      <header class="decisions-header">
        <div>
          <h2>Decision workspace</h2>
          <p>Durable reasoning, exact evidence, bounded deliberation and canonical approval.</p>
        </div>
        <button type="button" class="icon-button decisions-close" aria-label="Close">×</button>
      </header>
      <div class="decisions-toolbar">
        <button type="button" class="ghost-button decisions-refresh">Refresh</button>
        <span class="decisions-status" aria-live="polite"></span>
      </div>
      <div class="decisions-layout">
        <aside class="decisions-list" aria-label="Decisions"></aside>
        <main class="decision-detail">
          <div class="decision-empty">Select a Decision to inspect its canonical record.</div>
        </main>
      </div>
    </div>`;
  document.body.appendChild(dialog);

  button.addEventListener('click', async () => {
    dialog.showModal();
    await refreshAll();
  });
  dialog.querySelector('.decisions-close').addEventListener('click', () => dialog.close());
  dialog.querySelector('.decisions-refresh').addEventListener('click', refreshAll);
}

function setStatus(message, isError = false) {
  const node = document.querySelector('.decisions-status');
  if (!node) return;
  node.textContent = message || '';
  node.classList.toggle('error', Boolean(isError));
}

function statusBadge(status) {
  return `<span class="decision-status ${esc(status || 'unknown')}">${esc(status || 'unknown')}</span>`;
}

function freshnessBadge(value) {
  if (!value) return '';
  return `<span class="decision-freshness ${esc(value)}">${esc(value)}</span>`;
}

async function refreshAll() {
  setStatus('Loading…');
  try {
    const payload = await request('/api/decisions');
    state.decisions = Array.isArray(payload?.items) ? payload.items : [];
    if (
      !state.selectedDecisionId
      || !state.decisions.some((item) => item.id === state.selectedDecisionId)
    ) {
      state.selectedDecisionId = state.decisions[0]?.id || '';
    }
    renderList();
    if (state.selectedDecisionId) {
      await loadDecision(state.selectedDecisionId);
    } else {
      document.querySelector('.decision-detail').innerHTML =
        '<div class="decision-empty">No Decisions exist in this workspace.</div>';
    }
    setStatus('Up to date');
  } catch (error) {
    setStatus(error.message || 'Failed to load Decisions', true);
  }
}

function renderList() {
  const host = document.querySelector('.decisions-list');
  if (!host) return;
  if (!state.decisions.length) {
    host.innerHTML = '<div class="decision-empty">No Decisions.</div>';
    return;
  }
  host.innerHTML = state.decisions.map((item) => {
    const selected = item.id === state.selectedDecisionId ? 'selected' : '';
    return `
      <button type="button" class="decision-row ${selected}" data-decision-id="${esc(item.id)}">
        <strong>${esc(item.title || item.id)}</strong>
        <span>${statusBadge(item.status)} · ${esc(item.importance)}</span>
        <small>r${esc(item.revision)} · updated ${esc(fmtTime(item.updated_at))}</small>
      </button>`;
  }).join('');
  host.querySelectorAll('.decision-row').forEach((row) => {
    row.addEventListener('click', async () => {
      state.selectedDecisionId = row.dataset.decisionId;
      renderList();
      await loadDecision(state.selectedDecisionId);
    });
  });
}

async function loadDecision(decisionId) {
  const host = document.querySelector('.decision-detail');
  if (!host) return;
  host.innerHTML = '<div class="decision-empty">Loading Decision…</div>';
  try {
    const encoded = encodeURIComponent(decisionId);
    const [detail, revisions, events, trace] = await Promise.all([
      request(`/api/decisions/${encoded}`),
      request(`/api/decisions/${encoded}/revisions`),
      request(`/api/decisions/${encoded}/events`),
      request(`/api/decisions/${encoded}/trace`),
    ]);
    state.detail = detail;
    state.revisions = revisions?.items || [];
    state.events = events?.items || [];
    state.trace = trace || null;
    renderDetail();
  } catch (error) {
    host.innerHTML = `<div class="decision-error">${esc(error.message || 'Failed to load Decision')}</div>`;
  }
}

function renderEvidence(rows) {
  if (!rows?.length) return '<p class="decision-muted">No evidence references.</p>';
  return rows.map((item) => {
    if (item.kind === 'metric_snapshot') {
      const href = `/api/metrics/${encodeURIComponent(item.metric_id)}/snapshots/${encodeURIComponent(item.metric_snapshot_id)}`;
      return `
        <article class="decision-subcard">
          <strong>Metric snapshot <a class="decision-link" href="${esc(href)}" target="_blank" rel="noopener">${esc(item.metric_snapshot_id)}</a></strong>
          <span>${freshnessBadge(item.metric_freshness)} · ${esc(item.observed_value ?? '—')} ${esc(item.unit || '')}</span>
          <small>metric ${esc(item.metric_id)} r${esc(item.metric_revision)} · observations: ${esc((item.observation_ids || []).join(', ') || 'none')}</small>
          <small>window: ${esc(fmtTime(item.window_start))} → ${esc(fmtTime(item.window_end))}</small>
          <small>${esc(item.summary || '')}</small>
        </article>`;
    }
    const href = `/api/evidence/${encodeURIComponent(item.evidence_id)}`;
    return `
      <article class="decision-subcard">
        <strong>Evidence <a class="decision-link" href="${esc(href)}" target="_blank" rel="noopener">${esc(item.evidence_id)}</a></strong>
        <small>${esc(item.summary || 'Canonical Evidence reference')}</small>
      </article>`;
  }).join('');
}

function renderTrace(decision) {
  const trace = state.trace || {};
  const goal = trace.goal?.goal || null;
  const workLinks = decision.work_links || [];
  const workItems = new Map((trace.work_items || []).map((item) => [item.ref, item]));
  const intents = new Map(
    (trace.action_intents || [])
      .filter((item) => item?.intent?.id)
      .map((item) => [item.intent.id, item]),
  );
  const goalLine = decision.goal_id
    ? `<div class="decision-trace-node"><span>Goal</span><strong>${esc(goal?.title || decision.goal_id)}</strong><small>${esc(decision.goal_id)} · ${esc(goal?.status || 'linked')}</small></div>`
    : '<div class="decision-trace-node"><span>Goal</span><strong>Not linked</strong><small>This Decision is workspace/project scoped.</small></div>';

  const work = workLinks.length
    ? workLinks.map((link) => {
        const item = workItems.get(link.work_item_ref);
        const intent = intents.get(link.action_intent_id)?.intent;
        return `
          <article class="decision-work-link">
            <div class="decision-work-heading">
              <strong>${esc(link.title)}</strong>
              <span class="decision-work-state ${esc(link.state)}">${esc(link.state)}</span>
            </div>
            <p>${esc(link.description)}</p>
            <div class="decision-grid">
              <div><span>Project</span><strong>${esc(link.project_id)}</strong></div>
              <div><span>ActionIntent</span><strong class="decision-break">${esc(link.action_intent_id || 'planned')}</strong></div>
              <div><span>Work Item</span><strong>${esc(link.work_item_ref || 'not reconciled')}</strong></div>
              <div><span>Work stage</span><strong>${esc(item?.current_stage || '—')}</strong></div>
              <div><span>Outcome</span><strong>${esc(item?.terminal_outcome || intent?.status || '—')}</strong></div>
              <div><span>Expected result</span><strong>${esc(link.expected_result || '—')}</strong></div>
            </div>
            ${link.last_error ? `<small class="decision-error">${esc(link.last_error)}</small>` : ''}
          </article>`;
      }).join('')
    : '<p class="decision-muted">No canonical work has been generated from this Decision.</p>';

  return `
    <div class="decision-trace">
      ${goalLine}
      <div class="decision-trace-arrow">→</div>
      <div class="decision-trace-node"><span>Decision</span><strong>${esc(decision.title)}</strong><small>${esc(decision.id)} · ${esc(decision.status)}</small></div>
    </div>
    <div class="decision-work-links">${work}</div>`;
}

function renderWorkForm(decision) {
  if (decision.status !== 'approved') return '';
  const nextIndex = (decision.work_links || []).length + 1;
  const needsReconcile = (decision.work_links || []).some((item) => item.state !== 'succeeded');
  return `
    <form class="decision-work-form" data-item-id="work-${esc(nextIndex)}">
      <div class="decision-work-form-grid">
        <label><span>Project ID</span><input name="project_id" required value="${esc(decision.project_id || '')}"></label>
        <label><span>Title</span><input name="title" required placeholder="Canonical work item title"></label>
        <label class="decision-work-wide"><span>Description</span><textarea name="description" required rows="3" placeholder="What approved consequence should be carried out?"></textarea></label>
        <label class="decision-work-wide"><span>Expected result</span><input name="expected_result" placeholder="Observable result"></label>
      </div>
      <div class="decision-actions">
        <button type="submit" class="primary-button">Generate canonical work</button>
        ${needsReconcile ? '<button type="button" class="ghost-button decision-reconcile-work">Reconcile work</button>' : ''}
      </div>
    </form>`;
}

function renderOptions(decision) {
  const recommended = decision.recommendation?.option_id;
  const finalId = decision.final_decision?.option_id;
  return (decision.options || []).map((item) => `
    <article class="decision-option ${item.id === finalId ? 'final' : ''} ${item.id === recommended ? 'recommended' : ''}">
      <div class="decision-option-heading">
        <strong>${esc(item.title)}</strong>
        <span>${item.id === finalId ? 'FINAL' : item.id === recommended ? 'RECOMMENDED' : ''}</span>
      </div>
      <p>${esc(item.description)}</p>
      <div class="decision-option-columns">
        <div><span>Pros</span><ul>${(item.pros || []).map((v) => `<li>${esc(v)}</li>`).join('') || '<li>—</li>'}</ul></div>
        <div><span>Cons</span><ul>${(item.cons || []).map((v) => `<li>${esc(v)}</li>`).join('') || '<li>—</li>'}</ul></div>
        <div><span>Risks</span><ul>${(item.risks || []).map((v) => `<li>${esc(v)}</li>`).join('') || '<li>—</li>'}</ul></div>
      </div>
    </article>
  `).join('');
}

function renderParticipants(decision) {
  return (decision.participants || []).map((item) => `
    <article class="decision-subcard">
      <strong>${esc(item.role)}</strong>
      <small>${esc(item.perspective)}</small>
      <small>${item.identity_id ? `identity: ${esc(item.identity_id)}` : 'analytical role; no identity authority'}</small>
    </article>
  `).join('');
}

function renderRounds(decision) {
  if (!decision.deliberation_rounds?.length) {
    return '<p class="decision-muted">No model-assisted deliberation rounds.</p>';
  }
  return decision.deliberation_rounds.map((round) => `
    <article class="decision-round">
      <strong>Round ${esc(round.round_number)}</strong>
      <small>synthesis invocation: ${esc(round.synthesis_model_invocation_id)}</small>
      <div class="decision-analyses">
        ${(round.analyses || []).map((item) => `
          <div>
            <strong>${esc(item.participant_id)}</strong>
            <p>${esc(item.analysis)}</p>
            <small>preferred: ${esc(item.preferred_option_id || 'none')} · invocation ${esc(item.model_invocation_id)}</small>
          </div>
        `).join('')}
      </div>
    </article>
  `).join('');
}

function renderApproval(detail) {
  const approval = detail?.approval_request;
  if (!approval) return '<p class="decision-muted">No canonical ApprovalRequest is bound.</p>';
  return `
    <div class="decision-grid">
      <div><span>ApprovalRequest</span><strong>${esc(approval.id)}</strong></div>
      <div><span>Status</span><strong>${esc(approval.status)}</strong></div>
      <div><span>Quorum</span><strong>${esc(approval.requirement?.quorum ?? 1)}</strong></div>
      <div><span>Assurance</span><strong>${esc(approval.requirement?.required_assurance || '—')}</strong></div>
      <div><span>Target version</span><strong>${esc(approval.target?.target_version || '—')}</strong></div>
      <div><span>Target digest</span><strong class="decision-break">${esc(approval.target?.target_digest || '—')}</strong></div>
    </div>`;
}

function renderActions(decision, approval) {
  const actions = [];
  if (['draft', 'analysis'].includes(decision.status)) {
    actions.push('<button type="button" class="ghost-button decision-deliberate">Run bounded deliberation</button>');
    if (decision.recommendation) {
      actions.push('<button type="button" class="primary-button decision-request-approval">Request canonical approval</button>');
    }
  }
  if (decision.status === 'awaiting_approval') {
    actions.push('<button type="button" class="primary-button decision-finalize">Sync/finalize canonical approval</button>');
    if (approval) {
      actions.push(`<span class="decision-muted">Approval state: ${esc(approval.status)}</span>`);
    }
  }
  return actions.length
    ? `<div class="decision-actions">${actions.join('')}</div>`
    : '';
}

function renderDetail() {
  const host = document.querySelector('.decision-detail');
  if (!host) return;
  const detail = state.detail || {};
  const decision = detail.item || {};
  const usage = detail.model_usage || {};
  const approval = detail.approval_request || null;

  host.innerHTML = `
    <section class="decision-card decision-summary">
      <div>
        <div class="decision-title-line">
          <h3>${esc(decision.title || decision.id)}</h3>
          ${statusBadge(decision.status)}
        </div>
        <p>${esc(decision.question || '')}</p>
      </div>
      <div class="decision-meta-strong">
        <span>${esc(decision.importance)} importance</span>
        <strong>r${esc(decision.revision)}</strong>
      </div>
    </section>

    ${renderActions(decision, approval)}

    <section class="decision-card decision-grid">
      <div><span>Canonical ID</span><strong>${esc(decision.id)}</strong></div>
      <div><span>Initiator</span><strong>${esc(decision.initiator_identity_id)}</strong></div>
      <div><span>Project</span><strong>${esc(decision.project_id || 'workspace')}</strong></div>
      <div><span>Review date</span><strong>${esc(fmtTime(decision.review_at))}</strong></div>
      <div><span>Expiry</span><strong>${esc(fmtTime(decision.expires_at))}</strong></div>
      <div><span>Superseded by</span><strong>${esc(decision.superseded_by_decision_id || '—')}</strong></div>
    </section>

    <section class="decision-card">
      <h4>Recommendation / final decision</h4>
      ${decision.recommendation ? `
        <div class="decision-recommendation">
          <strong>Recommendation: ${esc(decision.recommendation.option_id)}</strong>
          <p>${esc(decision.recommendation.rationale)}</p>
          <small>confidence ${esc(decision.recommendation.confidence)} · uncertainty: ${esc((decision.recommendation.uncertainty || []).join('; ') || 'none recorded')}</small>
        </div>` : '<p class="decision-muted">No recommendation recorded.</p>'}
      ${decision.final_decision ? `
        <div class="decision-final">
          <strong>Approved final: ${esc(decision.final_decision.option_id)}</strong>
          <p>${esc(decision.final_decision.rationale)}</p>
          <small>ApprovalRequest ${esc(decision.final_decision.approval_request_id)} · ${esc(fmtTime(decision.final_decision.decided_at))}</small>
        </div>` : ''}
    </section>

    <section class="decision-card">
      <h4>Options</h4>
      <div class="decision-options">${renderOptions(decision)}</div>
    </section>

    <section class="decision-card">
      <h4>Participants</h4>
      <div class="decision-subcards">${renderParticipants(decision)}</div>
    </section>

    <section class="decision-card">
      <h4>Evidence & metric provenance</h4>
      <div class="decision-subcards">${renderEvidence(decision.evidence)}</div>
    </section>

    <section class="decision-card">
      <h4>Canonical approval</h4>
      ${renderApproval(detail)}
    </section>

    <section class="decision-card">
      <h4>Goal → Decision → Work → Result trace</h4>
      ${renderTrace(decision)}
      ${renderWorkForm(decision)}
    </section>

    <section class="decision-card">
      <h4>Bounded deliberation</h4>
      <div class="decision-grid">
        <div><span>Calls</span><strong>${esc(usage.calls ?? 0)} / ${esc(decision.budget?.max_model_calls ?? '—')}</strong></div>
        <div><span>Input tokens</span><strong>${esc(usage.input_tokens ?? 0)} / ${esc(decision.budget?.max_input_tokens ?? '—')}</strong></div>
        <div><span>Output tokens</span><strong>${esc(usage.output_tokens ?? 0)} / ${esc(decision.budget?.max_output_tokens ?? '—')}</strong></div>
        <div><span>Cost</span><strong>$${esc(usage.cost_usd ?? 0)} / $${esc(decision.budget?.max_cost_usd ?? '—')}</strong></div>
      </div>
      <div class="decision-rounds">${renderRounds(decision)}</div>
    </section>

    <section class="decision-card">
      <h4>Dissent</h4>
      ${decision.dissent?.length ? decision.dissent.map((item) => `
        <article class="decision-subcard">
          <strong>${esc(item.participant_id)}${item.option_id ? ` · ${esc(item.option_id)}` : ''}</strong>
          <small>${esc(item.rationale)}</small>
        </article>
      `).join('') : '<p class="decision-muted">No material dissent recorded.</p>'}
    </section>

    <section class="decision-card">
      <h4>Post-execution reviews</h4>
      ${decision.post_execution_reviews?.length ? decision.post_execution_reviews.map((item) => `
        <article class="decision-subcard">
          <strong>${esc(item.outcome)} · ${esc(fmtTime(item.reviewed_at))}</strong>
          <small>${esc(item.summary)}</small>
          <small>Evidence: ${esc((item.evidence_refs || []).join(', ') || 'none')}</small>
        </article>
      `).join('') : '<p class="decision-muted">No post-execution review recorded.</p>'}
    </section>

    <section class="decision-card">
      <h4>Revision & event history</h4>
      <div class="decision-history">
        ${state.revisions.map((item) => `
          <div><strong>r${esc(item.revision)}</strong><span>${esc(item.reason)} · ${esc(item.revised_by)} · ${esc(fmtTime(item.revised_at))}</span></div>
        `).join('')}
        ${state.events.map((item) => `
          <div><strong>${esc(item.event_type)}</strong><span>${esc(item.reason)} · r${esc(item.revision)}</span></div>
        `).join('')}
      </div>
    </section>`;

  const workForm = host.querySelector('.decision-work-form');
  if (workForm) workForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(workForm);
    const itemId = workForm.dataset.itemId;
    const body = {
      items: [{
        id: itemId,
        project_id: String(form.get('project_id') || '').trim(),
        title: String(form.get('title') || '').trim(),
        description: String(form.get('description') || '').trim(),
        expected_result: String(form.get('expected_result') || '').trim() || null,
        labels: [],
        blocked_by_item_ids: [],
      }],
      reason: 'Generate approved Decision consequence as canonical work.',
    };
    await mutate(
      `/api/decisions/${encodeURIComponent(decision.id)}/work`,
      body,
      'Generating canonical Decision work…',
    );
  });
  const reconcileWork = host.querySelector('.decision-reconcile-work');
  if (reconcileWork) reconcileWork.addEventListener('click', () => mutate(
    `/api/decisions/${encodeURIComponent(decision.id)}/work/reconcile`,
    {},
    'Reconciling Decision work…',
  ));

  const deliberate = host.querySelector('.decision-deliberate');
  if (deliberate) deliberate.addEventListener('click', () => mutate(
    `/api/decisions/${encodeURIComponent(decision.id)}/deliberate`,
    {},
    'Running bounded deliberation…',
  ));
  const approvalButton = host.querySelector('.decision-request-approval');
  if (approvalButton) approvalButton.addEventListener('click', () => mutate(
    `/api/decisions/${encodeURIComponent(decision.id)}/approval-request`,
    { reason: 'Decision recommendation is ready for canonical review.' },
    'Creating canonical ApprovalRequest…',
  ));
  const finalize = host.querySelector('.decision-finalize');
  if (finalize) finalize.addEventListener('click', () => mutate(
    `/api/decisions/${encodeURIComponent(decision.id)}/approval/finalize`,
    {},
    'Synchronizing canonical approval…',
  ));
}

async function mutate(path, body, message) {
  setStatus(message);
  try {
    await request(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    await refreshAll();
  } catch (error) {
    setStatus(error.message || 'Decision action failed', true);
  }
}

ensureShell();
