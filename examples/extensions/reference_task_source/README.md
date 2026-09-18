# Reference TaskSource extension

This is the minimal distributable example for the canonical codex-web extension
package contract tracked by issue #169.

From the repository root, run:

```bash
python examples/extensions/reference_task_source/build.py
```

The builder writes `dist/manifest.json` and `dist/payload.cwext`. The payload
digest is computed from the source bytes and injected into the validated
manifest; do not hand-edit the generated digest.

The sample implements the provider-neutral `TaskSource` shape as a deliberately
read-only adapter. Installation, configuration, authorization and enablement
remain separate canonical extension lifecycle operations. Packaging does not
grant capabilities or execute the payload.

The local package catalog currently treats `payload.cwext` as opaque bytes.
Loading/executing untrusted extension code must use the separately tracked
isolated execution/signing trust boundaries; this example intentionally does
not bypass them.
