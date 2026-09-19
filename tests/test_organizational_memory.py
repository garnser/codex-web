from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.data_governance import (
    DataClassification,
    GovernanceAction,
    GovernanceActionRequest,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.organizational_memory import (
    KnowledgeCreate,
    KnowledgeFreshness,
    KnowledgeIngestBatch,
    KnowledgeLifecycle,
    KnowledgeObjectType,
    KnowledgeProcedurePromotion,
    KnowledgeProvenance,
    KnowledgeQuery,
    KnowledgeRelationshipCreate,
    KnowledgeRelationshipType,
    KnowledgeRetrievalBudget,
    KnowledgeRevise,
    KnowledgeSourceKind,
)
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.retrieval import (
    DeterministicLocalEmbeddingProvider,
    LocalLexicalRetrievalBackend,
    LocalVectorRetrievalBackend,
    RetrievalSearchRequest,
)
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class OrganizationalMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = 2_000_000_000.0
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        authority = install_authority_roles(registry)
        governance = DataGovernanceService(DataGovernanceStore(sqlite))
        self.governance = governance
        self.store = OrganizationalMemoryStore(sqlite)
        self.service = OrganizationalMemoryService(
            self.store,
            governance,
            authority,
            clock=lambda: self.now,
        )
        self.actor = AuthenticationActor(
            identity_id="local-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.other = AuthenticationActor(
            identity_id="local-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="other",
            workspace_id="other",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _create(
        self,
        logical_key: str,
        *,
        title: str,
        summary: str,
        content: str,
        object_type: KnowledgeObjectType = KnowledgeObjectType.OTHER,
        project_id: str | None = None,
        tags: tuple[str, ...] = (),
        classification: DataClassification = DataClassification.INTERNAL,
        deny_model_context: bool = False,
        required_role_ids: tuple[str, ...] = (),
        review_after: float | None = None,
        valid_until: float | None = None,
        retention_expires_at: float | None = None,
        retention_action: GovernanceAction = GovernanceAction.REDACT,
        relationships: tuple[KnowledgeRelationshipCreate, ...] = (),
        source_governance_record_ids: tuple[str, ...] = (),
    ):
        return self.service.create(
            KnowledgeCreate(
                logical_key=logical_key,
                object_type=object_type,
                title=title,
                summary=summary,
                content=content,
                project_id=project_id,
                tags=tags,
                relationships=relationships,
                provenance=KnowledgeProvenance(
                    source_kind=KnowledgeSourceKind.MANUAL,
                    source_ref=f"test://{logical_key}",
                    authored_by=self.actor.identity_id,
                    authored_at=self.now - 100,
                    observed_at=self.now - 10,
                    source_governance_record_ids=source_governance_record_ids,
                ),
                classification=classification,
                deny_model_context=deny_model_context,
                required_role_ids=required_role_ids,
                review_after=review_after,
                valid_until=valid_until,
                retention_expires_at=retention_expires_at,
                retention_action=retention_action,
            ),
            actor=self.actor,
        )

    def test_hybrid_semantic_and_structured_retrieval_is_bounded_and_audited(self) -> None:
        database = self._create(
            "adr/database/postgres",
            title="PostgreSQL persistence decision",
            summary="Use PostgreSQL as the primary application database.",
            content=(
                "The architecture decision standardizes durable relational "
                "persistence on PostgreSQL for the control plane."
            ),
            object_type=KnowledgeObjectType.ARCHITECTURE_DECISION,
            project_id="project-a",
            tags=("architecture", "database"),
        )
        self._create(
            "product/onboarding",
            title="Onboarding product note",
            summary="Improve first-run activation.",
            content="Customer onboarding experiments target activation.",
            object_type=KnowledgeObjectType.PRODUCT,
            project_id="project-a",
            tags=("product",),
        )

        result = self.service.search(
            KnowledgeQuery(
                text="db storage architecture",
                object_types=(KnowledgeObjectType.ARCHITECTURE_DECISION,),
                project_ids=("project-a",),
                tags=("architecture",),
                budget=KnowledgeRetrievalBudget(
                    top_k=3,
                    candidate_limit=10,
                    max_context_tokens=128,
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].knowledge_id, database.id)
        self.assertGreater(result.items[0].semantic_score, 0)
        self.assertLessEqual(result.packed_tokens, 128)
        self.assertLessEqual(result.items[0].estimated_tokens, 128)
        self.assertIn("semantic:", " ".join(result.items[0].reasons))

        run = self.service.retrieval_run(result.retrieval_id, actor=self.actor)
        self.assertEqual(run.selected_knowledge_ids, (database.id,))
        self.assertIn(database.id, run.selected_scores)
        self.assertEqual(
            run.selected_freshness[database.id],
            KnowledgeFreshness.FRESH,
        )
        self.assertEqual(run.max_context_tokens, 128)
        self.assertNotIn("db storage architecture", str(run.filters))
        self.assertEqual(len(run.query_sha256), 64)

    def test_token_budget_truncates_large_content_without_exceeding_budget(self) -> None:
        record = self._create(
            "procedure/large-runbook",
            title="Large recovery runbook",
            summary="A deliberately large procedure.",
            content=("recovery database rollback validation " * 2000),
            object_type=KnowledgeObjectType.PROCEDURE,
            tags=("recovery",),
        )

        result = self.service.search(
            KnowledgeQuery(
                text="database recovery rollback",
                logical_keys=(record.logical_key,),
                budget=KnowledgeRetrievalBudget(
                    top_k=1,
                    candidate_limit=5,
                    max_context_tokens=128,
                ),
            ),
            actor=self.actor,
        )

        self.assertEqual(len(result.items), 1)
        self.assertLessEqual(result.packed_tokens, 128)
        self.assertLessEqual(result.items[0].estimated_tokens, 128)
        self.assertIn("truncated by memory retrieval budget", result.items[0].context_excerpt)

    def test_governance_classification_and_deny_model_context_fail_closed(self) -> None:
        confidential = self._create(
            "policy/confidential",
            title="Confidential policy",
            summary="Tenant-internal policy.",
            content="confidential operating policy",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.CONFIDENTIAL,
        )
        restricted = self._create(
            "policy/restricted",
            title="Restricted policy",
            summary="Restricted policy.",
            content="restricted operating policy",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.RESTRICTED,
        )
        secret = self._create(
            "policy/secret",
            title="Secret policy",
            summary="Secret policy.",
            content="secret operating policy",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.SECRET,
        )
        denied = self._create(
            "policy/no-model",
            title="No model context",
            summary="Must never enter model context.",
            content="model restricted internal policy",
            object_type=KnowledgeObjectType.POLICY,
            deny_model_context=True,
        )

        default_result = self.service.search(
            KnowledgeQuery(
                text="operating policy",
                include_stale=True,
                budget=KnowledgeRetrievalBudget(top_k=10),
            ),
            actor=self.actor,
        )
        ids = {item.knowledge_id for item in default_result.items}
        self.assertIn(confidential.id, ids)
        self.assertNotIn(restricted.id, ids)
        self.assertNotIn(secret.id, ids)
        self.assertNotIn(denied.id, ids)
        reasons = {
            item.knowledge_id: item.reason
            for item in default_result.denied
        }
        self.assertEqual(
            reasons[restricted.id],
            "governance:classification_exceeds_context_limit",
        )
        self.assertEqual(
            reasons[secret.id],
            "governance:secret_data_never_enters_model_context",
        )
        self.assertEqual(
            reasons[denied.id],
            "governance:record_denies_model_context",
        )

        restricted_result = self.service.search(
            KnowledgeQuery(
                text="operating policy",
                max_classification=DataClassification.RESTRICTED,
                budget=KnowledgeRetrievalBudget(top_k=10),
            ),
            actor=self.actor,
        )
        restricted_ids = {
            item.knowledge_id for item in restricted_result.items
        }
        self.assertIn(restricted.id, restricted_ids)
        self.assertNotIn(secret.id, restricted_ids)

    def test_required_operational_role_and_tenant_scope_are_enforced(self) -> None:
        restricted_role = self._create(
            "policy/security-only",
            title="Security-only policy",
            summary="Only security role holders may retrieve it.",
            content="security handling requirements",
            object_type=KnowledgeObjectType.POLICY,
            required_role_ids=("security-specialist",),
        )

        result = self.service.search(
            KnowledgeQuery(
                text="security requirements",
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        self.assertEqual(result.items, ())
        self.assertEqual(
            {item.knowledge_id: item.reason for item in result.denied}[
                restricted_role.id
            ],
            "required_operational_role_missing",
        )

        with self.assertRaises(Exception):
            self.service.get(restricted_role.id, actor=self.other)

        foreign = self.service.search(
            KnowledgeQuery(
                text="security requirements",
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.other,
        )
        self.assertEqual(foreign.items, ())
        self.assertEqual(foreign.candidate_count, 0)

    def test_revision_makes_supersession_explicit_and_current_wins_default_retrieval(self) -> None:
        original = self._create(
            "policy/deployment",
            title="Deployment policy v1",
            summary="Old deployment policy.",
            content="Deploy directly after tests.",
            object_type=KnowledgeObjectType.POLICY,
            tags=("deployment",),
        )
        revised = self.service.revise(
            original.id,
            KnowledgeRevise(
                title="Deployment policy v2",
                summary="Current deployment policy.",
                content="Promote immutable artifacts through staged environments.",
                provenance=KnowledgeProvenance(
                    source_kind=KnowledgeSourceKind.MANUAL,
                    source_ref="test://policy/deployment/v2",
                    authored_by=self.actor.identity_id,
                    observed_at=self.now,
                ),
                reason="replace unsafe direct deployment guidance",
            ),
            actor=self.actor,
        )

        versions = self.service.versions(revised.id, actor=self.actor)
        self.assertEqual([item.version for item in versions], [1, 2])
        self.assertEqual(versions[0].lifecycle, KnowledgeLifecycle.SUPERSEDED)
        self.assertEqual(versions[0].superseded_by_id, revised.id)
        self.assertEqual(revised.previous_version_id, original.id)

        current = self.service.search(
            KnowledgeQuery(
                text="deployment staged immutable artifacts",
                logical_keys=(original.logical_key,),
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        self.assertEqual(
            {item.knowledge_id for item in current.items},
            {revised.id},
        )

        historical = self.service.search(
            KnowledgeQuery(
                text="deployment policy",
                logical_keys=(original.logical_key,),
                include_superseded=True,
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        by_id = {item.knowledge_id: item for item in historical.items}
        self.assertEqual(
            by_id[original.id].freshness,
            KnowledgeFreshness.SUPERSEDED,
        )
        self.assertEqual(
            by_id[revised.id].freshness,
            KnowledgeFreshness.FRESH,
        )
        relations = self.service.relationships(revised.id, actor=self.actor)
        self.assertTrue(
            any(
                item.relationship_type == KnowledgeRelationshipType.SUPERSEDES
                and item.target_knowledge_id == original.id
                for item in relations
            )
        )

    def test_stale_and_expired_memory_are_excluded_unless_explicitly_requested(self) -> None:
        stale = self._create(
            "architecture/stale",
            title="Stale architecture note",
            summary="Needs review.",
            content="legacy queue architecture",
            object_type=KnowledgeObjectType.ARCHITECTURE_DECISION,
            review_after=self.now - 1,
        )
        expired = self._create(
            "architecture/expired",
            title="Expired architecture note",
            summary="No longer temporally valid.",
            content="expired queue architecture",
            object_type=KnowledgeObjectType.ARCHITECTURE_DECISION,
            valid_until=self.now - 1,
        )

        default_result = self.service.search(
            KnowledgeQuery(
                text="queue architecture",
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        self.assertEqual(default_result.items, ())

        explicit = self.service.search(
            KnowledgeQuery(
                text="queue architecture",
                include_stale=True,
                budget=KnowledgeRetrievalBudget(top_k=5),
            ),
            actor=self.actor,
        )
        freshness = {
            item.knowledge_id: item.freshness
            for item in explicit.items
        }
        self.assertEqual(freshness[stale.id], KnowledgeFreshness.STALE)
        self.assertEqual(freshness[expired.id], KnowledgeFreshness.EXPIRED)

    def test_governed_deletion_cascades_to_derived_memory_and_removes_index_content(self) -> None:
        source = self._create(
            "policy/source",
            title="Source policy",
            summary="Source of derived policy.",
            content="source policy content",
            object_type=KnowledgeObjectType.POLICY,
            retention_action=GovernanceAction.DELETE,
        )
        derived = self.service.revise(
            source.id,
            KnowledgeRevise(
                title="Source policy revision",
                summary="Derived current policy.",
                content="derived current policy content",
                provenance=KnowledgeProvenance(
                    source_kind=KnowledgeSourceKind.MANUAL,
                    source_ref="test://policy/source/v2",
                    authored_by=self.actor.identity_id,
                    observed_at=self.now,
                ),
                reason="derive a new current version",
            ),
            actor=self.actor,
        )

        request = self.governance.request_action(
            GovernanceActionRequest(
                record_id=source.governance_record_id,
                action=GovernanceAction.DELETE,
                reason="test governed deletion",
            ),
            actor=self.actor,
        )
        completed = self.governance.execute_request(
            request.id,
            actor=self.actor,
        )
        self.assertEqual(completed.status.value, "completed")

        source_after = self.service.get(
            source.id,
            actor=self.actor,
            include_inactive=True,
        )
        derived_after = self.service.get(
            derived.id,
            actor=self.actor,
            include_inactive=True,
        )
        self.assertEqual(source_after.lifecycle, KnowledgeLifecycle.DELETED)
        self.assertEqual(derived_after.lifecycle, KnowledgeLifecycle.DELETED)
        self.assertEqual(source_after.content, "")
        self.assertEqual(derived_after.content, "")
        state = self.store.load()
        self.assertNotIn(
            source.id,
            {item.knowledge_id for item in state.embeddings},
        )
        self.assertNotIn(
            derived.id,
            {item.knowledge_id for item in state.embeddings},
        )

        result = self.service.search(
            KnowledgeQuery(
                text="source derived policy",
                include_superseded=True,
                include_invalid=True,
                include_stale=True,
            ),
            actor=self.actor,
        )
        self.assertEqual(result.items, ())


    def test_retrieval_backend_swap_preserves_canonical_memory_and_records_index_provenance(self) -> None:
        record = self._create(
            "architecture/retrieval-backend",
            title="Retrieval backend decision",
            summary="Canonical memory is independent from the derived index.",
            content="Use a pluggable retrieval backend for memory search.",
            object_type=KnowledgeObjectType.ARCHITECTURE_DECISION,
            tags=("architecture", "retrieval"),
        )

        vector_result = self.service.search(
            KnowledgeQuery(
                text="pluggable retrieval backend",
                budget=KnowledgeRetrievalBudget(top_k=3),
            ),
            actor=self.actor,
        )
        self.assertEqual(vector_result.items[0].knowledge_id, record.id)
        self.assertEqual(vector_result.retrieval_backend_id, "local-vector")
        self.assertEqual(vector_result.embedding_provider_id, "local")
        self.assertEqual(vector_result.embedding_model_id, "concept-hash")
        self.assertEqual(vector_result.embedding_model_revision, "1")
        vector_run = self.service.retrieval_run(
            vector_result.retrieval_id,
            actor=self.actor,
        )
        self.assertEqual(
            vector_run.retrieval_index_revision,
            vector_result.retrieval_index_revision,
        )

        self.service.retrieval_backend = LocalLexicalRetrievalBackend(
            backend_id="test-lexical"
        )
        rebuilt = self.service.rebuild_retrieval_index(actor=self.actor)
        self.assertEqual(rebuilt.backend_id, "test-lexical")
        self.assertEqual(rebuilt.document_count, 1)

        lexical_result = self.service.search(
            KnowledgeQuery(
                text="retrieval backend",
                logical_keys=(record.logical_key,),
                budget=KnowledgeRetrievalBudget(top_k=3),
            ),
            actor=self.actor,
        )
        self.assertEqual(lexical_result.items[0].knowledge_id, record.id)
        self.assertEqual(lexical_result.retrieval_backend_id, "test-lexical")
        self.assertIsNone(lexical_result.embedding_provider_id)
        current = self.service.get(record.id, actor=self.actor)
        self.assertEqual(current.id, record.id)
        self.assertEqual(current.content_sha256, record.content_sha256)

    def test_external_embedding_index_requires_governed_export_and_excludes_secret_memory(self) -> None:
        internal = self._create(
            "policy/external-index-internal",
            title="Internal indexing policy",
            summary="Internal memory may be exported when governance authorizes it.",
            content="governed external embedding test internal",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.INTERNAL,
        )
        secret = self._create(
            "policy/external-index-secret",
            title="Secret indexing policy",
            summary="Secret memory must not leave the governed boundary.",
            content="governed external embedding test secret",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.SECRET,
        )

        external = LocalVectorRetrievalBackend(
            DeterministicLocalEmbeddingProvider(
                provider_id="external-provider",
                model_id="embedding-model",
                model_revision="2026-09",
                local=False,
                residency_tags=("eu",),
            ),
            backend_id="external-vector",
        )
        self.service.retrieval_backend = external
        self.service.embedding_identity_validator = (
            lambda identity, actor: None
        )
        self.service._retrieval_index_dirty = True

        with self.assertRaisesRegex(
            Exception,
            "requires an authenticated administrator",
        ):
            self.service.rebuild_retrieval_index()

        status = self.service.rebuild_retrieval_index(actor=self.actor)
        self.assertEqual(status.backend_id, "external-vector")
        self.assertEqual(status.document_count, 1)
        self.assertFalse(status.embedding_identity.local)
        self.assertEqual(
            status.embedding_identity.provider_id,
            "external-provider",
        )

        internal_hits = external.search(
            RetrievalSearchRequest(
                text="governed external embedding test",
                organization_id=self.actor.organization_id,
                workspace_id=self.actor.workspace_id,
                allowed_knowledge_ids=(internal.id, secret.id),
                limit=10,
            )
        )
        self.assertEqual(
            {item.knowledge_id for item in internal_hits},
            {internal.id},
        )

        later_secret = self._create(
            "policy/external-index-secret-later",
            title="Later secret indexing policy",
            summary="Secret memory remains outside external embeddings.",
            content="later external secret content",
            object_type=KnowledgeObjectType.POLICY,
            classification=DataClassification.SECRET,
        )
        self.assertNotIn(
            later_secret.id,
            {
                item.knowledge_id
                for item in external.search(
                    RetrievalSearchRequest(
                        text="later external secret content",
                        organization_id=self.actor.organization_id,
                        workspace_id=self.actor.workspace_id,
                        allowed_knowledge_ids=(later_secret.id,),
                        limit=10,
                    )
                )
            },
        )


    def test_ingestion_is_idempotent_and_revises_changed_authorized_source(self) -> None:
        first = KnowledgeCreate(
            logical_key="github/issue/118",
            object_type=KnowledgeObjectType.OTHER,
            title="Memory ingestion issue",
            summary="Track governed memory ingestion.",
            content="Initial source snapshot.",
            project_id="project-a",
            tags=("github", "issue"),
            provenance=KnowledgeProvenance(
                source_kind=KnowledgeSourceKind.ISSUE,
                source_ref="github:garnser/codex-web#118",
                source_url="https://github.com/garnser/codex-web/issues/118",
                source_revision="updated-1",
                authored_by="maintainer-a",
                observed_at=self.now,
            ),
        )
        created = self.service.ingest(
            KnowledgeIngestBatch(items=(first,)),
            actor=self.actor,
        )
        self.assertEqual(created[0][0], "created")
        self.assertEqual(created[0][1].version, 1)

        unchanged = self.service.ingest(
            KnowledgeIngestBatch(items=(first,)),
            actor=self.actor,
        )
        self.assertEqual(unchanged[0][0], "unchanged")
        self.assertEqual(unchanged[0][1].id, created[0][1].id)

        changed = first.model_copy(
            update={
                "content": "Revised source snapshot with retrieval inspection.",
                "provenance": first.provenance.model_copy(
                    update={"source_revision": "updated-2"}
                ),
            }
        )
        revised = self.service.ingest(
            KnowledgeIngestBatch(
                items=(changed,),
                reason="Git issue source revision changed",
            ),
            actor=self.actor,
        )
        self.assertEqual(revised[0][0], "revised")
        self.assertEqual(revised[0][1].version, 2)
        self.assertEqual(
            revised[0][1].provenance.source_revision,
            "updated-2",
        )
        versions = self.service.versions(
            revised[0][1].id,
            actor=self.actor,
        )
        self.assertEqual(
            [item.lifecycle for item in versions],
            [KnowledgeLifecycle.SUPERSEDED, KnowledgeLifecycle.CURRENT],
        )

    def test_verified_recurring_solution_promotes_to_governed_procedure(self) -> None:
        incident = self._create(
            "incident/database-lock",
            title="Database lock incident",
            summary="A recurring lock caused failed deploys.",
            content="Release the stale lease, verify ownership, then retry.",
            object_type=KnowledgeObjectType.INCIDENT,
            project_id="project-a",
            tags=("database", "lease"),
            classification=DataClassification.CONFIDENTIAL,
        )
        postmortem = self._create(
            "postmortem/database-lock",
            title="Database lock postmortem",
            summary="Validated recovery sequence.",
            content="The recovery sequence succeeded twice without data loss.",
            object_type=KnowledgeObjectType.POSTMORTEM,
            project_id="project-a",
            tags=("database", "recovery"),
        )

        procedure = self.service.promote_procedure(
            KnowledgeProcedurePromotion(
                source_knowledge_ids=(incident.id, postmortem.id),
                evidence_ids=("evidence-run-1", "evidence-run-2"),
                logical_key="procedure/database-lock-recovery",
                title="Recover stale database execution lease",
                summary="Deterministic recovery procedure for the known lock pattern.",
                content=(
                    "Detect the verified stale-lock signature, release the expired "
                    "lease, verify ownership, then retry once."
                ),
                project_id="project-a",
                tags=("database", "recovery"),
            ),
            actor=self.actor,
        )

        self.assertEqual(procedure.object_type, KnowledgeObjectType.PROCEDURE)
        self.assertEqual(procedure.classification, DataClassification.CONFIDENTIAL)
        self.assertIn("known-pattern", procedure.tags)
        self.assertEqual(
            procedure.provenance.evidence_ids,
            ("evidence-run-1", "evidence-run-2"),
        )
        self.assertEqual(
            set(procedure.provenance.source_governance_record_ids),
            {
                incident.governance_record_id,
                postmortem.governance_record_id,
            },
        )
        relationships = self.service.relationships(
            procedure.id,
            actor=self.actor,
        )
        self.assertEqual(
            {
                item.target_knowledge_id
                for item in relationships
                if item.relationship_type == KnowledgeRelationshipType.DERIVED_FROM
            },
            {incident.id, postmortem.id},
        )

    def test_project_retrieval_can_include_company_policy_and_retrieval_runs_are_inspectable(self) -> None:
        company = self._create(
            "policy/company-database",
            title="Company database policy",
            summary="Use the approved relational database baseline.",
            content="All transactional services use PostgreSQL unless a Decision supersedes this policy.",
            object_type=KnowledgeObjectType.POLICY,
            tags=("database",),
        )
        project = self._create(
            "architecture/project-database",
            title="Project database architecture",
            summary="Project A follows the company database policy.",
            content="Project A will provision PostgreSQL.",
            object_type=KnowledgeObjectType.ARCHITECTURE_DECISION,
            project_id="project-a",
            tags=("database",),
        )

        result = self.service.search(
            KnowledgeQuery(
                text="PostgreSQL database policy",
                project_ids=("project-a",),
                include_company_scope=True,
                budget=KnowledgeRetrievalBudget(top_k=8, max_context_tokens=512),
            ),
            actor=self.actor,
        )
        self.assertEqual(
            {item.knowledge_id for item in result.items},
            {company.id, project.id},
        )
        runs = self.service.retrieval_runs(actor=self.actor)
        self.assertEqual(runs[0].id, result.retrieval_id)
        self.assertEqual(runs[0].selected_knowledge_ids, tuple(item.knowledge_id for item in result.items))
        self.assertLessEqual(runs[0].packed_tokens, 512)



if __name__ == "__main__":
    unittest.main()
