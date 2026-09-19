# Reference TaskSource example

This example implements a small read-only task source that behaves like an
external ticket system.

It demonstrates:

- explicit TaskSource capability declaration;
- provider-native identity through `TaskSourceIdentity`;
- normalized `TaskSourceSnapshot` records;
- deterministic event normalization;
- deterministic projection into canonical Work Item stage/owner facts;
- fail-closed behavior for unsupported writes.

The adapter deliberately does **not** implement provider mutation capabilities.
Its write methods raise `UnsupportedTaskSourceCapability`, matching the
declared capabilities.

Run the demo from the repository root:

```bash
python examples/task_sources/reference_task_source/demo.py
```

A production adapter would replace the in-memory rows with a provider SDK/client
and resolve credentials through the Secret boundary. Provider assignees remain
provider provenance; they do not grant canonical Codex Web authority.
