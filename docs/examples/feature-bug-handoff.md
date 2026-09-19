# Worked example: feature, bug, blocker and multi-agent handoff

## Scenario

Fictional organization **Northstar Labs** has project `catalog-api`. A feature asks for a new health field and a bug reports a regression in its serializer.

## Feature success path

1. The external issue is projected into canonical Work Item `catalog-api#41`.
2. Implementation ownership is assigned to an execution agent.
3. The worker receives only the bounded repository workspace and required execution contract.
4. The agent changes the serializer and tests.
5. Test output is captured as Evidence.
6. The Work Item is handed to validation.
7. Validation verifies the artifact/Evidence and transitions the item toward completion.

**Healthy success signal:** canonical Work Item history shows implementation → handoff → validation, while repository/CI remain authoritative for code/test facts.

## Bug-fix path

A second Work Item `catalog-api#42` reproduces the regression first, then fixes it.

The key rule is that “I fixed it” is not completion. A failing test before the change and passing test afterward provide stronger Evidence.

## Intentionally blocked path

The implementation agent discovers the required fixture is owned by another service and is unavailable.

Healthy behavior:

- mark the Work Item blocked;
- record the blocker;
- identify the next owner/action;
- create an Attention item if human intervention is required;
- do not keep spending tokens retrying the same unavailable dependency.

Unhealthy behavior would be silently fabricating the fixture or declaring success.

## Multi-agent handoff

After implementation:

```text
implementer
   -> canonical handoff
      -> validator
         -> Evidence review
            -> completion or blocker
```

The receiving agent/person should acknowledge the handoff. Ownership is canonical state, not inferred from who last wrote in a thread.

## Recovery example

If validation reveals a regression, transition back through the supported lifecycle with the finding attached. Do not erase the failed validation history; it is useful provenance.
