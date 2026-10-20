import { request } from './api_client.js';

export function createWorkItemSourceActions(state, { sourceEditor, refreshAll, setStatus }) {
async function syncSource() {
  if (!state.projectId || !sourceEditor.canLeave()) return;
  const op = sourceEditor.capture();
  setStatus('Reconciling authoritative source…');
  try {
    const result = await request(`/api/work-items/sync/${encodeURIComponent(op.projectId)}`, {
      method: 'POST',
      body: JSON.stringify({ actor: 'operator', reason: 'operator requested source resync' }),
    });
    if (!op.current()) return;
    await refreshAll();
    if (op.current()) setStatus(`Synced ${result.synced ?? 0} work items`);
  } catch (error) {
    if (op.current()) setStatus(error.message || 'Source sync failed', true);
  }
}
async function fetchSourceRecord() {
  if (!state.projectId || !sourceEditor.canLeave()) return;
  const reference = document.querySelector('.work-items-source-reference').value.trim();
  if (!reference) {
    setStatus('Enter the exact existing source reference', true);
    return;
  }
  const op = sourceEditor.capture();
  setStatus('Fetching the existing source record…');
  try {
    // No fallback: old servers must refuse this action, never resync all items.
    const result = await request(`/api/work-items/sync-source-record-v1/${encodeURIComponent(op.projectId)}`, {
      method: 'POST',
      body: JSON.stringify({ reference, reason: 'operator requested one existing source record' }),
    });
    if (!op.current()) return;
    await refreshAll();
    if (op.current()) setStatus(`Fetched ${result.synced ?? 0} existing source record`);
  } catch (error) {
    if (op.current()) setStatus(error.message || 'Source record could not be fetched', true);
  }
}
  return { syncSource, fetchSourceRecord };
}
