# Canonical Execution Contract Schema

## Status

**Milestone 2 architecture contract.** This document defines the machine-readable execution contract that codex-web derives from canonical work-item state before agent work is dispatched.

The contract complements the role rules in `codex_web/execution_contracts.py`; it does not create a second ownership, work-item, permission, accounting, or authority system.

## Design constraints

The contract follows the repository architecture policy:

- deterministic state is read from the existing `WorkItemState` model;
- the execution role is resolved from the existing canonical role policy;
- contract construction and validation require no LLM call;
- authoritative TaskSource/work-item state remains the operational source of truth;
- sandbox and approval controls are inherited and cannot be weakened by the contract;
- retry/deadline/failure/checkpoint state comes from `WorkItemState.execution` rather than prompt prose;
- every model execution has a stable work-item accounting hook;
- future authority controls must extend the versioned contract rather than bypass it;
- the prompt path receives only the schema version in addition to the existing role contract, avoiding duplicated structured payload tokens.

## Version 1.3

Schema `1.3` is a backward-compatible evolution of the v1 contract family. It adds structured execution-resume and accounting data while retaining the same ownership, target, permissions, output, success, and failure semantics introduced in 1.0.

`ExecutionContractV1` contains:

| Field | Purpose |
| --- | --- |
| `schema_version` | Exact schema version, currently `1.3`. |
| `work_item_ref` | Canonical work-item identity. |
| `role_id` | Resolved execution-role contract. |
| `agent_id` | Current canonical owner, or pending handoff recipient when applicable. |
| `target` | Known repository, branch, and environment target. Unknown values remain unset rather than guessed. |
| `permissions` | Existing execution controls. V1 explicitly inherits thread/project sandbox and approval policy and cannot weaken them. |
| `inputs` | Minimum canonical state required for execution, including stage/handoff plus retry, deadline, failure classification, and latest checkpoint resume hints. |
| `accounting` | Stable work-item/checkpoint/goal/decision attribution hooks; usage recording is required whenever model execution occurs. |
| `expected_outputs` | Artifacts required by the resolved role. |
| `success_criteria` | Conditions required for a successful execution outcome. |
| `failure_conditions` | Failure conditions already defined by the resolved role contract. |

The Pydantic models use `extra="forbid"` so unknown fields cannot silently change the execution contract. Schema-version mismatches fail validation rather than falling back to a different interpretation.

### Resume inputs

The v1.3 input envelope adds:

```yaml
retry_attempt: 0
retry_max_attempts: 3
timeout_seconds: null
deadline_at: null
failure_category: null
failure_code: null
checkpoint_id: null
checkpoint_summary: null
```

Only the latest checkpoint id and summary are copied into the contract. Full checkpoint history stays in canonical work-item state and the event audit log, keeping dispatch context bounded.

### Accounting envelope

Every v1.3 contract includes:

```yaml
accounting:
  work_item_ref: group/project#42
  checkpoint_id: null
  goal_id: null
  decision_id: null
  usage_recording_required: true
```

`goal_id` and `decision_id` are optional forward-compatible attribution hooks. Their presence does not create strategy or decision entities; later milestones may populate them from their canonical stores.

## Permission boundary

Version 1 does **not** implement the later roadmap authority model. Its permission envelope remains intentionally limited to:

```yaml
permissions:
  source: thread-project-policy
  sandbox: inherit
  approval_policy: inherit
  can_weaken_controls: false
```

Milestone 5 may introduce explicit role/action authority and project overrides. That work should evolve this schema deliberately and preserve compatibility rather than making v1 imply powers that are not currently enforced.

## Dispatch behavior

Before a canonical work-item wake-up is formatted for an agent, `WorkItemContractService`:

1. resolves split-brain findings deterministically;
2. resolves the execution role from canonical state;
3. constructs and validates `ExecutionContractV1` schema 1.3;
4. carries only the compact latest-checkpoint resume hint into the structured contract;
5. continues through the existing role-prompt path.

The service also exposes the structured contract through the historical host composition seam as `_work_item_execution_contract`, allowing APIs, audit storage, worker isolation, and policy checks to consume the same validated object without reparsing prompt prose.

## Evolution rules

A future schema change must:

1. use an explicit new version when semantics or required fields change;
2. retain deterministic construction and validation;
3. avoid duplicating work-item, accounting, or authority state;
4. include migration or compatibility handling for persisted contracts if persistence is introduced;
5. keep resume context bounded rather than replaying full history;
6. add focused validation tests before becoming a dispatch dependency.


## Execution workspace target

Schema 1.3 adds the optional `target.workspace` projection. It carries canonical execution-workspace and lease identity, resource IDs, isolated path/branch, pinned base/head revision, lifecycle status, and lease expiry. The field is derived from canonical Work Item execution state; prompts do not invent workspace ownership.
