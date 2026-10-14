from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from codex_web.code_hosts import CodeHostCheckFact, CodeHostValidationRunFact, CodeHostError, CodeHostProviderBinding
from codex_web.resources import Resource, ResourceAlias
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.services.delivery_validation import validate_delivery_runs
from codex_web.services.publication_recovery import PublicationRecoveryConflict
from scripts.ci.check_gate import OWNERS, expected_functional_shard_names
from tests.publication_recovery_ci_fixtures import ROOT, qualified_runs, observation, SECURITY_CONFIGURATION


class DeliveryValidationTests(unittest.TestCase):
    revision = 'a' * 40

    def runs(self, *, full=True):
        tests, security, recovery = qualified_runs(self.revision)
        if not full:
            tests = tests.model_copy(update={'selection_full': False, 'checks': tuple(c.model_copy(update={
                'conclusion': 'success' if c.name == 'focused-documentation' else 'skipped'})
                if c.name == 'focused-documentation' or c.name in expected_functional_shard_names() else c
                for c in tests.checks)})
        return tests, recovery, security

    def test_packaged_protocol_positive_and_typed_missing_unreadable_unsupported_refusal(self):
        # This entire module also runs in the actual supported image, as its
        # unprivileged user with no network and a read-only filesystem.
        self.assertEqual(expected_functional_shard_names(), {
            'chromium-functional-shard-1', 'chromium-functional-shard-2', 'chromium-functional-shard-3'})
        validate_delivery_runs(self.runs(), self.revision, recovery_required=True)
        runs = self.runs()
        for error in (FileNotFoundError('missing'), PermissionError('unreadable'), UnicodeError('encoding')):
            with self.subTest(error=type(error).__name__), patch.object(Path, 'read_text', side_effect=error):
                with self.assertRaisesRegex(PublicationRecoveryConflict, 'inspect.*packaged'):
                    validate_delivery_runs(runs, self.revision, recovery_required=True)
        for body in ('', '  functional-shards:\n    strategy:\n      matrix:\n        shard: [1,2,3]\n',
                     '  functional-shards:\n    strategy:\n      matrix:\n        shard:\n          - 1\n        include: []\n    steps:\n'):
            runs = self.runs()
            with self.subTest(body=body), patch.object(Path, 'read_text', return_value=body):
                with self.assertRaisesRegex(PublicationRecoveryConflict, 'unsupported'):
                    validate_delivery_runs(runs, self.revision, recovery_required=True)

    def test_actual_mixed_success_and_proven_optional_skip_qualifies(self):
        result = validate_delivery_runs(self.runs(), self.revision, recovery_required=True)
        self.assertEqual([c['name'] for c in result if c['inapplicable']], ['focused-documentation', 'qualified-release-promotion'])

    def test_documentation_selection_skips_only_unselected_functional_shards(self):
        result = validate_delivery_runs(self.runs(full=False), self.revision, recovery_required=False)
        self.assertTrue(next(c for c in result if c['name'] == 'chromium-functional-shard-1')['inapplicable'])

    def test_selection_missing_failed_cancelled_and_unsupported_skips_deny(self):
        tests, recovery, security = self.runs()
        for name in ('validation-selection', 'unit-static', 'chromium-functional-shard-1'):
            for conclusion in ('failure', 'cancelled', 'skipped', None):
                current = tests.model_copy(update={'checks': tuple(c.model_copy(update={'conclusion': conclusion}) if c.name == name else c for c in tests.checks)})
                with self.subTest(name=name, conclusion=conclusion), self.assertRaises(PublicationRecoveryConflict):
                    validate_delivery_runs((current, recovery, security), self.revision, recovery_required=True)
        for name in (*OWNERS, 'chromium', 'validation-selection', 'focused-documentation'):
            current = tests.model_copy(update={'checks': tuple(c for c in tests.checks if c.name != name)})
            with self.subTest(missing=name), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((current, recovery, security), self.revision, recovery_required=True)
        with self.assertRaises(PublicationRecoveryConflict):
            validate_delivery_runs((tests.model_copy(update={'selection_full': None}), recovery, security), self.revision, recovery_required=True)

    def test_promotion_skip_requires_actual_candidate_trigger_not_skip_label(self):
        tests, recovery, security = self.runs()
        for update in ({'event':'workflow_dispatch'}, {'head_branch':'foreign'}, {'state':'in_progress','conclusion':None}, {'conclusion':'failure'}):
            with self.subTest(update=update), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((tests, recovery.model_copy(update=update), security), self.revision, recovery_required=True)
        with self.assertRaises(PublicationRecoveryConflict):
            validate_delivery_runs((tests,), self.revision, recovery_required=True)

    def test_each_selected_shard_missing_and_ambiguous_names_deny(self):
        tests, recovery, security = self.runs()
        for name in expected_functional_shard_names():
            changed = tests.model_copy(update={'checks': tuple(c for c in tests.checks if c.name != name)})
            with self.subTest(missing=name), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((changed, recovery, security), self.revision, recovery_required=True)
        for extra in (tests.checks[0], tests.checks[0].model_copy(update={'name': 'chromium-functional-shard-4'})):
            changed = tests.model_copy(update={'checks': tests.checks + (extra,)})
            with self.subTest(extra=extra.name), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((changed, recovery, security), self.revision, recovery_required=True)

    def test_required_security_presence_configuration_and_each_selected_job(self):
        tests, recovery, security = self.runs()
        for recovery_required in (False, True):
            with self.subTest(recovery_required=recovery_required), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((tests, recovery), self.revision, recovery_required=recovery_required)
        for job in security.checks:
            for status, conclusion in (('completed', 'failure'), ('completed', 'cancelled'),
                                       ('in_progress', None), ('queued', None), ('completed', 'skipped')):
                changed = security.model_copy(update={'checks': tuple(c.model_copy(update={'state': status,
                    'conclusion': conclusion}) if c.name == job.name else c for c in security.checks)})
                with self.subTest(job=job.name, status=status, conclusion=conclusion), self.assertRaises(PublicationRecoveryConflict):
                    validate_delivery_runs((tests, recovery, changed), self.revision, recovery_required=True)
            changed = security.model_copy(update={'checks': tuple(c for c in security.checks if c.name != job.name)})
            with self.subTest(missing=job.name), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((tests, recovery, changed), self.revision, recovery_required=True)
        for update in ({'security_configuration': None}, {'checks': security.checks + (security.checks[0],)}):
            with self.subTest(update=update), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((tests, recovery, security.model_copy(update=update)), self.revision, recovery_required=True)

    def test_exact_default_setup_aliases_normalize_without_observed_job_inference(self):
        tests, recovery, security = self.runs()
        self.assertEqual(SECURITY_CONFIGURATION['languages'],
                         ['actions', 'javascript', 'javascript-typescript', 'python', 'typescript'])
        validate_delivery_runs((tests, recovery, security), self.revision, recovery_required=True)
        for patch in ({'state': 'disabled'}, {'state': 'unknown'}, {'languages': []},
                      {'languages': ['python', 'future-language']}, {'updated_at': None},
                      {'protocol_version': 'future'}, {'query_suite': 'unknown'}):
            changed = security.model_copy(update={'security_configuration': {**SECURITY_CONFIGURATION, **patch}})
            with self.subTest(patch=patch), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((tests, recovery, changed), self.revision, recovery_required=True)


class GitHubValidationObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_pr_source_and_tested_merge_selection_are_distinct(self):
        selection = json.loads((ROOT / '1429-selection.json').read_text())
        run = observation(38626937167, 'run')
        jobs = observation(38626937167, 'jobs')
        self.assertEqual(run['head_sha'], selection['head'])
        self.assertNotEqual(selection['head'], selection['tested_revision'])
        self.assertIn(selection['job_id'], [j['id'] for j in jobs['jobs']])
        class Client:
            async def get_json(inner, base, path, **kwargs):
                if path.endswith('/code-scanning/default-setup'):
                    return SECURITY_CONFIGURATION
                if path.endswith('/actions/runs'):
                    return {'total_count': 1, 'workflow_runs': [run]}
                return jobs
            async def request_bytes(inner, *args, **kwargs):
                output = {key: selection[key] for key in ('full', 'head', 'tested_revision')}
                return ('2026-10-14T00:00:00Z ' + json.dumps(output) + '\n').encode(), False, 'text/plain'
        provider = GitHubCodeHostProvider(client=Client())
        binding = CodeHostProviderBinding(id='binding', organization_id='local', workspace_id='default',
            provider_type='github', provider_instance='github.com', base_url='https://api.github.com',
            capabilities=tuple(provider.capabilities()))
        resource = Resource(organization_id='local', workspace_id='default', resource_type='repository', name='Repo',
            aliases=[ResourceAlias(provider='github', namespace='repository', value='garnser/codex-web')])
        observed = await provider.validation_runs(binding, resource, selection['head'], credential='fixture-credential')
        self.assertEqual(observed[0].selection_revision, selection['head'])
        self.assertTrue(observed[0].selection_full)

    async def test_exact_provider_selection_output_and_latest_attempt_are_used(self):
        revision = 'a' * 40
        rows = [{'id':1,'path':'.github/workflows/tests.yml','head_sha':revision,'run_number':1,'run_attempt':1,
                 'status':'completed','conclusion':'success','event':'push','head_branch':'main'},
                {'id':2,'path':'.github/workflows/tests.yml','head_sha':revision,'run_number':1,'run_attempt':2,
                 'status':'completed','conclusion':'success','event':'push','head_branch':'main'}]
        class Client:
            async def get_json(inner, base, path, **kwargs):
                if path.endswith('/code-scanning/default-setup'):
                    return SECURITY_CONFIGURATION
                if path.endswith('/actions/runs'):
                    return {'total_count':2, 'workflow_runs':rows}
                self.assertIn('/runs/2/jobs',path)
                return {'total_count':1,'jobs':[{'id':10,'name':'validation-selection','status':'completed','conclusion':'success','head_sha':revision}]}
            async def request_bytes(inner, *args, **kwargs):
                return ('2026-10-14T00:00:00Z '+json.dumps({'full':'true','tested_revision':revision})+'\n').encode(), False, 'text/plain'
        provider = GitHubCodeHostProvider(client=Client())
        binding = CodeHostProviderBinding(id='binding', organization_id='local', workspace_id='default', provider_type='github',
            provider_instance='github.com', base_url='https://api.github.com', capabilities=tuple(provider.capabilities()))
        resource = Resource(organization_id='local', workspace_id='default', resource_type='repository', name='Repo',
            aliases=[ResourceAlias(provider='github',namespace='repository',value='group/app')])
        observed = await provider.validation_runs(binding, resource, revision, credential='fixture-credential')
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0].external_id,'2')
        self.assertTrue(observed[0].selection_full)
        self.assertEqual(observed[0].selection_revision,revision)
        rows[1]['head_sha']='b'*40
        with self.assertRaises(AssertionError):
            await provider.validation_runs(binding,resource,revision,credential='fixture-credential')
