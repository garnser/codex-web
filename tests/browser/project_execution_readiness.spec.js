const {test, expect} = require('@playwright/test');
const base = process.env.CODEX_WEB_BROWSER_BASE || 'http://127.0.0.1:18766';
const open = page => page.goto(base + '/tests/browser/project_execution_readiness_fixture.html');

test('canonical scratch context enables actual composer while generic repository readiness stays blocked', async ({page}) => {
  await open(page);
  await expect(page.locator('#send')).toBeEnabled();
  await page.locator('#prompt').fill('One bounded original request');
  await expect(page.locator('#prompt')).toHaveValue('One bounded original request');
  expect(await page.evaluate(()=>window.CodexProjectSetup.getState().readiness.execution_ready)).toBe(false);
  const before=await page.evaluate(()=>window.fixture.calls.filter(x=>x.query.includes('execution_profile_id')).length);
  await page.evaluate(()=>{window.fixture.render();window.fixture.render();});
  expect(await page.evaluate(()=>window.fixture.calls.filter(x=>x.query.includes('execution_profile_id')).length)).toBe(before);
  await page.selectOption('#execution-profile','repository-write');
  await expect(page.locator('#send')).toBeDisabled();
  await expect(page.locator('#execution-readiness-status')).toContainText('blocked');
});

for(const change of ['profile','project','definition','sandbox']){
 test(`late readiness cannot enable a changed ${change} context`,async({page})=>{
  await open(page);await expect(page.locator('#send')).toBeEnabled();
  await page.evaluate(()=>{window.fixture.hold={};void window.CodexProjectSetup.refresh();});
  await expect.poll(()=>page.evaluate(()=>window.fixture.hold.pending)).toBe(true);
  if(change==='profile')await page.selectOption('#execution-profile','repository-write');
  if(change==='sandbox')await page.selectOption('#sandbox','read-only');
  if(change==='definition')await page.evaluate(()=>{window.fixture.blockedRevisions=[2];window.fixture.publish(2);});
  if(change==='project')await page.evaluate(()=>{window.fixture.workerReady=false;window.fixture.switchProject('project-b');});
  await expect(page.locator('#send')).toBeDisabled();
  await page.evaluate(()=>window.fixture.hold.release());
  await expect.poll(()=>page.evaluate(()=>window.fixture.hold.settled)).toBe(true);
  await expect(page.locator('#send')).toBeDisabled();
 });
}

test('unavailable profile and worker denial remain disabled',async({page})=>{
 await open(page);await expect(page.locator('#send')).toBeEnabled();
 await page.evaluate(()=>{window.fixture.workerReady=false;void window.CodexProjectSetup.refresh();});
 await expect(page.locator('#send')).toBeDisabled();
 await page.evaluate(()=>{window.fixture.catalog=()=>({items:[],definition:null});window.fixture.render();});
 await expect(page.locator('#prompt')).toBeDisabled();
 await expect(page.locator('#execution-readiness-status')).toContainText('available execution profile');
});


test('failed contextual request cannot enable the composer',async({page})=>{
 await open(page);await expect(page.locator('#send')).toBeEnabled();
 await page.evaluate(()=>{
  const previous=window.fetch;
  window.fetch=(input,options)=>String(input).includes('execution_profile_id')
   ?Promise.resolve(new Response(JSON.stringify({detail:'private fixture unavailable'}),{status:503}))
   :previous(input,options);
  void window.CodexProjectSetup.refresh();
 });
 await expect(page.locator('#prompt')).toBeDisabled();
 await expect(page.locator('#execution-readiness-status')).toContainText('readiness unavailable');
});
