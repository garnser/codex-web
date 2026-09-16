from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.executive import ExecutiveChatRequest
from codex_web.executive_integration import MultiProviderExecutiveService
from codex_web.services.executive_knowledge import (
    ExecutiveKnowledgeStore,
    ExecutiveKnowledgeUpsert,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


class ExecutiveKnowledgeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        sqlite = SQLiteStateStore(self.data_dir / "state.db")
        self.host = SimpleNamespace(
            DATA_DIR=self.data_dir,
            app=SimpleNamespace(state=SimpleNamespace(sqlite_state_store=sqlite)),
        )
        self.store = ExecutiveKnowledgeStore(self.host)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unscoped_search_only_returns_company_knowledge(self) -> None:
        self.store.upsert(
            ExecutiveKnowledgeUpsert(
                id="company-db",
                title="Database standard",
                content="PostgreSQL is the preferred durable relational database.",
                tags=["database", "postgresql"],
                source="architecture decision",
            )
        )
        self.store.upsert(
            ExecutiveKnowledgeUpsert(
                id="project-a",
                scope="project",
                project_id="a",
                title="Project A PostgreSQL migration",
                content="Project A migration is scheduled for Q4.",
                tags=["postgresql"],
            )
        )

        results = self.store.search("Which PostgreSQL database standard applies?")

        self.assertEqual([entry.id for entry in results], ["company-db"])

    def test_project_search_includes_company_and_matching_project_but_not_other_projects(self) -> None:
        for payload in (
            ExecutiveKnowledgeUpsert(
                id="company",
                title="Authentication baseline",
                content="All services use SSO and least privilege.",
                tags=["authentication"],
            ),
            ExecutiveKnowledgeUpsert(
                id="project-a",
                scope="project",
                project_id="a",
                title="Authentication rollout",
                content="Project A uses Keycloak for authentication.",
                tags=["authentication", "keycloak"],
            ),
            ExecutiveKnowledgeUpsert(
                id="project-b",
                scope="project",
                project_id="b",
                title="Authentication rollout",
                content="Project B is evaluating another authentication provider.",
                tags=["authentication"],
            ),
        ):
            self.store.upsert(payload)

        results = self.store.search("authentication Keycloak", project_id="a")
        ids = {entry.id for entry in results}

        self.assertEqual(ids, {"company", "project-a"})
        self.assertNotIn("project-b", ids)

    def test_upsert_preserves_created_at_and_delete_is_explicit(self) -> None:
        first = self.store.upsert(
            ExecutiveKnowledgeUpsert(id="decision", title="Pricing", content="Start at $99.")
        )
        second = self.store.upsert(
            ExecutiveKnowledgeUpsert(id="decision", title="Pricing", content="Start at $129.")
        )

        self.assertEqual(first.created_at, second.created_at)
        self.assertGreaterEqual(second.updated_at, first.updated_at)
        self.assertEqual(second.content, "Start at $129.")
        self.assertTrue(self.store.delete("decision"))
        self.assertFalse(self.store.delete("decision"))

    def test_project_scope_requires_project_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "project_id"):
            self.store.upsert(
                ExecutiveKnowledgeUpsert(
                    scope="project",
                    title="Invalid",
                    content="No project supplied.",
                )
            )

    def test_fallback_store_stays_inside_host_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            store = ExecutiveKnowledgeStore(SimpleNamespace(DATA_DIR=data_dir))

            self.assertEqual(store.store.path, data_dir / "codex-web.db")


class ExecutiveKnowledgeInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_company_knowledge_is_injected_into_executive_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            sqlite = SQLiteStateStore(data_dir / "state.db")
            host = SimpleNamespace(
                DATA_DIR=data_dir,
                app=SimpleNamespace(state=SimpleNamespace(sqlite_state_store=sqlite)),
            )
            service = MultiProviderExecutiveService(host)
            service.provider = "openai"
            service.knowledge.upsert(
                ExecutiveKnowledgeUpsert(
                    id="db-policy",
                    title="Database standard",
                    content="PostgreSQL is the company standard for new transactional services.",
                    tags=["postgresql", "database"],
                    source="ADR-12",
                )
            )

            captured: list[dict] = []

            class Responses:
                async def create(self, **kwargs):
                    captured.append(kwargs)
                    return SimpleNamespace(output_text="Use the existing PostgreSQL standard.")

            service._openai_client = SimpleNamespace(responses=Responses())

            result = await service.chat(
                ExecutiveChatRequest(
                    session_id="knowledge-test",
                    message="Which database should the new transactional service use? PostgreSQL?",
                )
            )

            self.assertEqual(result.reply, "Use the existing PostgreSQL standard.")
            self.assertEqual(len(captured), 1)
            instructions = captured[0]["instructions"]
            self.assertIn("DURABLE COMPANY / PROJECT KNOWLEDGE", instructions)
            self.assertIn("PostgreSQL is the company standard", instructions)
            self.assertIn("source=ADR-12", instructions)


if __name__ == "__main__":
    unittest.main()
