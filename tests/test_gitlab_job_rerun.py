"""Governed GitLab job retry, with private transport and canonical owners (#2021)."""
from __future__ import annotations

from copy import deepcopy
import unittest

import httpx

from tests import test_gitlab_action_provider as fixtures
from codex_web.action_intents import (
    ActionDecisionSnapshot, ActionIntentClaimRequest, ActionIntentCreate,
    ActionIntentReconcileRequest, ActionIntentRetryRequest, ActionIntentStatus,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.action_intents import (
    ActionIntentService, ActionIntentConflictError,
)
from codex_web.services.action_providers import ActionProviderError, ActionRequirementError
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.code_host_action_contract import CODE_HOST_JOB_RERUN_ACTION_ID
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.definition_registry import DefinitionRegistryStore


class GitLabJobRerunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Composition keeps the borrowed TestCase out of unittest discovery.
        self.f = fixtures.GitLabActionProviderTests(methodName='runTest')
        await self.f.asyncSetUp()
        self.job = {
            'id': 91, 'name': 'unit', 'stage': 'test', 'status': 'failed',
            'archived': False, 'ref': 'feature/61',
            'commit': {'id': 'a' * 40},
            'pipeline': {'id': 81, 'project_id': 42, 'sha': 'a' * 40, 'ref': 'feature/61'},
            'web_url': 'https://gitlab.example/group/platform/codex-web/-/jobs/91',
        }
        self.jobs = {91: deepcopy(self.job)}
        self.retry_response = None
        self.transport_error = None
        self.seen = []
        self.posts = 0

        async def handler(request):
            self.seen.append(request)
            self.assertEqual(request.headers['PRIVATE-TOKEN'], 'secret-gitlab-token')
            prefix = '/api/v4/projects/group/platform/codex-web'
            path = request.url.path
            if request.method == 'GET' and path == prefix:
                return httpx.Response(200, json={'id': 42})
            if request.method == 'GET' and path.startswith(prefix + '/jobs/'):
                key = int(path.rsplit('/', 1)[1])
                return httpx.Response(200, json=self.jobs[key])
            if request.method == 'POST' and path == prefix + '/jobs/91/retry':
                self.posts += 1
                self.assertFalse(request.content)  # no variables, inputs, or new scope
                new = deepcopy(self.job)
                new.update(id=92, status='pending', web_url=self.job['web_url'].removesuffix('91') + '92')
                self.jobs[92] = new
                if self.transport_error is not None:
                    raise self.transport_error
                return httpx.Response(201, json=self.retry_response or new)
            self.fail('unexpected private GitLab request path')

        self.f.provider.client = GitLabClient(transport=httpx.MockTransport(handler))

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    def request(self, parameters=None):
        return self.f._request(CODE_HOST_JOB_RERUN_ACTION_ID,
                               {'job_id': 91} if parameters is None else parameters)

    async def execute(self):
        return await self.f.execution.execute(self.f.binding.id, self.request(), actor=self.f.actor)

    async def verify(self, result):
        return await self.f.execution.verify(self.f.binding.id, result, actor=self.f.actor)

    async def intent(self):
        authority = install_authority_roles(
            DefinitionRegistryService(DefinitionRegistryStore(self.f.sqlite)), self.f.resources)
        service = ActionIntentService(ActionIntentStore(self.f.sqlite), self.f.execution,
                                      authority=authority, identity=self.f.identity)
        worker = AuthenticationActor(identity_id='action-worker', principal_kind=PrincipalKind.SERVICE,
            organization_id=self.f.actor.organization_id, workspace_id=self.f.actor.workspace_id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=('action-intent:worker', 'secret:use'))
        # Same explicit controlled canonical-policy convention as the existing
        # procedure-result fixtures; this is not a live trust/QA proof.
        creation = ActionIntentCreate(binding_id=self.f.binding.id, request=self.request(),
            policy_decision=ActionDecisionSnapshot(outcome='allow', source='policy:private-job-rerun',
                reason='Controlled fixture permits this exact repository job retry'))
        original = service.create(creation, actor=self.f.actor)
        self.assertEqual(original.retry_policy.max_attempts, 1)
        service.claim(ActionIntentClaimRequest(worker_id='worker-1', lease_seconds=30),
                      intent_id=original.id, actor=worker)
        completed = await service.execute_claimed(original.id, 'worker-1', actor=worker)
        self.assertNotEqual(completed.status, ActionIntentStatus.CANCELLED, completed.last_error)
        return service, worker, creation, completed

    async def test_failed_job_is_registered_executed_once_and_independently_verified(self):
        # OLD uses only the preexisting public catalog/API, and fails here on
        # absent GitLab capability rather than a missing new client method.
        self.assertIn(CODE_HOST_JOB_RERUN_ACTION_ID,
                      {a.action_id for a in self.f.provider.actions()},
                      'Governed GitLab failed-job retry is unavailable')
        prepared = await self.f.execution.prepare(self.f.binding.id, self.request(), actor=self.f.actor)
        self.assertEqual(prepared.provider_plan['job_id'], 91)
        result = await self.execute()
        self.assertEqual(self.posts, 1)
        self.assertEqual(result.external_id, '92')
        self.assertEqual(result.output['job_id'], 91)
        self.assertEqual(result.output['retried_job_id'], 92)
        self.assertEqual(result.output['pipeline_id'], 81)
        self.assertEqual(result.output['head_sha'], 'a' * 40)
        self.assertEqual(result.evidence[0].evidence_type, 'code-host-job-rerun')
        self.assertEqual(self.jobs[91]['status'], 'failed')
        self.assertTrue((await self.verify(result)).verified)
        self.assertEqual(self.posts, 1)
        self.assertIn('/projects/group%2Fplatform%2Fcodex-web/jobs/91/retry', str(self.seen[2].url))
        self.assertNotIn('merge_verified', result.output)
        self.assertNotIn('ci_passed', result.output)

    async def test_canceled_terminal_job_can_be_retried(self):
        self.jobs[91]['status'] = 'canceled'
        result = await self.execute()
        self.assertTrue((await self.verify(result)).verified)
        self.assertEqual(self.posts, 1)

    async def test_success_active_manual_and_unknown_jobs_never_post(self):
        for state in ('success', 'running', 'pending', 'manual', 'skipped', 'unknown', None):
            with self.subTest(state=state):
                self.jobs[91]['status'] = state
                with self.assertRaises(ActionRequirementError):
                    await self.execute()
                self.assertEqual(self.posts, 0)

    async def test_invalid_parameters_refused_before_provider_reads(self):
        for parameters in ({}, {'job_id': True}, {'job_id': '091'}, {'job_id': 0},
                           {'job_id': 91.0}, {'job_id': 91, 'variables': []}):
            with self.subTest(parameters=parameters):
                with self.assertRaises(ActionProviderError):
                    await self.f.execution.execute(self.f.binding.id, self.request(parameters), actor=self.f.actor)
                self.assertEqual(self.seen, [])

    async def test_missing_or_foreign_original_identity_never_posts(self):
        malformed = [dict(id=90), dict(id=True), dict(pipeline=None), dict(commit=None),
                     dict(name=''), dict(stage=None), dict(ref='other'), dict(archived=True)]
        for replacement in malformed:
            with self.subTest(replacement=replacement):
                self.jobs[91] = {**deepcopy(self.job), **replacement}
                with self.assertRaises(ActionRequirementError):
                    await self.execute()
                self.assertEqual(self.posts, 0)
        for field, value in (('project_id', 43), ('id', 0), ('sha', 'b' * 40)):
            with self.subTest(field=field):
                self.jobs[91] = deepcopy(self.job)
                self.jobs[91]['pipeline'][field] = value
                with self.assertRaises(ActionRequirementError):
                    await self.execute()
                self.assertEqual(self.posts, 0)

    async def test_mismatched_post_response_is_not_claimed_success(self):
        for field, value in (('id', 91), ('ref', 'foreign'), ('name', 'foreign'),
                             ('stage', 'foreign'), ('status', 'manual')):
            with self.subTest(field=field):
                response = deepcopy(self.job)
                response.update(id=92, status='pending')
                response[field] = value
                self.retry_response = response
                before = self.posts
                with self.assertRaises(ValueError):
                    await self.execute()
                self.assertEqual(self.posts, before + 1)

    async def test_changed_retry_pipeline_or_head_response_is_not_success(self):
        for field, value in (('id', 82), ('project_id', 43), ('sha', 'b' * 40)):
            with self.subTest(field=field):
                response = deepcopy(self.job)
                response.update(id=92, status='pending')
                response['pipeline'][field] = value
                self.retry_response = response
                with self.assertRaises(ValueError):
                    await self.execute()

    async def test_verification_requires_fresh_exact_retry_identity(self):
        result = await self.execute()
        original = deepcopy(self.jobs[92])
        for field, value in (('id', 93), ('name', 'foreign'), ('ref', 'foreign'),
                             ('stage', 'foreign'), ('status', 'manual')):
            with self.subTest(field=field):
                self.jobs[92] = {**deepcopy(original), field: value}
                self.assertFalse((await self.verify(result)).verified)
        for field, value in (('id', 82), ('project_id', 43), ('sha', 'b' * 40)):
            with self.subTest(pipeline_field=field):
                self.jobs[92] = deepcopy(original)
                self.jobs[92]['pipeline'][field] = value
                self.assertFalse((await self.verify(result)).verified)
        self.assertEqual(self.posts, 1)

    async def test_receipt_identity_and_original_scope_cannot_be_forged(self):
        result = await self.execute()
        for field, value in (('job_id', 92), ('retried_job_id', True),
                             ('job_project_id', 43), ('head_sha', 'b' * 40),
                             ('ref', 'foreign'), ('pipeline_id', 81.0), ('original_status', 'success')):
            with self.subTest(field=field):
                changed = result.model_copy(update={'output': {**result.output, field: value}})
                self.assertFalse((await self.verify(changed)).verified)
        changed = result.model_copy(update={'external_id': '91'})
        self.assertFalse((await self.verify(changed)).verified)
        self.jobs[91]['archived'] = True
        self.assertFalse((await self.verify(result)).verified)
        self.jobs[91]['archived'] = False
        self.jobs[91]['name'] = 'different original'
        self.assertFalse((await self.verify(result)).verified)

    async def test_foreign_binding_or_resource_is_refused_without_post(self):
        result = await self.execute()
        with self.assertRaises((ValueError, ActionProviderError)):
            await self.verify(result.model_copy(update={'provider_binding_id': 'foreign-binding'}))
        changed = result.model_copy(update={'output': {**result.output, 'resource_id': 'foreign-repository'}})
        with self.assertRaises((ValueError, ActionProviderError)):
            await self.verify(changed)
        self.assertEqual(self.posts, 1)

    async def test_foreign_execution_scope_and_credential_override_fail_before_transport(self):
        for request in (
            self.request().model_copy(update={'resource_ids': ('foreign-repository',)}),
            self.request().model_copy(update={'credential_ref': 'foreign-secret'}),
        ):
            with self.subTest(resource_ids=request.resource_ids, credential_ref=request.credential_ref):
                with self.assertRaises(ActionProviderError):
                    await self.f.execution.execute(self.f.binding.id, request, actor=self.f.actor)
                self.assertEqual(self.seen, [])

    async def test_canonical_intent_retains_original_and_deduplicates_same_request(self):
        service, worker, creation, completed = await self.intent()
        self.assertEqual(completed.status, ActionIntentStatus.SUCCEEDED, completed.last_error)
        self.assertEqual(completed.attempt, 1)
        duplicate = service.create(creation, actor=self.f.actor)
        self.assertEqual(duplicate.id, completed.id)
        self.assertEqual(self.posts, 1)
        history = service.history(completed.id, self.f.actor)
        self.assertNotIn('secret-gitlab-token', str(history))
        self.assertEqual(history['receipts'][-1]['outcome'], 'completed')

    async def test_policy_denial_is_canonical_and_never_reaches_transport(self):
        authority = install_authority_roles(
            DefinitionRegistryService(DefinitionRegistryStore(self.f.sqlite)), self.f.resources)
        service = ActionIntentService(ActionIntentStore(self.f.sqlite), self.f.execution,
                                      authority=authority, identity=self.f.identity)
        denied = service.create(ActionIntentCreate(binding_id=self.f.binding.id, request=self.request(),
            policy_decision=ActionDecisionSnapshot(outcome='deny', source='policy:private-job-rerun',
                reason='Controlled fixture refuses this repository job retry')), actor=self.f.actor)
        self.assertEqual(denied.status, ActionIntentStatus.CANCELLED)
        self.assertEqual(denied.attempt, 0)
        self.assertEqual(self.seen, [])

    async def test_response_loss_retains_unknown_and_never_reposts(self):
        self.transport_error = httpx.ReadError('private response lost')
        service, worker, creation, completed = await self.intent()
        self.assertEqual(completed.status, ActionIntentStatus.UNCERTAIN, completed.last_error)
        self.assertTrue(completed.failure.requires_reconciliation)
        self.assertEqual(self.posts, 1)
        self.assertEqual(service.history(completed.id, self.f.actor)['receipts'][-1]['outcome'], 'unknown')
        reconciled = await service.reconcile(completed.id, ActionIntentReconcileRequest(), actor=worker)
        self.assertEqual(reconciled.status, ActionIntentStatus.REQUIRES_RECONCILIATION)
        self.assertEqual(reconciled.attempt, 1)
        with self.assertRaises(ActionIntentConflictError):
            service.retry(completed.id, ActionIntentRetryRequest(reason='no blind retry'), actor=self.f.actor)
        self.assertEqual(self.posts, 1)
        self.assertEqual(service.create(creation, actor=self.f.actor).id, completed.id)

    async def test_malformed_post_receipt_retains_unknown_canonical_intent(self):
        self.retry_response = {'id': 92}
        service, worker, _, completed = await self.intent()
        self.assertEqual(completed.status, ActionIntentStatus.UNCERTAIN, completed.last_error)
        self.assertTrue(completed.failure.requires_reconciliation)
        self.assertEqual(self.posts, 1)
        self.assertEqual(service.history(completed.id, self.f.actor)['receipts'][-1]['outcome'], 'unknown')
