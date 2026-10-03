const field = id => document.getElementById(id);

export function resetProviderCatalogFields() {
  field('model-provider-catalog-discovery').checked = false;
  field('model-provider-catalog-ttl').value = 300;
}
export function loadProviderCatalogFields(item) {
  field('model-provider-catalog-discovery').checked = Boolean(item.catalog_discovery_enabled);
  field('model-provider-catalog-ttl').value = item.catalog_ttl_seconds || 300;
}
export function providerCatalogPayload() {
  return {
    catalog_discovery_enabled: Boolean(field('model-provider-catalog-discovery')?.checked),
    catalog_ttl_seconds: Number(field('model-provider-catalog-ttl')?.value || 300),
  };
}
export function resetModelCatalogFields() {
  field('model-definition-availability').value = 'static';
  field('model-definition-upstream-provider').value = '';
  field('model-definition-upstream-model').value = '';
}
export function loadModelCatalogFields(item) {
  field('model-definition-availability').value = item.availability_source || 'static';
  field('model-definition-upstream-provider').value = item.upstream_provider_id || '';
  field('model-definition-upstream-model').value = item.upstream_model_id || '';
}
export function modelCatalogPayload() {
  return {
    availability_source: field('model-definition-availability')?.value || 'static',
    upstream_provider_id: field('model-definition-upstream-provider')?.value.trim() || null,
    upstream_model_id: field('model-definition-upstream-model')?.value.trim() || null,
  };
}

export function numberOrNull(id) {
  const value = document.getElementById(id)?.value ?? "";
  return value === "" ? null : Number(value);
}
