import { request } from './api_client.js';

export function renderProjectDelivery(project, field, capture, fmtTime) {
  const delivery = project?.delivery_supervision;
  field('delivery-thread').value = delivery?.thread_id || '';
  field('delivery-status').textContent = delivery?.enabled
    ? `Automatic discovery and delivery enabled every ${delivery.interval_seconds}s for this Project. Actor: ${delivery.actor_identity_id}.`
    : 'Automatic project delivery supervision is disabled.';
  if (delivery) {
    const op = capture();
    request(`/api/projects/${encodeURIComponent(op.projectId)}/delivery-supervision`).then(result => {
      if (!op.current()) return;
      const scan = result.last_scan;
      if (scan) field('delivery-status').textContent += ` Last scan: ${fmtTime(scan.observed_at)} · ${scan.outcome}${scan.error ? ` (${scan.error})` : ''}.`;
    }).catch(error => { if (op.current()) field('delivery-status').textContent += ` Status unavailable: ${error.message}.`; });
  }
}
