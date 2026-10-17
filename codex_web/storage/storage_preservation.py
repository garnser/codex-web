"""Lossless physical StateStore rows. Never interprets a domain payload as JSON.

This is an additive storage format, not a semantic BackupSnapshot or permission
 to restore a running store. The Recovery boundary owns authorization/isolation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from dataclasses import dataclass
from typing import Iterable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from codex_web.compatibility import ContractSpec
from codex_web.storage.state_store import check_operation_budget

STORAGE_PRESERVATION_CONTRACT = ContractSpec("state-storage-preservation", "1.0", ("1.0",))


class StoragePreservationRefused(ValueError):
    """Fixed, payload-free error; never include decoder excerpts in transport."""


@dataclass(frozen=True)
class StorageSnapshotLimits:
    # Defensive wire ceilings, not grants or changes to runtime resource limits.
    max_rows: int = 1_000_000
    max_value_bytes: int = 32 * 1024**2
    max_key_bytes: int = 65536
    max_payload_bytes: int = 2 * 1024**3
    max_plaintext_bytes: int = 4 * 1024**3
    max_record_bytes: int = 64 * 1024**2
    fetch_bytes: int = 8 * 1024**2

    def __post_init__(self):
        ceilings = (1_000_000, 32*1024**2, 65536, 2*1024**3,
                    4*1024**3, 64*1024**2, 8*1024**2)
        for value, maximum in zip(self.__dict__.values(), ceilings):
            if type(value) is not int or not 0 < value <= maximum:
                raise StoragePreservationRefused("invalid_or_expanded_parser_limit")


class StateStorageSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["state-storage-preservation"] = "state-storage-preservation"
    format_version: Literal["1.0"] = "1.0"
    backend: Literal["sqlite", "postgres"]
    storage_schema_version: int = Field(ge=1)
    encoding: Literal["UTF8"] = "UTF8"


class StoragePreservationVerificationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    integrity_status: Literal["verified", "refused", "unknown"]
    semantic_status: Literal["valid", "invalid", "unknown"] = "unknown"
    side_effects_enabled: Literal[False] = False
    rows: int = Field(ge=0)
    payload_bytes: int = Field(ge=0)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _frame(value: bytes) -> bytes:
    return struct.pack(">Q", len(value)) + value


def export_rows(header: StateStorageSnapshotV1, rows, limits: StorageSnapshotLimits) -> Iterator[bytes]:
    """Encode bounded rows incrementally, including the header in the digest."""
    digest = hashlib.sha256()
    count = payload_bytes = plaintext_bytes = 0
    previous = None

    def emit(chunk):
        nonlocal plaintext_bytes
        check_operation_budget()
        plaintext_bytes += len(chunk)
        if plaintext_bytes > limits.max_plaintext_bytes:
            raise StoragePreservationRefused("snapshot_plaintext_limit")
        digest.update(chunk)
        return chunk

    yield emit(_frame(canonical(header.model_dump(mode="json"))))
    for table, key, value, timestamp in rows:
        check_operation_budget()
        if table not in ("documents", "metadata") or not isinstance(key, str) or not isinstance(value, str):
            raise StoragePreservationRefused("unsupported_storage_row_type")
        key_bytes = key.encode("utf-8", errors="strict")
        position = (table, key_bytes)
        if previous is not None and position <= previous:
            raise StoragePreservationRefused("duplicate_or_unordered_storage_row")
        previous = position
        raw = value.encode("utf-8", errors="strict")
        if len(key_bytes) > limits.max_key_bytes or len(raw) > limits.max_value_bytes:
            raise StoragePreservationRefused("storage_row_limit")
        if not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp):
            raise StoragePreservationRefused("unsupported_storage_timestamp")
        count += 1
        payload_bytes += len(raw)
        if count > limits.max_rows or payload_bytes > limits.max_payload_bytes:
            raise StoragePreservationRefused("snapshot_inventory_limit")
        item = {"kind": "row", "table": table, "key": key,
                "updated_at_b64": base64.b64encode(struct.pack(">d", timestamp)).decode("ascii"),
                "value_b64": ""}
        prefix, suffix = canonical(item).split(b'"value_b64":""')
        prefix += b'"value_b64":"'
        suffix = b'"' + suffix
        length = len(prefix) + 4*((len(raw)+2)//3) + len(suffix)
        if length > limits.max_record_bytes:
            raise StoragePreservationRefused("encoded_record_limit")
        yield emit(struct.pack(">Q", length))
        yield emit(prefix)
        # Multiples of three avoid interior base64 padding. Never encode a
        # complete large row alongside a complete JSON/base64 copy.
        for offset in range(0, len(raw), 49152):
            yield emit(base64.b64encode(raw[offset:offset+49152]))
        yield emit(suffix)
        del raw
    footer = {"kind": "end", "rows": count, "payload_bytes": payload_bytes,
              "raw_sha256": digest.hexdigest()}
    end = _frame(canonical(footer))
    if plaintext_bytes + len(end) > limits.max_plaintext_bytes:
        raise StoragePreservationRefused("snapshot_plaintext_limit")
    yield end


class _Reader:
    def __init__(self, chunks: Iterable[bytes], limits):
        self.chunks = iter(chunks)
        self.pending = memoryview(b"")
        self.total = 0
        self.limits = limits

    def read(self, length, *, eof=False):
        if length > self.limits.max_record_bytes:
            raise StoragePreservationRefused("declared_record_limit")
        data = bytearray()
        while len(data) < length:
            check_operation_budget()
            if not self.pending:
                try:
                    chunk = next(self.chunks)
                except StopIteration:
                    if eof and not data:
                        return None
                    raise StoragePreservationRefused("truncated_storage_snapshot") from None
                if not isinstance(chunk, bytes) or len(chunk) > self.limits.max_record_bytes:
                    raise StoragePreservationRefused("transport_chunk_limit")
                self.pending = memoryview(chunk)
                if not chunk:
                    continue
            take = min(length-len(data), len(self.pending))
            data.extend(self.pending[:take])
            self.pending = self.pending[take:]
            self.total += take
            if self.total > self.limits.max_plaintext_bytes:
                raise StoragePreservationRefused("snapshot_plaintext_limit")
        return bytes(data)


def import_rows(chunks, *, backend, schema_version, limits):
    """Validate frames lazily. Caller MUST enclose every yielded insert in ONE
    empty isolated target transaction, committing only after exhausting footer.
    """
    reader = _Reader(chunks, limits)
    digest = hashlib.sha256()
    previous = None
    count = payload_bytes = 0
    first = True
    while True:
        check_operation_budget()
        prefix = reader.read(8)
        length = struct.unpack(">Q", prefix)[0]
        raw = reader.read(length)
        try:
            item = json.loads(raw)
        except (ValueError, UnicodeError):
            raise StoragePreservationRefused("invalid_storage_frame") from None
        if not isinstance(item, dict) or canonical(item) != raw:
            raise StoragePreservationRefused("noncanonical_storage_frame")
        if first:
            try:
                header = StateStorageSnapshotV1.model_validate(item)
            except ValueError:
                raise StoragePreservationRefused("unsupported_storage_contract") from None
            STORAGE_PRESERVATION_CONTRACT.require(header.format_version)
            if header.backend != backend or header.storage_schema_version != schema_version:
                raise StoragePreservationRefused("backend_or_schema_mismatch")
            first = False
        elif item.get("kind") == "end":
            if item != {"kind":"end", "rows":count, "payload_bytes":payload_bytes,
                        "raw_sha256":digest.hexdigest()}:
                raise StoragePreservationRefused("storage_footer_mismatch")
            if reader.read(1, eof=True) is not None:
                raise StoragePreservationRefused("trailing_storage_content")
            return
        else:
            if set(item) != {"kind", "table", "key", "value_b64", "updated_at_b64"} or item.get("kind") != "row":
                raise StoragePreservationRefused("unsupported_storage_record")
            table, key = item["table"], item["key"]
            if table not in ("documents", "metadata") or not isinstance(key, str):
                raise StoragePreservationRefused("invalid_storage_key")
            key_bytes = key.encode("utf8", errors="strict")
            position = (table, key_bytes)
            if len(key_bytes) > limits.max_key_bytes or (previous is not None and position <= previous):
                raise StoragePreservationRefused("duplicate_or_unordered_storage_row")
            previous = position
            try:
                value_bytes = base64.b64decode(item["value_b64"], validate=True)
                value = value_bytes.decode("utf8", errors="strict")
                ts_bytes = base64.b64decode(item["updated_at_b64"], validate=True)
                timestamp = struct.unpack(">d", ts_bytes)[0]
            except (ValueError, TypeError, struct.error):
                raise StoragePreservationRefused("invalid_storage_encoding") from None
            if (base64.b64encode(value_bytes).decode("ascii") != item["value_b64"]
                or base64.b64encode(ts_bytes).decode("ascii") != item["updated_at_b64"]
                or not math.isfinite(timestamp)):
                raise StoragePreservationRefused("noncanonical_storage_value")
            count += 1
            payload_bytes += len(value_bytes)
            if len(value_bytes) > limits.max_value_bytes or count > limits.max_rows or payload_bytes > limits.max_payload_bytes:
                raise StoragePreservationRefused("snapshot_inventory_limit")
            yield table, key, value, timestamp
        digest.update(prefix)
        digest.update(raw)


def database_rows(connection, *, backend, limits, deadline=lambda: None):
    """Only fixed StateStore tables/columns; size before grouped payload fetch.

    Connection custody and one repeatable snapshot belong to the backend. SQL
    values are always bound; table/column identifiers below are code-owned.
    """
    pg = backend == "postgres"
    tables = (("documents", "codex_state_documents" if pg else "state_documents", "namespace", "payload"),
              ("metadata", "codex_state_metadata" if pg else "state_metadata", "key", "value"))
    bind = "%s" if pg else "?"
    collate = ' COLLATE "C"' if pg else " COLLATE BINARY"
    total_rows = total_bytes = 0
    for tag, table, key, value in tables:
        deadline()
        with_cursor = connection.cursor()
        try:
            with_cursor.execute(f"SELECT * FROM {table} WHERE 1=0")
            if [d[0] for d in with_cursor.description] != [key, value, "updated_at"]:
                raise StoragePreservationRefused("unsupported_storage_columns")
            length = f"octet_length({value})" if pg else f"length(CAST({value} AS BLOB))"
            key_length = f"octet_length({key})" if pg else f"length(CAST({key} AS BLOB))"
            with_cursor.execute(f"SELECT COUNT(*), COALESCE(SUM({length}),0), COALESCE(MAX({length}),0), COALESCE(MAX({key_length}),0) FROM {table}")
            n, size, biggest, biggest_key = with_cursor.fetchone()
            total_rows += int(n)
            total_bytes += int(size)
            if (total_rows > limits.max_rows or total_bytes > limits.max_payload_bytes or
                    biggest > limits.max_value_bytes or biggest_key > limits.max_key_bytes):
                raise StoragePreservationRefused("snapshot_inventory_limit")
        finally:
            with_cursor.close()
    for tag, table, key, value in tables:
        last = None
        length = f"octet_length({value})" if pg else f"length(CAST({value} AS BLOB))"
        while True:
            check_operation_budget()
            deadline()
            cursor = connection.cursor()
            try:
                where = "" if last is None else f" WHERE {key}{collate} > {bind}"
                cursor.execute(f"SELECT {key},{length} FROM {table}{where} ORDER BY {key}{collate} LIMIT 64", () if last is None else (last,))
                page = cursor.fetchall()  # <=64 keys/lengths, NEVER payloads.
            finally:
                cursor.close()
            if not page:
                break
            group, group_bytes = [], 0
            groups = []
            for name, size in page:
                if group and group_bytes + size > limits.fetch_bytes:
                    groups.append(group)
                    group, group_bytes = [], 0
                group.append(name)
                group_bytes += size
                if group_bytes >= limits.fetch_bytes:
                    groups.append(group)
                    group, group_bytes = [], 0
            if group:
                groups.append(group)
            for keys in groups:
                check_operation_budget()
                deadline()
                cursor = connection.cursor()
                try:
                    placeholders = ",".join([bind]*len(keys))
                    cursor.execute(f"SELECT {key},{value},updated_at FROM {table} WHERE {key} IN ({placeholders}) ORDER BY {key}{collate}", tuple(keys))
                    fetched = 0
                    while True:
                        check_operation_budget()
                        row = cursor.fetchone()
                        if row is None:
                            break
                        fetched += 1
                        yield tag, *row
                        del row
                    if fetched != len(keys):
                        raise StoragePreservationRefused("snapshot_row_changed")
                finally:
                    cursor.close()
            last = page[-1][0]


def restore_database_rows(connection, chunks, *, backend, schema_version, limits, max_target_bytes=None):
    """Low-level atomic import. Never called on a live target by Recovery API."""
    pg = backend == "postgres"
    documents = "codex_state_documents" if pg else "state_documents"
    metadata = "codex_state_metadata" if pg else "state_metadata"
    bind = "%s" if pg else "?"
    cursor = connection.cursor()
    try:
        if pg:
            cursor.execute(f"LOCK TABLE {documents}, {metadata} IN ACCESS EXCLUSIVE MODE")
        cursor.execute(f"SELECT COUNT(*) FROM {documents}")
        if cursor.fetchone()[0] != 0:
            raise StoragePreservationRefused("isolated_target_not_empty")
        cursor.execute(f"SELECT key,value FROM {metadata}")
        if cursor.fetchall() != [("schema_version", str(schema_version))]:
            raise StoragePreservationRefused("isolated_target_metadata_not_seed")
        cursor.execute(f"DELETE FROM {metadata} WHERE key={bind}", ("schema_version",))
        seen_schema = False
        batch=[];batch_bytes=0;batch_table=None
        def flush():
            nonlocal batch,batch_bytes
            if not batch:return
            check_operation_budget()
            target,column,payload = ((documents,"namespace","payload") if batch_table=="documents"
                                     else (metadata,"key","value"))
            # psycopg's executemany pipelines bounded rows on the SAME owning
            # connection/transaction. No per-row network roundtrip or COPY
            # escape; SQLite uses its existing bounded executemany primitive.
            cursor.executemany(f"INSERT INTO {target}({column},{payload},updated_at) VALUES ({bind},{bind},{bind})",batch)
            check_operation_budget()
            if max_target_bytes is not None:
                # Cooperative growth accounting on the owning target transaction,
                # after each bounded batch; not an OS/filesystem hard quota.
                if pg:
                    cursor.execute("SELECT pg_total_relation_size('codex_state_documents')+pg_total_relation_size('codex_state_metadata')")
                    physical=int(cursor.fetchone()[0])
                else:
                    cursor.execute('PRAGMA page_count');pages=int(cursor.fetchone()[0])
                    cursor.execute('PRAGMA page_size');physical=pages*int(cursor.fetchone()[0])
                if physical>max_target_bytes:
                    raise StoragePreservationRefused('isolated_target_growth_limit')
            batch=[];batch_bytes=0
        for table, key, value, timestamp in import_rows(chunks, backend=backend, schema_version=schema_version, limits=limits):
            check_operation_budget()
            if table == "metadata":
                if key == "schema_version":
                    if value != str(schema_version):
                        raise StoragePreservationRefused("storage_schema_mismatch")
                    seen_schema = True
            size=len(key.encode('utf8'))+len(value.encode('utf8'))+8
            if batch and (table!=batch_table or len(batch)>=256 or batch_bytes+size>4*1024**2):
                flush()
            batch_table=table
            batch.append((key,value,timestamp));batch_bytes+=size
            if batch_bytes>=4*1024**2:flush()
        # import_rows must have exhausted authenticated footer and EOF before
        # the final flush; any exception rolls back earlier bounded batches.
        flush()
        if not seen_schema:
            raise StoragePreservationRefused("missing_storage_schema")
        check_operation_budget()
    finally:
        cursor.close()


def inspect_database_page(connection, *, namespace, after=None, limit=100, backend):
    """Internal read-only bounded opaque page. No tenant inference or migration.

    Callers must authorize before reading and must never serialize raw rows to
    an API. Detailed diagnostics need independent whole-store authorization.
    """
    from codex_web.storage.state_store import state_record_prefix, state_record_marker
    if not isinstance(namespace, str) or not 1 <= limit <= 100:
        raise StoragePreservationRefused("invalid_inspection_page")
    pg = backend == "postgres"
    table = "codex_state_documents" if pg else "state_documents"
    bind = "%s" if pg else "?"
    collate = ' COLLATE "C"' if pg else " COLLATE BINARY"
    prefix = state_record_prefix(namespace)
    marker = state_record_marker(namespace)
    length = "octet_length(payload)" if pg else "length(CAST(payload AS BLOB))"
    cursor = connection.cursor()
    try:
        cursor.execute(f"SELECT namespace,{length} FROM {table} WHERE "
                       f"(namespace={bind} OR namespace={bind} OR (namespace{collate}>={bind} AND namespace{collate}<{bind})) "
                       f"AND namespace{collate}>{bind} ORDER BY namespace{collate} LIMIT {limit+1}",
                       (namespace,marker,prefix,prefix+'\uffff',after or ''))
        keys = cursor.fetchall()
        selected = keys[:limit]
        if sum(size for _,size in selected) > 8*1024**2:
            raise StoragePreservationRefused("usage_inspection_page_byte_limit")
        rows = []
        for key, _ in selected:
            check_operation_budget()
            cursor.execute(f"SELECT payload FROM {table} WHERE namespace={bind}", (key,))
            row = cursor.fetchone()
            if row is None:
                raise StoragePreservationRefused("usage_inspection_changed")
            rows.append((key,row[0]))
        return rows, selected[-1][0] if len(keys)>limit else None
    finally:
        cursor.close()
