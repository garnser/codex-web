# Reference BusinessDataSource extension example

This example packages a synthetic CRM-like BusinessDataSource integration using
the same provider-neutral contract as production connectors.

It is intentionally read/sync only. There is no provider-write method.

## Files

- `adapter.py` — minimal deterministic adapter over synthetic snapshots.
- `manifest.template.json` — extension metadata/capability declaration.
- `build.py` — creates a reproducible zip package for local extension testing.

## Safety properties

- normalized scalar fields only;
- credential reference is configuration metadata, never fixture data;
- declared capabilities match implemented read/sync behavior;
- no direct external mutation surface;
- suitable for conformance tests with duplicate/out-of-order events, tombstones
  and bounded paging.

For a production connector, replace the in-memory fixture access with a provider
SDK/client behind SecretReference resolution. Keep the returned
BusinessDataSnapshot contract unchanged.
