import assert from 'node:assert/strict';
import test from 'node:test';
globalThis.location = {pathname: '/', search: ''};
globalThis.document = {body: {dataset: {}}, addEventListener() {}};
globalThis.window = {location, addEventListener() {}};
const {failureHtml} = await import('../../static/action_intent_outcome_view.js');
const failure = {reason_code: 'action_timeout_unknown_outcome', outcome: 'unknown',
  retryability: 'reconcile_required', remediation_key: 'action.reconcile', summary: 'Original outcome unknown.'};

test('failure inspector shows closed phases and finite timing metadata without claiming HTTP ran', () => {
  for (const [phase, label] of Object.entries({execute_admission: 'Execution admission',
    provider_execution: 'Provider execution (including guards)', result_admission_check: 'Result admission check',
    result_admission: 'Result admission'})) {
    const html = failureHtml({...failure, details: {timeout_phase: phase,
      phase_elapsed_seconds: 3.456, shared_remaining_at_phase_entry: 90,
      shared_remaining_at_timeout: 0}});
    assert.ok(html.includes(`Timeout phase: ${label}`));
    assert.ok(html.includes('elapsed 3.46s'));
    assert.ok(html.includes('remaining at entry 90s'));
    assert.ok(html.includes('remaining at timeout 0s'));
    assert.ok(html.includes('action_timeout_unknown_outcome'));
    assert.ok(html.includes('</small> <small>Canonical failure:'));
    assert.ok(!html.includes('HTTP'));
  }
});

test('unknown phases, invalid numbers and arbitrary details are never rendered', () => {
  for (const phase of ['<script>secret</script>', 'toString', '__proto__', '', null]) {
    assert.equal(failureHtml({...failure, details: {timeout_phase: phase, phase_elapsed_seconds: 3,
      secret: 'PRIVATE_BODY'}}), failureHtml(failure));
  }
  for (const value of [-1, NaN, Infinity, -Infinity, null, true, '45', '<img src=x>']) {
    const html = failureHtml({...failure, details: {timeout_phase: 'provider_execution',
      phase_elapsed_seconds: value, shared_remaining_at_phase_entry: value,
      shared_remaining_at_timeout: value, secret: 'PRIVATE_BODY', provider_body: 'PRIVATE_BODY',
      guarded_deadline_at: '<img src=x>'}});
    assert.ok(html.includes('Provider execution (including guards)'));
    for (const forbidden of ['elapsed', 'remaining at', 'PRIVATE_BODY', '<img', 'NaN', 'Infinity']) {
      assert.ok(!html.includes(forbidden), forbidden);
    }
  }
});

test('missing and legacy details keep prior rendering and failure text remains escaped', () => {
  const expected = '<small>Canonical failure: action_timeout_unknown_outcome · outcome unknown · retry reconcile_required · remediation action.reconcile · Original outcome unknown.</small>';
  assert.equal(failureHtml(null), '');
  assert.equal(failureHtml(failure), expected);
  assert.equal(failureHtml({...failure, details: {reason: 'timeout'}}), expected);
  const html = failureHtml({...failure, summary: '<script>unsafe</script>',
    details: {timeout_phase: 'execute_admission'}});
  assert.ok(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));
  assert.ok(!html.includes('<script>'));
});
