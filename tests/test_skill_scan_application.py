"""Regression coverage for the provider actually installed by application composition."""
import unittest


class SkillScanApplicationTests(unittest.TestCase):
    def test_composed_provider_reports_actionable_readiness(self):
        import codex_web.application as application
        actor = application.identity_service.local_trusted_actor()
        providers = application.skill_scanner_registry.list(tenant_scope=actor.tenant)
        provider = next(item for item in providers if item['providerId'] == 'nvidia-skillspector')
        self.assertIn('readiness', provider)
        self.assertFalse(provider['readiness']['ready'])
        self.assertTrue(provider['readiness']['blockers'])


class IsolatedSkillScanApplicationTests(unittest.TestCase):
    """Keep the application-installed provider; replace only its deployment dependencies.

    The harmless scanner runs through real Bubblewrap, assignments, leases,
    scratch provisioning and Artifact/Evidence storage. It is not NVIDIA scanner
    qualification and never imports the upstream source-data bridge.
    """
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import codex_web.application as application
        from codex_web.models import Project
        from codex_web.configuration import ConfigurationDraftCreate, ConfigurationPublishRequest
        from codex_web.services.configuration import ConfigurationService
        from codex_web.services.skill_scanner_configuration import install_skill_scanner_configuration, SCANNER_ROOT, SCANNER_PROJECT
        from codex_web.services.definitions import DefinitionRegistryService
        from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
        from codex_web.services.execution_workers import ExecutionWorkerService
        from codex_web.services.execution_workspaces import ExecutionWorkspaceService
        from codex_web.services.identity import IdentityService
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
        from codex_web.local_execution_backend import BubblewrapExecutionBackend
        from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
        from codex_web.storage.sqlite_state import SQLiteStateStore
        from codex_web.storage.configuration_registry import ConfigurationRegistryStore
        from codex_web.storage.definition_registry import DefinitionRegistryStore
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
        from codex_web.storage.identity_state import IdentityStateStore
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
        from codex_web.services.local_artifact_content import LocalArtifactContentStore
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.skills import SkillCreate, SkillOrigin, SkillProvenance
        import uuid
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        state = SQLiteStateStore(self.root / 'state.sqlite3')
        self.state = state
        self.identity = IdentityService(IdentityStateStore(state))
        self.identity.bootstrap_local()
        self.actor = self.identity.local_trusted_actor()
        worker_actor = self.identity.bootstrap_service_actor(identity_id='test-skill-scanner', name='Test scanner worker', scope=self.actor.tenant,
            service_scopes=('execution-worker:run',))
        project = Project(id='scan-test', name='Scan test', path=str(self.root), organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id)
        self.workspaces = ExecutionWorkspaceService(ExecutionWorkspaceStateStore(state), LocalGitWorkspaceBackend(self.root/'workspaces'),
            ResourceCatalogService(ResourceCatalogStore(state)), lambda project_id: project)
        self.workers = ExecutionWorkerService(ExecutionWorkerStore(state), identity=self.identity, workspaces=self.workspaces)
        backend = BubblewrapExecutionBackend()
        self.backend = backend
        status = backend.probe()
        self.assertTrue(status.ready, f'Real isolated transport prerequisite missing: {status}')
        worker = self.workers.ensure_local_worker(service_identity_id=worker_actor.identity_id, version='test-scan',
            capabilities=status.capabilities, supported_execution_contract_versions=('skill-scan/1.0',), actor=self.actor)
        self.runtime = LocalExecutionWorkerRuntime(self.workers, self.workspaces, backend, worker=worker,
            worker_actor=worker_actor, control_actor=self.actor)
        configuration = ConfigurationService(ConfigurationRegistryStore(state))
        install_skill_scanner_configuration(configuration)
        self.runtime_base = self.root/'runtimes'
        runtime_base_patch = patch('codex_web.services.skill_scanner_configuration.SCANNER_RUNTIME_BASE', str(self.runtime_base))
        runtime_base_patch.start()
        self.addCleanup(runtime_base_patch.stop)
        self.scanner_root = self.runtime_base/'scanner-runtime'
        (self.scanner_root/'bin').mkdir(parents=True)
        self.scanner = self.scanner_root/'bin'/'skillspector'
        self.scanner.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
assert not any(key in os.environ for key in ('OPENAI_API_KEY', 'CODEX_WEB_CONTROL_PLANE_URL', 'ANTHROPIC_API_KEY'))
assert not pathlib.Path('/home/nbingester/codex-web-native-runtime/data/state.sqlite3').exists()
if sys.argv[1:] == ['--version']:
    print('SkillSpector v2.12.0')
    sys.exit(0)
assert sys.argv[1:] == ['scan', '/opt/codex-skill-input/SKILL.md', '--format', 'json', '--no-llm'], sys.argv
content = pathlib.Path(sys.argv[2]).read_text()
assert 'Harmless isolated scanner test' in content
print(json.dumps({'risk_assessment':{'score':0,'severity':'LOW','recommendation':'SAFE'},'issues':[],'execution_successful':True,'analysis_completeness':{'status':'complete','is_complete':True,'execution_successful':True},'metadata':{'skillspector_version':'2.12.0','llm_requested':False,'meta_analysis_applied':False,'inference_usage':[]}}))
''')
        self.scanner.chmod(0o700)
        for key,value in ((SCANNER_ROOT,str(self.scanner_root)),(SCANNER_PROJECT,'scan-test')):
            record = configuration.create_draft(ConfigurationDraftCreate(key=key, value=value, scope_type='workspace', scope_id=self.actor.workspace_id,
                actor=self.actor.identity_id, reason='isolated test fixture'))
            configuration.publish(record.id, ConfigurationPublishRequest(actor=self.actor.identity_id, reason='isolated test fixture'))
        definitions = DefinitionRegistryService(DefinitionRegistryStore(state))
        profiles = install_execution_profile_definitions(definitions)
        content_registry = ArtifactContentRegistry()
        content_registry.register(LocalArtifactContentStore(self.root/'artifacts'))
        self.artifacts = ArtifactEvidenceService(ArtifactEvidenceStore(state), content=ArtifactContentService(content_registry))
        self.provider = application.skill_scanner_registry.get('nvidia-skillspector', tenant_scope=self.actor.tenant)
        self.application = application
        # Real Upgrade service + persisted Plan; step execution/backup approval
        # counterexamples live in test_upgrades. Here arrange the qualified state.
        from codex_web.services.upgrades import UpgradeService, SKILL_SCAN_CONTRACT_HANDLER
        from codex_web.storage.upgrades import UpgradeStore
        from codex_web.upgrades import UpgradePlan, UpgradeCompatibilityProfile, UpgradeStep, UpgradeHostedScope
        self.upgrades = UpgradeService(UpgradeStore(state), state_store=state, definitions=definitions,
            workers=self.workers, extensions=None, recovery=None, releases=None, action_intents=None,
            approvals=None, evidence=self.artifacts, service_instance_id='scan-test-instance')
        profile = UpgradeCompatibilityProfile(source_app_version='old', target_app_version='new', deployment_mode='local',
            supported_control_plane_versions_during_rollout=('new',), supported_worker_versions_during_rollout=('test-scan',),
            supported_execution_contract_versions=('skill-scan/1.0',), source_state_schema_version=1, target_state_schema_version=1,
            supported_event_contract_versions=('1.0',), supported_api_contract_versions=('1.0',),
            target_definition_engine_version='1.0', target_definition_schemas={}, target_extension_host_version='1.0')
        self.upgrade_plan = UpgradePlan(organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id,
            release_id='test-release', current_app_version='old', target_app_version='new', compatibility=profile,
            observed_control_plane_versions=('new',), created_by=self.actor.identity_id, status='completed',
            maintenance_scope='service', service_instance_id='scan-test-instance',
            covered_scopes=(UpgradeHostedScope(organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id),),
            irreversible_boundary_crossed=True, rollback_available=False, pre_upgrade_backup_id='verified-test-snapshot',
            steps=(UpgradeStep(id='scanner-contract', phase='contract', kind='state_schema', description='Skill scan persisted contract',
                handler_id=SKILL_SCAN_CONTRACT_HANDLER, idempotent=True, reversible=False, irreversible=True,
                requires_drain=True, requires_backup=True, status='succeeded', evidence_id='test-step-evidence',
                result={'backup_verification_id':'test-restore-verification'}),))
        def persist_upgrade(current):
            current.plans[self.upgrade_plan.id] = self.upgrade_plan
            return current
        self.upgrades.store.update(persist_upgrade)
        self.workspaces.skill_scan_admission = self.upgrades.skill_scan_admission
        self.workers.skill_scan_admission = self.upgrades.skill_scan_admission
        self.workers.maintenance_guard = self.upgrades.worker_assignment_allowed
        self.workers.maintenance_admission = self.upgrades.worker_admission
        from codex_web.storage.skill_security import SkillSecurityStore
        from codex_web.storage.skill_catalog import SkillSourceStore
        from codex_web.services.skill_security import SkillSecurityService
        security_store = SkillSecurityStore(state)
        SkillSecurityService(security_store, definitions, application.skill_scanner_registry)
        security_patch = patch.multiple(application.skill_security_service, write_admission=self.upgrades.skill_scan_admission,
            store=security_store, definitions=definitions, source_store=SkillSourceStore(state))
        security_patch.start()
        self.addCleanup(security_patch.stop)
        self.patcher = patch.multiple(self.provider, runtime=self.runtime, configuration=configuration, profiles=profiles, artifacts=self.artifacts, write_admission=self.upgrades.skill_scan_admission)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.view = application.skill_service.create(SkillCreate(skill_id=f'scan-test-{uuid.uuid4().hex}',
            name='Harmless isolated scanner test', instructions='Harmless isolated scanner test.',
            provenance=SkillProvenance(origin=SkillOrigin.IMPORTED, source_type='catalog_bundle', source_id='isolated-test')),
            actor=self.actor)

    def revoke_worker_identity(self):
        import time
        def revoke(state):
            state.services = [item.model_copy(update={'disabled_at':time.time()})
                              if item.id == self.runtime.worker_actor.identity_id else item for item in state.services]
            return state
        self.identity.store.update(revoke)

    def test_revoked_identity_before_dispatch_never_spawns(self):
        from unittest.mock import patch
        self.revoke_worker_identity()
        with patch.object(self.backend, '_popen', wraps=self.backend._popen) as spawn:
            result = self.scan()
        self.assertEqual(result['status'], 'scan_error')
        spawn.assert_not_called()
        self.assertFalse(result['assignmentDecision']['allowed'])

    def test_immediate_exit_revocation_rejects_terminal_output_without_poll_loop(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        def immediate_exit(*args, **kwargs):
            kwargs['stdout'].write(b'SkillSpector v2.12.0\n')
            kwargs['stdout'].flush()
            self.revoke_worker_identity()
            return SimpleNamespace(poll=lambda:0, wait=lambda:0)
        with patch.object(self.backend, '_popen', side_effect=immediate_exit) as spawn:
            result = self.scan()
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(result['status'], 'scan_error', result)
        self.assertFalse(result['assignmentDecision']['allowed'])
        self.assertFalse(any(a.status == 'succeeded' for a in self.workers.store.load().assignments))

    def test_maintenance_discovery_writes_no_workspace_or_assignment_rows(self):
        from codex_web.services.skill_security import SkillSecurityBlocked
        from codex_web.upgrades import UpgradeStatus
        self.upgrades._update(self.upgrade_plan.id, self.actor,
            lambda plan: plan.model_copy(update={'maintenance_mode':True, 'status':UpgradeStatus.DRAINING}))
        before = self.state.record_items(self.workspaces.store.workspace_namespace)
        readiness = self.provider.inspect(actor=self.actor)['readiness']
        self.assertFalse(readiness['ready'])
        self.assertIn('drained', readiness['blockers'][0]['message'])
        with self.assertRaisesRegex(SkillSecurityBlocked, 'drained'):
            self.scan()
        self.assertEqual(self.state.record_items(self.workspaces.store.workspace_namespace), before)
        self.assertEqual(self.workers.store.load().assignments, [])

    def test_canonical_workspace_and_assignment_apis_cannot_bypass_contract_admission(self):
        from codex_web.services.upgrades import UpgradeConflictError
        from codex_web.execution_subjects import ExecutionSubject
        from codex_web.execution_workspaces import ExecutionWorkspaceAcquire
        from codex_web.execution_workers import ExecutionAssignmentCreate
        self.upgrades.store.update(lambda state: state.model_copy(update={'plans':{}}))
        subject = ExecutionSubject(kind='skill_scan', ref='direct-api-bypass')
        with self.assertRaisesRegex(UpgradeConflictError, 'irreversible'):
            self.workspaces.acquire(ExecutionWorkspaceAcquire(subject=subject, execution_id='direct-api-bypass',
                project_id='scan-test', resource_ids=(), scratch=True), actor=self.actor)
        _, reference = self.provider.profiles.resolve('orchestration-only', organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id, project_id='scan-test')
        with self.assertRaisesRegex(UpgradeConflictError, 'irreversible'):
            self.workers.create_assignment(ExecutionAssignmentCreate(subject=subject, execution_id='direct-api-bypass',
                project_id='scan-test', resource_ids=(), required_capabilities=('command_execution',),
                execution_profile_id='orchestration-only', execution_profile_definition=reference,
                execution_contract_version='skill-scan/1.0'), actor=self.actor)
        self.assertEqual(self.workspaces.store.load().workspaces, [])
        self.assertEqual(self.workers.store.load().assignments, [])

    def test_missing_upgrade_blocks_discovery_and_error_result_writes(self):
        from codex_web.services.skill_security import SkillSecurityBlocked
        snapshot = self.upgrades.store.load()
        self.upgrades.store.update(lambda state: state.model_copy(update={'plans':{}}))
        before = self.application.skill_security_service.store.scans(self.actor.organization_id, self.actor.workspace_id, record_id=self.view['recordId'])
        readiness = self.provider.inspect(actor=self.actor)['readiness']
        self.assertFalse(readiness['ready'])
        self.assertEqual(readiness['blockers'][0]['code'], 'scanner_upgrade_required')
        with self.assertRaisesRegex(SkillSecurityBlocked, 'irreversible'):
            self.scan()
        self.assertEqual(self.workers.store.load().assignments, [])
        self.assertEqual(self.application.skill_security_service.store.scans(self.actor.organization_id, self.actor.workspace_id, record_id=self.view['recordId']), before)

    def test_catalog_import_and_manual_scan_share_canonical_source_trust_policy(self):
        from codex_web.services.skill_catalog import SkillCatalogService
        from codex_web.skill_catalog import SkillSourceCreate, SkillSourceSyncRequest, SkillCatalogEntry, SkillSourceUpdate
        from codex_web.skill_security import SkillSecurityPolicyUpdate, SkillScanRequest
        from codex_web.skills import SkillCreate, SkillPublish
        from codex_web.services.skill_security import SkillSecurityBlocked
        from codex_web.storage.skill_catalog import SkillSourceStore
        import uuid
        security = self.application.skill_security_service
        catalog = SkillCatalogService(SkillSourceStore(self.state), self.application.skill_service)
        security.update_policy(SkillSecurityPolicyUpdate(semantic_for_untrusted_sources=True, reason='Require semantic assurance'), actor=self.actor)
        source_id = 'source-' + uuid.uuid4().hex
        skill_id = 'import-' + uuid.uuid4().hex
        catalog.create(SkillSourceCreate(source_id=source_id, name='Pending scanner test', location='https://example.invalid/catalog', trust='pending'), actor=self.actor)
        result = catalog.sync(source_id, SkillSourceSyncRequest(source_revision='one', entries=(SkillCatalogEntry(upstream_id='one',
            manifest=SkillCreate(skill_id=skill_id, name='Harmless isolated scanner test', instructions='Harmless isolated scanner test.')),)), actor=self.actor)
        view = self.application.skill_service.get(skill_id, actor=self.actor)
        self.assertEqual(security.summary(view, actor=self.actor)['status'], 'scan_error')
        manual = security.scan(view, SkillScanRequest(), actor=self.actor)
        self.assertEqual(manual['scan']['mode'], 'semantic')
        self.assertFalse(manual['assignmentDecision']['allowed'])
        with self.assertRaises(SkillSecurityBlocked):
            self.application.skill_service.publish(skill_id, view['recordId'], SkillPublish(), actor=self.actor)
        self.assertEqual(self.workers.store.load().assignments, [])
        catalog.update(source_id, SkillSourceUpdate(trust='approved'), actor=self.actor)
        self.assertEqual(security.scan(view, SkillScanRequest(), actor=self.actor)['status'], 'passed')
        catalog.update(source_id, SkillSourceUpdate(trust='pending'), actor=self.actor)
        self.assertEqual(security.decision(view, actor=self.actor, phase='assignment')['reason'], 'semantic_scan_required')

    def test_semantic_policy_manual_static_cannot_clear_import_failure(self):
        from unittest.mock import patch
        from codex_web.skill_security import SkillSecurityPolicy
        policy = SkillSecurityPolicy(semantic_for_untrusted_sources=True)
        with patch.object(self.application.skill_security_service, 'policy', return_value=policy):
            result = self.scan()
            self.assertEqual(result['status'], 'scan_error')
            self.assertEqual(result['scan']['mode'], 'semantic')
            self.assertFalse(result['assignmentDecision']['allowed'])
            self.assertFalse(self.application.skill_security_service.decision(self.view, actor=self.actor, phase='publish')['allowed'])
        self.assertEqual(self.workers.store.load().assignments, [])

    def test_later_semantic_policy_rejects_existing_clean_static_scan(self):
        from unittest.mock import patch
        from codex_web.skill_security import SkillSecurityPolicy
        self.assertEqual(self.scan()['status'], 'passed')
        with patch.object(self.application.skill_security_service, 'policy', return_value=SkillSecurityPolicy(semantic_for_untrusted_sources=True)):
            for phase in ('publish', 'assignment'):
                decision = self.application.skill_security_service.decision(self.view, actor=self.actor, phase=phase)
                self.assertFalse(decision['allowed'])
                self.assertEqual(decision['reason'], 'semantic_scan_required')

    def test_revocation_during_command_preparation_prevents_spawn(self):
        from unittest.mock import patch
        prepare = self.backend.build_command
        def prepare_then_revoke(*args, **kwargs):
            command = prepare(*args, **kwargs)
            self.revoke_worker_identity()
            return command
        with patch.object(self.backend, 'build_command', side_effect=prepare_then_revoke), patch.object(self.backend, '_popen', wraps=self.backend._popen) as spawn:
            result = self.scan()
        spawn.assert_not_called()
        self.assertEqual(result['status'], 'scan_error')

    def test_real_collection_rows_fail_previous_readers_and_matching_snapshot_restores(self):
        import importlib.util
        from pathlib import Path
        from pydantic import ValidationError
        from codex_web.storage.sqlite_state import SQLiteStateStore
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
        spec = importlib.util.spec_from_file_location('skill_scan_previous_readers', Path(__file__).parent/'fixtures'/'skill_scan_previous_readers.py')
        previous = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(previous)
        # Force actual collection materialization before the pre-write snapshot.
        self.workers.store.load()
        self.workspaces.store.load()
        backup = self.root/'pre-scanner.sqlite3'
        upgrade_state = self.state.get(self.upgrades.store.namespace)
        self.state.put(self.upgrades.store.namespace, {'schema_version':'1.0', 'plans':{}})
        self.state.backup_to(backup)
        self.state.put(self.upgrades.store.namespace, upgrade_state)
        self.assertEqual(self.scan()['status'], 'passed')
        for namespace in (self.workers.store.assignment_namespace, self.workspaces.store.workspace_namespace):
            rows = self.state.record_items(namespace)
            self.assertTrue(rows)
            for row in rows.values():
                with self.assertRaises(ValidationError):
                    previous.ExecutionSubject.model_validate(row['subject'])
        stored = self.application.skill_security_service.store.state.record_items(self.application.skill_security_service.store.SCANS)
        row = next(value for value in stored.values() if value['record_id'] == self.view['recordId'])
        with self.assertRaises(ValidationError): previous.SkillScanResult.model_validate(row)
        restored = SQLiteStateStore(backup)
        self.assertEqual(ExecutionWorkerStore(restored).load().assignments, [])
        self.assertEqual(ExecutionWorkspaceStateStore(restored).load().workspaces, [])
        self.assertEqual(restored.record_items(self.workers.store.assignment_namespace), {})
        # Restore before contract activation must remove admission, not replay work.
        from unittest.mock import patch
        from codex_web.storage.upgrades import UpgradeStore
        from codex_web.services.upgrades import UpgradeConflictError
        with patch.object(self.upgrades, 'store', UpgradeStore(restored)):
            with self.assertRaisesRegex(UpgradeConflictError, 'irreversible'):
                with self.upgrades.skill_scan_admission(self.actor): pass

    def test_database_contract_enablement_survives_process_instance_replacement(self):
        from unittest.mock import patch
        with patch.object(self.upgrades, 'service_instance_id', 'replacement-process-instance'):
            self.assertEqual(self.scan()['status'], 'passed')

    def test_system_python_interpreter_links_use_the_installed_abi(self):
        from codex_web.services.skill_scanner_configuration import validate_scanner_runtime
        import sys
        link = self.scanner_root/'bin'/f'python{sys.version_info.major}.{sys.version_info.minor}'
        link.symlink_to('/usr/bin/python3')
        self.assertEqual(validate_scanner_runtime(self.scanner_root), self.scanner_root)

    def test_hostile_runtime_paths_never_mount_host_directories(self):
        from codex_web.services.skill_scanner_configuration import validate_scanner_runtime
        from codex_web.services.skill_scanners import SkillScannerError
        for path in ('/', '/home', '/home/nbingester', str(self.root), str(self.runtime_base)):
            with self.subTest(path=path), self.assertRaises(SkillScannerError):
                validate_scanner_runtime(path)
        for name, target in (('.env', None), ('lib', '/home/nbingester')):
            path = self.scanner_root/name
            if target: path.symlink_to(target)
            else: path.write_text('harmless credential fixture')
            try:
                with self.assertRaises(SkillScannerError): validate_scanner_runtime(self.scanner_root)
            finally: path.unlink()

    def test_public_ca_bundle_required_at_import_survives_isolated_dispatch(self):
        from pathlib import Path
        # Real SkillSpector imports LangChain, which creates an SSL context with
        # certifi's public bundle even when --no-llm and networking are disabled.
        bundle = self.scanner_root/'lib'/'python3.14'/'site-packages'/'certifi'/'cacert.pem'
        bundle.parent.mkdir(parents=True)
        bundle.write_bytes((Path(__file__).with_name('fixtures')/'skill_scanner_legacy_ca.pem').read_bytes())
        self.scanner.write_text(self.scanner.read_text().replace(
            'import json, os, pathlib, sys',
            'import json, os, pathlib, sys\nimport ssl\nssl.create_default_context(cafile=' + repr(str(bundle)) + ')'))
        readiness = self.provider.inspect(actor=self.actor)['readiness']
        self.assertTrue(readiness['ready'], readiness)
        result = self.scan()
        self.assertEqual(result['status'], 'passed', result)
        proof = result['scan']['execution_attribution']
        assignment = self.workers.store.assignment(proof['assignment_id'])
        self.assertEqual(assignment.status, 'succeeded')
        self.assertFalse(assignment.network.enabled)
        self.assertEqual(assignment.secret_refs, ())
        self.assertTrue(result['scan']['report_artifact_id'])

    def scan(self):
        from codex_web.skill_security import SkillScanRequest
        return self.application.skill_security_service.scan(self.view, SkillScanRequest(), actor=self.actor)

    def test_application_dispatches_actual_isolated_scanner_and_persists_exact_report(self):
        readiness = self.provider.inspect(actor=self.actor)['readiness']
        self.assertTrue(readiness['ready'], readiness)
        self.assertEqual(readiness['scannerVersion'], '2.12.0')
        result = self.scan()
        self.assertEqual(result['status'], 'passed', result)
        scan = result['scan']
        self.assertEqual(scan['record_id'], self.view['recordId'])
        self.assertEqual(scan['checksum'], self.view['checksum'])
        self.assertTrue(scan['report_artifact_id'])
        proof = scan['execution_attribution']
        assignment = self.workers.store.assignment(proof['assignment_id'])
        self.assertEqual(assignment.status, 'succeeded')
        self.assertEqual(assignment.subject.kind, 'skill_scan')
        self.assertIsNone(assignment.work_item_ref)
        self.assertFalse(assignment.network.enabled)
        self.assertEqual(assignment.secret_refs, ())
        self.assertIsNone(assignment.lease)
        self.assertEqual(self.workspaces.store.workspace(assignment.execution_workspace_id).status, 'discarded')
        self.assertTrue(proof['evidence_id'])

    def test_application_failed_transport_has_no_critical_finding_and_blocks_gates(self):
        self.scanner.write_text('#!/usr/bin/python3\nimport sys\nprint("SkillSpector v2.12.0") if sys.argv[1:] == ["--version"] else sys.exit(2)\n')
        result = self.scan()
        self.assertEqual(result['status'], 'scan_error', result)
        self.assertIsNone(result['scan']['risk_score'])
        self.assertIsNone(result['scan']['severity'])
        self.assertEqual(result['scan']['findings'], [])
        self.assertFalse(result['assignmentDecision']['allowed'])
        self.assertFalse(self.application.skill_security_service.decision(self.view, actor=self.actor, phase='publish')['allowed'])
        assignments = self.workers.store.load().assignments
        self.assertTrue(any(item.status == 'failed' for item in assignments))
        self.assertTrue(all(item.lease is None for item in assignments))

    def test_application_unavailable_worker_is_actionable_and_never_runs_scanner(self):
        from unittest.mock import patch
        from codex_web.local_execution_backend import LocalIsolationStatus
        unavailable = LocalIsolationStatus('bubblewrap', False, 'test unavailable', True, False, True)
        with patch.object(self.backend, 'probe', return_value=unavailable):
            readiness = self.provider.inspect(actor=self.actor)['readiness']
            self.assertFalse(readiness['ready'])
            self.assertEqual(readiness['blockers'][0]['code'], 'isolated_transport_unavailable')
            result = self.scan()
        self.assertEqual(result['status'], 'scan_error')
        self.assertEqual(self.workers.store.load().assignments, [])

    def test_semantic_mode_never_borrows_credentials_or_dispatches(self):
        from codex_web.skill_security import SkillScanRequest
        result = self.application.skill_security_service.scan(self.view, SkillScanRequest(mode='semantic'), actor=self.actor)
        self.assertEqual(result['status'], 'scan_error')
        self.assertIn('separately governed', result['scan']['error'])
        self.assertEqual(self.workers.store.load().assignments, [])

    def test_scanner_findings_exit_one_is_not_a_transport_error(self):
        source = self.scanner.read_text().replace("'score':0", "'score':95").replace("'issues':[]", "'issues':[{'id':'injection','severity':'CRITICAL','explanation':'Review unsafe instructions','category':'prompt-injection','location':{'file':'SKILL.md','start_line':4}}]")
        self.scanner.write_text(source + '\nsys.exit(1)\n')
        result = self.scan()
        self.assertEqual(result['status'], 'quarantined', result)
        self.assertEqual(result['scan']['severity'], 'critical')
        self.assertEqual(result['scan']['findings'][0]['rule_id'], 'injection')
        self.assertEqual(self.workers.store.assignment(result['scan']['execution_attribution']['assignment_id']).status, 'succeeded')

    def test_provider_api_uses_application_composed_discovery_and_tenant_scope(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from codex_web.api.skill_security import build_skill_security_router
        app = FastAPI()
        @app.middleware('http')
        async def actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_skill_security_router(self.application.skill_security_service, self.application.skill_service))
        with TestClient(app) as client:
            response = client.get('/api/skill-security/providers')
            self.assertEqual(response.status_code, 200, response.text)
            readiness = response.json()['items'][0]['readiness']
            self.assertTrue(readiness['ready'], readiness)
            self.assertEqual(readiness['scannerVersion'], '2.12.0')
            self.assertEqual(self.workers.store.assignment(readiness['assignmentId']).status, 'succeeded')

    def test_partial_or_wrong_version_reports_never_become_passing_scans(self):
        original = self.scanner.read_text()
        for old, new in (("'is_complete':True", "'is_complete':False"), ("'execution_successful':True", "'execution_successful':False"), ("'skillspector_version':'2.12.0'", "'skillspector_version':'99.0.0'"), ("'llm_requested':False", "'llm_requested':True")):
            with self.subTest(contract=old):
                self.scanner.write_text(original.replace(old, new))
                result = self.scan()
                self.assertEqual(result['status'], 'scan_error', result)
                self.assertFalse(result['assignmentDecision']['allowed'])

    def test_legacy_subject_readers_and_contracts_fail_closed_for_new_scan_records(self):
        from typing import Literal
        from pydantic import BaseModel, ValidationError
        from codex_web.compatibility import ContractSpec, ContractCompatibilityError
        from codex_web.execution_subjects import ExecutionSubject
        from codex_web.execution_workers import EXECUTION_WORKER_CONTRACT
        from codex_web.storage.execution_workspaces import EXECUTION_WORKSPACE_STATE_CONTRACT
        class PreviousSubjectReader(BaseModel):
            kind: Literal['work_item', 'thread', 'thread_bootstrap']
            ref: str
        for kind in ('work_item', 'thread', 'thread_bootstrap'):
            old = {'kind':kind,'ref':'existing'}
            self.assertEqual(ExecutionSubject.model_validate(old).ref, PreviousSubjectReader.model_validate(old).ref)
        with self.assertRaises(ValidationError):
            PreviousSubjectReader.model_validate({'kind':'skill_scan','ref':'scan-1'})
        for name, old, current in (('execution-worker-state','1.11',EXECUTION_WORKER_CONTRACT),
                                   ('execution-workspace-state','1.7',EXECUTION_WORKSPACE_STATE_CONTRACT)):
            with self.assertRaises(ContractCompatibilityError):
                ContractSpec(name, old, (old,)).require(current.current)
        old_workers = {'schema_version':'1.11','workers':[], 'assignments':[], 'events':[]}
        old_spaces = {'schema_version':'1.7','workspaces':[], 'leases':[], 'events':[]}
        self.assertEqual(self.workers.store._decode(old_workers).schema_version, '1.13')
        self.assertEqual(self.workspaces.store._decode(old_spaces).schema_version, '1.8')
        self.assertEqual(old_workers['schema_version'], '1.11')
        self.assertEqual(old_spaces['schema_version'], '1.7')

    def test_native_report_discards_snippets_and_redacts_sensitive_explanation(self):
        original = self.scanner.read_text()
        self.scanner.write_text(original.replace("'issues':[]", "'issues':[{'id':'credential','severity':'HIGH','explanation':'api_key=private-example-value','code_snippet':'private-example-value','finding':'private-example-value','location':{'file':'SKILL.md','start_line':3}}]"))
        result = self.scan()
        self.assertEqual(result['status'], 'quarantined', result)
        self.assertNotIn('private-example-value', str(result))
        self.assertIn('[REDACTED]', result['scan']['findings'][0]['message'])

    def test_missing_installation_is_visible_without_dispatch(self):
        self.scanner.unlink()
        readiness = self.provider.inspect(actor=self.actor)['readiness']
        self.assertEqual(readiness['blockers'][0]['code'], 'scanner_installation_missing')
        result = self.scan()
        self.assertEqual(result['status'], 'scan_error')
        self.assertEqual(result['scan']['error_code'], 'scanner_installation_missing')
        self.assertEqual(self.workers.store.load().assignments, [])

    def test_unsupported_scanner_discovery_reports_actual_version(self):
        self.scanner.write_text('#!/usr/bin/python3\nprint("SkillSpector v99.0.0")\n')
        readiness = self.provider.inspect(actor=self.actor)['readiness']
        self.assertFalse(readiness['ready'])
        self.assertEqual(readiness['scannerVersion'], '99.0.0')
        self.assertEqual(readiness['blockers'][0]['code'], 'scanner_version_incompatible')

    def test_failure_preserves_canonical_assignment_and_failure_evidence(self):
        self.scanner.write_text('#!/usr/bin/python3\nimport sys\nprint("SkillSpector v2.12.0") if sys.argv[1:] == ["--version"] else sys.exit(2)\n')
        result = self.scan()['scan']
        proof = result['execution_attribution']
        self.assertEqual(result['error_code'], 'scanner_execution_failed')
        self.assertEqual(self.workers.store.assignment(proof['assignment_id']).status, 'failed')
        self.assertTrue(proof['evidence_id'])
        self.assertEqual(result['record_id'], self.view['recordId'])
        self.assertEqual(result['checksum'], self.view['checksum'])
