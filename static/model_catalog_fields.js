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
export function resetModelCatalogFields() {
  field('model-definition-availability').value = 'static';
  field('model-definition-upstream-provider').value = '';
  field('model-definition-upstream-model').value = '';
  field('model-definition-family').value = '';
}
export function loadModelCatalogFields(item) {
  field('model-definition-availability').value = item.availability_source || 'static';
  field('model-definition-upstream-provider').value = item.upstream_provider_id || '';
  field('model-definition-upstream-model').value = item.upstream_model_id || '';
  field('model-definition-family').value = item.model_family || '';
}
export function modelCatalogPayload() {
  return {
    availability_source: field('model-definition-availability')?.value || 'static',
    upstream_provider_id: field('model-definition-upstream-provider')?.value.trim() || null,
    upstream_model_id: field('model-definition-upstream-model')?.value.trim() || null,
    model_family: field('model-definition-family')?.value.trim() || null,
  };
}

export function numberOrNull(id) {
  const value = document.getElementById(id)?.value ?? "";
  return value === "" ? null : Number(value);
}
