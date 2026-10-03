import { confirmAction } from './action_confirmation.js';

export function confirmKeyLifecycle(action, keyId, row, operation, button, version) {
  let title, consequence, recovery;
  if (action === 'rotate') {
    title = 'Rotate key';
    consequence = 'The current version becomes decrypt-only; the backend generates a new active version. Existing ciphertext is not rewritten.';
    recovery = 'Retained decrypt-only versions remain available for existing ciphertext. Rotation does not restore revoked material.';
  } else if (action === 'revoke-version') {
    title = 'Revoke key version';
    consequence = 'Data encrypted with this version may become undecryptable. The active current version cannot be revoked here.';
    recovery = 'Revocation cannot be undone in this interface. Verify independent recovery material before proceeding.';
  } else if (action === 'revoke') {
    title = 'Revoke entire key';
    consequence = 'All versions become revoked. Dependent encrypted data may become unreadable.';
    recovery = 'Revocation cannot be undone in this interface. Key material is never exposed.';
  } else return Promise.resolve(false);
  return confirmAction({ action: title, target: version ? `${keyId}:v${version}` : keyId, risk: 'high', consequence, recovery,
    impact: row.querySelector('[data-key-usage-result]')?.textContent, current: operation.current, trigger: button });
}
