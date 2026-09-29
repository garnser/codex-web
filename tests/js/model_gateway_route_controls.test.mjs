import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../../static/model_gateway_route_controls.js', import.meta.url), 'utf8');
const controls = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('refresh retains a pin and never silently replaces a missing pin with automatic', () => {
  let markup = '';
  const pin = {
    value: 'pinned',
    get innerHTML() { return markup; },
    set innerHTML(value) { markup = value; this.value = ''; },
  };
  const workloads = { innerHTML: '' };
  globalThis.document = { getElementById: id => ({
    'model-route-pinned-model': pin, 'model-workload-options': workloads,
  })[id] };
  try {
    controls.populateTaskRouteControls([{ id: 'pinned', provider_id: 'provider', workload_classes: ['review'] }]);
    assert.equal(pin.value, 'pinned');
    assert.match(markup, /provider \/ pinned/);
    controls.populateTaskRouteControls([]);
    assert.equal(pin.value, 'pinned');
    assert.match(markup, /Unavailable \/ pinned/);
    assert.equal(controls.readTaskRoutePreferences().pinned_model_id, 'pinned');
    pin.value = '';
    assert.equal(controls.readTaskRoutePreferences().pinned_model_id, null);
    pin.value = '<missing>"';
    controls.populateTaskRouteControls([]);
    assert.ok(!markup.includes('<missing>'));
    assert.match(markup, /&lt;missing&gt;&quot;/);
  } finally {
    delete globalThis.document;
  }
});
