from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.data_governance import DataClassification
from codex_web.executive import ExecutiveChatRequest
from codex_web.executive_integration import MultiProviderExecutiveService
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.organizational_memory import (
    KnowledgeCreate,
    KnowledgeObjectType,
    KnowledgeProvenance,
    KnowledgeSourceKind,
)
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.organizational_memory import OrganizationalMemoryService
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.organizational_memory import OrganizationalMemoryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ExecutiveOrganizationalMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_executive_reasoning_uses_canonical_bounded_memory_and_returns_retrieval_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            sqlite = SQLiteStateStore(data_dir / "state.db")
            registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
            authority = install_authority_roles(registry)
            governance = DataGovernanceService(DataGovernanceStore(sqlite))
            memory = OrganizationalMemoryService(
                OrganizationalMemoryStore(sqlite),
                governance,
                authority,
            )
            host = SimpleNamespace(
                DATA_DIR=data_dir,
                app=SimpleNamespace(state=SimpleNamespace(sqlite_state_store=sqlite)),
            )
            actor = AuthenticationActor(
                identity_id="local-admin",
                principal_kind=PrincipalKind.HUMAN,
                organization_id="local",
                workspace_id="default",
                roles=(MembershipRole.ADMIN,),
                assurance=AuthenticationAssurance.LOCAL_TRUSTED,
            )
            policy = memory.create(
                KnowledgeCreate(
                    logical_key="policy/database",
                    object_type=KnowledgeObjectType.POLICY,
                    title="Database standard",
                    summary="Use PostgreSQL for transactional services.",
                    content=(
                        "PostgreSQL is the approved company standard for new "
                        "transactional services."
                    ),
                    tags=("database", "postgresql"),
                    provenance=KnowledgeProvenance(
                        source_kind=KnowledgeSourceKind.REPOSITORY,
                        source_ref="repo://architecture/ADR-12",
                        source_revision="abc123",
                        authored_by="architecture-team",
                    ),
                    classification=DataClassification.INTERNAL,
                ),
                actor=actor,
            )

            service = MultiProviderExecutiveService(
                host,
                organizational_memory=memory,
            )
            service.provider = "openai"
            captured: list[dict] = []

            class Responses:
                async def create(self, **kwargs):
                    captured.append(kwargs)
                    return SimpleNamespace(
                        output_text=(
                            f"Use the existing PostgreSQL standard "
                            f"[memory:{policy.id}@v1]."
                        )
                    )

            service._openai_client = SimpleNamespace(responses=Responses())
            result = await service.chat(
                ExecutiveChatRequest(
                    session_id="canonical-memory-test",
                    message="Which database should the new transactional service use?",
                    project_id="project-a",
                ),
                actor=actor,
            )

            self.assertEqual(len(captured), 1)
            instructions = captured[0]["instructions"]
            self.assertIn("canonical governed operational knowledge", instructions)
            self.assertIn(f"[memory:{policy.id}@v1]", instructions)
            self.assertIn("repo://architecture/ADR-12", instructions)
            self.assertEqual(len(result.memory_retrieval_ids), 1)
            run = memory.retrieval_run(
                result.memory_retrieval_ids[0],
                actor=actor,
            )
            self.assertEqual(run.selected_knowledge_ids, (policy.id,))
            self.assertLessEqual(run.packed_tokens, 6000)


if __name__ == "__main__":
    unittest.main()
