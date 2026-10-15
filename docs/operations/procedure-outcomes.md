# Keep operational procedures current

After an operational change, inspect its assignment's procedure outcome using
`GET /api/execution-workers/assignments/{id}/procedure/outcome`. Review the exact
Skill revision/checksum, Resource/version context, step Evidence, input/output
Artifact pins, deviations, applicability and expiry dates. A failed run or
recovered rollback is a review need, not proof that the original fix worked.

Request an inactive draft with `POST .../procedure/outcome-draft` and `{}`.
Optionally supply `incident_id` to link the resolved Incident and its postmortem,
or `skill_id` to propose a new procedure instead of a revision. Incident
postmortem creation can supply `procedure_assignment_ids` for the same conversion.
Duplicates reuse the canonical draft. Local edits/newer publication cause a
visible conflict; inspect revision history and deliberately reconcile or fork.
Do not retry a changed source as though it were unchanged.

The returned diff describes verified observations separately from hypotheses.
For example, a successful GitLab restart is observed restoration; it does not
establish the cause of the underlying library crash. Follow source links to
inspect diagnostic signatures and deviation reasons with the appropriate
authority. The generated report excludes raw logs and conversation history.

Edit the proposed steps, applicability/version bounds and review dates through
the existing Skill API only after checking their supporting Evidence. Untested
recovery and new environments need separate verification. Run existing Skill
validation/security scanning and review the exact resulting checksum. Publish
that revision explicitly using the normal reviewed Skill/Definition lifecycle.
Then separately assign/enable it for future use. The completed execution keeps
its old immutable procedure pin throughout.

Inspect Memory classification/retention and source freshness before reuse.
Deleted/redacted sources erase derived content; stale proof or changed Resource
context blocks publication/reuse and requires review. A governance tombstone is
quarantined and cannot be revived by editing its descendants. Resolve the source
or create a new independently evidenced procedure with explicit review.

Automatic conversion does not elevate authentication or authority. If it reports
a review/authority need in Attention, use an authorized operator session for
explicit conversion. Rich authoring, diff and source-link UI is tracked by
[#1428](https://github.com/garnser/codex-web/issues/1428); the APIs and existing
canonical inspectors provide the current operator path.
