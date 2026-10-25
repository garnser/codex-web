import unittest

from pydantic import ValidationError

from codex_web.ux_briefs import UxBrief, UxBriefSection, UxBriefSource, UxBriefSelection
from codex_web.services.organizational_memory import KnowledgeValidationError


class UxBriefFormatTests(unittest.TestCase):
    def test_round_trip_preserves_exact_sources_and_sections(self):
        source = UxBriefSource(kind="knowledge", record_id="observed-v1", revision="1", sha256="a"*64)
        brief = UxBrief(sections={"observed_needs": UxBriefSection(text="Users need a visible recovery path.", sources=(source,)),
            "approved_copy": UxBriefSection(text="Try again")})
        restored = UxBrief.model_validate_json(brief.model_dump_json())
        self.assertEqual(restored, brief)
        self.assertEqual(restored.sections["observed_needs"].sources[0].record_id, "observed-v1")

    def test_empty_or_unknown_sections_and_authority_fields_refuse(self):
        for payload in ({"sections":{}}, {"sections":{"target_users":{"text":""}}},
            {"sections":{"sandbox":{"text":"danger-full-access"}}},
            {"sections":{"target_users":{"text":"Users","policy":"override"}}}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                UxBrief.model_validate(payload)

    def test_repeated_sources_cannot_escape_total_reference_budget(self):
        sources = tuple(UxBriefSource(kind="artifact", record_id=f"artifact-{i}", revision="main", sha256="a"*64) for i in range(17))
        with self.assertRaises(ValidationError):
            UxBrief(sections={"target_users":UxBriefSection(text="Users", sources=sources[:16]),
                "observed_needs":UxBriefSection(text="Needs", sources=sources[16:])})

    def test_reference_bytes_count_toward_context_limit(self):
        sources = tuple(UxBriefSource(kind="evidence", record_id=str(i)+"x"*498, revision="r"*1000, sha256="b"*64) for i in range(16))
        with self.assertRaises(ValidationError):
            UxBrief(sections={"target_users":UxBriefSection(text="Users", sources=sources)})

    def test_selection_is_bounded_and_revision_pinned(self):
        selected = UxBriefSelection(knowledge_id="brief-v1", version=1, sha256="a"*64, sections=("target_users",))
        self.assertEqual(selected.version, 1)
        for change in ({"max_context_tokens":4001}, {"version":0}, {"sha256":"unverified"},
            {"sections":("target_users","target_users")}, {"sections":()}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                UxBriefSelection.model_validate({**selected.model_dump(), **change})


class UxBriefRetrievalFoundationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_organizational_memory import OrganizationalMemoryTests
        OrganizationalMemoryTests.setUp(self)

    def tearDown(self):
        self.temp.cleanup()

    def create_memory(self, key, **changes):
        from codex_web.organizational_memory import KnowledgeCreate
        return self.service.create(KnowledgeCreate(logical_key=key, object_type="other", title="UX brief",
            summary="Curated task context", content="User needs and copy examples", project_id="project-a",
            provenance={"source_kind":"manual", "source_ref":"fixture:brief"}, **changes), actor=self.actor)

    def test_exact_selection_does_not_substitute_newer_logical_version(self):
        from codex_web.organizational_memory import KnowledgeQuery, KnowledgeRevise
        old=self.create_memory("ux.brief")
        newer=self.service.revise(old.id,KnowledgeRevise(content="Different user needs", reason="Research update",
            provenance={"source_kind":"manual", "source_ref":"fixture:updated"}),actor=self.actor)
        result=self.service.search(KnowledgeQuery(knowledge_ids=(old.id,),project_ids=("project-a",)),actor=self.actor)
        self.assertEqual(result.items,())
        historical=self.service.search(KnowledgeQuery(knowledge_ids=(old.id,),project_ids=("project-a",),include_superseded=True),actor=self.actor)
        self.assertEqual([item.knowledge_id for item in historical.items],[old.id])
        self.assertNotIn(newer.id,[item.knowledge_id for item in historical.items])

    def selection(self, record, max_tokens=2000):
        return UxBriefSelection(knowledge_id=record.id,version=record.version,sha256=record.content_sha256,
            sections=("approved_copy",),max_context_tokens=max_tokens)

    def test_projection_runs_only_after_governance_and_project_filters(self):
        from codex_web.organizational_memory import KnowledgeQuery, KnowledgeRetrievalBudget
        denied=self.create_memory("ux.denied",deny_model_context=True)
        calls=[]
        result=self.service.search(KnowledgeQuery(knowledge_ids=(denied.id,),project_ids=("project-a",),budget=KnowledgeRetrievalBudget(max_context_tokens=2000)),
            actor=self.actor,context_projection=lambda item:calls.append(item.id) or "Forbidden text",context_selection=self.selection(denied))
        self.assertEqual(result.items,())
        self.assertEqual(calls,[])
        allowed=self.create_memory("ux.allowed")
        wrong_project=self.service.search(KnowledgeQuery(knowledge_ids=(allowed.id,),project_ids=("project-b",),budget=KnowledgeRetrievalBudget(max_context_tokens=2000)),
            actor=self.actor,context_projection=lambda item:calls.append(item.id) or "Wrong Project",context_selection=self.selection(allowed))
        self.assertEqual(wrong_project.items,())
        self.assertEqual(calls,[])

    def test_selected_section_projection_and_pins_use_existing_retrieval_audit(self):
        from codex_web.organizational_memory import KnowledgeQuery, KnowledgeRetrievalBudget
        brief=self.create_memory("ux.selected")
        selection=self.selection(brief,128)
        result=self.service.search(KnowledgeQuery(knowledge_ids=(brief.id,),project_ids=("project-a",),
            budget=KnowledgeRetrievalBudget(top_k=1,max_context_tokens=128)),actor=self.actor,
            context_projection=lambda item:"Approved copy: Try again",context_selection=selection)
        self.assertEqual(len(result.items),1)
        self.assertIn("Approved copy: Try again",result.items[0].context_excerpt)
        self.assertNotIn(brief.content,result.items[0].context_excerpt)
        self.assertLessEqual(result.packed_tokens,128)
        audit=self.service.retrieval_run(result.retrieval_id,actor=self.actor)
        self.assertEqual(audit.selected_knowledge_ids,(brief.id,))
        self.assertEqual(audit.filters["context_selection"],selection.model_dump(mode="json"))
        self.assertNotIn("Try again",audit.model_dump_json())


class UxBriefMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        UxBriefRetrievalFoundationTests.setUp(self)
        from codex_web.services.ux_briefs import UxBriefService
        self.briefs=UxBriefService(self.service)

    tearDown=UxBriefRetrievalFoundationTests.tearDown
    create_memory=UxBriefRetrievalFoundationTests.create_memory

    def test_ancestry_context_reads_do_not_materialize_unrelated_governance(self):
        from unittest.mock import patch
        from codex_web.organizational_memory import KnowledgeQuery
        source = self.create_memory("ux.ancestry-cost")
        brief = self.create_brief(source)
        state = self.governance.store.load()
        root = next(row for row in state.records if row.id == source.governance_record_id)
        ancestors = [root.model_copy(update={"id": f"ancestor-{i}", "object_id": f"ancestor-{i}",
            "source_record_ids": (f"ancestor-{i+1}",) if i < 11 else ()}) for i in range(12)]
        state.records = [row.model_copy(update={"source_record_ids": (ancestors[0].id,)})
            if row.id == root.id else row for row in state.records]
        state.records.extend(ancestors)
        state.records.extend(root.model_copy(update={"id": f"unrelated-{i}", "object_id": f"unrelated-{i}",
            "source_record_ids": ()}) for i in range(800))
        self.governance.store.store.put(self.governance.store.namespace, state.model_dump(mode="json"))
        query = KnowledgeQuery(knowledge_ids=(brief.id,), project_ids=("project-a",))
        original_get = self.governance.get_record
        with patch.object(self.governance, "get_record",
                side_effect=lambda identity, actor, **_: original_get(identity, actor)):
            expected = self.service.search(query, actor=self.actor)
        with patch.object(self.governance.store, "load", side_effect=AssertionError("whole governance catalog")):
            actual = self.service.search(query, actor=self.actor)
        self.assertEqual(actual.model_dump(exclude={"retrieval_id"}), expected.model_dump(exclude={"retrieval_id"}))
        self.assertEqual([row.knowledge_id for row in actual.items], [brief.id])
        # A current ancestor denial still excludes the brief on the next read.
        state.records = [row.model_copy(update={"deny_model_context": True})
            if row.id == ancestors[-1].id else row for row in state.records]
        self.governance.store.store.put(self.governance.store.namespace, state.model_dump(mode="json"))
        self.assertEqual(self.service.search(query, actor=self.actor).items, ())

    def test_ancestry_context_preserves_missing_foreign_and_cycle_decisions(self):
        from codex_web.data_governance import DataClassification
        from codex_web.services.ux_brief_sources import brief_context_allowed
        source = self.create_memory("ux.ancestry-negative")
        brief = self.create_brief(source)
        baseline = self.governance.store.load()
        root = next(row for row in baseline.records if row.id == source.governance_record_id)
        ancestor = root.model_copy(update={"id": "ancestor", "object_id": "ancestor", "source_record_ids": ()})
        for changed, expected in ((None, False),
                (ancestor.model_copy(update={"organization_id": "foreign"}), False),
                (ancestor.model_copy(update={"deny_model_context": True}), False),
                (ancestor.model_copy(update={"source_record_ids": (root.id,)}), True)):
            with self.subTest(ancestor=changed, expected=expected):
                rows = [row.model_copy(update={"source_record_ids": (ancestor.id,)})
                    if row.id == root.id else row for row in baseline.records]
                if changed is not None:
                    rows.append(changed)
                state = baseline.model_copy(update={"records": rows})
                self.governance.store.store.put(self.governance.store.namespace, state.model_dump(mode="json"))
                self.assertEqual(brief_context_allowed(self.service, brief, self.actor,
                    DataClassification.INTERNAL), expected)

    def create_brief(self, source=None, key="ux.curated", **changes):
        from codex_web.organizational_memory import KnowledgeCreate
        sections={"observed_needs":UxBriefSection(text="Show recovery status beside the failed task."),
            "approved_copy":UxBriefSection(text="Try again"),
            "uncertainties":UxBriefSection(text="A fixture observation has not been validated with real users.")}
        if source:
            pin=self.briefs.pin_source("knowledge",source.id,project_id="project-a",actor=self.actor)
            sections["observed_needs"]=UxBriefSection(text=sections["observed_needs"].text,sources=(pin,))
        return self.briefs.create(KnowledgeCreate(logical_key=key,object_type="ux_context_brief",project_id="project-a",
            title="Recovery UI brief",summary="Curated recovery constraints",content=UxBrief(sections=sections).model_dump_json(),
            provenance={"source_kind":"manual","source_ref":"fixture:curation"},**changes),project_id="project-a",actor=self.actor)

    def select(self, record, sections=("observed_needs","approved_copy")):
        return UxBriefSelection(knowledge_id=record.id,version=record.version,sha256=record.content_sha256,sections=sections)

    def test_curated_ui_task_packs_only_selected_sections_with_exact_sources(self):
        source=self.create_memory("ux.observation")
        brief=self.create_brief(source)
        result=self.briefs.preview(self.select(brief),project_id="project-a",actor=self.actor)
        self.assertIn("Show recovery status",result["context"])
        self.assertIn("Try again",result["context"])
        self.assertNotIn("has not been validated",result["context"])
        self.assertIn(source.id,result["context"])
        self.assertEqual(result["model_calls"],0)
        self.assertFalse(result["delivery_proven"])
        self.assertLessEqual(result["packed_tokens"],2000)
        audit=self.service.retrieval_run(result["retrieval_id"],actor=self.actor)
        self.assertEqual(audit.filters["context_selection"]["version"],brief.version)
        self.assertEqual(audit.filters["context_selection"]["sources"][0]["revision"],"1")
        self.assertNotIn("Try again",audit.model_dump_json())

    def test_brief_revision_keeps_historical_pin_and_never_delivers_superseded_brief(self):
        from codex_web.organizational_memory import KnowledgeRevise
        old=self.create_brief()
        updated=UxBrief(sections={"approved_copy":UxBriefSection(text="Retry selected task")})
        newer=self.briefs.revise(old.id,KnowledgeRevise(content=updated.model_dump_json(),reason="Update copy",
            provenance={"source_kind":"manual","source_ref":"fixture:revision"}),project_id="project-a",actor=self.actor)
        old_result=self.briefs.preview(self.select(old, ("approved_copy",)),project_id="project-a",actor=self.actor)
        self.assertEqual(old_result["context"],"")
        self.assertEqual(old_result["omitted"][0]["reason"],"brief_superseded")
        self.assertEqual(old_result["selection"]["knowledge_id"],old.id)
        new_result=self.briefs.preview(self.select(newer,("approved_copy",)),project_id="project-a",actor=self.actor)
        self.assertIn("Retry selected task",new_result["context"])
        with self.assertRaises(KnowledgeValidationError):
            forged=self.select(newer).model_copy(update={"sha256":old.content_sha256})
            self.briefs.preview(forged,project_id="project-a",actor=self.actor)

    def test_source_supersession_omits_instead_of_substituting_newer_findings(self):
        from codex_web.organizational_memory import KnowledgeRevise
        source=self.create_memory("ux.source")
        brief=self.create_brief(source)
        self.service.revise(source.id,KnowledgeRevise(content="A different result",reason="Superseded fixture",
            provenance={"source_kind":"manual","source_ref":"fixture:changed"}),actor=self.actor)
        result=self.briefs.preview(self.select(brief),project_id="project-a",actor=self.actor)
        self.assertEqual(result["context"],"")
        self.assertNotIn("A different result",str(result))
        self.assertTrue(all(row["reason"]=="brief_stale" for row in result["omitted"]))

    def test_source_governance_inherits_classification_and_model_context_denial(self):
        from codex_web.data_governance import DataClassification
        source=self.create_memory("ux.private",classification=DataClassification.CONFIDENTIAL,deny_model_context=True)
        brief=self.create_brief(source)
        self.assertEqual(brief.classification,DataClassification.CONFIDENTIAL)
        self.assertTrue(brief.deny_model_context)
        self.assertIn(source.governance_record_id,brief.provenance.source_governance_record_ids)
        result=self.briefs.preview(self.select(brief),project_id="project-a",actor=self.actor)
        self.assertEqual(result["context"],"")
        self.assertTrue(result["omitted"])

    def test_cross_project_and_tenant_selection_cannot_reuse_brief(self):
        brief=self.create_brief()
        with self.assertRaises(KnowledgeValidationError):
            self.briefs.preview(self.select(brief),project_id="project-b",actor=self.actor)
        with self.assertRaises(KeyError):
            self.briefs.preview(self.select(brief),project_id="project-a",actor=self.other)

    def test_canonical_source_deletion_removes_derived_brief_payload(self):
        from codex_web.data_governance import GovernanceActionRequest
        source=self.create_memory("ux.deleted-source")
        brief=self.create_brief(source)
        action=self.governance.request_action(GovernanceActionRequest(record_id=source.governance_record_id,
            action="delete",reason="Remove fixture source"),actor=self.actor)
        self.governance.execute_request(action.id,actor=self.actor)
        removed=self.service.store.get(brief.id)
        self.assertEqual(removed.content,"")
        self.assertEqual(removed.lifecycle.value,"deleted")
        with self.assertRaises(KnowledgeValidationError):
            self.briefs.preview(self.select(brief),project_id="project-a",actor=self.actor)

    def test_evidence_and_artifact_sources_pin_exact_canonical_record_hashes(self):
        from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate
        from codex_web.organizational_memory import KnowledgeCreate
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        artifacts=ArtifactEvidenceService(ArtifactEvidenceStore(self.store.store),governance=self.governance,
            organizational_memory=self.service)
        self.service.artifact_evidence=artifacts
        self.briefs.artifact_evidence=artifacts
        artifact=artifacts.create_artifact(ArtifactCreate(artifact_type="screenshot",name="Controlled UI fixture",project_id="project-a",
            revision="design-v1",digest={"algorithm":"sha256","value":"a"*64}),actor=self.actor)
        evidence=artifacts.create_evidence(EvidenceCreate(evidence_type="test_result",result="fail",project_id="project-a",
            artifact_ids=(artifact.id,),summary="A controlled negative result, not real user research"),actor=self.actor)
        pins=tuple(self.briefs.pin_source(kind,item.id,project_id="project-a",actor=self.actor)
            for kind,item in (("artifact",artifact),("evidence",evidence)))
        brief=self.briefs.create(KnowledgeCreate(logical_key="ux.evidence",object_type="ux_context_brief",project_id="project-a",
            title="Fixture critique",summary="Known limitations",content=UxBrief(sections={
                "uncertainties":UxBriefSection(text="Conflicting fixture expectations remain unresolved.",sources=pins)}).model_dump_json(),
            provenance={"source_kind":"manual","source_ref":"fixture:critique"}),project_id="project-a",actor=self.actor)
        selected=self.select(brief,("uncertainties",))
        result=self.briefs.preview(selected,project_id="project-a",actor=self.actor)
        self.assertIn("Conflicting fixture expectations",result["context"])
        self.assertEqual(result["selection"]["sources"][0]["revision"],"design-v1")
        self.assertEqual(result["selection"]["sources"][1]["sha256"],pins[1].sha256)
        self.assertEqual(result["model_calls"],0)
        # An outcome of fail is usable context, not proof of source invalidity.
        self.assertEqual(result["omitted"],[])

    def test_memory_http_routes_enforce_project_scope_and_human_step_up(self):
        import asyncio
        from types import SimpleNamespace
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from codex_web.api.organizational_memory import build_organizational_memory_router
        from codex_web.services.projects import ProjectNotFoundError
        from codex_web.identity import AuthenticationAssurance
        fixture=self
        class Projects:
            def get(self, project_id, scope):
                if project_id!="project-a" or scope.organization_id!="local":
                    raise ProjectNotFoundError(project_id)
                return SimpleNamespace(id=project_id)
        app=FastAPI()
        @app.middleware("http")
        async def identity(request, call_next):
            request.state.tenant_scope=SimpleNamespace(organization_id="local",workspace_id="default")
            request.state.identity_actor=fixture.actor.model_copy(update={"assurance":AuthenticationAssurance.PRIMARY}) if request.headers.get("x-low-assurance") else fixture.actor
            return await call_next(request)
        app.include_router(build_organizational_memory_router(self.service,brief_service=self.briefs,projects=Projects()))
        brief=self.create_brief()
        selection=self.select(brief,("approved_copy",)).model_dump(mode="json")
        async def exercise():
            async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
                listed=await client.get('/api/memory/projects/project-a/ux-briefs')
                self.assertEqual(listed.status_code,200)
                self.assertEqual(listed.json()["items"][0]["id"],brief.id)
                preview=await client.post('/api/memory/projects/project-a/ux-briefs/preview',json=selection)
                self.assertEqual(preview.status_code,200)
                self.assertIn("Try again",preview.json()["context"])
                foreign=await client.post('/api/memory/projects/project-b/ux-briefs/preview',json=selection)
                self.assertEqual(foreign.status_code,404)
                protected=await client.post('/api/memory/projects/project-a/ux-briefs/preview',json={**selection,"sandbox":"danger-full-access"})
                self.assertEqual(protected.status_code,422)
                revision={"content":UxBrief(sections={"approved_copy":UxBriefSection(text="Retry selected task")}).model_dump_json(),
                    "reason":"Update fixture copy","provenance":{"source_kind":"manual","source_ref":"fixture:revision"}}
                denied=await client.post(f'/api/memory/projects/project-a/ux-briefs/{brief.id}/revise',json=revision,headers={"x-low-assurance":"yes"})
                self.assertEqual(denied.status_code,403)
                revised=await client.post(f'/api/memory/projects/project-a/ux-briefs/{brief.id}/revise',json=revision)
                self.assertEqual(revised.status_code,200)
                self.assertEqual(revised.json()["item"]["version"],2)
        asyncio.run(exercise())

    def test_forged_projection_pin_never_runs_the_context_builder(self):
        from codex_web.organizational_memory import KnowledgeQuery, KnowledgeRetrievalBudget
        brief=self.create_brief()
        selected=self.select(brief,("approved_copy",)).model_copy(update={"sha256":"0"*64})
        calls=[]
        result=self.service.search(KnowledgeQuery(knowledge_ids=(brief.id,),project_ids=("project-a",),
            budget=KnowledgeRetrievalBudget(top_k=1,max_context_tokens=2000)),actor=self.actor,
            context_projection=lambda record:calls.append(record.id) or "Unverified context",context_selection=selected)
        self.assertEqual(calls,[])
        self.assertEqual(result.items,())
        self.assertTrue(any(item.reason=="context_selection_revision_mismatch" for item in result.denied))

    def test_generic_memory_write_applies_brief_source_governance(self):
        from codex_web.organizational_memory import KnowledgeCreate, KnowledgeQuery
        source=self.create_memory("ux.generic-private",deny_model_context=True)
        pin=self.briefs.pin_source("knowledge",source.id,project_id="project-a",actor=self.actor)
        brief=self.service.create(KnowledgeCreate(logical_key="ux.generic-entry",object_type="ux_context_brief",project_id="project-a",
            title="Generic API brief",summary="Governance must apply",content=UxBrief(sections={
                "observed_needs":UxBriefSection(text="Sensitive derived fixture",sources=(pin,))}).model_dump_json(),
            provenance={"source_kind":"manual","source_ref":"fixture:generic"}),actor=self.actor)
        self.assertTrue(brief.deny_model_context)
        self.assertIn(source.governance_record_id,brief.provenance.source_governance_record_ids)
        self.assertEqual(self.service.search(KnowledgeQuery(knowledge_ids=(brief.id,)),actor=self.actor).items,())

    def test_generic_memory_retrieval_rechecks_artifact_record_snapshot(self):
        from codex_web.artifact_evidence import ArtifactCreate
        from codex_web.organizational_memory import KnowledgeCreate, KnowledgeQuery
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        artifacts=ArtifactEvidenceService(ArtifactEvidenceStore(self.store.store),governance=self.governance,organizational_memory=self.service)
        self.service.artifact_evidence=artifacts
        artifact=artifacts.create_artifact(ArtifactCreate(artifact_type="screenshot",name="UI fixture",project_id="project-a",revision="design-v1"),actor=self.actor)
        pin=self.briefs.pin_source("artifact",artifact.id,project_id="project-a",actor=self.actor)
        brief=self.service.create(KnowledgeCreate(logical_key="ux.generic-artifact",object_type="ux_context_brief",project_id="project-a",
            title="Design brief",summary="Snapshot",content=UxBrief(sections={"design_constraints":UxBriefSection(text="Preserve keyboard recovery",sources=(pin,))}).model_dump_json(),
            provenance={"source_kind":"manual","source_ref":"fixture:design"}),actor=self.actor)
        query=KnowledgeQuery(knowledge_ids=(brief.id,))
        self.assertEqual(len(self.service.search(query,actor=self.actor).items),1)
        def mutate(state):
            state.artifacts[0].metadata["fixture_change"]="changed canonical record"
            return state
        artifacts.store.update(mutate)
        self.assertEqual(self.service.freshness(self.service.get(brief.id,actor=self.actor)).value,"stale")
        self.assertEqual(self.service.search(query,actor=self.actor).items,())

    def test_memory_v11_migration_preserves_old_records_and_old_reader_refuses_v12(self):
        from codex_web.compatibility import ContractSpec, ContractCompatibilityError
        source=self.create_memory("ux.legacy-state")
        payload=self.store.store.get(self.store.namespace)
        payload["schema_version"]="1.1"
        before=payload["records"]
        self.store.store.put(self.store.namespace,payload)
        state=self.store.load()
        self.assertEqual(state.schema_version,"1.3")
        self.assertEqual([item.model_dump(mode="json") for item in state.records],before)
        self.assertEqual(self.store.get(source.id).content_sha256,source.content_sha256)
        with self.assertRaises(ContractCompatibilityError):
            ContractSpec("organizational-memory-state","1.1",("1.0","1.1")).require(state.schema_version)

    def test_context_budget_omits_whole_sections_and_retains_complete_references(self):
        from codex_web.organizational_memory import KnowledgeRevise
        brief=self.create_brief()
        body=UxBrief(sections={"observed_needs":UxBriefSection(text="A"*2000),
            "approved_copy":UxBriefSection(text="Try again")})
        brief=self.briefs.revise(brief.id,KnowledgeRevise(content=body.model_dump_json(),reason="Budget fixture",
            provenance={"source_kind":"manual","source_ref":"fixture:budget"}),project_id="project-a",actor=self.actor)
        selected=self.select(brief).model_copy(update={"max_context_tokens":128})
        result=self.briefs.preview(selected,project_id="project-a",actor=self.actor)
        self.assertIn("Try again",result["context"])
        self.assertNotIn("A"*100,result["context"])
        self.assertNotIn("truncated",result["context"])
        self.assertTrue(any(row["section"]=="observed_needs" and row["reason"]=="section_exceeds_remaining_context_budget" for row in result["omitted"]))
        self.assertLessEqual(result["packed_tokens"],128)

    def turn_context(self):
        from types import SimpleNamespace
        from codex_web.services.ux_brief_turns import UxBriefTurnService
        identity = SimpleNamespace(actor_for_identity=lambda identity_id, scope: self.actor)
        project = SimpleNamespace(id="project-a", organization_id="local", workspace_id="default")
        return UxBriefTurnService(self.briefs, identity), project, identity

    def test_retained_selection_revalidates_sources_and_preserves_exact_restart_pins(self):
        from fastapi import HTTPException
        from codex_web.organizational_memory import KnowledgeRevise
        from codex_web.ux_briefs import UxBriefTurnBinding
        service, project, identity = self.turn_context()
        source = self.create_memory("ux.turn.source")
        brief = self.create_brief(source)
        binding = service.bind(self.select(brief), project=project, actor=self.actor)
        restored = UxBriefTurnBinding.model_validate_json(binding.model_dump_json())
        preview, actor = service.prepare(restored, project=project)
        self.assertIn("Try again", preview["context"])
        self.assertFalse(preview["delivery_proven"])
        self.assertNotIn("Try again", binding.model_dump_json())
        self.service.revise(source.id, KnowledgeRevise(content="Changed source", reason="New findings",
            provenance={"source_kind":"manual", "source_ref":"fixture:changed"}), actor=self.actor)
        with self.assertRaises(HTTPException) as denied:
            service.prepare(restored, project=project)
        self.assertEqual(denied.exception.detail["code"], "memory_context_denied")

    def test_delivery_receipt_requires_native_acceptance_and_contains_only_metadata(self):
        service, project, identity = self.turn_context()
        brief = self.create_brief()
        binding = service.bind(self.select(brief), project=project, actor=self.actor)
        preview, actor = service.prepare(binding, project=project)
        args = dict(thread_id="thread-fixture", execution_id="execution-fixture",
            provider_id="fixture", runtime_id="fixture-runtime", message="task plus selected context")
        service.delivered(preview, actor, native_turn_id=None, **args)
        run = self.service.retrieval_run(preview["retrieval_id"], actor=actor)
        self.assertNotIn("native_delivery", run.filters)
        service.delivered(preview, actor, native_turn_id="native-fixture", **args)
        run = self.service.retrieval_run(preview["retrieval_id"], actor=actor)
        self.assertTrue(run.filters["native_delivery"]["delivery_proven"])
        self.assertEqual(run.filters["native_delivery"]["native_turn_id"], "native-fixture")
        self.assertNotIn("Try again", run.model_dump_json())
        self.assertNotIn("task plus selected context", run.model_dump_json())

    def test_delivery_rechecks_current_actor_and_project_scope(self):
        from fastapi import HTTPException
        service, project, identity = self.turn_context()
        binding = service.bind(self.select(self.create_brief()), project=project, actor=self.actor)
        identity.actor_for_identity = lambda identity_id, scope: self.other
        with self.assertRaises(HTTPException):
            service.prepare(binding, project=project)
        project.id = "project-b"
        with self.assertRaises(HTTPException):
            service.prepare(binding, project=project)

    def test_queue_context_binding_is_optional_and_does_not_change_legacy_snapshots(self):
        from codex_web.models import QueuedTurn, TurnCreate
        payload = TurnCreate(message="Existing free text")
        self.assertNotIn("memory_context_selection", payload.model_dump())
        queued = QueuedTurn(id="q", thread_id="t", project_id="project-a", message="task", created_at=1)
        self.assertNotIn("memory_context_binding", queued.model_dump())
        service, project, identity = self.turn_context()
        binding = service.bind(self.select(self.create_brief()), project=project, actor=self.actor)
        queued.memory_context_binding = binding
        self.assertEqual(QueuedTurn.model_validate_json(queued.model_dump_json()).memory_context_binding, binding)

    def test_delayed_service_context_rechecks_token_revocation_expiry_and_read_grant(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        service, project, identity = self.turn_context()
        service_actor = self.actor.model_copy(update={"principal_kind":"service", "service_token_id":"token-fixture",
            "service_scopes":("memory:read",)})
        brief = self.create_brief()
        binding = service.bind(self.select(brief), project=project, actor=service_actor)
        identity.actor_for_identity = lambda identity_id, scope: service_actor.model_copy(update={"service_scopes":()})
        token = SimpleNamespace(id="token-fixture", service_identity_id=service_actor.identity_id,
            organization_id="local", workspace_id="default", revoked_at=None, expires_at=None, scopes=["memory:read"])
        identity.store = SimpleNamespace(load=lambda: SimpleNamespace(service_tokens=[token]))
        preview, current = service.prepare(binding, project=project)
        self.assertIn("Try again", preview["context"])
        self.assertEqual(current.service_scopes, ("memory:read",))
        for field, value in (("revoked_at",self.now), ("expires_at",self.now-1), ("scopes",[])):
            original = getattr(token,field)
            setattr(token,field,value)
            with self.subTest(field=field), self.assertRaises(HTTPException):
                service.prepare(binding, project=project)
            setattr(token,field,original)
