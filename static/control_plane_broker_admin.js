(async () => {
  const BASE = window.location.pathname.startsWith('/codex') ? '/codex' : '';
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { brokerAuditHtml, brokerDeclarationHtml } = await import(`${BASE}/static/control_plane_broker_view.js`);
  const { currentProjectId } = await import(`${BASE}/static/project_view_scope.js`);
  let generation = 0;
  const pending = new Set();
  function invalidate() {
    generation += 1;
    pending.forEach(controller => controller.abort());
    pending.clear();
    document.querySelectorAll('.control-plane-broker-status').forEach(node => node.remove());
  }

  function renderAssignment(item, viewGeneration) {
    const projectId = currentProjectId();
    if (!projectId || item.project_id !== projectId) return;
    const host = document.querySelector(`[data-execution-assignment-management-host="${CSS.escape(item.id)}"]`);
    if (!host) return;
    const panel = document.createElement('section');
    panel.className = 'control-plane-broker-status';
    panel.setAttribute('aria-label', 'Broker scope and outcomes');
    panel.innerHTML = brokerDeclarationHtml(item, projectId);
    host.before(panel);
    panel.querySelector('[data-broker-graph]').addEventListener('click', event => {
      if (generation !== viewGeneration || projectId !== currentProjectId()) return;
      const graph = document.getElementById('work-graph-panel');
      if (!graph) return;
      event.preventDefault();
      graph.scrollIntoView({block:'nearest'});
      const refresh = document.getElementById('refresh-work-graph');
      refresh?.focus();
      refresh?.click();
    });
    const button = panel.querySelector('button');
    const status = panel.querySelector('[role="status"]');
    button.addEventListener('click', async () => {
      const controller = new AbortController();
      pending.add(controller);
      const owned = () => generation === viewGeneration && projectId === currentProjectId() && panel.isConnected;
      button.disabled = true;
      status.textContent = 'Loading canonical broker audit…';
      panel.setAttribute('aria-busy', 'true');
      try {
        const response = await apiRequest(`/api/control-plane-broker/audit?assignment_id=${encodeURIComponent(item.id)}&limit=20`, {signal:controller.signal});
        if (!owned()) return;
        const events = (response.items || []).filter(event => event.assignment_id === item.id && event.project_id === projectId);
        status.textContent = events.length ? `Latest ${events.length} recorded broker outcomes; this bounded sample is not a complete action history.` : 'No broker audit events recorded for this assignment.';
        panel.querySelector('[data-broker-events]').innerHTML = events.map(entry => brokerAuditHtml(entry, projectId)).join('');
      } catch (error) {
        if (owned()) {
          panel.querySelector('[data-broker-events]').replaceChildren();
          status.textContent = `${[401,403].includes(error.status) ? 'Broker audit access denied' : 'Broker audit unavailable'}: ${error.message}. Outcomes remain unknown until a successful read.`;
        }
      } finally {
        pending.delete(controller);
        if (owned()) { button.disabled = false; panel.removeAttribute('aria-busy'); }
      }
    });
  }
  window.addEventListener('codex:project-changed', invalidate);
  window.addEventListener('codex:execution-worker-state-rendered', event => {
    invalidate();
    (event.detail?.assignments || []).forEach(item => renderAssignment(item, generation));
  });
})();
