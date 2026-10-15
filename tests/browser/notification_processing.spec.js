const {test,expect}=require('@playwright/test');
const fixture='http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?batch=native_input_started&batchProcessing';
async function review(page,extra='') {
  await page.goto(fixture+extra);
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__ready))).toBe(true);
  await page.getByRole('button',{name:'Review original processing and replies'}).click();
}
test('reviewed per-original processing shows canonical follow-up and retained reply preview without sending',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await review(page);
  await expect(page.getByRole('link',{name:'View work item: Follow-up fixture/repo#1'})).toHaveAttribute('href','/projects/project-a/work-items?consumer_type=work_item&consumer_id=fixture%2Frepo%231');
  await expect(page.getByRole('link',{name:'View evidence: evidence fixture-evidence'})).toHaveAttribute('href','/projects/project-a/operations?consumer_type=evidence&consumer_id=fixture-evidence');
  await expect(page.getByRole('link',{name:'View verification: verification fixture-independent-review'})).toHaveAttribute('href','/projects/project-a/operations?consumer_type=verification&consumer_id=fixture-independent-review');
  await expect(page.locator('#messages')).toContainText('fixture-room');
  await expect(page.locator('#messages')).toContainText('fixture-independent-review');
  expect(await page.evaluate(()=>Boolean(window.__unsafe))).toBe(false);
  const process=page.getByRole('button',{name:'Process reviewed original original-one'});
  await process.focus();await page.keyboard.press('Enter');
  await expect(page.getByText('Canonical processing: processed_current.',{exact:false})).toBeVisible();
  expect(await page.evaluate(()=>window.__processingBody)).toEqual({original_id:'original-one',expected_processing_sha256:'a'.repeat(64)});
  expect(await page.evaluate(()=>window.__apiCalls.filter(x=>x.method==='POST'))).toEqual([
    {path:'/api/threads/thread-1/notification-batches/fixture-batch/processing',method:'POST'}]);
  await expect(page.getByRole('button',{name:/Send|Replay|ACK/})).toHaveCount(0);
});

for (const context of ['Project','identity']) {
  test(`retained ${context} panel refuses old review and process requests before API access`,async({page})=>{
    await review(page);
    const before=await page.evaluate(()=>window.__apiCalls.length);
    const text=await page.locator('#messages').textContent();
    await page.evaluate(context=>{
      if (context==='Project') {
        window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'foreign'}}));
        window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'project-a'}}));
      } else window.dispatchEvent(new CustomEvent('codex:identity-state-rendered',{detail:{actor:{
        identity_id:'new-actor',organization_id:'org-a',workspace_id:'workspace-a',session_id:'new-session'}}}));
    },context);
    await page.getByRole('button',{name:'Review original processing and replies'}).click();
    await page.getByRole('button',{name:'Process reviewed original original-one'}).click();
    expect(await page.evaluate(()=>window.__apiCalls.length)).toBe(before);
    expect(await page.locator('#messages').textContent()).toBe(text);
    await page.evaluate(()=>window.__ui.render('thread-1'));
    await expect(page.getByRole('button',{name:'Review original processing and replies'})).toHaveCount(0);
  });
  test(`held preview cannot repaint retained panel after ${context} selection changes`,async({page})=>{
    await review(page,'&holdProcessingPreview');
    await expect.poll(()=>page.evaluate(()=>Boolean(window.__finishProcessingPreview))).toBe(true);
    await page.evaluate(context=>{
      if (context==='Project') window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'foreign'}}));
      else window.dispatchEvent(new CustomEvent('codex:identity-state-rendered',{detail:{actor:{identity_id:'new-actor'}}}));
      window.__finishProcessingPreview.resolve();
    },context);
    await page.waitForTimeout(100);
    await expect(page.getByRole('button',{name:'Process reviewed original original-one'})).toHaveCount(0);
    await expect(page.locator('#messages')).not.toContainText('Follow-up owner: fixture-owner');
    expect(await page.evaluate(()=>window.__apiCalls.filter(x=>x.method==='POST').length)).toBe(0);
  });
}

test('retained old-thread panel and replaced preview buttons cannot submit stale proof',async({page})=>{
  await review(page);
  const staleProcess=await page.getByRole('button',{name:'Process reviewed original original-one'}).elementHandle();
  await page.getByRole('button',{name:'Review original processing and replies'}).click();
  await staleProcess.evaluate(button=>button.click());
  expect(await page.evaluate(()=>window.__apiCalls.filter(x=>x.method==='POST').length)).toBe(0);
  await page.evaluate(()=>window.__ui.load('thread-2'));
  const before=await page.evaluate(()=>window.__apiCalls.length);
  await page.getByRole('button',{name:'Review original processing and replies'}).click();
  await page.getByRole('button',{name:'Process reviewed original original-one'}).click();
  expect(await page.evaluate(()=>window.__apiCalls.length)).toBe(before);
});
test('stale proof is visible and does not expose processing',async({page})=>{
  await review(page,'&processingStale');
  await expect(page.locator('#messages')).toContainText('historical_requires_reconciliation');
  await expect(page.locator('#messages')).toContainText('evidence_inactive_or_expired');
  await expect(page.getByRole('button',{name:'Process reviewed original original-one'})).toHaveCount(0);
});
test('denied or unsupported installed runtime explains unavailable processing',async({page})=>{
  await review(page,'&processingDenied');
  await expect(page.getByText('Processing review unavailable or denied:',{exact:false})).toBeVisible();
});
test('unknown processing response requires read reconciliation and never retries automatically',async({page})=>{
  await review(page,'&processingConflict');
  const process=page.getByRole('button',{name:'Process reviewed original original-one'});
  await process.click();
  await expect(page.getByText('Processing unavailable or outcome unknown:',{exact:false})).toBeVisible();
  await expect(process).toBeDisabled();
  expect(await page.evaluate(()=>window.__apiCalls.filter(x=>x.method==='POST').length)).toBe(1);
});
test('late processing response cannot mutate a different Project view',async({page})=>{
  await review(page,'&holdProcessing');
  await page.getByRole('button',{name:'Process reviewed original original-one'}).click();
  await expect.poll(()=>page.evaluate(()=>Boolean(window.__finishProcessing))).toBe(true);
  await page.evaluate(()=>{
    window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'foreign'}}));
    window.__finishProcessing.resolve({original:{processing_state:'late-foreign-state',reply:{delivery_state:'not_sent'}}});
  });
  await page.waitForTimeout(100);
  await expect(page.locator('#messages')).not.toContainText('late-foreign-state');
});
