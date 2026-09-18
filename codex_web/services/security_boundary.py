from __future__ import annotations

import ipaddress
import re
import socket
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from codex_web.action_providers import ActionDefinition, ActionProviderBinding, ActionRequest
from codex_web.identity import AuthenticationActor
from codex_web.security import (
    ExecutionSecurityPolicy,
    NetworkEgressPolicy,
    SecurityDecisionOutcome,
    SecurityEvent,
    SecurityTrustDecision,
    SecurityViolationKind,
    TrustZone,
    ZONE_CLASS,
    envelope_untrusted,
    render_untrusted_content,
    security_boundary_instructions,
    validate_path_within,
)
from codex_web.storage.security_events import SecurityEventStore


class SecurityBoundaryError(RuntimeError):
    pass


class SecurityTrustViolation(SecurityBoundaryError):
    pass


class EgressPolicyViolation(SecurityBoundaryError):
    pass


class FilesystemPolicyViolation(SecurityBoundaryError):
    pass


class ProcessPolicyViolation(SecurityBoundaryError):
    pass


class SupplyChainPolicyViolation(SecurityBoundaryError):
    pass


SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"\b(?:glpat|xox[baprs]|gh[pousr])-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*([^\s,;]+)"),
)


def redact_sensitive_text(value: str) -> str:
    text = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def redact_boundary_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if any(
                    fragment in str(key).casefold().replace("-", "_")
                    for fragment in ("password", "secret", "credential", "token", "authorization", "cookie")
                )
                else redact_boundary_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_boundary_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_boundary_payload(item) for item in value)
    return value


class SecurityBoundaryService:
    """Canonical trust-boundary evaluator and security-event recorder."""

    def __init__(
        self,
        store: SecurityEventStore,
        *,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ) -> None:
        self.store = store
        self.resolver = resolver

    def _record(
        self,
        actor: AuthenticationActor,
        *,
        event_type: str,
        outcome: SecurityDecisionOutcome,
        violation_kind: SecurityViolationKind | None = None,
        reason: str | None = None,
        source_zone: TrustZone | None = None,
        work_item_ref: str | None = None,
        execution_id: str | None = None,
        action_intent_id: str | None = None,
        resource_ids: tuple[str, ...] = (),
        details: dict[str, str | int | float | bool | None] | None = None,
    ) -> SecurityEvent:
        event = SecurityEvent(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            event_type=event_type,
            violation_kind=violation_kind,
            outcome=outcome,
            actor_identity_id=actor.identity_id,
            work_item_ref=work_item_ref,
            execution_id=execution_id,
            action_intent_id=action_intent_id,
            resource_ids=resource_ids,
            source_zone=source_zone,
            reason=reason,
            details=redact_boundary_payload(details or {}),
        )

        def apply(state):
            state.events.append(event)
            state.events = state.events[-5000:]
            return state

        self.store.update(apply)
        return event

    def events(
        self,
        actor: AuthenticationActor,
        *,
        violation_only: bool = False,
    ) -> list[SecurityEvent]:
        items = [
            item
            for item in self.store.load().events
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]
        if violation_only:
            items = [item for item in items if item.violation_kind is not None]
        return sorted(items, key=lambda item: (item.occurred_at, item.id), reverse=True)

    @staticmethod
    def prompt_boundary() -> str:
        return security_boundary_instructions()

    @staticmethod
    def untrusted(zone: TrustZone, source: str, content: str) -> str:
        if ZONE_CLASS[zone].value != "untrusted_data":
            raise SecurityTrustViolation(f"{zone.value} is not an untrusted-data zone")
        return render_untrusted_content(envelope_untrusted(zone, source, content))

    @staticmethod
    def _private_ip_allowed(address: ipaddress._BaseAddress, policy: NetworkEgressPolicy) -> bool:
        return any(
            address in ipaddress.ip_network(cidr, strict=False)
            for cidr in policy.allowed_private_cidrs
        )

    def validate_outbound_url(
        self,
        url: str,
        policy: NetworkEgressPolicy,
        *,
        actor: AuthenticationActor | None = None,
    ) -> str:
        if not policy.enabled:
            raise EgressPolicyViolation("network egress is disabled")
        parsed = urlsplit(str(url).strip())
        scheme = parsed.scheme.casefold()
        if scheme not in {value.casefold() for value in policy.allowed_schemes}:
            raise EgressPolicyViolation("URL scheme is not allowed")
        if parsed.username is not None or parsed.password is not None:
            raise EgressPolicyViolation("credentials in outbound URLs are forbidden")
        host = (parsed.hostname or "").casefold().rstrip(".")
        if not host:
            raise EgressPolicyViolation("outbound URL requires a hostname")
        if policy.allowed_hosts and not any(
            self._host_matches(host, allowed) for allowed in policy.allowed_hosts
        ):
            raise EgressPolicyViolation("outbound host is outside the canonical allowlist")
        port = parsed.port or (443 if scheme == "https" else 80)
        if policy.allowed_ports and port not in policy.allowed_ports:
            raise EgressPolicyViolation("outbound port is outside the canonical allowlist")

        try:
            literal = ipaddress.ip_address(host.strip("[]"))
            addresses = {literal}
        except ValueError:
            try:
                resolved = self.resolver(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise EgressPolicyViolation("outbound hostname resolution failed") from exc
            addresses = set()
            for item in resolved:
                sockaddr = item[4]
                if not sockaddr:
                    continue
                try:
                    addresses.add(ipaddress.ip_address(sockaddr[0]))
                except ValueError:
                    continue
            if not addresses:
                raise EgressPolicyViolation("outbound hostname resolved to no usable address")

        for address in addresses:
            unsafe = (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            )
            if unsafe and not self._private_ip_allowed(address, policy):
                raise EgressPolicyViolation(
                    "outbound target resolves to private/local/reserved address outside canonical policy"
                )
        return parsed.geturl()

    @staticmethod
    def _host_matches(host: str, allowed: str) -> bool:
        normalized = allowed.casefold().rstrip(".")
        if normalized.startswith("*."):
            suffix = normalized[1:]
            return host.endswith(suffix) and host != normalized[2:]
        return host == normalized

    @staticmethod
    def validate_read_path(path: str | Path, policy: ExecutionSecurityPolicy) -> Path:
        try:
            return validate_path_within(Path(path), policy.filesystem.allowed_read_roots)
        except ValueError as exc:
            raise FilesystemPolicyViolation(str(exc)) from exc

    @staticmethod
    def validate_write_path(path: str | Path, policy: ExecutionSecurityPolicy) -> Path:
        try:
            return validate_path_within(Path(path), policy.filesystem.allowed_write_roots)
        except ValueError as exc:
            raise FilesystemPolicyViolation(str(exc)) from exc

    @staticmethod
    def validate_process(
        executable: str,
        *,
        shell: bool,
        policy: ExecutionSecurityPolicy,
    ) -> str:
        process = policy.process
        if not process.allow_process_execution:
            raise ProcessPolicyViolation("process execution is disabled")
        if shell and not process.allow_shell:
            raise ProcessPolicyViolation("shell execution is disabled")
        name = Path(executable).name
        if process.allowed_executables and name not in process.allowed_executables:
            raise ProcessPolicyViolation("executable is outside canonical allowlist")
        return name

    @staticmethod
    def require_supply_chain_digest(
        *,
        executable: bool,
        digest: str | None,
        policy: ExecutionSecurityPolicy,
    ) -> None:
        if executable and policy.require_digest_for_executable_artifacts and not digest:
            raise SupplyChainPolicyViolation(
                "executable/generated artifact requires immutable digest before privileged use"
            )

    @staticmethod
    def trusted_decision_source(source: str) -> bool:
        normalized = str(source or "").strip().casefold()
        return normalized.startswith(
            (
                "canonical:",
                "identity:",
                "policy:",
                "security:",
                "approval:",
            )
        )

    @staticmethod
    def _parameter_values(parameters: Any, keys: set[str]) -> list[str]:
        values: list[str] = []
        if isinstance(parameters, dict):
            for key, value in parameters.items():
                normalized = str(key).casefold().replace("-", "_")
                if isinstance(value, str) and (
                    normalized in keys
                    or any(normalized.endswith(f"_{item}") for item in keys)
                ):
                    values.append(value)
                values.extend(SecurityBoundaryService._parameter_values(value, keys))
        elif isinstance(parameters, (list, tuple)):
            for item in parameters:
                values.extend(SecurityBoundaryService._parameter_values(item, keys))
        return values

    def _validate_declared_boundaries(
        self,
        definition: ActionDefinition,
        request: ActionRequest,
        policy: ExecutionSecurityPolicy,
    ) -> list[str]:
        reasons: list[str] = []
        if definition.network_access:
            if not policy.network.enabled:
                reasons.append("action requires network but egress policy disables it")
            else:
                for candidate in self._parameter_values(
                    request.parameters,
                    {"url", "endpoint", "api_base", "webhook_url"},
                ):
                    try:
                        self.validate_outbound_url(candidate, policy.network)
                    except EgressPolicyViolation as exc:
                        reasons.append(f"network target denied: {exc}")
        if definition.filesystem_access in {"read", "write"}:
            roots = (
                policy.filesystem.allowed_write_roots
                if definition.filesystem_access == "write"
                else (
                    policy.filesystem.allowed_read_roots
                    or policy.filesystem.allowed_write_roots
                )
            )
            if not roots:
                reasons.append(
                    f"action requires filesystem {definition.filesystem_access} but no root is allowed"
                )
            else:
                for candidate in self._parameter_values(
                    request.parameters,
                    {"path", "file_path", "cwd", "directory"},
                ):
                    try:
                        validate_path_within(Path(candidate), roots)
                    except ValueError as exc:
                        reasons.append(f"filesystem target denied: {exc}")
        if definition.process_access:
            if not policy.process.allow_process_execution:
                reasons.append("action requires process execution but process policy disables it")
            else:
                executable_values = self._parameter_values(
                    request.parameters,
                    {"executable", "binary", "command"},
                )
                for executable in executable_values:
                    first = executable.split()[0] if executable.split() else executable
                    try:
                        self.validate_process(first, shell=False, policy=policy)
                    except ProcessPolicyViolation as exc:
                        reasons.append(f"process target denied: {exc}")
                digest = request.parameters.get("artifact_digest")
                try:
                    self.require_supply_chain_digest(
                        executable=True,
                        digest=str(digest) if digest else None,
                        policy=policy,
                    )
                except SupplyChainPolicyViolation as exc:
                    reasons.append(str(exc))
        return reasons

    def evaluate_action(
        self,
        *,
        binding: ActionProviderBinding,
        definition: ActionDefinition,
        request: ActionRequest,
        authority_outcome: str,
        authority_source: str,
        policy_outcome: str,
        policy_source: str,
        actor: AuthenticationActor,
        action_intent_id: str | None = None,
        work_item_ref: str | None = None,
        execution_id: str | None = None,
    ) -> SecurityTrustDecision:
        security_policy = binding.security_policy
        reasons: list[str] = []

        if tuple(request.resource_ids) and binding.resource_ids and not set(
            request.resource_ids
        ).issubset(set(binding.resource_ids)):
            reasons.append("resource target escapes provider binding")

        privileged = definition.risk_class.value in {"high", "critical"}
        if authority_outcome == "deny" or policy_outcome == "deny":
            reasons.append("authority or policy denied action")
        if privileged:
            if authority_outcome != "allow" or not self.trusted_decision_source(authority_source):
                reasons.append("privileged action lacks trusted explicit authority allow")
            if policy_outcome != "allow" or not self.trusted_decision_source(policy_source):
                reasons.append("privileged action lacks trusted explicit policy allow")

        reasons.extend(
            self._validate_declared_boundaries(
                definition,
                request,
                security_policy,
            )
        )
        if privileged and security_policy.sandbox == "danger-full-access":
            reasons.append("privileged action cannot use danger-full-access sandbox")

        outcome = (
            SecurityDecisionOutcome.DENY
            if reasons
            else SecurityDecisionOutcome.ALLOW
        )
        decision = SecurityTrustDecision(
            outcome=outcome,
            risk_class=definition.risk_class.value,
            resource_ids=request.resource_ids,
            sandbox=security_policy.sandbox,
            network_enabled=security_policy.network.enabled,
            authority_source=authority_source,
            policy_source=policy_source,
            reasons=tuple(reasons),
        )
        self._record(
            actor,
            event_type="action_trust_evaluated",
            outcome=outcome,
            violation_kind=(
                SecurityViolationKind.ACTION_TRUST_BOUNDARY
                if outcome == SecurityDecisionOutcome.DENY
                else None
            ),
            reason="; ".join(reasons) if reasons else None,
            work_item_ref=work_item_ref,
            execution_id=execution_id,
            action_intent_id=action_intent_id,
            resource_ids=request.resource_ids,
            details={
                "provider_type": binding.provider_type,
                "provider_instance": binding.provider_instance,
                "action_id": definition.action_id,
                "risk_class": definition.risk_class.value,
            },
        )
        return decision
