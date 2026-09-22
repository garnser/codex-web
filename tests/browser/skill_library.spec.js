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
