# Canonical Incident domain

## Status

Canonical production-operations incident contract. Incidents are durable canonical
records; logs, alerts, model chats and dashboards may detect or discuss an
incident but do not replace Incident state.

## Lifecycle

The canonical lifecycle is:

detected -> triaged -> active -> contained -> monitoring -> resolved ->
postmortem -> closed

Legal transitions are deterministic. Resolution is a dedicated operation because
it requires restoration Evidence and cannot be achieved by changing a status
field directly.

Each Incident retains tenant/workspace/project scope, severity, affected Resource
IDs, detection source/event, commander and owners, impact/customer effect,
related Goals/Decisions/Work/Releases, ActionIntents, Evidence, runbooks,
Attention item, occurrence count, policy, reasoning budget and a structured
timeline.

## Detection and correlation

Detection accepts deterministic provider/health/security/action facts and
computes a bounded dedupe fingerprint when the source does not supply one.
Repeated active alerts with the same fingerprint increment occurrence count and
append timeline evidence instead of creating parallel incidents.

New incidents create one canonical Attention item. SEV1 maps to critical
attention, SEV2 to high, SEV3 to warning and SEV4 to info. SEV1/SEV2 receive
mandatory timed escalation policy. The Attention service remains responsible for
human inbox delivery/escalation.

## Incident command

Triage explicitly assigns or updates commander/owners and structured impact.
Command handoff is an append-only timeline event carrying previous/new commander
and reason. There is no implicit commander inferred from chat participation.

## Containment and recovery

Containment and recovery never call providers directly. IncidentAction requests
create ordinary ActionIntents with:

- canonical affected Resource IDs;
- incident identity/severity/reason;
- credential reference only;
- provider/action binding;
- mandatory provider verification;
- action-specific rollback requirement.

ActionIntent creation re-evaluates canonical Role authority, trust/security,
credential and provider capability. Incident severity does not bypass those
checks. A denied ActionIntent blocks the incident operation.

Emergency approval changes are not implemented inside Incident. canonical break-glass
remains the only pre-authorized, quorum/MFA/audit-bound mechanism for temporarily
raising autonomy/approval behavior.

## Resolution

Resolution requires valid PASS Evidence with a configured machine-readable type,
by default deployment_verification or runtime_result.

SEV1 additionally requires an independent VERIFIED Verification referencing the
restoration Evidence. This prevents an incident from being marked resolved from
the same actor/provider assertion that performed recovery.

Resolving an incident records Evidence in the timeline and resolves the linked
Attention item.

## Reasoning budgets and runbooks

IncidentPolicy carries severity-specific bounded reasoning attempts, token/cost
limits and agent-handoff limits. SEV4 defaults to zero model reasoning. These
values are data for orchestration policy; the Incident domain does not start an
open-ended agent conversation.

Runbook/procedure Knowledge IDs can be attached to the Incident so deterministic
procedures can be consulted before model reasoning.

## Postmortem and learning

A postmortem is structured canonical state with summary, contributing factors,
Evidence and corrective Work Item/Goal references. When requested, the service
publishes the postmortem into Organizational Memory as a POSTMORTEM Knowledge
record with canonical references back to the Incident and corrective work.

This makes learning reusable through the existing governed memory/retrieval
boundary instead of leaving it in chat or free-form logs.

## Events and audit

Incident transitions emit canonical incident.status events where the event
service is configured. The Incident timeline is authoritative domain history;
canonical events provide cross-domain activation/correlation. Related
ActionIntents, Evidence, Attention and canonical autonomy audit remain independently
authoritative for their own domains.

## API and UI

The /api/incidents surface supports detection/list/read, reasoning-budget
inspection, triage, command handoff, legal lifecycle transitions,
containment/recovery ActionIntents, Evidence attachment, resolution and
postmortem.

Human mutations require administrator authority + MFA; service principals need
incident:admin.

Issues #121/#125/#127 own product presentation: command/timeline, affected
Resources, containment/recovery actions, restoration Evidence, postmortem and
critical-incident prominence in the shared operator inbox.
