"""Isolated canonical composition; only transport/native execution are doubles."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from codex_web.api.conversation_channels import build_conversation_channels_router
from codex_web.api.identity import install_identity_middleware
from codex_web.crypto import KeyPurpose, ManagedKeyCreate
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.models import BotConnectionCreate, Project
from codex_web.runtime.execution import TurnExecutionService
from codex_web.runtime.bots import SlackSocketCleanClose
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.conversation_channel_adapters import SlackConversationChannel
from codex_web.services.conversation_channels import ConversationChannelRegistry, ConversationChannelService
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.identity import IdentityService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.bot_state import BotStateRepositories
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.conversation_channels import ConversationChannelStore
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository
from tests.test_slack_socket_backpressure import _runtime, _Socket
from tests.test_turn_execution import _Host


def payload(index=0, channel="C1"):
    return {"type": "event_callback", "event_id": f"E{index}", "team_id": "untrusted",
        "event": {"type": "message", "channel": channel, "user": "U1", "ts": f"100.{index + 1}",
            "text": "  original <@U2> input\n" + "x" * 40000, "thread_ts": "99.9"}}


def compose(root: Path, *, stage="normal"):
    root.mkdir(parents=True, exist_ok=True)
    state = SQLiteStateStore(root / "state.sqlite3")
    identity = IdentityService(IdentityStateStore(state))
    identity.bootstrap_local()
    admin = identity.local_trusted_actor()
    projects = ProjectRepository(root / "projects.json", store=state)
    if state.get("projects") is None:
        projects.save([Project(id="fixture", name="Isolated", path=str(root), sandbox="read-only")])
    repositories = BotStateRepositories(state, connections_file=root / "connections.json", bindings_file=root / "bindings.json")
    secrets = SecretBroker(SecretStateStore(state), {"local": LocalFileSecretBackend(root / "secrets")})
    connections = BotConnectionService(load_connections=repositories.connections.load,
        save_connections=repositories.connections.save, load_bindings=repositories.bindings.load,
        save_bindings=repositories.bindings.save,
        projects=SimpleNamespace(get=lambda key: next(p for p in projects.load() if p.id == key)),
        binding_prefix=lambda binding: binding.route_prefix, identity_service=identity, secret_broker=secrets)
    existing = repositories.connections.load()
    connection = existing[0] if existing else connections.upsert(BotConnectionCreate(
        id="fixture-slack", provider="slack", name="Fixture", project_id="fixture",
        bot_token="controlled-bot-token", slack_app_token="controlled-app-token"), actor=admin)
    crypto = CryptoKeyService(CryptoKeyStore(state), {"local": LocalFileKeyBackend(root / "keys")})
    if not crypto.store.load().keys:
        crypto.create_key(ManagedKeyCreate(project_id="fixture", purpose=KeyPurpose.INTEGRATION_PAYLOAD), actor=admin)
    governance = DataGovernanceService(DataGovernanceStore(state))
    queue = TurnQueueRepository(state, root / "queue.json")
    host = _Host()
    host._thread_queue_record = queue.get
    host._thread_queue = queue.get
    host._update_thread_queue_record = queue.update
    host._update_thread_queue_with_ingress = queue.update_with_ingress
    execution = TurnExecutionService(host)
    preflight = ExecutionPreflightService(ExecutionPreflightStore(state))
    execution.queued_preflight = preflight
    async def route(message):
        admitted = execution.enqueue_turn(thread_id="fixture-thread", project_id="fixture", message=message.text,
            source="slack", sandbox="read-only", approval_policy="on-request",
            execution_profile_id="orchestration-only", agent_profile_id="fixture-agent", agent_profile_revision=1,
            agent_profile_actor_id="system-bot-runtime", writable_repository_resource_ids=(), read_only_repository_resource_ids=(),
            canonical_ingress_id=message.canonical_ingress_id)
        if stage == "queue-crash":
            os._exit(78)
        return {"ok": True, "queued": True, "threadId": admitted.thread_id, "queuedId": admitted.id}
    registry = ConversationChannelRegistry()
    registry.register_provider("slack", SlackConversationChannel)
    service = ConversationChannelService(ConversationChannelStore(state), registry,
        CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(state))), route,
        crypto=crypto, governance=governance, authority_snapshot=connections.ingress_snapshot)
    governance.register_action_handler("conversation_ingress", service.governance_action)
    app = FastAPI()
    install_identity_middleware(app, identity)
    app.include_router(build_conversation_channels_router(service))
    runtime = _runtime()
    runtime.connections = connections
    runtime.conversation_channels = service
    runtime.secret_broker = secrets
    runtime._recover_retained_slack = AsyncMock()  # deterministic cut before routing
    return SimpleNamespace(state=state, identity=identity, admin=admin, connection=connection, connections=connections,
        crypto=crypto, governance=governance, service=service, queue=queue, execution=execution, preflight=preflight, app=app, runtime=runtime)


async def run(root, stage):
    fixture = compose(root, stage=stage)
    if stage == "ack-crash":
        class CrashSocket(_Socket):
            async def send(self, raw):
                super_result = await super().send(raw)
                print(raw, flush=True)
                os._exit(77)
                return super_result
        socket = CrashSocket([{"envelope_id": "controlled-envelope", "payload": payload()}])
        with patch("codex_web.runtime.bots.websockets.connect", return_value=socket):
            await fixture.runtime._run_slack(fixture.connection)
    else:
        actor = fixture.connections.runtime_actor("fixture")
        for event in fixture.service.pending_events(actor=actor, connection_id=fixture.connection.id, project_id="fixture"):
            await fixture.service.ingest(event, actor=actor)
        print(json.dumps(fixture.service.status(actor=actor)), flush=True)


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
