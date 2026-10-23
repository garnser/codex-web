"""Actual published procedure/worker/intent/Evidence owners; simulated GitLab only."""
import copy
import json
import unittest

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.resources import ResourceAlias, ResourceUpdate
from codex_web.resources import ResourceCreate
from codex_web.execution_workers import ExecutionAssignmentCreate
from codex_web.services.action_intents import ActionIntentConflictError
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.services.code_host_action_contract import (
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID, CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID,
)
from tests.test_procedure_execution import ProcedureExecutionTests


class MergeObservationClient:
    def __init__(self):
        self.row = {"iid":33,"state":"opened","sha":"a"*40,"merge_commit_sha":None,
            "source_branch":"delivery","target_branch":"main","draft":False,
            "merge_status":"can_be_merged","detailed_merge_status":"mergeable",
            "head_pipeline":{"id":9,"status":"success"}}
        self.reads = []
        self.merges = 0
        self.after_read = None

    async def merge_request(self, api_base, project, iid, *, token):
        if (api_base,project,iid) != ("https://gitlab.example/api/v4","group/private",33):
            raise AssertionError("read escaped exact governed repository/MR")
        self.reads.append((project,iid))
        row = copy.deepcopy(self.row)
        if self.after_read is not None:
            self.after_read()
        return row

    async def accept_merge_request(self, api_base, project, iid, *, token, payload):
        if payload["sha"] != self.row["sha"]:
            raise AssertionError("merge head CAS changed")
        self.merges += 1
        self.row.update(state="merged",merge_commit_sha="b"*40)
        return copy.deepcopy(self.row)


class MergeVerificationProcedureTests(ProcedureExecutionTests):
    def assignment_options(self):
        return {"repository_scope":{"organization_id":self.actor.organization_id,
            "workspace_id":self.actor.workspace_id,"project_id":"home",
            "writable_repository_ids":[self.repo.id],"source":"explicit"}}

    def configure(self, content):
        self.repo = self.resources.update(self.repo.id,ResourceUpdate(aliases=[*self.repo.aliases,
            ResourceAlias(namespace="gitlab-project",provider="gitlab",value="group/private")]),actor=self.actor)
        self.client = MergeObservationClient()
        self.provider = GitLabActionProvider(self.resources,self.client,
            provider_instance="gitlab.example",api_base="https://gitlab.example/api/v4")
        self.registry.register(self.provider,tenant_scope=self.actor.tenant)
        self.binding = self.registry.bind(ActionProviderBindingCreate(provider_type="gitlab",
            provider_instance="gitlab.example",project_id="home",resource_ids=self.assignment.resource_ids,
            credential_ref=self.secret_fixture_ref,
            security_policy={"network":{"enabled":True,"allowed_schemes":["https"],"allowed_hosts":["gitlab.example"],"allowed_ports":[443]}}),
            actor=self.actor,resources=self.resources)
        self.snapshot = self.artifact(json.dumps({"pull_request_number":33,"expected_head_sha":"a"*40,
            "merge_method":"merge"}).encode(),"merge-inputs")
        def value(name,type):
            return {"name":name,"type":type,"description":"Canonical exact MR value"}
        outputs = [value("pull_request_number","integer"),value("head_sha","string"),value("merge_commit_sha","string")]
        def step(name,kind,action,inputs,outputs):
            return {"id":name,"kind":kind,"description":"Use the registered exact repository operation",
                "inputs":inputs,"outputs":outputs,"preconditions":[],
                "expected_evidence":[{"name":"verification","kind":"verification","description":"Actual original provider verification"}],
                "timeout_seconds":30,"max_attempts":1,"retry_backoff_seconds":0,
                "permissions":[{"capability":"repository.pull-request.merge" if kind=="mutation" else "repository.read.metadata",
                    "level":"execute" if kind=="mutation" else "read","resource_selector":"repository"}],
                "failure":{"outcome":"escalate","escalation_owner_identity_id":self.actor.identity_id,"attention_reason":"Inspect the same original intent; never replay unknown work"},
                "action":{"provider_type":"gitlab","provider_instance":"gitlab.example","action_id":action,
                    "provider_contract_version":"1.2","resource_selector":"repository"}}
        top = [value("pull_request_number","integer"),value("expected_head_sha","string"),value("merge_method","string")]
        merge = step("merge","mutation",CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,top,outputs)
        merge.update(impact={"resource_selector":"repository","change":"configuration","description":"Merge the exact existing repository reference; no service restart","max_unavailable_seconds":0,"reversible":False},
            verification_step_id="observe")
        observe = step("observe","verification",CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID,outputs,
            [*outputs,value("merge_verified","boolean")])
        observe["preconditions"]=[{"source":"step","name":"merge","predicate":"succeeded"}]
        return {"schema_version":"1.0","owner_identity_id":self.actor.identity_id,
            "task_triggers":["existing-mr-close"],"operation_triggers":[CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID],
            "resource_selectors":[{"id":"repository","resource_types":["repository"],"resource_ids":[self.repo.id]}],
            "environments":["staging"],"inputs":top,"reviewed_at":1,"review_due_at":4102444800,"expires_at":4102444801,
            "required_provider_capabilities":[],"required_worker_capabilities":[],"conflict_group":"private-merge",
            "precedence":1,"max_duration_seconds":60,"steps":[merge,observe]}

    async def test_published_two_step_secret_reference_merge_then_actual_read_verification(self):
        first = await self.advance()
        self.assertEqual(first.steps["merge"].status,"succeeded",first.failure_code)
        self.assertEqual(self.client.merges,1)
        second = await self.advance()
        self.assertEqual(second.steps["observe"].status,"succeeded",second.failure_code)
        completed = await self.advance()
        self.assertEqual(completed.status,"completed",completed.failure_code)
        self.assertEqual(self.client.merges,1)
        self.assertEqual(len(self.client.reads),4)  # merge preflight+verify, observe+independent verify
        origins = []
        for state in completed.steps.values():
            intent = self.intents.get(state.action_intent_id,self.actor)
            history = self.intents.store.load(intent_id=intent.id)
            self.assertEqual(len(history.receipts),1)
            self.assertEqual(intent.status,"succeeded")
            proof = self.artifacts.get_evidence(state.evidence["verification"],actor=self.actor)
            self.assertEqual(proof.source,"action-intent:"+intent.id)
            self.assertEqual(proof.metadata["verification_id"],intent.last_verification_id)
            origins.append(intent.id)
        self.assertEqual(len(set(origins)),2)

    async def test_changed_merge_observation_secret_reference_refuses_no_second_mutation(self):
        await self.advance()
        self.client.row["merge_commit_sha"]="c"*40
        result = await self.advance()
        self.assertNotEqual(result.steps["observe"].status,"succeeded")
        self.assertEqual(self.client.merges,1)

    async def test_worker_revocation_during_read_secret_reference_blocks_verified_completion(self):
        await self.advance()
        def revoke():
            self.client.after_read = None
            self.workers.set_lifecycle(self.worker.id,"revoked",actor=self.actor,reason="Private current-custody negative")
        self.client.after_read = revoke
        result = await self.advance()
        self.assertNotEqual(result.steps["observe"].status,"succeeded")
        self.assertEqual(self.client.merges,1)
        self.assertEqual(len(self.client.reads),3)

    async def test_binding_revocation_at_verify_edge_secret_reference_blocks_result(self):
        await self.advance()
        def revoke_at_fourth_read():
            if len(self.client.reads)==4:
                self.registry.set_enabled(self.binding.id,False,actor=self.actor)
        self.client.after_read = revoke_at_fourth_read
        result = await self.advance()
        self.assertNotEqual(result.steps["observe"].status,"succeeded")
        self.assertEqual(self.client.merges,1)
        self.assertEqual(len(self.client.reads),4)

    async def test_unknown_read_secret_reference_retains_same_original_no_second_transport(self):
        await self.advance()
        def lose_response():
            raise TimeoutError("Controlled read response lost")
        self.client.after_read = lose_response
        first = await self.advance()
        original = first.steps["observe"].action_intent_id
        self.assertEqual(first.steps["observe"].status,"reconciliation_required")
        second = await self.advance()
        self.assertEqual(second.steps["observe"].action_intent_id,original)
        self.assertEqual(len(self.client.reads),3)
        self.assertEqual(self.client.merges,1)
        history = self.intents.store.load(intent_id=original)
        self.assertEqual(len(history.receipts),1)
        self.assertEqual(history.receipts[0].outcome,"unknown")

    async def test_exact_read_scope_secret_reference_does_not_authorize_mutation_or_foreign_repo(self):
        other = self.resources.create(ResourceCreate(resource_type="repository",name="Private writable sibling"),actor=self.actor)
        assignment = self.workers.create_assignment(ExecutionAssignmentCreate(subject={"kind":"thread","ref":"private-read-scope"},
            execution_id="private-read-scope",project_id="home",resource_ids=(other.id,self.repo.id),
            execution_contract_version="1.0",required_capabilities=("command_execution",),
            repository_scope={"organization_id":self.actor.organization_id,"workspace_id":self.actor.workspace_id,
                "project_id":"home","writable_repository_ids":[other.id],"read_only_repository_ids":[self.repo.id],"source":"explicit"}),actor=self.actor)
        from codex_web.action_providers import ActionRequest
        request = ActionRequest(action_id=CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID,
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,project_id="home",
            resource_ids=(self.repo.id,),parameters={"pull_request_number":33,"head_sha":"a"*40,"merge_commit_sha":"b"*40})
        from unittest.mock import patch
        with patch.object(self.secret_broker,"metadata",side_effect=AssertionError("scope inspection must not consume credentials")):
            self.intents._validate_execution_repository_scope(assignment.execution_id,request,self.actor,binding_id=self.binding.id)
        mutation = request.model_copy(update={"action_id":CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID})
        with self.assertRaisesRegex(ActionIntentConflictError,"writable scope"):
            self.intents._validate_execution_repository_scope(assignment.execution_id,mutation,self.actor,binding_id=self.binding.id)
        self.provider.contract_version="1.1"
        with self.assertRaisesRegex(ActionIntentConflictError,"provider contract"):
            self.intents._validate_execution_repository_scope(assignment.execution_id,request,self.actor,binding_id=self.binding.id)

    async def test_strict_read_inputs_secret_reference_refuse_before_transport(self):
        from codex_web.action_providers import ActionRequest
        request = ActionRequest(action_id=CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID,
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,
            resource_ids=(self.repo.id,),parameters={"pull_request_number":33,"head_sha":"a"*40,"merge_commit_sha":"b"*40})
        for change in ({"pull_request_number":True},{"head_sha":"A"*40},{"merge_commit_sha":""},{"url":"https://untrusted.invalid"}):
            with self.assertRaises(ValueError):
                self.provider.contract.pull_request_verify_merge(request.model_copy(update={"parameters":{**request.parameters,**change}}))
        self.assertEqual(self.client.reads,[])
        self.assertEqual(self.client.merges,0)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(MergeVerificationProcedureTests(name)
        for name in MergeVerificationProcedureTests.__dict__ if name.startswith("test_"))
