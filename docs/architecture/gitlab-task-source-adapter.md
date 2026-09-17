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

The adapter declares:

- discovery;
- read;
- event normalization;
- owner write-back;
- state write-back.

Comments and artifact-link mutation are not declared until the GitLab transport provides explicit implementations. Calls to unsupported capabilities fail closed through `UnsupportedTaskSourceCapability`.

## Discovery and read

Discovery preserves the existing GitLab group-issue behavior: the configured provider scope is interpreted as a GitLab group scope and opened issues are normalized into `TaskSourceSnapshot` objects.

Read operations parse the routable external ID and fetch the exact project issue through `GitLabClient`.

Invalid/unroutable issue records are not allowed to invent canonical identity.

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

## Event normalization

Issue webhook payloads normalize into `TaskSourceEvent` plus a provider-neutral snapshot. Event normalization does not mutate work-item state.

Non-issue GitLab events remain outside this initial issue-adapter boundary until their authoritative task identity and canonical semantics are migrated explicitly; they must not be forced into an issue identity merely to satisfy the adapter contract.

## Conformance

Representative discovery, read, event, and projection outputs must pass the shared `TaskSourceConformanceSuite`. GitLab-specific tests add transport/label/lifecycle assertions on top of that shared gate.

The runtime migration must preserve current stale-event handling, accepted-handoff protection, canonical transition authority, and existing GitLab-backed work references while callers are switched from special-case GitLab helpers to this adapter.
