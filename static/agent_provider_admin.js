(async () => {
  const BASE = window.location.pathname.startsWith('/codex') ? '/codex' : '';
  const { request: apiRequest } = await import(`${BASE}/static/api_client.js`);

  const capabilityLabel = (value) => String(value || '').replaceAll('_', ' ');
  const hasCapability = (session, capability) => (
    (session?.capability_snapshot || session?.runtime_registration?.capabilities || []).includes(capability)
  );

  function installStyles() {
    if (document.getElementById('agent-provider-admin-styles')) return;
    const style = document.createElement('style');
    style.id = 'agent-provider-admin-styles';
    style.textContent = `
      #agent-provider-card{grid-column:1/-1}
      .agent-provider-toolbar{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}
      .agent-provider-toolbar h2{margin:0}
      .agent-provider-status{font-size:11px;color:var(--muted,#9aa4b5)}
      .agent-provider-layout{display:grid;grid-template-columns:minmax(0,.9fr) minmax(0,1.1fr);gap:12px;margin-top:10px}
      .agent-provider-section{min-width:0;display:grid;gap:8px}
      .agent-provider-section>h3{margin:0;color:var(--muted,#9aa4b5);font-size:11px;text-transform:uppercase}
      .agent-provider-list,.agent-session-list{display:grid;gap:7px;max-height:360px;overflow:auto}
      .agent-provider-item,.agent-session-item{border:1px solid var(--border,var(--line,#313744));border-radius:8px;padding:9px;background:rgba(127,127,127,.035);display:grid;gap:6px}
      .agent-provider-head,.agent-session-head{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}
      .agent-provider-title,.agent-session-title{font-size:12px;font-weight:800;overflow-wrap:anywhere}
      .agent-provider-meta,.agent-session-meta,.agent-provider-note{font-size:10px;color:var(--muted,#9aa4b5);overflow-wrap:anywhere}
      .agent-capabilities{display:flex;gap:4px;flex-wrap:wrap}
      .agent-capability,.agent-health,.agent-usage-quality{font-size:9px;border:1px solid var(--border,var(--line,#313744));border-radius:999px;padding:2px 6px;text-transform:uppercase}
      .agent-capability.execution{font-weight:800}
      .agent-health.healthy{color:#78c99b}.agent-health.degraded{color:#e5b567}.agent-health.unavailable{color:#e78585}
      .agent-session-actions{display:flex;gap:6px;flex-wrap:wrap}
      .agent-session-actions button{min-height:30px}
      .agent-session-actions button:disabled{opacity:.45;cursor:not-allowed}
      .agent-route-explainer{grid-column:1/-1;border-top:1px solid var(--border,var(--line,#313744));padding-top:10px;display:grid;gap:7px}
      .agent-route-form{display:grid;grid-template-columns:minmax(140px,1fr) minmax(140px,1fr) auto;gap:7px}
      .agent-route-form input{min-width:0;height:36px;border:1px solid var(--line,#313744);border-radius:7px;background:var(--input-bg,var(--surface));color:var(--text);padding:7px 9px}
      .agent-route-result{font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--border,var(--line,#313744));border-radius:7px;padding:8px;background:var(--code,var(--surface-soft))}
      .agent-empty{font-size:11px;color:var(--muted,#9aa4b5);padding:7px}
      @media(max-width:760px){.agent-provider-layout{grid-template-columns:1fr}.agent-route-form{grid-template-columns:1fr}.agent-session-head,.agent-provider-head{align-items:stretch;flex-direction:column}}
    `;
    document.head.appendChild(style);
  }

  const textNode = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text;
    return node;
  };

  function capabilityPills(capabilities = []) {
    const root = document.createElement('div');
    root.className = 'agent-capabilities';
    for (const capability of capabilities) {
      const pill = textNode(
        'span',
        `agent-capability ${capability === 'agent_execution' ? 'execution' : ''}`,
        capabilityLabel(capability),
      );
      root.appendChild(pill);
    }
    return root;
  }

  function providerView(discovery, runtimeByProvider) {
    const provider = discovery.provider || discovery;
    const item = document.createElement('article');
    item.className = 'agent-provider-item';

    const head = document.createElement('div');
    head.className = 'agent-provider-head';
    const titleBox = document.createElement('div');
    titleBox.append(
      textNode('div', 'agent-provider-title', provider.display_name || provider.id),
      textNode(
        'div',
        'agent-provider-meta',
        `${provider.id} · ${provider.lifecycle || 'unknown'} · ${provider.compatibility || 'unknown'}`,
      ),
    );
    const health = textNode(
      'span',
      `agent-health ${provider.health || 'unknown'}`,
      provider.health || 'unknown',
    );
    head.append(titleBox, health);
    item.appendChild(head);

    const effective = discovery.effective_capabilities
      || (provider.declared_capabilities || []).filter((cap) => (provider.granted_capabilities || []).includes(cap));
    item.appendChild(capabilityPills(effective));

    const kinds = [];
    if (effective.includes('model_inference')) kinds.push('model inference');
    if (effective.includes('agent_execution')) kinds.push('execution agent');
    const runtimes = runtimeByProvider.get(provider.id) || [];
    item.appendChild(textNode(
      'div',
      'agent-provider-meta',
      `${kinds.join(' + ') || 'No effective capabilities'} · ${runtimes.length} runtime${runtimes.length === 1 ? '' : 's'}`,
    ));

    if (provider.credential_refs?.length) {
      item.appendChild(textNode(
        'div',
        'agent-provider-note',
        `Credential references: ${provider.credential_refs.join(', ')} (values are not returned)`,
      ));
    }
    if (!discovery.eligible && discovery.reasons?.length) {
      item.appendChild(textNode(
        'div',
        'agent-provider-note',
        `Ineligible: ${discovery.reasons.join('; ')}`,
      ));
    }
    for (const runtime of runtimes) {
      item.appendChild(textNode(
        'div',
        'agent-provider-note',
        `Runtime ${runtime.runtime_id} · ${runtime.runtime_type} · capability rev ${runtime.capability_revision} · ${runtime.health}`,
      ));
    }
    return item;
  }

  function sessionView(session, refresh) {
    const item = document.createElement('article');
    item.className = 'agent-session-item';

    const head = document.createElement('div');
    head.className = 'agent-session-head';
    const titleBox = document.createElement('div');
    titleBox.append(
      textNode('div', 'agent-session-title', session.id),
      textNode(
        'div',
        'agent-session-meta',
        `${session.provider_id}/${session.runtime_id} · ${session.model || 'provider default model'} · ${session.status}`,
      ),
    );
    const health = textNode(
      'span',
      `agent-health ${session.runtime_health || 'unknown'}`,
      session.runtime_health || 'unknown',
    );
    head.append(titleBox, health);
    item.appendChild(head);

    item.appendChild(textNode(
      'div',
      'agent-session-meta',
      `Native: ${session.provider_native_session_id || 'unavailable'} · assignment: ${session.assignment_id || 'none'} · workspace: ${session.execution_workspace_id || 'none'}`,
    ));
    item.appendChild(capabilityPills(session.capability_snapshot || []));

    const usage = session.latest_usage;
    const quality = usage?.telemetry_completeness || 'unavailable';
    const usageRow = document.createElement('div');
    usageRow.className = 'agent-session-meta';
    const qualityPill = textNode('span', 'agent-usage-quality', quality);
    const tokens = usage
      ? ` ${usage.input_tokens ?? '?'} in / ${usage.output_tokens ?? '?'} out${usage.cost_usd == null ? '' : ` · $${Number(usage.cost_usd).toFixed(4)}`}`
      : ' no canonical runtime usage yet';
    usageRow.append(qualityPill, document.createTextNode(tokens));
    item.appendChild(usageRow);

    const actions = document.createElement('div');
    actions.className = 'agent-session-actions';
    const specs = [
      ['Interrupt', 'interrupt', 'interrupt_cancel'],
      ['Compact', 'compact', 'native_context_compaction'],
      ['Close', 'close', null],
    ];
    for (const [label, action, capability] of specs) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'ghost-button';
      button.textContent = label;
      const supported = !capability || hasCapability(session, capability);
      button.disabled = !supported || (action === 'close' && session.status === 'closed');
      if (!supported) {
        button.title = `Unsupported: runtime does not declare ${capabilityLabel(capability)}`;
        button.setAttribute('aria-label', `${label} unavailable: runtime does not support ${capabilityLabel(capability)}`);
      }
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          await apiRequest(`/api/agent-sessions/${encodeURIComponent(session.id)}/${action}`, { method: 'POST' });
          await refresh();
        } catch (error) {
          item.appendChild(textNode('div', 'agent-provider-note', `${label} failed: ${error.message}`));
        } finally {
          if (item.isConnected) button.disabled = !supported;
        }
      });
      actions.appendChild(button);
    }
    const approvals = document.createElement('a');
    approvals.href = '#approvals';
    approvals.className = 'ghost-button';
    approvals.textContent = 'Canonical approvals';
    approvals.title = 'Approval decisions are owned by codex-web ApprovalRequest state';
    actions.appendChild(approvals);
    item.appendChild(actions);
    return item;
  }

  async function loadInventory() {
    const [providers, runtimes, sessions] = await Promise.all([
      apiRequest('/api/agent-providers/discover', {
        method: 'POST',
        body: JSON.stringify({ required_capabilities: [] }),
      }),
      apiRequest('/api/agent-runtimes'),
      apiRequest('/api/agent-sessions'),
    ]);
    return {
      providers: providers.items || [],
      runtimes: runtimes.items || [],
      sessions: sessions.items || [],
    };
  }

  function renderInventory(card, inventory, refresh) {
    const providerList = card.querySelector('.agent-provider-list');
    const sessionList = card.querySelector('.agent-session-list');
    providerList.replaceChildren();
    sessionList.replaceChildren();

    const runtimeByProvider = new Map();
    for (const runtime of inventory.runtimes) {
      const values = runtimeByProvider.get(runtime.provider_id) || [];
      values.push(runtime);
      runtimeByProvider.set(runtime.provider_id, values);
    }
    if (!inventory.providers.length) {
      providerList.appendChild(textNode('div', 'agent-empty', 'No AgentProvider records are configured.'));
    } else {
      for (const provider of inventory.providers) {
        providerList.appendChild(providerView(provider, runtimeByProvider));
      }
    }

    if (!inventory.sessions.length) {
      sessionList.appendChild(textNode('div', 'agent-empty', 'No canonical AgentSessions yet.'));
    } else {
      for (const session of inventory.sessions) {
        sessionList.appendChild(sessionView(session, refresh));
      }
    }
  }

  async function refreshCard(card) {
    const button = card.querySelector('.agent-provider-refresh');
    const status = card.querySelector('.agent-provider-status');
    button.disabled = true;
    status.textContent = 'Loading provider/runtime/session state…';
    try {
      const inventory = await loadInventory();
      renderInventory(card, inventory, () => refreshCard(card));
      status.textContent = `${inventory.providers.length} provider${inventory.providers.length === 1 ? '' : 's'} · ${inventory.runtimes.length} runtime${inventory.runtimes.length === 1 ? '' : 's'} · ${inventory.sessions.length} session${inventory.sessions.length === 1 ? '' : 's'}`;
    } catch (error) {
      status.textContent = `Agent provider state unavailable: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function explainRoute(card) {
    const status = card.querySelector('.agent-route-result');
    const projectId = card.querySelector('.agent-route-project').value.trim();
    const provider = card.querySelector('.agent-route-provider').value.trim();
    if (!projectId) {
      status.textContent = 'Project ID is required.';
      return;
    }
    status.textContent = 'Evaluating canonical routing policy…';
    try {
      const payload = {
        project_id: projectId,
        require_persistent_session: true,
        preferred_provider_ids: provider ? [provider] : [],
      };
      const result = await apiRequest('/api/agent-routing/route', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      const route = result.route || {};
      const selected = route.selected_runtime || {};
      const candidates = (route.runtime_candidates || [])
        .map((candidate) => `${candidate.provider_id}/${candidate.runtime_id}: ${candidate.routing_reason}`)
        .join('\n');
      const rejected = (route.rejected_reasons || []).join('\n');
      status.textContent = [
        `Selected: ${selected.provider_id || '?'} /${selected.runtime_id || '?'}`,
        `Reason: ${selected.routing_reason || 'not supplied'}`,
        candidates ? `Candidates:\n${candidates}` : '',
        rejected ? `Rejected:\n${rejected}` : '',
      ].filter(Boolean).join('\n');
    } catch (error) {
      status.textContent = `Routing failed: ${error.message}`;
    }
  }

  function installCard() {
    installStyles();
    const grid = document.querySelector('#developer-panel .developer-grid');
    if (!grid || document.getElementById('agent-provider-card')) return;
    const card = document.createElement('section');
    card.id = 'agent-provider-card';
    card.className = 'developer-card';
    card.innerHTML = `
      <div class="agent-provider-toolbar">
        <h2>Agent Providers & Sessions</h2>
        <div><span class="agent-provider-status">Open Developer to load canonical agent state.</span> <button type="button" class="ghost-button agent-provider-refresh">Refresh</button></div>
      </div>
      <div class="agent-provider-layout">
        <section class="agent-provider-section"><h3>Providers & runtimes</h3><div class="agent-provider-list"></div></section>
        <section class="agent-provider-section"><h3>Canonical sessions</h3><div class="agent-session-list"></div></section>
        <section class="agent-route-explainer">
          <h3>Explain runtime routing</h3>
          <div class="agent-route-form">
            <input class="agent-route-project" aria-label="Project ID" value="home" placeholder="Project ID">
            <input class="agent-route-provider" aria-label="Preferred provider ID" placeholder="Preferred provider (optional)">
            <button type="button" class="ghost-button agent-route-run">Explain</button>
          </div>
          <div class="agent-route-result" aria-live="polite">No routing evaluation run yet.</div>
        </section>
      </div>
    `;
    grid.prepend(card);
    card.querySelector('.agent-provider-refresh').addEventListener('click', () => refreshCard(card));
    card.querySelector('.agent-route-run').addEventListener('click', () => explainRoute(card));
    const panel = document.getElementById('developer-panel');
    panel?.addEventListener('toggle', () => { if (panel.open) refreshCard(card); });
    if (panel?.open) refreshCard(card);
  }

  if (document.readyState === 'loading') {
    window.addEventListener('DOMContentLoaded', installCard, { once: true });
  } else {
    installCard();
  }
})();
