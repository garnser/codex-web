import { confirmAction } from './action_confirmation.js';
import { secretOperation, canManage, secretPath } from './secret_reference_ui.js';

export function secretCreator({ draft, validation, actor, refresh, setStatus }) {
  const byId = id => document.getElementById(id);
  return async function createSecret() {
    if (!canManage(actor()) || byId('create-secret').disabled) return;
    if (!validation.validate()) return;
    const ticket = draft.submission();
    const op = secretOperation(setStatus); const apiRequest = op.request;
    const name = byId('secret-create-name').value.trim();
    if (!await confirmAction({ action: 'Create workspace secret', target: name, risk: 'bounded', consequence: `Create workspace secret reference "${name}"? It will be available by canonical ACL across Projects, never rendered back, and must be bound through its consumer settings.`, recovery: 'The stored value is never displayed. Rotate or revoke through the canonical lifecycle.' })) return;
    if (!ticket.current() || byId('create-secret').disabled) return;
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
      if (!op.current() || !ticket.current()) return;
      ticket.saved();
      if (draft.dirty()) { validation.clear(); op.status(`Created ${name}. Newer reference metadata remains unsaved.`); return; }
      validation.clear(); history.replaceState(history.state, '', secretPath(response.item.id));
      await refresh(); op.status(`Created ${name}. Bind its reference through TaskSource or configuration settings.`);
    } catch (error) { if (op.current() && ticket.current()) { setStatus('Secret creation failed. Check your metadata and permissions; re-enter the write-only value to retry.'); validation.server({ detail: 'The request could not be saved. Your secret value was cleared; re-enter it to retry.' }); } }
    finally { if (op.current()) byId('create-secret').disabled = !canManage(actor()); }
  };
}
