import assert from 'node:assert/strict';
import test from 'node:test';
globalThis.location = {pathname:'/projects/project-a/operations',search:''};
globalThis.document = {body:{dataset:{}},addEventListener(){}};
globalThis.window = {location,addEventListener(){}};
const {continuationInputHtml} = await import('../../static/mr_continuation_input_presenter.js');
const fingerprint = 'a'.repeat(64);
const artifact = (extra={}) => ({project_id:'project-a',metadata:{
  input_method:'code-host-mr-procedure-input.v2',observed_requirements_fingerprint:fingerprint,
  source_evidence_ids:JSON.stringify(['evidence-review','evidence-ci','evidence-policy']),
  verification_id:'verification-original',...extra}});

test('governed input exposes semantic identity and canonical project-scoped proof links for both protocols', () => {
  for (const version of ['v1','v2']) {
    const html=continuationInputHtml(artifact({input_method:'code-host-mr-procedure-input.'+version}));
    assert.ok(html.includes(fingerprint));
    for (const id of ['evidence-review','evidence-ci','evidence-policy','verification-original']) assert.ok(html.includes(id));
    assert.match(html,/\/projects\/project-a\/operations\?/);
    assert.match(html,/consumer_type=evidence/);assert.match(html,/consumer_type=verification/);
    assert.match(html,/Historical provenance only/);assert.match(html,/No QA or merge approval/);
  }
});
test('Evidence links to its exact continuation input without exposing provider payloads or arbitrary metadata', () => {
  const html=continuationInputHtml({project_id:'project-a',metadata:{
    verification_method:'code-host-mr-continuation-input.v2',continuation_input_artifact_id:'artifact-original',
    provider_snapshot_json:'SECRET_PROVIDER_PAYLOAD',secret_value:'SECRET_VALUE',unrelated:'DENIED_METADATA'}});
  assert.match(html,/consumer_type=artifact/);assert.match(html,/artifact-original/);
  for (const value of ['SECRET_PROVIDER_PAYLOAD','SECRET_VALUE','DENIED_METADATA']) assert.ok(!html.includes(value));
  assert.match(html,/no QA or merge approval/);
});
test('malformed or absent metadata is unavailable, never a fresh or valid observation assertion', () => {
  for (const value of ['not-json','null','{}','[]','["only-one"]',JSON.stringify(['a','b',{}]),'x'.repeat(4097)]) {
    const html=continuationInputHtml(artifact({source_evidence_ids:value,verification_id:{},observed_requirements_fingerprint:'<unsafe>'}));
    assert.match(html,/Source Evidence: unavailable/);assert.match(html,/Verification: unavailable/);
    assert.match(html,/semantic fingerprint: unavailable/);assert.ok(!html.includes('<unsafe>'));
  }
  assert.equal(continuationInputHtml({metadata:{provider_snapshot_json:'secret'}}),'');
  assert.equal(continuationInputHtml(artifact({input_method:'unsupported'})),'');
});
test('untrusted reference labels are escaped and remain internal links; absent project adds no fabricated project', () => {
  const html=continuationInputHtml(artifact({source_evidence_ids:JSON.stringify(['<script>','proof"quote','proof&value']),verification_id:'<verification>'}));
  assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<verification>'));
  assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;verification&gt;/);
  assert.match(html,/%3Cscript%3E/);
  const noProject=artifact();noProject.project_id=null;
  assert.ok(!continuationInputHtml(noProject).includes('/projects/project-a/'));
});
test('presentation never fetches content or mutates input and never recalculates semantic business identity', () => {
  const item=artifact({provider_snapshot_json:'DO_NOT_RENDER'});const before=JSON.stringify(item);
  const html=continuationInputHtml(item);
  assert.equal(JSON.stringify(item),before);assert.ok(!html.includes('DO_NOT_RENDER'));
  assert.ok(html.includes(fingerprint));
});
