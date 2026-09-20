from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.models import BotBinding
from codex_web.services.bot_bindings import BotBindingLifecycleService


def _binding(binding_id: str = "binding-1") -> BotBinding:
    return BotBinding(
        id=binding_id,
        provider="slack",
        external_conversation_id="C1",
        thread_id="thread-1",
        project_id="project-a",
        created_at=1.0,
        updated_at=2.0,
    )


class BotBindingChangeEventTests(unittest.TestCase):
    def _service(self):
        stored: list[BotBinding] = []
        events: list[dict[str, object]] = []

        def load():
            return [item.model_copy(deep=True) for item in stored]

        def save(values):
            stored[:] = [item.model_copy(deep=True) for item in values]

        service = BotBindingLifecycleService(
            load_bindings=load,
            save_bindings=save,
            connections=SimpleNamespace(dedupe_integrations=lambda: None),
            selection=SimpleNamespace(),
            targets=SimpleNamespace(),
            presentation=SimpleNamespace(),
            projects=SimpleNamespace(),
            runtime_request=lambda *_args, **_kwargs: None,
            set_thread_name=lambda *_args, **_kwargs: None,
            on_change=events.append,
        )
        return service, stored, events

    def test_upsert_emits_bounded_project_thread_hint(self) -> None:
        service, stored, events = self._service()

        saved = service.upsert(_binding())

        self.assertEqual(len(stored), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0],
            {
                "type": "binding.updated",
                "projectId": "project-a",
                "threadId": "thread-1",
                "bindingId": saved.id,
                "updatedAt": 2.0,
            },
        )
        self.assertNotIn("bot_token", events[0])

    def test_remove_emits_same_scoped_hint(self) -> None:
        service, _stored, events = self._service()
        saved = service.upsert(_binding())
        events.clear()

        service.remove(saved.id)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "binding.updated")
        self.assertEqual(events[0]["projectId"], "project-a")
        self.assertEqual(events[0]["threadId"], "thread-1")
        self.assertEqual(events[0]["bindingId"], saved.id)
        self.assertTrue(events[0]["removed"])


if __name__ == "__main__":
    unittest.main()
