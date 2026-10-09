# Action providers and external side effects

Codex-web external mutations use a provider-neutral `ActionProvider` contract. Core autonomy, Executive, Goal, Decision, and Work Item logic must not consume provider-native request/response objects.

## Contract

ActionProvider contract version **1.2** declares a provider type/instance and a catalog of `ActionDefinition` records. Each action describes, before execution:

- risk class;
- required canonical resource types;
- required authority strings;
- whether a credential reference is required and its purpose;
- prepare/execute/dry-run/idempotency/rollback/verification/progress/evidence capabilities;
- timeout and retry bounds;
- reversibility;
- expected evidence.

Unsupported capabilities raise `UnsupportedActionCapabilityError`; callers must never guess support from provider type.

## Invocation model

`ActionRequest` contains only canonical scope and references:

- organization/workspace;
- optional Project;
- canonical Resource IDs;
- structured provider-neutral parameters;
- optional secret reference;
- optional idempotency key;
- dry-run/correlation/requester metadata.

Raw secret material is forbidden from the request. The execution service resolves a secret reference through the credential broker only inside the provider execution callback.

A provider binding may scope one provider instance to a workspace, Project and/or canonical Resources. Resource-scoped bindings require explicit targets, and requests cannot escape the configured resource set. A configured binding credential cannot be silently overridden per request.

## Registry and configuration

`ActionProviderRegistry` owns provider implementation registration and persisted `ActionProviderBinding` configuration. Implementations live in code; mutable provider bindings live in versioned SQLite state.

Resolution fails closed when:

- a provider implementation is not registered;
- a binding is missing, disabled or belongs to another tenant;
- Project or Resource scope differs;
- required Resource types are absent;
- required credential references are absent;
- requested capabilities are unsupported.

Provider/action risk and authority requirements are visible during `prepare`. Central authority enforcement is layered by the authority/policy milestone rather than duplicated inside provider implementations.

## Execution lifecycle

`ActionExecutionService` is the single provider-neutral side-effect boundary:

1. resolve binding/provider/action;
2. validate tenant/Project/Resource scope;
3. validate declared capabilities and credential references;
4. prepare a provider-neutral plan;
5. execute through the provider, resolving secret material only at the callback boundary;
6. verify when the capability exists;
7. rollback only when declared reversible and rollback-capable.

Execution and verification resolve a binding's credential reference independently
through `SecretBroker`. The raw credential exists only inside the bounded provider
callback and cannot be copied into an `ActionRequest`, `ActionResult`, receipt,
verification, Evidence record, log, or worker workspace. This lets reconciliation
perform a fresh provider read without weakening the credential boundary.

Autonomy exposes these same prepare/execute/verify/rollback methods through the service and does not call provider-native side-effect APIs directly.

## Evidence and results

Providers return `ActionResult`, `ActionVerification`, and `ActionEvidence` models. Provider-native response bodies must be normalized into these structures before they reach core logic.

Evidence is reference/summary/metadata oriented and must not contain credentials.

## Authoritative TaskSource creation adapter

The code-owned `task-source/authoritative` provider adapts the singular project
TaskSource configuration into the ActionIntent side-effect lifecycle. It exposes
only `task-source.create` and accepts the provider-neutral
`title`/`body`/`owners`/`labels` creation facts.

Preparation calls the same deterministic authoritative CREATE resolution used by
`WorkItemService`, so a missing project source, incompatible adapter, or missing
CREATE capability fails before an external mutation. Execution then delegates to
`WorkItemService.create_authoritative(...)` and returns the actual projected
Work Item ref plus the TaskSource identity in the ActionResult.

The action deliberately declares no ActionProvider idempotency, rollback, or
provider verification and limits execution to one attempt. TaskSource CREATE does
not currently guarantee a common provider-level idempotency primitive, so an
unknown post-send outcome must enter the existing ActionIntent reconciliation
path rather than risk duplicate task creation.

The adapter is registered in code, while tenant/project enablement remains an
ordinary persisted ActionProvider binding. A Goal decomposition commit must find
an enabled binding for `task-source/authoritative` whose project scope permits
the proposed item. If no such binding exists, commit is blocked and the operator
must configure it through the existing ActionProvider administration surface.
Bindings for this action must also allow network egress because an authoritative
TaskSource may be remote; provider credentials remain behind the TaskSource
integration boundary and are never accepted as action parameters.

## Reference provider and conformance

The in-memory `ReferenceActionProvider` implements a reversible `reference.set` action with dry-run, idempotency, verification, rollback, and evidence. `ActionProviderConformanceSuite` validates contract version, unique action IDs and capability semantics, then exercises a provider through the shared lifecycle.

New real providers must pass the same suite before they are wired into autonomy.

## Governed code-host delivery actions

GitHub and GitLab implement one provider-neutral delivery catalog:

- `code-host.issue.create` (GitHub);
- `code-host.issue.comment`;
- `code-host.issue.update`;
- `code-host.pull-request.upsert`, normalized as a provider-neutral change
  request and materialized as a GitHub pull request or
  GitLab merge request;
- `code-host.branch.publish`.
- `code-host.pull-request.merge` (GitHub).
- `code-host.job.rerun` (GitHub).

Every request targets exactly one active canonical repository Resource. The
provider locator is resolved from that Resource's GitHub or GitLab alias, while
the ActionProvider binding independently constrains tenant, Project, Resource,
provider instance, network policy, and credential reference. A provider-side
repository name or permission never grants canonical authority.

Comments and change requests carry a deterministic digest marker derived from
the ActionIntent idempotency key. Reconciliation finds only objects bearing that
marker and refuses to adopt an unowned pull/merge request for the same branches.
Issue creation uses the same durable ownership marker and reconciles before
creating; issue state updates converge on the requested state. Pull-request
merge is a separate high-risk, single-attempt action that fails closed unless
GitHub reports the request mergeable with a `clean` state and then verifies the
merged result and merge commit. Branch publication pushes the exact committed
revision from an active, write-leased, clean canonical execution workspace;
branch name, head, ancestry, Resource membership, and workspace-root containment
are re-attested immediately before each attempt. The normal target is the
workspace's canonical branch. A recovery may instead fast-forward an existing
`codex/` change-request branch when the request supplies its exact expected
remote revision, that revision is an ancestor of the attested workspace head,
and Git enforces the same expected revision with `--force-with-lease`. This lets
an isolated replacement execution repair an existing pull request without
granting arbitrary branch replacement.

For GitLab, `existing_change_request_number` permits a legacy source branch only
when the canonical workspace and active lease agree on a repository-qualified
issue reference. The provider independently verifies an opened same-project MR,
its exact source branch and expected head, its GitLab closing-issue relation, and
an unprotected non-default remote branch at that same revision. Local workspace
attestation is repeated after provider reads, and Git still enforces the exact
remote SHA with compare-and-swap. A thread-bootstrap workspace without an issue
scope cannot use this recovery; acquire a separate canonical issue workspace.
Missing, unrelated, forked, protected, stale or changed proofs fail closed. The
existing ActionIntent preparation and receipts expose the MR and issue reference;
no separate mutation path or authority is introduced.

Provider success returns normalized `code-host-*` Evidence. Verification uses a
fresh brokered credential to read the comment, issue, change request, or branch
back from the provider and compare it with the durable ActionResult. ActionIntent
receipts and verification receipts remain the authoritative reconciliation
history, including unknown outcomes and bounded idempotent retries.

CI job reruns are medium-risk external mutations. The assignment broker creates
an ActionIntent for one exact GitHub job ID, the provider accepts only a
completed failure-like conclusion, and the action is single-attempt so an
unknown outcome cannot silently start a second run. Verification reads the job
back and accepts only queued, running, or completed provider state.

The same broker exposes read-only CI diagnostics without giving the execution
worker a provider credential. Failed-job logs are UTF-8 decoded, capped at a
caller-selected maximum of 320 KiB, and redact common authorization, token,
secret, password, API-key, and GitHub-token forms. Workflow-run artifact lists
are capped to one provider page, and artifact downloads return bounded base64
content with an explicit truncation flag. All operations retain canonical
tenant, Project, repository, assignment-fence, authority, rate-limit, and audit
checks.

The shared ActionProvider administration surface exposes both provider catalogs
and binding state, so this slice requires no provider-specific UI. Operators see
canonical binding/resource scope and ActionIntent receipts through the existing
provider and action-intent views; secret values are never rendered.

GitHub derives `owner/repository` from its validated Resource alias. GitLab
accepts validated nested `group/project` aliases and requires its configured API
and Web bases to be credential-free HTTPS URLs on the same authority. Each
transport performs Git publication with a temporary owner-only askpass helper in
the trusted provider process. The token is environment-scoped to that subprocess
and is absent from the remote URL, command line, worker, ActionRequest, receipt,
Evidence, and logs.

GitLab action delivery uses `CODEX_WEB_GITLAB_ACTION_API_BASE` (and optionally
`CODEX_WEB_GITLAB_WEB_BASE`) rather than the read-side
`CODEX_WEB_GITLAB_API_BASE`. This keeps credentials for governed mutations on an
HTTPS endpoint even when discovery and webhook development use a trusted local
HTTP proxy.

## API and UI

The administration API exposes provider/binding catalogs and prepare previews:

- `GET /api/action-providers`
- `GET/POST /api/action-providers/bindings`
- `GET /api/action-providers/bindings/{binding_id}`
- `PATCH /api/action-providers/bindings/{binding_id}` for the canonical
  enabled state, with the existing tenant-scoped admin/MFA or scoped-service
  authorization checks
- `POST /api/action-providers/bindings/{binding_id}/prepare`

The operator binding surface projects the canonical enabled state and requires
an explicit target-and-impact review before changing it. After a PATCH it
reloads canonical state rather than treating a local UI update as authoritative.
Disabling a binding prevents new resolution through it; it does not rewrite or
cancel existing durable ActionIntents or provider outcomes.

External execution is admitted only through canonical ActionIntent authority,
policy, security, worker-lease, receipt, and reconciliation handling. The direct
service method remains an internal provider boundary used by ActionIntent workers,
not an operator mutation API. #141 should project provider capabilities/bindings
and requirements from these canonical APIs rather than hard-code provider features
in the UI.
