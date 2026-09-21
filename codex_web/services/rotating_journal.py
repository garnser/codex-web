from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field


class JournalSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    sequence: int = 0
    path: str
    tail_path: str | None = None
    rotated_at: float
    indexed: bool = False
    compressed: bool = False
    bytes: int = 0
    source_bytes: int = 0
    event_count: int = 0
    protected_records: int = 0
    oldest_event_at: float | None = None
    newest_event_at: float | None = None
    sha256: str | None = None


class JournalManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    active_started_at: float | None = None
    segments: list[JournalSegment] = Field(default_factory=list)
    rotation_count: int = 0
    rotation_failures: int = 0
    archive_count: int = 0
    archive_failures: int = 0
    cleanup_deleted_segments: int = 0
    cleanup_deleted_bytes: int = 0
    last_cleanup: list[dict[str, Any]] = Field(default_factory=list)
    last_rotation_at: float | None = None
    last_maintenance_at: float | None = None
    last_error_class: str | None = None
    last_error_at: float | None = None


class RotatingJsonlJournal:
    """Crash-safe segmented JSONL journal with bounded hot-tail reads."""

    TAIL_RECORDS = 300
    RECENT_READ_BUDGET_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        active_path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.active_path = Path(active_path)
        self.manifest_path = self.active_path.with_suffix(
            self.active_path.suffix + ".manifest.json"
        )
        self.segments_dir = self.active_path.parent / (
            self.active_path.stem + ".segments"
        )
        self.clock = clock
        self._lock = threading.RLock()
        self._maintenance_lock = threading.Lock()
        self._manifest = self._load_manifest()
        self._recent_metrics = self._empty_recent_metrics()
        self._rotation_requested = False
        self._ensure_active_metadata()

    @staticmethod
    def _empty_recent_metrics() -> dict[str, int]:
        return {
            "bytesRead": 0,
            "chunksRead": 0,
            "linesConsidered": 0,
            "validEvents": 0,
            "fileSize": 0,
        }

    @staticmethod
    def _env_float(
        name: str,
        default: float,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            value = float(os.environ.get(name) or str(default))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

    @staticmethod
    def _env_int(
        name: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(os.environ.get(name) or str(default))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().casefold() in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
        }

    @classmethod
    def rotate_bytes(cls) -> int:
        return cls._env_int(
            "CODEX_WEB_BOT_EVENT_ROTATE_BYTES",
            64 * 1024 * 1024,
            minimum=1024 * 1024,
            maximum=4 * 1024 * 1024 * 1024,
        )

    @classmethod
    def rotate_seconds(cls) -> float:
        return cls._env_float(
            "CODEX_WEB_BOT_EVENT_ROTATE_SECONDS",
            24 * 3600,
            minimum=60.0,
            maximum=30 * 24 * 3600.0,
        )

    @classmethod
    def maintenance_interval_seconds(cls) -> float:
        return cls._env_float(
            "CODEX_WEB_BOT_EVENT_MAINTENANCE_SECONDS",
            30.0,
            minimum=5.0,
            maximum=3600.0,
        )

    @classmethod
    def retention_segments(cls) -> int:
        return cls._env_int(
            "CODEX_WEB_BOT_EVENT_RETENTION_SEGMENTS",
            20,
            minimum=2,
            maximum=10000,
        )

    @classmethod
    def minimum_segments(cls) -> int:
        return cls._env_int(
            "CODEX_WEB_BOT_EVENT_MIN_SEGMENTS",
            2,
            minimum=1,
            maximum=100,
        )

    @classmethod
    def retention_seconds(cls) -> float:
        days = cls._env_float(
            "CODEX_WEB_BOT_EVENT_RETENTION_DAYS",
            14.0,
            minimum=1.0,
            maximum=3650.0,
        )
        return days * 24 * 3600.0

    @classmethod
    def protected_retention_seconds(cls) -> float | None:
        days = cls._env_float(
            "CODEX_WEB_BOT_EVENT_PROTECTED_RETENTION_DAYS",
            0.0,
            minimum=0.0,
            maximum=3650.0,
        )
        if days <= 0:
            return None
        return max(30.0, days) * 24 * 3600.0

    @classmethod
    def minimum_retention_seconds(cls) -> float:
        return cls._env_float(
            "CODEX_WEB_BOT_EVENT_MIN_RETENTION_SECONDS",
            3600.0,
            minimum=300.0,
            maximum=30 * 24 * 3600.0,
        )

    @classmethod
    def retention_bytes(cls) -> int:
        return cls._env_int(
            "CODEX_WEB_BOT_EVENT_RETENTION_BYTES",
            1024 * 1024 * 1024,
            minimum=16 * 1024 * 1024,
            maximum=64 * 1024 * 1024 * 1024,
        )

    @classmethod
    def compression_enabled(cls) -> bool:
        return cls._env_bool(
            "CODEX_WEB_BOT_EVENT_COMPRESS",
            False,
        )

    @classmethod
    def hot_uncompressed_segments(cls) -> int:
        return cls._env_int(
            "CODEX_WEB_BOT_EVENT_HOT_SEGMENTS",
            1,
            minimum=1,
            maximum=20,
        )

    @classmethod
    def archive_max_segments_per_cycle(cls) -> int:
        return cls._env_int(
            "CODEX_WEB_BOT_EVENT_ARCHIVE_MAX_SEGMENTS_PER_CYCLE",
            1,
            minimum=1,
            maximum=20,
        )

    def _load_manifest(self) -> JournalManifest:
        try:
            raw = json.loads(
                self.manifest_path.read_text(encoding="utf-8")
            )
            if isinstance(raw, dict):
                return JournalManifest.model_validate(raw)
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
            pass
        return JournalManifest()

    def _ensure_active_metadata(self) -> None:
        now = float(self.clock())
        if self._manifest.active_started_at is None:
            try:
                self._manifest.active_started_at = float(
                    self.active_path.stat().st_mtime
                )
            except FileNotFoundError:
                self._manifest.active_started_at = now

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_manifest_locked(self) -> None:
        self.active_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        payload = (
            json.dumps(
                self._manifest.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        temp = self.manifest_path.with_name(
            self.manifest_path.name
            + f".{uuid.uuid4().hex}.tmp"
        )
        fd = os.open(
            temp,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.manifest_path)
            self._fsync_directory(self.manifest_path.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    @staticmethod
    def is_protected_event(value: dict[str, Any]) -> bool:
        retention_class = str(
            value.get("retention_class") or ""
        ).strip().casefold()
        if retention_class in {
            "audit",
            "canonical",
            "non_reconstructible",
            "protected",
        }:
            return True
        if bool(value.get("audit_required")):
            return True
        if bool(value.get("non_reconstructible")):
            return True
        event_type = str(value.get("type") or "").casefold()
        return event_type.startswith(
            ("audit.", "audit_", "security.", "security_")
        )

    def append(self, value: dict[str, Any]) -> None:
        encoded = (
            json.dumps(
                value,
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        ).encode("utf-8")
        with self._lock:
            self.active_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            fd = os.open(
                self.active_path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            try:
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            finally:
                os.close(fd)

            try:
                size = int(self.active_path.stat().st_size)
            except FileNotFoundError:
                size = 0
            age = max(
                0.0,
                float(self.clock())
                - float(
                    self._manifest.active_started_at
                    or self.clock()
                ),
            )
            if (
                size >= self.rotate_bytes()
                or age >= self.rotate_seconds()
            ):
                self._rotation_requested = True

    def _segment_id(self, now: float) -> str:
        return (
            f"segment-{int(now * 1000):013d}-"
            f"{uuid.uuid4().hex[:12]}"
        )

    @staticmethod
    def _segment_id_from_name(name: str) -> str | None:
        for suffix in (
            ".tail.jsonl",
            ".jsonl.gz",
            ".jsonl",
        ):
            if name.endswith(suffix):
                value = name[: -len(suffix)]
                return value if value.startswith("segment-") else None
        return None

    def _discover_segments_locked(self) -> None:
        self.segments_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.segments_dir, 0o700)
        discovered: dict[str, dict[str, Path]] = {}
        for path in self.segments_dir.iterdir():
            if not path.is_file():
                continue
            segment_id = self._segment_id_from_name(path.name)
            if segment_id is None:
                continue
            slot = discovered.setdefault(segment_id, {})
            if path.name.endswith(".tail.jsonl"):
                slot["tail"] = path
            elif path.name.endswith(".jsonl.gz"):
                slot["gzip"] = path
            elif path.name.endswith(".jsonl"):
                slot["plain"] = path

        normalized_existing: list[JournalSegment] = []
        next_sequence = 1
        for segment in self._manifest.segments:
            sequence = int(segment.sequence or 0)
            if sequence <= 0:
                sequence = next_sequence
                segment = segment.model_copy(
                    update={"sequence": sequence}
                )
            next_sequence = max(next_sequence, sequence + 1)
            normalized_existing.append(segment)
        by_id = {
            segment.id: segment
            for segment in normalized_existing
        }
        changed = normalized_existing != self._manifest.segments
        reconciled: list[JournalSegment] = []
        existing_ids = [
            segment.id
            for segment in sorted(
                normalized_existing,
                key=lambda item: item.sequence,
            )
        ]
        new_ids = sorted(
            set(discovered) - set(by_id),
            key=lambda segment_id: (
                (
                    discovered[segment_id].get("plain")
                    or discovered[segment_id].get("gzip")
                ).stat().st_mtime
                if (
                    discovered[segment_id].get("plain")
                    or discovered[segment_id].get("gzip")
                ) is not None
                else 0.0,
                segment_id,
            ),
        )
        all_ids = [*existing_ids, *new_ids]
        for segment_id in all_ids:
            current = by_id.get(segment_id)
            files = discovered.get(segment_id, {})
            plain = files.get("plain")
            zipped = files.get("gzip")
            tail = files.get("tail")
            if plain is None and zipped is None:
                changed = True
                continue

            preferred = (
                zipped
                if current is not None
                and current.compressed
                and zipped is not None
                else plain or zipped
            )
            assert preferred is not None
            compressed = preferred.name.endswith(".gz")
            if current is None:
                stat = preferred.stat()
                current = JournalSegment(
                    id=segment_id,
                    sequence=next_sequence,
                    path=preferred.name,
                    tail_path=(
                        tail.name if tail is not None else None
                    ),
                    rotated_at=float(stat.st_mtime),
                    compressed=compressed,
                    bytes=int(stat.st_size),
                    source_bytes=(
                        0 if compressed else int(stat.st_size)
                    ),
                )
                next_sequence += 1
                changed = True
            else:
                updates: dict[str, Any] = {}
                if current.path != preferred.name:
                    updates["path"] = preferred.name
                if current.compressed != compressed:
                    updates["compressed"] = compressed
                expected_tail = tail.name if tail is not None else None
                if current.tail_path != expected_tail:
                    updates["tail_path"] = expected_tail
                if updates:
                    current = current.model_copy(update=updates)
                    changed = True
            reconciled.append(current)

        reconciled.sort(key=lambda item: item.sequence)
        if reconciled != self._manifest.segments:
            self._manifest.segments = reconciled
            changed = True
        if changed:
            self._write_manifest_locked()

        # A crash after committing compressed metadata but before deleting the
        # source can leave both files. The manifest is authoritative.
        for segment in self._manifest.segments:
            files = discovered.get(segment.id, {})
            plain = files.get("plain")
            zipped = files.get("gzip")
            tail = files.get("tail")
            if segment.compressed:
                if (
                    plain is not None
                    and zipped is not None
                    and tail is not None
                    and segment.path == zipped.name
                ):
                    with contextlib.suppress(OSError):
                        plain.unlink()
                continue

            # If archive files were committed before a crash but the manifest
            # still points at the plain source, the plain source is
            # authoritative. Remove the uncommitted archive artifacts so
            # restart cannot accumulate ambiguous duplicate representations.
            if plain is not None and zipped is not None:
                with contextlib.suppress(OSError):
                    zipped.unlink()
                if tail is not None:
                    with contextlib.suppress(OSError):
                        tail.unlink()

    @staticmethod
    def _decode_event(raw: bytes) -> dict[str, Any] | None:
        if not raw.strip():
            return None
        try:
            value = json.loads(
                raw.decode("utf-8", errors="replace")
            )
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def _inspect_segment(
        self,
        segment: JournalSegment,
    ) -> JournalSegment:
        path = self.segments_dir / segment.path
        opener = gzip.open if path.name.endswith(".gz") else open
        digest = hashlib.sha256()
        event_count = 0
        protected = 0
        oldest: float | None = None
        newest: float | None = None
        tail: deque[bytes] = deque(maxlen=self.TAIL_RECORDS)
        source_bytes = 0
        with opener(path, "rb") as handle:
            for raw in handle:
                source_bytes += len(raw)
                digest.update(raw)
                value = self._decode_event(raw)
                if value is None:
                    continue
                event_count += 1
                tail.append(raw if raw.endswith(b"\n") else raw + b"\n")
                if self.is_protected_event(value):
                    protected += 1
                created_at = value.get("created_at")
                if isinstance(created_at, (int, float)):
                    created = float(created_at)
                    oldest = (
                        created
                        if oldest is None
                        else min(oldest, created)
                    )
                    newest = (
                        created
                        if newest is None
                        else max(newest, created)
                    )

        tail_path = segment.tail_path
        if path.name.endswith(".gz") and tail_path is None:
            tail_path = f"{segment.id}.tail.jsonl"
            self._write_private_file(
                self.segments_dir / tail_path,
                b"".join(tail),
            )

        return segment.model_copy(
            update={
                "indexed": True,
                "bytes": int(path.stat().st_size),
                "source_bytes": source_bytes,
                "event_count": event_count,
                "protected_records": protected,
                "oldest_event_at": oldest,
                "newest_event_at": newest,
                "sha256": digest.hexdigest(),
                "tail_path": tail_path,
            }
        )

    @staticmethod
    def _write_private_file(path: Path, data: bytes) -> None:
        temp = path.with_name(
            path.name + f".{uuid.uuid4().hex}.tmp"
        )
        fd = os.open(
            temp,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            RotatingJsonlJournal._fsync_directory(path.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    def _index_unindexed_segments(self) -> None:
        with self._lock:
            pending = [
                segment.model_copy(deep=True)
                for segment in self._manifest.segments
                if not segment.indexed
            ]
        for segment in pending:
            indexed = self._inspect_segment(segment)
            with self._lock:
                self._manifest.segments = [
                    indexed if item.id == indexed.id else item
                    for item in self._manifest.segments
                ]
                self._write_manifest_locked()

    def _active_needs_rotation_locked(self) -> bool:
        try:
            stat = self.active_path.stat()
        except FileNotFoundError:
            return False
        if stat.st_size <= 0:
            return False
        age = max(
            0.0,
            float(self.clock())
            - float(
                self._manifest.active_started_at
                or stat.st_mtime
            ),
        )
        return bool(
            self._rotation_requested
            or stat.st_size >= self.rotate_bytes()
            or age >= self.rotate_seconds()
        )

    def rotate_if_needed(
        self,
        *,
        force: bool = False,
        fail_at: str | None = None,
    ) -> JournalSegment | None:
        now = float(self.clock())
        try:
            with self._lock:
                self._discover_segments_locked()
                if not force and not self._active_needs_rotation_locked():
                    return None
                try:
                    stat = self.active_path.stat()
                except FileNotFoundError:
                    return None
                if stat.st_size <= 0:
                    self._manifest.active_started_at = now
                    self._rotation_requested = False
                    self._write_manifest_locked()
                    return None

                self.segments_dir.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                os.chmod(self.segments_dir, 0o700)
                segment_id = self._segment_id(now)
                destination = (
                    self.segments_dir / f"{segment_id}.jsonl"
                )
                with self.active_path.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.replace(self.active_path, destination)
                self._fsync_directory(self.active_path.parent)
                if fail_at == "after_rename":
                    raise RuntimeError(
                        "injected journal rotation interruption after rename"
                    )

                fd = os.open(
                    self.active_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self._fsync_directory(self.active_path.parent)

                segment = JournalSegment(
                    id=segment_id,
                    sequence=(
                        max(
                            (
                                item.sequence
                                for item in self._manifest.segments
                            ),
                            default=0,
                        )
                        + 1
                    ),
                    path=destination.name,
                    rotated_at=now,
                    bytes=int(destination.stat().st_size),
                    source_bytes=int(destination.stat().st_size),
                )
                self._manifest.segments.append(segment)
                self._manifest.segments.sort(
                    key=lambda item: item.sequence
                )
                self._manifest.active_started_at = now
                self._manifest.rotation_count += 1
                self._manifest.last_rotation_at = now
                self._manifest.last_error_class = None
                self._rotation_requested = False
                self._write_manifest_locked()
                if fail_at == "after_manifest":
                    raise RuntimeError(
                        "injected journal rotation interruption after manifest"
                    )

            indexed = self._inspect_segment(segment)
            with self._lock:
                self._manifest.segments = [
                    indexed if item.id == indexed.id else item
                    for item in self._manifest.segments
                ]
                self._write_manifest_locked()
            return indexed
        except Exception as exc:
            with self._lock:
                # If rename happened before a new active file was created,
                # restore an empty active append target. Orphan discovery will
                # reconcile the closed segment on the next maintenance pass.
                if not self.active_path.exists():
                    self.active_path.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    fd = os.open(
                        self.active_path,
                        os.O_CREAT | os.O_WRONLY,
                        0o600,
                    )
                    os.close(fd)
                    self._manifest.active_started_at = float(
                        self.clock()
                    )
                    self._rotation_requested = False
                self._manifest.rotation_failures += 1
                self._manifest.last_error_class = type(exc).__name__
                self._manifest.last_error_at = float(self.clock())
                with contextlib.suppress(Exception):
                    self._discover_segments_locked()
                    self._write_manifest_locked()
            raise

    def _read_plain_tail(
        self,
        path: Path,
        requested: int,
        chunk_size: int,
        metrics: dict[str, int],
        read_budget: int,
    ) -> list[dict[str, Any]]:
        if requested <= 0 or not path.exists():
            return []
        events_reverse: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            carry = b""
            while position > 0 and len(events_reverse) < requested:
                remaining_budget = max(
                    0,
                    read_budget - metrics["bytesRead"],
                )
                if remaining_budget <= 0:
                    break
                read_size = min(
                    chunk_size,
                    position,
                    remaining_budget,
                )
                position -= read_size
                handle.seek(position)
                block = handle.read(read_size)
                metrics["bytesRead"] += len(block)
                metrics["chunksRead"] += 1
                data = block + carry
                parts = data.split(b"\n")
                if position > 0:
                    carry = parts[0]
                    complete = parts[1:]
                else:
                    carry = b""
                    complete = parts
                for raw in reversed(complete):
                    if len(events_reverse) >= requested:
                        break
                    if not raw.strip():
                        continue
                    metrics["linesConsidered"] += 1
                    value = self._decode_event(raw)
                    if value is not None:
                        events_reverse.append(value)
            if (
                position == 0
                and carry.strip()
                and len(events_reverse) < requested
            ):
                metrics["linesConsidered"] += 1
                value = self._decode_event(carry)
                if value is not None:
                    events_reverse.append(value)
        return list(reversed(events_reverse))

    def _read_compressed_tail_fallback(
        self,
        path: Path,
        requested: int,
        metrics: dict[str, int],
        read_budget: int,
    ) -> list[dict[str, Any]]:
        rows: deque[dict[str, Any]] = deque(maxlen=requested)
        with gzip.open(path, "rb") as handle:
            for raw in handle:
                if metrics["bytesRead"] >= read_budget:
                    break
                allowed = min(
                    len(raw),
                    read_budget - metrics["bytesRead"],
                )
                metrics["bytesRead"] += allowed
                if allowed < len(raw):
                    break
                metrics["linesConsidered"] += 1
                value = self._decode_event(raw)
                if value is not None:
                    rows.append(value)
        metrics["chunksRead"] += 1
        return list(rows)

    def _recent_sources_locked(self) -> list[tuple[Path, bool]]:
        sources: list[tuple[Path, bool]] = []
        if self.active_path.exists():
            sources.append((self.active_path, False))
        for segment in sorted(
            self._manifest.segments,
            key=lambda item: item.sequence,
            reverse=True,
        ):
            path = self.segments_dir / segment.path
            if segment.compressed:
                if segment.tail_path:
                    tail = self.segments_dir / segment.tail_path
                    if tail.exists():
                        sources.append((tail, False))
                        continue
                plain_fallback = (
                    self.segments_dir / f"{segment.id}.jsonl"
                )
                if plain_fallback.exists():
                    sources.append((plain_fallback, False))
                elif path.exists():
                    sources.append((path, True))
            elif path.exists():
                sources.append((path, False))
        return sources

    def recent(
        self,
        limit: int = 80,
        *,
        chunk_size: int = 64 * 1024,
        max_bytes: int = RECENT_READ_BUDGET_BYTES,
    ) -> list[dict[str, Any]]:
        requested = max(1, min(int(limit), self.TAIL_RECORDS))
        chunk_size = max(1024, int(chunk_size))
        read_budget = max(
            chunk_size,
            min(int(max_bytes), 64 * 1024 * 1024),
        )
        metrics = self._empty_recent_metrics()
        try:
            metrics["fileSize"] = int(
                self.active_path.stat().st_size
            )
        except FileNotFoundError:
            pass

        chunks: list[list[dict[str, Any]]] = []
        remaining = requested
        # Keep rotation/archive/cleanup from replacing a selected path while
        # this bounded read is in progress. Append shares the same lock, so
        # the maximum writer pause is bounded by read_budget rather than by
        # total historical journal size.
        with self._lock:
            self._discover_segments_locked()
            sources = list(self._recent_sources_locked())
            for path, compressed in sources:
                if remaining <= 0:
                    break
                if compressed:
                    values = self._read_compressed_tail_fallback(
                        path,
                        remaining,
                        metrics,
                        read_budget,
                    )
                else:
                    values = self._read_plain_tail(
                        path,
                        remaining,
                        chunk_size,
                        metrics,
                        read_budget,
                    )
                if values:
                    chunks.append(values)
                    remaining -= len(values)

        result: list[dict[str, Any]] = []
        for values in reversed(chunks):
            result.extend(values)
        if len(result) > requested:
            result = result[-requested:]
        metrics["validEvents"] = len(result)
        self._recent_metrics = metrics
        return result

    def recent_metrics(self) -> dict[str, int]:
        return dict(self._recent_metrics)

    def _compress_segment(
        self,
        segment: JournalSegment,
        *,
        fail_at: str | None = None,
    ) -> JournalSegment:
        source = self.segments_dir / segment.path
        if segment.compressed or not source.exists():
            return segment
        gzip_path = self.segments_dir / f"{segment.id}.jsonl.gz"
        tail_path = self.segments_dir / f"{segment.id}.tail.jsonl"
        gzip_temp = gzip_path.with_name(
            gzip_path.name + f".{uuid.uuid4().hex}.tmp"
        )
        tail: deque[bytes] = deque(maxlen=self.TAIL_RECORDS)
        try:
            with source.open("rb") as incoming, gzip.open(
                gzip_temp,
                "wb",
                compresslevel=6,
            ) as outgoing:
                for raw in incoming:
                    outgoing.write(raw)
                    if self._decode_event(raw) is not None:
                        tail.append(
                            raw if raw.endswith(b"\n") else raw + b"\n"
                        )
            with gzip_temp.open("rb") as handle:
                os.fsync(handle.fileno())
            self._write_private_file(
                tail_path,
                b"".join(tail),
            )
            os.replace(gzip_temp, gzip_path)
            self._fsync_directory(self.segments_dir)
            if fail_at == "after_archive_files":
                raise RuntimeError(
                    "injected journal archive interruption after archive files"
                )

            updated = segment.model_copy(
                update={
                    "path": gzip_path.name,
                    "tail_path": tail_path.name,
                    "compressed": True,
                    "bytes": int(gzip_path.stat().st_size),
                }
            )
            with self._lock:
                self._manifest.segments = [
                    updated if item.id == updated.id else item
                    for item in self._manifest.segments
                ]
                self._manifest.archive_count += 1
                self._manifest.last_error_class = None
                self._write_manifest_locked()
            if fail_at == "after_archive_manifest":
                raise RuntimeError(
                    "injected journal archive interruption after archive manifest"
                )

            source.unlink()
            self._fsync_directory(self.segments_dir)
            return updated
        except Exception as exc:
            with contextlib.suppress(FileNotFoundError):
                gzip_temp.unlink()
            with self._lock:
                self._manifest.archive_failures += 1
                self._manifest.last_error_class = type(exc).__name__
                self._manifest.last_error_at = float(self.clock())
                self._write_manifest_locked()
            raise

    def _archive_segments(self) -> list[str]:
        if not self.compression_enabled():
            return []
        with self._lock:
            values = [
                item.model_copy(deep=True)
                for item in sorted(
                    self._manifest.segments,
                    key=lambda segment: segment.sequence,
                    reverse=True,
                )
            ]
        candidates = [
            item
            for item in values[
                self.hot_uncompressed_segments():
            ]
            if item.indexed and not item.compressed
        ]
        archived: list[str] = []
        for segment in candidates[
            : self.archive_max_segments_per_cycle()
        ]:
            self._compress_segment(segment)
            archived.append(segment.id)
        return archived

    def _segment_can_delete(
        self,
        segment: JournalSegment,
        *,
        now: float,
    ) -> bool:
        age = max(0.0, now - segment.rotated_at)
        if age < self.minimum_retention_seconds():
            return False
        if segment.protected_records <= 0:
            return True
        protected_retention = self.protected_retention_seconds()
        if protected_retention is None:
            return False
        return age >= protected_retention

    def _delete_segment_files(self, segment: JournalSegment) -> int:
        removed = 0
        names = {
            segment.path,
            segment.tail_path,
            f"{segment.id}.jsonl",
            f"{segment.id}.jsonl.gz",
            f"{segment.id}.tail.jsonl",
        }
        for name in names:
            if not name:
                continue
            path = self.segments_dir / name
            try:
                removed += int(path.stat().st_size)
                path.unlink()
            except FileNotFoundError:
                continue
        self._fsync_directory(self.segments_dir)
        return removed

    def _cleanup_retention(self) -> list[dict[str, Any]]:
        now = float(self.clock())
        with self._lock:
            segments = [
                item.model_copy(deep=True)
                for item in sorted(
                    self._manifest.segments,
                    key=lambda segment: segment.sequence,
                )
            ]
        try:
            active_bytes = int(self.active_path.stat().st_size)
        except FileNotFoundError:
            active_bytes = 0
        total_bytes = active_bytes + sum(
            max(0, int(item.bytes))
            for item in segments
        )
        deleted: list[dict[str, Any]] = []
        remaining = list(segments)
        for segment in segments:
            age = max(0.0, now - segment.rotated_at)
            over_age = age >= self.retention_seconds()
            over_count = len(remaining) > self.retention_segments()
            over_bytes = total_bytes > self.retention_bytes()
            if not (over_age or over_count or over_bytes):
                continue
            if len(remaining) <= self.minimum_segments():
                break
            if not segment.indexed:
                continue
            if not self._segment_can_delete(segment, now=now):
                continue

            with self._lock:
                if not any(
                    item.id == segment.id
                    for item in self._manifest.segments
                ):
                    continue
                removed_bytes = self._delete_segment_files(segment)
                total_bytes = max(0, total_bytes - removed_bytes)
                remaining = [
                    item
                    for item in remaining
                    if item.id != segment.id
                ]
                deleted.append(
                    {
                        "segmentId": segment.id,
                        "bytes": removed_bytes,
                        "protectedRecords": segment.protected_records,
                        "reason": (
                            "age"
                            if over_age
                            else "count"
                            if over_count
                            else "bytes"
                        ),
                    }
                )
                self._manifest.segments = [
                    item
                    for item in self._manifest.segments
                    if item.id != segment.id
                ]
                self._manifest.cleanup_deleted_segments += 1
                self._manifest.cleanup_deleted_bytes += removed_bytes
                self._manifest.last_cleanup = [
                    *self._manifest.last_cleanup,
                    deleted[-1],
                ][-20:]
                self._write_manifest_locked()
        return deleted

    def maintain(
        self,
        *,
        force_rotate: bool = False,
    ) -> dict[str, Any]:
        if not self._maintenance_lock.acquire(blocking=False):
            return {
                "coalesced": True,
                "status": self.status(),
            }
        try:
            rotated: str | None = None
            archived: list[str] = []
            deleted: list[dict[str, Any]] = []
            with self._lock:
                self._discover_segments_locked()
            segment = self.rotate_if_needed(force=force_rotate)
            if segment is not None:
                rotated = segment.id
            self._index_unindexed_segments()
            archived = self._archive_segments()
            deleted = self._cleanup_retention()
            with self._lock:
                self._manifest.last_maintenance_at = float(
                    self.clock()
                )
                self._manifest.last_error_class = None
                self._write_manifest_locked()
            return {
                "coalesced": False,
                "rotated": rotated,
                "archived": archived,
                "deleted": deleted,
                "status": self.status(),
            }
        except Exception as exc:
            with self._lock:
                self._manifest.last_error_class = type(exc).__name__
                self._manifest.last_error_at = float(self.clock())
                with contextlib.suppress(Exception):
                    self._write_manifest_locked()
            raise
        finally:
            self._maintenance_lock.release()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._discover_segments_locked()
            manifest = self._manifest.model_copy(deep=True)
        try:
            active_stat = self.active_path.stat()
            active_bytes = int(active_stat.st_size)
        except FileNotFoundError:
            active_bytes = 0
        segment_bytes = sum(
            max(0, int(item.bytes))
            for item in manifest.segments
        )
        oldest_candidates = [
            value
            for value in [
                manifest.active_started_at,
                *[
                    item.oldest_event_at or item.rotated_at
                    for item in manifest.segments
                ],
            ]
            if value is not None
        ]
        newest_candidates = [
            value
            for value in [
                manifest.last_rotation_at,
                *[
                    item.newest_event_at or item.rotated_at
                    for item in manifest.segments
                ],
            ]
            if value is not None
        ]
        newest_uncompressed = sorted(
            manifest.segments,
            key=lambda item: item.sequence,
            reverse=True,
        )
        archive_backlog = 0
        if self.compression_enabled():
            archive_backlog = sum(
                1
                for item in newest_uncompressed[
                    self.hot_uncompressed_segments():
                ]
                if item.indexed and not item.compressed
            )
        return {
            "activePath": str(self.active_path),
            "manifestPath": str(self.manifest_path),
            "segmentsDirectory": str(self.segments_dir),
            "activeSizeBytes": active_bytes,
            "segmentCount": len(manifest.segments),
            "totalRetainedBytes": active_bytes + segment_bytes,
            "oldestRetainedAt": (
                min(oldest_candidates)
                if oldest_candidates
                else None
            ),
            "newestRetainedAt": (
                max(newest_candidates)
                if newest_candidates
                else None
            ),
            "protectedSegments": sum(
                1
                for item in manifest.segments
                if item.protected_records > 0
            ),
            "protectedRecords": sum(
                item.protected_records
                for item in manifest.segments
            ),
            "archiveBacklog": archive_backlog,
            "rotationCount": manifest.rotation_count,
            "rotationFailures": manifest.rotation_failures,
            "archiveCount": manifest.archive_count,
            "archiveFailures": manifest.archive_failures,
            "cleanupDeletedSegments": (
                manifest.cleanup_deleted_segments
            ),
            "cleanupDeletedBytes": manifest.cleanup_deleted_bytes,
            "lastCleanup": list(manifest.last_cleanup),
            "lastRotationAt": manifest.last_rotation_at,
            "lastMaintenanceAt": manifest.last_maintenance_at,
            "lastErrorClass": manifest.last_error_class,
            "lastErrorAt": manifest.last_error_at,
            "rotationRequested": self._rotation_requested,
            "rotationBytes": self.rotate_bytes(),
            "rotationSeconds": self.rotate_seconds(),
            "retentionSegments": self.retention_segments(),
            "minimumSegments": self.minimum_segments(),
            "retentionSeconds": self.retention_seconds(),
            "retentionBytes": self.retention_bytes(),
            "compressionEnabled": self.compression_enabled(),
            "hotUncompressedSegments": (
                self.hot_uncompressed_segments()
            ),
        }

    async def run_maintenance_forever(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.maintain)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(
                self.maintenance_interval_seconds()
            )
