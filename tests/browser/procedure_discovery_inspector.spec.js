const { test, expect } = require('@playwright/test');
const fixture = `<!doctype html><html><head><link rel="stylesheet" href="/static/styles.css"></head><body data-project-id="project-a"><details id="developer-panel" open>
<button id="refresh-action-intents">Refresh</button><div id="action-intent-status"></div>
<div id="action-intent-list"></div></details><script src="/static/action_intent_admin.js"></script></body></html>`;

test('canonical discovery history exposes exact pins and blockers, escapes content, and fences late scope responses', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  let release;
  let calls = 0;
  let historyRequested;
  const requested = new Promise(resolve => {historyRequested = resolve;});
  const held = new Promise(resolve => {release = resolve;});
  const row = {id:'intent-a',project_id:'project-a',action_id:'service.rotate',status:'failed',provider_type:'ops',provider_instance:'test',request:{parameters:{}},resource_ids:['gitlab']};
  const discovery = {outcome:'inaccessible',organization_id:'org-a',workspace_id:'workspace-a',project_id:'project-a',actor_identity_id:'human-a',operation:'service.rotate',context_checksum:'context-exact',policy_source:'canonical:policy',policy_allows:true,blockers:['procedure_expired <script>bad()</script>'],supporting_sources:['memory:guide@3'],instruction_checksum:'instruction-exact',selected:[{definition_id:'gitlab-logs',record_id:'record-exact',revision:4,checksum:'checksum-exact'}],candidates:[{reference:{definition_id:'gitlab-logs',revision:4},outcome:'blocked',reasons:['procedure_expired']}]};
  await page.route('**/procedure-inspector-fixture',route=>route.fulfill({contentType:'text/html',body:fixture}));
  await page.route('**/api/**',async route=>{
    const path = new URL(route.request().url()).pathname;
    if(path === '/api/action-intents') return route.fulfill({json:{items:[row]}});
    if(path === '/api/identity/me') return route.fulfill({json:{identity_id:'human-a',principal_kind:'human',assurance:'local_trusted',roles:['admin']}});
    if(path === '/api/action-intents/intent-a/history') {
      if (++calls === 2) { historyRequested(); await held; }
      return route.fulfill({json:{intent:{...row,procedure_discoveries:[discovery],rollback_checkpoint:{status:"unknown",origin_receipt_id:"receipt-original",receipt_id:null,error_code:"provider_timeout <script>bad()</script>"}},receipts:[],verifications:[],inbox:[]}});
    }
    return route.fulfill({status:404,json:{detail:'unavailable'}});
  });
  await page.goto(`${process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766'}/procedure-inspector-fixture`);
  await page.locator('[data-action-intent-row="intent-a"] > summary').click();
  await page.getByRole('button',{name:'Load side-effect timeline'}).click();
  const history=page.locator('#action-intent-history-intent-a');
  await expect(history).toContainText('record-exact');
  await expect(history).toContainText('checksum-exact');
  await expect(history).toContainText('procedure_expired <script>bad()</script>');
  await expect(history.locator('script')).toHaveCount(0);
  await expect(history).toContainText('canonical:policy');
  await expect(history).toContainText('memory:guide@3');
  await expect(history).toContainText('Originating-intent rollback · unknown');
  await expect(history).toContainText('receipt-original');
  await expect(history).toContainText('pending reconciliation');
  await expect(history).toContainText('provider_timeout <script>bad()</script>');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('button',{name:'Load side-effect timeline'}).click();
  await requested;
  // A late response from the old Project cannot resurrect its provenance.
  await page.evaluate(()=>{document.body.dataset.projectId='project-b';window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'project-b'}}));});
  release();
  await expect(page.locator('#action-intent-list')).not.toContainText('record-exact');
  await expect(page.locator('[data-action-intent-row="intent-a"]')).toHaveCount(0);
});
