# Home operator conversation reference

Home can retain one administrator-selected conversation using `Project.home_operator`.
The reference contains its native thread ID, selecting identity, revision and timestamp.
It is a navigation binding on the existing Project, not a new session, execution role,
agent identity, goal owner or permission grant. Ordinary Projects have no such entry point.
Absent bindings are omitted from existing Project serialization for compatibility.

`GET /api/home/operator` reads the current tenant's Home Project and revalidates the
thread through canonical ThreadScope. An unavailable, foreign, archived or replaced
thread is displayed as unavailable. Its reference remains unchanged; lookup never
creates a replacement, resumes a runner, starts a turn or retries an external action.
Current execution settings are read from the normal thread settings service. Missing
profile selection is shown explicitly, and stored settings are not a claim that an
assignment is currently admitted or that its profile remains executable.

`PUT /api/home/operator` requires the normal canonical administrator authority. The
selected conversation must already be named, unarchived, owned by Home in the actor's
tenant and have an explicitly selected execution profile. The request carries an
`expected_revision`; changing the pointer is an atomic update of the existing Project
record. A stale revision refuses with 409. Clearing the shortcut retains a revisioned
tombstone so an old initial-selection request cannot overwrite a newer choice.
Concurrent fields and other Projects remain unchanged. This operation never modifies
a profile, worker, lease, goal, queue, credential or thread settings.

The Home card opens the existing conversation using its normal deep link and displays
its current profile, sandbox and approval policy. Failed or uncertain selection saves
are never automatically retried. Administrators select a different conversation only
after refreshing the canonical revision. Normal conversation naming, creation, profile
selection, runner inspection and goal controls remain in their existing surfaces.

This contract is the durable entry-point slice of [#1669](https://github.com/garnser/codex-web/issues/1669).
It does not establish host administration capability. The current execution profile
schema explicitly forbids host mutation and network enablement; designating a Home
operator does not override those invariants. Trusted host-access execution, same-native
Goal lifecycle, delegated completion follow-up and deployment checkpoint/resume require
their canonical execution/authority contracts and separate qualification. The feature
issue remains open until its full UI and live acceptance is satisfied.
