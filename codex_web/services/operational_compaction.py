from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from codex_web.identity import AuthenticationActor
from codex_web.models import BotBinding, BotReplyTarget
from codex_web.services.identity import IdentityService
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.state_store import StateStore


OPERATIONAL_COMPACTION_VERSION = "1.0"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


class OperationalStoreInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store: str
    count: int | None = None
    count_exact: bool = True
    bytes: int | None = None
    oldest_at: float | None = None
    newest_at: float | None = None
    warning: bool = False
    reason_code: str = "within_threshold"
    details: dict[str, Any] = Field(default_factory=dict)


class OperationalInspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = OPERATIONAL_COMPACTION_VERSION
    generated_at: float
    stores: tuple[OperationalStoreInspection, ...]


class DeliveryTargetCompactionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: str = OPERATIONAL_COMPACTION_VERSION
    source_revision: float | None
    source_count: int
    retained_count: int
    removed_count: int
    canonical_upsert_count: int
    preserved_unattributed_count: int
    estimated_removed_bytes: int
    delete_keys: tuple[str, ...]
    upserts: dict[str, BotReplyTarget]
    source_checksum: str
    result_checksum: str
    generated_at: float


class OperationalCompactionExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    plan_id: str
    actor_identity_id: str
    status: str
    backup_ref: str
    source_count: int
    retained_count: int
    removed_count: int
    source_checksum: str
    result_checksum: str
    started_at: float
    completed_at: float | None = None
    error_code: str | None = None
    rollback_instructions: str


class OperationalCompactionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = OPERATIONAL_COMPACTION_VERSION
    executions: list[OperationalCompactionExecution] = Field(
        default_factory=list
    )


class OperationalCompactionError(RuntimeError):
    pass


class OperationalCompactionStale(OperationalCompactionError):
    pass


class OperationalCompactionService:
    STATE_NAMESPACE = "operational_compaction"
    DELIVERY_NAMESPACE = "bot_delivery_targets"

    def __init__(
        self,
        *,
        state_store: StateStore,
        delivery_targets: ModelMapRepository[BotReplyTarget],
        reply_targets: ModelMapRepository[BotReplyTarget],
        turn_queues: Any,
        load_bindings: Callable[[], list[BotBinding]],
        event_journal: Path,
        backup_directory: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_store = state_store
        self.delivery_targets = delivery_targets
        self.reply_targets = reply_targets
        self.turn_queues = turn_queues
        self.load_bindings = load_bindings
        self.event_journal = event_journal
        self.backup_directory = backup_directory
        self.clock = clock
        self._mutation_lock = threading.RLock()

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        IdentityService.require_admin(actor)

    @staticmethod
    def _target_identity(
        target: BotReplyTarget,
    ) -> tuple[str, str, str]:
        return (
            target.provider.casefold(),
            target.external_conversation_id,
            target.thread_id,
        )

    @staticmethod
    def _binding_identity(
        binding: BotBinding,
    ) -> tuple[str, str, str]:
        return (
            binding.provider.casefold(),
            binding.external_conversation_id,
            binding.thread_id,
        )

    @staticmethod
    def _canonical_keys(binding: BotBinding) -> tuple[str, str]:
        provider = binding.provider.casefold()
        conversation = binding.external_conversation_id
        thread_id = binding.thread_id
        return (
            f"{provider}:{conversation}:{thread_id}",
            f"thread:{thread_id}:{provider}:{conversation}",
        )

    @staticmethod
    def _external_keys(target: BotReplyTarget) -> tuple[str, ...]:
        values = {
            str(value)
            for value in (
                target.external_thread_id,
                target.message_id,
            )
            if value
        }
        return tuple(
            sorted(
                f"{target.provider.casefold()}:"
                f"{target.external_conversation_id}:external:{value}"
                for value in values
            )
        )

    def inspect_bounded(self) -> OperationalInspectionReport:
        now = float(self.clock())
        event_bytes = 0
        event_mtime: float | None = None
        try:
            stat = self.event_journal.stat()
            event_bytes = int(stat.st_size)
            event_mtime = float(stat.st_mtime)
        except FileNotFoundError:
            pass

        manifest_path = self.event_journal.with_suffix(
            self.event_journal.suffix + ".manifest.json"
        )
        segment_count: int | None = None
        retained_bytes: int | None = None
        try:
            raw_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            raw_segments = raw_manifest.get("segments")
            if isinstance(raw_segments, list):
                segment_count = len(raw_segments)
                retained_bytes = event_bytes + sum(
                    int(item.get("bytes") or 0)
                    for item in raw_segments
                    if isinstance(item, dict)
                )
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass

        delivery_count = self.delivery_targets.count()
        reply_count = self.reply_targets.count()
        queue_threads = self.turn_queues.store.record_count(
            self.turn_queues.namespace
        )

        stores = (
            OperationalStoreInspection(
                store="bot_event_journal",
                count=None,
                count_exact=False,
                bytes=event_bytes,
                newest_at=event_mtime,
                warning=event_bytes >= 128 * 1024 * 1024,
                reason_code=(
                    "event_journal_large"
                    if event_bytes >= 128 * 1024 * 1024
                    else "within_threshold"
                ),
                details={
                    "inspection": "metadata_only",
                    "full_scan_performed": False,
                    "segment_count": segment_count,
                    "total_retained_bytes": retained_bytes,
                },
            ),
            OperationalStoreInspection(
                store="bot_delivery_targets",
                count=delivery_count,
                warning=delivery_count >= 25_000,
                reason_code=(
                    "delivery_target_registry_large"
                    if delivery_count >= 25_000
                    else "within_threshold"
                ),
            ),
            OperationalStoreInspection(
                store="bot_reply_targets",
                count=reply_count,
                warning=reply_count >= 25_000,
                reason_code=(
                    "reply_target_registry_large"
                    if reply_count >= 25_000
                    else "within_threshold"
                ),
            ),
            OperationalStoreInspection(
                store="turn_queues",
                count=queue_threads,
                warning=queue_threads >= 5_000,
                reason_code=(
                    "turn_queue_registry_large"
                    if queue_threads >= 5_000
                    else "within_threshold"
                ),
                details={"unit": "thread_queues"},
            ),
        )
        return OperationalInspectionReport(
            generated_at=now,
            stores=stores,
        )

    def _delivery_records(self) -> dict[str, BotReplyTarget]:
        result: dict[str, BotReplyTarget] = {}
        cursor: str | None = None
        while True:
            page, next_cursor = self.delivery_targets.page(
                after=cursor,
                limit=1000,
            )
            result.update(page)
            if next_cursor is None:
                break
            cursor = next_cursor
        return result

    @staticmethod
    def _serialized_records(
        values: dict[str, BotReplyTarget],
    ) -> dict[str, Any]:
        return {
            key: value.model_dump(mode="json")
            for key, value in sorted(values.items())
        }

    def plan_delivery_target_compaction(
        self,
        *,
        actor: AuthenticationActor,
    ) -> DeliveryTargetCompactionPlan:
        self._require_admin(actor)
        with self._mutation_lock:
            source_revision = self.state_store.namespace_revision(
                self.DELIVERY_NAMESPACE
            )
            records = self._delivery_records()
            bindings = self.load_bindings()
            bindings_by_identity = {
                self._binding_identity(binding): binding
                for binding in bindings
            }
            candidate_keys: dict[
                tuple[str, str, str],
                list[str],
            ] = {}
            for key, target in records.items():
                candidate_keys.setdefault(
                    self._target_identity(target),
                    [],
                ).append(key)

            deletes: set[str] = set()
            upserts: dict[str, BotReplyTarget] = {}
            attributed_keys: set[str] = set()

            for identity, binding in sorted(
                bindings_by_identity.items(),
                key=lambda item: item[0],
            ):
                keys = candidate_keys.get(identity, [])
                if not keys:
                    continue
                attributed_keys.update(keys)
                latest_key = max(
                    keys,
                    key=lambda key: (
                        records[key].updated_at,
                        key,
                    ),
                )
                latest = records[latest_key]
                canonical_keys = self._canonical_keys(binding)
                external_keep = set(self._external_keys(latest))
                for key in canonical_keys:
                    if records.get(key) != latest:
                        upserts[key] = latest
                for key in external_keep:
                    if records.get(key) != latest:
                        upserts[key] = latest

                external_prefix = (
                    f"{binding.provider.casefold()}:"
                    f"{binding.external_conversation_id}:external:"
                )
                for key in keys:
                    if (
                        key.startswith(external_prefix)
                        and key not in external_keep
                        and records[key].updated_at <= latest.updated_at
                    ):
                        deletes.add(key)

            result = dict(records)
            for key in deletes:
                result.pop(key, None)
            result.update(upserts)
            source_payload = self._serialized_records(records)
            result_payload = self._serialized_records(result)
            removed_bytes = sum(
                len(
                    json.dumps(
                        {
                            "key": key,
                            "value": records[key].model_dump(mode="json"),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                for key in deletes
            )
            plan_basis = {
                "source_revision": source_revision,
                "source_checksum": _digest(source_payload),
                "result_checksum": _digest(result_payload),
                "delete_keys": sorted(deletes),
                "upserts": {
                    key: value.model_dump(mode="json")
                    for key, value in sorted(upserts.items())
                },
            }
            return DeliveryTargetCompactionPlan(
                id=f"delivery-target-compaction-{_digest(plan_basis)[:24]}",
                source_revision=source_revision,
                source_count=len(records),
                retained_count=len(result),
                removed_count=len(deletes),
                canonical_upsert_count=len(upserts),
                preserved_unattributed_count=len(
                    set(records) - attributed_keys
                ),
                estimated_removed_bytes=removed_bytes,
                delete_keys=tuple(sorted(deletes)),
                upserts=upserts,
                source_checksum=plan_basis["source_checksum"],
                result_checksum=plan_basis["result_checksum"],
                generated_at=float(self.clock()),
            )

    def _backup_path(self, backup_id: str) -> Path:
        return self.backup_directory / f"{backup_id}.json"

    def _write_backup(
        self,
        *,
        plan: DeliveryTargetCompactionPlan,
        records: dict[str, BotReplyTarget],
    ) -> str:
        self.backup_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.backup_directory, 0o700)
        backup_id = (
            f"delivery-targets-{int(self.clock())}-"
            f"{uuid.uuid4().hex[:12]}"
        )
        path = self._backup_path(backup_id)
        payload = {
            "version": OPERATIONAL_COMPACTION_VERSION,
            "backup_id": backup_id,
            "plan_id": plan.id,
            "namespace": self.DELIVERY_NAMESPACE,
            "source_checksum": plan.source_checksum,
            "records": self._serialized_records(records),
        }
        data = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        fd = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            directory_fd = os.open(
                self.backup_directory,
                os.O_RDONLY,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
        return f"file://{path.resolve()}"

    def _load_backup(self, backup_ref: str) -> dict[str, Any]:
        prefix = "file://"
        if not backup_ref.startswith(prefix):
            raise OperationalCompactionError(
                "unsupported compaction backup reference"
            )
        path = Path(backup_ref[len(prefix):]).resolve()
        root = self.backup_directory.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise OperationalCompactionError(
                "compaction backup reference escapes backup directory"
            ) from exc
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise OperationalCompactionError(
                "invalid compaction backup payload"
            )
        return raw

    def _save_execution(
        self,
        execution: OperationalCompactionExecution,
    ) -> None:
        def update(current: Any) -> dict[str, Any]:
            state = OperationalCompactionState.model_validate(
                current or {}
            )
            state.executions = [
                item
                for item in state.executions
                if item.id != execution.id
            ]
            state.executions.append(execution)
            state.executions = state.executions[-200:]
            return state.model_dump(mode="json")

        self.state_store.update(
            self.STATE_NAMESPACE,
            update,
            default={},
        )

    def apply_delivery_target_compaction(
        self,
        plan: DeliveryTargetCompactionPlan,
        *,
        actor: AuthenticationActor,
        fail_at: str | None = None,
    ) -> OperationalCompactionExecution:
        self._require_admin(actor)
        with self._mutation_lock:
            current_revision = self.state_store.namespace_revision(
                self.DELIVERY_NAMESPACE
            )
            if current_revision != plan.source_revision:
                raise OperationalCompactionStale(
                    "delivery-target state changed; create a new compaction plan"
                )
            records = self._delivery_records()
            if _digest(self._serialized_records(records)) != plan.source_checksum:
                raise OperationalCompactionStale(
                    "delivery-target state changed; create a new compaction plan"
                )

            backup_ref = self._write_backup(
                plan=plan,
                records=records,
            )
            execution = OperationalCompactionExecution(
                id=f"operational-compaction-{uuid.uuid4().hex}",
                plan_id=plan.id,
                actor_identity_id=actor.identity_id,
                status="backup_created",
                backup_ref=backup_ref,
                source_count=plan.source_count,
                retained_count=plan.retained_count,
                removed_count=plan.removed_count,
                source_checksum=plan.source_checksum,
                result_checksum=plan.result_checksum,
                started_at=float(self.clock()),
                rollback_instructions=(
                    "Quiesce bot runtime, then POST the execution backup_ref "
                    "to the operational-state restore endpoint."
                ),
            )
            self._save_execution(execution)
            if fail_at == "after_backup":
                raise OperationalCompactionError(
                    "injected interruption after backup"
                )

            self.state_store.record_apply(
                self.DELIVERY_NAMESPACE,
                upserts={
                    key: value.model_dump(mode="json")
                    for key, value in plan.upserts.items()
                },
                deletes=plan.delete_keys,
            )
            execution.status = "canonical_replaced"
            self._save_execution(execution)
            if fail_at == "after_replace":
                raise OperationalCompactionError(
                    "injected interruption after canonical replacement"
                )

            verified = self._delivery_records()
            verified_checksum = _digest(
                self._serialized_records(verified)
            )
            if verified_checksum != plan.result_checksum:
                execution.status = "verification_failed"
                execution.error_code = "result_checksum_mismatch"
                self._save_execution(execution)
                raise OperationalCompactionError(
                    "post-compaction checksum verification failed"
                )

            self.delivery_targets.flush_legacy_mirror()
            if fail_at == "after_mirror":
                raise OperationalCompactionError(
                    "injected interruption after compatibility mirror"
                )

            execution.status = "completed"
            execution.completed_at = float(self.clock())
            self._save_execution(execution)
            return execution

    def restore_delivery_target_backup(
        self,
        backup_ref: str,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        self._require_admin(actor)
        with self._mutation_lock:
            raw = self._load_backup(backup_ref)
            if raw.get("namespace") != self.DELIVERY_NAMESPACE:
                raise OperationalCompactionError(
                    "backup namespace is not bot_delivery_targets"
                )
            records = raw.get("records")
            if not isinstance(records, dict):
                raise OperationalCompactionError(
                    "backup records are missing"
                )
            validated = {
                str(key): BotReplyTarget.model_validate(value)
                for key, value in records.items()
            }
            checksum = _digest(
                self._serialized_records(validated)
            )
            if checksum != raw.get("source_checksum"):
                raise OperationalCompactionError(
                    "backup checksum verification failed"
                )
            self.state_store.record_replace(
                self.DELIVERY_NAMESPACE,
                {
                    key: value.model_dump(mode="json")
                    for key, value in validated.items()
                },
            )
            self.delivery_targets.flush_legacy_mirror()
            restored = self._delivery_records()
            restored_checksum = _digest(
                self._serialized_records(restored)
            )
            if restored_checksum != checksum:
                raise OperationalCompactionError(
                    "restored delivery-target checksum verification failed"
                )
            return {
                "restored": True,
                "count": len(restored),
                "checksum": restored_checksum,
                "backup_ref": backup_ref,
            }

    def executions(
        self,
        *,
        actor: AuthenticationActor,
    ) -> tuple[OperationalCompactionExecution, ...]:
        self._require_admin(actor)
        state = OperationalCompactionState.model_validate(
            self.state_store.get(self.STATE_NAMESPACE) or {}
        )
        return tuple(
            sorted(
                state.executions,
                key=lambda item: (
                    item.started_at,
                    item.id,
                ),
                reverse=True,
            )
        )
