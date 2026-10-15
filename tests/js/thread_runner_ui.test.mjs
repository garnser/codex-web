import assert from 'node:assert/strict';
import test from 'node:test';
import {applyRunner, runnerFor, allowsRuntimeActivity, runnerLabel, runnerFailureText} from '../../static/thread_runner_ui.js';

const live = (id, created=10, fence=2, observed=20) => ({schemaVersion:'1.0',state:'running',live:true,
  assignmentId:id,assignmentCreatedAt:created,fence,updatedAt:observed,observedAt:observed,
  leaseValid:true,leaseExpiresAt:Date.now()/1000+3600});

test('cancelled custody cannot be replaced by older active snapshot',()=>{
  const runner={...live('old'),state:'cancelled',live:false,leaseValid:false};
  applyRunner({id:'cancelled',runner});
  applyRunner({id:'cancelled',runner:live('old',10,1,19)});
  assert.equal(allowsRuntimeActivity('cancelled'),false);
  assert.equal(runnerLabel('cancelled'),'Runner cancelled');
});
test('new assignment supersedes older custody without comparing unrelated fences',()=>{
  applyRunner({id:'rebound',runner:live('old',10,5,20)});
  applyRunner({id:'rebound',runner:live('new',30,1,40)});
  applyRunner({id:'rebound',runner:live('old',10,6,50)});
  assert.equal(runnerFor('rebound').assignmentId,'new');
  assert.equal(allowsRuntimeActivity('rebound'),true);
});
test('unavailable current evidence and expired leases cannot indicate live work',()=>{
  applyRunner({id:'unavailable',runner:live('active')});
  applyRunner({id:'unavailable',runner:{schemaVersion:'1.0',state:'unknown',live:false,observedAt:30}});
  assert.equal(allowsRuntimeActivity('unavailable'),false);
  applyRunner({id:'expired',runner:{...live('expired'),leaseExpiresAt:0}});
  assert.equal(allowsRuntimeActivity('expired'),false);
  assert.equal(runnerLabel('expired'),'Unknown runner · lease expired');
});

test('canonical refresh restores waiting and renewed leases without trusting an event', async()=>{
  const {createRunnerRefresh} = await import('../../static/thread_runner_ui.js');
  let current={...live('renewed',100,1,100),state:'waiting',live:false};
  let reads=0;
  const refresh=createRunnerRefresh({delay:5,read:async id=>{reads++;return {id,runner:current};},apply:()=>{}});
  applyRunner({id:'refresh',runner:current});
  assert.equal(allowsRuntimeActivity('refresh'),false);
  current=live('renewed',100,1,101);
  refresh('refresh');refresh('refresh');
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(reads,1);
  assert.equal(allowsRuntimeActivity('refresh'),true);
  current={...live('renewed',100,1,102),leaseExpiresAt:0};
  applyRunner({id:'refresh',runner:current});
  assert.equal(allowsRuntimeActivity('refresh'),false);
  current=live('renewed',100,2,103);
  refresh('refresh');await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(allowsRuntimeActivity('refresh'),true);
  applyRunner({id:'refresh',runner:live('renewed',100,1,104)});
  assert.equal(runnerFor('refresh').fence,2);
});

test('failed refresh is explicit unknown and retains assignment ordering watermark',async()=>{
  const {createRunnerRefresh} = await import('../../static/thread_runner_ui.js');
  const now=Date.now()/1000;
  applyRunner({id:'failed-refresh',runner:live('current',200,2,now-2)});
  const refresh=createRunnerRefresh({delay:5,read:async()=>{throw Error('unavailable');},apply:()=>{}});
  refresh('failed-refresh');await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(runnerLabel('failed-refresh'),'Unknown runner');
  applyRunner({id:'failed-refresh',runner:live('superseded',100,9,now+1)});
  assert.equal(runnerFor('failed-refresh').assignmentId,'current');
  assert.equal(allowsRuntimeActivity('failed-refresh'),false);
  applyRunner({id:'failed-refresh',runner:live('current',200,3,now+2)});
  assert.equal(allowsRuntimeActivity('failed-refresh'),true);
});

test('same-fence failure explains phase cleanup and unknown outcome without changing custody',()=>{
  const runner={...live('failed-observation'),live:false,state:'stopped',diagnosticState:'unavailable',
    failureObservation:{assignment_id:'failed-observation',fence:2,code:'execution_timeout',phase:'validation',
      cleanup_state:'stopped',timings:{validation_wait_seconds:0.125}}};
  applyRunner({id:'failure-detail',runner});
  assert.match(runnerFailureText(runner),/phase validation/);
  assert.match(runnerFailureText(runner),/validation_wait_seconds: 0.125s/);
  assert.match(runnerFailureText(runner),/unfinished work outcome unknown/);
  assert.match(runnerFailureText(runner),/reconcile provider receipts before recovery/);
  assert.equal(allowsRuntimeActivity('failure-detail'),false);
  assert.equal(runnerFailureText({...runner,fence:3}),'');
});
