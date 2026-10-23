import test from 'node:test';
import assert from 'node:assert/strict';
import { mountHomeHostOutputReview } from '../static/home_host_output_review.js';

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.value = ''; this.isConnected = true; }
  append(...items) { for (const item of items) { if (this.tagName === 'SELECT' && this.children.length === 0) this.value = item.value; item.parent = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute() {}
  addEventListener(name, callback) { this.listeners[name] = callback; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); }
}
globalThis.document = { createElement: tag => new Element(tag) };
const descendant = (node, tag) => node.children.find(item => item.tagName === tag);

// This module is a human UI. It never calls release or fetches secret preview at mount.
test('protected preview is explicit and reviewed byte digest goes only to human API', async () => {
  const container = new Element('section');
  const calls = [];
  let released;
  const dispose = mountHomeHostOutputReview(container, { artifactId: 'artifact-source', homeRevision: 3,
    currentRevision: () => 3, onReleased: value => { released = value; },
    api: async (url, options) => {
      calls.push([url, options]);
      if (!options) return { streams: { stdout: '<script>ordinary</script>', stderr: '' }, expires_at: Date.now() / 1000 + 30 };
      return { artifact_id: 'artifact-reviewed' };
    },
  });
  try {
    assert.equal(calls.length, 0);
    await descendant(container, 'BUTTON').listeners.click();
    const form = descendant(container, 'FORM');
    const preview = descendant(form, 'TEXTAREA');
    assert.equal(preview.value, '<script>ordinary</script>');
    assert.equal(preview.readOnly, true);
    const reason = form.children.find(item => item.tagName === 'INPUT' && item.type !== 'number');
    reason.value = 'Reviewed normal source output';
    await form.listeners.submit({ preventDefault() {} });
    assert.equal(calls.length, 2);
    const body = JSON.parse(calls[1][1].body);
    assert.equal(body.expected_home_revision, 3);
    assert.equal(body.selection.byte_start, 0);
    assert.equal(body.selection.byte_end, 25);
    assert.match(body.selection.expected_sha256, /^[a-f0-9]{64}$/);
    assert.equal(body.classification, 'confidential');
    assert.equal(released.artifact_id, 'artifact-reviewed');
    assert.equal(preview.value, '');
  } finally { dispose(); }
});

test('changed Home after preview request suppresses all decrypted text', async () => {
  const container = new Element('section'); let revision = 3;
  const dispose = mountHomeHostOutputReview(container, { artifactId: 'artifact-source', homeRevision: 3,
    currentRevision: () => revision, api: async () => { revision = 4; return { streams: { stdout: 'must not display', stderr: '' }, expires_at: Date.now() / 1000 + 30 }; },
  });
  try {
    await descendant(container, 'BUTTON').listeners.click();
    assert.equal(descendant(container, 'FORM'), undefined);
    assert.match(descendant(container, 'P').textContent, /Home selection changed/);
  } finally { dispose(); }
});

test('preview denial has no automatic retry or release request', async () => {
  const container = new Element('section'); let calls = 0;
  const dispose = mountHomeHostOutputReview(container, { artifactId: 'artifact-source', homeRevision: 3,
    currentRevision: () => 3, api: async () => { calls++; throw new Error('Current Home output preview is unavailable'); },
  });
  try {
    await descendant(container, 'BUTTON').listeners.click();
    assert.equal(calls, 1);
    assert.equal(descendant(container, 'FORM'), undefined);
    assert.match(descendant(container, 'P').textContent, /unavailable/);
  } finally { dispose(); }
});
