import assert from 'node:assert/strict';
import test from 'node:test';
globalThis.location = {pathname:'/',search:''};
globalThis.document = {body:{dataset:{}},addEventListener(){}};
globalThis.window = {location,addEventListener(){}};
const {observationPolicyHtml} = await import('../../static/artifact_evidence_presenters.js');
test('canonical observation policy is escaped historical provenance, never UI authority', () => {
  const policy={version:'2.0',seconds:120,key:'code_host.mr_observation_window_seconds',
    source:'published',record_id:'<untrusted>',revision:2,record_sha256:'a'.repeat(64),resolved_at:1000};
  const html=observationPolicyHtml({metadata:{observation_policy:JSON.stringify(policy),observation_expires_at:1120}});
  assert.match(html,/120s/);
  assert.match(html,/&lt;untrusted&gt; r2/);
  assert.match(html,/Historical provenance only/);
  assert.match(html,/revocation are revalidated by the server/);
  assert.ok(!html.includes('<untrusted>'));
  assert.equal(observationPolicyHtml({metadata:{}}),'');
  assert.match(observationPolicyHtml({metadata:{observation_policy:'not-json'}}),/invalid/);
  assert.match(observationPolicyHtml({metadata:{observation_policy:JSON.stringify({...policy,seconds:301})}}),/invalid/);
});
