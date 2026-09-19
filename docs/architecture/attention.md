# Canonical Human Attention

Issue: #160.

AttentionItem is the durable operator-inbox primitive for states that require human intervention. It represents the need for attention, not a copy of the source domain's state.

## Invariants

- The source domain remains authoritative. ApprovalRequest owns approval lifecycle/quorum; ActionIntent owns external action/reconciliation state; incidents own incident state.
- AttentionItem stores a source reference, reason, routing, severity, due/expiry metadata and operator lifecycle only.
- Repeated source events coalesce by deterministic dedupe key rather than creating notification spam.
- Canonical state is persisted before any notification adapter is invoked. Notification-provider failure cannot roll back or lose the AttentionItem.
- Notification adapters are provider-neutral and receive canonical AttentionItems after persistence.
- Critical/mandatory escalation is represented in canonical escalation policy and cannot be weakened by a delivery-provider preference.
- Escalation uses the durable scheduler and canonical schedule events; no process-local sleeps and no LLM polling.
- Acknowledgement and resolution retain the acting canonical identity.
- UI refresh reads canonical state only and never causes model reasoning.
- Notification content must contain only the minimum operator-safe summary and source/deep-link metadata; secrets and authentication material are never embedded.

## Lifecycle

Canonical statuses are:

- `open`
- `acknowledged`
- `snoozed`
- `resolved`
- `expired`
- `superseded`
- `escalated`

Acknowledgement does not resolve the source condition. Resolution records operator handling of the AttentionItem; source-domain state remains independently authoritative.

## Routing and visibility

Items carry organization/workspace scope plus optional owner, recipient identities and recipient teams. Unrouted items are visible within their tenant/workspace to eligible operators. Explicitly routed items are visible to their recipients/teams/owner and administrative operators.

Routing controls delivery and Inbox visibility only. It never grants authority to mutate the source object.

## ApprovalRequest integration

Approval transition events are projected deterministically:

- pending/partially-approved -> one deduped `approval.required` AttentionItem
- rejected/expired/invalidated -> operator-review AttentionItem
- approved/consumed/cancelled/superseded -> resolve the existing approval AttentionItem

The item deep-links to the exact ApprovalRequest. It never copies or recalculates approval quorum.

## Escalation

An AttentionItem may carry an EscalationPolicy with a durable escalation timestamp and mandatory routing. The scheduler emits `schedule.due`; the attention service recognizes its own trigger type and transitions the canonical item to escalated. Duplicate schedule firing is suppressed by scheduler/event idempotency.

## Notification boundary

Adapters implement a provider-neutral delivery contract. Delivery occurs only after the canonical item is safely persisted. Adapter errors are isolated and logged; the Inbox remains complete and can be retried or delivered through another provider later.

The browser/in-app Inbox is the baseline delivery surface and reads `/api/attention` directly.

## Operator UI

The Inbox shows type, severity, source, due/expiry state, owner, escalation count and source deep link, with deterministic filters and acknowledge/snooze/resolve controls. Source-specific decisions continue through source-domain APIs—for example, approval decisions remain in the canonical ApprovalRequest UI.
