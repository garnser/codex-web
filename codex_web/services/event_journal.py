from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from codex_web.storage.json_files import atomic_write_text


JOURNAL_MANIFEST_VERSION = "1.0"
_SEGMENT_RE = re.compile(
    r"\.seg-(?P<sequence>\d{20})-(?P<closed_ms>\d{13})\.jsonl(?:\.gz)?$"
)


class JournalSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    sequence: int
    filename: str
    archive_filename: str | None = None
    created_at: float | None = None
    closed_at: float
    size_bytes: int
    event_count: int | None = None
    oldest_event_at: float | None = None
    newest_event_at: float | None = None
    protected_records: int | None = None
    indexed: bool = False
    compressed: bool = False
    archived: bool = False
    compression_bytes: int | None = None


class EventJournalManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = JOURNAL_MANIFEST_VERSION
    active_sequence: int = 1
    segments: list[JournalSegment] = Field(default_factory=list)
    last_rotation_at: float | None = None
    last_maintenance_at: float | None = None
    rotation_failures: int = 0
    archive_failures: int = 0
    cleanup_failures: int = 0
    cleanup_removed_segments: int = 0
    cleanup_removed_bytes: int = 0


class EventJournal:
    """Crash-safe segmented JSONL runtime journal with bounded active state."""

    def __init__(
        self,
        active_file: Path,
        *,
        archive_directory: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.active_file = active_file
        self.manifest_file = active_file.with_name(
            f"{active_file.name}.manifest.json"
        )
        self.archive_directory = archive_directory or active_file.parent / (
            f"{active_file.name}.archive"
        )
        self.clock = clock
        self._lock = threading.RLock()
        self._recent_metrics = self._empty_recent_metrics()
        self._maintenance_runs = 0
        self._maintenance_error_class: str | None = None
        self._active_created_at: float | None = None
        self._active_oldest_at: float | None = None
        self._active_newest_at: float | None = None
        self._active_protected_records = 0
        self._active_event_count = 0
        self._manifest = self._recover_manifest()
        self._initialize_active_state()

    @staticmethod
    def max_active_bytes() -> int:
        try:
            value = int(
                os.environ.get("CODEX_WEB_EVENT_JOURNAL_MAX_BYTES")
                or str(64 * 1024 * 1024)
            )
        except ValueError:
            value = 64 * 1024 * 1024
        return max(1 * 1024 * 1024, min(value, 4 * 1024 * 1024 * 1024))

    @staticmethod
    def max_active_age_seconds() -> float:
        try:
            value = float(
                os.environ.get("CODEX_WEB_EVENT_JOURNAL_MAX_AGE_SECONDS")
                or str(24 * 3600)
            )
        except ValueError:
            value = 24 * 3600.0
        return max(60.0, min(value, 30 * 24 * 3600.0))

    @staticmethod
    def maintenance_interval_seconds() -> float:
        try:
            value = float(
                os.environ.get("CODEX_WEB_EVENT_JOURNAL_MAINTENANCE_SECONDS")
                or "30"
            )
        except ValueError:
            value = 30.0
        return max(5.0, min(value, 3600.0))

    @staticmethod
    def hot_segments() -> int:
        try:
            value = int(
                os.environ.get("CODEX_WEB_EVENT_JOURNAL_HOT_SEGMENTS")
                or "1"
            )
        except ValueError:
            value = 1
        return max(1, min(value, 10))

    @staticmethod
    def retention_seconds() -> float:
        try:
            value = float(
                os.environ.get("CODEX_WEB_EVENT_JOURNAL_RETENTION_SECONDS")
                or str(30 * 24 * 3600)
            )
        except ValueError:
            value = 30 * 24 * 3600.0
        return max(24 * 3600.0, min(value, 3650 * 24 * 3600.0))

    @staticmethod
    def retention_max_segments() -> int:
        try:
            value = int(
                os.environ.get("CODEX_WEB_EVENT_JOURNAL_MAX_SEGMENTS")
                or "48"
            )
        except ValueError:
            value = 48
        return max(2, min(value, 10000))

    @staticmethod
    def retention_max_bytes() -> int:
        try:
            value = int(
                os.environ.get("CODEX_WEB_EVENT_JOURNAL_RETENTION_MAX_BYTES")
                or str(4 * 1024 * 1024 * 1024)
            )
        except ValueError:
            value = 4 * 1024 * 1024 * 1024
        minimum = EventJournal.max_active_bytes() * 2
        return max(minimum, min(value, 1024 * 1024 * 1024 * 1024))

    @staticmethod
    def compression_enabled() -> bool:
        raw = str(
            os.environ.get("CODEX_WEB_EVENT_JOURNAL_COMPRESS")
            or "true"
        ).strip().casefold()
        return raw not in {"0", "false", "no", "off"}

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
    def _protected_event(event: dict[str, Any]) -> bool:
        retention_class = str(
            event.get("retention_class")
            or event.get("retentionClass")
            or ""
        ).strip().casefold()
        if retention_class in {
            "audit",
            "protected",
            "non_reconstructible",
            "non-reconstructible",
        }:
            return True
        event_type = str(event.get("type") or "").strip().casefold()
        protected_prefixes = (
            "audit_",
            "audit.",
            "security_",
            "security.",
            "secret_",
            "secret.",
            "approval_",
            "approval.",
            "action_intent_",
            "action-intent.",
            "execution_authority_",
            "execution-authority.",
        )
        return event_type.startswith(protected_prefixes)

    def _load_manifest(self) -> EventJournalManifest | None:
        if not self.manifest_file.exists():
            return None
        try:
            raw = json.loads(
                self.manifest_file.read_text(encoding="utf-8")
            )
            return EventJournalManifest.model_validate(raw)
        except Exception:
            return None

    def _persist_manifest(self) -> None:
        atomic_write_text(
            self.manifest_file,
            json.dumps(
                self._manifest.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            private=True,
        )

    def _segment_name(
        self,
        sequence: int,
        closed_at: float,
    ) -> str:
        return (
            f"{self.active_file.name}.seg-{sequence:020d}-"
            f"{int(closed_at * 1000):013d}.jsonl"
        )

    @staticmethod
    def _parse_sequence(path: Path) -> int | None:
        match = _SEGMENT_RE.search(path.name)
        if match is None:
            return None
        return int(match.group("sequence"))

    def _discover_segment_files(
        self,
    ) -> dict[int, tuple[Path | None, Path | None]]:
        discovered: dict[int, tuple[Path | None, Path | None]] = {}
        for directory in (self.active_file.parent, self.archive_directory):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                sequence = self._parse_sequence(path)
                if sequence is None:
                    continue
                source, archived = discovered.get(
                    sequence,
                    (None, None),
                )
                if path.suffix == ".gz":
                    archived = path
                else:
                    source = path
                discovered[sequence] = (source, archived)
        return discovered

    def _recover_manifest(self) -> EventJournalManifest:
        manifest = self._load_manifest() or EventJournalManifest()
        known = {item.sequence: item for item in manifest.segments}
        discovered = self._discover_segment_files()
        now = float(self.clock())

        for sequence, (source, archived) in discovered.items():
            current = known.get(sequence)
            if current is None:
                path = source or archived
                assert path is not None
                current = JournalSegment(
                    id=f"segment-{sequence}",
                    sequence=sequence,
                    filename=(
                        source.name
                        if source is not None
                        else path.name.removesuffix(".gz")
                    ),
                    archive_filename=(
                        archived.name if archived is not None else None
                    ),
                    closed_at=float(path.stat().st_mtime),
                    size_bytes=int(path.stat().st_size),
                    indexed=False,
                    compressed=archived is not None,
                    archived=archived is not None,
                )
                manifest.segments.append(current)
                known[sequence] = current
            else:
                if source is not None:
                    current.filename = source.name
                    current.size_bytes = int(source.stat().st_size)
                if archived is not None:
                    current.archive_filename = archived.name
                    current.compressed = True
                    current.archived = True
                    current.compression_bytes = int(
                        archived.stat().st_size
                    )

        manifest.segments.sort(key=lambda item: item.sequence)
        max_sequence = max(
            [item.sequence for item in manifest.segments],
            default=0,
        )
        manifest.active_sequence = max(
            int(manifest.active_sequence),
            max_sequence + 1,
        )
        manifest.last_maintenance_at = (
            manifest.last_maintenance_at or now
        )
        self._manifest = manifest
        self._cleanup_stale_temporary_files()
        self._persist_manifest()
        return manifest

    def _cleanup_stale_temporary_files(self) -> None:
        for directory in (
            self.active_file.parent,
            self.archive_directory,
        ):
            if not directory.exists():
                continue
            for path in directory.glob("*.gz.tmp"):
                with contextlib.suppress(OSError):
                    path.unlink()

    def _initialize_active_state(self) -> None:
        self.active_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.active_file.exists():
            self.active_file.touch()
        stat = self.active_file.stat()
        self._active_created_at = float(stat.st_mtime)
        # Existing legacy files may contain millions of events. Do not scan
        # them during startup; exact metadata is computed after rotation in
        # the maintenance worker.
        self._active_event_count = 0
        self._active_oldest_at = None
        self._active_newest_at = None
        self._active_protected_records = 0

    def _active_size(self) -> int:
        try:
            return int(self.active_file.stat().st_size)
        except FileNotFoundError:
            return 0

    def _rotation_due(self, now: float | None = None) -> bool:
        current = float(self.clock()) if now is None else float(now)
        size_due = self._active_size() >= self.max_active_bytes()
        created = self._active_created_at
        age_due = bool(
            created is not None
            and self._active_size() > 0
            and current - created >= self.max_active_age_seconds()
        )
        return size_due or age_due

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _rotate_locked(self, now: float | None = None) -> JournalSegment | None:
        current = float(self.clock()) if now is None else float(now)
        if self._active_size() <= 0:
            self._active_created_at = current
            return None

        sequence = int(self._manifest.active_sequence)
        segment_name = self._segment_name(sequence, current)
        segment_path = self.active_file.parent / segment_name
        try:
            with self.active_file.open("ab") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(self.active_file, segment_path)
            self.active_file.touch()
            self._fsync_directory(self.active_file.parent)

            segment = JournalSegment(
                id=f"segment-{sequence}",
                sequence=sequence,
                filename=segment_name,
                created_at=self._active_created_at,
                closed_at=current,
                size_bytes=int(segment_path.stat().st_size),
                event_count=(
                    self._active_event_count
                    if self._active_oldest_at is not None
                    else None
                ),
                oldest_event_at=self._active_oldest_at,
                newest_event_at=self._active_newest_at,
                protected_records=(
                    self._active_protected_records
                    if self._active_oldest_at is not None
                    else None
                ),
                indexed=self._active_oldest_at is not None,
            )
            self._manifest.segments = [
                item
                for item in self._manifest.segments
                if item.sequence != sequence
            ]
            self._manifest.segments.append(segment)
            self._manifest.segments.sort(
                key=lambda item: item.sequence
            )
            self._manifest.active_sequence = sequence + 1
            self._manifest.last_rotation_at = current
            self._persist_manifest()

            self._active_created_at = current
            self._active_event_count = 0
            self._active_oldest_at = None
            self._active_newest_at = None
            self._active_protected_records = 0
            return segment
        except Exception:
            self._manifest.rotation_failures += 1
            with contextlib.suppress(Exception):
                self._persist_manifest()
            raise

    def rotate_if_due(self) -> JournalSegment | None:
        with self._lock:
            if not self._rotation_due():
                return None
            return self._rotate_locked()

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.active_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            if not self.active_file.exists():
                self.active_file.touch()
                self._active_created_at = float(self.clock())

            now = float(self.clock())
            if self._rotation_due(now):
                self._rotate_locked(now)

            sequence = int(self._manifest.active_sequence)
            offset = self._active_size()
            payload = {
                "created_at": now,
                "journal_cursor": f"{sequence}:{offset}",
                **event,
            }
            encoded = (
                json.dumps(
                    payload,
                    separators=(",", ":"),
                    default=str,
                )
                + "\n"
            ).encode("utf-8")
            with self.active_file.open("ab") as handle:
                handle.write(encoded)
                handle.flush()

            self._active_event_count += 1
            self._active_oldest_at = (
                now
                if self._active_oldest_at is None
                else self._active_oldest_at
            )
            self._active_newest_at = now
            if self._protected_event(payload):
                self._active_protected_records += 1

            if self._rotation_due(now):
                self._rotate_locked(now)
            return payload

    @staticmethod
    def _read_tail_reverse(
        path: Path,
        requested: int,
        *,
        chunk_size: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        metrics = {
            "bytesRead": 0,
            "chunksRead": 0,
            "linesConsidered": 0,
            "validEvents": 0,
            "fileSize": 0,
        }
        if requested <= 0 or not path.exists():
            return [], metrics

        result: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            metrics["fileSize"] = position
            carry = b""

            while position > 0 and len(result) < requested:
                read_size = min(chunk_size, position)
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
                    if len(result) >= requested:
                        break
                    if not raw.strip():
                        continue
                    metrics["linesConsidered"] += 1
                    with contextlib.suppress(Exception):
                        value = json.loads(
                            raw.decode(
                                "utf-8",
                                errors="replace",
                            )
                        )
                        if isinstance(value, dict):
                            result.append(value)

            if (
                position == 0
                and carry.strip()
                and len(result) < requested
            ):
                metrics["linesConsidered"] += 1
                with contextlib.suppress(Exception):
                    value = json.loads(
                        carry.decode("utf-8", errors="replace")
                    )
                    if isinstance(value, dict):
                        result.append(value)

        metrics["validEvents"] = len(result)
        return result, metrics

    def _hot_segment_paths(self) -> list[Path]:
        values = [
            item
            for item in sorted(
                self._manifest.segments,
                key=lambda item: item.sequence,
                reverse=True,
            )
            if not item.archived
        ]
        return [
            self.active_file.parent / item.filename
            for item in values[: self.hot_segments()]
            if (self.active_file.parent / item.filename).exists()
        ]

    def recent(
        self,
        limit: int = 80,
        *,
        chunk_size: int = 64 * 1024,
    ) -> list[dict[str, Any]]:
        requested = max(1, min(int(limit), 300))
        chunk_size = max(1024, int(chunk_size))
        metrics = self._empty_recent_metrics()
        values_reverse: list[dict[str, Any]] = []

        with self._lock:
            paths = [self.active_file, *self._hot_segment_paths()]
            for path in paths:
                if len(values_reverse) >= requested:
                    break
                remaining = requested - len(values_reverse)
                rows, item_metrics = self._read_tail_reverse(
                    path,
                    remaining,
                    chunk_size=chunk_size,
                )
                values_reverse.extend(rows)
                metrics["bytesRead"] += item_metrics["bytesRead"]
                metrics["chunksRead"] += item_metrics["chunksRead"]
                metrics["linesConsidered"] += item_metrics[
                    "linesConsidered"
                ]
                metrics["fileSize"] += item_metrics["fileSize"]

        metrics["validEvents"] = len(values_reverse)
        self._recent_metrics = metrics
        return list(reversed(values_reverse[:requested]))

    def recent_metrics(self) -> dict[str, int]:
        return dict(self._recent_metrics)

    def _index_segment(
        self,
        segment: JournalSegment,
    ) -> JournalSegment:
        path = self.active_file.parent / segment.filename
        if not path.exists():
            return segment

        count = 0
        oldest: float | None = None
        newest: float | None = None
        protected = 0
        with path.open("rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                with contextlib.suppress(Exception):
                    value = json.loads(
                        raw.decode("utf-8", errors="replace")
                    )
                    if not isinstance(value, dict):
                        continue
                    count += 1
                    created = value.get("created_at")
                    if isinstance(created, (int, float)):
                        created_value = float(created)
                        oldest = (
                            created_value
                            if oldest is None
                            else min(oldest, created_value)
                        )
                        newest = (
                            created_value
                            if newest is None
                            else max(newest, created_value)
                        )
                    if self._protected_event(value):
                        protected += 1

        segment.event_count = count
        segment.oldest_event_at = oldest
        segment.newest_event_at = newest
        segment.protected_records = protected
        segment.size_bytes = int(path.stat().st_size)
        segment.indexed = True
        return segment

    def _compress_and_archive(
        self,
        segment: JournalSegment,
    ) -> JournalSegment:
        source = self.active_file.parent / segment.filename
        if not source.exists():
            return segment
        self.archive_directory.mkdir(
            parents=True,
            exist_ok=True,
        )
        os.chmod(self.archive_directory, 0o700)
        archive_name = f"{segment.filename}.gz"
        target = self.archive_directory / archive_name
        temporary = target.with_name(f"{target.name}.tmp")

        try:
            with source.open("rb") as source_handle:
                with temporary.open("wb") as raw_output:
                    with gzip.GzipFile(
                        fileobj=raw_output,
                        mode="wb",
                        mtime=0,
                    ) as compressed:
                        while True:
                            block = source_handle.read(1024 * 1024)
                            if not block:
                                break
                            compressed.write(block)
                    raw_output.flush()
                    os.fsync(raw_output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            self._fsync_directory(self.archive_directory)
            source.unlink()
            self._fsync_directory(self.active_file.parent)

            segment.archive_filename = archive_name
            segment.compressed = True
            segment.archived = True
            segment.compression_bytes = int(target.stat().st_size)
            return segment
        except Exception:
            self._manifest.archive_failures += 1
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
            raise

    def _archive_backlog(self) -> list[JournalSegment]:
        hot_sequences = {
            item.sequence
            for item in sorted(
                self._manifest.segments,
                key=lambda item: item.sequence,
                reverse=True,
            )[: self.hot_segments()]
        }
        return [
            item
            for item in sorted(
                self._manifest.segments,
                key=lambda item: item.sequence,
            )
            if (
                item.sequence not in hot_sequences
                and not item.archived
                and (self.active_file.parent / item.filename).exists()
            )
        ]

    def _retention_candidates(
        self,
        now: float,
    ) -> list[JournalSegment]:
        segments = sorted(
            self._manifest.segments,
            key=lambda item: item.sequence,
        )
        hot_sequences = {
            item.sequence
            for item in segments[-self.hot_segments():]
        }
        total_bytes = self._active_size() + sum(
            (
                item.compression_bytes
                if item.archived and item.compression_bytes is not None
                else item.size_bytes
            )
            for item in segments
        )
        over_count = max(
            0,
            len(segments) - self.retention_max_segments(),
        )
        candidates: list[JournalSegment] = []

        for item in segments:
            if item.sequence in hot_sequences:
                continue
            if not item.indexed:
                continue
            if (item.protected_records or 0) > 0:
                continue
            retained_size = (
                item.compression_bytes
                if item.archived and item.compression_bytes is not None
                else item.size_bytes
            )
            age_due = (
                now - item.closed_at >= self.retention_seconds()
            )
            size_due = total_bytes > self.retention_max_bytes()
            count_due = over_count > 0
            if not (age_due or size_due or count_due):
                continue
            candidates.append(item)
            total_bytes = max(0, total_bytes - retained_size)
            if over_count > 0:
                over_count -= 1

        return candidates

    def _delete_segment(
        self,
        segment: JournalSegment,
    ) -> int:
        removed = 0
        paths = [
            self.active_file.parent / segment.filename,
        ]
        if segment.archive_filename:
            paths.append(
                self.archive_directory / segment.archive_filename
            )
        for path in paths:
            try:
                size = int(path.stat().st_size)
            except FileNotFoundError:
                continue
            path.unlink()
            removed += size
        self._manifest.segments = [
            item
            for item in self._manifest.segments
            if item.sequence != segment.sequence
        ]
        self._manifest.cleanup_removed_segments += 1
        self._manifest.cleanup_removed_bytes += removed
        return removed

    def maintain_once(self) -> dict[str, Any]:
        with self._lock:
            now = float(self.clock())
            rotated = None
            indexed = None
            archived = None
            removed: list[str] = []
            try:
                if self._rotation_due(now):
                    rotated = self._rotate_locked(now)

                unindexed = next(
                    (
                        item
                        for item in sorted(
                            self._manifest.segments,
                            key=lambda item: item.sequence,
                        )
                        if (
                            not item.indexed
                            and not item.archived
                            and (
                                self.active_file.parent
                                / item.filename
                            ).exists()
                        )
                    ),
                    None,
                )
                if unindexed is not None:
                    indexed = self._index_segment(unindexed)

                if self.compression_enabled():
                    backlog = self._archive_backlog()
                    if backlog:
                        candidate = backlog[0]
                        if not candidate.indexed:
                            candidate = self._index_segment(
                                candidate
                            )
                        archived = self._compress_and_archive(
                            candidate
                        )

                for candidate in self._retention_candidates(now):
                    self._delete_segment(candidate)
                    removed.append(candidate.id)

                self._manifest.last_maintenance_at = now
                self._maintenance_runs += 1
                self._maintenance_error_class = None
                self._persist_manifest()
            except Exception as exc:
                self._maintenance_error_class = type(exc).__name__
                self._manifest.cleanup_failures += 1
                self._manifest.last_maintenance_at = now
                with contextlib.suppress(Exception):
                    self._persist_manifest()
                raise

            return {
                "rotated": rotated.id if rotated else None,
                "indexed": indexed.id if indexed else None,
                "archived": archived.id if archived else None,
                "removed": removed,
                "status": self.status(),
            }

    async def run_forever(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.maintain_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.maintenance_interval_seconds())

    def status(self) -> dict[str, Any]:
        with self._lock:
            active_size = self._active_size()
            segments = list(self._manifest.segments)
            retained_bytes = active_size + sum(
                (
                    item.compression_bytes
                    if item.archived
                    and item.compression_bytes is not None
                    else item.size_bytes
                )
                for item in segments
            )
            oldest_values = [
                item.oldest_event_at
                for item in segments
                if item.oldest_event_at is not None
            ]
            if self._active_oldest_at is not None:
                oldest_values.append(self._active_oldest_at)
            newest_values = [
                item.newest_event_at
                for item in segments
                if item.newest_event_at is not None
            ]
            if self._active_newest_at is not None:
                newest_values.append(self._active_newest_at)
            protected_segments = sum(
                1
                for item in segments
                if (item.protected_records or 0) > 0
            )
            unknown_protection = sum(
                1
                for item in segments
                if item.protected_records is None
            )
            backlog = self._archive_backlog()

            return {
                "version": JOURNAL_MANIFEST_VERSION,
                "activeFile": str(self.active_file),
                "activeSequence": self._manifest.active_sequence,
                "activeBytes": active_size,
                "maxActiveBytes": self.max_active_bytes(),
                "maxActiveAgeSeconds": self.max_active_age_seconds(),
                "segmentCount": len(segments),
                "hotSegmentCount": min(
                    self.hot_segments(),
                    len(segments),
                ),
                "archivedSegmentCount": sum(
                    1 for item in segments if item.archived
                ),
                "archiveBacklog": len(backlog),
                "retainedBytes": retained_bytes,
                "retentionMaxBytes": self.retention_max_bytes(),
                "retentionMaxSegments": self.retention_max_segments(),
                "retentionSeconds": self.retention_seconds(),
                "oldestRetainedAt": (
                    min(oldest_values)
                    if oldest_values
                    else None
                ),
                "newestRetainedAt": (
                    max(newest_values)
                    if newest_values
                    else None
                ),
                "protectedSegmentCount": protected_segments,
                "unknownProtectionSegmentCount": (
                    unknown_protection
                ),
                "lastRotationAt": self._manifest.last_rotation_at,
                "lastMaintenanceAt": (
                    self._manifest.last_maintenance_at
                ),
                "rotationFailures": (
                    self._manifest.rotation_failures
                ),
                "archiveFailures": self._manifest.archive_failures,
                "cleanupFailures": self._manifest.cleanup_failures,
                "cleanupRemovedSegments": (
                    self._manifest.cleanup_removed_segments
                ),
                "cleanupRemovedBytes": (
                    self._manifest.cleanup_removed_bytes
                ),
                "maintenanceRuns": self._maintenance_runs,
                "maintenanceErrorClass": (
                    self._maintenance_error_class
                ),
                "compressionEnabled": self.compression_enabled(),
            }
