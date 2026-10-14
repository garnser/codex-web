const {test, expect} = require('@playwright/test');
const {resolveAction} = require('./action_confirmation_helpers');
const fixture = `<!doctype html><html><head><meta charset="utf-8"></head><body><details id="developer-panel" open><button id="refresh-execution-workspaces">Refresh</button>
<div id="execution-workspace-status" role="status"></div><div id="execution-workspace-list"></div></details>
<script src="/static/execution_workspace_admin.js"></script></body></html>`;
async function mount(page, {denied=false, retired=false}={}) {
  const posts=[];
  const scope = {work_item_ref:'group/app#42', repository_resource_id:'repo', assignment_id:'assignment', assignment_fence:7,
    authorized_by:'owner', authorized_at:1, expires_at:9999999999, lease_id:'lease', source_execution_id:'execution', source_subject:{kind:'thread',ref:'thread'}};
  const workspace = {id:'workspace', subject:{kind:'thread',ref:'thread'},execution_id:'execution',project_id:'project',owner_identity_id:'owner',
    kind:'git_worktree',status:'active',repository_resource_id:'repo',resource_ids:['repo'],head_revision:'a'.repeat(40),publication_recovery:scope,
    publication_recovery_history:retired?[{prior_scope:{...scope,work_item_ref:'group/app#41'},current_scope:scope,retired_at:1,
      source_identity:{revision:'terminal-r2'},source_state:'closed',merge_revision:'b'.repeat(40),head_revision:'a'.repeat(40),
      execution_profile_definition:{record_id:'pin-r1',revision:1},agent_profile_record_id:'agent-r10',authorized_by:'owner',idempotency_key:'prior-handoff',
      receipt_ids:['exact-receipt'],verification_ids:['exact-verification'],completion_intent_ids:['exact-intent'],validation_checks:[{name:'required',revision:'a'.repeat(40),conclusion:'success'}]}]:[]};
  await page.route('**/handoff-fixture',r=>r.fulfill({contentType:'text/html',body:fixture}));
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/identity/me')return route.fulfill({json:{identity_id:'owner',principal_kind:'human',roles:['admin'],assurance:'mfa'}});
    if(path==='/api/projects')return route.fulfill({json:[]});
    if(path==='/api/resources')return route.fulfill({json:{items:[]}});
    if(path==='/api/execution-workspaces/inspection')return route.fulfill({json:{items:[{workspace,lease_active:true,lease:{id:'lease',mode:'write',expires_at:9999999999}}]}});
    if(path.includes('/publication-recovery/')) {
      posts.push({path,body:route.request().postDataJSON()});
      if(denied)return route.fulfill({status:409,json:{detail:{reason:'Intent pending reconciliation',next_action:'Inspect exact unknown receipt; do not replay'}}});
      if(path.endsWith('/preview'))return route.fulfill({json:{allowed:true,source_identity:{revision:'terminal-r3'},merge_revision:'b'.repeat(40),validation_checks:[{}],next_action:'Confirm exact scope retirement'}});
      return route.fulfill({json:{item:workspace}});
    }
    return route.fulfill({status:404,json:{detail:'Missing fixture route'}});
  });
  await page.goto('http://127.0.0.1:18766/handoff-fixture');
  await expect(page.getByText('Current publication recovery')).toBeAttached();
  await page.locator('[data-workspace-row] > summary').click();
  await page.getByLabel('Next owned Work Item').fill('group/app#43');
  await page.getByLabel('Verified completion intent IDs').fill('branch-intent, merge-intent');
  return posts;
}
test('handoff requires preview and scoped confirmation with exact preconditions',async({page})=>{
  const posts=await mount(page);
  await page.getByRole('button',{name:'Preview publication recovery handoff'}).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('dialog')).toContainText('group/app#42 → group/app#43');
  await expect(page.getByRole('dialog')).toContainText('fence 7');
  await expect.poll(()=>posts.length).toBe(1);
  await resolveAction(page);
  await expect.poll(()=>posts.length).toBe(2);
  expect(posts[1].body).toEqual(posts[0].body);
  expect(posts[1].body.expected_prior_scope.work_item_ref).toBe('group/app#42');
  expect(posts[1].body.expected_head_revision).toBe('a'.repeat(40));
});
test('denied preview retains draft and exposes reconciliation next action',async({page})=>{
  const posts=await mount(page,{denied:true});
  await page.getByRole('button',{name:'Preview publication recovery handoff'}).click();
  await expect(page.locator('[data-handoff-result]')).toContainText('Inspect exact unknown receipt; do not replay');
  await expect(page.getByLabel('Next owned Work Item')).toHaveValue('group/app#43');
  expect(posts).toHaveLength(1);
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
test('retired scope shows exact provenance separately from current scope',async({page})=>{
  await mount(page,{retired:true});
  await page.getByText('Retired scope: group/app#41',{exact:false}).click();
  await expect(page.getByText('Terminal source:',{exact:false})).toContainText('terminal-r2');
  await expect(page.getByText('Receipts:',{exact:false})).toContainText('exact-receipt');
  await expect(page.getByRole('link',{name:'Inspect completion intent exact-intent'})).toBeVisible();
});
