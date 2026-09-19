# Documentation and release readiness

Documentation is part of the release gate for user-visible, operational,
security, compatibility and configuration changes.

## Required review triggers

A PR/release must review documentation when it changes:

- API/event/persisted contract;
- Definition kind/schema/lifecycle;
- configuration/environment/feature rollout;
- identity/session/authority/approval behavior;
- secret/key/encryption behavior;
- provider/model/runtime/worker/extension contract;
- release/incident/capacity/upgrade/recovery operation;
- operator UI/workflow;
- compatibility/deprecation behavior.

## Automated checks

Release readiness should include:

- relative documentation link validation;
- required critical-document presence;
- screenshot manifest/fixture sanitization;
- regenerated documentation screenshot artifact;
- browser landmarks for captured workflows;
- critical runbook/topic assertions;
- upgrade/restore/incident/release examples remaining linked/reachable.

## Critical procedural verification

At least these procedures must remain reproducible against supported releases:

1. Definition draft → validate → publish → supersede/rollback;
2. safe upgrade preflight/drain/migration/verification;
3. encrypted backup + isolated restore verification;
4. release qualification/promotion/rollback;
5. Incident containment/recovery/restoration Evidence;
6. worker drain/quarantine/replacement;
7. extension upgrade/quarantine/uninstall;
8. key rotation dependency/recovery check.

When a procedure changes, update its docs and tests in the same delivery cycle.

## Screenshots

Use the fixture-based workflow in
[Reproducible documentation screenshots](../screenshots/README.md). Materially
stale screenshots are defects.

## Documentation gaps

Do not add prose `TODO` markers as a hidden backlog. Create/update a GitHub issue
with:

- affected workflow/surface;
- release/milestone;
- risk/impact;
- expected documentation or screenshot;
- owning feature/UI issue when applicable.

## Deprecation and redirects

When replacing documentation:

1. update inbound links;
2. leave a short deprecation pointer when users may have bookmarked the old
   path;
3. state the replacement and compatibility window;
4. remove the obsolete page only after supported-release references no longer
   require it.

Do not keep two contradictory “current” runbooks.
