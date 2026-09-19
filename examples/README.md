# Codex Web examples

These examples are intentionally small reference implementations of public/shared
Codex Web contracts. They are designed to be copied, modified, and exercised
without real provider credentials or external side effects.

## Available examples

| Example | What it demonstrates |
| --- | --- |
| `extensions/reference_business_data_source/` | Read/sync BusinessDataSource normalization, paging, events, and tombstones. |
| `task_sources/reference_task_source/` | Provider-neutral task discovery, reads, event normalization, and deterministic projection into canonical Work Item facts. |
| `action_providers/reference_action_provider/` | Governed external-action shape with prepare, dry-run, idempotency, evidence, verification, and rollback. |
| `input_plugins/context_enricher/` | Bounded prompt/input enrichment using composable fields without touching protected identity, authority, policy, or secret fields. |

## Choosing a boundary

Use a **TaskSource** when another system owns task/work state and Codex Web should
discover or reconcile it.

Use a **BusinessDataSource** when another business system supplies factual
context for company reasoning, KPIs, or decisions but does not own Codex Web
work.

Use an **ActionProvider** for consequential external mutations. Real mutations
should flow through canonical ActionIntent, authority, resource, credential,
receipt, and verification boundaries instead of being called directly from an
agent or UI.

Use an **input plugin** to normalize, enrich, compose, or optimize model input.
Plugins propose bounded patches; they cannot rewrite protected identity,
authority, policy, tenant, sandbox, or secret-reference fields.

## Running the demos

From the repository root:

```bash
python examples/task_sources/reference_task_source/demo.py
python examples/action_providers/reference_action_provider/demo.py
python examples/input_plugins/context_enricher/demo.py
```

The demos are synthetic and deterministic. They do not need network access or
credentials.

For production integrations, keep provider credentials behind SecretReference
resolution, declare only capabilities the implementation actually supports, and
use the canonical lifecycle/authority boundaries described in the architecture
documentation.
