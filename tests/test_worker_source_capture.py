"""Private canonical worker/Work/content/governance; no provider/native input.

The transport fixture's fixed sandbox capture observation is controlled; separate
program tests exercise real temporary Git objects inside Root's NoApp boundary.
Neither is a claim of live worker qualification.
"""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_worker_verifications as fixtures


def observation(body=b'canonical source\n'):
    blob = hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()
    item = dict(path='component-map.json',blob_sha=blob,sha256=hashlib.sha256(body).hexdigest())
    return item,dict(method='repository-source-capture.v1',commit_sha='a'*40,
        files=[{**item,'size_bytes':len(body),'content_base64':base64.b64encode(body).decode()}])


class WorkerSourceCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = f = fixtures.WorkerVerificationTests()
        f.addCleanup,f.addAsyncCleanup = self.addCleanup,self.addAsyncCleanup
        await f.asyncSetUp()
        from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
        from codex_web.services.local_artifact_content import LocalArtifactContentStore
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
        registry = ArtifactContentRegistry()
        registry.register(LocalArtifactContentStore(Path(f.temp.name)/'source-content'))
        f.evidence.content = ArtifactContentService(registry)
        f.evidence.governance = DataGovernanceService(DataGovernanceStore(f.sqlite))
        f.evidence.governance.store.load()
        item,self.captured = observation()
        self.payload = dict(method='repository-source-capture.v1',request_id='1'*32,
            expected_fence=f.assignment.fence,work_item_ref=f.ref,
            repository_resource_id=f.target.id,commit_sha='a'*40,files=[item])
        self.calls = []
        def capture(assignment,**options):
            self.calls.append((assignment,options))
            options['poll_hook']()
            return json.loads(json.dumps(self.captured))
        self.runtime = LocalExecutionWorkerRuntime(f.workers,f.workspaces,
            SimpleNamespace(capture_repository_source=capture),worker=f.worker,
            worker_actor=f.worker_actor,control_actor=f.admin,artifact_evidence=f.evidence)
        f.service.local_execution_worker = self.runtime
        self.initial = f.evidence.store.load()

    async def post(self,payload=None,repository=None):
        return await self.f._request('POST','/api/repository-source-captures?repository_resource_id='+
            (repository or self.f.target.id),payload=self.payload if payload is None else payload)

    async def test_admitted_worker_captures_governed_source_info_without_operator_ingest(self):
        status,_,body = await self.post()
        self.assertEqual(status,200,body)  # OLD must fail here, not on a missing new API import.
        artifact = self.f.evidence.get_artifact(body['artifact_id'],actor=self.f.worker_actor)
        proof = self.f.evidence.get_evidence(body['evidence_id'],actor=self.f.worker_actor)
        self.assertEqual(artifact.producer_identity_id,self.f.worker_actor.identity_id)
        self.assertEqual(proof.producer_identity_id,self.f.worker_actor.identity_id)
        self.assertEqual(artifact.resource_ids,(self.f.target.id,))
        self.assertEqual(artifact.work_item_ref,self.f.ref)
        self.assertEqual(proof.artifact_ids,(artifact.id,))
        self.assertEqual(proof.digest,artifact.digest)
        self.assertEqual(proof.result,'info')
        self.assertEqual(proof.evidence_type,'artifact_verification')
        self.assertIsNotNone(artifact.governance_record_id)
        self.assertIsNotNone(proof.governance_record_id)
        self.assertEqual(artifact.metadata['assignment_fence'],self.f.assignment.fence)
        pointer,chunks = self.f.evidence.open_artifact_content(artifact.id,actor=self.f.worker_actor)
        self.assertEqual(json.loads(b''.join(chunks)),self.captured)
        self.assertEqual(pointer.size_bytes,len(json.dumps(self.captured,sort_keys=True,separators=(',',':')).encode()))
        self.assertTrue(body['source_information_only'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.f.evidence.store.load().verifications,self.initial.verifications)

    async def test_exact_read_lease_captures_without_any_write_authority(self):
        from codex_web.resources import RepositoryExecutionScope
        current = self.f.assignment
        def read_workspace(state):
            workspace = next(w for w in state.workspaces if w.id==current.execution_workspace_id)
            workspace.read_only_primary_repository_id = self.f.target.id
            workspace.writable_repository_ids = ()
            workspace.repository_members = tuple(m.model_copy(update={'access_mode':'read'})
                for m in workspace.repository_members)
            lease = next(l for l in state.leases if l.id==workspace.lease_id)
            lease.mode = 'read'
            lease.resource_modes = {self.f.target.id:'read'}
            return state
        self.f.workspaces.store.update(read_workspace)
        scope = RepositoryExecutionScope(organization_id=current.organization_id,workspace_id=current.workspace_id,
            project_id=current.project_id,read_only_repository_ids=(self.f.target.id,),
            write_mode='read_only',source='work_item')
        def read_assignment(state):
            state.assignments[0].repository_scope = scope
            state.assignments[0].sandbox = 'read-only'
            return state
        self.f.workers.store.update_assignment(current.assigned_worker_id,current.id,read_assignment)
        status,_,body = await self.post()
        self.assertEqual(status,200,body)
        self.assertEqual(len(self.calls),1)
        observed = self.calls[0][0]
        self.assertEqual(observed.repository_scope.writable_repository_ids,())
        self.assertEqual(observed.sandbox,'read-only')

    async def test_operation_descriptor_reports_metadata_scope_without_claiming_admission(self):
        report = self.f.service.public_assignment_capability(self.f.assignment)
        operation = next(o for o in report['operations'] if o['id']=='repository.source.capture')
        self.assertEqual(operation['admitted_repository_resource_ids'],[self.f.target.id])
        self.assertTrue(operation['metadata_compatible'])
        self.assertFalse(operation['admission_checked'])
        self.assertEqual(operation['max_content_bytes'],65536)
        scratch = self.f.assignment.model_copy(update={'repository_scope':None,'resource_ids':()})
        unavailable = self.f.service.public_assignment_capability(scratch)
        item = next(o for o in unavailable['operations'] if o['id']=='repository.source.capture')
        self.assertFalse(item['metadata_compatible'])
        self.assertIn('context_or_worker_missing',item['blocked_reason'])
        self.assertFalse(item['admission_checked'])
        self.assertEqual(self.calls,[])

    async def test_expired_assignment_lease_refuses_before_capture(self):
        def expire(state):
            lease = state.assignments[0].lease
            state.assignments[0].lease = type(lease).model_validate({**lease.model_dump(),
                'acquired_at':lease.acquired_at-100,'expires_at':lease.acquired_at-1,'renewed_at':None})
            return state
        self.f.workers.store.update_assignment(self.f.assignment.assigned_worker_id,self.f.assignment.id,expire)
        status,_,body = await self.post()
        self.assertEqual(status,403,body)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_current_work_owner_change_refuses_before_capture(self):
        state = self.f.items.get(self.f.ref)
        self.f.items.put(self.f.ref,state.model_copy(update={'current_owner':'other-owner'}))
        status,_,body = await self.post()
        self.assertEqual(status,403,body)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_same_request_joins_original_ids_without_second_capture(self):
        first = await self.post()
        self.assertEqual(first[0],200,first[2])
        second = await self.post()
        self.assertEqual(second[0],200,second[2])
        for field in ('artifact_id','evidence_id','request_sha256','content_sha256'):
            self.assertEqual(first[2][field],second[2][field])
        self.assertTrue(second[2]['deduplicated'])
        self.assertEqual(len(self.calls),1)

    async def test_response_loss_after_canonical_commit_joins_original_without_recapture(self):
        with patch.object(self.f.service,'_audit',side_effect=OSError('controlled lost response after commit')):
            status,_,body = await self.post()
        self.assertNotEqual(status,200,body)
        before = self.f.evidence.store.load()
        status,_,body = await self.post()
        self.assertEqual(status,200,body)
        self.assertTrue(body['deduplicated'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.f.evidence.store.load(),before)

    async def test_changed_artifact_cannot_borrow_immutable_original_info_digest(self):
        status,_,body = await self.post()
        self.assertEqual(status,200,body)
        from codex_web.artifact_evidence import sha256_digest
        def change(state):
            item = next(a for a in state.artifacts if a.id==body['artifact_id'])
            item.digest = sha256_digest(b'changed source bundle')
            return state
        self.f.evidence.store.update(change)
        before = self.f.evidence.store.load()
        status,_,body = await self.post()
        self.assertEqual(status,403,body)
        self.assertEqual(self.f.evidence.store.load(),before)
        self.assertEqual(len(self.calls),1)

    async def test_same_identity_changed_payload_preserves_original(self):
        first = await self.post()
        self.assertEqual(first[0],200,first[2])
        before = self.f.evidence.store.load()
        status,_,body = await self.post({**self.payload,'commit_sha':'b'*40})
        self.assertEqual(status,403,body)
        self.assertEqual(self.f.evidence.store.load(),before)
        self.assertEqual(len(self.calls),1)

    async def test_foreign_scope_fence_work_and_paths_never_capture(self):
        for update, expected_status in (({'expected_fence':self.f.assignment.fence+1},403),
                ({'work_item_ref':'foreign/repository!9'},403),
                ({'files':[{**self.payload['files'][0],'path':'../outside'}]},422),
                ({'files':[self.payload['files'][0]]*9},413)):
            with self.subTest(update=update):
                status,_,body = await self.post({**self.payload,**update})
                self.assertEqual(status,expected_status,body)
        status,_,body = await self.post(repository='foreign-resource')
        self.assertEqual(status,403,body)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_bad_worker_bytes_and_oversize_never_persist(self):
        for body in (b'other source',b'x'*65537):
            with self.subTest(size=len(body)):
                self.captured['files'][0]['content_base64'] = base64.b64encode(body).decode()
                status,_,response = await self.post()
                self.assertEqual(status,403,response)
                self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_fence_drift_during_capture_refuses_all_records(self):
        original = self.runtime.backend.capture_repository_source
        def drift(assignment,**options):
            result = original(assignment,**options)
            def change(state):
                current = state.assignments[0]
                lease = current.lease
                current.fence += 1
                current.lease = type(lease).model_validate({**lease.model_dump(),
                    'fence':current.fence,'lease_token':'private-successor-lease-token-long'})
                return state
            # A coherent private successor incarnation, not cancel_bootstrap:
            # this borrowed canonical assignment has a Work-item subject.
            self.f.workers.store.update_assignment(assignment.assigned_worker_id,assignment.id,change)
            return result
        self.runtime.backend.capture_repository_source = drift
        status,_,body = await self.post()
        self.assertEqual(status,403,body)
        current = self.f.workers.store.assignment(self.f.assignment.id)
        self.assertEqual(current.fence,self.f.assignment.fence+1)
        self.assertEqual(current.lease.fence,current.fence)
        self.assertEqual(self.f.evidence.store.load(),self.initial)
        # Separately exercise canonical owner I/O conflicts as typed refusal;
        # never count an invalid bootstrap cancellation as actual fence drift.
        def restore(state):
            state.assignments[0] = self.f.assignment.model_copy(deep=True)
            return state
        self.f.workers.store.update_assignment(current.assigned_worker_id,current.id,restore)
        from codex_web.services.execution_workers import WorkerConflictError,WorkerLeaseError
        for error in (WorkerConflictError,WorkerLeaseError):
            with self.subTest(error=error.__name__):
                def conflict(*args,**kwargs):
                    raise error('Controlled canonical custody conflict')
                self.runtime.backend.capture_repository_source = conflict
                status,_,body = await self.post()
                self.assertEqual(status,409,body)
                self.assertEqual(body['error']['code'],'source_capture_custody_or_content_refused')
                self.assertEqual(self.f.evidence.store.load(),self.initial)

    async def test_governance_failure_commits_no_partial_artifact_or_evidence(self):
        before = self.f.evidence.governance.store.load()
        register = self.f.evidence.governance.register_domain_record
        calls = []
        def fail_second(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:
                raise RuntimeError('controlled INFO governance failure')
            return register(*args,**kwargs)
        with patch.object(self.f.evidence.governance,'register_domain_record',side_effect=fail_second):
            status,_,body = await self.post()
        self.assertNotEqual(status,200,body)
        self.assertEqual(self.f.evidence.store.load(),self.initial)
        self.assertEqual(self.f.evidence.governance.store.load(),before)
        # Immutable CAS content may exist, but has no accepted metadata. The
        # SAME request settles through normal owners after the known failure.
        status,_,body = await self.post()
        self.assertEqual(status,200,body)

    async def test_expired_governance_blocks_original_without_recapture(self):
        status,_,body = await self.post()
        self.assertEqual(status,200,body)
        record = self.f.evidence.get_artifact(body['artifact_id'],actor=self.f.worker_actor)
        def deny(state):
            state.records = [r.model_copy(update={'deny_model_context':True}) if r.id==record.governance_record_id
                else r for r in state.records]
            return state
        self.f.evidence.governance.store.update(deny)
        status,_,body = await self.post()
        self.assertEqual(status,403,body)
        self.assertEqual(len(self.calls),1)


class SourceCaptureProgramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = {'PATH':'/usr/bin:/bin','HOME':str(self.root),'GIT_CONFIG_NOSYSTEM':'1',
            'GIT_CONFIG_GLOBAL':'/dev/null','GIT_NO_REPLACE_OBJECTS':'1','GIT_NO_LAZY_FETCH':'1'}
        self.git('init','-q')
        self.body = b'{"component":"canonical"}\n'
        (self.root/'component-map.json').write_bytes(self.body)
        self.git('add','component-map.json')
        self.git('-c','user.name=Private fixture','-c','user.email=fixture@example.invalid','commit','-qm','source')
        self.head = self.git('rev-parse','HEAD').strip()
        self.blob = self.git('rev-parse','HEAD:component-map.json').strip()

    def git(self,*args):
        return subprocess.run(['/usr/bin/git',*args],cwd=self.root,env=self.environment,
            check=True,capture_output=True,text=True,timeout=5).stdout

    def capture_request(self):
        from codex_web.source_capture import SourceCaptureRequest
        return SourceCaptureRequest.model_validate(dict(method='repository-source-capture.v1',
            request_id='a'*32,expected_fence=1,work_item_ref='fixture/repository!1',
            repository_resource_id='private-repository',commit_sha=self.head,
            files=[dict(path='component-map.json',blob_sha=self.blob,
                sha256=hashlib.sha256(self.body).hexdigest())]))

    def run_capture(self):
        from codex_web.source_capture import CAPTURE_PROGRAM
        request = self.capture_request()
        return subprocess.run([sys.executable,'-I','-S','-c',CAPTURE_PROGRAM,request.model_dump_json()],
            cwd=self.root,env=self.environment,capture_output=True,text=True,timeout=10)

    def test_real_commit_blob_and_selected_regular_file_match(self):
        result = self.run_capture()
        self.assertEqual(result.returncode,0,result.stderr)
        row = json.loads(result.stdout)['files'][0]
        self.assertEqual(base64.b64decode(row['content_base64']),self.body)
        self.assertEqual(row['blob_sha'],self.blob)

    def test_backend_uses_fixed_readonly_offline_program_without_runtime_mounts(self):
        from codex_web.local_execution_backend import BubblewrapExecutionBackend,LocalExecutionResult
        from tests import test_control_plane_broker as broker_fixtures
        assignment = broker_fixtures.ControlPlaneBrokerTests._assignment()
        before = assignment.model_copy(deep=True)
        backend = BubblewrapExecutionBackend()
        result = LocalExecutionResult(executable='private-controlled-capture',command_digest='sha256:'+'a'*64,
            exit_code=0,stdout=json.dumps(observation()[1]),stderr='',duration_seconds=0)
        guard = unittest.mock.Mock()
        with patch.object(backend,'run',return_value=result) as run:
            backend.capture_repository_source(assignment,workspace_path=self.root,source_path=self.root,
                request=self.capture_request(),poll_hook=guard)
        restricted = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(restricted.sandbox,'read-only')
        self.assertFalse(restricted.network.enabled)
        self.assertEqual(restricted.network.allowed_hosts,())
        self.assertEqual(options['argv'][:2],('/usr/bin/env','-i'))
        self.assertIn('/usr/bin/python3',options['argv'])
        self.assertNotIn('trusted_readonly_mounts',options)
        self.assertNotIn('trusted_writable_mounts',options)
        self.assertEqual(options['environment']['GIT_NO_REPLACE_OBJECTS'],'1')
        self.assertEqual(options['environment']['GIT_NO_LAZY_FETCH'],'1')
        guard.assert_called_once_with()
        self.assertEqual(assignment,before)

    def test_oversize_committed_source_refuses_before_publishing_bytes(self):
        self.body = b'x'*65537
        (self.root/'component-map.json').write_bytes(self.body)
        self.git('add','component-map.json')
        self.git('-c','user.name=Private fixture','-c','user.email=fixture@example.invalid','commit','-qm','oversize')
        self.head = self.git('rev-parse','HEAD').strip()
        self.blob = self.git('rev-parse','HEAD:component-map.json').strip()
        result = self.run_capture()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(result.stdout)

    def test_uncommitted_bytes_cannot_substitute_for_immutable_source(self):
        (self.root/'component-map.json').write_bytes(b'uncommitted replacement')
        result = self.run_capture()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(result.stdout)

    def test_worktree_symlink_is_never_followed(self):
        source = self.root/'component-map.json'
        source.unlink()
        outside = self.root/'outside'
        outside.write_bytes(self.body)
        source.symlink_to(outside)
        result = self.run_capture()
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(result.stdout)

    def test_stale_commit_and_changed_blob_refuse(self):
        for field in ('commit','blob'):
            with self.subTest(field=field):
                original = getattr(self,'head' if field=='commit' else 'blob')
                setattr(self,'head' if field=='commit' else 'blob','b'*40)
                result = self.run_capture()
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(result.stdout)
                setattr(self,'head' if field=='commit' else 'blob',original)

    def test_traversal_absolute_git_and_duplicate_paths_are_not_wire_authority(self):
        from codex_web.source_capture import SourceCaptureRequest
        base = dict(request_id='a'*32,expected_fence=1,work_item_ref='fixture/repository!1',
            repository_resource_id='private-repository',commit_sha=self.head)
        for path in ('../outside','/outside','.git/config','a//b','a/./b','a\\b'):
            with self.subTest(path=path),self.assertRaises(ValueError):
                SourceCaptureRequest(**base,files=[dict(path=path,blob_sha=self.blob,sha256='a'*64)])
        item = dict(path='component-map.json',blob_sha=self.blob,sha256='a'*64)
        with self.assertRaisesRegex(ValueError,'paths must be unique'):
            SourceCaptureRequest(**base,files=[item,item])


