const {test, expect} = require('@playwright/test');
const origin = process.env.CODEX_WEB_BROWSER_TEST_ORIGIN || 'http://127.0.0.1:18766';
const assignment = (project='project-a', id='assignment-a') => ({
  id, project_id:project, execution_id:'execution-a', execution_profile_id:'orchestration-admin',
  work_item_ref:'group/source#1', brokered_control_plane:{enabled:true,
    operations:[{id:'repository.issue.create'},{id:'work_graph.edge.create'},
      {id:'repository.read.delivery-evidence',path_template:'/api/action-intents/{intent_id}/delivery-evidence',
        supported_action_ids:['code-host.branch.publish','code-host.pull-request.upsert','<img src=x onerror=alert(1)>']}], assignment_context:{
      agent_profile:{profile_id:'orchestrator',profile_revision:8,profile_record_id:'profile-eight',role_id:'orchestrator'},
      declared_repository_scope:{writable_repository_ids:[],read_only_repository_ids:[]},
      declared_administrative_scope:{repository_ids:['repository-a','<img src=x onerror=alert(1)>'],
        definition:{record_id:'execution-definition',definition_id:'execution.profiles.default',revision:8},
        authority_role_id:'ops-admin',authority_definition:{record_id:'role-definition',definition_id:'authority.roles.default',revision:3},
        requester_identity_id:'assignment-requester',definition_is_current:true,
        current_agent_profile:{profile_id:'orchestrator',revision:8,record_id:'profile-eight',lifecycle:'active',role_id:'orchestrator',authority_role_id:'ops-admin'},
        configuration_observation:{status:'valid',reason:'Declaration checks passed; each operation still requires live authorization',operation_id:'repository.issue.create',repository_id:'repository-a'}}}}});
const audit = [{id:'audit-a',project_id:'project-a',assignment_id:'assignment-a',operation_id:'work_item.read',
  target_ref:'group/target#2',decision:'deny',response_status:403,worker_id:'worker-a',fence:7,
  actor_identity_id:'authenticated-worker',denial_reason:'lease fence expired <script>alert(1)</script>',correlation_id:'correlation-a'}];
async function mountBroker(page) {
  await page.route('**/broker-inspection-fixture', route=>route.fulfill({contentType:'text/html',body:
    '<!doctype html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/design_tokens.css"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/workspace_components.css"><style>html,body{height:auto;overflow:auto}body{display:block;padding:16px;box-sizing:border-box}</style></head><body data-project-id="project-a"><details open><summary>Assignments</summary><div data-execution-assignment-management-host="assignment-a"></div><div data-execution-assignment-management-host="assignment-b"></div></details><script src="/static/control_plane_broker_admin.js"></script></body>'}));
  await page.goto(origin+'/broker-inspection-fixture');
}
async function emit(page,item) {
  await expect.poll(async()=>{
    await page.evaluate(value=>window.dispatchEvent(new CustomEvent('codex:execution-worker-state-rendered',{detail:{assignments:[value]}})),item);
    return page.locator('.control-plane-broker-status').count();
  }).toBe(1);
}
test('administrative declaration, pinned attribution and actual denied audit remain distinct and escaped',async({page})=>{
  const reads=[];
  await page.route('**/api/control-plane-broker/audit?*',route=>{reads.push(route.request().url());return route.fulfill({json:{items:audit}})});
  await mountBroker(page); await emit(page,assignment());
  const panel=page.getByRole('region',{name:'Broker scope and outcomes'});
  await expect(panel).toContainText('orchestrator@r8');
  await expect(panel).toContainText('authority.roles.default@r3');
  await expect(panel).toContainText('assignment-requester');
  await expect(panel).toContainText('Git scope: writable none');
  await expect(panel).toContainText('Delivery evidence: read-only');
  await expect(panel).toContainText('code-host.pull-request.upsert');
  await expect(panel).toContainText('/api/action-intents/{intent_id}/delivery-evidence');
  await expect(panel).toContainText('missing receipts or timestamps alone do not prove provider outcome');
  await expect(panel.locator('img,script')).toHaveCount(0);
  await expect(panel.getByRole('link',{name:'View resource: repository-a',exact:true}).first()).toHaveAttribute('href','/projects/project-a/resources?consumer_type=resource&consumer_id=repository-a');
  expect(reads).toEqual([]);
  const button=panel.getByRole('button'); await button.focus(); await page.keyboard.press('Enter');
  await expect(panel).toContainText('Latest 1 recorded broker outcomes');
  await panel.locator('[data-broker-events] summary').click();
  await expect(panel).toContainText('authenticated broker actor: authenticated-worker');
  await expect(panel).toContainText('lease fence expired <script>alert(1)</script>');
  await expect(panel.locator('img,script')).toHaveCount(0);
  await expect(panel.getByRole('link',{name:'View work item: group/target#2'})).toHaveAttribute('href','/projects/project-a/work-items?consumer_type=work_item&consumer_id=group%2Ftarget%232');
  await expect(panel.getByRole('link',{name:'Inspect related ActionIntents and provider receipts'})).toHaveAttribute('href','/projects/project-a/automations?action_execution=execution-a');
  expect(new URL(reads[0]).searchParams.get('assignment_id')).toBe('assignment-a');
  expect(new URL(reads[0]).searchParams.get('limit')).toBe('20');
  await page.setViewportSize({width:375,height:812});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});
test('changed definition/profile and missing capability are shown as observations without granting controls',async({page})=>{
  await mountBroker(page); const item=assignment();
  const admin=item.brokered_control_plane.assignment_context.declared_administrative_scope;
  admin.definition_is_current=false; admin.current_agent_profile.revision=9;admin.current_agent_profile.lifecycle='disabled';
  admin.configuration_observation={status:'blocked',reason:'administrative Agent Profile is disabled or its binding changed'};
  await emit(page,item);
  const panel=page.locator('.control-plane-broker-status');
  await expect(panel).toContainText('changed / stale pin');await expect(panel).toContainText('orchestrator@r9');
  await expect(panel).toContainText('lifecycle disabled');await expect(panel).toContainText('validation: blocked');
  delete admin.current_agent_profile;delete admin.configuration_observation;
  admin.inspection_error='pinned administrative definition is unavailable';item.brokered_control_plane.operations=[];
  await emit(page,item);await expect(panel).toContainText('Current Agent Profile: unavailable');
  await expect(panel).toContainText(admin.inspection_error);await expect(panel).toContainText('Declared operation allowlist: none');
});
test('Project change fences an old audit response even when transport ignores cancellation',async({page})=>{
  await mountBroker(page);await emit(page,assignment());
  await page.evaluate(()=>{const original=window.fetch;window.fetch=(url,options)=>String(url).includes('/api/control-plane-broker/audit')
    ? new Promise(resolve=>{window.releaseAudit=()=>resolve(new Response(JSON.stringify({items:[{project_id:'project-a',assignment_id:'assignment-a',operation_id:'old-private-outcome'}]})))})
    : original(url,options)});
  await page.getByRole('button').click();await expect(page.locator('[role=status]')).toHaveText('Loading canonical broker audit…');
  await page.evaluate(()=>window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'project-b'}})));
  await expect(page.locator('.control-plane-broker-status')).toHaveCount(0);
  await emit(page,assignment('project-b','assignment-b'));
  await page.evaluate(async()=>{releaseAudit();await new Promise(resolve=>setTimeout(resolve,0));});
  await expect(page.locator('.control-plane-broker-status')).toContainText('Project project-b');
  await expect(page.locator('.control-plane-broker-status')).not.toContainText('old-private-outcome');
  await expect(page.locator('[data-execution-assignment-management-host="assignment-a"]')).toBeEmpty();
});
test('audit denial remains unknown and can retry a successful empty read',async({page})=>{
  let denied=true;await page.route('**/api/control-plane-broker/audit?*',route=>denied
    ? route.fulfill({status:403,json:{detail:'Role revoked'}}):route.fulfill({json:{items:[]}}));
  await mountBroker(page);await emit(page,assignment());await page.getByRole('button').click();
  await expect(page.locator('[role=status]')).toContainText('Broker audit access denied: Role revoked');
  await expect(page.locator('[role=status]')).toContainText('Outcomes remain unknown');
  denied=false;await page.getByRole('button').click();await expect(page.locator('[role=status]')).toHaveText('No broker audit events recorded for this assignment.');
});
const intent = (id='intent-a',project='project-a')=>({id,project_id:project,execution_id:'execution-a',action_id:'code-host.issue.create',
  status:'requires_reconciliation',resource_ids:['repository-a'],requested_by:'requester',request:{parameters:{title:'PRIVATE_BODY_NOT_FOR_UI'}},
  retry_policy:{max_attempts:1},attempt:1,last_receipt_id:'receipt-a',last_verification_id:'verification-a',work_item_ref:'group/source#1'});
async function mountIntents(page, responses={}) {
  await page.route('**/intent-inspection-fixture*',route=>route.fulfill({contentType:'text/html',body:
    '<!doctype html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/design_tokens.css"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/workspace_components.css"><style>html,body{height:auto;overflow:auto}body{display:block;padding:16px;box-sizing:border-box}</style></head><body data-project-id="project-a"><details id="developer-panel" open><summary>Actions</summary><button id="refresh-action-intents">Refresh</button><div id="action-intent-status" role="status"></div><div id="action-intent-recovery-assurance"></div><button id="recover-stale-action-intents">Recover</button><div id="action-intent-list"></div></details><script src="/static/action_intent_admin.js"></script></body>'}));
  await page.route('**/api/**',route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/action-intents')return responses.intents?.(route)||route.fulfill({json:{items:[intent(),intent('foreign','project-b'),{...intent('other-execution'),execution_id:'execution-other'}]}});
    if(path==='/api/identity/me')return route.fulfill({json:{identity_id:'operator',roles:['admin'],assurance:'mfa',principal_kind:'human'}});
    return route.fulfill({json:{receipts:[{id:'receipt-a',outcome:'unknown',attempt:1,result:{status:'uncertain',output:{secret:'PRIVATE_RECEIPT_NOT_FOR_UI'}}}],verifications:[{id:'verification-a',verified:false,findings:['Outcome unknown']}],inbox:[]}});
  });
  await page.goto(origin+'/intent-inspection-fixture?action_execution=execution-a');
}
test('execution-linked ActionIntents show canonical selected resources and uncertain provider receipts',async({page})=>{
  await mountIntents(page);const row=page.locator('[data-action-intent-row="intent-a"]');
  await expect(row).toBeVisible();await expect(page.locator('[data-action-intent-row="foreign"],[data-action-intent-row="other-execution"]')).toHaveCount(0);
  await row.locator('summary').click();await expect(row.getByRole('link',{name:'View resource: repository-a'})).toHaveAttribute('href','/projects/project-a/resources?consumer_type=resource&consumer_id=repository-a');
  const button=row.getByRole('button',{name:'Load side-effect timeline'});await button.focus();await page.keyboard.press('Enter');
  await expect(row).toContainText('Provider receipt · attempt 1 · unknown');await expect(row).toContainText('Verification receipt · not verified');
  await expect(row).not.toContainText('PRIVATE_BODY_NOT_FOR_UI');await expect(row).not.toContainText('PRIVATE_RECEIPT_NOT_FOR_UI');
});
test('Project switching clears old ActionIntents and fences a pending refresh',async({page})=>{
  let held=false,release;
  await mountIntents(page,{intents:route=>held ? new Promise(resolve=>{release=()=>{route.fulfill({json:{items:[intent('late-old')]}}).then(resolve)}}):route.fulfill({json:{items:[intent()]}})});
  await expect(page.locator('[data-action-intent-row]')).toHaveCount(1);held=true;
  await page.getByRole('button',{name:'Refresh',exact:true}).click();await expect.poll(()=>Boolean(release)).toBe(true);
  await page.evaluate(()=>window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'project-b'}})));
  await expect(page.locator('[data-action-intent-row]')).toHaveCount(0);
  const resumed=page.waitForResponse(response=>response.url().includes('/api/action-intents') && response.status()===200);
  release();await (await resumed).finished();
  await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,0)));
  await expect(page.locator('#action-intent-status')).toContainText('Project changed');await expect(page.locator('#recover-stale-action-intents')).toBeDisabled();
  await expect(page.locator('[data-action-intent-row]')).toHaveCount(0);
});
test('denied ActionIntent reads disable stale actor controls and preserve outcome uncertainty',async({page})=>{
  await mountIntents(page,{intents:route=>route.fulfill({status:403,json:{detail:'Role binding revoked'}})});
  await expect(page.locator('#action-intent-status')).toContainText('access denied: Role binding revoked');
  await expect(page.locator('#action-intent-status')).toContainText('Provider outcomes are unknown');
  await expect(page.locator('#recover-stale-action-intents')).toBeDisabled();
  await expect(page.locator('#action-intent-recovery-assurance')).toContainText('has not been loaded');
});

test('unchanged worker redraw preserves a pending audit and its completed history',async({page})=>{
  await mountBroker(page);const item=assignment();await emit(page,item);
  await page.evaluate(events=>{const original=window.fetch;window.fetch=(url,options)=>String(url).includes('/api/control-plane-broker/audit')
    ? new Promise(resolve=>{window.releaseAudit=()=>resolve(new Response(JSON.stringify({items:events})))})
    : original(url,options)},audit);
  await page.getByRole('button').click();
  await page.evaluate(value=>{
    document.querySelector('details').innerHTML='<summary>Assignments</summary><div data-execution-assignment-management-host="assignment-a"></div>';
    window.dispatchEvent(new CustomEvent('codex:execution-worker-state-rendered',{detail:{assignments:[value]}}));
  },item);
  await expect(page.locator('.control-plane-broker-status')).toHaveAttribute('aria-busy','true');
  await expect(page.locator('[role=status]')).toHaveText('Loading canonical broker audit…');
  await page.evaluate(async()=>{releaseAudit();await new Promise(resolve=>setTimeout(resolve,0));});
  await expect(page.locator('[role=status]')).toContainText('Latest 1 recorded broker outcomes');
  await page.evaluate(value=>window.dispatchEvent(new CustomEvent('codex:execution-worker-state-rendered',{detail:{assignments:[value]}})),item);
  await expect(page.locator('[role=status]')).toContainText('Latest 1 recorded broker outcomes');
  await expect(page.locator('.control-plane-broker-status')).toHaveCount(1);
});
test('changed pinned/current profile replaces the panel and refuses the old audit response',async({page})=>{
  await mountBroker(page);const item=assignment();await emit(page,item);
  await page.evaluate(()=>{const original=window.fetch;window.fetch=(url,options)=>String(url).includes('/api/control-plane-broker/audit')
    ? new Promise(resolve=>{window.releaseAudit=()=>resolve(new Response(JSON.stringify({items:[{project_id:'project-a',assignment_id:'assignment-a',operation_id:'old-profile-private-outcome'}]})))})
    : original(url,options)});
  await page.getByRole('button').click();
  item.brokered_control_plane.assignment_context.agent_profile.profile_revision=9;
  item.brokered_control_plane.assignment_context.declared_administrative_scope.current_agent_profile.revision=9;
  await emit(page,item);
  await page.evaluate(async()=>{releaseAudit();await new Promise(resolve=>setTimeout(resolve,0));});
  await expect(page.locator('.control-plane-broker-status')).toContainText('orchestrator@r9');
  await expect(page.locator('[role=status]')).toHaveText('Broker audit has not been loaded.');
  await expect(page.locator('.control-plane-broker-status')).not.toContainText('old-profile-private-outcome');
});

test('filtered assignment drops its pending read before the same row returns',async({page})=>{
  await mountBroker(page);const item=assignment();await emit(page,item);
  await page.evaluate(()=>{const original=window.fetch;window.fetch=(url,options)=>String(url).includes('/api/control-plane-broker/audit')
    ? new Promise(resolve=>{window.releaseAudit=()=>resolve(new Response(JSON.stringify({items:[{project_id:'project-a',assignment_id:'assignment-a',operation_id:'filtered-old-result'}]})))})
    : original(url,options)});
  await page.getByRole('button').click();
  await page.evaluate(value=>{
    document.querySelector('details').innerHTML='<summary>Assignments</summary>';
    window.dispatchEvent(new CustomEvent('codex:execution-worker-state-rendered',{detail:{assignments:[value]}}));
  },item);
  await expect(page.locator('.control-plane-broker-status')).toHaveCount(0);
  await page.evaluate(()=>document.querySelector('details').insertAdjacentHTML('beforeend','<div data-execution-assignment-management-host="assignment-a"></div>'));
  await emit(page,item);
  await page.evaluate(async()=>{releaseAudit();await new Promise(resolve=>setTimeout(resolve,0));});
  await expect(page.locator('[role=status]')).toHaveText('Broker audit has not been loaded.');
  await expect(page.locator('.control-plane-broker-status')).not.toContainText('filtered-old-result');
});

test('unchanged redraw keeps the expanded assignment visible while its audit completes',async({page})=>{
  await page.route('**/api/control-plane-broker/audit?*',route=>route.fulfill({json:{items:audit}}));
  await mountBroker(page);const item=assignment();await emit(page,item);
  await page.evaluate(value=>{
    document.querySelector('details').outerHTML='<details><summary>Assignments</summary><div data-execution-assignment-management-host="assignment-a"></div></details>';
    window.dispatchEvent(new CustomEvent('codex:execution-worker-state-rendered',{detail:{assignments:[value]}}));
  },item);
  await expect(page.locator('.control-plane-broker-status')).toBeVisible();
  await page.getByRole('button').click();
  await expect(page.locator('[role=status]')).toContainText('Latest 1 recorded broker outcomes');
});

test('worker administration loaded after DOM readiness still initializes and binds refresh',async({page})=>{
  let reads=0;
  await page.route('**/worker-late-fixture',route=>route.fulfill({contentType:'text/html',body:
    '<meta charset="utf-8"><body data-project-id="project-a"><details id="developer-panel" open><summary>Developer</summary><button id="refresh-execution-workers">Refresh workers</button><div id="execution-worker-status"></div><div id="execution-worker-list"></div><div id="execution-assignment-list"></div><div id="execution-worker-events"></div></details></body>'}));
  await page.route('**/api/execution-workers**',route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/assignments')){reads++;return route.fulfill({json:{items:[{...assignment(),subject:{kind:'work_item',ref:'group/source#1'},resource_ids:[],required_capabilities:[],status:'cancelled'}]}})}
    return route.fulfill({json:{items:[]}});
  });
  await page.goto(origin+'/worker-late-fixture');
  await page.addScriptTag({url:origin+'/static/execution_worker_admin.js'});
  await expect(page.locator('[data-execution-assignment-management-host="assignment-a"]')).toHaveCount(1);
  const before=reads;await page.getByRole('button',{name:'Refresh workers'}).click();
  await expect.poll(()=>reads).toBeGreaterThan(before);
});


test('broker receipt link opens the routed workspace containing the ActionIntent timeline',async({page})=>{
  const fs=require('node:fs');
  const fixture=fs.readFileSync(require.resolve('./product_workspaces_fixture.html'),'utf8')
    .replace('<h2>Execution Workers</h2>', '<h2>Execution Workers</h2><div data-execution-assignment-management-host="assignment-a"></div>')
    .replace('<div class="developer-card" id="daemon-card">', '<div class="developer-card"><h2>ActionIntents &amp; Side Effects</h2><button id="refresh-action-intents">Refresh</button><div id="action-intent-status"></div><div id="action-intent-recovery-assurance"></div><button id="recover-stale-action-intents">Recover</button><div id="action-intent-list"></div></div><div class="developer-card" id="daemon-card">')
    .replace('</body>', '<script src="/static/control_plane_broker_admin.js"></script><script src="/static/action_intent_admin.js"></script></body>');
  await page.route('**/projects/home/*',route=>route.fulfill({contentType:'text/html',body:fixture}));
  await page.route('**/api/**',route=>{
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/action-intents')return route.fulfill({json:{items:[intent('intent-a','home')]}});
    if(path==='/api/identity/me')return route.fulfill({json:{identity_id:'operator',roles:['admin'],assurance:'mfa',principal_kind:'human'}});
    return route.fulfill({json:{receipts:[{id:'receipt-a',outcome:'unknown',attempt:1}],verifications:[{id:'verification-a',verified:false}],inbox:[]}});
  });
  await page.goto(origin+'/projects/home/workers');
  await expect(page.locator('[data-product-workspace-host="workers"]')).toBeVisible();
  await emit(page,assignment('home'));
  await page.getByRole('link',{name:'Inspect related ActionIntents and provider receipts'}).click();
  const row=page.locator('[data-action-intent-row="intent-a"]');
  await expect(page.locator('#refresh-action-intents')).toBeVisible();
  await expect(row).toBeVisible();
  await row.locator('summary').click();
  await row.getByRole('button',{name:'Load side-effect timeline'}).click();
  await expect(row).toContainText('receipt-a');
  await expect(row).toContainText('verification-a');
  await expect(row).toContainText('unknown');
});


test('broker dependency graph link opens its Project work workspace',async({page})=>{
  const fs=require('node:fs');
  const fixture=fs.readFileSync(require.resolve('./product_workspaces_fixture.html'),'utf8')
    .replace('id="work-card"','id="work-graph-panel"')
    .replace('<h2>Execution Workers</h2>', '<h2>Execution Workers</h2><div data-execution-assignment-management-host="assignment-a"></div>')
    .replace('</body>', '<script src="/static/control_plane_broker_admin.js"></script></body>');
  await page.route('**/projects/home/*',route=>route.fulfill({contentType:'text/html',body:fixture}));
  await page.goto(origin+'/projects/home/workers');
  await expect(page.locator('[data-product-workspace-host="workers"]')).toBeVisible();
  await emit(page,assignment('home'));
  const link=page.getByRole('link',{name:'Inspect dependency graph',exact:true});
  await link.focus();await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/projects\/home\/work-items/);
  await expect(page.locator('#work-graph-panel')).toBeVisible();
  await expect(page.locator('#refresh-work-graph')).toHaveAttribute('data-clicked', /[1-9]/);
});


async function mountLateRoutedInspector(page,kind) {
  await page.route('**/routed-inspector-late-fixture',route=>route.fulfill({contentType:'text/html',body:
    `<meta charset="utf-8"><body data-project-id="project-a"><details id="developer-panel"><summary>Developer</summary></details>
     <section data-product-workspace-panel="${kind}">
      <button id="refresh-execution-workers">Refresh workers</button><div id="execution-worker-status"></div><div id="execution-worker-list"></div><div id="execution-assignment-list"></div><div id="execution-worker-events"></div>
      <button id="refresh-action-intents">Refresh actions</button><div id="action-intent-status"></div><div id="action-intent-recovery-assurance"></div><button id="recover-stale-action-intents">Recover</button><div id="action-intent-list"></div>
     </section></body>`}));
  await page.route('**/api/**',route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/assignments'))return route.fulfill({json:{items:[{...assignment(),subject:{kind:'work_item',ref:'group/source#1'},resource_ids:[],required_capabilities:[],status:'cancelled'}]}});
    if(path==='/api/action-intents')return route.fulfill({json:{items:[intent(),intent('intent-b','project-b')]}});
    if(path==='/api/identity/me')return route.fulfill({json:{identity_id:'operator',roles:['admin'],assurance:'mfa',principal_kind:'human'}});
    return route.fulfill({json:{items:[]}});
  });
  await page.goto(origin+'/routed-inspector-late-fixture');
  await page.addScriptTag({url:origin+'/static/'+(kind==='workers'?'execution_worker_admin':'action_intent_admin')+'.js'});
}

test('late worker controller loads an already-open routed workspace with Developer closed',async({page})=>{
  await mountLateRoutedInspector(page,'workers');
  await expect(page.locator('[data-execution-assignment-management-host="assignment-a"]')).toHaveCount(1);
  await expect(page.locator('#developer-panel')).not.toHaveAttribute('open','');
});

test('late ActionIntent controller loads an already-open routed workspace with Developer closed',async({page})=>{
  await mountLateRoutedInspector(page,'autonomy');
  await expect(page.locator('[data-action-intent-row="intent-a"]')).toBeVisible();
  await expect(page.locator('[data-action-intent-row="intent-b"]')).toHaveCount(0);
  await expect(page.locator('#developer-panel')).not.toHaveAttribute('open','');
});

test('visible ActionIntent workspace reloads canonical scope after a Project change',async({page})=>{
  await mountLateRoutedInspector(page,'autonomy');
  await expect(page.locator('[data-action-intent-row="intent-a"]')).toBeVisible();
  await page.evaluate(()=>window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'project-b'}})));
  await expect(page.locator('[data-action-intent-row="intent-b"]')).toBeVisible();
  await expect(page.locator('[data-action-intent-row="intent-a"]')).toHaveCount(0);
});

test('repository CREATE conflict displays canonical 409 denial and safe guidance without inferring provider outcome',async({page})=>{
  const message='Repository action admission conflicts with origin, scope or idempotency. Inspect authorized intent and delivery evidence; an existing intent may have executed or be uncertain. Do not automatically retry or replay.';
  const correlation='repository-admission-conflict';
  const entry={...audit[0],operation_id:'repository.pull-request.upsert',target_ref:null,
    decision:'deny',response_status:409,denial_reason:message,correlation_id:correlation};
  let mutationCalls=0;
  await page.route('**/api/repository-actions/**',route=>{
    mutationCalls+=1;return route.fulfill({status:409,json:{error:{
      code:'repository_action_admission_conflict',message,correlation_id:correlation}}});
  });
  await page.route('**/api/control-plane-broker/audit?*',route=>route.fulfill({json:{items:[entry]}}));
  await mountBroker(page);await emit(page,assignment());
  const panel=page.getByRole('region',{name:'Broker scope and outcomes'});
  await panel.getByRole('button').click();
  await expect(panel.locator('[data-broker-events] summary')).toHaveText('repository.pull-request.upsert · deny · HTTP 409');
  await panel.locator('[data-broker-events] summary').click();
  await expect(panel).toContainText(message);
  await expect(panel).toContainText(`correlation ${correlation}`);
  await expect(panel).toContainText('A broker admission is distinct from a provider result');
  await expect(panel.getByRole('link',{name:'Inspect related ActionIntents and provider receipts'})).toBeVisible();
  await expect(panel).not.toContainText('never executed');
  expect(mutationCalls).toBe(0);
  await page.setViewportSize({width:375,height:812});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});
