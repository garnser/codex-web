export function createRunTimelineUi({ state, request, esc, fmtTime, pathRef, setStatus, pageSize }) {
function keyValueRows(values) {
  return Object.entries(values).map(([key, value]) => `
    <div><span>${esc(key.replaceAll('_', ' '))}</span><strong>${esc(value ?? '—')}</strong></div>`).join('');
}

function repositoryScopeSummary(scope) {
  const writable = Array.isArray(scope?.writableRepositoryIds) ? scope.writableRepositoryIds : [];
  const readOnly = Array.isArray(scope?.readOnlyRepositoryIds) ? scope.readOnlyRepositoryIds : [];
  if (!scope || (!writable.length && !readOnly.length)) return '—';
  const parts = [];
  if (writable.length) parts.push(`write: ${writable.join(', ')}`);
  if (readOnly.length) parts.push(`read: ${readOnly.join(', ')}`);
  return parts.join(' · ');
}

function repositoryScopeHtml(scope) {
  if (!scope) return '<small>No repository execution scope recorded.</small>';
  const writable = Array.isArray(scope.writableRepositoryIds) ? scope.writableRepositoryIds : [];
  const readOnly = Array.isArray(scope.readOnlyRepositoryIds) ? scope.readOnlyRepositoryIds : [];
  return `
    <div class="work-kv work-kv-wide">${keyValueRows({
      write_mode: scope.writeMode,
      writable_repositories: writable.join(', ') || '—',
      read_only_repositories: readOnly.join(', ') || '—',
      selection_source: scope.source,
      selection_ref: scope.sourceRef,
    })}</div>`;
}

function repositoryOutcomesHtml(outcomes) {
  if (!Array.isArray(outcomes) || !outcomes.length) {
    return '<small>No repository-specific outcomes recorded yet.</small>';
  }
  return outcomes.map((entry) => {
    const integration = entry.integration || {};
    const conflicts = Array.isArray(integration.conflicts) ? integration.conflicts : [];
    const detail = [
      integration.outcome || entry.status,
      integration.strategy,
      entry.headRevision ? `head ${entry.headRevision}` : null,
      conflicts.length ? `conflicts: ${conflicts.join(', ')}` : null,
    ].filter(Boolean).join(' · ');
    return `
      <div class="work-run-activity-row">
        <strong>${esc(entry.repositoryId || 'repository')} · ${esc(entry.status || 'pending')}</strong>
        <span>${esc(detail || 'No integration result recorded')}</span>
        <small>${esc(fmtTime(integration.recordedAt))}</small>
      </div>`;
  }).join('');
}

function runTimelineHtml() {
  const active = Array.isArray(state.runs?.active) ? state.runs.active : [];
  const history = Array.isArray(state.runs?.items) ? state.runs.items : [];
  const rows = [
    ...active.map((run) => ({ ...run, pinnedActive: true })),
    ...history,
  ];
  const cards = rows.length ? rows.map((run) => {
    const agent = run.agent || {};
    const runtime = run.runtime || {};
    const usage = run.usage || {};
    const retries = run.retryLineage || {};
    const failure = run.failure || {};
    return `
      <details class="work-run" data-execution-id="${esc(run.executionId)}">
        <summary>
          <span class="work-run-status ${esc(run.status)}">${esc(run.status)}</span>
          <strong>${esc(agent.profileId || run.executionId)}</strong>
          <span>${esc(runtime.providerId || agent.selectedProviderId || 'provider —')} · ${esc(runtime.runtimeId || agent.selectedRuntimeId || 'runtime —')}</span>
          <small>${esc(fmtTime(run.startedAt || run.createdAt))}</small>
          ${run.pinnedActive ? '<span class="work-run-live">live</span>' : ''}
        </summary>
        <div class="work-run-summary">
          <div class="work-kv work-kv-wide">${keyValueRows({
            execution_id: run.executionId,
            assignment_id: run.assignmentId,
            worker: run.worker?.workerId,
            fence: run.worker?.fence,
            attempts: retries.attemptCount,
            repositories: repositoryScopeSummary(run.repositoryScope),
            repository_outcome: run.repositoryOutcomeStatus,
            model: (usage.models || []).join(', ') || agent.modelId,
            tokens: usage.totalTokens,
            cost_usd: usage.costUsd,
            completed: fmtTime(run.completedAt),
            failure: failure.summary || run.failureMessage,
          })}</div>
          <div class="work-run-expanded"><small>Expand to load bounded Run activity, evidence and provenance.</small></div>
        </div>
      </details>`;
  }).join('') : '<small>No Runs recorded for this Work Item.</small>';

  return `
    <div class="work-run-heading">
      <div><h4>Run timeline</h4><small>Canonical execution projection · active Runs pinned</small></div>
      ${state.runs?.activeTruncated ? '<span class="work-item-warning">Active Run list truncated</span>' : ''}
    </div>
    <div class="work-run-list">${cards}</div>
    <div class="work-run-footer">
      <button type="button" class="ghost-button work-runs-load-more" ${state.runs?.hasMore ? '' : 'disabled'}>${state.runs?.hasMore ? 'Load older Runs' : 'All Runs loaded'}</button>
    </div>`;
}

function runDetailHtml(run) {
  const usage = run.usage || {};
  const attempts = run.retryLineage?.attempts || [];
  const artifacts = run.artifacts || [];
  const evidence = run.evidence || [];
  const verifications = run.verifications || [];
  const actions = run.actions || [];
  const approvals = run.approvals || [];
  const attention = run.attention || [];
  const repositoryOutcomes = Array.isArray(run.repositoryOutcomes) ? run.repositoryOutcomes : [];
  const repositoryActivity = Array.isArray(run.repositoryActivity) ? run.repositoryActivity : [];
  const repositoryActivityRows = repositoryActivity.length ? repositoryActivity.map((entry) => {
    const repositories = Array.isArray(entry.repositoryIds) && entry.repositoryIds.length
      ? entry.repositoryIds.join(', ')
      : 'run/global';
    return `
      <div class="work-run-activity-row">
        <strong>${esc(entry.kind)} · ${esc(entry.label || entry.id)}</strong>
        <span>${esc(repositories)} · ${esc(entry.status || 'recorded')}</span>
        <small>${esc(fmtTime(entry.occurredAt))}</small>
      </div>`;
  }).join('') : '<small>No repository-attributed Run activity recorded.</small>';
  const attemptRows = attempts.length ? attempts.map((attempt) => `
    <div class="work-run-activity-row">
      <strong>Attempt ${esc(attempt.attempt)}</strong>
      <span>${esc(attempt.workerId || 'unassigned')} · ${esc(attempt.outcome || 'in progress')}</span>
      <small>${esc(fmtTime(attempt.startedAt || attempt.claimedAt))}</small>
    </div>`).join('') : '<small>No retry attempts recorded.</small>';
  const activityRows = actions.length ? actions.map((action) => `
    <div class="work-run-activity-row">
      <strong>${esc(action.actionId)}</strong>
      <span>${esc(action.providerType)} · ${esc(action.status)}</span>
      <small>${esc(fmtTime(action.completedAt || action.createdAt))}</small>
    </div>`).join('') : '<small>No structured external actions recorded.</small>';
  const evidenceRows = [
    ...artifacts.map((item) => `Artifact · ${esc(item.type)} · ${esc(item.name)}`),
    ...evidence.map((item) => `Evidence · ${esc(item.type)} · ${esc(item.result)}${item.summary ? ` · ${esc(item.summary)}` : ''}`),
    ...verifications.map((item) => `Verification · ${esc(item.result)} · ${esc(item.method)}`),
  ];
  return `
    <div class="work-detail-columns">
      <div>
        <h5>Execution provenance</h5>
        <div class="work-kv">${keyValueRows({
          agent: run.agent?.profileId,
          agent_revision: run.agent?.profileRevision,
          role: run.agent?.roleId,
          provider: run.runtime?.providerId || run.agent?.selectedProviderId,
          runtime: run.runtime?.runtimeId || run.agent?.selectedRuntimeId,
          model: (usage.models || []).join(', ') || run.agent?.modelId,
          worker: run.worker?.workerId,
          contract: run.executionContractVersion,
        })}</div>
      </div>
      <div>
        <h5>Usage & activity</h5>
        <div class="work-kv">${keyValueRows({
          input_tokens: usage.inputTokens,
          output_tokens: usage.outputTokens,
          reasoning_tokens: usage.reasoningTokens,
          total_tokens: usage.totalTokens,
          cost_usd: usage.costUsd,
          tool_calls: usage.toolCalls,
          file_edits: usage.fileEdits,
          git_operations: usage.gitOperations,
        })}</div>
      </div>
    </div>
    <h5>Repository execution scope</h5>
    <div class="work-run-repositories">${repositoryScopeHtml(run.repositoryScope)}</div>
    <h5>Repository outcomes</h5>
    <div class="work-kv work-kv-wide">${keyValueRows({
      aggregate_outcome: run.repositoryOutcomeStatus,
    })}</div>
    <div class="work-run-activity work-run-repository-outcomes">${repositoryOutcomesHtml(repositoryOutcomes)}</div>
    <h5>Combined repository activity</h5>
    <div class="work-run-activity work-run-repository-activity">${repositoryActivityRows}</div>
    <h5>Attempts / retry lineage</h5>
    <div class="work-run-activity">${attemptRows}</div>
    <h5>Structured actions</h5>
    <div class="work-run-activity">${activityRows}</div>
    <h5>Artifacts, Evidence & Verification</h5>
    <div class="work-run-links">${evidenceRows.length ? evidenceRows.map((row) => `<span>${row}</span>`).join('') : '<small>No linked artifacts/evidence.</small>'}</div>
    <div class="work-run-related">
      <span>Approvals: <strong>${esc(approvals.length)}</strong></span>
      <span>Attention: <strong>${esc(attention.length)}</strong></span>
      <span>Action receipts: <strong>${esc((run.actionReceiptIds || []).length)}</strong></span>
      <span>Correlations: <strong>${esc((run.correlationIds || []).length)}</strong></span>
    </div>
    ${run.failure ? `<div class="work-item-error"><strong>${esc(run.failure.reason_code || run.failure.reasonCode || run.failureCode || 'failure')}</strong> ${esc(run.failure.summary || run.failureMessage || '')}</div>` : ''}`;
}

async function loadRunDetail(details) {
  if (details.dataset.loaded === 'true' || details.dataset.loading === 'true') return;
  const executionId = details.dataset.executionId;
  if (!state.selectedRef || !executionId) return;
  details.dataset.loading = 'true';
  const target = details.querySelector('.work-run-expanded');
  if (target) target.innerHTML = '<small>Loading Run detail…</small>';
  try {
    const payload = await request(
      `/api/work-items/${pathRef(state.selectedRef)}/runs/${encodeURIComponent(executionId)}`,
    );
    if (target) target.innerHTML = runDetailHtml(payload.run || {});
    details.dataset.loaded = 'true';
  } catch (error) {
    if (target) target.innerHTML = `<div class="work-item-error">${esc(error.message || 'Failed to load Run detail')}</div>`;
  } finally {
    details.dataset.loading = 'false';
  }
}

function wireRunTimeline() {
  const timeline = document.querySelector('.work-run-timeline');
  if (!timeline) return;
  timeline.querySelectorAll('.work-run').forEach((details) => {
    details.addEventListener('toggle', () => {
      if (details.open) loadRunDetail(details);
    });
  });
  timeline.querySelector('.work-runs-load-more')?.addEventListener('click', () => {
    refreshRuns({ append: true }).catch((error) => setStatus(error.message || 'Failed to load older Runs', true));
  });
}

function renderRunTimelineOnly() {
  const timeline = document.querySelector('.work-run-timeline');
  if (!timeline) return;
  timeline.innerHTML = runTimelineHtml();
  wireRunTimeline();
}

async function refreshRuns({ append = false } = {}) {
  if (!state.selectedRef) return;
  const query = new URLSearchParams({ limit: String(pageSize) });
  if (append && state.runs?.nextCursor) query.set('cursor', state.runs.nextCursor);
  const payload = await request(
    `/api/work-items/${pathRef(state.selectedRef)}/runs?${query}`,
  );
  const page = Array.isArray(payload.items) ? payload.items : [];
  if (append) {
    const byId = new Map((state.runs.items || []).map((run) => [run.executionId, run]));
    page.forEach((run) => byId.set(run.executionId, run));
    state.runs.items = [...byId.values()];
    state.runs.active = Array.isArray(payload.active) ? payload.active : state.runs.active;
  } else {
    state.runs = {
      active: Array.isArray(payload.active) ? payload.active : [],
      items: page,
      nextCursor: payload.nextCursor || null,
      hasMore: Boolean(payload.hasMore),
      activeTruncated: Boolean(payload.activeTruncated),
    };
  }
  state.runs.nextCursor = payload.nextCursor || null;
  state.runs.hasMore = Boolean(payload.hasMore);
  state.runs.activeTruncated = Boolean(payload.activeTruncated);
  renderRunTimelineOnly();
}


  return { runTimelineHtml, wireRunTimeline, refreshRuns };
}
