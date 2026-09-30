import { confirmAction } from './action_confirmation.js';
import { request } from './api_client.js';
import { captureProjectView } from './project_view_scope.js';

export async function confirmResourceChange(resourceId, payload, originalLifecycle, root, button, report) {
  const lifecycle = payload.lifecycle;
  if (lifecycle === originalLifecycle || !['disabled', 'deleted'].includes(lifecycle)) {
    return confirmAction({ action: 'Update resource', target: resourceId,
      consequence: `Lifecycle: ${lifecycle}; risk: ${payload.risk}; sensitivity: ${payload.sensitivity}.`,
      recovery: 'Metadata can be edited again through the canonical resource editor.', current: () => root.isConnected, trigger: button });
  }
  const view = captureProjectView();
  try {
    const result = await request(`/api/resources/${encodeURIComponent(resourceId)}/relationships?direction=both`);
    if (!Array.isArray(result.items)) throw new Error('Relationship inventory unavailable');
    const relationships = result.items;
    return await confirmAction({ action: lifecycle === 'deleted' ? 'Delete resource registration' : 'Disable resource', target: resourceId, risk: 'high',
      consequence: `Lifecycle becomes ${lifecycle}. Privileged resolution will no longer treat this resource as available.`,
      impact: `${relationships.length} canonical relationship(s): ${relationships.slice(0, 100).map(item => `${item.relationship_type}: ${item.from_resource_id} → ${item.to_resource_id}`).join('; ')}. ${relationships.length > 100 ? 'Only the first 100 are displayed. ' : ''}This is the registered relationship projection, not a complete external dependency scan.`,
      recovery: 'This changes the canonical registration, not the external asset. A later lifecycle edit remains subject to canonical validation.', current: () => view.current() && root.isConnected, trigger: button });
  } catch (error) { if (view.current() && root.isConnected) report(`Resource lifecycle blocked: ${error.message}`); return false; }
}
