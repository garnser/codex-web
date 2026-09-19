# Worked example: TaskSource, ActionProvider, policy and isolated execution

## TaskSource

Project `catalog-api` binds one authoritative fictional GitLab source.

A provider issue is projected to a canonical Work Item. Provider identity/provenance is retained; codex-web does not let the integration mutate lifecycle state through a GitLab-specific shortcut.

### Healthy failure

The GitLab credential is revoked.

Expected behavior:

- synchronization fails visibly;
- existing canonical Work Items remain intact;
- operator repairs the credential/reference;
- synchronization resumes/reconciles;
- no raw token is copied into a prompt.

## Isolated execution

An implementation assignment declares its repository workspace, supported execution contract, capabilities and sandbox/network policy.

A worker whose supported execution-contract version does not match must reject the assignment. Guessing compatibility would be unsafe.

## ActionProvider

After validation, a fictional `deployment` ActionProvider exposes `deploy.release`.

The canonical request contains:

- target Resource;
- immutable release artifact/digest;
- idempotency key;
- credential reference;
- policy/authority decisions;
- verification/rollback requirements.

The provider receives the credential only through the supported broker/boundary.

## Policy change

The team changes policy so production deployment requires two distinct human approvals.

The change should be published through the canonical policy/Definition boundary. The UI should preview effective impact; it must not implement a separate client-only rule.

## Intentionally denied action

A developer without the required Role requests production deployment.

Healthy behavior:

1. authority evaluation denies the action;
2. no provider call occurs;
3. the denial/reason is explainable;
4. changing issue text or model output cannot grant the missing Role.

## Unknown provider outcome

If the network drops after the provider may have accepted a non-idempotent action:

- ActionIntent becomes uncertain/requires reconciliation;
- inspect provider state/receipt;
- do **not** blindly retry;
- reconcile canonical and provider truth first.
