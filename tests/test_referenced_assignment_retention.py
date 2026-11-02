"""Real canonical SQLite retention; no application/native/provider execution."""
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from tests import test_execution_workers as fixture
from tests import test_postgres_connection_pool as pg_fixture
from unittest.mock import Mock
from codex_web.agent_runtime import AgentSession, AgentSessionState
from codex_web.artifact_evidence import Artifact, ArtifactEvidenceState
from codex_web.execution_workers import ExecutionAssignment, WorkerLifecycle
from codex_web.goal_execution_bindings import GoalExecutionBinding, GoalExecutionBindingState
from codex_web.home_goal import HomeGoalDelivery, HomeGoalProgress
from codex_web.definitions import DefinitionReference
from codex_web.models import (ActiveThreadTurn, QueuedTurn, OwnerLaneRecoveryContext,
    HomeOperatorCoordinationDispatch, ExecutionPreflightAttempt, ExecutionPreflightBlocker)
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.upgrades import UpgradeStore
from codex_web.thread_bootstrap import ThreadBootstrapBinding
from codex_web.upgrades import HomeCheckpointDisposition, UpgradeCompatibilityProfile, UpgradePlan


class ReferencedAssignmentRetentionTests(unittest.TestCase):
    def setUp(self):
        self.env = fixture.ExecutionWorkerServiceTests()
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.workers = self.env.service.store
        self.db = self.workers.store
        self.active = self.env._assignment()
        self.terminals = []
        for number in range(140):
            value = self.active.model_dump(mode='json')
            value.update(id=f'assignment-private-terminal-{number:03d}', status='lost',
                assigned_worker_id=self.env.worker.id, lease=None, fence=1,
                created_at=float(number), updated_at=float(number+1), deadline_at=float(number+200),
                execution_workspace_id=f'private-workspace-{number}')
            self.terminals.append(ExecutionAssignment.model_validate(value))
        self.db.record_apply(self.workers.assignment_namespace,
            upserts={a.id:a.model_dump(mode='json') for a in self.terminals})

    def session(self, assignment, *, session_id='private-retained-session'):
        return AgentSession(id=session_id, organization_id='local', workspace_id='default',
            project_id='home', provider_id='openai', runtime_id='codex', runtime_type='native',
            provider_native_session_id='private-native-'+session_id, assignment_id=assignment.id,
            execution_id=assignment.execution_id, execution_workspace_id=assignment.execution_workspace_id,
            worker_id=self.env.worker.id, status='ready')

    def ordinary_worker_update(self):
        return self.env.service.set_lifecycle(self.env.worker.id, WorkerLifecycle.DRAINING,
            actor=self.env.admin, reason='private retention regression')

    def test_unrelated_worker_update_preserves_session_and_bootstrap_predecessors(self):
        first, second = self.terminals[:2]
        sessions = AgentSessionStore(self.db)
        session = self.session(first)
        sessions.upsert(session)
        bindings = ThreadBootstrapBindingStore(self.db)
        binding = ThreadBootstrapBinding(id='private-bootstrap', organization_id='local',
            workspace_id='default', bootstrap_id='private-origin', thread_id='private-thread',
            execution_id=second.execution_id, assignment_id=second.id,
            execution_workspace_id='private-retained-workspace', created_by='local-admin')
        bindings.update(lambda state:state.model_copy(update={'bindings':[binding]}))
        active_before = self.workers.assignment(self.active.id)
        self.ordinary_worker_update()
        self.assertEqual(self.workers.assignment(first.id), first)
        self.assertEqual(self.workers.assignment(second.id), second)
        self.assertEqual(sessions.find_unique_by_native_id(session.provider_native_session_id, provider_id='openai', runtime_id='codex'),session)
        self.assertEqual(bindings.load().bindings,[binding])
        self.assertEqual(self.workers.assignment(self.active.id),active_before)
        retained = self.workers.load().assignments
        self.assertEqual(sum(a.status=='lost' for a in retained),130)  # two roots +128 unreferenced
        self.assertIsNone(self.workers.assignment(self.terminals[2].id))

    def test_unreferenced_cache_is_still_128_and_active_custody_unchanged(self):
        active_before = self.workers.assignment(self.active.id)
        self.ordinary_worker_update()
        terminals = [a for a in self.workers.load().assignments if a.status=='lost']
        self.assertEqual([a.id for a in terminals],[a.id for a in self.terminals[-128:]])
        self.assertEqual(self.workers.assignment(self.active.id),active_before)

    def test_home_goal_recovery_and_upgrade_checkpoint_roots_preserve_exact_rows(self):
        goal_assignment, recovery_assignment, checkpoint_assignment = self.terminals[:3]
        session = self.session(self.terminals[-1])
        AgentSessionStore(self.db).upsert(session)
        goal = GoalExecutionBinding(organization_id='local',workspace_id='default',goal_id='private-goal',
            goal_revision=1,project_id='home',provider_id='openai',runtime_id='codex',
            agent_session_id=session.id,execution_owner_id='local-admin',created_by='local-admin',
            updated_by='local-admin',change_reason='private retained Goal',
            home_continuation=HomeGoalProgress(delivery=HomeGoalDelivery(
                id='private-delivery',requested_execution_id='private-goal-input',goal_revision=1,
                requested_by='local-admin',home_revision=1,thread_id='private-Home',
                assignment_id=goal_assignment.id,execution_id=goal_assignment.execution_id,
                execution_workspace_id=goal_assignment.execution_workspace_id,worker_id=self.env.worker.id,
                fence=1,profile_id='private-Home',profile_revision=1,profile_record_id='private-profile',
                definition=DefinitionReference(definition_id='private-role',kind='agent.skill',revision=1,
                    record_id='private-definition-record',checksum='a'*64,definition_schema_version='1.0'),
                message='private retained Goal input',message_sha256='b'*64)))
        GoalExecutionBindingStore(self.db).update(lambda state:GoalExecutionBindingState(bindings=[goal]))
        original = ActiveThreadTurn(thread_id='private-recovery',assignment_id=recovery_assignment.id,
            project_id='home',execution_id=recovery_assignment.execution_id,fence=1,started_at=1,updated_at=2)
        self.db.record_apply('active_turn_recovery_records',upserts={'private-recovery':{
            'thread_id':original.thread_id,'recovery_id':'private-original','original':original.model_dump(mode='json')}})
        artifact = Artifact(organization_id='local',workspace_id='default',project_id='home',
            artifact_type='report',name='Private Home checkpoint metadata',producer_identity_id='local-admin',
            source='home-upgrade-checkpoint',metadata={'checkpoint':json.dumps({'assignment_id':checkpoint_assignment.id})})
        self.db.put('artifact_evidence',ArtifactEvidenceState(artifacts=[artifact]).model_dump(mode='json'))
        compatibility = UpgradeCompatibilityProfile(source_app_version='private-old',target_app_version='private-new',
            supported_control_plane_versions_during_rollout=('private-old','private-new'),
            source_state_schema_version=1,target_state_schema_version=1,target_definition_engine_version='1.0',
            target_extension_host_version='1.0')
        plan = UpgradePlan(organization_id='local',workspace_id='default',release_id='private-release',
            current_app_version='private-old',target_app_version='private-new',compatibility=compatibility,
            observed_control_plane_versions=('private-old',),steps=(),created_by='local-admin',
            home_checkpoint_disposition=HomeCheckpointDisposition(outcome='recorded',recorded_artifact_id=artifact.id))
        UpgradeStore(self.db).update(lambda state:state.model_copy(update={'plans':{plan.id:plan}}))
        before_roots = {name:self.db.get(name) for name in ('upgrades','artifact_evidence','goal_execution_bindings')}
        self.ordinary_worker_update()
        for value in (goal_assignment,recovery_assignment,checkpoint_assignment):
            self.assertEqual(self.workers.assignment(value.id),value)
        self.assertEqual(before_roots,{name:self.db.get(name) for name in before_roots})

    def test_queued_failed_and_home_target_and_typed_preflight_roots_are_kept(self):
        failed, target, refused = self.terminals[:3]
        queued = QueuedTurn(id='private-queued-root',thread_id='private-owner',project_id='home',
            message='private retained request',created_at=1,
            owner_lane_recovery=OwnerLaneRecoveryContext(bot_binding_id='private-binding',
                failed_assignment_id=failed.id,failed_fence=1,work_revision='a'*64,action_owner='james'),
            project_coordination_dispatch=HomeOperatorCoordinationDispatch(intent_id='private-origin',
                assignment_id=self.terminals[-1].id,fence=1,home_revision=1,
                target_assignment_id=target.id,target_fence=1,message_sha256='b'*64))
        TurnQueueRepository(self.db,Path(self.env.temp.name)/'private-queue.json').put(queued.thread_id,[queued])
        attempt = ExecutionPreflightAttempt(id='private-refusal',correlation_id='private-correlation',
            execution_id='private-unstarted',organization_id='local',workspace_id='default',
            thread_id='private-request',project_id='home',message='private retained unstarted request',
            status='blocked',created_at=1,updated_at=2,blockers=(ExecutionPreflightBlocker(
                code='private-positive-prestart',message='private typed assignment reference',
                target_type='execution_assignment',target_id=refused.id),))
        ExecutionPreflightStore(self.db).put(attempt)
        self.ordinary_worker_update()
        for value in (failed,target,refused):
            self.assertEqual(self.workers.assignment(value.id),value)
        self.assertEqual(ExecutionPreflightStore(self.db).get(attempt.id),attempt)
        self.assertEqual(TurnQueueRepository(self.db,Path(self.env.temp.name)/'private-queue.json').get(queued.thread_id),[queued])

    def test_malformed_or_partial_optional_inventory_preserves_history_but_worker_update_commits(self):
        cases = ('bad-session-marker','bad-bootstrap','bad-goals','incomplete-projected-page')
        sessions = AgentSessionStore(self.db)
        sessions.upsert(self.session(self.terminals[0]))
        for case in cases:
            with self.subTest(case=case):
                if case=='bad-session-marker':
                    self.db.record_apply(sessions.records_namespace,upserts={'meta':{'schema_version':'future'}})
                    context = patch.object(self.db,'record_page_projected',wraps=self.db.record_page_projected)
                elif case=='bad-bootstrap':
                    self.db.record_apply(sessions.records_namespace,upserts={'meta':{'schema_version':'1.0'}})
                    self.db.put('thread_bootstrap_bindings',{'schema_version':'future','bindings':[]})
                    context = patch.object(self.db,'record_page_projected',wraps=self.db.record_page_projected)
                elif case=='bad-goals':
                    self.db.delete('thread_bootstrap_bindings')
                    self.db.put('goal_execution_bindings',[])
                    context = patch.object(self.db,'record_page_projected',wraps=self.db.record_page_projected)
                else:
                    self.db.delete('goal_execution_bindings')
                    self.db.delete('thread_bootstrap_bindings')
                    context = patch('codex_web.storage.assignment_retention.MAX_REFERENCE_ROWS',0)
                with context:
                    self.ordinary_worker_update()
                self.assertEqual(len([a for a in self.workers.load().assignments if a.status=='lost']),140)
                self.assertEqual(self.workers.worker(self.env.worker.id).lifecycle,'draining')

    def test_malformed_schema_versions_are_optional_inventory_failures(self):
        from codex_web.storage.assignment_retention import referenced_assignments, ReferenceInventoryUnavailable
        for index, (namespace, body) in enumerate((
            ('agent_sessions', {'sessions': []}),
            ('thread_bootstrap_bindings', {'bindings': []}),
            ('goal_execution_bindings', {'bindings': []}),
            ('upgrades', {'plans': {}}),
        )):
            for version_index, version in enumerate(([], {})):
                with self.subTest(namespace=namespace, version=version):
                    isolated = SQLiteStateStore(Path(self.env.temp.name)/f'malformed-{index}-{version_index}.sqlite3')
                    isolated.put(namespace, dict(body, schema_version=version))
                    with self.assertRaises(ReferenceInventoryUnavailable):
                        referenced_assignments(isolated)

    def test_session_projection_never_reads_capability_snapshots_and_ignores_rollback_mirror(self):
        current = self.session(self.terminals[-1])
        sessions = AgentSessionStore(self.db);sessions.upsert(current)
        # A legacy rollback checkpoint is no longer the session authority.
        self.db.put(sessions.namespace,AgentSessionState(sessions=[self.session(self.terminals[0])]).model_dump(mode='json'))
        original_items = self.db.record_items
        original_project = self.db.record_page_projected
        selected=[]
        def items(namespace):
            self.assertNotEqual(namespace,sessions.records_namespace,'full session catalog read')
            return original_items(namespace)
        def projected(namespace,*,fields,**kw):
            if namespace==sessions.records_namespace:
                self.assertTrue(set(fields)<= {'id','assignment_id','session_id'})
                selected.append(fields)
            return original_project(namespace,fields=fields,**kw)
        with patch.object(self.db,'record_items',side_effect=items),patch.object(self.db,'record_page_projected',side_effect=projected):
            self.ordinary_worker_update()
        self.assertTrue(selected)
        self.assertIsNone(self.workers.assignment(self.terminals[0].id))
        self.assertEqual(self.workers.assignment(self.terminals[-1].id),self.terminals[-1])

    def test_missing_predecessor_is_not_recreated_and_custom_backend_skips_gc(self):
        missing = self.terminals[0].model_copy(update={'id':'assignment-already-missing'})
        AgentSessionStore(self.db).upsert(self.session(missing))
        old_method = self.db.record_mutate_many
        def legacy_method(selected,updater):return old_method(selected,updater)
        with patch.object(self.db,'record_mutate_many',new=legacy_method):
            self.ordinary_worker_update()
        self.assertIsNone(self.workers.assignment(missing.id))
        self.assertEqual(len([a for a in self.workers.load().assignments if a.status=='lost']),140)

    def test_reference_writer_racing_prune_is_seen_inside_canonical_transaction(self):
        other = SQLiteStateStore(Path(self.env.temp.name)/'state.sqlite3')
        sessions = AgentSessionStore(other);sessions._ensure_records()
        session = self.session(self.terminals[0])
        writer_locked=threading.Event();pruner_started=threading.Event();release=threading.Event();errors=[]
        real_mutate = other.record_mutate
        def held_mutate(namespace,keys,updater):
            def held(rows):
                writer_locked.set()
                self.assertTrue(release.wait(5),'test barrier not released')
                return updater(rows)
            return real_mutate(namespace,keys,held)
        def write():
            try:
                with patch.object(other,'record_mutate',side_effect=held_mutate):sessions.upsert(session)
            except BaseException as exc:errors.append(exc)
        def prune():
            try:
                pruner_started.set();self.ordinary_worker_update()
            except BaseException as exc:errors.append(exc)
        writer=threading.Thread(target=write);pruner=threading.Thread(target=prune)
        writer.start();self.assertTrue(writer_locked.wait(5));pruner.start();self.assertTrue(pruner_started.wait(5))
        release.set();writer.join(5);pruner.join(5)
        self.assertFalse(writer.is_alive());self.assertFalse(pruner.is_alive());self.assertEqual(errors,[])
        self.assertEqual(self.workers.assignment(self.terminals[0].id),self.terminals[0])
        self.assertEqual(sessions.get(session.id,organization_id='local',workspace_id='default'),session)

    def test_namespace_guards_are_read_only_and_existing_empty_selectors_keep_semantics(self):
        guard='private-never-initialized-root'
        self.assertFalse(self.db.record_collection_exists(guard));self.assertIsNone(self.db.get(guard))
        self.db.record_mutate_many({'private-old-empty':()},lambda _: {},guard_namespaces=(guard,))
        self.assertTrue(self.db.record_collection_exists('private-old-empty'))
        self.assertFalse(self.db.record_collection_exists(guard));self.assertIsNone(self.db.get(guard))
        self.db.record_mutate_many({'private-dynamic':()},lambda _: {'private-dynamic':{'new':{'id':'new'}}},
            guard_namespaces=(guard,))
        self.assertEqual(self.db.record_get('private-dynamic','new'),{'id':'new'})
        with self.assertRaisesRegex(TypeError,'selected collections'):
            self.db.record_mutate_many({'private-dynamic':()},lambda _: {guard:{'bad':{}}},guard_namespaces=(guard,))

    def test_programming_error_is_not_treated_as_optional_inventory_failure(self):
        with patch('codex_web.storage.assignment_retention.referenced_assignments',side_effect=TypeError('private bug')):
            with self.assertRaisesRegex(TypeError,'private bug'):self.ordinary_worker_update()
        self.assertEqual(self.workers.worker(self.env.worker.id).lifecycle,'active')

    def test_all_declared_reference_namespaces_are_guarded_without_record_initialization(self):
        from codex_web.storage.assignment_retention import REFERENCE_NAMESPACES
        real = self.db.record_mutate_many
        seen=[]
        def guarded(selected,updater,*,guard_namespaces=()):
            seen.append(guard_namespaces)
            return real(selected,updater,guard_namespaces=guard_namespaces)
        with patch.object(self.db,'record_mutate_many',new=guarded):self.ordinary_worker_update()
        self.assertEqual(seen,[REFERENCE_NAMESPACES])
        for name in REFERENCE_NAMESPACES:
            self.assertFalse(self.db.record_collection_exists(name))
            self.assertIsNone(self.db.get(name))


class PostgresNamespaceGuardTests(unittest.TestCase):
    def test_namespace_guards_use_owning_callback_reader_without_root_writes(self):
        # Driver/connection edge only; this is not a live PostgreSQL claim.
        cursor = Mock()
        store = pg_fixture.PostgresConnectionPoolTests._store_with_cursor(cursor)
        initialized = []
        def callback(rows):
            self.assertEqual(rows, {'private-owner': {}})
            with store._read_connection() as connection:
                self.assertIs(connection,store._pool.connection_value)
            return {}
        with patch.object(store,'_ensure_record_collection_in_cursor',side_effect=lambda c,n:initialized.append(n)), \
             patch.object(store,'_lock_many',wraps=store._lock_many) as lock:
            store.record_mutate_many({'private-owner':()},callback,
                guard_namespaces=('private-root','private-session'))
        self.assertEqual(initialized,['private-owner'])
        self.assertEqual(set(lock.call_args.args[1]),{'private-owner','private-root','private-session'})
        self.assertFalse(any('INSERT' in str(call) or 'UPDATE' in str(call) or 'DELETE' in str(call)
            for call in cursor.execute.call_args_list))
