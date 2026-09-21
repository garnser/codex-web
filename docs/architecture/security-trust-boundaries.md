# Security trust boundaries and untrusted-content handling

Codex-web separates **control-plane authority** from **content-plane data**. Text that looks like an instruction does not become policy merely because it appears in a task, repository, webhook, tool response, model response, log, memory record, or provider payload.

## Trust zones

The canonical trust model classifies data into explicit zones:

- `canonical_control`: typed codex-web identity, policy, approval, resource, Work Item, ActionIntent, and security decisions;
- `user_input`, `provider_content`, `task_text`, `repository_content`, `retrieved_memory`, `web_content`, `webhook_content`, `tool_output`, and `model_output`: **untrusted data**;
- `secret_material`: sensitive data that must not cross model/tool/log/provider boundaries except through a bounded credential-use seam;
- `execution_sandbox` and `privileged_action_provider`: constrained execution zones.

Untrusted content may contain useful facts and requested work. It cannot grant itself permissions, redefine identity, weaken approval/sandbox policy, create trusted authorization evidence, or authorize an external side effect.

## Model boundary

Every effective developer-instruction contract includes the mandatory security trust-boundary rule.

Work Item task/provider text is wrapped as:

`<UNTRUSTED_DATA ...> ... </UNTRUSTED_DATA>`

before the validated canonical execution contract is appended.

The wrapper is a semantic boundary, not an attempt to delete suspicious words. Prompt-injection strings remain visible as task data, but the trusted instruction explicitly states that the enclosed text is not authority.

Generated security instructions are injected at runtime and stripped before thread instructions are persisted so they do not become mutable user-owned configuration.

## Model/tool output and authorization

Model output and tool output are always untrusted data. A model saying "approved", "policy allows", or "run this privileged action" is not authorization evidence.

Privileged ActionIntents require:

1. an explicit authority decision with outcome `allow`;
2. an explicit policy decision with outcome `allow`;
3. both decisions to come from trusted canonical sources such as `identity:`, `policy:`, `security:`, `approval:`, or `canonical:`;
4. an ActionIntent security decision with outcome `allow`;
5. a second security evaluation immediately before provider execution.

High/critical actions therefore fail closed when authority/policy is absent, denied, or attributed to model/tool/content sources.

## ActionProvider contract 1.1

ActionProvider 1.1 adds machine-readable execution-boundary requirements to each ActionDefinition:

- `network_access`;
- `filesystem_access = none|read|write`;
- `process_access`.

Provider bindings add an `ExecutionSecurityPolicy` containing:

- sandbox mode;
- outbound network allowlist;
- allowed schemes/ports/private CIDRs;
- filesystem read/write roots;
- process execution/shell/executable allowlist;
- immutable-digest requirement for executable/generated artifacts.

Provider 1.0 remains explicitly supported and deprecated during migration. Missing 1.1 fields use restrictive defaults.

## Durable ActionIntent trust decision

ActionIntent state schema 1.1 snapshots both:

- the binding's execution security policy;
- the security trust decision produced when the intent is created.

Legacy 1.0 ActionIntents migrate with a **deny** decision saying security re-evaluation is required. They are not grandfathered into trust.

Before a claimed intent transitions to provider execution, codex-web resolves the current binding again and re-evaluates security. A binding/policy change after queueing can therefore cancel an unsafe action before any provider mutation occurs.

## Network and SSRF boundary

Network is disabled by default.

When enabled, outbound URLs are validated against canonical policy:

- scheme must be explicitly allowed;
- embedded URL credentials are forbidden;
- host must match the allowlist when configured;
- port must be allowed;
- DNS results are inspected;
- loopback, link-local, private, multicast, reserved, and unspecified addresses are rejected unless they fall inside an explicitly allowed private CIDR.

This protects against literal-IP SSRF and DNS-rebinding-style resolution to metadata/internal addresses.

Provider/action URL-like parameters such as `url`, `endpoint`, `api_base`, and `webhook_url` are validated when the action declares network access.

## Filesystem boundary

Filesystem access is denied unless the binding policy declares allowed roots.

Paths are resolved before comparison, so `..` traversal and symlink resolution cannot escape the configured roots.

Actions declaring read/write filesystem access are checked against the corresponding roots. Path-like action parameters are validated before the provider runs.

## Process boundary

Process execution is controlled separately from filesystem access.

Policy can:

- disable processes entirely;
- restrict executable names;
- disable shell execution.

Process-capable actions must comply with these controls before provider execution.

## Supply-chain boundary

When policy requires immutable executable artifacts, process-capable/generated executable inputs must include an immutable digest before privileged use.

This is the platform seam for later package/signature/provenance verification rather than trusting filenames or generated scripts.

## Secret-exfiltration boundary

Security-event details and other boundary payloads are redacted for credential/token/password/authorization/cookie fields and common token-like text.

The existing SecretBroker also scrubs secret material from returned Pydantic provider results before they can become durable ActionIntent receipts.

Raw credentials remain resolved only inside the bounded provider execution callback.

## Security events

Security evaluations produce durable tenant-scoped `SecurityEvent` records.

Denied action trust evaluations record:

- violation kind;
- actor;
- Work Item/execution/ActionIntent attribution;
- Resource IDs;
- provider/action/risk metadata;
- secret-safe reason/details.

Tenant administrators can inspect:

- `GET /api/security/trust-zones`
- `GET /api/security/events`
- `GET /api/security/events?violation_only=true`

Events are diagnostic/audit facts. They do not themselves grant authority.

## Multi-repository execution boundary

Repository content, migrated thread content, task text and model output remain untrusted data. They cannot select a repository target, widen a sandbox, grant broker operations, introduce a SecretReference, or convert read-only context into mutation authority.

For local isolated execution, the selected repository workspace is the only repository mount that may be writable. Explicit sibling repository context is mounted read-only. `danger-full-access` does not change that resource boundary, does not mount the host root, does not disable tenant/resource leases or fencing, and does not grant control-plane administrator credentials. Network capability remains independently qualified.

Orchestration-only assignments have no repository/Git authority. Their control-plane access is assignment-bound, allowlisted, tenant/project scoped, fenced and audited. Arbitrary localhost/network proxying is rejected.

Legacy sandbox/profile migration must surface material authority differences. Historical `danger-full-access` semantics are not silently treated as equivalent to the current contained worker boundary.

See [Multi-repository security qualification](../operations/multi-repository-security-qualification.md) for executable evidence.

## Fail-closed rules

Codex-web fails closed when:

- privileged authority comes from untrusted/model/tool content;
- a resource target leaves the provider binding;
- network is required but disabled;
- a network target violates host/port/address policy;
- filesystem access has no allowed root or escapes it;
- process execution/shell/executable is disallowed;
- a required executable digest is absent;
- a high/critical action requests `danger-full-access`;
- a pre-security ActionIntent has not yet been re-evaluated.

## Adversarial test coverage

Focused security tests cover:

- malicious task prompt injection retained as untrusted data;
- model-output attempts to self-authorize privileged actions;
- canonical policy authorization of a bounded privileged action;
- DNS resolution to link-local metadata endpoints;
- URL credential/disallowed-host/private-address SSRF;
- filesystem traversal;
- process/shell escape;
- missing executable digest;
- secret-like boundary payload redaction;
- policy changes between ActionIntent queueing and execution;
- legacy ActionIntent fail-closed migration.

Operations UI work in #141 should show trust decisions and security violations from canonical records rather than inferring security state from model narration.
