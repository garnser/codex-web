"""Borrowed metadata readers never substitute for current Role evaluation."""
from contextlib import contextmanager
from types import MethodType
import unittest
from unittest.mock import patch

from codex_web.authority import AuthorityDelegation
from codex_web.resources import ResourceUpdate
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.definitions import DefinitionError, DefinitionRegistryService
from codex_web.services.resources import ResourceCatalogError, ResourceCatalogService
from tests import test_authority_roles as fixture


class AuthorityAdmissionReadersTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.AuthorityRoleServiceTests(
            'test_one_complete_grant_must_satisfy_scope_budget_risk_and_approval')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f._publish(self.f._catalog(bindings=(self.f._binding(),)))

    def observed(self, callback):
        return self.f.registry.observe_reads(lambda definitions:
            self.f.resources.observe_reads(lambda resources: callback(definitions, resources)))

    @contextmanager
    def reads(self):
        backend = self.f.registry.store.store
        original = backend.get
        calls = []
        def get(namespace):
            calls.append(namespace)
            return original(namespace)
        with patch.object(backend, 'get', get):
            yield calls

    def test_real_evaluate_resolves_current_catalog_and_reuses_raw_reads_only(self):
        f = self.f
        ordinary = f.service.evaluate(f._request(), actor=f.actor, now=100)
        self.assertEqual(ordinary.outcome, 'allow')
        def evaluate(definitions, resources):
            first = f.service.evaluate(f._request(), actor=f.actor, now=100,
                _definitions=definitions, _resources=resources)
            second = f.service.evaluate(f._request(amount_usd=101), actor=f.actor, now=100,
                _definitions=definitions, _resources=resources)
            self.assertEqual(first.outcome, ordinary.outcome)
            self.assertEqual(first.definition_ref, ordinary.definition_ref)
            self.assertEqual(first.matched_grant_ids, ordinary.matched_grant_ids)
            self.assertEqual(second.outcome, 'deny')
            self.assertTrue(any('monetary amount exceeds grant' in r for r in second.reasons))
            self.assertIsNot(first, second)
        with self.reads() as calls:
            self.observed(evaluate)
        self.assertEqual(calls.count(f.registry.store.namespace), 1)
        self.assertEqual(calls.count(f.resources.store.namespace), 1)

    def test_each_judgment_rechecks_actor_tenant_scope_and_approvals(self):
        f = self.f
        foreign = f.actor.model_copy(update={'organization_id': 'foreign-org'})
        requests = [(f.actor, f._request(), 'allow'),
                    (foreign, f._request(), 'deny'),
                    (f.actor, f._request(project_id='other-project'), 'deny'),
                    (f.actor, f._request(approval_role_ids=()), 'deny')]
        def evaluate(definitions, resources):
            for actor, request, expected in requests:
                result = f.service.evaluate(request, actor=actor, now=100,
                    _definitions=definitions, _resources=resources)
                self.assertEqual(result.outcome, expected)
        self.observed(evaluate)

    def test_delegation_expiry_is_evaluated_each_time_with_same_views(self):
        f = self.f
        f._publish(f._catalog(delegations=(AuthorityDelegation(id='private-delegation',
            role_id='developer', delegate_identity_id=f.actor.identity_id,
            organization_id=f.actor.organization_id, workspace_id=f.actor.workspace_id,
            project_ids=('project-a',), expires_at=200, reason='Private bounded delegation',
            delegated_by_identity_id='local-admin'),)))
        def evaluate(definitions, resources):
            before = f.service.evaluate(f._request(), actor=f.actor, now=199,
                _definitions=definitions, _resources=resources)
            at_expiry = f.service.evaluate(f._request(), actor=f.actor, now=200,
                _definitions=definitions, _resources=resources)
            self.assertEqual(before.outcome, 'allow')
            self.assertEqual(before.expires_at, 200)
            self.assertEqual(at_expiry.outcome, 'deny')
        self.observed(evaluate)

    def test_current_catalog_effective_window_is_not_memoized(self):
        f = self.f
        current = f.service.catalog_record(actor=f.actor, project_id='project-a')
        f.registry.store.update(lambda records: [record.model_copy(update={'effective_until': 200})
            if record.record_id == current.record_id else record for record in records])
        def evaluate(definitions, resources):
            allowed = f.service.evaluate(f._request(), actor=f.actor, now=199,
                _definitions=definitions, _resources=resources)
            denied = f.service.evaluate(f._request(), actor=f.actor, now=201,
                _definitions=definitions, _resources=resources)
            self.assertEqual(allowed.outcome, 'allow')
            self.assertEqual(denied.outcome, 'deny')
        self.observed(evaluate)

    def test_next_callback_sees_role_revocation_and_resource_lifecycle(self):
        f = self.f
        request = f._request()
        read = lambda definitions, resources: f.service.evaluate(request, actor=f.actor,
            _definitions=definitions, _resources=resources)
        self.assertEqual(self.observed(read).outcome, 'allow')
        f.resources.update(f.repo.id, ResourceUpdate(lifecycle='disabled'), actor=f.admin)
        disabled = self.observed(read)
        self.assertEqual(disabled.outcome, 'deny')
        self.assertTrue(any('not active' in reason for reason in disabled.reasons))
        f.resources.update(f.repo.id, ResourceUpdate(lifecycle='active'), actor=f.admin)
        self.assertEqual(self.observed(read).outcome, 'allow')
        current = f.service.catalog_record(actor=f.actor, project_id='project-a')
        f.registry.quarantine(current.record_id, actor=f.admin.identity_id, reason='Private current Role revocation')
        self.assertEqual(self.observed(read).outcome, 'deny')

    def test_foreign_and_closed_views_refuse_instead_of_granting(self):
        f = self.f
        foreign_registry = DefinitionRegistryService(f.registry.store)
        foreign_resource = ResourceCatalogService(f.resources.store)
        def with_definition(definitions):
            with self.assertRaises(DefinitionError):
                f.service.evaluate(f._request(), actor=f.actor, _definitions=definitions)
        foreign_registry.observe_reads(with_definition)
        def with_resource(resources):
            with self.assertRaises(ResourceCatalogError):
                f.service.evaluate(f._request(), actor=f.actor, _resources=resources)
        foreign_resource.observe_reads(with_resource)
        captured = []
        self.observed(lambda definitions, resources: captured.extend((definitions, resources)))
        with self.assertRaises(DefinitionError):
            f.service.evaluate(f._request(), actor=f.actor, _definitions=captured[0])
        with self.assertRaises(ResourceCatalogError):
            f.service.evaluate(f._request(), actor=f.actor, _resources=captured[1])

    def test_optional_readers_are_independent_and_catalog_path_is_not_bypassed(self):
        f = self.f
        f.registry.observe_reads(lambda definitions: self.assertEqual(f.service.evaluate(
            f._request(), actor=f.actor, _definitions=definitions).outcome, 'allow'))
        f.resources.observe_reads(lambda resources: self.assertEqual(f.service.evaluate(
            f._request(), actor=f.actor, _resources=resources).outcome, 'allow'))
        failure = RuntimeError('Private exact-record shortcut must not be used')
        with patch.object(f.service, 'evaluate_record', side_effect=failure):
            # Custom Role owner opts out, so integration must use ordinary calls.
            self.assertFalse(f.service.can_observe_reads())
            self.assertEqual(f.service.evaluate(f._request(), actor=f.actor).outcome, 'allow')

    def test_none_reader_path_keeps_custom_helper_signatures_and_order(self):
        f = self.f
        calls = []
        catalog = f.service.catalog_record
        evaluate = f.service._evaluate_record
        grant = f.service._grant_findings
        resource = f.service._resource_findings
        def custom_catalog(*, actor, project_id=None, now=None):
            calls.append('catalog')
            return catalog(actor=actor, project_id=project_id, now=now)
        def custom_evaluate(record, request, *, actor, evaluated_at):
            calls.append('evaluate')
            return evaluate(record, request, actor=actor, evaluated_at=evaluated_at)
        def custom_grant(path, request, actor):
            calls.append('grant')
            return grant(path, request, actor)
        def custom_resource(path, request, actor):
            calls.append('resource')
            return resource(path, request, actor)
        with patch.object(f.service, 'catalog_record', custom_catalog), \
                patch.object(f.service, '_evaluate_record', custom_evaluate), \
                patch.object(f.service, '_grant_findings', custom_grant), \
                patch.object(f.service, '_resource_findings', custom_resource):
            self.assertFalse(f.service.can_observe_reads())
            self.assertEqual(f.service.evaluate(f._request(), actor=f.actor).outcome, 'allow')
        self.assertEqual(calls, ['catalog', 'evaluate', 'grant', 'resource'])

    def test_instance_class_and_foreign_bound_helpers_decline_observations(self):
        f = self.f
        methods = ('catalog_record', 'role_ids_for_actor', '_evaluate_record', 'evaluate_record',
                   'evaluate', '_grant_findings', '_resource_findings', '_require_readers')
        for name in methods:
            with self.subTest(instance=name), patch.object(f.service, name, lambda *_a, **_k: None):
                self.assertFalse(f.service.can_observe_reads())
            with self.subTest(class_override=name), patch.object(type(f.service), name, lambda *_a, **_k: None):
                self.assertFalse(f.service.can_observe_reads())
            foreign = AuthorityRoleService(f.registry, f.resources)
            with self.subTest(rebound=name), patch.object(f.service, name,
                    MethodType(getattr(type(f.service), name), foreign)):
                self.assertFalse(f.service.can_observe_reads())
        for name in ('_binding_matches', '_delegation_matches', '_expand_role_paths', '_deny'):
            with self.subTest(static_override=name), patch.object(f.service, name, lambda *_a, **_k: None):
                self.assertFalse(f.service.can_observe_reads())

    def test_custom_owner_or_readers_never_bypass_original_refusal(self):
        f = self.f
        failure = RuntimeError('Private current grant refusal')
        with patch.object(f.service, '_grant_findings', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                f.service.evaluate(f._request(), actor=f.actor)
            self.assertIs(caught.exception, failure)
            with self.assertRaisesRegex(ValueError, 'canonical owners'):
                self.observed(lambda definitions, resources: f.service.evaluate(f._request(),
                    actor=f.actor, _definitions=definitions, _resources=resources))
