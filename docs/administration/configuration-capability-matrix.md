# Configuration capability matrix

This document is the operator-facing capability inventory for configuration and adjacent canonical state. It complements the architecture contracts by declaring what can be viewed or changed, where those changes belong, and which values are intentionally immutable, inherited, computed, or externally managed.

Legend: **Y** = supported canonical product flow, **R/O** = intentionally read-only in product UI, **N/A** = operation does not apply to the resource model, **API** = canonical backend operation exists but still needs first-class product workflow, **External** = owned by deployment/provider infrastructure rather than codex-web.

| Resource / field family | View | Create | Update | Version / publish | Attach / detach | Archive / restore | Delete / retire | Explain / usage | Canonical surface / disposition |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Organization / workspace identity | Y | Y | Y | N/A | Y | N/A | Y | Y | Administration → Users / Access. Tenant scope is authoritative and never inferred from repository state. |
| Human users, groups, memberships and scoped roles | Y | Y | Y | N/A | Y | N/A | Y | Y | Administration → Users / Access. Authorization and assurance remain server-enforced. |
| Authentication policy, sessions, MFA / SSO boundaries | Y | API | API | N/A | N/A | N/A | Y | Y | Administration → Authentication. Provider-owned IdP configuration remains external and must be identified as such. |
| Service identities / service tokens | Y | Y | Y | N/A | Y | N/A | Y / rotate | Y | Administration → Authentication. Secret token material is shown only at issuance where policy permits; persisted views expose metadata only. |
| Projects and Project topology | Y | Y | Y | N/A | Y | N/A | Y | Y | Project Settings. Repository/resource selection is explicit canonical Project state. |
| Repository selection / multi-repository execution policy | Y | Y | Y | N/A | Y | N/A | Y | Y | Project Settings / execution composer. Exactly one writable target per mutating execution; optional read-only siblings. |
| Project readiness / bootstrap / task-source authority | Y | Y | Y | N/A | Y | N/A | Y | Y | Project Settings owns readiness, preflight/plan/apply and guided bootstrap remediation; Work Items owns authoritative task-source create/update/clear. Readiness remediation links route to the canonical owning surface, and execution remains gated by server-reported blockers. |
| Agent Profiles: identity, role/instruction refs, runtime/model policy, execution profile, sandbox/network requirements, budgets | Y | Y | Y | Y via immutable revision | Y | Y | Y via archive retirement | Y | Agents. Create/edit writes only declared mutable fields and creates canonical revisions; Skills remain managed through their contextual attachment surface. Disable/archive/restore require a reason and impact confirmation. Hard delete is intentionally unsupported because revision/audit provenance is durable; archive is the retirement operation. |
| Agent Profile skill refs | Y | N/A | N/A | N/A | Y | N/A | N/A | Y | Agent Profiles → manage Skills / Skills workspace. Exact published Skill revisions are pinned. |
| Teams: membership, delegation policy, budgets, lifecycle | Y | Y | Y | Y via immutable revision | Y | Y | Y via archive retirement | Y | Teams / Squads. Membership, delegation policy and budgets are edited through revision creation; lifecycle changes require reason plus impact confirmation. Hard delete is intentionally unsupported so historical delegation/audit provenance remains valid; archive is the retirement operation. |
| Skills: draft/update/publish, exact revision use | Y | Y | Y | Y | Y | Y | retire | Y | Skills. Consumers, provenance and lifecycle are visible; published revision identity is immutable. |
| Typed configuration specs | Y | R/O | R/O | R/O | N/A | N/A | N/A | Y | Configuration. Specs are code-owned schema/invariants and are not editable data. |
| Typed configuration values / feature rollout records | Y | Y | Y via new revision | Y | N/A | rollback / disable | retire via superseding state | Y | Configuration. Effective value, source scope, precedence, revision and actor/time/reason must be shown. |
| Execution profiles | Y | Y via Definition Registry | Y via new revision | Y | Y | Y via quarantine / rollback | Y via new catalog revision | Y | Definitions / Contracts is the canonical lifecycle surface; execution selectors link there contextually. Profiles are members of the versioned `execution-profile-catalog`, so changes create validated revisions rather than mutating runtime state in place. |
| Providers / runtimes / model routing | Y | Y | Y | API | Y | API | Y | Y | Providers / Runtime. Provider-owned remote state is separate from codex-web binding state. |
| Workers and execution workspaces | Y | Y / enroll | Y | N/A | Y | drain / quarantine | retire | Y | Operations / Runtime. Live lease/fencing/runtime state is canonical operational state and not ordinary configuration. |
| Integrations / extensions / input and action providers | Y | Y | Y | versioned where supported | Y | disable / restore | Y | Y | Integrations / Developer / Administration. Installation, enablement and permission grant are distinct operations. |
| Automations | Y | Y | Y | N/A | Y | disable / re-enable | Y | Y | Automations. Trigger/cadence, target scope and execution authority remain independently validated. |
| Entitlements / quotas / usage | Y | API | API | N/A | N/A | N/A | API | Y | Configuration / Entitlements. Effective capability and usage are visible; billing/hosted authority may be externally managed. |
| Secret references / credentials | Y metadata only | Y | rotate / metadata | N/A | Y | revoke / restore where policy allows | revoke | Y metadata only | Credentials / Secrets. Raw secret material is never rendered back or stored in ordinary configuration. |
| Encryption key references / lifecycle metadata | Y metadata only | External / backend-specific | rotate metadata | N/A | Y | backend-specific | revoke with dependency checks | Y metadata only | Credentials / Keys. Private key material is never exposed in product UI. |
| Recovery / operational policy | Y | API | API | versioned where contract requires | Y | API | API | Y | Operations / Recovery. Operational state and evidence are distinct from editable policy/configuration. |
| Computed runtime state, leases, health, traces, evidence | Y | N/A | N/A | N/A | N/A | N/A | retention policy only | Y | Operations. Intentionally R/O because these values are derived canonical facts, not user configuration. |
| Deployment environment variables / process flags | Y where safe | External | External | N/A | N/A | N/A | External | Y with change guidance | Configuration should identify source and restart requirements; product UI must not pretend these values are directly mutable. |

## Field-level UI rules

Every configuration detail view should declare the disposition of each field rather than merely disabling controls:

- **Mutable value**: show configured value, effective value, scope, validation and save/reset actions.
- **Inherited value**: show effective value, source scope and an explicit “override” action.
- **Explicit override**: show the local value, inherited fallback and an explicit “revert to inherited” action.
- **Immutable revision field**: label it immutable and point to the canonical create-new-revision workflow.
- **Computed field**: label it computed and identify the producing subsystem.
- **Externally managed field**: name the external source (deployment environment, IdP, provider, secret backend, etc.) and explain how change is performed.
- **Sensitive reference**: render reference identity/lifecycle metadata only; never render the referenced secret or private key.

## Required provenance and impact metadata

Where applicable, product surfaces should show:

- effective value and configured value;
- source scope and inheritance/override chain;
- revision/checksum;
- actor, timestamp and change reason;
- startup-only vs hot-reload behavior;
- current consumers / usage;
- destructive-change or blast-radius preview.

## Parity contract for future fields

A newly introduced mutable backend configuration field is not product-complete until it has one of these declared dispositions:

1. first-class product management flow;
2. intentionally read-only/computed with an explanation;
3. externally managed with an actionable source/change path;
4. deliberately API-only, with a documented rationale and follow-up owner.

Tests should fail when a new user-manageable capability is added without a declared UI disposition. The matrix is the human-readable companion to that parity check.


## Declared API-only exceptions

An `API` disposition is allowed only where a first-class product mutation flow is deliberately deferred. These exceptions are not permission bypasses: the canonical API remains authoritative for validation, authorization, tenant/workspace scope, concurrency and audit.

| Resource / operation | Why product mutation is deferred | Owning subsystem / follow-up surface |
| --- | --- | --- |
| Authentication policy and session-policy create/update | Identity-provider topology, assurance policy and session invalidation can affect the whole tenant and may be deployment/IdP-owned. The product currently presents effective policy and diagnostics rather than implying every upstream IdP control is locally editable. | Identity / Authentication administration. Promote individual operations to UI only when their provider ownership, impact preview and assurance requirements are explicit. |
| Provider/runtime versioned lifecycle and archive/restore | Runtime/provider registrations mix codex-web binding state with provider-owned remote state. Generic version/archive controls would imply authority over external provider state that codex-web may not possess. | Providers / Runtime administration. Add lifecycle controls capability-by-capability when the adapter contract exposes a safe canonical operation. |
| Entitlement/quota create, update and retirement | Hosted billing/entitlement authority can live outside the application. Local UI mutation would be misleading where codex-web is only a consumer of effective capability and usage. | Entitlements service / hosted control plane. UI remains explanatory unless the deployment exposes codex-web as the entitlement authority. |
| Recovery/operational policy create, update, archive and retirement | Recovery settings can change restart, fencing and disaster-recovery behavior and require deployment-specific qualification. Live recovery state is also distinct from editable policy. | Operations / Recovery. Promote only typed, versioned policy with impact/rollback semantics; runtime evidence and health stay read-only. |

Any new `API` cell added to the matrix must be accompanied by an entry here (or an equivalent linked rationale) that names both the reason and the owning follow-up surface.
