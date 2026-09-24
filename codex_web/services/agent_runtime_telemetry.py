from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from codex_web.agent_providers import AgentProviderCapability
from codex_web.agent_runtime import AgentRuntimeEvent, AgentSession
from codex_web.agent_runtime_usage import (
    AgentRuntimeUsage,
    RuntimeTelemetryCompleteness,
    RuntimeTerminalOutcome,
)
from codex_web.artifact_evidence import (
    EvidenceCreate,
    EvidenceResult,
    EvidenceType,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
)
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.agent_sessions import AgentSessionStore


AttributionResolver = Callable[[AgentSession], dict[str, str | None]]


class AgentRuntimeTelemetryService:
    """Normalize provider runtime telemetry without persisting transcripts."""

    def __init__(
        self,
        store: AgentRuntimeUsageStore,
        sessions: AgentSessionStore,
        runtimes: AgentRuntimeRegistry,
        *,
        artifact_evidence: ArtifactEvidenceService | None = None,
        attribution_resolver: AttributionResolver | None = None,
        observation_notifier: Callable[[AgentRuntimeUsage], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.runtimes = runtimes
        self.artifact_evidence = artifact_evidence
        self.attribution_resolver = attribution_resolver
        self.observation_notifier = observation_notifier
        self.clock = clock
        self._unsubscribers: list[Callable[[], None]] = []

    def close(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    def subscribe(self, adapter: Any) -> Callable[[], None]:
        def listener(event: AgentRuntimeEvent) -> None:
            self.observe_event(
                adapter.provider_id,
                adapter.runtime_id,
                event,
            )

        unsubscribe = adapter.subscribe_events(listener)
        self._unsubscribers.append(unsubscribe)
        return unsubscribe

    def list(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None = None,
        execution_id: str | None = None,
        agent_session_id: str | None = None,
    ) -> list[AgentRuntimeUsage]:
        records = [
            record
            for record in self.store.list()
            if record.organization_id == actor.organization_id
            and record.workspace_id == actor.workspace_id
        ]
        if project_id is not None:
            records = [record for record in records if record.project_id == project_id]
        if execution_id is not None:
            records = [record for record in records if record.execution_id == execution_id]
        if agent_session_id is not None:
            records = [
                record
                for record in records
                if record.agent_session_id == agent_session_id
            ]
        return records

    def _canonical_session(
        self,
        provider_id: str,
        runtime_id: str,
        native_session_id: str | None,
    ) -> AgentSession | None:
        if not native_session_id:
            return None
        matches = [
            session
            for session in self.sessions.list()
            if session.provider_id == provider_id
            and session.runtime_id == runtime_id
            and session.provider_native_session_id == native_session_id
        ]
        # A provider-native id is compatibility data, not canonical identity.
        # If it is ambiguous across tenants, do not guess.
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _fingerprint(
        provider_id: str,
        runtime_id: str,
        event: AgentRuntimeEvent,
    ) -> str:
        material = json.dumps(
            {
                "provider": provider_id,
                "runtime": runtime_id,
                "event_type": event.event_type,
                "session": event.provider_native_session_id,
                "turn": event.provider_native_turn_id,
                "payload": event.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            result = int(value)
        except (TypeError, ValueError):
            return None
        return result if result >= 0 else None

    @staticmethod
    def _float(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result >= 0 else None

    @classmethod
    def _usage_fields(cls, payload: dict[str, Any]) -> dict[str, int | float | None]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            params = payload.get("params")
            if isinstance(params, dict):
                token_usage = params.get("tokenUsage")
                if isinstance(token_usage, dict):
                    last = token_usage.get("last")
                    usage = last if isinstance(last, dict) else token_usage
        if not isinstance(usage, dict):
            usage = {}

        input_tokens = cls._int(
            usage.get("input_tokens")
            if "input_tokens" in usage
            else usage.get("inputTokens")
        )
        output_tokens = cls._int(
            usage.get("output_tokens")
            if "output_tokens" in usage
            else usage.get("outputTokens")
        )
        cached_input = cls._int(
            usage.get("cache_read_input_tokens")
            if "cache_read_input_tokens" in usage
            else usage.get("cachedInputTokens")
        )
        cache_write = cls._int(
            usage.get("cache_creation_input_tokens")
            if "cache_creation_input_tokens" in usage
            else usage.get("cacheWriteInputTokens")
        )
        reasoning = cls._int(
            usage.get("reasoning_output_tokens")
            if "reasoning_output_tokens" in usage
            else usage.get("reasoningOutputTokens")
        )
        total = cls._int(
            usage.get("total_tokens")
            if "total_tokens" in usage
            else usage.get("totalTokens")
        )
        if total is None and input_tokens is not None and output_tokens is not None:
            total = input_tokens + output_tokens

        cost = cls._float(
            payload.get("total_cost_usd")
            if "total_cost_usd" in payload
            else usage.get("cost_usd")
        )
        duration_ms = cls._float(
            payload.get("duration_ms")
            if "duration_ms" in payload
            else payload.get("durationMs")
        )
        duration = cls._float(payload.get("duration_seconds"))
        if duration is None and duration_ms is not None:
            duration = duration_ms / 1000.0

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_input,
            "cache_write_input_tokens": cache_write,
            "reasoning_output_tokens": reasoning,
            "total_tokens": total,
            "cost_usd": cost,
            "runtime_duration_seconds": duration,
        }

    @staticmethod
    def _models(payload: dict[str, Any]) -> tuple[str, ...]:
        values: list[str] = []
        for key in ("model", "model_id", "modelId"):
            value = payload.get(key)
            if value:
                values.append(str(value))
        model_usage = payload.get("modelUsage")
        if isinstance(model_usage, dict):
            values.extend(str(key) for key in model_usage if key)
        message = payload.get("message")
        if isinstance(message, dict) and message.get("model"):
            values.append(str(message["model"]))
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _request_ids(payload: dict[str, Any]) -> tuple[str, ...]:
        values: list[str] = []
        for key in ("request_id", "requestId", "provider_request_id"):
            value = payload.get(key)
            if value is not None:
                values.append(str(value))
        return tuple(dict.fromkeys(values))

    @staticmethod
    def _runtime_version(payload: dict[str, Any]) -> str | None:
        for key in (
            "runtime_version",
            "claude_code_version",
            "version",
            "app_server_version",
        ):
            value = payload.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _terminal_outcome(event_type: str, payload: dict[str, Any]) -> RuntimeTerminalOutcome:
        value = event_type.casefold()
        subtype = str(payload.get("subtype") or "").casefold()
        if any(term in value for term in ("failed", "error")) or subtype in {
            "error",
            "failure",
            "failed",
        }:
            return RuntimeTerminalOutcome.FAILED
        if any(term in value for term in ("interrupt", "cancel")) or subtype in {
            "interrupted",
            "cancelled",
            "canceled",
        }:
            return RuntimeTerminalOutcome.INTERRUPTED
        if any(term in value for term in ("completed", "success")) or subtype in {
            "success",
            "succeeded",
            "completed",
        }:
            return RuntimeTerminalOutcome.SUCCEEDED
        return RuntimeTerminalOutcome.UNKNOWN

    @staticmethod
    def _tool_counts(event: AgentRuntimeEvent) -> dict[str, int]:
        event_type = event.event_type.casefold()
        payload = event.payload
        tools: list[tuple[str, dict[str, Any]]] = []

        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools.append(
                        (
                            str(block.get("name") or ""),
                            block.get("input") if isinstance(block.get("input"), dict) else {},
                        )
                    )

        params = payload.get("params")
        item = params.get("item") if isinstance(params, dict) else None
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            if item_type:
                tools.append((item_type, item))

        if not tools and "tool/requested" in event_type:
            tools.append((str(payload.get("tool_name") or "tool"), payload))

        counts = {
            "tool_call_count": 0,
            "shell_command_count": 0,
            "file_edit_count": 0,
            "git_operation_count": 0,
        }
        for name, tool_input in tools:
            lower = name.casefold()
            counts["tool_call_count"] += 1
            if lower in {"bash", "shell", "commandexecution", "command_execution"}:
                counts["shell_command_count"] += 1
                command = str(
                    tool_input.get("command")
                    or tool_input.get("cmd")
                    or tool_input.get("input")
                    or ""
                ).lstrip()
                if command.startswith("git ") or command == "git":
                    counts["git_operation_count"] += 1
            if lower in {
                "write",
                "edit",
                "multiedit",
                "notebookedit",
                "filechange",
                "file_change",
            }:
                counts["file_edit_count"] += 1
            if lower in {"git", "gitoperation", "git_operation"}:
                counts["git_operation_count"] += 1
        return counts

    def _record_id(
        self,
        session: AgentSession,
        native_turn_id: str | None,
    ) -> str:
        material = (
            f"{session.id}\n{native_turn_id or 'session'}"
        ).encode("utf-8")
        return "runtime-usage-" + hashlib.sha256(material).hexdigest()[:32]

    def _attribution(self, session: AgentSession) -> dict[str, str | None]:
        values: dict[str, str | None] = {
            "goal_id": None,
            "work_item_ref": None,
            "decision_id": None,
        }
        if self.attribution_resolver is not None:
            resolved = self.attribution_resolver(session)
            for key in values:
                value = resolved.get(key)
                values[key] = str(value) if value else None
        return values

    def _evidence_actor(self, record: AgentRuntimeUsage) -> AuthenticationActor:
        return AuthenticationActor(
            identity_id="service-agent-runtime-telemetry",
            principal_kind=PrincipalKind.SERVICE,
            organization_id=record.organization_id,
            workspace_id=record.workspace_id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("artifact-evidence:admin",),
        )

    def _emit_terminal_evidence(
        self,
        record: AgentRuntimeUsage,
    ) -> AgentRuntimeUsage:
        if self.artifact_evidence is None or record.evidence_ids:
            return record
        if record.terminal_outcome == RuntimeTerminalOutcome.UNKNOWN:
            return record

        evidence_result = {
            RuntimeTerminalOutcome.SUCCEEDED: EvidenceResult.PASS,
            RuntimeTerminalOutcome.FAILED: EvidenceResult.FAIL,
            RuntimeTerminalOutcome.INTERRUPTED: EvidenceResult.WARN,
        }.get(record.terminal_outcome, EvidenceResult.INFO)
        evidence = self.artifact_evidence.create_evidence(
            EvidenceCreate(
                project_id=record.project_id,
                work_item_ref=record.work_item_ref,
                execution_id=record.execution_id,
                execution_workspace_id=record.execution_workspace_id,
                evidence_type=EvidenceType.RUNTIME_RESULT,
                provider=record.provider_id,
                source=f"agent-runtime:{record.runtime_id}",
                external_id=record.provider_native_turn_id,
                result=evidence_result,
                summary=(
                    f"{record.provider_id}/{record.runtime_id} runtime "
                    f"{record.terminal_outcome.value}; telemetry "
                    f"{record.telemetry_completeness.value}"
                ),
                metadata={
                    "runtime_usage_id": record.id,
                    "agent_session_id": record.agent_session_id,
                    "runtime_id": record.runtime_id,
                    "capability_revision": record.capability_revision,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "tool_call_count": record.tool_call_count,
                    "shell_command_count": record.shell_command_count,
                    "file_edit_count": record.file_edit_count,
                },
            ),
            actor=self._evidence_actor(record),
        )
        return record.model_copy(
            update={"evidence_ids": (*record.evidence_ids, evidence.id)}
        )

    def observe_event(
        self,
        provider_id: str,
        runtime_id: str,
        event: AgentRuntimeEvent,
    ) -> AgentRuntimeUsage | None:
        session = self._canonical_session(
            provider_id,
            runtime_id,
            event.provider_native_session_id,
        )
        if session is None:
            return None

        registration = self.runtimes.registration(provider_id, runtime_id)
        fingerprint = self._fingerprint(provider_id, runtime_id, event)
        record_id = self._record_id(session, event.provider_native_turn_id)
        existing = next(
            (item for item in self.store.list() if item.id == record_id),
            None,
        )
        if existing is not None and fingerprint in existing.event_fingerprints:
            return existing

        now = self.clock()
        attribution = self._attribution(session)
        usage = self._usage_fields(event.payload)
        counts = self._tool_counts(event)
        outcome = self._terminal_outcome(event.event_type, event.payload)
        has_measurement = any(value is not None for value in usage.values()) or any(
            counts.values()
        )
        completeness = (
            RuntimeTelemetryCompleteness.PARTIAL
            if has_measurement
            or AgentProviderCapability.USAGE_PARTIAL in registration.capabilities
            else RuntimeTelemetryCompleteness.UNAVAILABLE
        )
        if AgentProviderCapability.USAGE_EXACT in registration.capabilities:
            completeness = RuntimeTelemetryCompleteness.EXACT

        if existing is None:
            record = AgentRuntimeUsage(
                id=record_id,
                organization_id=session.organization_id,
                workspace_id=session.workspace_id,
                project_id=session.project_id,
                goal_id=attribution["goal_id"],
                work_item_ref=attribution["work_item_ref"],
                decision_id=attribution["decision_id"],
                execution_id=session.execution_id,
                assignment_id=session.assignment_id,
                execution_workspace_id=session.execution_workspace_id,
                agent_session_id=session.id,
                provider_id=provider_id,
                runtime_id=runtime_id,
                runtime_type=registration.runtime_type,
                capability_revision=registration.capability_revision,
                provider_native_session_id=event.provider_native_session_id,
                provider_native_turn_id=event.provider_native_turn_id,
                provider_request_ids=self._request_ids(event.payload),
                observed_model_ids=tuple(
                    dict.fromkeys(
                        [
                            *((session.model,) if session.model else ()),
                            *self._models(event.payload),
                        ]
                    )
                ),
                runtime_version=self._runtime_version(event.payload),
                context_compaction_count=(
                    1
                    if "compact" in event.event_type.casefold()
                    and "completed" in event.event_type.casefold()
                    else 0
                ),
                model_call_count=(
                    1
                    if outcome != RuntimeTerminalOutcome.UNKNOWN
                    and any(value is not None for value in usage.values())
                    else None
                ),
                telemetry_completeness=completeness,
                terminal_outcome=outcome,
                started_at=now if outcome == RuntimeTerminalOutcome.UNKNOWN else None,
                completed_at=(
                    now if outcome != RuntimeTerminalOutcome.UNKNOWN else None
                ),
                observed_at=now,
                event_fingerprints=(fingerprint,),
                **usage,
                **counts,
            )
        else:
            update: dict[str, Any] = {
                "provider_request_ids": tuple(
                    dict.fromkeys(
                        (*existing.provider_request_ids, *self._request_ids(event.payload))
                    )
                ),
                "observed_model_ids": tuple(
                    dict.fromkeys(
                        (*existing.observed_model_ids, *self._models(event.payload))
                    )
                ),
                "runtime_version": (
                    self._runtime_version(event.payload) or existing.runtime_version
                ),
                "tool_call_count": existing.tool_call_count
                + counts["tool_call_count"],
                "shell_command_count": existing.shell_command_count
                + counts["shell_command_count"],
                "file_edit_count": existing.file_edit_count
                + counts["file_edit_count"],
                "git_operation_count": existing.git_operation_count
                + counts["git_operation_count"],
                "context_compaction_count": existing.context_compaction_count
                + (
                    1
                    if "compact" in event.event_type.casefold()
                    and "completed" in event.event_type.casefold()
                    else 0
                ),
                "telemetry_completeness": (
                    RuntimeTelemetryCompleteness.EXACT
                    if RuntimeTelemetryCompleteness.EXACT
                    in {existing.telemetry_completeness, completeness}
                    else RuntimeTelemetryCompleteness.PARTIAL
                    if RuntimeTelemetryCompleteness.PARTIAL
                    in {existing.telemetry_completeness, completeness}
                    else RuntimeTelemetryCompleteness.UNAVAILABLE
                ),
                "terminal_outcome": (
                    outcome
                    if outcome != RuntimeTerminalOutcome.UNKNOWN
                    else existing.terminal_outcome
                ),
                "started_at": existing.started_at or now,
                "completed_at": (
                    now
                    if outcome != RuntimeTerminalOutcome.UNKNOWN
                    else existing.completed_at
                ),
                "observed_at": now,
                "event_fingerprints": (
                    *existing.event_fingerprints,
                    fingerprint,
                ),
            }
            for key, value in usage.items():
                if value is not None:
                    update[key] = value
            record = existing.model_copy(update=update)

        if (
            record.runtime_duration_seconds is None
            and record.started_at is not None
            and record.completed_at is not None
        ):
            record = record.model_copy(
                update={
                    "runtime_duration_seconds": max(
                        0.0,
                        record.completed_at - record.started_at,
                    )
                }
            )
        record = self._emit_terminal_evidence(record)
        persisted = self.store.upsert(record)
        if self.observation_notifier is not None:
            try:
                self.observation_notifier(persisted)
            except Exception:
                # Usage projections are observational and must not break
                # canonical runtime telemetry persistence.
                pass
        return persisted
