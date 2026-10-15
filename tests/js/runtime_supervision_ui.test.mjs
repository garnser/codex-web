import {test} from 'node:test';
import assert from 'node:assert/strict';
import {supervisionObservationText} from '../../static/runtime_supervision_ui.js';
test('old observations preserve current authority and unavailable evidence',()=>{
  const text=supervisionObservationText({event_type:'agent_runtime_supervision_failed',details:{observed_fence:1,current_fence:3,current_status:'running'}});
  assert.match(text,/observed fence 1 · current fence 3/);
  assert.match(text,/canonical state running/);
  assert.match(text,/Diagnostic evidence unavailable/);
  assert.match(text,/completion not asserted/);
});

test('canonical worker observation exposes failure phase and cleanup without asserting completion',()=>{
  const text=supervisionObservationText({event_type:'agent_runtime_supervision_failed',details:{failure_code:'resource_limit',
    observed_fence:1,current_fence:1,current_status:'running',phase:'validation',cleanup_state:'stopped',validation_custody_state:'unavailable'}});
  assert.match(text,/phase validation · cleanup stopped · validation custody unavailable/);
  assert.match(text,/completion not asserted/);
});
