(async () => {
  const BASE = window.location.pathname.startsWith('/codex') ? '/codex' : '';
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);
  const { brokerAuditHtml, brokerDeclarationHtml } = await import(`${BASE}/static/control_plane_broker_view.js`);
  const { currentProjectId } = await import(`${BASE}/static/project_view_scope.js`);
  let generation = 0;
  const pending = new Set();
  const panels = new Map();
  function invalidate() {
    generation += 1;
    pending.forEach(controller => controller.abort());
    pending.clear();
    panels.clear();
    document.querySelectorAll('.control-plane-broker-status').forEach(node => node.remove());
  }

  function renderAssignment(item, viewGeneration) {
    const projectId = currentProjectId();
    if (!projectId || item.project_id !== projectId) return;
    const host = document.querySelector(`[data-execution-assignment-management-host="${CSS.escape(item.id)}"]`);
    if (!host) return;
    const fingerprint = JSON.stringify(item);
    const previous = panels.get(item.id);
    if (previous?.fingerprint === fingerprint) {
      // Worker rendering replaces its host nodes. Reattach the same inspector
      // only while its complete public assignment/scope snapshot is unchanged.
      const expanded = previous.panel.closest('details')?.open === true;
      host.before(previous.panel);
      if (expanded) {
        const parent = host.closest('details');
        if (parent) parent.open = true;
      }
      return;
    }
    previous?.controller?.abort();
    previous?.panel.remove();
    const panel = document.createElement('section');
    panel.className = 'control-plane-broker-status';
    panel.setAttribute('aria-label', 'Broker scope and outcomes');
    panel.innerHTML = brokerDeclarationHtml(item, projectId);
    host.before(panel);
    const record = {fingerprint, panel, controller:null};
    panels.set(item.id, record);
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
      record.controller = controller;
      const owned = () => generation === viewGeneration && projectId === currentProjectId()
        && panels.get(item.id) === record && panel.isConnected;
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
        if (record.controller === controller) record.controller = null;
        if (owned()) { button.disabled = false; panel.removeAttribute('aria-busy'); }
      }
    });
  }
  window.addEventListener('codex:project-changed', invalidate);
  window.addEventListener('codex:execution-worker-state-rendered', event => {
    const assignments = event.detail?.assignments || [];
    const current = new Set(assignments.filter(item => item.project_id === currentProjectId()
      && document.querySelector(`[data-execution-assignment-management-host="${CSS.escape(item.id)}"]`)).map(item => item.id));
    for (const [id, record] of panels) {
      if (!current.has(id)) {
        record.controller?.abort();
        record.panel.remove();
        panels.delete(id);
      }
    }
    assignments.forEach(item => renderAssignment(item, generation));
  });
})();
