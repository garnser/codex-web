import test from 'node:test';
import assert from 'node:assert/strict';
import { appendHomeHostOutputs } from '../static/home_host_output_panel.js';

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.isConnected = true; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  setAttribute() {}
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
globalThis.document = { createElement: tag => new Element(tag) };
const state = { can_configure: true, thread: { id: 'home-current' }, binding: { revision: 3 } };

test('mounted human panel loads only refs on click and mounts actual review control by choice', async () => {
  const host = new Element('div'), calls = [], mounted = [];
  let disposed = 0;
  const dispose = appendHomeHostOutputs(host, state, { read: async (url) => {
    calls.push(url);
    if (url === '/api/home/operator') return state;
    return { home_revision: 3, thread_id: 'home-current', items: [{ artifact_id: 'source', exit_code: 0 }] };
  }, mountReview: (container, options) => { mounted.push([container, options]); return () => disposed++; } });
  assert.equal(calls.length, 0); assert.equal(mounted.length, 0);
  const panel = host.children[0];
  await panel.children[1].listeners.click();
  assert.equal(calls.filter(url => url.includes('host-outputs')).length, 1);
  assert.equal(calls.some(url => url.includes('/preview') || url.includes('/review')), false);
  panel.children[3].children[0].listeners.click();
  assert.equal(mounted.length, 1); assert.equal(mounted[0][1].artifactId, 'source');
  assert.equal(mounted[0][1].homeRevision, 3);
  dispose(); assert.equal(disposed, 1);
  assert.equal(mounted[0][1].currentRevision(), null);
});

test('selection change while refs load refuses mount and clears panel', async () => {
  const host = new Element('div'); let changed = false, mounted = 0;
  appendHomeHostOutputs(host, state, { read: async (url) => {
    if (url === '/api/home/operator') return changed ? { ...state, binding: { revision: 4 } } : state;
    changed = true;
    return { home_revision: 3, thread_id: 'home-current', items: [{ artifact_id: 'source', exit_code: 0 }] };
  }, mountReview: () => { mounted++; return () => {}; } });
  const panel = host.children[0]; await panel.children[1].listeners.click();
  assert.equal(panel.children[3].children.length, 0); assert.equal(mounted, 0);
  assert.match(panel.children[2].textContent, /changed/);
});

test('panel is absent for navigation-only/non-admin Home', () => {
  const host = new Element('div');
  appendHomeHostOutputs(host, { ...state, can_configure: false });
  assert.equal(host.children.length, 0);
});
