const {test, expect} = require('@playwright/test');
const origin = process.env.CODEX_WEB_BROWSER_TEST_ORIGIN || 'http://127.0.0.1:18766';
const assignment = () => ({id:'assignment-a', project_id:'project-a', execution_id:'execution-a',
  execution_profile_id:'repository-write', brokered_control_plane:{enabled:true, credential_exposed:false,
    operations:[{id:'repository.read.merge-requirements'}], assignment_context:{
      merge_requirements_declaration:{declared:true},
      declared_repository_scope:{writable_repository_ids:['repository-a'], read_only_repository_ids:['repository-b']}}}});
const response = () => ({scope:{project_id:'project-a', assignment_id:'assignment-a', repository_id:'repository-a',
  execution_id:'execution-a', fence:4, inspecting_identity_id:'operator-a', requester_identity_id:'requester-a', inspection_kind:'operator'},
  item:{schema_version:'1.0', resource_id:'repository-a', external_id:'7', provider_type:'github', provider_instance:'github.example',
    web_url:'https://github.example/acme/widgets/pull/7', head_revision:'a'.repeat(40), base_revision:'b'.repeat(40), target_branch:'main',
    observed_at:Date.now()/1000, expires_at:Date.now()/1000+30, complete:true, changed:false, pull_request_state:'OPEN', draft:false,
    mergeable:'MERGEABLE', merge_state:'CLEAN', review_decision:'APPROVED', strict_checks_required:true,
    reviews:{approving_count:0, code_owner_required:false, last_push_approval_required:false, dismiss_stale_approvals:true},
    independent_approval_count:0, current_head_approval_count:0, conversation_resolution_required:true,
    conversation_count:3, unresolved_conversation_count:0,
    sources:[{source:'effective_rules', availability:'complete', provider_source:'github:effective_rules', record_count:0, rule_ids:[]}],
    required_checks:[{context:'unit',provider_app_id:'15368',source_ids:['org-rule']}],
    observed_checks:[{context:'unit',state:'success',publisher_known:true,provider_app_id:'15368',provider_state:'completed',provider_conclusion:'success'}],
    blockers:[], unsupported_rules:[]}});

async function mount(page, item=assignment()) {
  await page.route('**/merge-fixture', route=>route.fulfill({contentType:'text/html', body:
    '<!doctype html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/design_tokens.css"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/workspace_components.css"><style>html,body{height:auto;overflow:auto}body{display:block;padding:16px;box-sizing:border-box}</style></head><body data-project-id="project-a"><details open><summary>Assignment</summary><div data-execution-assignment-management-host="assignment-a"></div></details><script src="/static/control_plane_broker_admin.js"></script></body>'}));
  await page.goto(origin+'/merge-fixture');
  await expect.poll(async()=>{
    await page.evaluate(value=>window.dispatchEvent(new CustomEvent('codex:execution-worker-state-rendered',{detail:{assignments:[value]}})), item);
    return page.locator('.control-plane-broker-status').count();
  }).toBe(1);
}
async function read(page) {
  await page.getByLabel('Pull request number').fill('7');
  await page.getByRole('button', {name:'Inspect current merge requirements'}).focus();
  await page.keyboard.press('Enter');
}
test('current canonical facts show exact scope, explicit zero, requirements and observations with keyboard access', async({page})=>{
  const reads=[];
  await page.route('**/api/control-plane-broker/merge-requirements?*', route=>{
    reads.push(new URL(route.request().url())); return route.fulfill({json:response()});
  });
  await mount(page); await read(page);
  const results=page.locator('[data-merge-results]');
  await expect(results).toContainText('complete observation');
  await expect(results).toContainText('Review minimum 0 · independent approvals 0');
  await expect(results).toContainText('unresolved 0');
  await expect(results).toContainText('Head '+ 'a'.repeat(40));
  await expect(results).toContainText('base '+ 'b'.repeat(40));
  await expect(results).toContainText('operator-a · requester requester-a · operator inspection');
  await expect(results.getByRole('link',{name:'PR 7',exact:true})).toHaveAttribute('href','https://github.example/acme/widgets/pull/7');
  await expect(results.getByRole('link',{name:'View resource: repository-a'})).toHaveAttribute('href',/projects\/project-a\/resources/);
  await expect(results.getByRole('link',{name:'View assignment: assignment-a'})).toHaveAttribute('href',/projects\/project-a\/workers/);
  await results.locator('summary').click();
  await expect(results).toContainText('Required check unit · publisher 15368');
  await expect(results).toContainText('Observed unit · success · publisher 15368');
  expect(reads[0].searchParams.get('project_id')).toBe('project-a');
  expect(reads[0].searchParams.get('assignment_id')).toBe('assignment-a');
  expect(reads[0].searchParams.get('repository_resource_id')).toBe('repository-a');
  await page.setViewportSize({width:375,height:812});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});
test('partial, unsupported and denied evidence stay unknown and provider text and links are escaped', async({page})=>{
  let denied=false;
  await page.route('**/api/control-plane-broker/merge-requirements?*', route=>{
    if (denied) return route.fulfill({status:403,json:{detail:'Read authority revoked'}});
    const value=response();value.item.complete=false;value.item.reviews.approving_count=null;
    value.item.independent_approval_count=null;value.item.unresolved_conversation_count=null;
    value.item.sources[0].availability='unsupported';value.item.sources[0].provider_source='<img src=x onerror=alert(1)>';
    value.item.blockers=['policy_unknown'];value.item.unsupported_rules=['future_rule'];value.item.web_url='javascript:alert(1)';
    return route.fulfill({json:value});
  });
  await mount(page);await read(page);
  const results=page.locator('[data-merge-results]');
  await expect(results).toContainText('partial / unknown observation');
  await expect(results).toContainText('Review minimum unknown · independent approvals unknown');
  await expect(results).toContainText('unresolved unknown');
  await results.locator('summary').click();
  await expect(results).toContainText('effective_rules: unsupported');
  await expect(results).toContainText('<img src=x onerror=alert(1)>');
  await expect(results.locator('img,script,a[href^="javascript:"]')).toHaveCount(0);
  denied=true;await page.getByRole('button',{name:'Inspect current merge requirements'}).click();
  await expect(page.locator('[data-merge-status]')).toContainText('access denied');
  await expect(results).toBeEmpty();
});
test('expiration visibly invalidates a retained observation without another provider request',async({page})=>{
  let reads=0;
  await page.route('**/api/control-plane-broker/merge-requirements?*',route=>{
    reads++; const value=response();value.item.expires_at=Date.now()/1000+1;
    return route.fulfill({json:value});
  });
  await mount(page);await read(page);
  await expect(page.locator('[data-merge-results]')).toContainText('complete observation');
  await expect(page.locator('[data-merge-status]')).toContainText('are stale');
  await expect(page.locator('[data-merge-results] strong')).toContainText('stale');
  expect(reads).toBe(1);
});
test('selection and Project changes fence responses even when cancellation is ignored',async({page})=>{
  await mount(page);
  await page.evaluate(()=>{const original=window.fetch;window.fetch=(url,options)=>String(url).includes('/api/control-plane-broker/merge-requirements?')
    ? new Promise(resolve=>{window.releaseMerge=value=>resolve(new Response(JSON.stringify(value)))}) :original(url,options)});
  await read(page);await expect(page.locator('[data-merge-status]')).toContainText('Reading');
  await page.getByLabel('Repository',{exact:true}).selectOption('repository-b');
  await page.evaluate(value=>releaseMerge(value),response());
  await expect(page.locator('[data-merge-results]')).toBeEmpty();
  await page.getByRole('button',{name:'Inspect current merge requirements'}).click();
  await expect(page.locator('[data-merge-status]')).toContainText('Reading');
  await page.evaluate(()=>window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'project-b'}})));
  await expect(page.locator('.control-plane-broker-status')).toHaveCount(0);
  await page.evaluate(value=>releaseMerge(value),response());
  await expect(page.locator('body')).not.toContainText('operator-a');
});
test('missing current declaration does not offer the read control',async({page})=>{
  const item=assignment();item.brokered_control_plane.assignment_context.merge_requirements_declaration.declared=false;
  await mount(page,item);
  await expect(page.getByRole('button',{name:'Inspect current merge requirements'})).toHaveCount(0);
  await expect(page.locator('.control-plane-broker-status')).toContainText('unavailable in the current pinned execution declaration');
});
