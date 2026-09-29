import { request } from './api_client.js';

// A UI response fence, never an authorization decision. The API checks actors
// and target scope independently for every operation.
let generation = 0;
let projectId = document.body?.dataset.projectId || document.body?.dataset.activeProject || null;
window.addEventListener('codex:project-changed', event => {
  const next = String(event.detail?.projectId || '').trim();
  if (next !== projectId) { generation += 1; projectId = next; }
});

export function definitionViewOperation(report) {
  const started = generation;
  const current = () => started === generation;
  const assertCurrent = () => {
    if (!current()) throw new DOMException('Project changed', 'AbortError');
  };
  return {
    current,
    status(message) { if (current()) report(message); },
    refresh() { if (current()) document.getElementById('refresh-definitions')?.click(); },
    async request(...args) {
      assertCurrent();
      const result = await request(...args);
      assertCurrent();
      return result;
    },
  };
}
