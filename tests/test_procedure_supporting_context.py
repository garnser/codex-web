import unittest
from unittest.mock import patch

from codex_web.organizational_memory import KnowledgeCreate, KnowledgeCanonicalRef, KnowledgeProvenance
from codex_web.services.procedure_discovery import OperationContext, ProcedureSupportingContextService
from tests import test_organizational_memory as fixtures


class ProcedureSupportingContextTests(unittest.TestCase):
    setUp = fixtures.OrganizationalMemoryTests.setUp
    tearDown = fixtures.OrganizationalMemoryTests.tearDown

    def create(self,key,*,project='project-a',target='resource-gitlab',deny=False,expired=False):
        return self.service.create(KnowledgeCreate(logical_key=key,object_type='other',title='GitLab rotation guidance',
            summary='GitLab service rotation reference',content=key+' GitLab service rotation guidance',project_id=project,
            canonical_refs=(KnowledgeCanonicalRef(object_type='resource',object_id=target),),
            provenance=KnowledgeProvenance(source_kind='repository',source_ref='repository:reviewed-guide'),
            deny_model_context=deny,valid_until=self.now-1 if expired else self.now+1000),actor=self.actor)

    def test_real_retrieval_bounds_scope_freshness_governance_provenance_and_zero_network(self):
        eligible=[self.create('eligible-'+str(i)) for i in range(8)]
        denied=self.create('protected-guidance',deny=True)
        self.create('other-project-guidance',project='project-b')
        self.create('other-resource-guidance',target='resource-other')
        self.create('expired-guidance',expired=True)
        context=OperationContext(actor=self.actor,project_id='project-a',resource_ids=('resource-gitlab',),operation='service.rotation')
        support=ProcedureSupportingContextService(self.service)
        with patch('socket.socket',side_effect=AssertionError('Routine retrieval must not call a model or network')):
            result=support(context,(),2048)
        self.assertLessEqual(len(result.text),2048)
        self.assertLessEqual(len([ref for ref in result.source_refs if ref.startswith('memory:')]),3)
        self.assertTrue(any(row.id in result.text for row in eligible))
        for forbidden in ('protected-guidance','other-project-guidance','other-resource-guidance','expired-guidance'):
            self.assertNotIn(forbidden,result.text)
        run=self.service.retrieval_run(result.source_refs[0].split(':',1)[1],actor=self.actor)
        self.assertEqual(run.top_k,3)
        self.assertLessEqual(run.packed_tokens,1000)
        self.assertTrue(any(row.knowledge_id==denied.id for row in run.denied))
        self.assertLessEqual(run.candidate_count,10)
