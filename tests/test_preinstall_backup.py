from __future__ import annotations

import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tests import test_upgrades as fixture
from codex_web.identity import AuthenticationAssurance
from codex_web.preinstall_backup import OfflineCaptureProof, PreinstallBackupRuntime, compose_capture_runtime, qualify_capture
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.upgrades import UpgradeConflictError, UpgradePreflightError
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.upgrades import UpgradeDeploymentMode


class PreinstallBackupTests(unittest.IsolatedAsyncioTestCase):
    profile = fixture.UpgradeTests.profile
    create_plan = fixture.UpgradeTests.create_plan

    async def asyncSetUp(self):
        await fixture.UpgradeTests.asyncSetUp(self)
        self.actor = self.actor.model_copy(update={
            'assurance': AuthenticationAssurance.LOCAL_TRUSTED})
        self.service.service_instance_id = 'installed-instance'
        self.releases.release.build.source_revision = 'b' * 40
        self.service.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.state))
        health = self.recovery.health(actor=self.actor)
        self.recovery.health = lambda **_: health.model_copy(update={
            'recovery_qualified': False, 'rpo_satisfied': False,
            'blockers': ('rpo_not_satisfied',)})
        self.plan = self.create_plan(require_drain=True,
            profile=self.profile(deployment_mode=UpgradeDeploymentMode.LOCAL))
        self.plan = self.service.start_service_drain(self.plan.id, actor=self.actor,
            service_instance_id='installed-instance', source_revision='a' * 40,
            target_revision='b' * 40)
        self.plan = self.service.preflight(self.plan.id, actor=self.actor)

    async def asyncTearDown(self):
        await fixture.UpgradeTests.asyncTearDown(self)

    def qualify(self, plan=None, **kwargs):
        return qualify_capture(self.service, plan or self.plan, actor=self.actor,
            source_revision='a' * 40, target_revision='b' * 40,
            service_instance_id='installed-instance', now=1000.0, **kwargs)

    async def test_stale_recovery_allows_capture_but_never_deployment(self):
        self.assertFalse(self.plan.preflight.satisfied)
        binding = self.qualify()
        with self.assertRaises(UpgradePreflightError):
            self.service.qualify_local_deployment(self.plan.id, actor=self.actor,
                source_revision='a' * 40, target_revision='b' * 40,
                service_instance_id='installed-instance',
                preflight_evidence_id=self.plan.preflight.evidence_id)
        captured = self.service.capture_pre_upgrade_backup(self.plan.id,
            actor=self.actor, capture_context=binding)
        self.assertEqual(captured.pre_upgrade_backup_id, 'backup-1')
        self.assertEqual(self.recovery.backup_count, 1)
        with self.assertRaises(UpgradePreflightError):
            self.qualify(captured)

    async def test_unrelated_recovery_failure_is_not_exempted(self):
        health = self.recovery.health(actor=self.actor)
        self.recovery.health = lambda **_: health.model_copy(update={
            'blockers': ('scheduled_owner_unavailable',)})
        current = self.service.preflight(self.plan.id, actor=self.actor)
        with self.assertRaises(UpgradePreflightError):
            self.qualify(current)
        self.assertEqual(self.recovery.backup_count, 0)

    async def test_actual_installed_admission_is_not_replaced_by_empty_process(self):
        # The new process can see zero local counters; the real installed
        # preflight's positive native admission must still deny capture.
        self.service._update(self.plan.id, self.actor, lambda plan:
            plan.model_copy(update={'maintenance_mode': False}))
        with self.service.native_turn_admission('org-a', 'ws-a') as admitted:
            self.assertTrue(admitted)
            self.service.start_service_drain(self.plan.id, actor=self.actor,
                service_instance_id='installed-instance', source_revision='a'*40,
                target_revision='b'*40)
            current = self.service.preflight(self.plan.id, actor=self.actor)
            self.assertEqual(current.preflight.active_native_admissions, 1)
        # The local counter is now zero. That cannot replace the required
        # fresh installed-service observation with a known positive counter.
        with self.assertRaises(UpgradePreflightError):
            self.qualify(current)

    async def test_canonical_preflight_mismatch_and_stale_evidence_are_denied(self):
        with self.assertRaises(UpgradePreflightError):
            self.qualify(self.plan.model_copy(update={'admission_epoch': 999}))
        with self.assertRaises(UpgradePreflightError):
            qualify_capture(self.service, self.plan, actor=self.actor,
                source_revision='a'*40, target_revision='b'*40,
                service_instance_id='installed-instance', now=1031.0)

    async def test_current_durable_assignment_blocks_capture(self):
        self.workers._assignments.append(SimpleNamespace(status='running',
            organization_id='org-a', workspace_id='ws-a'))
        with self.assertRaises(UpgradePreflightError):
            self.qualify()

    async def test_plan_race_preserves_manifest_without_false_association(self):
        binding = self.qualify()
        original = self.recovery.create_backup
        def capture(**kwargs):
            manifest = original(**kwargs)
            self.service._update(self.plan.id, self.actor, lambda plan:
                plan.model_copy(update={'admission_epoch': plan.admission_epoch + 1}))
            return manifest
        self.recovery.create_backup = capture
        with self.assertRaises(UpgradeConflictError):
            self.service.capture_pre_upgrade_backup(self.plan.id,
                actor=self.actor, capture_context=binding)
        self.assertEqual(self.recovery.backup_count, 1)
        self.assertIsNone(self.service.get(self.plan.id, actor=self.actor).pre_upgrade_backup_id)
        with self.assertRaises(UpgradeConflictError):
            self.service.capture_pre_upgrade_backup(self.plan.id,
                actor=self.actor, capture_context=binding)
        self.assertEqual(self.recovery.backup_count, 1)

    def runtime(self, refresh=None):
        from codex_web.storage.identity_state import IdentityStateStore
        self.store = IdentityStateStore(self.state)
        async def manual(operation, *, authenticate, identity):
            self.assertIs(identity, self)
            return operation(authenticate())
        self.recovery.run_manual = manual
        self.events = []
        self.runtime_checks = 0
        def check():
            self.runtime_checks += 1
        return PreinstallBackupRuntime(self.service, self, lambda: self.actor,
            refresh or (lambda: self.service.preflight(self.plan.id, actor=self.actor)),
            check, lambda kind, data: self.events.append((kind, data)),
            'a'*40, 'b'*40, 'installed-instance', lambda: 1000.0)

    async def test_owned_runtime_refreshes_both_edges_and_never_reuses(self):
        runtime = self.runtime()
        plan = await runtime.capture_once()
        self.assertEqual(plan.pre_upgrade_backup_id, 'backup-1')
        self.assertEqual([kind for kind, _ in self.events],
            ['capture-started', 'manifest-created', 'associated'])
        self.assertEqual(self.runtime_checks, 4)
        with self.assertRaises(UpgradeConflictError):
            await runtime.capture_once()
        self.assertEqual(self.recovery.backup_count, 1)

    async def test_owned_runtime_rechecks_durable_work_before_capture(self):
        runtime = self.runtime()
        self.workers._assignments.append(SimpleNamespace(status='running',
            organization_id='org-a', workspace_id='ws-a', execution_contract_version='1.0'))
        with self.assertRaises(UpgradePreflightError):
            await runtime.capture_once()
        self.assertEqual(self.recovery.backup_count, 0)
        self.assertEqual(self.events, [])

    async def test_owned_runtime_retains_manifest_when_postcapture_drain_changes(self):
        runtime = self.runtime()
        original = self.recovery.create_backup
        def capture(**kwargs):
            result = original(**kwargs)
            self.workers._assignments.append(SimpleNamespace(status='running',
                organization_id='org-a', workspace_id='ws-a', execution_contract_version='1.0'))
            return result
        self.recovery.create_backup = capture
        with self.assertRaises(UpgradePreflightError):
            await runtime.capture_once()
        self.assertEqual(self.recovery.backup_count, 1)
        self.assertEqual([kind for kind, _ in self.events], ['capture-started', 'manifest-created'])
        self.assertIsNone(self.service.get(self.plan.id, actor=self.actor).pre_upgrade_backup_id)

    async def test_association_rechecks_authority_inside_identity_guard(self):
        from codex_web.services.identity import AuthorizationError
        from codex_web.storage.identity_state import IdentityStateStore
        runtime = self.runtime()
        calls = 0
        def authenticate():
            nonlocal calls
            calls += 1
            if calls == 4:  # actor entry, pre, post, then locked association
                raise AuthorizationError('Private fixture membership revoked')
            return self.actor
        runtime.authenticate = authenticate
        before = self.state.get(IdentityStateStore.namespace)
        with self.assertRaises(AuthorizationError):
            await runtime.capture_once()
        self.assertEqual(calls, 4)
        self.assertEqual(self.recovery.backup_count, 1)
        self.assertIsNone(self.service.get(self.plan.id, actor=self.actor).pre_upgrade_backup_id)
        self.assertEqual(self.state.get(IdentityStateStore.namespace), before)
        self.assertEqual([kind for kind, _ in self.events], ['capture-started', 'manifest-created'])

    async def test_factory_does_not_bootstrap_an_absent_operator_or_directories(self):
        from pathlib import Path
        from codex_web.services.identity import AuthenticationError
        from codex_web.storage.identity_state import IdentityStateStore
        missing_keys = Path(self.temp.name) / 'uncreated-keys'
        missing_backups = Path(self.temp.name) / 'uncreated-backups'
        before = self.state.get(IdentityStateStore.namespace)
        with self.assertRaises(AuthenticationError):
            compose_capture_runtime(self.state, operator_token='invalid-private-fixture-token',
                key_material_dir=missing_keys, backup_dir=missing_backups,
                refresh=lambda: self.fail('No unauthenticated HTTP'),
                check_runtime=lambda: self.fail('No authenticated runtime guard yet'),
                journal=lambda *_: self.fail('No capture'),
                source_revision='a'*40, target_revision='b'*40,
                service_instance_id='installed-instance')
        self.assertEqual(self.state.get(IdentityStateStore.namespace), before)
        self.assertFalse(missing_keys.exists())
        self.assertFalse(missing_backups.exists())

    async def test_factory_uses_existing_identity_and_real_encrypted_restore(self):
        from pathlib import Path
        from tests import test_recovery as recovery_fixture
        from codex_web.identity import TenantScope
        from codex_web.services.identity import IdentityService, AuthenticationError
        from codex_web.storage.identity_state import IdentityStateStore
        from codex_web.storage.upgrades import UpgradeStore
        from codex_web.storage.sqlite_state import SQLiteStateStore
        fixture = recovery_fixture.RecoveryTests()
        fixture.setUp()
        try:
            # Only this private fixture creates a principal/token. The helper
            # consumes an existing credential and must never bootstrap one.
            identity = IdentityService(IdentityStateStore(fixture.state))
            scope = TenantScope(organization_id='org-a', workspace_id='ws-a')
            identity.bootstrap_service_actor(identity_id='fixture-operator',
                name='Private fixture operator', scope=scope)
            credential = identity.create_service_token(service_identity_id='fixture-operator',
                scope=scope, scopes=('upgrade:admin', 'recovery:admin',
                    'crypto:admin', 'artifact-evidence:admin'))
            UpgradeStore(fixture.state).update(lambda state: state)
            before = fixture.state.get(IdentityStateStore.namespace)
            def compose():
                return compose_capture_runtime(fixture.state, operator_token=credential.token,
                key_material_dir=Path(fixture.temp.name)/'keys',
                backup_dir=Path(fixture.temp.name)/'backups',
                refresh=lambda: self.fail('No installed HTTP in fixture'),
                check_runtime=lambda: None, journal=lambda *_: None,
                source_revision='a'*40, target_revision='b'*40,
                service_instance_id='installed-instance')
            from codex_web.storage.execution_workers import ExecutionWorkerStore
            from codex_web.storage.action_intents import ActionIntentStore
            collections = (ActionIntentStore.records_namespace,
                ExecutionWorkerStore.worker_namespace, ExecutionWorkerStore.assignment_namespace,
                ExecutionWorkerStore.event_namespace, ExecutionWorkerStore.enrollment_namespace)
            before_collections = fixture.state.record_collections_exist(collections)
            with self.assertRaisesRegex(UpgradeConflictError, 'installed-service migration'):
                compose()
            self.assertEqual(fixture.state.record_collections_exist(collections), before_collections)
            # Fixture initialization only; the composed helper cannot do this.
            ExecutionWorkerStore(fixture.state).load()
            ActionIntentStore(fixture.state).load()
            runtime = compose()
            self.assertEqual(fixture.state.get(IdentityStateStore.namespace), before)
            recovery = runtime.upgrades.recovery
            try:
                manifest = await recovery.run_manual(lambda actor: recovery.create_backup(actor=actor),
                    authenticate=runtime.authenticate, identity=runtime.identity)
                target = SQLiteStateStore(Path(fixture.temp.name)/'isolated.sqlite3')
                verified = recovery.restore_into_isolated(manifest.id, target,
                    actor=runtime.authenticate())
                self.assertEqual(verified.status, 'pass')
                self.assertTrue(target.documents())
                identity.revoke_service_token(credential.token_id)
                with self.assertRaises(AuthenticationError):
                    runtime.authenticate()
            finally:
                await recovery.stop()
        finally:
            fixture.tearDown()

    def offline_runtime(self):
        import hashlib
        from codex_web.canonical_json import canonical_json_bytes
        runtime = self.runtime(refresh=lambda: self.plan)
        self.time = 1000.0
        self.stopped = True
        def stopped():
            if not self.stopped:
                raise UpgradePreflightError('Private fixture service restarted')
        runtime.clock = lambda: self.time
        runtime.offline = OfflineCaptureProof(hashlib.sha256(canonical_json_bytes(
            self.plan.model_dump(mode='json'))).hexdigest(), 1000.0, 1600.0, stopped)
        return runtime

    async def test_offline_capture_keeps_old_preflight_historical_after_long_capture(self):
        runtime = self.offline_runtime()
        original = self.recovery.create_backup
        def capture(**kwargs):
            self.time += 70
            return original(**kwargs)
        self.recovery.create_backup = capture
        plan = await runtime.capture_once()
        self.assertEqual(plan.pre_upgrade_backup_id, 'backup-1')
        self.assertEqual(plan.preflight.evaluated_at, 1000.0)
        self.assertFalse(plan.preflight.satisfied)

    async def test_offline_postcapture_still_checks_actual_evidence_expiry(self):
        runtime = self.offline_runtime()
        self.service.evidence.store.update(lambda state: state.model_copy(update={
            'evidence': [item.model_copy(update={'retention_expires_at': 1050.0})
                for item in state.evidence]}))
        original = self.recovery.create_backup
        def capture(**kwargs):
            self.time += 70
            return original(**kwargs)
        self.recovery.create_backup = capture
        with self.assertRaises(UpgradePreflightError):
            await runtime.capture_once()
        self.assertEqual(self.recovery.backup_count, 1)
        self.assertIsNone(self.service.get(self.plan.id, actor=self.actor).pre_upgrade_backup_id)

    async def test_offline_service_restart_or_deadline_refuses_association(self):
        for change in ('restart', 'deadline'):
            with self.subTest(change=change):
                runtime = self.offline_runtime()
                self.recovery.backup_count = 0
                def capture(**kwargs):
                    self.recovery.backup_count += 1
                    self.stopped = change != 'restart'
                    self.time = 1601.0 if change == 'deadline' else 1070.0
                    return SimpleNamespace(id='retained-private-manifest')
                self.recovery.create_backup = capture
                with self.assertRaises(UpgradePreflightError):
                    await runtime.capture_once()
                self.assertEqual(self.recovery.backup_count, 1)
                self.assertIsNone(self.service.get(self.plan.id, actor=self.actor).pre_upgrade_backup_id)


class OfflineExecutableTests(unittest.IsolatedAsyncioTestCase):
    """A private orchestration fixture: no host/API/cgroup/control effects."""
    profile = PreinstallBackupTests.profile
    create_plan = PreinstallBackupTests.create_plan
    asyncSetUp = PreinstallBackupTests.asyncSetUp
    asyncTearDown = PreinstallBackupTests.asyncTearDown
    runtime = PreinstallBackupTests.runtime

    def invoke(self, clean_stop):
        from dataclasses import dataclass
        import os
        from pathlib import Path
        import sys
        from types import ModuleType
        from codex_web import preinstall_backup_cli as cli
        from codex_web.recovery import RecoveryPolicy, RecoveryState
        from codex_web.storage.recovery import RecoveryStore
        # Existing canonical fixture policy; never a production domain.
        self.state.put(RecoveryStore.namespace, RecoveryStore._encode(RecoveryState(
            policy=RecoveryPolicy(backup_key_id='private-fixture-key'))))
        runtime = self.runtime()
        async def stop():
            pass
        self.recovery.stop = stop
        @dataclass(frozen=True)
        class Cap:
            memory_max_bytes: int = 6*1024**3
            memory_swap_max_bytes: int = 0
            current_bytes: int = 123
        memory = ModuleType('codex_web.services.recovery_memory')
        class PrivateMemoryRefused(RuntimeError):
            pass
        memory.RecoveryMemoryRefused = PrivateMemoryRefused
        memory.require_process_memory_limit = lambda **_: Cap()
        class Host:
            def __init__(host, *_):
                host.initial = {'InvocationID': 'private-fixture-invocation'}
            def check(host):
                pass
            def wait_stopped(host, _deadline):
                # The executable must have durably journaled genuine pre-stop
                # evidence before the external operator can stop the service.
                self.assertTrue((Path(self.temp.name)/'journal/pre-stop-qualified.json').is_file())
                if not clean_stop:
                    raise RuntimeError('Private fixture service failed to stop')
            def service(host):
                return {'ActiveState': 'inactive', 'MainPID': '0'}
        def api(_base, token, method, path):
            self.assertEqual(token, 'private-fixture-token')
            if path == '/api/version':
                return {'gitRevision': 'a'*40}
            if path == '/api/recovery':
                return {'scheduled_work': {'local_executor': {'accepting': True,
                    'waiting_or_running': 0, 'inflight_work': 0, 'quiescence_guard': False}}}
            self.assertEqual((method, path), ('POST', '/api/upgrades/'+self.plan.id+'/preflight'))
            return {'item': self.plan.model_dump(mode='json')}
        def compose(*_, **kwargs):
            runtime.refresh = kwargs['refresh']
            runtime.check_runtime = kwargs['check_runtime']
            runtime.journal = kwargs['journal']
            return runtime
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'private-fixture-token')
        os.close(write_fd)
        root = Path(self.temp.name)
        producer = Path(cli.__file__).resolve().parents[1]
        args = ['--plan-id', self.plan.id, '--service-instance-id', 'installed-instance',
            '--expected-runtime-revision', 'a'*40, '--expected-producer-revision', 'b'*40,
            '--runtime-checkout', str(producer), '--producer-checkout', str(producer),
            '--sqlite-path', str(root/'unused.sqlite3'), '--key-material-directory', str(root/'keys'),
            '--backup-directory', str(root/'backups'), '--journal-directory', str(root/'journal'),
            '--qualification-lock', str(root/'qualification.lock'), '--expected-runtime-pid', '123',
            '--operator-token-fd', str(read_fd), '--execute']
        try:
            with patch.object(cli, 'LOCAL_DEPLOY_LOCK', root/'deploy.lock'), \
                    patch.object(cli, 'OfflineHostGuard', Host), patch.object(cli, 'local_api', api), \
                    patch.object(cli, 'compose_capture_runtime', compose), \
                    patch.object(cli.resource, 'setrlimit'), patch.object(cli.time, 'time', return_value=1000.0), \
                    patch('codex_web.storage.state_store.build_state_store', return_value=self.state), \
                    patch('codex_web.services.recovery.RecoveryMemoryConflictError', PrivateMemoryRefused, create=True), \
                    patch.dict(sys.modules, {'codex_web.services.recovery_memory': memory}):
                return cli.main(args)
        finally:
            os.close(read_fd)

    async def test_private_executable_records_real_pre_stop_then_single_association(self):
        # main owns a loop; run this synchronous orchestration outside the
        # fixture's IsolatedAsyncio loop, as an actual CLI invocation would.
        import asyncio
        result = await asyncio.to_thread(self.invoke, True)
        self.assertEqual(result, 0)
        self.assertEqual(self.recovery.backup_count, 1)
        from pathlib import Path
        files = {item.name for item in (Path(self.temp.name)/'journal').iterdir()}
        self.assertTrue({'pre-stop-qualified.json', 'service-stopped.json',
            'capture-started.json', 'manifest-created.json', 'associated.json',
            'capture-completed.json'}.issubset(files))
        self.assertNotIn('private-fixture-token', ''.join(item.read_text()
            for item in (Path(self.temp.name)/'journal').iterdir()))

    async def test_private_executable_never_captures_without_clean_stop(self):
        import asyncio
        self.assertEqual(await asyncio.to_thread(self.invoke, False), 2)
        self.assertEqual(self.recovery.backup_count, 0)
        from pathlib import Path
        self.assertFalse((Path(self.temp.name)/'journal/capture-started.json').exists())
