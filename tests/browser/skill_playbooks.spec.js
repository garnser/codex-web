const { test, expect } = require('@playwright/test');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { resolveAction } = require('./action_confirmation_helpers');

// Chromium talks to production routers and fresh SQLite-backed services. Route
// interception below is limited to holding actual HTTP responses for race tests.
const root = path.resolve(__dirname, '../..');
let service, base, metadata, diagnostics;
test.describe.configure({ timeout: 90_000 });

test.beforeEach(async ({ page }, testInfo) => {
  diagnostics = '';
  const localPython = path.join(root, '.venv/bin/python');
  const python = fs.existsSync(localPython) ? localPython : (process.env.PYTHON || 'python3');
  const env = Object.fromEntries(['PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'VIRTUAL_ENV', 'PYTHONPATH']
    .filter(key => process.env[key]).map(key => [key, process.env[key]]));
  const fixtureArgs = ['tests/browser/fixtures/skill_playbooks_server.py'];
  if (testInfo.title.includes('approval checkpoint')) fixtureArgs.push('--approval');
  if (testInfo.title.includes('policy checked deviation')) fixtureArgs.push('--deviation');
  service = spawn(python, fixtureArgs, {
    cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'],
  });
  service.stderr.on('data', chunk => { diagnostics += chunk; });
  const port = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Canonical fixture startup timed out: ${diagnostics}`)), 30_000);
    let output = '';
    service.once('error', error => { clearTimeout(timeout); reject(error); });
    service.once('exit', code => { clearTimeout(timeout); reject(new Error(`Canonical fixture exited ${code}: ${diagnostics}`)); });
    service.stdout.on('data', chunk => {
      output += chunk;
      const ready = output.match(/\{"port":\s*(\d+)\}/);
      if (ready) { clearTimeout(timeout); resolve(Number(ready[1])); }
    });
  });
  base = `http://127.0.0.1:${port}`;
  await expect.poll(async () => {
    try { return (await page.request.get(`${base}/fixture/metadata`)).ok(); }
    catch { return false; }
  }).toBe(true);
  metadata = await (await page.request.get(`${base}/fixture/metadata`)).json();
  await page.goto(`${base}/projects/home/skills`);
  await expect(page.getByRole('heading', { name: 'Skills', exact: true })).toBeVisible();
});

test.afterEach(async () => {
  if (!service || service.exitCode !== null) return;
  const stopped = new Promise(resolve => service.once('exit', resolve));
  service.kill('SIGTERM');
  await stopped;
});

async function selected(page) {
  await page.getByRole('button', { name: /Controlled GitLab/ }).click();
  await expect(page.locator('[data-skill-detail]')).toContainText(metadata.pin.record_id);
}

async function canonicalSkill(page) {
  const response = await page.request.get(`${base}/api/skills/${metadata.skill_id}`);
  expect(response.ok(), await response.text()).toBe(true);
  return (await response.json()).item;
}

async function tenant(page, id) {
  await page.request.post(`${base}/fixture/tenant/${id}`);
  const actor = await (await page.request.get(`${base}/api/identity/me`)).json();
  await page.evaluate(actor => window.dispatchEvent(new CustomEvent('codex:identity-state-rendered', { detail: { actor } })), actor);
}

async function project(page, id) {
  await page.evaluate(projectId => {
    history.replaceState({}, '', `/projects/${projectId}/skills`);
    document.body.dataset.projectId = projectId;
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId } }));
  }, id);
}

test('structured authoring preserves canonical rejection, scans exact revision and publishes with history', async ({ page }) => {
  await selected(page);
  await page.locator('[data-skill-edit]').click();
  const form = page.locator('[data-skill-editor]');
  const description = form.locator('[data-procedure-path="steps.0.description"] [data-procedure-value]');
  await expect(description).toHaveValue('Controlled GitLab-style workflow qualification');
  for (const name of ['worker', 'judgment']) {
    const branch = form.locator(`[data-procedure-path="steps.0.${name}"]`);
    await expect(branch.locator(':scope > label > [data-procedure-optional]')).not.toBeChecked();
    const controls = branch.locator('[data-procedure-value]');
    expect(await controls.count()).toBeGreaterThan(0);
    expect(await controls.evaluateAll(fields => fields.every(field => field.disabled))).toBe(true);
  }
  await expect(form.locator('[data-procedure-path="steps.0.action.provider_type"] [data-procedure-value]')).toBeEnabled();
  await description.fill('Operator reviewed backup evidence before bounded maintenance');
  await form.locator('[data-field=reason]').fill('Review bounded backup playbook');
  const timeout = form.locator('[data-procedure-path="steps.0.timeout_seconds"] [data-procedure-value]');
  await timeout.fill('21');
  await form.locator('button[type=submit]').click();
  await expect(form.locator('[data-editor-result]')).toContainText(/timeout|procedure_reference_invalid/i);
  await expect(description).toHaveValue('Operator reviewed backup evidence before bounded maintenance');
  await expect(form.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  expect((await canonicalSkill(page)).recordId).toBe(metadata.pin.record_id);
  page.once('dialog', dialog => dialog.dismiss());
  await form.locator('[data-editor-cancel]').click();
  await expect(description).toHaveValue('Operator reviewed backup evidence before bounded maintenance');
  await timeout.fill('10');
  await form.locator('button[type=submit]').click();
  await expect.poll(async () => (await canonicalSkill(page)).revision).toBe(2);
  const draft = await canonicalSkill(page);
  expect(draft.revision).toBe(2);
  expect(draft.definitionLifecycle).toBe('draft');
  expect(draft.skill.operational_procedure.steps[0].description).toBe('Operator reviewed backup evidence before bounded maintenance');
  const blocked = await page.request.post(`${base}/api/skills/${metadata.skill_id}/revisions/${draft.recordId}/publish`, { data: {} });
  expect(blocked.status()).toBe(409);
  expect((await canonicalSkill(page)).definitionLifecycle).toBe('draft');
  await page.reload();
  await page.getByRole('button', { name: /Controlled GitLab/ }).click();
  await page.getByRole('button', { name: 'Scan now', exact: true }).click();
  await expect.poll(async () => (await canonicalSkill(page)).security.status).toBe('passed');
  await expect(page.locator('[data-skill-detail] .skill-status').filter({ hasText: /^passed$/ })).toBeVisible();
  await page.locator('[data-procedure-validate]').click();
  await expect.poll(async () => (await canonicalSkill(page)).definitionLifecycle).toBe('validated');
  await page.reload();
  await page.getByRole('button', { name: /Controlled GitLab/ }).click();
  await expect(page.locator('[data-skill-detail]')).toContainText(draft.recordId);
  await expect(page.locator('[data-procedure-summary]')).toContainText('Inactive');
  await expect(page.locator('[data-skill-publish]')).toBeVisible();
  const publication = page.waitForRequest(request => request.method() === 'POST'
    && request.url().endsWith(`/api/skills/${metadata.skill_id}/revisions/${draft.recordId}/publish`));
  await page.locator('[data-skill-publish]').click();
  await resolveAction(page);
  expect((await publication).postDataJSON().expected_active_revision).toBe(1);
  await expect.poll(async () => (await canonicalSkill(page)).definitionLifecycle).toBe('published');
  const history = await (await page.request.get(`${base}/api/skills/${metadata.skill_id}/revisions`)).json();
  expect(history.items.map(item => item.revision).sort()).toEqual([1, 2]);
  expect(history.items.find(item => item.revision === 1).checksum).toBe(metadata.pin.checksum);
  await page.locator('[data-skill-edit]').click();
  await expect(form.locator('[data-procedure-path="steps.0.description"] [data-procedure-value]')).toHaveValue('Operator reviewed backup evidence before bounded maintenance');
});

test('late real catalog response cannot restore prior Project after A to B to A', async ({ page }) => {
  await selected(page);
  await page.addInitScript(() => {
    const original = fetch;
    window.fetch = (url, options = {}) => original(url, { ...options, signal: undefined });
  });
  let release, requested;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { requested = resolve; });
  let first = true;
  await page.route('**/api/skills?**', async route => {
    const response = await route.fetch();
    if (first) { first = false; requested(); await held; }
    await route.fulfill({ response });
  });
  await page.locator('[data-skill-refresh]').click();
  await started;
  await project(page, 'other');
  await project(page, 'home');
  await page.locator('[data-skill-new]').click();
  await page.locator('[data-field=name]').fill('New unsaved Project A draft');
  release();
  await expect(page.locator('[data-field=name]')).toHaveValue('New unsaved Project A draft');
  await expect(page.locator('[data-skill-editor] [data-dirty-editor-status]')).toHaveText('Unsaved changes');
  const storage = await page.evaluate(() => JSON.stringify([Object.values(localStorage), Object.values(sessionStorage)]));
  expect(storage).not.toContain('New unsaved Project A draft');
});

async function previewInputs(page, overrides = {}) {
  const panel = page.locator('[data-procedure-preview]');
  const values = { binding_id: metadata.binding_id, task_kind: metadata.task_kind,
    resource_ids: metadata.resource_id, environment: 'staging', operation: metadata.operation,
    execution_id: 'fixture-execution', ...overrides };
  for (const [key, value] of Object.entries(values)) await panel.locator(`[data-field="${key}"]`).fill(value);
  return panel;
}

test('canonical applicability exposes exact selected and rejected candidates, stale inputs and tenant denial', async ({ page }) => {
  const panel = await previewInputs(page);
  const result = panel.locator('[data-procedure-preview-result]');
  await panel.getByRole('button', { name: 'Preview applicability' }).click();
  await expect(result).toContainText('Procedure discovery · matched');
  await expect(result).toContainText(metadata.pin.record_id);
  await expect(result).toContainText(metadata.pin.checksum);
  await expect(result).toContainText('canonical:role-authority');
  await expect(result).toContainText('worker:');
  await expect(result).toContainText('mutation not authorized');
  await expect(result).toContainText('production-only@1: rejected');
  await expect(result).toContainText('environment_does_not_match');
  await panel.locator('[data-field=task_kind]').fill('unrelated.incident');
  await panel.getByRole('button', { name: 'Preview applicability' }).click();
  // A mismatched task cannot evade a procedure governing this same operation.
  await expect(result).toContainText('task unrelated.incident');
  await expect(result).toContainText('Procedure discovery · matched');
  await panel.locator('[data-field=task_kind]').fill(metadata.task_kind);
  await panel.locator('[data-field=environment]').fill('production');
  await panel.getByRole('button', { name: 'Preview applicability' }).click();
  await expect(result).toContainText('procedure_preview_environment_conflict');
  await expect(result).toContainText('canonical environment staging');
  await expect(result).toContainText('Selected: none');
  await tenant(page, 'foreign');
  await previewInputs(page);
  await panel.getByRole('button', { name: 'Preview applicability' }).click();
  await expect(result).toContainText('procedure_preview_denied');
  await expect(result).not.toContainText(metadata.pin.record_id);
  const denied = await page.request.get(`${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure`);
  expect(denied.status()).toBe(409);
  expect((await denied.json()).detail.code).toBeTruthy();
});

test('matching instructions do not override canonical role policy or permit publication', async ({ page }) => {
  const panel = await previewInputs(page, { execution_id: '' });
  await tenant(page, 'member');
  await previewInputs(page, { execution_id: '' });
  const canonicalDecision = page.waitForResponse(response => response.url().endsWith('/api/skills/procedure-applicability'));
  await panel.getByRole('button', { name: 'Preview applicability' }).click();
  const authority = (await (await canonicalDecision).json()).authority;
  const result = panel.locator('[data-procedure-preview-result]');
  await expect(result).toContainText('canonical:role-authority');
  await expect(result).toContainText('blocks');
  expect(authority.outcome).toBe('deny');
  await expect(result).toContainText(authority.reason);
  const denied = await page.request.post(`${base}/api/skills/${metadata.skill_id}/revisions/${metadata.pin.record_id}/publish`, { data: {} });
  expect(denied.status()).toBe(403);
  const scope = await (await page.request.get(`${base}/api/identity/me`)).json();
  expect(scope.identity_id).toBe('limited-reader');
});

for (const failure of [false, true]) {
  test(`schema ${failure ? 'failure' : 'success'} after typing preserves dirty values and cancelled navigation`, async ({ page }) => {
    let release, requested;
    const held = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { requested = resolve; });
    await page.route('**/api/skills/procedure-schema', async route => {
      const response = await route.fetch();
      requested(); await held;
      if (failure) await route.fulfill({ status: 503, json: { detail: 'Controlled schema transport unavailable' } });
      else await route.fulfill({ response });
    });
    await page.locator('[data-skill-new]').click();
    await started;
    const form = page.locator('[data-skill-editor]');
    await form.locator('[data-field=name]').fill('Typed before schema arrived');
    await expect(form.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
    release();
    if (failure) await expect(form.locator('[data-procedure-editor]')).toContainText('schema unavailable');
    else await expect(form.locator('[data-procedure-enabled]')).toBeVisible();
    await expect(form.locator('[data-field=name]')).toHaveValue('Typed before schema arrived');
    await expect(form.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
    page.once('dialog', dialog => dialog.dismiss());
    await form.locator('[data-editor-cancel]').click();
    await expect(form.locator('[data-field=name]')).toHaveValue('Typed before schema arrived');
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('[data-skill-new]').click();
    await expect(form.locator('[data-field=name]')).toHaveValue('Typed before schema arrived');
  });
}

test('untouched schema insertion remains clean and a late prior-scope schema cannot install an editor', async ({ page }) => {
  await page.locator('[data-skill-new]').click();
  await expect(page.locator('[data-procedure-enabled]')).toBeVisible();
  await expect(page.locator('[data-skill-editor] [data-dirty-editor-status]')).toHaveText('No unsaved changes');
  await page.locator('[data-editor-cancel]').click();
  let release, requested;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { requested = resolve; });
  await page.route('**/api/skills/procedure-schema', async route => {
    const response = await route.fetch(); requested(); await held; await route.fulfill({ response });
  });
  await page.locator('[data-skill-new]').click();
  await started;
  await project(page, 'other');
  await tenant(page, 'foreign');
  release();
  await expect(page.locator('[data-skill-editor]')).toHaveCount(0);
  await expect(page.locator('[data-procedure-enabled]')).toHaveCount(0);
});

test('tenant change fences a held real schema response independently of Project navigation', async ({ page }) => {
  let release, requested;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { requested = resolve; });
  await page.route('**/api/skills/procedure-schema', async route => {
    const response = await route.fetch(); requested(); await held; await route.fulfill({ response });
  });
  await page.locator('[data-skill-new]').click();
  await started;
  await tenant(page, 'foreign');
  release();
  await expect(page.locator('[data-skill-editor]')).toHaveCount(0);
  await expect(page.locator('[data-procedure-enabled]')).toHaveCount(0);
  await expect(page.locator('[data-skill-list]')).not.toContainText('Controlled GitLab');
});

for (const scope of ['Project', 'tenant']) {
  test(`${scope} change fences held real applicability response`, async ({ page }) => {
    const panel = await previewInputs(page);
    let release, requested;
    const held = new Promise(resolve => { release = resolve; });
    const started = new Promise(resolve => { requested = resolve; });
    await page.route('**/api/skills/procedure-applicability', async route => {
      const response = await route.fetch(); requested(); await held; await route.fulfill({ response });
    });
    await panel.getByRole('button', { name: 'Preview applicability' }).click();
    await started;
    if (scope === 'Project') {
      await project(page, 'other'); await project(page, 'home');
    } else {
      await tenant(page, 'foreign');
    }
    release();
    const result = panel.locator('[data-procedure-preview-result]');
    await expect(result).toContainText('Scope changed');
    await expect(result).not.toContainText(metadata.pin.record_id);
  });
}

async function runPanel(page) {
  const run = page.locator(`[data-procedure-run="${metadata.assignment_id}"]`);
  await expect(run).toHaveCount(1);
  await run.evaluate(node => { node.closest('details').open = true; });
  return run;
}

async function advance(page, run) {
  await run.locator('[data-procedure-action=advance]').click();
  await resolveAction(page);
  await expect(run.locator('[data-procedure-run-status]')).toContainText('Canonical result loaded', { timeout: 30_000 });
}

test('unknown provider outcome retains pinned checkpoint and prevents duplicate effects or premature learning', async ({ page }) => {
  const run = await runPanel(page);
  await expect(run.locator('[data-procedure-checkpoint]')).toContainText(metadata.pin.checksum);
  await advance(page, run); // canonical backup ActionIntent and Evidence
  await advance(page, run); // bounded isolated diagnostic
  await page.request.post(`${base}/fixture/provider/unknown`);
  await advance(page, run); // controlled effect with lost response
  await expect(run.locator('[data-procedure-checkpoint]')).toContainText('reconciliation_required');
  await expect(run.locator('[data-procedure-checkpoint]')).toContainText('Unknown outcome');
  const endpoint = `${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure`;
  const before = (await (await page.request.get(endpoint)).json()).procedure_checkpoint;
  expect(before.definition.record_id).toBe(metadata.pin.record_id);
  expect(before.steps.change.action_intent_id).toBeTruthy();
  await advance(page, run);
  const after = (await (await page.request.get(endpoint)).json()).procedure_checkpoint;
  expect(after.steps.change.action_intent_id).toBe(before.steps.change.action_intent_id);
  expect(after.steps.change.attempts).toBe(before.steps.change.attempts);
  await run.locator('[data-procedure-action=outcome-draft]').click();
  await resolveAction(page);
  await expect(run.locator('[data-procedure-outcome]')).toContainText('Review required');
  expect((await canonicalSkill(page)).revision).toBe(1);
  const history = await (await page.request.get(`${base}/api/action-intents/${before.steps.change.action_intent_id}/history`)).json();
  expect(history.intent.status).toBe('uncertain');
});

test('verified execution derives visible inactive review draft with evidence and canonical diff', async ({ page }) => {
  const run = await runPanel(page);
  // The engine also durably checkpoints conditional recovery steps as skipped;
  // completion is its canonical terminal result, not a browser step counter.
  for (let step = 0; step < 8; step++) {
    const status = (await (await page.request.get(`${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure`)).json()).procedure_checkpoint.status;
    if (status === 'completed') break;
    await advance(page, run);
  }
  await expect(run.locator('[data-procedure-checkpoint]')).toContainText('Pinned playbook · completed');
  await run.locator('[data-procedure-action=outcome]').click();
  await expect(run.locator('[data-procedure-outcome]')).toContainText('Verified outcome review');
  await expect(run.locator('[data-procedure-outcome]')).toContainText(metadata.pin.record_id);
  const startFields = run.locator('[data-procedure-start-form]');
  const deviationFields = run.locator('[data-procedure-deviation]');
  const draftFields = run.locator('[data-procedure-draft-fields]');
  await startFields.locator('[name=input_snapshot_id]').fill('Unsent start snapshot');
  await deviationFields.locator('[name=reason]').fill('Unsent independent deviation');
  let release, requested;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { requested = resolve; });
  await page.route('**/procedure/outcome-draft', async route => {
    const response = await route.fetch(); requested(); await held; await route.fulfill({ response });
  });
  await run.locator('[data-procedure-action=outcome-draft]').click();
  await resolveAction(page);
  await started;
  await draftFields.locator('[data-procedure-draft-id]').fill('newer-unsent-draft-id');
  release();
  await expect(run.locator('[data-procedure-outcome]')).toContainText('Inactive draft');
  await expect(run.locator('[data-procedure-outcome]')).toContainText('publication required');
  await expect(startFields.locator('[name=input_snapshot_id]')).toHaveValue('Unsent start snapshot');
  await expect(deviationFields.locator('[name=reason]')).toHaveValue('Unsent independent deviation');
  await expect(draftFields.locator('[data-procedure-draft-id]')).toHaveValue('newer-unsent-draft-id');
  for (const group of [startFields, deviationFields, draftFields]) await expect(group.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  const draft = await canonicalSkill(page);
  expect(draft.revision).toBe(2);
  expect(draft.definitionLifecycle).toBe('draft');
  expect(draft.skill.provenance.evidence_ids.length).toBeGreaterThan(0);
  const outcome = await (await page.request.get(`${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure/outcome`)).json();
  expect(outcome.drafts[0].draft.recordId).toBe(draft.recordId);
  expect(outcome.drafts[0].diff).toContain(metadata.pin.record_id);
  const pin = await (await page.request.get(`${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure`)).json();
  expect(pin.procedure_checkpoint.definition.record_id).toBe(metadata.pin.record_id);
  page.once('dialog', dialog => dialog.accept());
  await run.locator('[data-procedure-outcome]').getByRole('link', { name: `View skill: ${metadata.skill_id} r2` }).click();
  await expect(page.locator('[data-skill-detail]')).toContainText(draft.recordId);
  await page.getByRole('button', { name: 'Scan now', exact: true }).click();
  await expect(page.locator('[data-skill-detail] .skill-status').filter({ hasText: /^passed$/ })).toBeVisible();
  await page.locator('[data-skill-publish]').click();
  await resolveAction(page);
  await expect.poll(async () => (await canonicalSkill(page)).definitionLifecycle).toBe('published');
  const retained = await (await page.request.get(`${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure`)).json();
  expect(retained.procedure_checkpoint.definition.record_id).toBe(metadata.pin.record_id);
});

test('rollback security denial exposes its retained inactive canonical revision for scan and review', async ({ page }) => {
  await selected(page);
  await page.locator('[data-skill-rollback]').click();
  await resolveAction(page);
  const result = page.locator('[data-skill-action-result]');
  await expect(result).toContainText('Canonical retained revision');
  await expect(result).toContainText('inactive');
  await expect(result.getByRole('link', { name: 'View skill: Review r2' })).toBeVisible();
  const draft = await canonicalSkill(page);
  expect(draft.revision).toBe(2);
  expect(draft.definitionLifecycle).toBe('draft');
  const original = await (await page.request.get(`${base}/api/skills/${metadata.skill_id}?revision=1`)).json();
  expect(original.item.recordId).toBe(metadata.pin.record_id);
  expect(original.item.definitionLifecycle).toBe('published');
  await result.getByRole('link', { name: 'View skill: Review r2' }).click();
  await expect(page.locator('[data-skill-detail]')).toContainText(draft.recordId);
  await expect(page.locator('[data-skill-publish]')).toBeVisible();
  await expect(page.locator('[data-procedure-summary]')).toContainText('Inactive draft');
});

test('Resource facts change canonical selection and block advancing an existing stale pinned context', async ({ page }) => {
  const panel = await previewInputs(page);
  await panel.getByRole('button', { name: 'Preview applicability' }).click();
  await expect(panel.locator('[data-procedure-preview-result]')).toContainText('controlled-gitlab@1: selected');
  const changed = await page.request.patch(`${base}/api/resources/${metadata.resource_id}`, {
    data: { operational_context: { environment: 'production' } },
  });
  expect(changed.ok(), await changed.text()).toBe(true);
  await panel.getByRole('button', { name: 'Preview applicability' }).click();
  await expect(panel.locator('[data-procedure-preview-result]')).toContainText('procedure_preview_environment_conflict');
  await panel.locator('[data-field=environment]').fill('production');
  await panel.getByRole('button', { name: 'Preview applicability' }).click();
  await expect(panel.locator('[data-procedure-preview-result]')).toContainText('production-only@1: selected');
  await expect(panel.locator('[data-procedure-preview-result]')).toContainText('controlled-gitlab@1: rejected');
  const run = await runPanel(page);
  await run.locator('[data-procedure-action=advance]').click();
  await resolveAction(page);
  await expect(run.locator('[data-procedure-checkpoint]')).toContainText('recovery_required');
  const checkpoint = await (await page.request.get(`${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure`)).json();
  expect(checkpoint.procedure_checkpoint.definition.record_id).toBe(metadata.pin.record_id);
  expect(checkpoint.procedure_checkpoint.steps.backup.action_intent_id).toBeNull();
});

test('approval checkpoint exposes its exact canonical request and repeated resume cannot invent permission', async ({ page }) => {
  const run = await runPanel(page);
  await advance(page, run);
  await advance(page, run);
  await advance(page, run);
  const checkpoint = run.locator('[data-procedure-checkpoint]');
  await expect(checkpoint).toContainText('Pinned playbook · awaiting_approval');
  await expect(checkpoint).toContainText('Awaiting exact canonical approval');
  const endpoint = `${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure`;
  const before = (await (await page.request.get(endpoint)).json()).procedure_checkpoint;
  expect(before.steps.change.approval_id).toBeTruthy();
  await expect(checkpoint.getByRole('link', { name: `View approval: ${before.steps.change.approval_id}` })).toBeVisible();
  expect(before.steps.change.action_intent_id).toBeNull();
  await advance(page, run);
  const after = (await (await page.request.get(endpoint)).json()).procedure_checkpoint;
  expect(after.steps.change.approval_id).toBe(before.steps.change.approval_id);
  expect(after.steps.change.action_intent_id).toBeNull();
  expect(after.status).toBe('awaiting_approval');
});

test('policy checked deviation records real evidence and preserves a denied verification edit', async ({ page }) => {
  const run = await runPanel(page);
  await advance(page, run);
  await advance(page, run);
  await advance(page, run);
  const endpoint = `${base}/api/execution-workers/assignments/${metadata.assignment_id}/procedure`;
  const pin = (await (await page.request.get(endpoint)).json()).procedure_checkpoint;
  const evidence = pin.steps.disk.evidence.disk;
  const form = run.locator('[data-procedure-deviation]');
  const startFields = run.locator('[data-procedure-start-form]');
  const draftFields = run.locator('[data-procedure-draft-fields]');
  await startFields.locator('[name=input_snapshot_id]').fill('Unsent independent start');
  await draftFields.locator('[data-procedure-draft-id]').fill('unsent-independent-outcome');
  await form.locator('[name=step_id]').fill('optional-disk');
  await form.locator('[name=reason]').fill('Current isolated diagnostic evidence already covers this optional observation.');
  await form.locator('[name=evidence_ids]').fill(evidence);
  await form.getByRole('button', { name: 'Record policy checked deviation' }).click();
  await resolveAction(page);
  await expect(run.locator('[data-procedure-run-status]')).toContainText('Canonical result loaded');
  await expect(run.locator('[data-procedure-checkpoint]')).toContainText('optional-disk · skipped');
  const revised = (await (await page.request.get(endpoint)).json()).procedure_checkpoint;
  expect(revised.deviations[0].evidence_ids).toEqual([evidence]);
  expect(revised.deviations[0].authority_definition.record_id).toBeTruthy();
  await expect(form.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
  for (const group of [startFields, draftFields]) await expect(group.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  await run.locator('[data-procedure-action=inspect]').click();
  await expect(run.locator('[data-procedure-run-status]')).toContainText('Canonical result loaded');
  await expect(startFields.locator('[name=input_snapshot_id]')).toHaveValue('Unsent independent start');
  await expect(draftFields.locator('[data-procedure-draft-id]')).toHaveValue('unsent-independent-outcome');
  for (const group of [startFields, draftFields]) await expect(group.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('#refresh-execution-workers').click();
  await expect(startFields.locator('[name=input_snapshot_id]')).toHaveValue('Unsent independent start');
  await expect(draftFields.locator('[data-procedure-draft-id]')).toHaveValue('unsent-independent-outcome');
  await form.locator('[name=step_id]').fill('ready');
  await form.locator('[name=reason]').fill('Attempt to bypass mandatory verification must fail.');
  await form.getByRole('button', { name: 'Record policy checked deviation' }).click();
  await resolveAction(page);
  await expect(run.locator('[data-procedure-run-status]')).toContainText('Operation blocked');
  await expect(form.locator('[name=reason]')).toHaveValue('Attempt to bypass mandatory verification must fail.');
  await expect(form.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  const retained = (await (await page.request.get(endpoint)).json()).procedure_checkpoint;
  expect(retained.steps.ready.status).toBe('pending');
  expect(retained.deviations).toHaveLength(1);
});

test('late canonical security policy response cannot overwrite a newly selected tenant policy', async ({ page }) => {
  await expect(page.locator('[data-policy-score]')).toHaveValue('37');
  let release, requested, first = true;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { requested = resolve; });
  await page.route('**/api/skill-security/policy', async route => {
    const response = await route.fetch();
    if (first) { first = false; requested(); await held; }
    await route.fulfill({ response });
  });
  await page.reload();
  await started;
  await tenant(page, 'foreign');
  const current = await (await page.request.get(`${base}/api/skill-security/policy`)).json();
  await expect(page.locator('[data-policy-score]')).toHaveValue(String(current.policy.max_risk_score));
  release();
  await expect(page.locator('[data-policy-score]')).toHaveValue(String(current.policy.max_risk_score));
  expect(current.policy.max_risk_score).not.toBe(37);
});

test('held canonical detail selection hides prior actions until the current exact revision can be scanned', async ({ page }) => {
  await selected(page);
  await page.locator('[data-skill-edit]').click();
  const form = page.locator('[data-skill-editor]');
  await form.locator('[data-field=description]').fill('Canonical selection race qualification');
  await form.locator('[data-field=reason]').fill('Qualify pending exact revision selection');
  await form.locator('button[type=submit]').click();
  await expect(page.locator('[data-skill-publish]')).toBeVisible();
  const draft = await canonicalSkill(page);
  expect(draft.revision).toBe(2);
  let release, requested;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { requested = resolve; });
  const paths = new Set();
  await page.route('**/api/skills/controlled-gitlab**', async route => {
    const response = await route.fetch();
    if (route.request().method() === 'GET') {
      paths.add(new URL(route.request().url()).pathname);
      if (paths.size === 3) requested();
      await held;
    }
    await route.fulfill({ response });
  });
  await page.getByRole('button', { name: /Controlled GitLab/ }).click();
  await started;
  await expect(page.locator('[data-skill-detail]')).toContainText('Loading');
  await expect(page.locator('[data-skill-detail] [data-skill-scan]')).toHaveCount(0);
  release();
  await expect(page.locator('[data-skill-detail]')).toContainText(draft.recordId);
  await page.getByRole('button', { name: 'Scan now', exact: true }).click();
  await expect.poll(async () => (await canonicalSkill(page)).security.status).toBe('passed');
  await expect(page.locator('[data-skill-detail] .skill-status').filter({ hasText: /^passed$/ })).toBeVisible();
});

test('late initial canonical catalogs cannot override an operator selection with the URL revision', async ({ page }) => {
  let release, requested;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { requested = resolve; });
  await page.route('**/api/skill-security/policy', async route => {
    const response = await route.fetch(); requested(); await held; await route.fulfill({ response });
  });
  const initialSelections = [];
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/skills/controlled-gitlab') initialSelections.push(request.url());
  });
  await page.goto(`${base}/projects/home/skills?skill_id=${metadata.skill_id}&skill_revision=1`);
  await started;
  await page.getByRole('button', { name: /Production only/ }).click();
  const detail = page.locator('[data-skill-detail]');
  await expect(detail.getByRole('heading', { name: 'Production only', exact: true })).toBeVisible();
  const completedPolicy = page.waitForResponse(response => response.url().endsWith('/api/skill-security/policy'));
  release();
  await (await completedPolicy).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(initialSelections).toEqual([]);
  await expect(detail.getByRole('heading', { name: 'Production only', exact: true })).toBeVisible();
});
