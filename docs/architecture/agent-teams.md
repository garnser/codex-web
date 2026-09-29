# Team lifecycle and consumer impact

Teams are reusable workspace identities distinct from individual Agent Profiles.
Canonical Team revisions retain the leader, members, delegated-access constraints,
budgets, exact instruction Definition reference and actor/reason provenance.
Lifecycle changes append immutable revisions; they do not rewrite prior membership,
execution links, delegation decisions or published Definitions.

The supported lifecycle is active, disabled and archived. The canonical APIs expose
disable, archive and restore. Archive is retirement; hard delete is unsupported.
The existing owner/admin, MFA and service-scope checks govern each mutation.
An optional `expected_revision` on lifecycle requests rejects stale edits.

`GET /api/agent-teams/{team_id}/usage` exposes a read-only `1.0` projection from
existing canonical sources:

- effective Automation Definitions targeting the Team in visible Projects;
- Team delegation history, grouped by Work Item with exact Team revision;
- execution assignments linked through coordinator/member execution IDs;
- queued delegated invocations linked by those execution IDs.

Enabled Automations and pending/claimed/running or queued linked executions block
disable/archive. Historical delegation records and terminal execution assignments
remain visible without blocking retirement. Delegation history is provenance,
not proof that new work may run: future delegation still checks Team lifecycle,
profile access, authority, budgets and execution controls. Disabling a Team does
not change those budgets or grant a member new authority.

The API checks Team visibility before projection and excludes foreign-tenant
assignments and queued work. It never returns queued message text, reply targets
or credentials. The visible list is limited to 100 consumers, while counts and
blockers include the complete verified projection up to a 5,000-consumer bound.
An unavailable or over-limit projection cannot authorize disable/archive.

The lifecycle API re-reads impact after normal mutation authorization, independently
of the browser preview. The UI exposes Usage, impact, a reason and confirmation,
and preserves inputs on rejection. Restore remains available when impact is
unavailable. This projection is an observation rather than a reservation across
consumer stores; existing execution/delegation admission checks remain mandatory.
No second consumer registry, mutable Team-definition catalog or model call is added.
