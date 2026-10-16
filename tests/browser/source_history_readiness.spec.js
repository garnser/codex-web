const {test, expect} = require('@playwright/test');
// Public fixture: invoke the same renderer used by workspace events/audit.
// No canonical state writes, application records, credentials or mock readiness.
test('canonical history observations expose target/local bounds refusal and provenance', async ({page}) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/source_history_readiness_fixture.html');
  const root = page.getByRole('region', {name:'History fixture'});
  await expect(root.getByRole('region',{name:'Source history readiness'})).toHaveCount(2);
  await expect(root).toContainText('Target aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
  await expect(root).toContainText('local bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');
  await expect(root).toContainText('4096 commits depth / 120 seconds');
  await expect(root).toContainText('inspect_history_bound_or_destination_refusal');
  await expect(root).toContainText('canonical workspace refresh');
  await expect(root.locator('script,img')).toHaveCount(0);
  await expect(root).toContainText('<img src=x onerror=alert(1)>');
});
