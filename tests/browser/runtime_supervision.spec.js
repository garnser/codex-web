const {test,expect}=require('@playwright/test');
const base=process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766';
test('lost recovering stopped and actual activity remain distinct with unavailable diagnostics',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${base}/tests/browser/runtime_supervision_fixture.html`);
  await expect(page.locator('#lost')).toContainText('Runner lost');
  await expect(page.locator('#recovering')).toContainText('waiting for worker');
  await expect(page.locator('#stopped')).toContainText('diagnostic evidence unavailable');
  await expect(page.locator('#stopped')).toContainText('validation report captured · outcome unverified');
  await expect(page.locator('#lost')).toContainText('validation evidence unavailable');
  for(const id of ['lost','recovering','stopped']) await expect(page.locator(`#${id}`)).toHaveAttribute('data-active','false');
  await expect(page.locator('#running')).toHaveAttribute('data-active','true');
  await expect(page.locator('#observation')).toContainText('observed fence 1 · current fence 2');
  await expect(page.locator('#observation')).toContainText('completion not asserted');
  await expect(page.locator('#observation img')).toHaveCount(0);
  await expect(page.locator('#observation')).toContainText('<img src=x onerror=alert(1)>');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
