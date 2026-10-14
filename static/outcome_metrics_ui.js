import { request } from './api_client.js';

const esc = (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
const measured = (value) => value === null || value === undefined ? 'Missing' : esc(value);

export function installOutcomeMetrics(host) {
  const panel = document.createElement('details');
  panel.className = 'outcome-metrics';
  panel.innerHTML = `<summary>Human acceptance and review effort</summary>
    <p>Optional human observations measure quality. They do not approve actions or prove technical completion.</p>
    <p class="outcome-scope" aria-live="polite"></p>
    <form class="outcome-window">
      <label>Enrollment start <input name="start" type="datetime-local" required></label>
      <label>Enrollment end <input name="end" type="datetime-local" required></label>
      <button type="submit">Inspect outcomes</button>
      <button type="button" class="outcome-publish" disabled>Record canonical Metrics</button>
    </form>
    <p class="outcome-status" role="status"></p><div class="outcome-results"></div>
    <form class="outcome-feedback" hidden>
      <h3>Optional outcome observation</h3>
      <label>Runtime attempt <select name="usage" required></select></label>
      <label>Workload <input name="workload" maxlength="64" pattern="[a-z0-9][a-z0-9._-]*" required></label>
      <label>Outcome <select name="outcome"><option>pending</option><option>accepted</option><option>rejected</option><option>unknown</option><option>reopened</option></select></label>
      <label>Correction needed <select name="correction"><option value="">Unmeasured</option><option value="true">Yes</option><option value="false">No</option></select></label>
      <label>Review count <input name="reviews" type="number" min="0" max="10000"></label>
      <label>Intervention count <input name="interventions" type="number" min="0" max="10000"></label>
      <label>Explicit review time (seconds) <input name="seconds" type="number" min="0" max="604800"></label>
      <label>Exact template Artifact ID (optional) <input name="template" maxlength="128"></label>
      <label><input name="replace" type="checkbox">Supersede the displayed current observations</label>
      <p class="outcome-target"></p><button type="submit">Save review Evidence</button>
    </form>`;
  host.appendChild(panel);
  let project = null, generation = 0, controller = null, tasks = [], draftKey = null;
  const windowForm = panel.querySelector('.outcome-window');
  const feedback = panel.querySelector('.outcome-feedback');
  const status = panel.querySelector('.outcome-status');
  const publish = panel.querySelector('.outcome-publish');
  function localTime(date) {
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  windowForm.elements.start.value = localTime(new Date(Date.now() - 7 * 86400000));
  windowForm.elements.end.value = localTime(new Date(Date.now() - 60000));
  function windowPayload() {
    return { start_at: new Date(windowForm.elements.start.value).getTime() / 1000,
      end_at: new Date(windowForm.elements.end.value).getTime() / 1000 };
  }
  function target() {
    const task = tasks.find((row) => row.runtime_usage_ids.at(-1) === feedback.elements.usage.value);
    panel.querySelector('.outcome-target').textContent = task
      ? `Task: ${task.task_key}. Current observations: ${(task.feedback_head_ids || []).join(', ') || 'None'}.`
      : '';
  }
  feedback.elements.usage.addEventListener('change', target);
  feedback.addEventListener('input', () => { draftKey = null; });
  function render(data) {
    tasks = data.cohorts.flatMap((cohort) => cohort.tasks);
    panel.querySelector('.outcome-results').innerHTML = `<p>All eligible tasks: ${esc(data.accepted_count)} accepted / ${esc(data.eligible_count)}. Acceptance rate: ${measured(data.accepted_outcome_rate)}.</p><p>Definition: ${esc(data.formula_version)} · ${esc(data.status)}.
      Enrollment: ${esc(new Date(data.window.start_at * 1000).toISOString())} to ${esc(new Date(data.window.end_at * 1000).toISOString())}.
      Observed through: ${esc(new Date(data.window.as_of * 1000).toISOString())}.</p>
      ${data.cohorts.length ? data.cohorts.map((cohort) => `<section><h3>${esc(cohort.workload)}</h3>
      <p>Models: ${esc(cohort.model_ids.join(', ') || 'Unobserved')} · Template: ${esc(cohort.template_artifact_id || 'Unrecorded')} ${esc(cohort.template_revision || '')} · ${esc(cohort.status)}</p>
      <p>Accepted: ${esc(cohort.accepted_count)} / ${esc(cohort.eligible_count)} eligible tasks. Correction observations: ${esc(cohort.correction_observation_count)}.</p>
      <p>${esc(JSON.stringify(cohort.outcome_counts))}</p>
      <table><thead><tr><th>Measure</th><th>Value</th></tr></thead><tbody>${Object.entries(cohort.values).map(([key, value]) =>
        `<tr><td>${esc(key.replaceAll('_', ' '))}</td><td>${measured(value)}</td></tr>`).join('')}</tbody></table>
      ${cohort.tasks.map((task) => `<details><summary>${esc(task.task_key)} · ${esc(task.outcome)}</summary>
        <p>Runtime usage: ${esc(task.runtime_usage_ids.join(', '))}. Executions: ${esc(task.execution_ids.filter(Boolean).join(', '))}.</p>
        <p>Review Evidence: ${esc(task.feedback_history_ids.join(', ') || 'Missing')}. Runtime Evidence: ${esc(task.source_evidence_ids.join(', ') || 'Missing')}.</p>
        <p>${esc(task.limitation || '')}</p></details>`).join('')}</section>`).join('') : '<p>No eligible tasks in this window.</p>'}
      <ul>${data.limitations.map((text) => `<li>${esc(text)}</li>`).join('')}</ul>`;
    feedback.hidden = !tasks.length;
    feedback.elements.usage.innerHTML = tasks.map((task) => `<option value="${esc(task.runtime_usage_ids.at(-1))}">${esc(task.task_key)} · ${esc(task.outcome)}</option>`).join('');
    publish.disabled = !data.cohorts.length;
    target();
  }
  async function call(suffix, options = {}) {
    if (!project) return null;
    const currentProject = project, epoch = generation;
    controller?.abort(); controller = new AbortController();
    status.textContent = 'Loading…'; publish.disabled = true;
    const path = `/api/metrics/outcomes/projects/${encodeURIComponent(project)}${suffix}`;
    try {
      const data = await request(path, { ...options, signal: controller.signal });
      if (epoch !== generation || currentProject !== project) return null;
      status.textContent = '';
      return data;
    } catch (error) {
      if (epoch === generation && currentProject === project && error.name !== 'AbortError') {
        status.textContent = `Unavailable or denied: ${error.message}. Inspect scope and authentication before retrying.`;
      }
      return null;
    }
  }
  async function inspect() {
    const data = await call(`?${new URLSearchParams(windowPayload())}`);
    if (data) render(data);
  }
  windowForm.addEventListener('submit', (event) => { event.preventDefault(); void inspect(); });
  publish.addEventListener('click', async () => {
    const data = await call('/publish', { method: 'POST', body: JSON.stringify(windowPayload()) });
    if (data) {
      render(data.report);
      status.textContent = `Recorded ${data.observations.length} canonical Metric observations. Inspect their definitions and Evidence in Metrics.`;
      for (const observation of data.observations) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = `Inspect ${observation.metric_id} · revision ${observation.metric_revision ?? 'unavailable'}`;
        button.addEventListener('click', () => window.dispatchEvent(new CustomEvent('codex-open-metric', {
          detail: { metricId: observation.metric_id },
        })));
        panel.querySelector('.outcome-results').appendChild(button);
      }
    }
  });
  feedback.addEventListener('submit', async (event) => {
    event.preventDefault();
    const epoch = generation;
    const selected = tasks.find((row) => row.runtime_usage_ids.at(-1) === feedback.elements.usage.value);
    const number = (name) => feedback.elements[name].value === '' ? null : Number(feedback.elements[name].value);
    const data = await call('/feedback', { method: 'POST', body: JSON.stringify({
      runtime_usage_id: feedback.elements.usage.value, workload: feedback.elements.workload.value,
      outcome: feedback.elements.outcome.value, correction_required: feedback.elements.correction.value === ''
        ? null : feedback.elements.correction.value === 'true', review_count: number('reviews'),
      intervention_count: number('interventions'), review_seconds: number('seconds'),
      template_artifact_id: feedback.elements.template.value || null,
      supersedes_evidence_ids: feedback.elements.replace.checked ? selected?.feedback_head_ids || [] : [],
      idempotency_key: draftKey || (draftKey = crypto.randomUUID()),
    }) });
    if (data && epoch === generation) {
      draftKey = null;
      await inspect();
      if (epoch === generation) status.textContent = `Saved optional review ${data.item.id}.`;
    }
  });
  return { sync(nextProject) {
    nextProject = nextProject || '';
    if (nextProject === project) return;
    controller?.abort(); generation += 1; project = nextProject || ''; tasks = []; draftKey = null;
    feedback.reset(); feedback.hidden = true; publish.disabled = true;
    panel.querySelector('.outcome-results').replaceChildren(); status.textContent = '';
    panel.querySelector('.outcome-scope').textContent = project ? `Project: ${project}` : 'Select a Project for outcome metrics.';
    windowForm.querySelector('button[type=submit]').disabled = !project;
  }};
}
