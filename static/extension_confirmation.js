import { confirmAction } from './action_confirmation.js';

export function confirmExtensionLifecycle({ action, label, installationId, lifecycle, button }) {
  return confirmAction({
    action: `${action.replaceAll('-', ' ')} extension`,
    target: `${label} (${installationId})`,
    risk: 'high',
    consequence: action === 'remove'
      ? `Current lifecycle: ${lifecycle}. Removal revokes active grants, stops future runtime use, and preserves the canonical tombstone; hard deletion is not supported.`
      : `Current lifecycle: ${lifecycle}. This updates canonical extension state.`,
    impact: 'Installation, lifecycle, grants and downstream runtime consumers remain canonically enforced. External consumers are not fully enumerated here.',
    recovery: action === 'remove'
      ? 'The tombstone remains; hard deletion and Undo are not supported.'
      : 'Any later enablement or quarantine recovery requires current canonical validation.',
    current: () => button.isConnected,
    trigger: button,
  });
}

export function confirmExtensionRevoke({ installationId, capability, grantId, quarantineWarning, button }) {
  return confirmAction({
    action: 'Revoke extension capability',
    target: `${installationId} / ${capability} / ${grantId}`,
    risk: 'high',
    consequence: `The capability grant stops authorizing use.${quarantineWarning}`,
    recovery: 'A replacement grant requires current canonical authority; clearing quarantine is separate.',
    current: () => button.isConnected,
    trigger: button,
  });
}

export function confirmExtensionGrant({ installationId, capability, scope, button }) {
  return confirmAction({
    action: 'Grant extension capability',
    target: `${installationId} / ${capability}`,
    risk: 'high',
    consequence: `The grant authorizes ${capability} for ${scope}. Installation and enablement remain separate operations.`,
    recovery: 'Revocation requires canonical authority and may quarantine an enabled extension with a mandatory capability.',
    current: () => button.isConnected,
    trigger: button,
  });
}
