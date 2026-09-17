# GitLab Task-Source Adapter

## Purpose

`GitLabTaskSource` is the GitLab implementation of the provider-neutral `TaskSource` contract. GitLab API payloads, label conventions, issue identifiers, and webhook shapes belong in this adapter rather than canonical work-item/execution interfaces.

This document describes durable adapter semantics. GitHub issues and pull requests remain the delivery-status source of truth.

## Source identity

The adapter uses:

- `source_type = gitlab`;
- the normalized GitLab API base URL as `source_instance`;
- the routable GitLab full issue reference (`group/project#iid`) as `external_id`;
- the GitLab issue URL and update timestamp as optional provenance when available.

The full reference is intentionally separate from codex-web's canonical `WorkItemState.ref` even when they currently happen to match.

## Supported capabilities

The API-backed adapter declares:

- discovery;
- read;
- event normalization;
- owner write-back;
- state write-back.

Comments and artifact-link mutation are not declared until the GitLab transport provides explicit implementations. Calls to unsupported capabilities fail closed through `UnsupportedTaskSourceCapability`.

`GitLabWebhookTaskSource` is a deliberately narrower adapter instance that declares only event normalization. Signed webhook payload normalization does not require a GitLab API token. This keeps credential availability separate from adapter semantics instead of requiring a fake credential for webhook-only operation.

## Discovery and read

Discovery preserves the existing GitLab group-issue behavior: the configured provider scope is interpreted as a GitLab group scope and opened issues are normalized into `TaskSourceSnapshot` objects.

Read operations parse the routable external ID and fetch the exact project issue through `GitLabClient`.

Invalid/unroutable issue records are not allowed to invent canonical identity.

Normalized snapshots are persisted through the shared `TaskSourceWorkItemProjector`. Existing canonical work-item refs are preserved by matching persisted provider identity; provider IDs never overwrite canonical identity merely because a source is migrated behind the adapter.

## Owner semantics

GitLab assignees are normalized into the snapshot as provider facts. Canonical codex-web ownership continues to use the existing `owner::<agent>` label convention during migration.

`project(...)` therefore derives canonical owner only from `owner::` labels. A GitLab assignee without an `owner::` label does not silently become a codex-web agent binding.

Owner write-back replaces only `owner::` labels and preserves unrelated/status labels.

## Lifecycle semantics

The adapter preserves the existing deterministic label mapping:

- GitLab closed/merged state -> `closed`;
- `status::awaiting confirmation` -> `ready_for_validation`;
- `status::blocked` -> `failed_with_action_owner`;
- `status::in progress` -> preserve a compatible active canonical lane when supplied, otherwise `implementation_active`.

State write-back projects the inverse operational labels:

- active implementation/validation/release lanes -> `status::in progress`;
- ready for validation -> `status::awaiting confirmation`;
- blocked -> `status::blocked`;
- closed -> remove the status label and issue a GitLab close event.

Writing a nonterminal state to a closed GitLab issue issues an explicit reopen event.

## Event normalization and reconciliation

Issue webhook payloads normalize into `TaskSourceEvent` plus a provider-neutral snapshot. Event normalization itself does not mutate work-item state.

Normalized issue events pass through `TaskSourceWorkItemEventReconciler`, which applies the shared authority/staleness policy and then delegates successful projections to `TaskSourceWorkItemProjector`. This keeps provider transport, reconciliation policy, and canonical persistence as separate deterministic boundaries.

The GitLab routing service may continue to own provider-specific notification/routing behavior. It must not require canonical work-item code to inspect a GitLab webhook payload in order to project issue state.

Non-issue GitLab events remain outside the issue-task adapter boundary until their authoritative task/artifact identity and canonical semantics are migrated explicitly; they must not be forced into an issue identity merely to satisfy the adapter contract.

## Conformance

Representative discovery, read, event, and projection outputs must pass the shared `TaskSourceConformanceSuite`. GitLab-specific tests add transport/label/lifecycle assertions on top of that shared gate.

Runtime migration must preserve stale-event handling, accepted-handoff protection, canonical transition authority, and existing GitLab-backed work references while callers are switched from special-case GitLab helpers to this adapter.
