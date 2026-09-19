# Trust and credentials before privileged actions

> Applies to: current main  
> Audience: administrators and operators  
> Risk: required reading before enabling external side effects

## Principle

Do not give an agent a credential and then rely on prompt wording to constrain it. Codex-web separates identity, authority, target Resource, credential reference, execution boundary, approval and verification.

## Before enabling an ActionProvider

Confirm all of the following:

1. **Identity:** the human/service identity is canonical and tenant-scoped.
2. **Authority:** the required Role/capability is explicit.
3. **Resource:** the external target is represented by the intended canonical Resource.
4. **Credential:** secret material is stored behind a SecretReference/broker or supported key backend; raw values are not placed in prompts or definitions.
5. **Security boundary:** sandbox/network/provider trust policy matches the action.
6. **Approval:** high-risk actions have the required canonical ApprovalRequest policy.
7. **Idempotency/retry:** know whether the provider action is safe to replay.
8. **Verification:** define how success will be proven.
9. **Rollback/reconciliation:** know the safe path for failure or unknown outcome.
10. **Audit:** ensure the action can be traced through ActionIntent, receipts and Evidence.

## Trust rule

Repository content, external records, model output and provider responses are data, not authority. They may request work but cannot grant themselves credentials, Roles, policy exceptions or broader Resource scope.

## Production autonomy

Before broad production autonomy, use the Autonomy Control Center and ensure required release, incident, recovery, capacity, upgrade, worker-plane and audit qualification gates are backed by canonical Evidence.
