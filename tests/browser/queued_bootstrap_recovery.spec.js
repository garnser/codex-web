const {test, expect} = require('@playwright/test');

async function setup(page, {pending=false, denied=false, failed=false, deferred=false, successor=false}={}) {
  await page.goto('http://127.0.0.1:18766/static/queued_bootstrap_recovery_ui.js');
  await page.evaluate(async values => {
    document.body.innerHTML = '<main id="messages"></main>';
    window.calls=[]; window.loads=[]; window.generation=0;
    const {createQueuedBootstrapRecoveryUi} = await import('/static/queued_bootstrap_recovery_ui.js');
    const ui = createQueuedBootstrapRecoveryUi({
      captureView:()=>{const generation=window.generation; return ()=>generation===window.generation;},
      api:async(path, options)=>{
        window.calls.push({path, options});
        if (!options) {
          if (values.denied) throw Error('Administrator MFA required');
          return {queued_turn_id:'original', project_id:'project', original_actor_id:'original-actor',
            ...(values.successor ? {source_thread_id:'fenced-pending-destination'} : {}),
            agent_profile_id:'original-profile', agent_profile_revision:6,
            execution_profile_id:'repository-write', writable_repository_resource_ids:['repo'],
            read_only_repository_resource_ids:['docs'], snapshot_sha256:'a'.repeat(64),
            state:values.pending?'pending':'not_requested', can_create:!values.pending, can_reconcile:values.pending};
        }
        if (values.failed) throw Error('Bootstrap outcome unresolved');
        if (values.deferred) return new Promise(resolve=>window.resolveRecovery=resolve);
        return {ok:true, threadId:'fresh'};
      },
      loadThread:async thread=>window.loads.push(thread), scheduleRefresh:()=>{}});
    ui.attach({can_recover_bootstrap:true, bootstrap_recovery_href:'/fixture/bootstrap-recovery'},
      document.querySelector('#messages'));
  }, {pending, denied, failed, deferred, successor});
}

test('successor submission pins the inspected pending destination', async({page})=>{
  await setup(page, {successor:true});
  await page.getByRole('button', {name:'Inspect fresh bootstrap recovery'}).click();
  await expect(page.locator('pre')).toContainText('Recovery source: fenced-pending-destination');
  await page.getByLabel('Fresh bootstrap recovery reason').fill('Preserve the original and append its successor');
  await page.getByRole('button', {name:'Create fresh bootstrap', exact:true}).click();
  const posts=await page.evaluate(()=>window.calls.filter(call=>call.options));
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0].options.body).expected_source_thread_id).toBe('fenced-pending-destination');
});

test('inspection makes no mutation and explicit submission preserves the preview guard', async({page})=>{
  await setup(page);
  expect(await page.evaluate(()=>window.calls.length)).toBe(0);
  await page.getByRole('button', {name:'Inspect fresh bootstrap recovery'}).click();
  await expect(page.locator('pre')).toContainText('original-profile revision 6');
  await expect(page.locator('pre')).toContainText('Read-only repositories: docs');
  const submit=page.getByRole('button', {name:'Create fresh bootstrap', exact:true});
  await expect(submit).toBeDisabled();
  await page.getByLabel('Fresh bootstrap recovery reason').fill('Recover the exact original');
  await submit.click();
  const posts=await page.evaluate(()=>window.calls.filter(call=>call.options));
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0].options.body)).toEqual({expected_snapshot_sha256:'a'.repeat(64), reason:'Recover the exact original'});
  expect(await page.evaluate(()=>window.loads)).toEqual(['fresh']);
  await expect(page.getByRole('status')).toContainText('remains blocked until Retry');
});

test('denied preview exposes its reason without a bootstrap mutation control', async({page})=>{
  await setup(page, {denied:true});
  await page.getByRole('button', {name:'Inspect fresh bootstrap recovery'}).click();
  await expect(page.getByRole('status')).toContainText('Administrator MFA required');
  await expect(page.getByLabel('Fresh bootstrap recovery reason')).toHaveCount(0);
  expect(await page.evaluate(()=>window.calls.filter(call=>call.options).length)).toBe(0);
});

test('unconfirmed creation preserves the reason and cannot reuse an old preview', async({page})=>{
  await setup(page, {failed:true});
  await page.getByRole('button', {name:'Inspect fresh bootstrap recovery'}).click();
  await page.getByLabel('Fresh bootstrap recovery reason').fill('Original reason');
  const submit=page.getByRole('button', {name:'Create fresh bootstrap', exact:true});
  await submit.click();
  await expect(page.getByRole('status')).toContainText('Reload and reconcile');
  await page.getByLabel('Fresh bootstrap recovery reason').fill('Edited reason');
  await expect(submit).toBeDisabled();
  expect(await page.evaluate(()=>window.calls.filter(call=>call.options).length)).toBe(1);
});

test('pending bootstrap offers receipt reconciliation', async({page})=>{
  await setup(page, {pending:true});
  await page.getByRole('button', {name:'Inspect fresh bootstrap recovery'}).click();
  await expect(page.getByRole('button', {name:'Create fresh bootstrap', exact:true})).toHaveCount(0);
  await expect(page.getByRole('button', {name:'Reconcile bootstrap receipt', exact:true})).toBeDisabled();
  expect(await page.evaluate(()=>window.calls.filter(call=>call.options).length)).toBe(0);
});

test('late response cannot reload another Project view', async({page})=>{
  await setup(page, {deferred:true});
  await page.getByRole('button', {name:'Inspect fresh bootstrap recovery'}).click();
  await page.getByLabel('Fresh bootstrap recovery reason').fill('Original scope');
  await page.getByRole('button', {name:'Create fresh bootstrap', exact:true}).click();
  await page.evaluate(()=>{window.generation++; window.resolveRecovery({ok:true, threadId:'fresh'});});
  expect(await page.evaluate(()=>window.loads)).toEqual([]);
});
