export function renderModelCatalogs({ host, providers, catalogs, escapeHtml, timeText, listText }) {
  if (!host) return;
  const byProvider = new Map(catalogs.map(item => [item.provider_id, item]));
  host.innerHTML = providers.map(provider => {
    const item = byProvider.get(provider.id);
    const status = item?.status || (provider.catalog_discovery_enabled ? 'not refreshed' : 'static only');
    const upstreams = Array.from(new Set((item?.entries || []).map(entry => entry.upstream_provider_id).filter(Boolean)));
    const rejected = (item?.entries || []).filter(entry => entry.executable === false);
    return `<div class="comm-entry"><strong>${escapeHtml(provider.id)} · ${escapeHtml(status)}</strong>
      <small>Mode: ${provider.catalog_discovery_enabled ? 'provider discovery' : 'static definitions'} · TTL ${escapeHtml(provider.catalog_ttl_seconds || 300)}s · Entries ${escapeHtml(item?.entries?.length || 0)}</small>
      <small>Revision: ${escapeHtml(item?.revision || 'none')} · discovered ${timeText(item?.discovered_at)} · expires ${timeText(item?.expires_at)} · upstreams ${listText(upstreams)}</small>
      ${rejected.length ? `<small>Runtime/access exclusions: ${rejected.map(entry => `${escapeHtml(entry.concrete_model)} (${escapeHtml(entry.exclusion_reason || "runtime rejected")})`).join(", ")}</small>` : ''}
      ${item?.error ? `<small>Last error: ${escapeHtml(item.error)}</small>` : ''}
      <button type="button" class="ghost-button" data-model-catalog-refresh="${escapeHtml(provider.id)}">Refresh catalog</button></div>`;
  }).join('') || '<div class="comm-entry"><strong>No providers registered.</strong></div>';
}

export function renderModelDefinitions({ host, items, escapeHtml, listText, referenceLink, referenceAttributes, focusReference }) {
  if (!host) return;
  host.innerHTML = items.map((item) => `<div class="comm-entry" ${referenceAttributes("model", item.id)}>
    <strong>${escapeHtml(item.id)} · ${escapeHtml(item.lifecycle)}</strong>
    <small>Provider: ${referenceLink("model_provider", item.provider_id)} · Concrete model: ${escapeHtml(item.concrete_model)}${item.model_version ? ` · version ${escapeHtml(item.model_version)}` : ""} · family ${escapeHtml(item.model_family || "unspecified")}</small>
    <small>Availability: ${escapeHtml(item.availability_source || "static")} · Upstream: ${escapeHtml(item.upstream_provider_id || "unknown")} / ${escapeHtml(item.upstream_model_id || "unknown")}</small>
    <small>Classes: ${listText(item.model_classes)} · Workloads: ${listText(item.workload_classes)} · Capabilities: ${listText(item.capabilities)} · Modalities: ${listText(item.modalities)} · Tools: ${item.supports_tools ? "yes" : "no"}</small>
    <small>Context: ${escapeHtml(item.context_window_tokens)} · Max output: ${escapeHtml(item.max_output_tokens)} · Latency: ${escapeHtml(item.latency_class)} · Route priority: ${escapeHtml(item.route_priority)}</small>
    <small>Pricing / 1M tokens: input ${item.input_price_per_million_usd == null ? "unknown" : `$${escapeHtml(item.input_price_per_million_usd)}`} · output ${item.output_price_per_million_usd == null ? "unknown" : `$${escapeHtml(item.output_price_per_million_usd)}`}</small>
    <small>Residency: ${listText(item.residency_tags)} · Compliance: ${listText(item.compliance_tags)} · Updated by: ${escapeHtml(item.updated_by)}</small>
  </div>`).join("") || '<div class="comm-entry"><strong>No model definitions registered.</strong></div>';
  focusReference(host);
}

export function bindModelCatalogRefresh({ host, api, setStatus, refresh }) {
  host?.addEventListener('click', async event => {
    const button = event.target.closest?.('[data-model-catalog-refresh]');
    if (!button || button.disabled) return;
    button.disabled = true;
    try {
      const providerId = button.dataset.modelCatalogRefresh;
      const response = await api(`/api/model-gateway/providers/${encodeURIComponent(providerId)}/catalog/refresh`, { method: 'POST' });
      setStatus(response.item?.status === 'ready' ? `Refreshed ${providerId} model catalog.` : `Catalog refresh recorded ${response.item?.status || 'unknown'}: ${response.item?.error || 'no provider entries'}`);
      await refresh();
    } catch (error) { setStatus(`Catalog refresh failed: ${error.message}`); }
    finally { if (button.isConnected) button.disabled = false; }
  });
}
