// Display context only. Canonical APIs own identity, scope and authority.
export function initialProjectId() {
  const route = location.pathname.match(/\/projects\/([^/]+)(?:\/|$)/);
  if (route) { try { return decodeURIComponent(route[1]); } catch { return ''; } }
  return document.body?.dataset.projectId || document.body?.dataset.activeProject
    || new URLSearchParams(location.search).get('project') || '';
}

export function observeIdentityScope(invalidate) {
  let identityScope = null;
  window.addEventListener('codex:identity-state-rendered', event => {
    const actor = event.detail?.actor;
    const next = JSON.stringify([actor?.identity_id, actor?.organization_id, actor?.workspace_id, actor?.session_id]);
    if (next !== identityScope) {
      identityScope = next; invalidate();
      window.dispatchEvent(new CustomEvent('codex:identity-scope-changed'));
    }
  });
}
