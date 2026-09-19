# Guide title

> Applies to: codex-web version/release **[state the supported version or "current main"]**  
> Audience: **[user / administrator / operator / developer]**  
> Risk: **[read-only / local mutation / privileged external action]**

## Goal

State the observable outcome in one paragraph.

## Prerequisites

List required product state, permissions, credentials and external dependencies. Never hide a prerequisite in the steps.

## Security and authority

State which identity/role performs the operation, which canonical policy/approval boundary applies, and whether credentials are used by reference.

## Steps

1. Use one deterministic action per step.
2. Show commands/API calls only when they are supported interfaces.
3. Call out external-provider mutations explicitly.

## Expected result

Describe the canonical state change and, separately, any expected external-provider state.

## Verify

Provide a UI/API/health/evidence check that proves the result.

## Failure modes

| Symptom | Likely cause | Next check |
| --- | --- | --- |
| Example | Example | Example |

## Recovery / rollback

Explain how to return to a known-safe state. If rollback is not guaranteed, say so.

## Related concepts

Link to Core Concepts and the relevant architecture/reference documents.

## Documentation review

Record or update version assumptions when behavior changes. Do not embed secrets, access tokens or private key material in examples.
