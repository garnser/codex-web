# ConversationChannel provider-neutral conversational intake

## Purpose

ConversationChannel is the provider-neutral inbound conversation boundary for
Slack, Telegram, Microsoft Teams and similar collaborative systems.

It is intentionally separate from TaskSource:

- TaskSource represents authoritative external work records.
- ConversationChannel represents conversational identity, messages and provider
  interaction semantics.
- AttentionItem remains the canonical attention/escalation model.
- Work/Goal/Decision state remains canonical codex-web state.

Provider message, thread or user IDs never become canonical application
identity or authority.

## Code-owned contract

Each adapter declares an immutable contract version, provider type/instance and
explicit capabilities:

- events;
- history;
- threads;
- attachments;
- references;
- mentions;
- commands;
- reactions;
- edits;
- deletes;
- cursors.

Unsupported capabilities fail explicitly. Providers do not emulate semantics
they do not support.

The built-in normalization adapters intentionally differ:

- Slack: threads, attachments, references, mentions/commands, reactions,
  edits/deletes and cursor metadata.
- Telegram: topics/replies, attachments, references, mentions/commands,
  edits and update cursors.
- Microsoft Teams: Graph-style chat/channel messages, attachments, references,
  mentions and edit/delete change notifications. Graph subscription/auth
  configuration remains an integration/extension concern rather than being
  fabricated inside the normalization contract.

## Normalized identity

ExternalConversationRef contains provider type, provider instance, conversation
ID and optional external thread ID.

ExternalMessageRef adds the provider-owned message ID.

Those references are provenance only. Canonical thread/work identity is created
by the existing routing layer.

ConversationSenderRef contains untrusted provider metadata. Provider user IDs,
tenant claims, roles or bot claims never grant codex-web authority. The
authenticated canonical actor that ingests the event determines tenant/workspace
scope.

## Canonical events and deduplication

Every normalized inbound event is committed first as
`conversation_channel.event`.

The provider delivery ID is the canonical event idempotency key. A repeated
delivery therefore dispatches once.

Providers can retry the same provider message under a different delivery ID.
ConversationChannel maintains tenant-scoped external-message position and rejects
a non-newer retry as stale before routing.

Message state keys include organization/workspace as well as provider/message
identity, so identical Slack/Teams/Telegram IDs in separate tenants cannot
collide.

## Ordering

When a provider has a monotonic sequence, a lower/equal sequence is stale.

Without a sequence, normalized occurrence time is the primary ordering position.
Provider-specific adapters must therefore normalize edit/delete occurrence time
to the provider change timestamp, not the original message timestamp.

Slack message edits use the edit timestamp. Telegram edits use edit_date.
Teams edits/deletes use Graph lastModifiedDateTime/deletedDateTime.

Opaque revisions are retained for provenance but are not treated as universal
cross-provider ordering tokens.

## Edits, deletes and reactions

A newly-created provider message can route into canonical thread/work semantics.

Later edits, deletes and reactions update provider conversation state only.
They do not replay existing canonical work automatically.

This prevents a Slack edit, Teams change notification or reaction retry from
creating duplicate Work or turns.

## Unknown routing outcomes

The canonical event and routing claim are durable before downstream routing.

If routing raises after the claim, the receipt becomes
`requires_reconciliation`. Automatic retry of the same delivery does not
blindly route again, because the downstream outcome may be unknown.

This follows the same fail-closed external-outcome principle used elsewhere in
codex-web.

## Attachments and references

Adapters normalize attachment/reference metadata only. ConversationChannel does
not download arbitrary provider content into model context.

Attachment URLs/IDs remain external references unless another governed
Artifact/Evidence flow explicitly materializes them.

## Slack and Telegram migration

The production Slack webhook/backfill, Slack socket runtime, Telegram webhook
and Telegram polling runtime receive the composed ConversationChannelService.

Their legacy direct BotInboundMessage route is retained only as a temporary
compatibility fallback while the runtime extraction/refactor completes.

Canonical Work/Thread routing semantics are unchanged.

## Microsoft Teams

TeamsConversationChannel normalizes Microsoft Graph message/change-notification
shapes without changing Work/Goal/Decision schemas.

The normalizer is deliberately read/inbound-only. A deployment may pair it with
an extension that owns Graph subscription lifecycle and SecretReferences.

No direct provider send/update/delete API exists on ConversationChannel.

## Outbound actions

Consequential external mutations—messages, replies, reactions, subscription
changes or other provider writes—must use the existing ActionIntent /
ActionProvider authority/evidence path.

ConversationChannel does not grant outbound authority simply because an inbound
provider connection exists.

## Extensions and secrets

ConversationChannel is an extension type with its own manifest entrypoint.

Extension registration can attach an availability gate so disabled,
quarantined or incompatible installations cannot resolve their adapter.

Credential material never belongs in ConversationChannel normalized events.
Existing bot/provider integrations use SecretReferences/SecretBroker. The
capability/provenance inspector exposes no raw secret material.

## Operator inspection

The read-only API exposes:

- code-owned provider capability contracts;
- recent tenant-scoped canonical message states;
- recent tenant-scoped routing projection receipts.

The Bot Integration dialog uses this API to show provider capabilities and
routing provenance. It does not create shadow integration state.

## Failure semantics

- unsupported capability -> explicit capability error;
- incompatible contract -> fail closed;
- duplicate delivery -> duplicate receipt, no reroute;
- same message under a newer delivery but non-newer provider position -> stale;
- out-of-order edit -> stale;
- delete -> provider state marked deleted, no canonical replay;
- reaction -> recorded, no canonical replay;
- routing exception after claim -> requires_reconciliation;
- cross-tenant same external IDs -> independent state;
- provider identity claim -> provenance only, no authority.
