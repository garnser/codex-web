from __future__ import annotations

import json
import gzip
import base64
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

    def test_native_pr_security_result_required_and_failures_not_hidden_by_successful_analysis_jobs(self):
        tests, recovery, security = self.runs()
        tests=tests.model_copy(update={'event':'pull_request','head_branch':'source'})
        observed=json.loads((ROOT/'1429-codeql-result.json').read_text())
        proof={'protocol_version':'github-codeql-check/1','external_id':str(observed['id']),
               'issuer':observed['app']['slug'],'revision':self.revision,'state':observed['status'],
               'conclusion':observed['conclusion']}
        validate_delivery_runs((tests,security.model_copy(update={'security_result':proof})),self.revision,recovery_required=False)
        for change in (None, {'conclusion':'failure'}, {'conclusion':'cancelled'},
                       {'state':'in_progress','conclusion':None}, {'conclusion':'skipped'}, {'issuer':'github-actions'}):
            result=None if change is None else {**proof,**change}
            with self.subTest(change=change),self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((tests,security.model_copy(update={'security_result':result})),self.revision,recovery_required=False)

    def test_control_workflow_revision_alone_does_not_prove_recovery_candidate_source(self):
        tests, recovery, security = self.runs()
        for proof in (None, {**recovery.recovery_publication, 'commit':'b'*40}):
            with self.subTest(proof=proof), self.assertRaises(PublicationRecoveryConflict):
                validate_delivery_runs((tests, recovery.model_copy(update={'recovery_publication':proof}), security),
                                       self.revision, recovery_required=True)

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
    async def test_candidate_source_is_independent_of_newer_workflow_control_revision(self):
        import io, zipfile, hashlib
        manifest=json.loads((ROOT/'candidate-protocol-conformance.json').read_text())
        revision=manifest['commit']; control='b'*40
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('candidate-evidence.json',json.dumps(manifest))
        raw=stream.getvalue()
        candidate={**observation(38628116952,'artifacts')['artifacts'][0],
                   'digest':'sha256:'+hashlib.sha256(raw).hexdigest()}
        release={**observation(38628116952,'run'),'head_sha':control}
        class Client:
            async def get_json(inner, base, path, **kwargs):
                if path.endswith('/code-scanning/default-setup'):return SECURITY_CONFIGURATION
                if path.endswith('/commits/'+revision+'/check-runs'):return {'total_count':0,'check_runs':[]}
                if path.endswith('/actions/runs'):
                    return {'total_count':2,'workflow_runs':[observation(38627482110,'run'),observation(38627481555,'run')]}
                if path.endswith('/actions/workflows/publish-release.yml/runs'):
                    return {'total_count':1,'workflow_runs':[release]}
                if path.endswith('/artifacts'):return {'total_count':1,'artifacts':[candidate]}
                for run_id in (38627482110,38627481555,38628116952):
                    if path.endswith('/'+str(run_id)+'/jobs'):
                        jobs=observation(run_id,'jobs')
                        if run_id==38628116952:
                            jobs={'total_count':jobs['total_count'],'jobs':[{**j,'head_sha':control} for j in jobs['jobs']]}
                        return jobs
                raise AssertionError('Unexpected provider read '+path)
            async def request_bytes(inner,*args,**kwargs):
                if args[2].endswith('/zip'):return raw,False,'application/zip'
                return (json.dumps({'full':'true','head':revision,'tested_revision':revision})+'\n').encode(),False,'text/plain'
        provider=GitHubCodeHostProvider(client=Client())
        binding=SimpleNamespace(base_url='https://api.github.com')
        resource=Resource(organization_id='local',workspace_id='default',resource_type='repository',name='Repo',
            aliases=[ResourceAlias(provider='github',namespace='repository',value='garnser/codex-web')])
        facts=await provider.validation_runs(binding,resource,revision,credential='fixture-credential')
        release_fact=next(f for f in facts if f.workflow_path.endswith('publish-release.yml'))
        self.assertEqual(release_fact.control_revision,control)
        self.assertEqual(release_fact.revision,revision)
        self.assertEqual(release_fact.recovery_publication['commit'],revision)
        validate_delivery_runs(facts,revision,recovery_required=True)

    async def test_exact_recovery_artifact_manifest_is_bounded_and_independent_of_workflow_control_head(self):
        import io, zipfile, hashlib
        manifest=json.loads((ROOT / 'candidate-protocol-conformance.json').read_text())
        revision=manifest['commit']
        metadata=observation(38628116952, 'artifacts')
        changes=[None, {'commit':'b'*40}, {'schema_version':'future'}, {'stage':'promotion'},
                 {'deterministic_qualification':{'compose_nginx':True}}]
        for change in changes:
            current={**manifest, **(change or {})}
            stream=io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('candidate-evidence.json', json.dumps(current))
            raw=stream.getvalue()
            candidate={**metadata['artifacts'][0], 'digest':'sha256:'+hashlib.sha256(raw).hexdigest()}
            class Client:
                async def get_json(inner, *args, **kwargs):
                    return {'total_count':1,'artifacts':[candidate]}
                async def request_bytes(inner, *args, **kwargs):
                    self.assertEqual(kwargs['max_bytes'],320 * 1024)
                    return raw, False, 'application/zip'
            provider=GitHubCodeHostProvider(client=Client())
            binding=SimpleNamespace(base_url='https://api.github.com')
            resource=Resource(organization_id='local',workspace_id='default',resource_type='repository',name='Repo',
                aliases=[ResourceAlias(provider='github',namespace='repository',value='garnser/codex-web')])
            if change is None:
                proof=await provider._recovery_publication(binding,resource,38628116952,revision,credential='fixture-credential')
                self.assertEqual(proof['commit'],revision)
                self.assertEqual(proof['artifact_id'],str(candidate['id']))
            else:
                with self.subTest(change=change), self.assertRaises(CodeHostError):
                    await provider._recovery_publication(binding,resource,38628116952,revision,credential='fixture-credential')
            candidate['expired']=True
            with self.assertRaises(CodeHostError):
                await provider._recovery_publication(binding,resource,38628116952,revision,credential='fixture-credential')

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
                if path.endswith('/actions/workflows/publish-release.yml/runs'):
                    return {'total_count':0,'workflow_runs':[]}
                if path.endswith('/actions/runs'):
                    return {'total_count': 1, 'workflow_runs': [run]}
                return jobs
            async def request_bytes(inner, *args, **kwargs):
                self.assertEqual(kwargs['max_bytes'], 320 * 1024)
                metadata=json.loads((ROOT/'1429-selection-log-metadata.json').read_text())
                raw=gzip.decompress(base64.b64decode(metadata['content_gzip_base64']))
                self.assertEqual(len(raw),86275)
                self.assertEqual(raw.index(b'"head"'),metadata['operator_raw_head_offset'])
                self.assertEqual(raw.index(b'"tested_revision"'),metadata['operator_raw_tested_revision_offset'])
                return raw, False, 'text/plain'
        provider = GitHubCodeHostProvider(client=Client())
        binding = CodeHostProviderBinding(id='binding', organization_id='local', workspace_id='default',
            provider_type='github', provider_instance='github.com', base_url='https://api.github.com',
            capabilities=tuple(provider.capabilities()))
        resource = Resource(organization_id='local', workspace_id='default', resource_type='repository', name='Repo',
            aliases=[ResourceAlias(provider='github', namespace='repository', value='garnser/codex-web')])
        observed = await provider.validation_runs(binding, resource, selection['head'], credential='fixture-credential')
        self.assertEqual(observed[0].selection_revision, selection['head'])
        self.assertTrue(observed[0].selection_full)
        self.assertEqual(observed[0].tested_revision, selection['tested_revision'])

    async def test_oversized_missing_ambiguous_or_truncated_selection_proof_is_refused(self):
        selection=json.loads((ROOT/'1429-selection.json').read_text())
        metadata=json.loads((ROOT/'1429-selection-log-metadata.json').read_text())
        raw=gzip.decompress(base64.b64decode(metadata['content_gzip_base64']))
        run=observation(38626937167,'run');jobs=observation(38626937167,'jobs')
        proof=json.dumps({k:selection[k] for k in ('full','head','tested_revision')}).encode()+b'\n'
        for content,truncated in ((raw,True),(raw+proof,False),(b'checkout only\n',False),(raw[:65536],True),
                                  (b' '*327680+proof,True)):
            class Client:
                async def get_json(inner,base,path,**kwargs):
                    if path.endswith('/code-scanning/default-setup'):return SECURITY_CONFIGURATION
                    if path.endswith('/actions/runs'):return {'total_count':1,'workflow_runs':[run]}
                    return jobs
                async def request_bytes(inner,*args,**kwargs):
                    bound=kwargs['max_bytes'];self.assertEqual(bound,327680)
                    return content[:bound],truncated,'text/plain'
            provider=GitHubCodeHostProvider(client=Client())
            binding=SimpleNamespace(base_url='https://api.github.com')
            resource=Resource(organization_id='local',workspace_id='default',resource_type='repository',name='Repo',
                aliases=[ResourceAlias(provider='github',namespace='repository',value='garnser/codex-web')])
            with self.subTest(size=len(content),truncated=truncated),self.assertRaises(CodeHostError):
                await provider.validation_runs(binding,resource,selection['head'],credential='fixture-credential')

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
                if path.endswith('/actions/workflows/publish-release.yml/runs'):
                    return {'total_count':0,'workflow_runs':[]}
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
