"""Canonical Project-owned published procedure, isolated from Home discovery."""
import unittest

from codex_web.models import ProjectCreate
from codex_web.services.projects import ProjectService
from codex_web.storage.projects import ProjectRepository
from codex_web.workspaces import WorkspaceMapper
from codex_web.services.procedure_discovery import OperationContext
from tests.test_procedure_gitlab_merge_verification import MergeVerificationProcedureTests


class ProjectScopedMergeProcedureTests(MergeVerificationProcedureTests):
    def configure_project(self, project):
        self.projects = ProjectService(ProjectRepository(self.root / "projects.json",
            store=self.sqlite, workspace_mapper=WorkspaceMapper(root=self.root)))
        self.home_project = self.projects.get("home", self.actor.tenant)
        return self.projects.create(ProjectCreate(name="VeridataOps fixture",path=project.path),
            scope=self.actor.tenant)

    def skill_create_options(self):
        return {"project_id":self.project.id}

    def configure(self, content):
        self.skills.projects = self.projects
        return super().configure(content)

    async def test_project_required_secret_reference_two_step_executes_without_home_selection(self):
        record = self.definition_registry.get_record(self.pin.record_id)
        self.assertEqual(record.scope_type,"project")
        self.assertEqual(record.scope_id,self.project.id)
        self.assertNotEqual(self.project.id,self.home_project.id)
        self.assertEqual(self.assignment.project_id,self.project.id)
        self.assertEqual(self.skills.validate_required_procedure(self.pin,actor=self.actor,
            project_id=self.project.id),self.pin)
        # Supply the same actual repository facts and operation: only the
        # canonical Project boundary should exclude this required procedure.
        context = OperationContext(actor=self.actor,project_id=self.home_project.id,
            resource_ids=self.assignment.resource_ids,operation="code-host.pull-request.merge",
            policy_source="canonical:private-project-isolation",policy_allows=True,
            enforcement="canonical_action",
            **self.discovery.resource_facts(self.assignment.resource_ids,self.actor))
        home, instructions = self.discovery.discover(context)
        self.assertTrue(home.admitted,home)
        self.assertEqual(home.outcome,"no_match")
        self.assertEqual(home.selected,())
        self.assertNotIn(self.pin,{candidate.reference for candidate in home.candidates})
        self.assertEqual(instructions,"")
        # This uses the actual published Skill lifecycle, canonical assignment,
        # ActionIntent/receipt/verification/Evidence owners and governed output.
        await super().test_published_two_step_secret_reference_merge_then_actual_read_verification()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ProjectScopedMergeProcedureTests(name)
        for name in ProjectScopedMergeProcedureTests.__dict__ if name.startswith("test_"))
