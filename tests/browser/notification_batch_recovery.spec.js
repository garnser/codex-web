const {test,expect}=require('@playwright/test');
const fixture='http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?batch=prepared&batchRecovery';

test('restoration shows exact scope and requires a reason before its single mutation',async({page})=>{
  await page.goto(fixture);
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Review queue restoration'}).click();
  await expect(page.getByText('2 originals: original-one, original-two.',{exact:false})).toBeVisible();
  await expect(page.getByText('destination: replacement-thread',{exact:false})).toBeVisible();
  const restore=page.getByRole('button',{name:'Return originals to queue'});
  await expect(restore).toBeDisabled();
  await page.getByLabel('Reason for returning these originals to the queue').fill('Reviewed fixture originals');
  await restore.click();
  await expect(page.getByText('Originals returned to the queue.',{exact:false})).toBeVisible();
  expect(await page.evaluate(()=>window.__recoveryBody)).toEqual({expected_admission_sha256:'a'.repeat(64),reason:'Reviewed fixture originals'});
  expect(await page.evaluate(()=>window.__reloadCalls)).toEqual(['replacement-thread']);
  expect(await page.evaluate(()=>window.__apiCalls.filter(call=>call.method==='POST').length)).toBe(1);
});

for(const condition of ['recoveryDenied','recoveryUnknown']) {
  test(`restoration ${condition} never offers a replay action`,async({page})=>{
    await page.goto(fixture+'&'+condition);
    await expect.poll(()=>page.evaluate(()=>Boolean(window.__ready))).toBe(true);
    await page.getByRole('button',{name:'Review queue restoration'}).click();
    await expect(page.getByRole('button',{name:'Return originals to queue'})).toHaveCount(0);
    expect(await page.evaluate(()=>window.__apiCalls.every(call=>call.method==='GET'))).toBe(true);
  });
}

test('stale reviewed restoration exposes conflict and does not reload',async({page})=>{
  await page.goto(fixture+'&recoveryConflict');
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Review queue restoration'}).click();
  await page.getByLabel('Reason for returning these originals to the queue').fill('Reviewed fixture');
  await page.getByRole('button',{name:'Return originals to queue'}).click();
  await expect(page.getByText('Restoration refused: Reviewed admission changed')).toBeVisible();
  expect(await page.evaluate(()=>window.__reloadCalls)).toEqual([]);
});

test('late restoration response cannot reload another Project view',async({page})=>{
  await page.goto(fixture+'&holdRecovery');
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Review queue restoration'}).click();
  await page.getByLabel('Reason for returning these originals to the queue').fill('Reviewed fixture');
  await page.getByRole('button',{name:'Return originals to queue'}).click();
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__finishRecovery))).toBe(true);
  await page.evaluate(()=>{
    window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'foreign'}}));
    window.__finishRecovery.resolve({threadId:'replacement-thread'});
  });
  await page.waitForTimeout(100);
  expect(await page.evaluate(()=>window.__reloadCalls)).toEqual([]);
});
