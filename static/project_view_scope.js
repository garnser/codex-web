import { initialProjectId, observeIdentityScope } from './view_scope_context.js';
import { request } from './api_client.js';

// UI fence; canonical APIs authorize scope.
let generation = 0;
let projectId = initialProjectId();
export function currentProjectId() { return projectId; }
observeIdentityScope(() => { generation += 1; });
window.addEventListener('codex:project-changed', event => {
  const next = String(event.detail?.projectId || '').trim();
  if (next !== projectId) { generation += 1; projectId = next; }
});

export function captureProjectView() {
  const started = generation;
  return { generation: started, current: () => started === generation };
}

export function projectViewOperation(report, refreshControl = 'refresh-definitions') {
  const { current } = captureProjectView();
  const selectedProject = projectId;
  const assertCurrent = () => {
    if (!current()) throw new DOMException('Project changed', 'AbortError');
  };
  return {
    current,
    status(message) { if (current()) report(message); },
    refresh() { if (current()) document.getElementById(refreshControl)?.click(); },
    async request(path, options) {
      assertCurrent();
      if (selectedProject && /^\/api\/(definitions|configuration)(\/|\?|$)/.test(path)) {
        const url = new URL(path, location.origin);
        const requested = url.searchParams.get('project_id');
        if (requested !== null && requested !== selectedProject) throw new DOMException('Project scope changed', 'AbortError');
        url.searchParams.set('project_id', selectedProject);
        path = url.pathname + url.search;
      }
      const result = await request(path, options);
      assertCurrent();
      return result;
    },
  };
}
