"""Governed storage operations composed from Recovery/Artifact/Evidence/KMS.

No new receipt store, repair or live restore. An incomplete reserved Artifact is
unknown custody, never permission to execute a second time.
"""
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from codex_web.artifact_evidence import (ArtifactCreate, ArtifactType, ArtifactDigest,
    EvidenceCreate, EvidenceType, EvidenceResult, EvidenceLifecycle, VerificationResult)
from codex_web.artifact_content import ArtifactContentScope, ArtifactContentPointer
from codex_web.crypto import EncryptionContext
from codex_web.data_governance import DataClassification
from codex_web.recovery_preservation import SOURCE, CAPACITY_CONTRACT, CAPTURE, VERIFY, StoragePreservationReportV1
from codex_web.resources import ResourceType, ResourceLifecycle
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_policy import RecoveryPolicyControl
from codex_web.services.recovery_preservation_configuration import preservation_configuration
from codex_web.services.recovery_reconciliation import require_operator
from codex_web.storage.storage_preservation import import_rows, StorageSnapshotLimits, StateStorageSnapshotV1, canonical
from codex_web.storage.state_store import check_operation_budget
from codex_web.services.recovery_storage_preservation import seal_storage_snapshot, open_storage_snapshot


def fingerprint(request):
    return hashlib.sha256(canonical(request.model_dump(mode='json'))).hexdigest()


class StoragePreservationOperations:
    def __init__(self, recovery, configuration, resources, resolve_actor):
        self.recovery, self.configuration, self.resources = recovery,configuration,resources
        self.resolve_actor = resolve_actor

    def current(self, payload, actor, *, capacity=True):
        actor = self.resolve_actor(actor.identity_id)
        require_operator(actor)
        config,pins = preservation_configuration(self.configuration)
        if payload.resource_id != config['resource_id']:
            raise RecoveryConflictError('Exact configured whole-StateStore Resource required')
        resource = self.resources.get(payload.resource_id,actor)
        if resource.resource_type != ResourceType.DATABASE or resource.lifecycle != ResourceLifecycle.ACTIVE:
            raise RecoveryConflictError('Current database Resource required')
        state = self.recovery.store.load()
        RecoveryPolicyControl(self.recovery).require_scope(state,actor)
        if state.policy is None or state.policy.fingerprint()!=payload.expected_policy_fingerprint:
            raise RecoveryConflictError('Recovery policy changed')
        key = self.recovery.crypto.get_key(state.policy.backup_key_id,actor)
        RecoveryPolicyControl(self.recovery).validate_backup_key(key)
        if capacity:
            self.capacity(payload,actor,config,pins)
        return actor,config,pins,key

    def capacity(self,payload,actor,config,pins,*,starting=True):
        evidence = self.recovery.evidence.get_evidence(payload.capacity_evidence_id,actor=actor)
        state = self.recovery.evidence.store.load()
        proof = next((v for v in state.verifications if v.id==payload.capacity_verification_id),None)
        now = time.time()
        if (evidence.lifecycle!=EvidenceLifecycle.VALID or proof is None
            or proof.organization_id!=actor.organization_id or proof.workspace_id!=actor.workspace_id
            or proof.result!=VerificationResult.VERIFIED or not proof.independent
            or proof.verifier_identity_id==evidence.producer_identity_id
            or evidence.id not in proof.evidence_ids or proof.verified_at<evidence.observed_at
            or evidence.observed_at>now or proof.verified_at>now
            or now-evidence.observed_at>(60 if starting else config['wall_seconds']+60)
            or evidence.retention_expires_at is not None and evidence.retention_expires_at<=now
            or payload.resource_id not in evidence.resource_ids):
            raise RecoveryConflictError('Fresh independently verified resource capacity Evidence required')
        m=evidence.metadata
        expected={'contract':CAPACITY_CONTRACT,'resource_id':payload.resource_id,
                  'configuration_sha256':hashlib.sha256(canonical(pins)).hexdigest()}
        if any(type(m.get(k)) is not type(v) or m.get(k)!=v for k,v in expected.items()):
            raise RecoveryConflictError('Capacity proof does not bind current Resource/configuration')
        if type(m.get('expires_at')) not in (int,float) or m['expires_at'] < now+(config['wall_seconds'] if starting else 0):
            raise RecoveryConflictError('Capacity reservation proof expires before operation budget')
        for name in ('memory_bytes','disk_bytes','wall_seconds'):
            if type(m.get(name)) is not int or m[name]<config[name]:
                raise RecoveryConflictError('Capacity proof does not cover effective operation budget')
        for name in ('database_reservation_directory','database_directory'):
            if not isinstance(m.get(name),str) or not Path(m[name]).is_absolute():
                raise RecoveryConflictError('Verified same-host database filesystem mapping required')
        return m

    def committed(self,payload,actor,*,intent_id,action_id,binding_id):
        actor,config,pins,key=self.current(payload,actor,capacity=False)
        found=self.recovery.evidence.find_idempotent_evidence(actor=actor,source=SOURCE,idempotency_key=intent_id)
        if found is None:
            return None
        expected={'intent_id':intent_id,'action_id':action_id,'binding_id':binding_id,
                  'request_sha256':fingerprint(payload),'idempotency_key':payload.idempotency_key,'operation_id':payload.operation_id,
                  'resource_id':payload.resource_id,'integrity_status':'verified'}
        StoragePreservationReportV1.model_validate_json(found.metadata.get('report_json',''))
        if found.lifecycle!=EvidenceLifecycle.VALID or any(found.metadata.get(k)!=v for k,v in expected.items()):
            raise RecoveryConflictError('Committed preservation receipt/request mismatch')
        if len(found.artifact_ids)!=1:
            raise RecoveryConflictError('Committed preservation Artifact unavailable')
        artifact=self.recovery.evidence.get_artifact(found.artifact_ids[0],actor=actor)
        if (artifact.source!=SOURCE or artifact.resource_ids!=(payload.resource_id,)
            or artifact.content is None or artifact.digest is None or artifact.lifecycle!='active'):
            raise RecoveryConflictError('Committed preservation content unavailable')
        if artifact.metadata.get('request_sha256')!=found.metadata.get('capture_request_sha256'):
            raise RecoveryConflictError('Preservation immutable source binding mismatch')
        return found

    def inventory(self):
        """Metadata only, same snapshot as sizing; never documents()/JSON decode."""
        source=self.recovery.state_store
        pg=hasattr(source,'dsn')
        with source._connection(**({'read_only_snapshot':True} if pg else {})) as connection:
            if not pg:
                connection.execute('BEGIN')
            header=StateStorageSnapshotV1(backend='postgres' if pg else 'sqlite',
                storage_schema_version=source.SCHEMA_VERSION)
            stats={'rows':0,'payload_bytes':0,'max_value_bytes':0,'key_bytes':0,
                   'framed_bytes':8+len(canonical(header.model_dump(mode='json')))}
            for table,key,value in (('documents','namespace','payload'),('metadata','key','value')):
                name=('codex_' if pg else '')+'state_'+table
                size=f'octet_length({value})' if pg else f'length(CAST({value} AS BLOB))'
                keysize=f'octet_length({key})' if pg else f'length(CAST({key} AS BLOB))'
                cursor=connection.cursor()
                try:
                    cursor.execute(f'SELECT COUNT(*),COALESCE(SUM({size}),0),COALESCE(MAX({size}),0),COALESCE(SUM({keysize}),0) FROM {name}')
                    rows,total,maximum,keys=cursor.fetchone()
                    stats['rows']+=int(rows);stats['payload_bytes']+=int(total)
                    stats['max_value_bytes']=max(stats['max_value_bytes'],int(maximum));stats['key_bytes']+=int(keys)
                finally:
                    cursor.close()
            limits=StorageSnapshotLimits()
            if (stats['rows']>limits.max_rows or stats['max_value_bytes']>limits.max_value_bytes
                or stats['payload_bytes']>limits.max_payload_bytes):
                raise RecoveryConflictError('Current storage exceeds preservation format')
            # Key/length-only, bounded server cursor: ordinary PG iteration
            # would still buffer the entire key catalog in libpq. No JSON or
            # payload TEXT is selected while measuring exact framing lengths.
            for table,key,value in (('documents','namespace','payload'),('metadata','key','value')):
                name=('codex_' if pg else '')+'state_'+table
                size=f'octet_length({value})' if pg else f'length(CAST({value} AS BLOB))'
                cursor=connection.cursor(name='preservation_inventory_'+uuid.uuid4().hex) if pg else connection.cursor()
                try:
                    cursor.execute(f'SELECT {key},{size} FROM {name}')
                    while True:
                        batch=cursor.fetchmany(64)
                        if not batch:break
                        for raw_key,length in batch:
                            check_operation_budget()
                            if len(raw_key.encode('utf8'))>limits.max_key_bytes:
                                raise RecoveryConflictError('Storage key exceeds preservation format')
                            item={'kind':'row','table':table,'key':raw_key,
                                  'updated_at_b64':'AAAAAAAAAAA=','value_b64':''}
                            frame=len(canonical(item))+4*((int(length)+2)//3)
                            if frame>limits.max_record_bytes:
                                raise RecoveryConflictError('Encoded row exceeds preservation format')
                            stats['framed_bytes']+=8+frame
                finally:cursor.close()
            stats['framed_bytes']+=8+len(canonical({'kind':'end','rows':stats['rows'],
                'payload_bytes':stats['payload_bytes'],'raw_sha256':'0'*64}))
            if stats['framed_bytes']>limits.max_plaintext_bytes:
                raise RecoveryConflictError('Storage framing exceeds preservation format')
            if pg:
                cursor=connection.cursor()
                try:
                    cursor.execute('SHOW data_directory')
                    stats['_database_directory']=cursor.fetchone()[0]
                    cursor.execute("SELECT pg_total_relation_size('codex_state_documents')+pg_total_relation_size('codex_state_metadata')")
                    stats['physical_database_bytes']=int(cursor.fetchone()[0])
                finally:
                    cursor.close()
            else:
                # File/WAL byte metadata only, never a payload read. The
                # reservation also covers a growing consistent source stream.
                stats['physical_database_bytes']=source.path.stat().st_size
                wal=Path(str(source.path)+'-wal')
                if wal.exists():stats['physical_database_bytes']+=wal.stat().st_size
            return stats

    @contextmanager
    def reservation(self,payload,actor,config,pins,backend,filesystem):
        from codex_web.services.local_artifact_content import LocalArtifactContentStore
        if not isinstance(backend,LocalArtifactContentStore):
            raise RecoveryConflictError('Current preservation requires local protected content disk admission')
        stats=self.inventory()
        limits=StorageSnapshotLimits()
        if stats['rows']>limits.max_rows or stats['max_value_bytes']>limits.max_value_bytes or stats['payload_bytes']>limits.max_payload_bytes:
            raise RecoveryConflictError('Current storage exceeds preservation format')
        # Conservative bounded framing/JSON/base64 overhead, never reserve the
        # entire defensive 6GiB ceiling for a small real source.
        # Headroom is reserved and enforced, not guessed unused capacity:
        # the actual later capture is its own consistent snapshot; concurrent
        # canonical audit/reservation growth cannot overrun admitted bytes.
        raw_bound=min(limits.max_plaintext_bytes,stats['framed_bytes']*105//100+65536)
        from codex_web.services.recovery_storage_preservation import CHUNK_BYTES,MAX_ENVELOPE,MAX_MANIFEST
        cipher_bound=((raw_bound+CHUNK_BYTES-1)//CHUNK_BYTES)*MAX_ENVELOPE+2*MAX_MANIFEST+65536
        target_bound=max(stats['payload_bytes']*3+stats['key_bytes']*4+stats['rows']*512,
                         stats['physical_database_bytes']*2+stats['payload_bytes']+stats['rows']*512)
        memory_bound=stats['max_value_bytes']*12+96*1024**2
        if memory_bound>config['memory_bytes']:
            raise RecoveryConflictError('Current maximum row exceeds admitted memory estimate')
        proof=self.capacity(payload,actor,config,pins)
        def protected_directory(value):
            original=Path(value).absolute()
            # Inspect before resolve: checking is_symlink on a resolved path
            # would silently erase the evidence. No operational mapping can
            # bypass the canonical binding's filesystem boundary.
            if any(path.is_symlink() for path in (original,*original.parents)):
                raise RecoveryConflictError('Preservation directory symlink refused')
            resolved=original.resolve(strict=True)
            if not resolved.is_dir():
                raise RecoveryConflictError('Preservation directory unavailable')
            return resolved
        content_root=protected_directory(backend.root)
        database=protected_directory(proof['database_directory'])
        reservation=protected_directory(proof['database_reservation_directory'])
        if not hasattr(self.recovery.state_store,'dsn'):
            actual=self.recovery.state_store.path.parent.resolve(strict=True)
        else:
            actual=Path(stats['_database_directory']).resolve(strict=True)
        if actual!=database or actual.stat().st_dev!=reservation.stat().st_dev:
            raise RecoveryConflictError('Database reservation filesystem mapping changed')
        permitted=[Path(root).resolve(strict=True) for root in filesystem.allowed_write_roots]
        for path in (content_root,reservation):
            if not any(path.is_relative_to(root) for root in permitted):
                raise RecoveryConflictError('Reservation path outside canonical binding filesystem policy')
        requirements={}
        for root,amount in [(content_root,cipher_bound*2),(reservation,target_bound)]:
            device=root.stat().st_dev
            prior=requirements.get(device)
            requirements[device]=(root,amount+(prior[1] if prior else 0))
        # Keep reservations until all real I/O joins. Peak charge includes BOTH
        # held reservation and content/target writes; this is conservative disk
        # reservation, not a claim of an operation-specific memory cgroup.
        charged=2*sum(n for _,n in requirements.values())
        if charged>config['disk_bytes']:
            raise RecoveryConflictError('Insufficient admitted preservation disk budget')
        files=[]
        try:
            for root,amount in requirements.values():
                check_operation_budget()
                if shutil.disk_usage(root).free<2*amount:
                    raise RecoveryConflictError('Insufficient current filesystem free bytes')
                fd,path=tempfile.mkstemp(prefix='.recovery-preservation-reserve-',dir=root)
                files.append((fd,path))
                os.fchmod(fd,0o600)
                os.posix_fallocate(fd,0,amount)
                os.fsync(fd)
            yield {**stats,'_raw_bound':raw_bound,'_cipher_bound':cipher_bound,
                   '_target_bound':target_bound,'reserved_peak_bytes':charged,
                   'memory_estimate_bytes':memory_bound}
        finally:
            for fd,path in files:
                try:
                    os.close(fd)
                finally:
                    os.unlink(path)

    @contextmanager
    def isolated_target(self,operation_id,root):
        source=self.recovery.state_store
        if not hasattr(source,'dsn'):
            from codex_web.storage.sqlite_state import SQLiteStateStore
            with tempfile.TemporaryDirectory(prefix='.preservation-isolated-',dir=root) as temporary:
                yield SQLiteStateStore(Path(temporary)/'isolated.sqlite')
            return
        from psycopg import sql
        from codex_web.storage.postgres_state import PostgresStateStore
        schema='preserve_'+hashlib.sha256(operation_id.encode()).hexdigest()[:40]
        owned=False
        # No application startup or source-table mutation; exact generated
        # namespace is reserved by this operation, never adopted after a crash.
        with source._connect_factory(source.dsn) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            owned=True
        def connect(dsn):
            connection=source._connect_factory(dsn)
            try:
                connection.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
                connection.commit()
                return connection
            except BaseException:
                connection.close();raise
        target=None
        try:
            target=PostgresStateStore(source.dsn,connect=connect)
            if getattr(target,'_pool',None) is getattr(source,'_pool',object()):
                raise RecoveryConflictError('Isolated target must not share owning source pool')
            yield target
        finally:
            if target is not None:
                target.close()
            if owned:
                with source._connect_factory(source.dsn) as connection:
                    connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def _context(self,payload,actor):
        return EncryptionContext(organization_id=actor.organization_id,workspace_id=actor.workspace_id,
            object_type='recovery_storage_preservation',object_id=payload.operation_id,resource_id=payload.resource_id)

    def _assess(self,chunks,source_backend):
        invalid=[];count=invalid_count=0
        digest=hashlib.sha256()
        def observed():
            for chunk in chunks:
                digest.update(chunk);yield chunk
        for table,key,value,timestamp in import_rows(observed(),backend=source_backend,
                schema_version=self.recovery.state_store.SCHEMA_VERSION,limits=StorageSnapshotLimits()):
            check_operation_budget();count+=1
            if table=='documents':
                try:
                    json.loads(value)
                except ValueError:
                    invalid_count+=1
                    if len(invalid)<100:
                        raw=value.encode('utf8')
                        invalid.append({'reference':'storage-key-sha256:'+hashlib.sha256(key.encode('utf8')).hexdigest(),'bytes':len(raw),
                            'sha256':hashlib.sha256(raw).hexdigest(),'error_code':'json_decode_error'})
        return {'rows':count,'raw_stream_sha256':digest.hexdigest(),
                'semantic_status':'invalid' if invalid_count else 'unknown',
                'invalid_json_rows':invalid_count,'diagnostics':invalid,
                'diagnostics_bounded':True,'diagnostics_truncated':invalid_count>len(invalid)}

    def execute(self,payload,actor,*,intent_id,action_id,binding_id,admission,filesystem):
        service=self.recovery
        actor,config,pins,key=self.current(payload,actor)
        existing=self.committed(payload,actor,intent_id=intent_id,action_id=action_id,binding_id=binding_id)
        if existing is not None:
            return existing
        evidence_service=service.evidence
        store=evidence_service.store
        if any(r.store is not service.state_store for r in (store,service.store,self.configuration.store,self.resources.store,service.crypto.store)):
            raise RecoveryConflictError('Preservation settlement requires shared canonical store')
        state=store.load()
        if any(a.metadata.get('preservation_intent_id')==intent_id for a in state.artifacts):
            raise RecoveryConflictError('Reserved preservation operation outcome unknown; observe only')
        content=evidence_service._require_content_service()
        backend_id=evidence_service._content_backend_id(actor,None)
        backend=content.backend(backend_id)
        with self.reservation(payload,actor,config,pins,backend,filesystem) as inventory:
            if action_id==CAPTURE:
                artifact=evidence_service.create_artifact(ArtifactCreate(artifact_type=ArtifactType.REPORT,
                    name='Opaque StateStore preservation',resource_ids=(payload.resource_id,),source=SOURCE,
                    external_id=intent_id,classification=DataClassification.RESTRICTED,deny_model_context=True,
                    metadata={'preservation_intent_id':intent_id,'request_sha256':fingerprint(payload),
                              'operation_id':payload.operation_id,'resource_id':payload.resource_id,
                              'state':'reserved_unknown_until_verified'}),actor=actor)
                def bounded_source():
                    consumed=0
                    source=service.state_store.iter_storage_snapshot()
                    try:
                        for chunk in source:
                            consumed+=len(chunk)
                            if consumed>inventory['_raw_bound']:
                                raise RecoveryConflictError('Snapshot grew beyond reserved framing; observe custody, do not replay')
                            yield chunk
                    finally:source.close()
                encrypted=seal_storage_snapshot(bounded_source(),crypto=service.crypto,
                    key_id=key.id,context=self._context(payload,actor),actor=actor)
                result=content.put(ArtifactContentScope(organization_id=actor.organization_id,workspace_id=actor.workspace_id),
                    encrypted,backend_id=backend_id,media_type='application/vnd.codex.state-storage-preservation')
                pointer=ArtifactContentPointer(backend_id=result.backend_id,locator=result.locator,
                    size_bytes=result.size_bytes,media_type=result.media_type,encryption_key_ref=key.id)
                # Retain the completed encrypted object's infrastructure
                # reference as protected UNKNOWN custody before further I/O.
                # This is not an available content pointer or integrity receipt.
                # A cancelled/revoked observer must not lose the actual write.
                reserved=artifact.model_copy(update={'metadata':{**artifact.metadata,
                    'pending_content_json':pointer.model_dump_json(),
                    'pending_content_sha256':result.sha256}})
                def retain(state):
                    selected=next((a for a in state.artifacts if a.id==artifact.id),None)
                    if selected!=artifact:
                        raise RecoveryConflictError('Reserved preservation custody changed')
                    state.artifacts=[reserved if a.id==artifact.id else a for a in state.artifacts]
                    return state
                # Custody-only append; no availability, authority or semantic
                # success is inferred, even if final admission is later denied.
                store.update(retain)
                artifact=reserved
                capture_sha=fingerprint(payload)
                capture_context=self._context(payload,actor)
                content_sha=result.sha256
            else:
                artifact=evidence_service.get_artifact(payload.artifact_id,actor=actor)
                if (artifact.source!=SOURCE or artifact.content is None or artifact.digest is None
                    or artifact.resource_ids!=(payload.resource_id,)):
                    raise RecoveryConflictError('Exact scoped preservation Artifact required')
                pointer=artifact.content;content_sha=artifact.digest.value
                if pointer.backend_id!=backend_id:
                    raise RecoveryConflictError('Preservation content backend differs from admitted disk')
                capture_sha=artifact.metadata['request_sha256']
                capture_context=self._context(payload,actor).model_copy(update={'object_id':artifact.metadata['operation_id']})
            scope=ArtifactContentScope(organization_id=actor.organization_id,workspace_id=actor.workspace_id)
            def opened():
                def ciphertext():
                    digest=hashlib.sha256();size=0
                    stream=content.open(scope,backend_id=pointer.backend_id,locator=pointer.locator)
                    try:
                        for chunk in stream:
                            check_operation_budget();size+=len(chunk)
                            if size>pointer.size_bytes:
                                raise RecoveryConflictError('Preservation content size changed')
                            digest.update(chunk);yield chunk
                        if size!=pointer.size_bytes or digest.hexdigest()!=content_sha:
                            raise RecoveryConflictError('Preservation content digest changed')
                    finally:
                        close=getattr(stream,'close',None)
                        if callable(close):close()
                return open_storage_snapshot(ciphertext(),crypto=service.crypto,context=capture_context,actor=actor)
            # Authenticate/count/diagnose separately; no payload normalized and
            # no valid JSON alone is labelled a usable domain restore.
            report=self._assess(opened(),'postgres' if hasattr(service.state_store,'dsn') else 'sqlite')
            if action_id==VERIFY:
                with self.isolated_target(intent_id,backend.root) as target:
                    target.restore_storage_snapshot(opened())
                    restored=hashlib.sha256()
                    for chunk in target.iter_storage_snapshot(): restored.update(chunk)
                    if restored.hexdigest()!=report['raw_stream_sha256']:
                        raise RecoveryConflictError('Isolated storage row digest mismatch')
                report['isolated_restore_verified']=True
            else:
                report['isolated_restore_verified']=False
            report.update(integrity_status='verified',semantic_recovery_qualified=False,
                source_snapshot_rows_preserved=True,source_crypto_audit_appends=True,
                reserved_peak_bytes=inventory['reserved_peak_bytes'],memory_enforcement='cooperative_estimate_not_job_cgroup')
            report=StoragePreservationReportV1.model_validate(report).model_dump(mode='json')
            check_operation_budget()
            now=time.time()
            metadata={'intent_id':intent_id,'action_id':action_id,'binding_id':binding_id,
                'request_sha256':fingerprint(payload),'capture_request_sha256':capture_sha,
                'idempotency_key':payload.idempotency_key,'request_json':payload.model_dump_json(),
                'operation_id':payload.operation_id,'resource_id':payload.resource_id,
                'integrity_status':'verified','semantic_status':report['semantic_status'],
                'report_json':json.dumps(report,sort_keys=True,separators=(',',':'))}
            domains={**admission['guard_rows'],store.namespace:(None,),service.store.namespace:(None,),
                     service.crypto.store.namespace:(None,),self.configuration.store.namespace:(None,)}
            def commit(updater):
                def apply(rows):
                    check_operation_budget()
                    admission['guard'](None)
                    current_actor,current_config,current_pins,current_key=self.current(payload,actor,capacity=False)
                    self.capacity(payload,current_actor,config,pins,starting=False)
                    if content.backend(backend_id) is not backend or evidence_service._content_backend_id(current_actor,None)!=backend_id:
                        raise RecoveryConflictError('Artifact content backend changed')
                    if current_config!=config or current_pins!=pins or current_key.id!=key.id or current_key.current_version!=key.current_version:
                        raise RecoveryConflictError('Preservation configuration or key changed')
                    current=store._decode(rows[store.namespace][None])
                    selected=next((a for a in current.artifacts if a.id==artifact.id),None)
                    if selected!=artifact:
                        raise RecoveryConflictError('Preservation Artifact changed before commit')
                    if action_id==CAPTURE:
                        changed=artifact.model_copy(update={'content':pointer,'digest':ArtifactDigest(value=content_sha),
                            'metadata':{k:v for k,v in {**artifact.metadata,'state':'verified_storage_only'}.items()
                                        if not k.startswith('pending_content_')}})
                        current.artifacts=[changed if a.id==artifact.id else a for a in current.artifacts]
                    current=updater(current)
                    admission['guard'](None)
                    return {store.namespace:{None:current.model_dump(mode='json')}}
                service.state_store.record_mutate_many(domains,apply)
            return evidence_service.create_evidence(EvidenceCreate(evidence_type=EvidenceType.ARTIFACT_VERIFICATION,
                artifact_ids=(artifact.id,),resource_ids=(payload.resource_id,),source=SOURCE,
                result=EvidenceResult.WARN,summary='Verified storage integrity; semantic Recovery remains unqualified',
                observed_at=now,classification=DataClassification.RESTRICTED,deny_model_context=True,
                metadata=metadata),actor=actor,idempotency_key=intent_id,_commit=commit)
