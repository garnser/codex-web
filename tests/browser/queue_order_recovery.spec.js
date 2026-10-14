const {test,expect}=require('@playwright/test');
async function setup(page,{allowed=true,denied=false,deferred=false}={}){
  await page.goto('http://127.0.0.1:18766/static/queue_order_recovery_ui.js');
  await page.evaluate(async({allowed,denied,deferred})=>{
    document.body.innerHTML='<main id="messages"></main>';
    window.calls=[];window.loads=[];window.generation=0;
    const {createQueueOrderRecoveryUi}=await import('/static/queue_order_recovery_ui.js');
    window.ui=createQueueOrderRecoveryUi({
      captureView:()=>{const generation=window.generation;return ()=>generation===window.generation;},
      api:async(path,options)=>{
        window.calls.push({path,options});
        if(options){if(denied)throw Error('Queue changed; inspect fresh preview');
          if(deferred)return new Promise(resolve=>window.resolveRepair=resolve);
          return {ok:true,audit_recorded:true};}
        return {current_order:['later-original','held-original'],proposed_order:['held-original','later-original'],
          snapshot_sha256:'a'.repeat(64),can_repair:allowed,
          blockers:allowed?[]:[{code:'queue_order_authority_denied',message:'Administrator MFA required'}]};
      },
      addMessage:(title,text)=>{const card=document.createElement('article');const body=document.createElement('div');
        body.className='body';body.textContent=title+'\n'+text;card.append(body);document.querySelector('#messages').append(card);return card;},
      loadThread:async id=>window.loads.push(id),scheduleRefresh:()=>{}});
    await window.ui.load('thread-fixture');window.ui.render('thread-fixture');
  },{allowed,denied,deferred});
}
test('inspector previews exact order and submits one explicit guarded repair',async({page})=>{
  await setup(page);
  await expect(page.locator('#messages')).toContainText('Unknown native outcomes remain unknown');
  const button=page.getByRole('button',{name:'Repair scheduling order'});
  await expect(button).toBeDisabled();
  await page.getByLabel('Queue order repair reason').fill('Restore recorded maintenance order');
  await button.click();
  await expect(page.getByRole('status')).toContainText('Native outcomes were preserved');
  const posts=await page.evaluate(()=>window.calls.filter(call=>call.options));expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0].options.body)).toEqual({expected_snapshot_sha256:'a'.repeat(64),reason:'Restore recorded maintenance order'});
  expect(await page.evaluate(()=>window.loads)).toEqual(['thread-fixture']);
});
test('denied authority exposes reason without a mutation action',async({page})=>{
  await setup(page,{allowed:false});
  await expect(page.locator('#messages')).toContainText('Administrator MFA required');
  await expect(page.getByRole('button',{name:'Repair scheduling order'})).toHaveCount(0);
});
test('stale repair preserves reason and does not automatically retry',async({page})=>{
  await setup(page,{denied:true});await page.getByLabel('Queue order repair reason').fill('Keep original fields');
  await page.getByRole('button',{name:'Repair scheduling order'}).click();
  await expect(page.getByRole('status')).toContainText('Reload the preview before retrying');
  await expect(page.getByLabel('Queue order repair reason')).toHaveValue('Keep original fields');
  expect(await page.evaluate(()=>window.calls.filter(call=>call.options).length)).toBe(1);
});
test('late response does not reload or alter another Project view',async({page})=>{
  await setup(page,{deferred:true});await page.getByLabel('Queue order repair reason').fill('Fixture');
  await page.getByRole('button',{name:'Repair scheduling order'}).click();
  await page.evaluate(()=>{window.generation++;window.resolveRepair({ok:true,audit_recorded:true});});
  expect(await page.evaluate(()=>window.loads)).toEqual([]);
  await expect(page.getByRole('status')).not.toContainText('Scheduling order repaired');
});
