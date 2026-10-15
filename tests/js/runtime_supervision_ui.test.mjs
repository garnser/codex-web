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
