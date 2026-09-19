# Reference ActionProvider example

This example models a small external feature-flag provider entirely in memory.
It demonstrates the shape expected of consequential external actions without
performing any real network mutation.

The provider supports:

- deterministic preparation;
- dry-run;
- idempotent execution;
- structured evidence;
- post-action verification;
- rollback.

The declared authority requirement is part of the action definition. In a real
Codex Web execution the provider is invoked behind the canonical ActionIntent,
authority, resource, credential, and verification boundaries; an agent should
not call `execute()` directly.

Run the synthetic contract demo:

```bash
python examples/action_providers/reference_action_provider/demo.py
```

A real provider that needs credentials should set `credential_required=True`,
declare a credential purpose, keep only a SecretReference on the binding, and
use the resolved credential only inside the provider boundary.
