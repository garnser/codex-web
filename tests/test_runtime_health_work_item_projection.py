from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from codex_web.models import WorkItemState
from codex_web.services.runtime_diagnostics import actionable_owner_projects
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


def _coerce_owner(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized if normalized not in {"", "none", "null"} else None


class RuntimeHealthWorkItemProjectionTests(unittest.TestCase):
    actionable_stages = frozenset(
        {
            "implementation_active",
            "failed_with_action_owner",
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }
    )

    def _repository(
        self,
        directory: str,
        payload: dict[str, object],
    ) -> tuple[ModelMapRepository[WorkItemState], SQLiteStateStore]:
        store = SQLiteStateStore(Path(directory) / "state.sqlite3")
        store.record_replace("work_item_states", payload)
        return (
            ModelMapRepository(
                store,
                namespace="work_item_states",
                legacy_path=Path(directory) / "work-items.json",
                model=WorkItemState,
            ),
            store,
        )

    def test_observed_catalog_uses_bounded_pages_and_narrow_projection(
        self,
    ) -> None:
        payload = {
            f"example/app#{index:04d}": {
                "project_id": "project-noise",
                "current_owner": "larry",
                "current_stage": "closed",
                "execution": "deliberately invalid unrelated field",
            }
            for index in range(1728)
        }
        payload["example/app#0001"] = {
            "project_id": "project-a",
            "current_owner": " James ",
            "current_stage": "implementation_active",
            "execution": "not validated by health projection",
        }
        payload["example/app#0002"] = {
            "project_id": "project-b",
            "next_owner": "quinn",
            "current_stage": "ready_for_validation",
        }

        with tempfile.TemporaryDirectory() as directory:
            repository, store = self._repository(directory, payload)
            with (
                patch.object(
                    store,
                    "record_items",
                    side_effect=AssertionError("whole catalog read"),
                ),
                patch.object(
                    store,
                    "record_page",
                    wraps=store.record_page,
                ) as page,
                patch.object(
                    WorkItemState,
                    "model_validate",
                    side_effect=AssertionError("full Work Item validation"),
                ),
            ):
                result = actionable_owner_projects(
                    repository,
                    owners=("james", "quinn"),
                    coerce_owner=_coerce_owner,
                    actionable_stages=self.actionable_stages,
                )

        self.assertEqual(
            result,
            {"james": {"project-a"}, "quinn": {"project-b"}},
        )
        self.assertEqual(page.call_count, 2)
        self.assertTrue(
            all(call.kwargs["limit"] == 1000 for call in page.call_args_list)
        )

    def test_projection_preserves_actionability_semantics(self) -> None:
        payload = {
            "active-current": {
                "project_id": "project-a",
                "current_owner": "JAMES",
                "next_owner": "quinn",
            },
            "active-next": {
                "project_id": "project-b",
                "current_owner": None,
                "next_owner": "quinn",
                "current_stage": "ready_to_close",
            },
            "pending": {
                "project_id": "project-pending",
                "current_owner": "james",
                "handoff": {},
            },
            "acknowledged": {
                "project_id": "project-acknowledged",
                "current_owner": "james",
                "handoff": {"status": "accepted"},
            },
            "closed": {
                "project_id": "project-closed",
                "current_owner": "james",
                "closed_at": 10.0,
            },
            "zero-closed-at": {
                "project_id": "project-zero",
                "current_owner": "james",
                "closed_at": 0.0,
            },
            "wrong-stage": {
                "project_id": "project-wrong-stage",
                "current_owner": "james",
                "current_stage": "closed",
            },
            "unknown-owner": {
                "project_id": "project-unknown",
                "current_owner": "nobody",
            },
            "no-project": {"current_owner": "james"},
        }
        with tempfile.TemporaryDirectory() as directory:
            repository, _store = self._repository(directory, payload)
            result = actionable_owner_projects(
                repository,
                owners=("james", "quinn"),
                coerce_owner=_coerce_owner,
                actionable_stages=self.actionable_stages,
            )

        self.assertEqual(
            result,
            {
                "james": {
                    "project-a",
                    "project-acknowledged",
                    "project-zero",
                },
                "quinn": {"project-b"},
            },
        )

    def test_relevant_corruption_fails_instead_of_changing_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, _store = self._repository(
                directory,
                {
                    "bad": {
                        "project_id": "project-a",
                        "current_owner": "james",
                        "handoff": "invalid",
                    }
                },
            )
            with self.assertRaises(ValidationError):
                actionable_owner_projects(
                    repository,
                    owners=("james",),
                    coerce_owner=_coerce_owner,
                    actionable_stages=self.actionable_stages,
                )

    def test_no_configured_owners_avoids_storage_io(self) -> None:
        repository = Mock()
        result = actionable_owner_projects(
            repository,
            owners=(),
            coerce_owner=_coerce_owner,
            actionable_stages=self.actionable_stages,
        )
        self.assertEqual(result, {})
        repository.raw_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
