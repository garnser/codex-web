const field = id => document.getElementById(id);

export function resetProviderCatalogFields() {
  field('model-provider-catalog-discovery').checked = false;
  field('model-provider-catalog-required').checked = false;
  field('model-provider-catalog-ttl').value = 300;
  field('model-provider-family').value = '';
  field('model-provider-runtime').value = '';
  field('model-provider-access').value = 'api_key';
  field('model-provider-usage').value = 'metered_api';
  field('model-provider-health').value = 'unknown';
}

export function loadProviderCatalogFields(item) {
  field('model-provider-catalog-discovery').checked = Boolean(item.catalog_discovery_enabled);
  field('model-provider-catalog-required').checked = Boolean(item.catalog_required);
  field('model-provider-catalog-ttl').value = item.catalog_ttl_seconds || 300;
  field('model-provider-family').value = item.provider_family || item.id;
  field('model-provider-runtime').value = item.runtime_provider || item.adapter_type;
  field('model-provider-access').value = item.access_source || 'api_key';
  field('model-provider-usage').value = item.usage_semantics || 'metered_api';
  field('model-provider-health').value = item.health || 'unknown';
}

export function providerCatalogPayload() {
  return {
    catalog_discovery_enabled: Boolean(field('model-provider-catalog-discovery')?.checked),
    catalog_required: Boolean(field('model-provider-catalog-required')?.checked),
    catalog_ttl_seconds: Number(field('model-provider-catalog-ttl')?.value || 300),
    provider_family: field('model-provider-family')?.value.trim() || field('model-provider-id')?.value.trim(),
    runtime_provider: field('model-provider-runtime')?.value.trim() || field('model-provider-adapter')?.value.trim(),
    access_source: field('model-provider-access')?.value || 'api_key',
    usage_semantics: field('model-provider-usage')?.value || 'metered_api',
    health: field('model-provider-health')?.value || 'unknown',
  };
}
