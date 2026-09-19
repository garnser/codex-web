# Claude execution-agent runtime

Issue: #352.

Claude execution is integrated behind the canonical AgentRuntimeAdapter and AgentSession contracts. Provider-native Claude session IDs, stream events and control messages are compatibility data; they are never canonical codex-web identity or authority.

## Canonical adapter boundary

ClaudeAgentRuntimeAdapter exposes the same lifecycle surface used by the Codex adapter:

- create, resume, read, close and restore a provider-backed session;
- start a turn and interrupt active work;
- project provider-native stream messages into AgentRuntimeEvent;
- send a provider permission response only after the canonical approval layer has decided it;
- report runtime health and recovery through the provider-neutral registry.

The adapter declares only semantics that codex-web can represent safely. In particular, it does not claim native context compaction because current Claude Agent SDK / Claude Code control semantics do not provide a programmatic compaction operation equivalent to the canonical capability.

## Authority and approval boundary

Claude tool permissions are not an authorization source. A native tool request may be used as transport for a canonical ApprovalRequest, but Role authority, quorum, target binding, expiry and approval consumption remain codex-web state.

Provider-native permission modes, settings files and remembered tool decisions must never widen the current ExecutionAssignment. A provider response is sent only after the canonical approval boundary permits it. An unavailable or ambiguous permission mapping fails closed.

## Assignment-bound execution

The production runtime is required to run behind the generalized assignment-bound worker session introduced by #350. That boundary owns:

- the canonical ExecutionAssignment and ExecutionWorkspace;
- worker lease/fence validation and revocation;
- sandbox, filesystem, network and resource limits;
- purpose-specific provider credentials resolved through SecretBroker;
- assignment-bound model-provider egress;
- deadline and worker-lifecycle termination.

Claude must not inherit control-plane state, unrelated workspaces, ambient credentials or generic host networking. Shell/file/Git tools remain limited to the leased execution workspace and the worker policy.

## Event normalization

Native Claude messages remain in the event payload for adapter-level diagnostics, while the canonical projection carries provider-native session/turn identifiers separately. The initial normalization distinguishes text, tool requests, result/status messages and control events without treating native identifiers as AgentSession IDs.

Cross-runtime usage/evidence normalization is owned by #353; this adapter therefore declares partial usage rather than inventing precision.

## UI impact

This backend slice adds no Claude-specific UI or client-side routing state. #354 owns the shared Agent Providers/runtime-selection/AgentSession operator surface, and #127 tracks required responsive/accessibility/product adaptations.
