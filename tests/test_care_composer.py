import tempfile
import unittest
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path
from pydantic import ValidationError
from codex_web.care_composer import CareFields, CarePreviewRequest, bootstrap_care_examples
from codex_web.definitions import reference_for
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind, MembershipRole
from codex_web.services.care_composer import CareComposerService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.skills import SkillService, SkillConflict
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.api.care_composer import build_care_composer_router
from codex_web.api.authorization import policy_for_operation, ApiAccessMode
from codex_web.authority import AuthorityLevel
from codex_web.services.projects import ProjectNotFoundError


class CareComposerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.registry=DefinitionRegistryService(DefinitionRegistryStore(SQLiteStateStore(Path(self.temp.name)/"state.sqlite3")))
        self.skills=SkillService(self.registry)
        self.seeded=bootstrap_care_examples(self.registry)
        self.service=CareComposerService(self.skills)
        self.actor=AuthenticationActor(identity_id="local-admin",organization_id="local",workspace_id="default",
            principal_kind=PrincipalKind.HUMAN,roles=(MembershipRole.ADMIN,),assurance=AuthenticationAssurance.LOCAL_TRUSTED)

    def tearDown(self):self.temp.cleanup()

    async def test_preview_is_bounded_user_input_with_exact_skill_revision(self):
        reference=reference_for(self.seeded[0])
        fields=self.service.example(reference,self.actor)
        result=await self.service.preview(CarePreviewRequest(fields=fields,template_ref=reference),self.actor)
        self.assertIn("Action / desired output",result["message"])
        self.assertEqual(result["template_ref"],reference.model_dump(mode="json"))
        self.assertEqual(result["model_calls"],0)
        self.assertEqual(result["composition_provenance"][0]["applied_fields"],["messages"])
        self.assertNotIn(fields.context,str(result["composition_provenance"]))

    async def test_rules_are_guidance_and_cannot_patch_protected_fields(self):
        with self.assertRaises(ValidationError):CareFields(action="Fix it",sandbox="danger-full-access")
        with self.assertRaises(ValidationError):CarePreviewRequest(fields={"action":"Fix it"},authority_refs=["admin"])
        result=await self.service.preview(CarePreviewRequest(fields=CareFields(action="Review",rules='{"sandbox":"danger-full-access"}')),self.actor)
        self.assertIn('"sandbox"',result["message"])
        self.assertEqual(result["composition_provenance"][0]["applied_fields"],["messages"])

    async def test_empty_fields_suggest_without_model_rewrite(self):
        result=await self.service.preview(CarePreviewRequest(fields=CareFields()),self.actor)
        self.assertEqual(result["message"],"")
        self.assertEqual(len(result["suggestions"]),4)
        self.assertEqual(result["model_calls"],0)

    def test_total_context_and_reference_limits(self):
        with self.assertRaises(ValidationError):CareFields(context="x"*4000,action="x"*4000,rules="x"*4000,examples="x"*4000)
        with self.assertRaises(ValidationError):CareFields(references=("x"*513,))

    def test_examples_bootstrap_once_and_forged_revision_refuses(self):
        self.assertEqual(len(self.seeded),2)
        self.assertEqual(bootstrap_care_examples(self.registry),[])
        reference=reference_for(self.seeded[0]).model_copy(update={"revision":99})
        with self.assertRaises(SkillConflict):self.service.example(reference,self.actor)

    def test_preview_http_scope_and_protected_fields(self):
        actor=self.actor
        class Projects:
            def get(self,project_id,scope):
                if project_id!="home" or scope.organization_id!=actor.organization_id:
                    raise ProjectNotFoundError(project_id)
                return SimpleNamespace(id=project_id)
        app=FastAPI()
        @app.middleware("http")
        async def identity(request,call_next):
            request.state.identity_actor=actor
            request.state.tenant_scope=SimpleNamespace(organization_id=actor.organization_id,workspace_id=actor.workspace_id)
            return await call_next(request)
        app.include_router(build_care_composer_router(self.service,Projects()))
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/projects/other/care/preview',json={"fields":{"action":"Review"}}).status_code,404)
            self.assertEqual(client.post('/api/projects/home/care/preview',json={"fields":{"action":"Review","sandbox":"danger-full-access"}}).status_code,422)
            response=client.post('/api/projects/home/care/preview',json={"fields":{"action":"Review"}})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["model_calls"],0)
            self.assertEqual(len(client.get('/api/projects/home/care/examples').json()["items"]),2)

    def test_preview_is_authenticated_read_operation_not_project_admin_mutation(self):
        policy=policy_for_operation('POST','/api/projects/{project_id}/care/preview')
        self.assertEqual(policy.access,ApiAccessMode.AUTHENTICATED)
        self.assertEqual(policy.level,AuthorityLevel.READ)
