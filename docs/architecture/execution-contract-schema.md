# Canonical Execution Contract Schema

## Status

**Milestone 2 architecture contract.** This document defines the machine-readable execution contract that codex-web derives from canonical work-item state before agent work is dispatched.

The contract complements the role rules in `codex_web/execution_contracts.py`; it does not create a second ownership, work-item, permission, or authority system.

## Design constraints

The contract follows the repository architecture policy:

- deterministic state is read from the existing `WorkItemState` model;
- the execution role is resolved from the existing canonical role policy;
- contract construction and validation require no LLM call;
- GitLab/work-item state remains the operational source of truth;
- sandbox and approval controls are inherited and cannot be weakened by the contract;
- future authority controls must extend the versioned contract rather than bypass it;
- the prompt path receives only the schema version in addition to the existing role contract, avoiding duplicated structured payload tokens.

## Version 1.0

`ExecutionContractV1` contains:

| Field | Purpose |
| --- | --- |
| `schema_version` | Exact schema version, currently `1.0`. |
| `work_item_ref` | Canonical work-item identity. |
| `role_id` | Resolved execution-role contract. |
| `agent_id` | Current canonical owner, or pending handoff recipient when applicable. |
| `target` | Known repository, branch, and environment target. Unknown values remain unset rather than guessed. |
| `permissions` | Existing execution controls. V1 explicitly inherits thread/project sandbox and approval policy and cannot weaken them. |
| `inputs` | Minimum canonical state required for execution: stage, artifact state, next action, blocker, handoff, and split-brain findings. |
| `expected_outputs` | Artifacts required by the resolved role. |
| `success_criteria` | Conditions required for a successful execution outcome. |
| `failure_conditions` | Failure conditions already defined by the resolved role contract. |

The Pydantic models use `extra="forbid"` so unknown fields cannot silently change the execution contract. Schema-version mismatches fail validation rather than falling back to a different interpretation.

## Permission boundary

Version 1 does **not** implement the later roadmap authority model. Its permission envelope is intentionally limited to:

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
3. constructs and validates `ExecutionContractV1`;
4. continues through the existing role-prompt path.

The service also exposes the structured contract through the historical host composition seam as `_work_item_execution_contract`, allowing future APIs, audit storage, and policy checks to consume the same validated object without reparsing prompt prose.

## Evolution rules

A future schema change must:

1. use an explicit new version when semantics or required fields change;
2. retain deterministic construction and validation;
3. avoid duplicating work-item or authority state;
4. include migration or compatibility handling for persisted contracts if persistence is introduced;
5. add focused validation tests before becoming a dispatch dependency.
