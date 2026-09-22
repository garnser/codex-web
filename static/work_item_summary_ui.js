function value(value) {
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—';
  return value == null || value === '' ? '—' : String(value);
}

function stateLabel(item, execution, diagnostics) {
  const failure = execution?.failure_reason;
  if (item.current_stage === 'closed') return { label: 'Completed', tone: 'complete' };
  if (failure) return { label: 'Execution failed', tone: 'danger' };
  if (item.blocker || (item.blocking_findings || []).length) return { label: 'Blocked', tone: 'danger' };
  if ((diagnostics || []).some((entry) => /approval|quorum/i.test(String(entry.kind || '')))) {
    return { label: 'Approval required', tone: 'attention' };
  }
  if (item.release_gate) return { label: 'Release gate', tone: 'attention' };
  return { label: String(item.current_stage || 'Active').replaceAll('_', ' '), tone: 'active' };
}

export function workItemSummaryHtml({ item = {}, diagnostics = [], esc }) {
  const execution = item.execution || {};
  const failure = execution.failure_reason || null;
  const condition = stateLabel(item, execution, diagnostics);
  const findings = Array.isArray(item.blocking_findings) ? item.blocking_findings : [];
  const related = [
    ['Goal', item.goal_id],
    ['Decision', item.decision_id],
    ['Resources', value(item.resource_ids)],
    ['Merge requests', value(item.mr_refs)],
  ].filter(([, entry]) => entry && entry !== '—');

  return `
    <section class="work-overview" aria-label="Work Item overview">
      <div class="work-overview-card">
        <span>State</span>
        <strong class="work-overview-state ${esc(condition.tone)}">${esc(condition.label)}</strong>
        <small>${esc(item.current_stage || '—')}</small>
      </div>
      <div class="work-overview-card">
        <span>Owner</span>
        <strong>${esc(item.current_owner || item.next_owner || 'Unowned')}</strong>
        <small>Next: ${esc(item.next_owner || '—')}</small>
      </div>
      <div class="work-overview-card">
        <span>Priority</span>
        <strong>${esc(item.priority || 'Unspecified')}</strong>
        <small>${esc(item.kind || 'Work Item')}</small>
      </div>
      <div class="work-overview-card work-overview-next">
        <span>Next relevant action</span>
        <strong>${esc(item.next_action || 'No next action recorded')}</strong>
        <small>${item.release_gate ? 'Release gate is active' : 'Canonical work-item state'}</small>
      </div>
    </section>
    ${item.blocker || findings.length ? `
      <section class="work-blocker-banner" role="status">
        <strong>Blocked</strong>
        <span>${esc(item.blocker || findings[0] || 'Blocking finding recorded')}</span>
        ${findings.length > 1 ? `<small>${esc(findings.slice(1).join(' · '))}</small>` : ''}
      </section>` : ''}
    ${failure ? `
      <section class="work-failure-banner" role="alert">
        <strong>${esc(failure.code || 'execution_failure')}</strong>
        <span>${esc(failure.message || 'Execution failed')}</span>
        <small>${esc(failure.category || 'failure')} · retry ${esc(execution.retry?.attempt ?? 0)}/${esc(execution.retry?.policy?.max_attempts ?? '—')}</small>
      </section>` : ''}
    <div class="work-detail-columns work-routing-columns">
      <section class="work-detail-card">
        <h4>Ownership & routing</h4>
        <div class="work-routing-lanes" aria-label="Work Item ownership lanes">
          <div><span>Implementation</span><strong>${esc(item.implementation_owner || '—')}</strong></div>
          <div><span>Validation</span><strong>${esc(item.validation_owner || '—')}</strong></div>
          <div><span>Release</span><strong>${esc(item.release_owner || '—')}</strong></div>
        </div>
        <small>Current owner and stage remain canonical; lanes describe the configured handoff path.</small>
      </section>
      <section class="work-detail-card">
        <h4>Related canonical objects</h4>
        <div class="work-related-objects">
          ${related.length ? related.map(([label, entry]) => `<div><span>${esc(label)}</span><strong>${esc(entry)}</strong></div>`).join('') : '<small>No related goal, decision, resource, or merge-request references recorded.</small>'}
        </div>
        ${item.url ? `<a class="work-external-link" href="${esc(item.url)}" target="_blank" rel="noreferrer">Open authoritative item</a>` : ''}
      </section>
    </div>`;
}
