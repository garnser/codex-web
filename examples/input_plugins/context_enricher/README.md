# Context-enrichment input plugin example

This example adds one deterministic context block to a model invocation.

It demonstrates the most important input-plugin boundary: plugins may propose
changes to **composable** fields such as `context_blocks`, but they cannot
rewrite protected identity, tenant, authority, policy, sandbox, or secret
fields.

The context text comes from the plugin registration's bounded settings rather
than from a secret or arbitrary control-plane lookup.

Run the demo:

```bash
python examples/input_plugins/context_enricher/demo.py
```

In production, register the plugin implementation in the code-owned input plugin
catalog and publish a scoped input-pipeline definition selecting its phase,
order, failure policy, and budgets. Do not let plugin code self-register or
self-grant access.
