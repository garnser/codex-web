const { test, expect } = require('@playwright/test');

test('typed references preserve the deployment prefix and Project, encode IDs, and fail closed for unknown editors', async ({ page }) => {
  await page.route('**/codex/projects/**', route => route.fulfill({ contentType: 'text/html', body: '<body><main></main></body>' }));
  await page.goto('http://127.0.0.1:18766/codex/projects/p-one/operations');
  const result = await page.evaluate(async () => {
    const { referencePath, referenceLink } = await import('/static/reference_navigation.js');
    const { bindingPath } = await import('/static/provider_binding_links.js');
    document.querySelector('main').innerHTML = referenceLink('runtime', '<cli>', { readOnlyReason: 'Deployment-owned runtime: change its registered adapter configuration.' });
    return { binding: bindingPath('provider/a'), skill: referencePath('skill', 'review', { revision: 3 }), foreign: referencePath('resource', 'repo', { projectId: 'p-two' }), invalid: referencePath('__proto__', 'x') };
  });
  expect(result).toEqual({ binding: '/codex/projects/p-one/operations?provider_id=provider%2Fa', skill: '/codex/projects/p-one/skills?skill_id=review&skill_revision=3', foreign: '/codex/projects/p-two/resources?consumer_type=resource&consumer_id=repo', invalid: null });
  await expect(page.locator('main')).toContainText('<cli> Deployment-owned runtime');
  await expect(page.locator('main a')).toHaveCount(0);
  await expect(page.locator('main cli')).toHaveCount(0);
});

test('shared focus opens only the exact requested canonical configuration revision', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/skill_library_fixture.html?configuration_key=route.preference&reference_revision=2');
  await page.evaluate(async () => {
    const host = document.createElement('main'); document.body.append(host);
    host.innerHTML = '<details data-configuration-key="route.preference" data-reference-revision="1"><summary>Older</summary></details><details data-configuration-key="route.preference" data-reference-revision="2"><summary>Requested</summary></details>';
    const { focusReference } = await import('/static/reference_links.js'); focusReference(host);
  });
  await expect(page.locator('main details[open]')).toHaveCount(1);
  await expect(page.locator('main details[open]')).toContainText('Requested');
  await expect(page.locator('main details[open]')).toBeFocused();
});

test('missing Project context offers inspection guidance without an invented scope', async ({ page }) => {
  await page.route('**/reference-empty', route => route.fulfill({ contentType: 'text/html', body: '<body><main></main></body>' }));
  await page.goto('http://127.0.0.1:18766/reference-empty');
  await page.evaluate(async () => {
    const { referenceLink } = await import('/static/reference_navigation.js');
    document.querySelector('main').innerHTML = referenceLink('resource', 'resource-one');
  });
  await expect(page.locator('main')).toContainText('no object editor is available in this Project context');
  await expect(page.locator('main a')).toHaveCount(0);
});

test('Work Item related objects use scoped canonical views and a changed Project rejects the old request', async ({ page }) => {
  await page.route('**/projects/p-one/work-items**', route => route.fulfill({ contentType: 'text/html', body: '<body><main></main></body>' }));
  await page.goto('http://127.0.0.1:18766/projects/p-one/work-items?consumer_type=work_item&consumer_id=repo%231');
  const selected = await page.evaluate(async () => {
    const { requestedReference } = await import('/static/reference_navigation.js');
    const { workItemSummaryHtml } = await import('/static/work_item_summary_ui.js');
    document.querySelector('main').innerHTML = workItemSummaryHtml({ item: { goal_id: 'goal-one', decision_id: 'decision-one', resource_ids: ['repo-one'] }, esc: value => String(value ?? '') });
    return [requestedReference('work_item', 'p-one'), requestedReference('work_item', 'p-two')];
  });
  expect(selected).toEqual(['repo#1', '']);
  await expect(page.getByRole('link', { name: 'View goal: goal-one' })).toHaveAttribute('href', '/projects/p-one/goals?consumer_type=goal&consumer_id=goal-one');
  await expect(page.getByRole('link', { name: 'View resource: repo-one' })).toHaveAttribute('href', '/projects/p-one/resources?consumer_type=resource&consumer_id=repo-one');
});
