const {test, expect} = require('@playwright/test');

async function setup(page, {denied=false, fail=false, unknown=false, deferred=false}={}) {
  await page.goto('http://127.0.0.1:18766/static/queued_operator_admission_ui.js');
  await page.evaluate(async values => {
    document.body.innerHTML='<main id="messages"></main>';
    window.calls=[];window.loads=[];window.generation=0;
    const {createQueuedOperatorAdmissionUi}=await import('/static/queued_operator_admission_ui.js');
    const ui=createQueuedOperatorAdmissionUi({captureView:()=>{const gen=window.generation;return ()=>gen===window.generation;},
      api:async(path,options)=>{
        window.calls.push({path,options});
        if (!options) {
          if (values.denied) throw Error('Current Role authority denied');
          return {queued_turn_id:'original',project_id:'project',source:'gitlab',original_actor_id:'original-actor',
            agent_profile_id:'profile',agent_profile_revision:7,execution_profile_id:'orchestration-only',
            writable_repository_resource_ids:[],read_only_repository_resource_ids:['docs'],reply_target:{channel:'fixture'},
            snapshot_sha256:'a'.repeat(64),queue_sha256:'b'.repeat(64)};
        }
        if (values.fail) throw Error('Reviewed queue changed');
        if (values.deferred) return new Promise(resolve=>window.resolveAdmission=resolve);
        return {threadId:'thread',attempt:{status:values.unknown?'reconciliation_required':'started',
          queued_start_receipt:values.unknown?null:{provider_native_turn_id:'native-actual'}}};
      },loadThread:async id=>window.loads.push(id),scheduleRefresh:()=>{}});
    ui.attach({can_admit_original:true,operator_admission_href:'/fixture/operator-admission'},document.querySelector('#messages'));
  },{denied,fail,unknown,deferred});
}

test('single original needs explicit reviewed scope and a reason',async({page})=>{
  await setup(page);
  expect(await page.evaluate(()=>window.calls.length)).toBe(0);
  await page.getByRole('button',{name:'Review single original admission'}).click();
  await expect(page.locator('pre')).toContainText('profile revision 7');
  await expect(page.locator('pre')).toContainText('Source: gitlab');
  await expect(page.getByRole('status')).toContainText('Automation stays paused');
  const submit=page.getByRole('button',{name:'Admit this original once'});
  await expect(submit).toBeDisabled();
  await page.getByLabel('Single original admission reason').fill('Reviewed original');
  await submit.click();
  await expect(page.getByRole('status')).toContainText('Native start confirmed: native-actual');
  const posts=await page.evaluate(()=>window.calls.filter(call=>call.options));
  expect(posts).toHaveLength(1);
  expect(JSON.parse(posts[0].options.body)).toEqual({expected_snapshot_sha256:'a'.repeat(64),expected_queue_sha256:'b'.repeat(64),reason:'Reviewed original'});
});

test('denied inspection exposes no native mutation action',async({page})=>{
  await setup(page,{denied:true});
  await page.getByRole('button',{name:'Review single original admission'}).click();
  await expect(page.getByRole('status')).toContainText('Current Role authority denied');
  await expect(page.getByLabel('Single original admission reason')).toHaveCount(0);
  expect(await page.evaluate(()=>window.calls.filter(call=>call.options).length)).toBe(0);
});

test('stale or refused submission retains its draft and cannot submit twice',async({page})=>{
  await setup(page,{fail:true});
  await page.getByRole('button',{name:'Review single original admission'}).click();
  await page.getByLabel('Single original admission reason').fill('Keep this review');
  await page.getByRole('button',{name:'Admit this original once'}).click();
  await expect(page.getByRole('status')).toContainText('Reviewed queue changed');
  await expect(page.getByLabel('Single original admission reason')).toHaveValue('Keep this review');
  await page.getByLabel('Single original admission reason').fill('Edited review');
  await expect(page.getByRole('button',{name:'Admit this original once'})).toBeDisabled();
  expect(await page.evaluate(()=>window.calls.filter(call=>call.options).length)).toBe(1);
});

test('unknown result never presents a native start as confirmed',async({page})=>{
  await setup(page,{unknown:true});
  await page.getByRole('button',{name:'Review single original admission'}).click();
  await page.getByLabel('Single original admission reason').fill('Reviewed original');
  await page.getByRole('button',{name:'Admit this original once'}).click();
  await expect(page.getByRole('status')).toContainText('not confirmed (reconciliation_required)');
});

test('late result cannot refresh a different Project view',async({page})=>{
  await setup(page,{deferred:true});
  await page.getByRole('button',{name:'Review single original admission'}).click();
  await page.getByLabel('Single original admission reason').fill('Reviewed original');
  await page.getByRole('button',{name:'Admit this original once'}).click();
  await page.evaluate(()=>{window.generation++;window.resolveAdmission({threadId:'old',attempt:{status:'started'}});});
  expect(await page.evaluate(()=>window.loads)).toEqual([]);
});
