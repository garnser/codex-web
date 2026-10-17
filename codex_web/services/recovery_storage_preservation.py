"""Bounded storage-preservation envelope codec for existing Recovery/content.

No destination, registry, authority or execution engine lives here. The caller
must use the canonical tracked action and ArtifactContent boundaries. A decoder
is an authenticated iterator: consumers MUST exhaust it before committing an
isolated target or reporting success. It never means semantic Recovery PASS.
"""
from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterator

from codex_web.crypto import EncryptedEnvelope
from codex_web.storage.storage_preservation import (
    StoragePreservationRefused, StorageSnapshotLimits, _Reader, canonical,
)
from codex_web.storage.state_store import check_operation_budget

MAGIC = b'CWSP1\n'
CHUNK_BYTES = 262144
MAX_CONTAINER = 6 * 1024**3
MAX_ENVELOPE = 400 * 1024
MAX_MANIFEST = 8 * 1024**2
MAX_CHUNKS = (4*1024**3 + CHUNK_BYTES-1)//CHUNK_BYTES


def _frame(item):
    raw = canonical(item)
    return struct.pack('>Q',len(raw)) + raw


def _blocks(chunks):
    pending = bytearray()
    for chunk in chunks:
        if not isinstance(chunk,bytes):
            raise StoragePreservationRefused('invalid_plaintext_transport')
        for start in range(0,len(chunk),CHUNK_BYTES):
            check_operation_budget()
            part = memoryview(chunk)[start:start+CHUNK_BYTES]
            while part:
                take = min(CHUNK_BYTES-len(pending),len(part))
                pending.extend(part[:take])
                part = part[take:]
                if len(pending)==CHUNK_BYTES:
                    yield bytes(pending)
                    pending.clear()
    if pending:
        yield bytes(pending)


def seal_storage_snapshot(chunks, *, crypto, key_id, context, actor) -> Iterator[bytes]:
    # Enter before advancing the source snapshot iterator: pin KMS outside its
    # owning transaction. Its final audited recheck occurs after cursor close.
    with crypto.envelope_batch(key_id,context,actor=actor,operation='encrypt') as batch:
        header = {'kind':'encrypted-state-storage-preservation','format_version':'1.0',
                  'snapshot_id':context.object_id,'key_id':key_id,'key_version':batch.key_version,
                  'context_sha256':hashlib.sha256(canonical(context.model_dump(mode='json'))).hexdigest()}
        prefix = MAGIC + _frame(header)
        yield prefix
        digests = []
        count = plain_bytes = 0
        container_bytes = len(prefix)
        source = iter(chunks)
        try:
            for block in _blocks(source):
                check_operation_budget()
                if count >= MAX_CHUNKS:
                    raise StoragePreservationRefused('encrypted_chunk_count_limit')
                ctx = context.model_copy(update={'object_id':context.object_id+':chunk:'+str(count)})
                envelope = batch.apply(block,ctx)
                record = _frame({'kind':'chunk','index':count,'envelope':envelope.model_dump(mode='json')})
                if len(record)>MAX_ENVELOPE:
                    raise StoragePreservationRefused('encrypted_chunk_size_limit')
                container_bytes += len(record)
                if container_bytes>MAX_CONTAINER:
                    raise StoragePreservationRefused('encrypted_container_limit')
                digests.append(hashlib.sha256(record).hexdigest())
                count += 1
                plain_bytes += len(block)
                yield record
        finally:
            close = getattr(source,'close',None)
            if callable(close):
                close()
        manifest = {'header':header,'encrypted_chunk_sha256':digests,
                    'chunks':count,'plaintext_bytes':plain_bytes}
        manifest_bytes = canonical(manifest)
        if len(manifest_bytes)>MAX_MANIFEST or plain_bytes>4*1024**3:
            raise StoragePreservationRefused('encrypted_manifest_limit')
        envelope = batch.apply(manifest_bytes,context.model_copy(update={'object_id':context.object_id+':manifest'}))
        terminal = _frame({'kind':'manifest','envelope':envelope.model_dump(mode='json')})
        if container_bytes+len(terminal)>MAX_CONTAINER:
            raise StoragePreservationRefused('encrypted_container_limit')
    # Batch audit/current-key recheck MUST finish before ArtifactContent can
    # consume the terminal record and atomically publish successful content.
    yield terminal


def open_storage_snapshot(chunks, *, crypto, context, actor) -> Iterator[bytes]:
    reader = _Reader(chunks,StorageSnapshotLimits(max_plaintext_bytes=4*1024**3))
    # Encrypted stream is larger than raw data; _Reader total is a transport
    # counter here, bounded separately rather than changing runtime admission.
    reader.limits = _ContainerLimits()
    if reader.read(len(MAGIC)) != MAGIC:
        raise StoragePreservationRefused('unsupported_encrypted_storage_format')
    def record(maximum):
        prefix = reader.read(8)
        size = struct.unpack('>Q',prefix)[0]
        if size>maximum:
            raise StoragePreservationRefused('declared_encrypted_record_limit')
        raw = reader.read(size)
        try:
            item = json.loads(raw)
        except (ValueError,UnicodeError):
            raise StoragePreservationRefused('invalid_encrypted_storage_record') from None
        if not isinstance(item,dict) or canonical(item)!=raw:
            raise StoragePreservationRefused('noncanonical_encrypted_storage_record')
        return item, prefix+raw
    header,_ = record(65536)
    if (set(header)!={'kind','format_version','snapshot_id','key_id','key_version','context_sha256'}
        or header.get('kind')!='encrypted-state-storage-preservation' or header.get('format_version')!='1.0'
        or header.get('snapshot_id')!=context.object_id
        or header.get('context_sha256')!=hashlib.sha256(canonical(context.model_dump(mode='json'))).hexdigest()
        or not isinstance(header.get('key_id'),str) or type(header.get('key_version')) is not int):
        raise StoragePreservationRefused('foreign_encrypted_storage_scope')
    count = plain_bytes = 0
    digests = []
    with crypto.envelope_batch(header['key_id'],context,actor=actor,operation='decrypt',key_version=header['key_version']) as batch:
        while True:
            item, framed = record(2*MAX_MANIFEST)
            if item.get('kind')=='manifest':
                if set(item)!={'kind','envelope'}:
                    raise StoragePreservationRefused('invalid_manifest_record')
                envelope = EncryptedEnvelope.model_validate(item['envelope'])
                manifest_raw = batch.apply(envelope,context.model_copy(update={'object_id':context.object_id+':manifest'}))
                if len(manifest_raw)>MAX_MANIFEST:
                    raise StoragePreservationRefused('manifest_plaintext_limit')
                expected = canonical({'header':header,'encrypted_chunk_sha256':digests,
                                      'chunks':count,'plaintext_bytes':plain_bytes})
                if manifest_raw!=expected:
                    raise StoragePreservationRefused('authenticated_manifest_mismatch')
                if reader.read(1,eof=True) is not None:
                    raise StoragePreservationRefused('trailing_encrypted_content')
                return
            if (set(item)!={'kind','index','envelope'} or item.get('kind')!='chunk'
                or type(item.get('index')) is not int or item['index']!=count
                or len(framed)>MAX_ENVELOPE or count>=MAX_CHUNKS):
                raise StoragePreservationRefused('encrypted_chunk_order_or_limit')
            envelope = EncryptedEnvelope.model_validate(item['envelope'])
            block = batch.apply(envelope,context.model_copy(update={'object_id':context.object_id+':chunk:'+str(count)}))
            if not 0<len(block)<=CHUNK_BYTES:
                raise StoragePreservationRefused('decrypted_chunk_limit')
            digests.append(hashlib.sha256(framed).hexdigest())
            count+=1
            plain_bytes+=len(block)
            if plain_bytes>4*1024**3:
                raise StoragePreservationRefused('decrypted_stream_limit')
            yield block


class _ContainerLimits:
    max_record_bytes = 2*MAX_MANIFEST
    max_plaintext_bytes = MAX_CONTAINER
