from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.conversation_channels import (
    ConversationChannelCapability,
    ConversationEventKind,
    ConversationProjectionOutcome,
    UnsupportedConversationChannelCapability,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.conversation_channel_adapters import (
    SlackConversationChannel,
    TeamsConversationChannel,
    TelegramConversationChannel,
)
from codex_web.services.conversation_channels import (
    ConversationChannelRegistry,
    ConversationChannelService,
)
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.conversation_channels import ConversationChannelStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Router:
    def __init__(self) -> None:
        self.messages = []
        self.fail: Exception | None = None

    async def __call__(self, message):
        self.messages.append(message)
        if self.fail is not None:
            raise self.fail
        return {
            "ok": True,
            "threadId": f"thread-{len(self.messages)}",
            "queued": False,
        }


class ConversationChannelAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_slack_normalizes_thread_attachments_mentions_references_and_edits(self) -> None:
        adapter = SlackConversationChannel("slack-connection-1")
        self.assertTrue(
            adapter.capabilities.supports(
                ConversationChannelCapability.THREADS
            )
        )
        self.assertTrue(
            adapter.capabilities.supports(
                ConversationChannelCapability.REACTIONS
            )
        )
        events = await adapter.normalize_events(
            {
                "event_id": "evt-1",
                "event": {
                    "type": "message",
                    "channel": "C1",
                    "user": "U1",
                    "text": "hello <@U2> https://example.test/a",
                    "ts": "100.1",
                    "thread_ts": "99.9",
                    "files": [
                        {
                            "id": "F1",
                            "name": "report.pdf",
                            "mimetype": "application/pdf",
                            "size": 42,
                            "url_private": "https://slack.test/file/F1",
                        }
                    ],
                },
            },
            connection_id="slack-connection-1",
            project_id="project-a",
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_kind, ConversationEventKind.MESSAGE_CREATED)
        self.assertEqual(event.message.conversation.conversation_id, "C1")
        self.assertEqual(event.message.conversation.thread_id, "99.9")
        self.assertEqual(event.message.message_id, "100.1")
        self.assertEqual(event.sender.provider_user_id, "U1")
        self.assertEqual(event.attachments[0].external_id, "F1")
        self.assertEqual(event.mentions[0].external_id, "U2")
        self.assertEqual(event.references[0].value, "https://example.test/a")

        edited = await adapter.normalize_events(
            {
                "event_id": "evt-edit",
                "event": {
                    "type": "message",
                    "subtype": "message_changed",
                    "channel": "C1",
                    "event_ts": "102.0",
                    "message": {
                        "ts": "100.1",
                        "thread_ts": "99.9",
                        "user": "U1",
                        "text": "edited",
                        "edited": {"ts": "102.0"},
                    },
                },
            }
        )
        self.assertEqual(edited[0].event_kind, ConversationEventKind.MESSAGE_EDITED)
        self.assertEqual(edited[0].occurred_at, 102.0)
        self.assertEqual(edited[0].provider_revision, "102.0")

        with self.assertRaises(UnsupportedConversationChannelCapability):
            await adapter.history(event.message.conversation)

    async def test_telegram_normalizes_topics_files_commands_and_edits(self) -> None:
        adapter = TelegramConversationChannel("telegram-connection-1")
        self.assertFalse(
            adapter.capabilities.supports(
                ConversationChannelCapability.REACTIONS
            )
        )
        events = await adapter.normalize_events(
            {
                "update_id": 17,
                "message": {
                    "message_id": 5,
                    "message_thread_id": 9,
                    "date": 100,
                    "chat": {"id": -1001, "title": "Team"},
                    "from": {
                        "id": 77,
                        "username": "alice",
                        "is_bot": False,
                    },
                    "text": "/status https://example.test/status",
                    "entities": [
                        {
                            "type": "bot_command",
                            "offset": 0,
                            "length": 7,
                        }
                    ],
                    "document": {
                        "file_id": "doc-1",
                        "file_name": "status.txt",
                        "mime_type": "text/plain",
                        "file_size": 12,
                    },
                },
            }
        )
        event = events[0]
        self.assertEqual(event.message.conversation.thread_id, "9")
        self.assertEqual(event.provider_sequence, 17)
        self.assertEqual(event.provider_cursor, "18")
        self.assertEqual(event.attachments[0].external_id, "doc-1")
        self.assertEqual(event.mentions[0].kind, "command")
        self.assertEqual(event.references[0].kind, "url")

        edited = await adapter.normalize_events(
            {
                "update_id": 18,
                "edited_message": {
                    "message_id": 5,
                    "message_thread_id": 9,
                    "edit_date": 120,
                    "chat": {"id": -1001},
                    "from": {"id": 77},
                    "text": "edited",
                },
            }
        )
        self.assertEqual(edited[0].event_kind, ConversationEventKind.MESSAGE_EDITED)
        self.assertEqual(edited[0].occurred_at, 120.0)
        self.assertEqual(edited[0].provider_sequence, 18)

    async def test_teams_normalizes_graph_message_without_trusting_identity_claims(self) -> None:
        adapter = TeamsConversationChannel("https://graph.microsoft.com/v1.0")
        self.assertTrue(
            adapter.capabilities.supports(
                ConversationChannelCapability.DELETES
            )
        )
        self.assertFalse(
            adapter.capabilities.supports(
                ConversationChannelCapability.REACTIONS
            )
        )
        events = await adapter.normalize_events(
            {
                "value": [
                    {
                        "id": "notification-1",
                        "changeType": "updated",
                        "resource": "/teams/team-1/channels/channel-1/messages/msg-1",
                        "resourceData": {
                            "id": "msg-1",
                            "chatId": "chat-1",
                            "replyToId": "root-1",
                            "createdDateTime": "2026-09-20T00:00:00Z",
                            "lastModifiedDateTime": "2026-09-20T00:01:00Z",
                            "body": {
                                "contentType": "html",
                                "content": "<p>Hello <b>team</b> https://example.test</p>",
                            },
                            "from": {
                                "user": {
                                    "id": "entra-user-1",
                                    "displayName": "Alice",
                                    "userIdentityType": "aadUser",
                                    "tenantId": "provider-tenant-claim",
                                }
                            },
                            "mentions": [
                                {
                                    "mentionText": "Bob",
                                    "mentioned": {"user": {"id": "entra-user-2"}},
                                }
                            ],
                            "attachments": [
                                {
                                    "id": "attachment-1",
                                    "name": "brief.docx",
                                    "contentType": "reference",
                                    "contentUrl": "https://graph.test/file",
                                }
                            ],
                        },
                    }
                ]
            }
        )
        event = events[0]
        self.assertEqual(event.event_kind, ConversationEventKind.MESSAGE_EDITED)
        self.assertEqual(event.message.conversation.conversation_id, "chat-1")
        self.assertEqual(event.message.conversation.thread_id, "root-1")
        self.assertEqual(event.sender.provider_user_id, "entra-user-1")
        self.assertIn("provider-tenant-claim", event.sender.provider_claims)
        self.assertEqual(event.text, "Hello  team  https://example.test")
        self.assertEqual(event.attachments[0].external_id, "attachment-1")
        self.assertEqual(event.mentions[0].external_id, "entra-user-2")


class ConversationChannelServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.events = CanonicalEventStore(state)
        self.bus = CanonicalEventBus(self.events)
        self.ingestion = CanonicalEventIngestionService(self.bus)
        self.store = ConversationChannelStore(state)
        self.registry = ConversationChannelRegistry()
        self.registry.register_provider("slack", SlackConversationChannel)
        self.registry.register_provider("telegram", TelegramConversationChannel)
        self.registry.register_provider("teams", TeamsConversationChannel)
        self.router = _Router()
        self.service = ConversationChannelService(
            self.store,
            self.registry,
            self.ingestion,
            self.router,
        )
        self.actor_a = AuthenticationActor(
            identity_id="runtime-a",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("conversation:ingest",),
        )
        self.actor_b = AuthenticationActor(
            identity_id="runtime-b",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-b",
            workspace_id="ws-b",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("conversation:ingest",),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def slack_payload(
        *,
        event_id: str,
        text: str = "hello",
        ts: str = "100.0",
        event_ts: str | None = None,
        subtype: str | None = None,
    ):
        event = {
            "type": "message",
            "channel": "C1",
            "user": "U-provider",
            "text": text,
            "ts": ts,
            "event_ts": event_ts or ts,
        }
        if subtype:
            event["subtype"] = subtype
        return {"event_id": event_id, "event": event}

    async def test_duplicate_delivery_and_retry_with_new_delivery_id_route_once(self) -> None:
        first = await self.service.ingest_raw(
            "slack",
            "slack-1",
            self.slack_payload(event_id="delivery-1"),
            actor=self.actor_a,
            connection_id="slack-1",
            project_id="project-a",
        )
        duplicate = await self.service.ingest_raw(
            "slack",
            "slack-1",
            self.slack_payload(event_id="delivery-1"),
            actor=self.actor_a,
            connection_id="slack-1",
            project_id="project-a",
        )
        retried = await self.service.ingest_raw(
            "slack",
            "slack-1",
            self.slack_payload(event_id="delivery-2"),
            actor=self.actor_a,
            connection_id="slack-1",
            project_id="project-a",
        )

        self.assertEqual(first[0].outcome, ConversationProjectionOutcome.ROUTED)
        self.assertEqual(duplicate[0].outcome, ConversationProjectionOutcome.DUPLICATE)
        self.assertEqual(retried[0].outcome, ConversationProjectionOutcome.STALE)
        self.assertEqual(len(self.router.messages), 1)
        self.assertEqual(len(self.events.recent()), 2)

    async def test_edit_delete_and_reordered_edit_never_replay_canonical_work(self) -> None:
        await self.service.ingest_raw(
            "slack",
            "slack-1",
            self.slack_payload(event_id="create", ts="100.0"),
            actor=self.actor_a,
            connection_id="slack-1",
        )
        edit_new = {
            "event_id": "edit-new",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C1",
                "event_ts": "120.0",
                "message": {
                    "ts": "100.0",
                    "user": "U-provider",
                    "text": "newer edit",
                    "edited": {"ts": "120.0"},
                },
            },
        }
        edit_old = {
            "event_id": "edit-old",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": "C1",
                "event_ts": "110.0",
                "message": {
                    "ts": "100.0",
                    "user": "U-provider",
                    "text": "older edit",
                    "edited": {"ts": "110.0"},
                },
            },
        }
        newer = await self.service.ingest_raw(
            "slack",
            "slack-1",
            edit_new,
            actor=self.actor_a,
        )
        older = await self.service.ingest_raw(
            "slack",
            "slack-1",
            edit_old,
            actor=self.actor_a,
        )
        deleted = await self.service.ingest_raw(
            "slack",
            "slack-1",
            {
                "event_id": "delete",
                "event": {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "C1",
                    "deleted_ts": "100.0",
                    "event_ts": "130.0",
                    "previous_message": {
                        "ts": "100.0",
                        "user": "U-provider",
                    },
                },
            },
            actor=self.actor_a,
        )

        self.assertEqual(newer[0].outcome, ConversationProjectionOutcome.UPDATED)
        self.assertEqual(older[0].outcome, ConversationProjectionOutcome.STALE)
        self.assertEqual(deleted[0].outcome, ConversationProjectionOutcome.DELETED)
        self.assertEqual(len(self.router.messages), 1)
        states = self.service.message_states(actor=self.actor_a)
        self.assertEqual(len(states), 1)
        self.assertTrue(states[0].deleted)
        self.assertEqual(states[0].occurred_at, 130.0)

    async def test_same_external_message_identity_is_isolated_between_tenants(self) -> None:
        payload = self.slack_payload(event_id="delivery-tenant")
        a = await self.service.ingest_raw(
            "slack",
            "shared-instance",
            payload,
            actor=self.actor_a,
        )
        b = await self.service.ingest_raw(
            "slack",
            "shared-instance",
            payload,
            actor=self.actor_b,
        )
        self.assertEqual(a[0].outcome, ConversationProjectionOutcome.ROUTED)
        self.assertEqual(b[0].outcome, ConversationProjectionOutcome.ROUTED)
        self.assertEqual(len(self.router.messages), 2)
        self.assertEqual(len(self.service.message_states(actor=self.actor_a)), 1)
        self.assertEqual(len(self.service.message_states(actor=self.actor_b)), 1)
        self.assertEqual(len(self.store.load().messages), 2)

    async def test_provider_sender_claims_do_not_change_canonical_ingest_actor(self) -> None:
        receipts = await self.service.ingest_raw(
            "teams",
            "graph-instance",
            {
                "id": "teams-delivery",
                "changeType": "created",
                "resourceData": {
                    "id": "msg-1",
                    "chatId": "chat-1",
                    "createdDateTime": "2026-09-20T00:00:00Z",
                    "body": {"content": "please deploy"},
                    "from": {
                        "user": {
                            "id": "provider-admin-claim",
                            "displayName": "Provider Admin",
                            "userIdentityType": "aadUser",
                            "tenantId": "other-provider-tenant",
                        }
                    },
                },
            },
            actor=self.actor_a,
            project_id="project-a",
        )
        self.assertEqual(receipts[0].outcome, ConversationProjectionOutcome.ROUTED)
        routed = self.router.messages[-1]
        self.assertEqual(routed.sender_id, "provider-admin-claim")
        self.assertEqual(routed.project_id, "project-a")
        canonical = self.events.recent(limit=1)[0]
        self.assertEqual(canonical.tenant_id, "org-a")
        self.assertEqual(canonical.workspace_id, "ws-a")

    async def test_routing_failure_is_durable_unknown_outcome_and_never_auto_retries(self) -> None:
        self.router.fail = RuntimeError("downstream outcome unknown")
        first = await self.service.ingest_raw(
            "telegram",
            "telegram-1",
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "date": 100,
                    "chat": {"id": 5},
                    "from": {"id": 7},
                    "text": "hello",
                },
            },
            actor=self.actor_a,
        )
        self.router.fail = None
        duplicate = await self.service.ingest_raw(
            "telegram",
            "telegram-1",
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "date": 100,
                    "chat": {"id": 5},
                    "from": {"id": 7},
                    "text": "hello",
                },
            },
            actor=self.actor_a,
        )
        self.assertEqual(
            first[0].outcome,
            ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
        )
        self.assertEqual(
            duplicate[0].outcome,
            ConversationProjectionOutcome.DUPLICATE,
        )
        self.assertEqual(len(self.router.messages), 1)


if __name__ == "__main__":
    unittest.main()
