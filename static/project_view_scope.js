import { request } from './api_client.js';

// A UI response fence, never an authorization decision. The API checks actors
// and target scope independently for every operation.
let generation = 0;
function initialProjectId() {
  const route = location.pathname.match(/\/projects\/([^/]+)(?:\/|$)/);
  if (route) { try { return decodeURIComponent(route[1]); } catch { return ''; } }
  return document.body?.dataset.projectId || document.body?.dataset.activeProject
    || new URLSearchParams(location.search).get('project') || '';
}
let projectId = initialProjectId();
export function currentProjectId() { return projectId; }
window.addEventListener('codex:project-changed', event => {
  const next = String(event.detail?.projectId || '').trim();
  if (next !== projectId) { generation += 1; projectId = next; }
});

export function projectViewOperation(report, refreshControl = 'refresh-definitions') {
  const started = generation;
  const current = () => started === generation;
  const assertCurrent = () => {
    if (!current()) throw new DOMException('Project changed', 'AbortError');
  };
  return {
    current,
    status(message) { if (current()) report(message); },
    refresh() { if (current()) document.getElementById(refreshControl)?.click(); },
    async request(...args) {
      assertCurrent();
      const result = await request(...args);
      assertCurrent();
      return result;
    },
  };
}
