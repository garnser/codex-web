"""Bounded semantic snapshot construction, preserving BackupSnapshot 1.0 bytes.

Only a bounded key/size inventory is retained. Payloads stay in the source
repeatable-read transaction until fetched in small indexed batches. No plaintext
spool or second database is created. The final plaintext buffer and existing
crypto/envelope allocations retain their separate Recovery admission checks.
"""
from contextlib import ExitStack
from io import BytesIO
import hashlib
import json

from codex_web.canonical_json import canonical_json_bytes, canonical_json_chunks
from codex_web.storage.state_store import (
    parse_state_record_storage_key, check_operation_budget, enter_recovery_capture_stage,
)
from codex_web.services.recovery_snapshot_strings import observe_snapshot_string_bound, require_snapshot_string_bound

MAX_ROWS = 1_000_000
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_ROW_BYTES = 32 * 1024**2
MAX_METADATA_BYTES = 512 * 1024**2
MAX_NAMESPACES = 4096
BATCH_ROWS = 512
FIXED_WORK_BYTES = 32 * 1024**2
ENCODED_CHUNK_BYTES = 64 * 1024


class SnapshotCaptureRefused(ValueError):
    """Closed refusal; never retain source payload in an exception."""


class SnapshotCapture:
    def __init__(self, state_store, admit, *, only_namespace=None):
        self.store = state_store
        self.only_namespace = only_namespace
        self.admit = admit
        self.stack = ExitStack()
        self.logical = {}
        self.sizes = {}
        self.overrides = {}

    def __enter__(self):
        # The nested audit-only transaction stays attributed to live_audit.
        if self.only_namespace is None:
            enter_recovery_capture_stage('inventory')
        from codex_web.storage.postgres_state import PostgresStateStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        if not isinstance(self.store, (PostgresStateStore, SQLiteStateStore)):
            raise SnapshotCaptureRefused('semantic_snapshot_backend_unsupported')
        self.pg = isinstance(self.store, PostgresStateStore)
        if getattr(getattr(self.store, '_callback_reads', None), 'connection', None) is not None:
            raise SnapshotCaptureRefused('semantic_snapshot_requires_own_transaction')
        try:
            self.connection = self.stack.enter_context(self.store._connection(
                **({'read_only_snapshot': True} if self.pg else {})))
            if not self.pg:
                # Bind metadata and all later indexed reads to the SAME snapshot.
                self.connection.execute('BEGIN')
            table = 'codex_state_documents' if self.pg else 'state_documents'
            length = 'octet_length' if self.pg else 'length'
            payload = 'payload' if self.pg else 'CAST(payload AS BLOB)'
            namespace = 'namespace' if self.pg else 'CAST(namespace AS BLOB)'
            self.table = table
            self.where, self.params = '', ()
            if self.only_namespace is not None:
                from codex_web.storage.state_store import state_record_prefix
                prefix = state_record_prefix(self.only_namespace)
                placeholder = '%s' if self.pg else '?'
                collation = ' COLLATE \"C\"' if self.pg else ''
                self.where = f' WHERE namespace={placeholder} OR (namespace{collation}>={placeholder} AND namespace{collation}<{placeholder})'
                self.params = (self.only_namespace, prefix, prefix+'\uffff')
            rows, source_bytes, key_bytes, largest = self.read_inventory()
            if (any(type(v) is not int or v < 0 for v in (rows, source_bytes, key_bytes, largest))
                    or key_bytes > source_bytes):
                raise SnapshotCaptureRefused('semantic_snapshot_inventory_unknown')
            metadata = rows * 512 + key_bytes * 4
            if (rows > MAX_ROWS or source_bytes > MAX_SOURCE_BYTES
                    or largest > MAX_ROW_BYTES or metadata > MAX_METADATA_BYTES):
                raise SnapshotCaptureRefused('semantic_snapshot_inventory_limit')
            # Conservatively retain all key metadata, a byte-bounded encoded batch and a
            # single decoded/canonicalized row. Never apply 12x to the full catalog.
            self.raw_batch_bound = min(MAX_ROW_BYTES, source_bytes - key_bytes)
            self.admit('capture_snapshot', metadata + self.raw_batch_bound * 8 + largest * 18 + FIXED_WORK_BYTES)
            self.metadata_bound = metadata
            self.largest = largest
            self.inventory = (rows, source_bytes, key_bytes, largest)
            seen = 0
            with self.cursor(named=True) as cursor:
                order = 'namespace COLLATE "C"' if self.pg else 'namespace'
                cursor.execute(f'SELECT namespace,{length}({payload}) FROM {table}'+self.where+f' ORDER BY {order}', self.params)
                while True:
                    self.checkpoint()
                    batch = cursor.fetchmany(64)
                    if not batch:
                        break
                    for name, size in batch:
                        check_operation_budget()
                        seen += 1
                        if seen > rows or type(size) is not int or not 0 <= size <= largest:
                            raise SnapshotCaptureRefused('semantic_snapshot_inventory_changed')
                        parsed = parse_state_record_storage_key(name)
                        self.sizes[name] = size
                        if parsed is None:
                            self.logical.setdefault(name, {'legacy': None, 'records': {}})['legacy'] = name
                        else:
                            namespace_name, key = parsed
                            item = self.logical.setdefault(namespace_name, {'legacy': None, 'records': {}})
                            if key is not None:
                                # Match documents(): later physical keys win if
                                # percent-decoding aliases the same logical key.
                                item['records'][key] = name
                        if len(self.logical) > MAX_NAMESPACES:
                            raise SnapshotCaptureRefused('semantic_snapshot_namespace_limit')
            if seen != rows:
                raise SnapshotCaptureRefused('semantic_snapshot_inventory_changed')
            self.namespaces = tuple(sorted(self.logical))
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *exc):
        try:
            return self.stack.__exit__(*exc)
        finally:
            self.logical.clear()
            self.sizes.clear()
            self.overrides.clear()

    def read_inventory(self):
        length = 'octet_length' if self.pg else 'length'
        payload = 'payload' if self.pg else 'CAST(payload AS BLOB)'
        namespace = 'namespace' if self.pg else 'CAST(namespace AS BLOB)'
        with self.cursor() as cursor:
            cursor.execute(f'SELECT COUNT(*),COALESCE(SUM({length}({payload})+{length}({namespace})),0),'
                f'COALESCE(SUM({length}({namespace})),0),COALESCE(MAX({length}({payload})),0) FROM {self.table}'
                + self.where, self.params)
            return cursor.fetchone()

    def checkpoint(self):
        check_operation_budget()
        if self.pg:
            self.store._operation_deadline(self.connection)

    def cursor(self, named=False):
        from contextlib import closing
        import uuid
        self.checkpoint()
        return closing(self.connection.cursor(name='semantic_capture_' + uuid.uuid4().hex)
                       if named and self.pg else self.connection.cursor())

    def values(self, keys):
        index = 0
        while index < len(keys):
            end, byte_count = index, 0
            while end < len(keys) and end-index < BATCH_ROWS:
                size = self.sizes[keys[end]]
                if end > index and byte_count + size > MAX_ROW_BYTES:
                    break
                byte_count += size
                end += 1
            selected = keys[index:end]
            index = end
            self.checkpoint()
            with self.cursor() as cursor:
                if self.pg:
                    cursor.execute(f'SELECT namespace,payload FROM {self.table} WHERE namespace=ANY(%s)', (list(selected),))
                else:
                    cursor.execute(f'SELECT namespace,payload FROM {self.table} WHERE namespace IN ('
                                   + ','.join('?' for _ in selected) + ')', selected)
                batch = dict(cursor.fetchmany(BATCH_ROWS + 1))
            if set(batch) != set(selected):
                raise SnapshotCaptureRefused('semantic_snapshot_row_changed')
            for key in selected:
                check_operation_budget()
                raw = batch.pop(key)
                if not isinstance(raw, str) or len(raw.encode('utf-8')) != self.sizes[key]:
                    raise SnapshotCaptureRefused('semantic_snapshot_row_changed')
                value = json.loads(raw)
                bound = observe_snapshot_string_bound(value)
                require_snapshot_string_bound(value, bound)
                yield value
                del value, raw
            del batch

    def get(self, namespace):
        if namespace not in self.logical:
            return None
        if namespace in self.overrides:
            return self.overrides[namespace][0]
        item = self.logical[namespace]
        if item['legacy'] is not None:
            return next(self.values((item['legacy'],)))
        entries = sorted(item['records'].items())
        if sum(self.sizes[row] for _, row in entries) > MAX_ROW_BYTES:
            raise SnapshotCaptureRefused('semantic_snapshot_control_namespace_limit')
        return {key: value for (key, _), value in zip(entries, self.values(tuple(row for _, row in entries)))}

    def record_keys(self, namespace):
        item = self.logical.get(namespace)
        return None if item is None or item['legacy'] is not None else tuple(sorted(item['records']))

    def record_items(self, namespace, keys):
        item = self.logical[namespace]
        selected = tuple(item['records'][key] for key in keys)
        for key, value in zip(keys, self.values(selected)):
            yield key, value
            del value

    def audit_integrity(self, audit, namespace, *, organization_id, workspace_id):
        self.validate_physical_rows()
        from codex_web.storage.autonomy_audit import AutonomyAuditStore
        from codex_web.autonomy_audit import AutonomyAuditState
        keys = self.record_keys(namespace)
        if keys is None or AutonomyAuditStore.meta_key not in keys:
            return audit.verify_snapshot(self.get(namespace), organization_id=organization_id, workspace_id=workspace_id)
        controls = tuple(key for key in keys if (key in AutonomyAuditState.model_fields and key != 'records') or key == AutonomyAuditStore.meta_key)
        # The large record catalog is excluded; control/checkpoint/schema data
        # retains an explicit bound and the original typed schema validation.
        if sum(self.sizes[self.logical[namespace]['records'][key]] for key in controls) > MAX_ROW_BYTES:
            raise SnapshotCaptureRefused('semantic_snapshot_audit_control_limit')
        control = dict(self.record_items(namespace, controls))
        records = tuple(key for key in keys if key.startswith('record:'))
        return audit.verify_snapshot_records(control, records,
            lambda selected: self.record_items(namespace, selected),
            organization_id=organization_id, workspace_id=workspace_id)

    def replace(self, namespace, value):
        if namespace not in self.logical:
            raise SnapshotCaptureRefused('semantic_snapshot_override_missing')
        self.overrides[namespace] = (value, observe_snapshot_string_bound(value))

    def row_chunks(self, value):
        # values() has already checked this physical row's <=32MiB raw size and
        # observed/rechecked every string. The existing largest*18 admission
        # covers its decoded graph plus native JSON text/bytes overlap (including
        # ensure_ascii escaping). Do not use this for an unbounded namespace.
        check_operation_budget()
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
        for offset in range(0, len(encoded), ENCODED_CHUNK_BYTES):
            check_operation_budget()
            yield encoded[offset:offset + ENCODED_CHUNK_BYTES]

    def namespace_chunks(self, namespace):
        check_operation_budget()
        if namespace in self.overrides:
            value, bound = self.overrides[namespace]
            require_snapshot_string_bound(value, bound)
            yield from canonical_json_chunks(value)
            return
        item = self.logical[namespace]
        if item['legacy'] is not None:
            value = next(self.values((item['legacy'],)))
            yield from self.row_chunks(value)
            return
        entries = sorted(item['records'].items())
        yield b'{'
        for index, ((key, _), value) in enumerate(zip(entries, self.values(tuple(row for _, row in entries)))):
            if index:
                yield b','
            yield canonical_json_bytes(key)
            yield b':'
            yield from self.row_chunks(value)
            del value
        yield b'}'

    def selected_physical_row(self, physical):
        parsed = parse_state_record_storage_key(physical)
        namespace = physical if parsed is None else parsed[0]
        if namespace in self.overrides:
            return False
        item = self.logical[namespace]
        if parsed is None:
            return item['legacy'] == physical
        return (item['legacy'] is None and parsed[1] is not None
                and item['records'].get(parsed[1]) == physical)

    def validate_physical_rows(self, *, shadowed_only=False):
        # documents() decodes even rows later shadowed by a legacy singleton.
        # Preserve that refusal: ignored logical data cannot hide malformed JSON.
        if getattr(self, '_validated_all', False):
            return
        keys = tuple(key for key in self.sizes if
            (not shadowed_only or not self.selected_physical_row(key)) and
            (self.pg or parse_state_record_storage_key(key) is None or
             parse_state_record_storage_key(key)[1] is not None))
        for value in self.values(keys):
            del value
        if not shadowed_only:
            self._validated_all = True

    def checksum(self):
        # Chosen rows are decoded and string-checked by namespace_chunks below.
        # Only shadowed/ignored rows need a separate validation pass. Mark the
        # full catalog validated only after the checksum consumes every row;
        # audit-only callers still use the unconditional full validation above.
        enter_recovery_capture_stage('shadowed_validation')
        self.validate_physical_rows(shadowed_only=True)
        enter_recovery_capture_stage('checksum')
        digest = hashlib.sha256()
        self.documents_json_bytes = 2 + max(0, len(self.namespaces)-1)
        for namespace in self.namespaces:
            self.documents_json_bytes += len(canonical_json_bytes(namespace)) + 1
            digest.update(namespace.encode('utf-8') + b'\x00')
            document = hashlib.sha256()
            for chunk in self.namespace_chunks(namespace):
                document.update(chunk)
                self.documents_json_bytes += len(chunk)
            digest.update(document.digest() + b'\x00')
        self._validated_all = True
        return digest.hexdigest()

    def plaintext(self, metadata):
        enter_recovery_capture_stage('plaintext')
        # The key inventory and source cursor remain bounded while the ONE
        # compatible plaintext buffer is assembled. No decoded catalog overlaps it.
        if not hasattr(self, 'documents_json_bytes'):
            raise SnapshotCaptureRefused('semantic_snapshot_checksum_required')
        bound = len(canonical_json_bytes({**metadata, 'documents': {}})) - 2 + self.documents_json_bytes
        self.admit('capture_serialize', bound * 2 + self.metadata_bound
                   + self.raw_batch_bound * 8 + self.largest * 18 + FIXED_WORK_BYTES)
        with BytesIO() as output:
            def write(chunk):
                check_operation_budget()
                if output.tell() + len(chunk) > bound:
                    raise SnapshotCaptureRefused('semantic_snapshot_plaintext_limit')
                output.write(chunk)
            write(b'{')
            for index, key in enumerate(sorted((*metadata, 'documents'))):
                if index:
                    write(b',')
                write(canonical_json_bytes(key)); write(b':')
                if key != 'documents':
                    for chunk in canonical_json_chunks(metadata[key]):
                        write(chunk)
                    continue
                write(b'{')
                for offset, namespace in enumerate(self.namespaces):
                    if offset:
                        write(b',')
                    write(canonical_json_bytes(namespace)); write(b':')
                    for chunk in self.namespace_chunks(namespace):
                        write(chunk)
                write(b'}')
            write(b'}')
            return output.getvalue()
