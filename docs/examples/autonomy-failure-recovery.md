# Worked example: bounded autonomy and healthy failure

## Scenario

A CI failure event arrives for a production service. Policy permits investigation and preparation automatically but requires approval for a high-risk provider action.

## Success path

1. Canonical event is committed.
2. Deterministic observation/routing runs first.
3. Bounded reasoning proposes one remediation ActionIntent.
4. Effective autonomy policy evaluates Role, Resource risk, qualification and budgets.
5. Required ApprovalRequest is created.
6. Attention routes the approval to humans.
7. After quorum, the ActionIntent executes through the provider.
8. Provider verification + Evidence prove the result.
9. The cycle/audit records preserve model/provider/runtime attribution.

## Healthy refusal

The same event targets a Resource outside the actor's authority.

Expected result: policy/authority denies it before provider execution. A model saying “this is urgent” does not change authority.

## Healthy budget block

An agent attempts more actions/model tokens than the cycle budget.

Expected result: the cycle blocks/stops. The system does not silently expand the budget to finish the task.

## Approval expiry

Humans do not approve before expiry.

Expected result: the old ApprovalRequest cannot be consumed. The action remains blocked and human Attention reflects the required intervention.

## Provider throttling

The model/action provider returns a rate limit with Retry-After.

Expected result:

- ProviderCapacity records throttling;
- work waits until the retry boundary;
- Capacity/circuit controls prevent a retry storm;
- critical incident/reconciliation capacity remains bounded/reserved.

## Incident escalation

If the production condition becomes a SEV1 Incident:

- canonical Incident command/ownership is explicit;
- containment/recovery uses ActionIntents;
- restoration requires machine-readable Evidence;
- independent verification is required by default for SEV1 resolution.

## Recovery / reconciliation

After an outage, codex-web must not unleash every overdue action simultaneously. Scheduler misfire rules, durable outbox batches, capacity admissions and recovery-storm limits bound catch-up.

An isolated Recovery restore pauses autonomy/schedules and puts nonterminal ActionIntents into reconciliation before external side effects can resume.
