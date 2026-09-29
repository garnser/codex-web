# External capability providers and ECC consideration

This is an architectural consideration, not an enabled integration or a commitment
to implement one. [ECC](https://github.com/affaan-m/ECC) is a potential external
capability provider and a benchmark for selected engineering workflows. Its
[upstream overview](https://github.com/affaan-m/ECC/blob/main/README.md) describes
agents, skills, hooks, memory/learning and AgentShield scanning. Those descriptions
are upstream claims, not codex-web qualification evidence. No ECC installation,
hook execution or runtime registration is implied here.

## Canonical ownership

Any future adapter must be provider-neutral and extend existing primitives:

| External concept | Canonical codex-web boundary |
| --- | --- |
| Skills and workflow guidance | Versioned Skill/Definition references with publisher, source revision, digest and scope; bounded discovery and selection, not automatic catalog injection |
| Specialized agents | Capabilities behind Agent Profiles and eligible AgentProviders/AgentRuntimes; no second identity or Team model |
| Hooks and executable tools | Extension lifecycle plus isolated execution-worker assignments; declared capabilities never grant authority |
| Memory, instincts and learned procedures | Candidate evidence/knowledge submitted through Organizational Memory with provenance, review, classification and retention |
| AgentShield findings | Security-scan Evidence linked to Artifacts, then canonical Attention/Incident/Audit as applicable; findings do not grant approvals or settle incidents themselves |
| External configuration | Typed configuration and secret references; canonical policy still controls tenant, Project, resources, network and side effects |

The governing contracts are [Agent Profiles](agent-profiles.md),
[Definitions](definition-registry.md), [extensions](extensions.md),
[worker isolation](execution-worker-boundary.md),
[Organizational Memory](organizational-memory.md),
[Evidence](artifact-evidence.md), and
[token efficiency](token-efficiency-rules.md). Package instructions, prompts,
findings and learned text are untrusted data. They cannot override those contracts.

## Evaluation required before an implementation proposal

A future proposal must identify a specific user task and compare the external
capability with the existing native path. Use pinned source/package versions and
an isolated, bounded qualification environment. Establish the generic discovery
and compatibility contract first; do not assume the existing extension and Skill
foundations already constitute a qualified ECC adapter.

Evaluate publisher provenance, license obligations, package integrity, dependencies,
upgrade/revocation behavior, supported harness versions and rollback limits.
Exercise denied Organization/Project scopes, conflicting guidance, malicious hooks,
unavailable dependencies and provider failure. Verify secrets remain behind the
credential boundary and side effects retain canonical ActionIntents and receipts.

Measure task quality, verification success, latency, model calls, context tokens,
cost and idle activity against the native baseline. Set budgets before running;
retrieve only selected capabilities. Include a deterministic path for routine
checks and a bounded escalation path. Benefits must exceed measured operational
and context overhead; a large catalog or claimed scanner coverage is insufficient.

Learning imports need source attribution, review and deletion propagation before
entering retrieval. Security findings need schema validation, deduplication,
severity/provenance and independent verification where required. Neither may
create a parallel authoritative memory or security system.

## Revisit and product impact

Revisit when a user requests the integration, a concrete missing capability is
costly to reproduce, a generic capability-provider interface needs a reference
adapter, or comparative evidence shows useful workflow/security improvements.
Provider discovery becoming a product requirement is also a valid trigger.

Any implementation must be proposed in separate GitHub issue(s), with explicit
scope, acceptance evidence, dependency ordering and UI adaptation. Operators must
be able to inspect source/version, installation versus enablement versus grants,
Organization/Project scope, effective policy, compatibility, denials, revocation
and produced evidence through canonical surfaces. This consideration alone adds
no operator object or action, so it requires no UI change.

Codex-web continues to own identity, authority, policy, work, approvals, execution,
canonical knowledge and evidence. External packages may supply bounded capabilities
within those controls. Issue [#919](https://github.com/garnser/codex-web/issues/919)
records the consideration's provenance; delivery status remains in GitHub.
