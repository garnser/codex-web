const escapeHtml = (value) => String(value ?? '').replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
const link = (path, label) => `<a href="${escapeHtml(path)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;

export function renderLocalInstallation(data) {
  const canonical = data.installation_verified
    ? 'Installation verified by canonical post-upgrade evidence'
    : 'Installation has no completed canonical verification';
  const process = data.process || { status: 'unknown' };
  const processText = process.status === 'running' ? `Tracked installer running · PID ${process.process_id}`
    : process.status === 'exited' ? `Tracked installer exited · code ${process.exit_code}; exit alone does not prove installation`
    : `Installer process unknown · ${process.reason || 'No live tracked handle'}`;
  const reports = data.reported_diagnostics || [];
  return `<p><strong>${escapeHtml(canonical)}</strong></p>
    <p>Source ${escapeHtml(data.source_revision)} → target ${escapeHtml(data.target_revision)}</p>
    <p>Plan ${escapeHtml(data.plan_status)} · maintenance ${escapeHtml(data.maintenance_mode)} · instance ${escapeHtml(data.service_instance_id || 'unbound')}</p>
    <p>${escapeHtml(processText)}</p>
    <p>${link(data.plan_href, 'Upgrade Plan')} · ${link(data.release_href, 'Immutable Release')}
      ${data.preflight_evidence_href ? ' · ' + link(data.preflight_evidence_href, 'Preflight Evidence') : ''}
      ${data.canonical_verification ? ' · ' + link(data.canonical_verification.href, 'Post-upgrade Evidence') : ''}</p>
    <p>Installer diagnostics are reported claims. They grant no permission and do not complete verification.</p>
    ${data.cancellation ? `<p>Unexecuted plan cancelled: ${escapeHtml(data.cancellation.reason)} · ${link(data.cancellation.href, 'Cancellation Evidence')}. Installation and rollback were not claimed.</p>` : ''}
    ${data.cancellation_request_available ? `<div><label>Cancellation reason <textarea data-installation-cancel-reason minlength="10" maxlength="500"></textarea></label>
      <button type="button" class="ghost-button" data-installation-cancel>Request cancellation of unexecuted plan</button>
      <p>The server verifies authority, current installation, installer lock and execution state. Other maintenance holds and paused autonomy remain effective.</p></div>` : ''}
    ${reports.length ? reports.map((report) => `<div class="acc-row">
      <strong>Reported ${escapeHtml(report.phase)}${report.stale_instance ? ' · stale instance' : ''}</strong>
      <small>Producer ${escapeHtml(report.producer_identity_id)} · ${link(report.href, 'Diagnostic Evidence')}</small>
      ${typeof report.fetched === 'boolean' ? `<small>Materialization: ${report.fetched ? 'reported fetched' : 'reported already present'}</small>` : ''}
      ${report.archive_sha256 ? `<small>Reported archive SHA256 ${escapeHtml(report.archive_sha256)}</small>` : ''}
      ${report.repository_source ? `<small>Repository ${escapeHtml(report.repository_source)}</small>` : ''}
      ${report.failure_code ? `<small>Failure ${escapeHtml(report.failure_code)} · inspect canonical scope, repository identity and preflight before retrying the governed installer.</small>` : ''}
    </div>`).join('') : '<p>No installer diagnostics recorded. Inspect the canonical Plan and Evidence; do not infer success from an absent report.</p>'}`;
}
