export {
  loadProviderCatalogFields,
  providerCatalogPayload,
  resetProviderCatalogFields,
} from './model_provider_catalog_fields.js';

const field = id => document.getElementById(id);
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
