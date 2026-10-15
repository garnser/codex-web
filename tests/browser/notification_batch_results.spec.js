const { test, expect } = require('@playwright/test');
const fixture='http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?batch=native_input_started&batchCapture';

test('native result reconciliation captures unverified evidence without replay',async({page})=>{
  await page.goto(fixture);
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Inspect batch originals'}).first().click();
  await expect(page.locator('pre').first()).toContainText('Candidate evidence requiring review: fixture-unreviewed-proposal');
  await expect(page.getByRole('link',{name:'View evidence: Inspect evidence fixture-unreviewed-proposal'})).toHaveAttribute('href','/projects/project-a/operations?consumer_type=evidence&consumer_id=fixture-unreviewed-proposal');
  await page.getByRole('button',{name:'Capture completed native proposals'}).click();
  await expect(page.getByText('Independent review is still required; no native turn was started.',{exact:false})).toBeVisible();
  expect(await page.evaluate(()=>window.__captureBody)).toEqual({expected_native_turn_id:'shared-native-turn'});
  expect(await page.evaluate(()=>window.__apiCalls.filter(call=>call.method==='POST'))).toEqual([
    {path:'/api/threads/thread-1/notification-batches/fixture-batch/results',method:'POST'}]);
  await expect(page.getByRole('button',{name:/^Retry$|Return originals to queue/})).toHaveCount(0);
});

test('blocked result capture shows failure and does not assert a verified outcome',async({page})=>{
  await page.goto(fixture+'&captureDenied');
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Capture completed native proposals'}).click();
  await expect(page.getByText('Cannot capture native proposals:',{exact:false})).toBeVisible();
  await expect(page.getByText('Every original outcome remains unresolved.',{exact:false})).toBeVisible();
  await expect(page.getByRole('button',{name:'Capture completed native proposals'})).toBeEnabled();
});

test('late capture cannot refresh a different Project view',async({page})=>{
  await page.goto(fixture+'&holdCapture');
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Capture completed native proposals'}).click();
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__finishCapture))).toBe(true);
  await page.evaluate(()=>{
    window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'foreign'}}));
    window.__finishCapture.resolve({evidence_ids:['late-foreign-proof'],independently_verified:false});
  });
  await page.waitForTimeout(100);
  await expect(page.locator('#messages')).not.toContainText('late-foreign-proof');
});
