import assert from 'node:assert/strict';
import test from 'node:test';
import {canAdmitDerivedArtifact, derivedArtifactRequest} from '../../static/derived_artifact_request.js';
const values = () => ({project_id:'project-a',work_item_ref:'group/app#340',resource_ids:'repo-a',
  source_evidence_ids:'evidence-approved',name:'Approved input',digest:'a'.repeat(64),idempotency_key:'input-1'});
test('builds explicit canonical scope, exact digest and source request without unrelated metadata', () => {
  const input = {...values(),source_evidence_ids:'evidence-approved, evidence-approved\nevidence-second',
    secret_value:'DO_NOT_FORWARD',metadata:'UNTRUSTED_METADATA',_commit:'FORBIDDEN'};
  const body = derivedArtifactRequest(input);
  assert.deepEqual(body.source_evidence_ids,['evidence-approved','evidence-second']);
  assert.equal(body.artifact.work_item_ref,'group/app#340');
  assert.deepEqual(body.artifact.resource_ids,['repo-a']);
  assert.equal(body.artifact.digest.value,'a'.repeat(64));
  assert.equal(body.idempotency_key,'input-1');
  for (const value of ['DO_NOT_FORWARD','UNTRUSTED_METADATA','FORBIDDEN']) assert.ok(!JSON.stringify(body).includes(value));
  assert.equal(body.artifact.execution_id,undefined);
});
test('refuses malformed digest, missing scope, excessive parents and unstable key', () => {
  for (const update of [{digest:'<script>'},{project_id:''},{work_item_ref:''},{resource_ids:''},
    {source_evidence_ids:''},{source_evidence_ids:Array.from({length:65},(_,i)=>'e'+i).join(',')},
    {idempotency_key:''},{idempotency_key:'x'.repeat(201)}]) {
    assert.throws(()=>derivedArtifactRequest({...values(),...update}));
  }
});
test('client affordance follows canonical admin assurance; no worker or plain member grant', () => {
  assert.equal(canAdmitDerivedArtifact(null),false);
  assert.equal(canAdmitDerivedArtifact({principal_kind:'human',roles:['admin'],assurance:'primary'}),false);
  assert.equal(canAdmitDerivedArtifact({principal_kind:'human',roles:['member'],assurance:'mfa'}),false);
  assert.equal(canAdmitDerivedArtifact({principal_kind:'human',roles:['admin'],assurance:'mfa'}),true);
  assert.equal(canAdmitDerivedArtifact({principal_kind:'human',roles:['owner'],assurance:'local_trusted'}),true);
  assert.equal(canAdmitDerivedArtifact({principal_kind:'service',service_scopes:[]}),false);
  assert.equal(canAdmitDerivedArtifact({principal_kind:'service',service_scopes:['artifact-evidence:admin']}),true);
});
