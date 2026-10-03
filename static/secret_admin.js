import { confirmAction } from './action_confirmation.js';
import { request as rawApiRequest } from './api_client.js';
import { currentProjectId } from './project_view_scope.js';
import { formValidation } from './form_validation.js';
import { esc, secretOperation, canManage, renderReferences, renderImpact, secretPath, projectPath } from './secret_reference_ui.js';

const MAX_AUDIT_ROWS = 100;
let identityNames = new Map(), actor = null, generation = 0, items = [];
let validation;
const byId = id => document.getElementById(id);
const identityText = id => identityNames.has(id) ? `${identityNames.get(id)} (${id})` : id || 'none';
const timeText = value => value ? new Date(Number(value) * 1000).toLocaleString() : 'none';
function setStatus(message) { if (byId('secret-admin-status')) byId('secret-admin-status').textContent = message; }
function clearValues() { document.querySelectorAll('#secret-create-value,[data-secret-rotate-value]').forEach(input => { input.value = ''; }); }
function clearView() {
  generation += 1; actor = null; items = []; identityNames.clear(); clearValues();
  for (const id of ['secret-admin-list', 'secret-audit-list', 'secret-broken-references']) byId(id)?.replaceChildren();
  for (const id of ['secret-create-name', 'secret-create-provider', 'secret-create-purpose', 'secret-create-expires']) if (byId(id)) byId(id).value = '';
  byId('create-secret')?.setAttribute('disabled', '');
  setStatus('Select or refresh a Project to inspect shared secret references.');
}
function identityChoices(state) {
  const members = new Set((state?.memberships || []).filter(item => item.organization_id === actor?.organization_id && item.workspace_id === actor?.workspace_id && !item.revoked_at).map(item => item.identity_id));
  const rows = [...(state?.humans || []).map(item => [item.id, item.display_name || item.email || item.id]), ...(state?.services || []).map(item => [item.id, item.name || item.id])].filter(([id]) => members.has(id));
  identityNames = new Map(rows);
  for (const id of ['secret-create-use-identities', 'secret-create-reveal-identities']) {
    const select = byId(id); if (!select) continue;
    select.innerHTML = rows.map(([key, name]) => `<option value="${esc(key)}">${esc(name)} · ${esc(key)}</option>`).join('');
    select.disabled = !state || !canManage(actor);
  }
}
function renderAudit(events, error) {
  if (!byId('secret-audit-list')) return;
  byId('secret-audit-status').textContent = error ? 'Workspace audit unavailable: admin authority and elevated assurance are required.' : `${events.length} workspace audit event(s). This audit is shared across Projects.`;
  byId('secret-audit-list').innerHTML = (events || []).slice(-MAX_AUDIT_ROWS).reverse().map(item => `<div class="comm-entry"><strong>${esc(item.action)} · ${esc(item.outcome)} · ${esc(item.secret_id)}</strong><small>${timeText(item.created_at)} · ${esc(identityText(item.actor_identity_id))}</small></div>`).join('') || '<p>No visible audit events.</p>';
}
function render() {
  const query = (byId('secret-search')?.value || '').trim().toLowerCase();
  byId('secret-admin-list').innerHTML = renderReferences(items.filter(item => `${item.id} ${item.name} ${item.provider || ''}`.toLowerCase().includes(query)), { manageable: canManage(actor), identityText, timeText });
}
async function showUsage(secretId, row, op = secretOperation(setStatus)) {
  const output = row?.querySelector('[data-secret-usage-result]');
  if (output) output.textContent = 'Loading canonical consumer references…';
  try {
    const usage = await op.request(`/api/secrets/${encodeURIComponent(secretId)}/usage`);
    if (!usage.available) throw new Error('Consumer impact unavailable');
    if (output?.isConnected) output.innerHTML = renderImpact(usage);
    return usage;
  } catch (error) {
    if (op.current() && output?.isConnected) output.textContent = 'Consumer impact unavailable. Retry before rotating or revoking this reference.';
    throw error;
  }
}
async function refresh() {
  if (!currentProjectId()) { clearView(); return; }
  const op = secretOperation(setStatus); const visit = ++generation;
  const current = () => op.current() && visit === generation;
  const apiRequest = op.request;
  clearValues(); setStatus('Loading shared secret metadata for this Project…');
  try {
    const [response, me, state, audit] = await Promise.all([
      apiRequest('/api/secrets'), rawApiRequest('/api/identity/me'),
      rawApiRequest('/api/identity').catch(() => null),
      apiRequest('/api/secrets/audit').then(value => ({ items: value.items })).catch(() => ({ items: [], error: true })),
    ]);
    if (!current()) return;
    actor = me; items = response.items || []; identityChoices(state); render(); renderAudit(audit.items || [], audit.error);
    byId('create-secret').disabled = !canManage(actor);
    byId('secret-create-assurance').textContent = canManage(actor) ? 'Workspace secret administration permitted; server authorization remains authoritative.' : 'Read only: creation, rotation and revocation require canonical admin authority and elevated assurance.';
    byId('secret-broken-references').innerHTML = (response.broken_references || []).map(item => `<div class="comm-entry"><strong>Missing or unusable reference: ${esc(item.secret_id)} · ${esc(item.status)}</strong><p>Replace or remove this reference in its consumer configuration; requesting metadata does not grant use permission.</p><a href="${esc(projectPath('work-items'))}">TaskSource settings</a> · <a href="${esc(projectPath('configuration'))}">Configuration</a></div>`).join('');
    setStatus(`${items.length} workspace secret reference(s) available in Project ${currentProjectId()}. Stored values are never returned.${response.impact_available ? '' : ' Consumer impact is unavailable; retry before a lifecycle change.'}`);
    const selected = new URLSearchParams(location.search).get('secret_id');
    if (selected) {
      const row = [...document.querySelectorAll('[data-secret-row]')].find(item => item.dataset.secretRow === selected);
      if (row) { row.scrollIntoView({ block: 'nearest' }); await showUsage(selected, row, op); }
      else setStatus('This reference is missing or unavailable to your identity. Check the consumer binding or contact a workspace administrator.');
    }
  } catch (error) { if (current()) { items = []; render(); setStatus(`Secret metadata unavailable: ${error.message}. Refresh to retry.`); } }
}
async function createSecret() {
  if (!canManage(actor)) return;
  if (!validation.validate()) return;
  const op = secretOperation(setStatus); const apiRequest = op.request;
  const name = byId('secret-create-name').value.trim();
  if (!await confirmAction({ action: 'Create workspace secret', target: name, risk: 'bounded', consequence: `Create workspace secret reference "${name}"? It will be available by canonical ACL across Projects, never rendered back, and must be bound through its consumer settings.`, recovery: 'The stored value is never displayed. Rotate or revoke through the canonical lifecycle.' })) return;
  const valueInput = byId('secret-create-value'); const value = valueInput.value; valueInput.value = "";
  const expires = byId('secret-create-expires').value;
  byId('create-secret').disabled = true;
  try {
    const response = await apiRequest('/api/secrets', { method: 'POST', body: JSON.stringify({
      name, value, provider: byId('secret-create-provider').value.trim() || null,
      purpose: byId('secret-create-purpose').value.trim() || null,
      allowed_identity_ids: [...byId('secret-create-use-identities').selectedOptions].map(item => item.value),
      reveal_identity_ids: [...byId('secret-create-reveal-identities').selectedOptions].map(item => item.value),
      expires_at: expires ? new Date(expires).getTime() / 1000 : null,
    }) });
    if (!op.current()) return;
    validation.clear(); history.replaceState(history.state, '', secretPath(response.item.id));
    await refresh(); op.status(`Created ${name}. Bind its reference through TaskSource or configuration settings.`);
  } catch (error) { if (op.current()) { setStatus('Secret creation failed. Check your metadata and permissions; re-enter the write-only value to retry.'); validation.server({ detail: 'The request could not be saved. Your secret value was cleared; re-enter it to retry.' }); } }
  finally { if (op.current()) byId('create-secret').disabled = !canManage(actor); }
}
async function mutate(button, rotate) {
  if (!canManage(actor)) return;
  const secretId = rotate ? button.dataset.secretRotate : button.dataset.secretRevoke;
  const row = button.closest('[data-secret-row]'); const valueInput = row.querySelector('[data-secret-rotate-value]');
  if (rotate && !valueInput?.value) { setStatus('Enter a replacement value before rotation.'); valueInput?.focus(); return; }
  const op = secretOperation(setStatus); const apiRequest = op.request; button.disabled = true;
  try {
    const usage = await showUsage(secretId, row, op);
    if (!await confirmAction({ action: rotate ? 'Rotate secret' : 'Revoke secret', target: secretId, risk: 'high',
      consequence: rotate ? 'Future use across Projects resolves the replacement value.' : 'Future use is denied across all Projects.',
      impact: `${usage.count} canonical reference(s), including ${usage.outside_view_count} outside this Project view. ${(usage.limitations || []).join(' ')}`,
      recovery: 'Stored material cannot be revealed or recovered here. Bind a replacement reference through each consumer when needed.', current: op.current, trigger: button })) return;
    if (rotate) {
      const value = valueInput.value; valueInput.value = "";
      await apiRequest(`/api/secrets/${encodeURIComponent(secretId)}/rotate`, { method: 'POST', body: JSON.stringify({ value }) });
    } else {
      if (valueInput) valueInput.value = "";
      await apiRequest(`/api/secrets/${encodeURIComponent(secretId)}`, { method: "DELETE" });
    }
    if (op.current()) { await refresh(); op.status(`${rotate ? 'Rotated' : 'Revoked'} ${secretId}. Workspace audit records the mutation.`); }
  } catch (error) { if (op.current()) { if (valueInput) valueInput.value = ""; setStatus('Secret lifecycle change failed or impact is unavailable. Refresh consumer impact and verify permissions; re-enter a replacement value if needed.'); } }
  finally { if (op.current() && button.isConnected) button.disabled = false; }
}
function bind() {
  if (!byId('secret-admin-list')) return;
  validation = formValidation(byId('secret-create-panel'));
  byId('secret-create-name').required = true; byId('secret-create-value').required = true;
  byId('refresh-secrets').addEventListener('click', refresh);
  byId('secret-search')?.addEventListener('input', () => { clearValues(); render(); });
  byId('create-secret').addEventListener('click', createSecret);
  byId('secret-admin-list').addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.dataset.secretUsage) showUsage(button.dataset.secretUsage, button.closest('[data-secret-row]')).catch(() => {});
    else if (button.dataset.secretRotate) mutate(button, true);
    else if (button.dataset.secretRevoke) mutate(button, false);
  });
  window.addEventListener('codex:project-changed', () => { clearView(); validation.clear(); if (/\/secrets\/?$/.test(location.pathname)) refresh(); });
  window.addEventListener('popstate', () => { if (/\/secrets\/?$/.test(location.pathname)) refresh(); });
  window.addEventListener('codex:project-workspace-page', event => { if (event.detail?.workspace !== 'secrets') clearValues(); });
  clearView(); if (/\/secrets\/?$/.test(location.pathname)) refresh();
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind, { once: true }); else bind();
