const { resolveAction } = require('./action_confirmation_helpers');
const { test, expect } = require('@playwright/test');

const skill = {
  skillId: 'release-check',
  revision: 2,
  recordId: 'skill-record-2',
  definitionLifecycle: 'published',
  checksum: 'abcdef1234567890',
  createdBy: 'admin',
  publishedBy: 'admin',
  skill: {
    name: 'Release Check',
    description: 'Verify a release deterministically.',
    instructions: 'Inspect candidate and record evidence.',
    owner_identity_id: 'admin',
    lifecycle: 'active',
    applicability_tags: ['release'],
    capability_tags: ['git'],
    required_provider_capabilities: ['git_operations'],
    required_worker_capabilities: ['git', 'command_execution'],
    input_expectations: ['candidate revision'],
    output_expectations: ['evidence summary'],
    provenance: { source_type: 'manual', source_ref: null, evidence_ids: [] },
    assets: [
      { path: 'references/release.md', kind: 'reference', security_class: 'untrusted_reference', context_mode: 'relevant', content: 'evidence' },
      { path: 'helpers/check.sh', kind: 'helper_script', security_class: 'executable_untrusted', context_mode: 'never', content: 'echo check' },
    ],
  },
};

test.beforeEach(async ({ page }) => {
  await page.route('**/api/skills?**', route => route.fulfill({ json: { items: [skill] } }));
  await page.route('**/api/agent-profiles?**', route => route.fulfill({ json: { items: [
    { profile_id: 'release-agent', revision: 4, name: 'Release Agent', lifecycle: 'active' },
  ] } }));
  await page.route('**/api/skills/release-check/revisions', route => route.fulfill({ json: { items: [
    { ...skill, revision: 1, recordId: 'skill-record-1', definitionLifecycle: 'superseded' },
    skill,
  ] } }));
  await page.route('**/api/skills/release-check/usage**', route => route.fulfill({ json: {
    items: [{ object_type: 'agent_profile', object_id: 'release-agent', revision: 4 }],
  } }));
  await page.route('**/api/skills/release-check?revision=**', route => {
    const url = new URL(route.request().url());
    const revision = Number(url.searchParams.get('revision'));
    route.fulfill({ json: { item: { ...skill, revision, recordId: `skill-record-${revision}`, definitionLifecycle: revision === 1 ? 'superseded' : 'published' } } });
  });
  await page.route('**/api/skills/release-check', route => route.fulfill({ json: { item: skill } }));
  await page.goto('http://127.0.0.1:18766/tests/browser/skill_library_fixture.html');
  await expect(page.getByRole('heading', { name: 'Skills' })).toBeVisible();
});

test('lists and inspects exact Skill revision and untrusted helper classification', async ({ page }) => {
  await expect(page.getByRole('button', { name: /Release Check/ })).toContainText('r2');
  await page.getByRole('button', { name: /Release Check/ }).click();
  await expect(page.locator('[data-skill-detail]')).toContainText('checksum abcdef1234567890');
  await expect(page.locator('[data-skill-detail]')).toContainText('helpers/check.sh');
  await expect(page.locator('[data-skill-detail]')).toContainText('executable_untrusted');
  await expect(page.locator('[data-skill-detail]')).toContainText('never auto-runs');
  await expect(page.locator('[data-skill-detail]')).toContainText('Release Agent');
});

test('revision selector keeps explicit historical pin visible', async ({ page }) => {
  await page.getByRole('button', { name: /Release Check/ }).click();
  await page.locator('[data-skill-revision]').selectOption('1');
  await expect(page.locator('[data-skill-detail]')).toContainText('superseded');
  await expect(page.locator('[data-skill-detail]')).toContainText('record skill-record-1');
});

test('import requires preview and makes draft semantics explicit', async ({ page }) => {
  await page.getByText('Import / promote').click();
  const confirm = page.locator('[data-skill-import-confirm]');
  await expect(confirm).toBeDisabled();
  await page.locator('[data-skill-import-json]').fill(JSON.stringify({
    format: 'codex-web-skill-bundle',
    version: '1.0',
    manifest: { skill_id: 'new-skill', name: 'New Skill', instructions: 'Do bounded work.', assets: [] },
    files: {},
  }));
  await page.locator('[data-skill-import-preview]').click();
  await expect(confirm).toBeEnabled();
  await expect(page.locator('[data-skill-import-result]')).toContainText('Publication: never automatic');
});

test('responsive layout collapses to one column', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const columns = await page.locator('.skill-layout').evaluate(node => getComputedStyle(node).gridTemplateColumns);
  expect(columns.trim().split(' ')).toHaveLength(1);
});

test('deep link opens the exact historical Skill revision and links its Agent consumer', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/skill_library_fixture.html?project=project-a&skill_id=release-check&skill_revision=1');
  await expect(page.locator('[data-skill-detail]')).toContainText('record skill-record-1');
  await expect(page.locator('[data-skill-detail]')).toContainText('superseded');
  await expect(page.getByRole('link', { name: 'View agent profile: release-agent' })).toHaveAttribute('href', '/projects/project-a/agent-profiles?consumer_type=agent_profile&consumer_id=release-agent');
});

test('missing deep-linked Skill reports failure without selecting an unrelated object', async ({ page }) => {
  await page.route('**/api/skills/missing**', route => route.fulfill({ status: 404, json: { detail: 'Skill not visible' } }));
  await page.goto('http://127.0.0.1:18766/tests/browser/skill_library_fixture.html?skill_id=missing');
  await expect(page.locator('[data-skill-detail]')).toContainText('Referenced Skill unavailable');
  await expect(page.locator('[data-skill-detail]')).not.toContainText('Release Check');
});


test('Skill archive reviews canonical exact-revision pins and blocks unavailable impact', async ({ page }) => {
  let unavailable = true; const writes = [];
  await page.route('**/api/skills/release-check/usage**', route => route.fulfill(unavailable
    ? { status: 503, json: { detail: 'Usage unavailable' } }
    : { json: { items: [{ object_id: 'release-agent', revision: 4 }] } }));
  // Select with usable initial impact, then make the action-time refresh fail.
  unavailable = false;
  await page.getByRole('button', { name: /Release Check/ }).click();
  await expect(page.locator('[data-skill-archive]')).toBeVisible();
  unavailable = true;
  await page.route('**/api/skills/release-check/archive', route => {
    writes.push(route.request().postDataJSON()); return route.fulfill({ json: { item: skill } });
  });
  await page.locator('[data-skill-archive]').click();
  await expect(page.locator('[data-skill-action-result]')).toContainText('Skill action blocked');
  expect(writes).toHaveLength(0);
  unavailable = false;
  await page.locator('[data-skill-archive]').click();
  const review = await resolveAction(page, false);
  expect(review).toContain('release-check r2'); expect(review).toContain('release-agent r4');
  expect(review).toContain('Restore is available'); expect(writes).toHaveLength(0);
  await page.locator('[data-skill-archive]').click(); await resolveAction(page);
  await expect.poll(() => writes.length).toBe(1);
});

test('Skill draft preserves invalid and rejected edits and requires explicit discard', async ({ page }) => {
  let writes = 0;
  await page.route('**/api/skills/release-check', route => {
    if (route.request().method() === 'PATCH') { writes++; return route.fulfill({ status: 422, json: { detail: 'Draft validation rejected' } }); }
    return route.fulfill({ json: { item: skill } });
  });
  await page.getByRole('button', { name: /Release Check/ }).click();
  await page.locator('[data-skill-edit]').click();
  const form = page.locator('[data-skill-editor]');
  await form.locator('[data-field=reason]').fill('Revise instructions');
  await form.locator('[data-field=assets]').fill('{ invalid');
  await form.locator('button[type=submit]').click();
  await expect(form.locator('[data-editor-result]')).toContainText('valid JSON');
  expect(writes).toBe(0);
  await form.locator('[data-field=assets]').fill('[]');
  await form.locator('[data-field=instructions]').fill('Unsaved instruction draft');
  await form.locator('button[type=submit]').click();
  await expect(form.locator('[data-editor-result]')).toContainText('Draft validation rejected');
  await expect(form.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.dismiss());
  await form.locator('[data-editor-cancel]').click();
  await expect(form.locator('[data-field=instructions]')).toHaveValue('Unsaved instruction draft');
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('[data-skill-new]').click();
  await expect(form.locator('[data-field=instructions]')).toHaveValue('Unsaved instruction draft');
  const stored = await page.evaluate(() => JSON.stringify([Object.values(localStorage), Object.values(sessionStorage)]));
  expect(stored).not.toContain('Unsaved instruction draft');
  page.once('dialog', dialog => dialog.accept());
  await form.locator('[data-editor-cancel]').click();
  await expect(form).toHaveCount(0);
});

test('late Skill draft save does not erase newer typing and saved drafts reopen from canonical revisions', async ({ page }) => {
  let pending, saved;
  await page.route('**/api/skills/release-check', route => {
    if (route.request().method() === 'PATCH') { pending = route; return; }
    return route.fulfill({ json: { item: saved || skill } });
  });
  await page.route('**/api/skills?**', route => route.fulfill({ json: { items: [saved || skill] } }));
  await page.route('**/api/skills/release-check?revision=3', route => route.fulfill({ json: { item: saved } }));
  await page.getByRole('button', { name: /Release Check/ }).click();
  await page.locator('[data-skill-edit]').click();
  const form = page.locator('[data-skill-editor]');
  await form.locator('[data-field=reason]').fill('Save recovery draft');
  await form.locator('[data-field=instructions]').fill('Submitted instructions');
  await form.locator('button[type=submit]').click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  await form.locator('[data-field=instructions]').fill('Newer unsaved instructions');
  saved = { ...skill, revision: 3, recordId: 'saved-draft-3', definitionLifecycle: 'draft', skill: { ...skill.skill, instructions: 'Submitted instructions' } };
  await pending.fulfill({ json: { item: saved } });
  await expect(form.locator('[data-editor-result]')).toContainText('Saved inactive draft');
  await expect(form.locator('[data-field=instructions]')).toHaveValue('Newer unsaved instructions');
  await expect(form.locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.accept());
  await form.locator('[data-editor-cancel]').click();
  await page.goto('http://127.0.0.1:18766/tests/browser/skill_library_fixture.html?skill_id=release-check&skill_revision=3');
  await expect(page.locator('[data-skill-detail]')).toContainText('saved-draft-3');
  await expect(page.locator('[data-skill-publish]')).toBeVisible();
  await page.locator('[data-skill-edit]').click();
  await expect(form.locator('[data-field=instructions]')).toHaveValue('Submitted instructions');
  await expect(form.locator('[data-dirty-editor-status]')).toHaveText('No unsaved changes');
});

test('editing a reviewed Skill bundle invalidates the preview and retains an unsaved import', async ({ page }) => {
  await page.getByText('Import / promote').click();
  const input = page.locator('[data-skill-import-json]');
  await input.fill(JSON.stringify({ format: 'codex-web-skill-bundle', manifest: { skill_id: 'imported', instructions: 'Review first' } }));
  await page.locator('[data-skill-import-preview]').click();
  await expect(page.locator('[data-skill-import-confirm]')).toBeEnabled();
  await input.fill('{ changed');
  await expect(page.locator('[data-skill-import-confirm]')).toBeDisabled();
  await expect(input.locator('..').locator('[data-dirty-editor-status]')).toHaveText('Unsaved changes');
  page.once('dialog', dialog => dialog.dismiss());
  expect(await page.evaluate(async () => (await import('/static/dirty_editor.js')).confirmDiscard())).toBe(false);
  await expect(input).toHaveValue('{ changed');
});
