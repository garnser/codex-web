# Author and review an operational procedure Skill

Use an ordinary Skill draft in the intended tenant workspace. Include an optional
`operational_procedure` object using the schema returned by the authenticated
`GET /api/skills/procedure-schema` endpoint. The Definition kind and outer schema
remain `agent.skill@1.0`; the nested procedure schema is explicitly `1.0`.

Start with canonical Resource IDs/types, the operation's registered ActionProvider
and contract version, required policy capabilities, intended environments and
service-version interval. Declare the owner, input contracts, review and expiry
dates, supported provider/worker capabilities, conflict group and precedence.
For every step, record evidence, finite timeout/attempt/backoff values, preconditions,
failure/escalation and reconciliation requirements. Describe rotation/restart
impact and verification. Conditional rollback requires a registered reversible
operation, a failure condition and its own subsequent verification.

A GitLab investigation can use a registered read operation for filtered logs,
a registered service change for rotation/restart, and an isolated worker artifact
for HTTP validation. A failed verification can conditionally propose the registered
rollback and then require rollback verification. This description creates no host
access or service permission. Supply current operational inputs by canonical
reference; secret references resolve only inside the authorized credential boundary.

Create the draft through `POST /api/skills` or import a reviewed bundle through
`POST /api/skills/import`. For repository runbooks, retain source location/revision
and evidence in normal Skill provenance. Imports are inactive drafts and are marked
imported for the existing security policy. A file path is source provenance, not an
authoritative catalog or instruction to execute the file.

Inspect the returned record ID/revision/checksum and full contract in the canonical
Definition inspector or Skill response. Run the existing static security scan for
that exact revision and review the findings. Procedure content is included in the
scan. Validate and publish through the existing lifecycle; the generic Definitions
path has the same procedure publication gate. A changed contract needs its own
review/scan under the effective policy. No reasoning/model call is needed to validate
known facts. Do not copy scan status or approval attestations from another revision.

If publication fails, keep the draft and inspect the canonical denial. Correct an
unsupported operation/version, tenant/Resource mismatch, unbounded step, raw secret,
malformed condition, expired review or current-revision scan failure. A missing
provider binding or actual execution permission cannot be fixed by adding a grant
to the procedure. Policy/approval and worker admission remain separate canonical
operator operations.

Supersede with a new draft when content or review dates change. Historical payloads
and checksum pins remain immutable. Rollback creates a new draft/revision and uses
the same publication/security checks; an imported rollback can require scanning
that new revision before publishing. Quarantine blocks use. Archiving can disable
an existing procedure even when it is stale or its operation registration is gone.

Required reference validation exposes missing, disabled, quarantined, expired,
incompatible and security-blocked results. A historical reference is inspectable
but does not imply that new execution is permitted. The procedure schema establishes
these contracts; execution discovery, step dispatch and learning are delivered in
separate slices. Structured operator editing/preview/traces are tracked by
[the linked UI issue](https://github.com/garnser/codex-web/issues/1428).

See [the architecture contract](../architecture/skill-operational-procedures.md),
[Skill security](../architecture/skill-security.md), and
[Definition Registry administration](../administration/definition-registry.md).
