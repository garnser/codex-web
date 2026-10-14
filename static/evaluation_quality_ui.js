import { request, apiBase } from './api_client.js';

const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');

function reportMarkup(data) {
  return `<p>Deterministic replay: <strong>${data.deterministic_passed ? 'passed' : 'failed'}</strong>. Semantic assessment: ${esc(data.status)}.</p>
    <p>${esc(data.limitations)}${data.truncated ? ' Only the newest 100 annotations are shown.' : ''}</p>
    ${(data.reports || []).map(report => `<article>
      <h4>${esc(report.status)} · ${esc(report.result.workload)}</h4>
      <p>Reviewer ${esc(report.reviewed_by)} · rubric ${esc(report.result.provenance.rubric.definition_id)} r${esc(report.result.provenance.rubric.revision)}
        · score ${report.result.score === null ? 'incomplete' : esc(report.result.score)} / threshold ${esc(report.result.minimum_score)}</p>
      <p>Rubric record ${esc(report.result.provenance.rubric.record_id)} · schema ${esc(report.result.provenance.rubric.definition_schema_version)} · checksum ${esc(report.result.provenance.rubric.checksum)}</p>
      <p><a href="${apiBase()}/api/evidence/${encodeURIComponent(report.evidence_id)}" target="_blank" rel="noreferrer">Annotation Evidence ${esc(report.evidence_id)}</a>
        · <a href="${apiBase()}/api/artifacts/${encodeURIComponent(report.pin.artifact_id)}" target="_blank" rel="noreferrer">Output Artifact</a></p>
      <p>Output revision ${esc(report.pin.artifact_revision)} · SHA256 ${esc(report.pin.artifact_sha256)} · run fingerprint ${esc(report.pin.run_sha256)}</p>
      <ul>${report.result.criteria.map(item => `<li>${esc(item.criterion_id)}: ${item.normalized_score === null ? 'unassessed' : esc(item.normalized_score)}
        ${item.passed === false ? ' · below criterion floor' : ''} — ${esc(item.finding || 'No annotation')}</li>`).join('')}</ul>
    </article>`).join('')}
    ${(data.invalidated || []).map(item => `<p>${esc(item.evidence_id)}: ${esc(item.reason)}</p>`).join('')}
    <label>Candidate annotation for comparison <select data-quality-candidate>${(data.reports || []).map(report =>
      `<option value="${esc(report.evidence_id)}">${esc(report.reviewed_by)} · ${esc(report.status)} · ${esc(report.evidence_id)}</option>`).join('')}</select></label>
    ${data.comparison ? `<p>Named baseline ${esc(data.comparison.baseline.provenance.annotation_ref)}:
      score delta ${data.comparison.score_delta === null ? 'incomplete' : esc(data.comparison.score_delta)}.</p>
      <ul>${Object.entries(data.comparison.criterion_deltas).map(([id, delta]) => `<li>${esc(id)} delta: ${delta === null ? 'incomplete' : esc(delta)}</li>`).join('')}</ul>
      ${data.comparison.baseline_evidence_id ? `<a href="${apiBase()}/api/evidence/${encodeURIComponent(data.comparison.baseline_evidence_id)}" target="_blank" rel="noreferrer">Baseline annotation Evidence</a>` : ''}` : ''}`;
}

export function installEvaluationQuality(host) {
  const dialog = document.createElement('dialog');
  dialog.className = 'evaluation-quality-dialog';
  dialog.setAttribute('aria-label', 'Human semantic evaluation');
  document.body.appendChild(dialog);
  let generation = 0;
  dialog.addEventListener('close', () => { generation += 1; });
  const style = document.createElement('style');
  style.textContent = `.evaluation-quality-dialog{width:min(760px,90vw);max-height:85vh;overflow:auto;box-sizing:border-box}
    .evaluation-quality-dialog label{display:block;margin:.7rem 0}.evaluation-quality-dialog select,
    .evaluation-quality-dialog textarea{display:block;max-width:100%;width:100%;box-sizing:border-box}
    .evaluation-quality-dialog p,.evaluation-quality-dialog li{overflow-wrap:anywhere}
    .evaluation-quality-dialog article,.evaluation-quality-dialog fieldset{margin:.7rem 0;padding:.6rem;border:1px solid #888}
    .evaluation-quality-dialog textarea{min-height:4rem}.evaluation-quality-dialog input{max-width:100%;box-sizing:border-box}`;
  document.head.appendChild(style);
  host.addEventListener('click', async event => {
    const button = event.target.closest('[data-evaluation-quality]');
    if (!button) return;
    if (dialog.open) dialog.close();
    const current = ++generation;
    const runId = button.dataset.evaluationQuality;
    const endpoint = `/api/evaluations/runs/${encodeURIComponent(runId)}/quality`;
    const active = () => current === generation && dialog.open && host.isConnected;
    dialog.innerHTML = '<h3>Human semantic evaluation</h3><button type="button" data-quality-close>Close</button><p role="status">Loading pinned review options…</p>';
    dialog.querySelector('[data-quality-close]').onclick = () => dialog.close();
    dialog.showModal();
    try {
      const [options, data] = await Promise.all([request(endpoint + '/options'), request(endpoint)]);
      if (!active()) return;
      dialog.innerHTML = `<h3>Human semantic evaluation</h3><button type="button" data-quality-close>Close</button>
        <p>Project ${esc(options.project_id)} · <a href="${apiBase()}/api/evaluations/runs/${encodeURIComponent(runId)}" target="_blank" rel="noreferrer">Exact replay run ${esc(runId)}</a></p>
        <p>Review the linked output separately. This form records manual findings; viewing it generates no output or model calls.
          Submission requires a human administrator with MFA. Unrated criteria stay inconclusive.</p>
        <div data-quality-reports>${reportMarkup(data)}</div>
        <form data-quality-form>
          <label>Exact output Artifact <select name="artifact" required>${options.artifacts.map((item, index) =>
            `<option value="${index}">${esc(item.name)} · ${esc(item.revision)}</option>`).join('')}</select></label>
          <label>Exact rubric revision <select name="rubric" required>${options.rubrics.map((item, index) =>
            `<option value="${index}">${esc(item.payload.title)} · ${esc(item.payload.workload)} · r${esc(item.reference.revision)} · ${esc(item.lifecycle)}</option>`).join('')}</select></label>
          <div data-quality-criteria></div>
          <button type="submit" ${!options.artifacts.length || !options.rubrics.length ? 'disabled' : ''}>Record manual annotation</button>
          ${!options.artifacts.length ? '<p>No active revision/digest-pinned output Artifact is linked by valid Evidence in this replay trace.</p>' : ''}
          ${!options.rubrics.length ? '<p>No scoped rubric revision exists. Create one in the Definition inspector before reviewing.</p>' : ''}
        </form>
        <label>Named baseline annotation Evidence ID <input data-quality-baseline maxlength="200"></label>
        <button type="button" data-quality-compare>Compare baseline</button>
        <p data-quality-status role="status" aria-live="polite"></p>`;
      dialog.querySelector('[data-quality-close]').onclick = () => dialog.close();
      const form = dialog.querySelector('form');
      const status = dialog.querySelector('[data-quality-status]');
      const renderCriteria = () => {
        const rubric = options.rubrics[Number(form.elements.rubric.value)];
        dialog.querySelector('[data-quality-criteria]').innerHTML = rubric ? rubric.payload.criteria.map((item, index) =>
          `<fieldset data-quality-criterion="${index}"><legend>${esc(item.id)} · weight ${esc(item.weight)}</legend>
            <p>${esc(item.description)} · normalized floor ${item.minimum_normalized_score === null ? 'none' : esc(item.minimum_normalized_score)}</p>
            <label>Rating (${esc(item.minimum)}–${esc(item.maximum)}), optional
              <input name="rating-${index}" type="number" step="any" min="${esc(item.minimum)}" max="${esc(item.maximum)}"></label>
            <label>Criterion finding <textarea name="finding-${index}" maxlength="2000"></textarea></label></fieldset>`).join('') : '';
      };
      form.elements.rubric.addEventListener('change', renderCriteria);
      renderCriteria();
      let lastBody = null, key = null;
      form.addEventListener('submit', async submit => {
        submit.preventDefault();
        const artifact = options.artifacts[Number(form.elements.artifact.value)];
        const rubric = options.rubrics[Number(form.elements.rubric.value)];
        const ratings = rubric.payload.criteria.flatMap((criterion, index) => {
          const raw = form.elements[`rating-${index}`].value;
          return raw === '' ? [] : [{ criterion_id: criterion.id, value: Number(raw), finding: form.elements[`finding-${index}`].value }];
        });
        const payload = { rubric: rubric.reference, artifact_id: artifact.id, artifact_revision: artifact.revision, ratings };
        const body = JSON.stringify(payload);
        if (body !== lastBody) { lastBody = body; key = crypto.randomUUID(); }
        const submitButton = form.querySelector('[type="submit"]');
        submitButton.disabled = true;
        status.textContent = 'Recording exact-revision human annotation…';
        try {
          await request(endpoint, { method: 'POST', body: JSON.stringify({ ...payload, idempotency_key: key }) });
          if (!active()) return;
          const refreshed = await request(endpoint);
          if (!active()) return;
          dialog.querySelector('[data-quality-reports]').innerHTML = reportMarkup(refreshed);
          status.textContent = 'Annotation retained as canonical Evidence. Deterministic replay results are unchanged.';
        } catch (error) { if (active()) status.textContent = error.message; }
        finally { if (active()) submitButton.disabled = false; }
      });
      dialog.querySelector('[data-quality-compare]').onclick = async () => {
        const baseline = dialog.querySelector('[data-quality-baseline]').value.trim();
        if (!baseline) { status.textContent = 'Enter an exact baseline annotation Evidence ID.'; return; }
        try {
          const candidate = dialog.querySelector('[data-quality-candidate]').value;
          const compared = await request(endpoint + '?baseline_evidence_id=' + encodeURIComponent(baseline)
            + '&candidate_evidence_id=' + encodeURIComponent(candidate));
          if (active()) dialog.querySelector('[data-quality-reports]').innerHTML = reportMarkup(compared);
        } catch (error) { if (active()) status.textContent = error.message; }
      };
    } catch (error) {
      if (active()) dialog.querySelector('[role="status"]').textContent = error.message;
    }
  });
}
