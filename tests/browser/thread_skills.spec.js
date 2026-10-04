const { test, expect } = require('@playwright/test');

const explicit = {
  skillId: 'review', revision: 2, recordId: 'review-r2', definitionLifecycle: 'published',
  definitionReference: { definition_id: 'review', kind: 'agent.skill', revision: 2, record_id: 'review-r2', checksum: 'a'.repeat(64), definition_schema_version: '1.0' },
  skill: { name: 'Review', categories: ['software engineering'], provenance: { source_id: 'engineering' } },
};
const inherited = { ...explicit, skillId: 'security', recordId: 'security-r1', revision: 1, skill: { name: 'Security', categories: ['security'], provenance: { source_id: 'local' } }, inheritedFrom: 'agent_profile:secure' };

test('Thread Skill picker distinguishes origins and persists exact explicit revisions', async ({ page }) => {
  const writes = [];
  await page.route('**/api/skill-sources', route => route.fulfill({ json: { items: [{ source_id: 'engineering', name: 'Engineering', trust: 'approved' }] } }));
  await page.route('**/api/skills?**', route => route.fulfill({ json: { items: [explicit] } }));
  await page.route('**/api/threads/thread-a/skills', route => {
    if (route.request().method() === 'PUT') {
      writes.push(route.request().postDataJSON());
      return route.fulfill({ json: { explicit: [explicit], inherited: [inherited], effective: [explicit, inherited] } });
    }
    return route.fulfill({ json: { explicit: [explicit], inherited: [inherited], effective: [explicit, inherited] } });
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/thread_skills_fixture.html');
  await page.locator('#thread-skills-manager > summary').click();
  await expect(page.locator('#thread-skills-effective')).toContainText('explicit r2');
  await expect(page.locator('#thread-skills-effective')).toContainText('inherited');
  await page.locator('#thread-skills-save').click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].skill_refs).toEqual([explicit.definitionReference]);
});
