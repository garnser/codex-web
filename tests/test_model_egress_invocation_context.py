from __future__ import annotations

import asyncio
import base64
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from codex_web.services.agent_model_egress import (
    AgentRuntimeModelEgressEndpoint,
    AssignmentBoundAgentModelEgressBroker,
)
from codex_web.services.native_model_provider import (
    NativeModelInvocationContext,
    NativeProcessOwnership,
    _current_native_model_invocation,
    current_native_model_invocation,
)
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError


class ModelEgressInvocationContextTests(unittest.IsolatedAsyncioTestCase):
    def context(self, name="original"):
        owner = NativeProcessOwnership(deadline_at=time.monotonic() + 30)
        return NativeModelInvocationContext(
            organization_id="org", workspace_id="workspace", actor_id="actor",
            project_id="project", activation_id=name, role_id="cpo",
            execution_id=f"executive:{name}:cpo", max_input_tokens=100,
            max_output_tokens=100, validate_current=owner.validate_current,
            process_owner=owner,
        )

    def broker(self, validator, *, cache=0):
        broker = AssignmentBoundAgentModelEgressBroker(
            (AgentRuntimeModelEgressEndpoint("chatgpt.com"),),
            validator=validator, validation_cache_seconds=cache,
        )
        self.brokers.append(broker)
        return broker

    async def asyncSetUp(self):
        self.brokers = []

    async def asyncTearDown(self):
        for broker in self.brokers:
            pool = broker._blocking_executor
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            await broker.stop()

    async def test_original_canonical_context_reaches_real_executor_and_is_restored(self):
        original = self.context()
        observed = []
        def validate():
            current = current_native_model_invocation()
            self.assertIs(current, original)
            current.validate_current()
            observed.append(threading.get_ident())
        broker = self.broker(validate)
        token = _current_native_model_invocation.set(original)
        try:
            await broker._validate_current_async()
            self.assertIs(current_native_model_invocation(), original)
        finally:
            _current_native_model_invocation.reset(token)
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0], threading.get_ident())
        with self.assertRaises(NativeReasoningPolicyError):
            current_native_model_invocation()
        # The same actual pool must not retain the previous invocation.
        loop = asyncio.get_running_loop()
        with self.assertRaises(NativeReasoningPolicyError):
            await loop.run_in_executor(broker._blocking_pool(), current_native_model_invocation)

    async def test_accepted_connect_keeps_server_owner_not_client_context(self):
        original, foreign = self.context(), self.context("foreign")
        observed = []
        def validate():
            current = current_native_model_invocation()
            self.assertIs(current, original)
            current.validate_current()
            observed.append(threading.get_ident())
        broker = self.broker(validate)
        token = _current_native_model_invocation.set(original)
        try:
            await broker.start()
        finally:
            _current_native_model_invocation.reset(token)

        class UpstreamWriter:
            def write(self, value): pass
            async def drain(self): pass
            def close(self): pass
            def is_closing(self): return False
            def can_write_eof(self): return False
            async def wait_closed(self): pass
        upstream = asyncio.StreamReader()
        upstream.feed_eof()
        token = _current_native_model_invocation.set(foreign)
        writer = None
        try:
            # Real private Unix accept/auth/dispatch; upstream networking alone
            # is replaced, after the unchanged authority check has succeeded.
            with patch.object(broker, "_open_upstream", AsyncMock(
                    return_value=(upstream, UpstreamWriter()))) as opened:
                reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
                auth = base64.b64encode(f"agent-runtime:{broker.capability}".encode()).decode()
                writer.write(("CONNECT chatgpt.com:443 HTTP/1.1\r\n"
                              f"Proxy-Authorization: Basic {auth}\r\n\r\n").encode())
                await writer.drain()
                response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                self.assertIn(b"200 Connection Established", response)
                self.assertEqual(opened.await_count, 1)
                self.assertIs(current_native_model_invocation(), foreign)
                self.assertEqual(broker.connections, 1)
                self.assertEqual(broker.denied_connections, 0)
                writer.close()
                await writer.wait_closed()
                await asyncio.wait_for(asyncio.gather(*tuple(broker._handler_tasks)), 2)
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            _current_native_model_invocation.reset(token)
        self.assertEqual(len(observed), 1)
        self.assertNotEqual(observed[0], threading.get_ident())

    async def test_missing_context_is_refused_without_caching_success(self):
        broker = self.broker(current_native_model_invocation)
        with self.assertRaises(NativeReasoningPolicyError):
            await broker._validate_current_async()
        self.assertIsNone(broker._last_validation_at)

    async def test_foreign_context_cannot_replace_original_authority(self):
        original, foreign = self.context(), self.context("foreign")
        def validate():
            current = current_native_model_invocation()
            if current is not original:
                raise NativeReasoningPolicyError("Original invocation changed")
            current.validate_current()
        broker = self.broker(validate)
        token = _current_native_model_invocation.set(foreign)
        try:
            with self.assertRaises(NativeReasoningPolicyError):
                await broker._validate_current_async()
            self.assertIs(current_native_model_invocation(), foreign)
        finally:
            _current_native_model_invocation.reset(token)
        self.assertIsNone(broker._last_validation_at)
        token = _current_native_model_invocation.set(original)
        try:
            await broker._validate_current_async()
        finally:
            _current_native_model_invocation.reset(token)

    async def test_cancelled_original_owner_remains_refused(self):
        original = self.context()
        original.process_owner.cancelled.set()
        broker = self.broker(lambda: current_native_model_invocation().validate_current())
        token = _current_native_model_invocation.set(original)
        try:
            with self.assertRaises(NativeReasoningPolicyError):
                await broker._validate_current_async()
        finally:
            _current_native_model_invocation.reset(token)
        self.assertIsNone(broker._last_validation_at)

    async def test_existing_success_cache_and_coalescing_are_preserved(self):
        original = self.context()
        observed = []
        def validate():
            self.assertIs(current_native_model_invocation(), original)
            original.validate_current()
            observed.append(True)
        broker = self.broker(validate, cache=5)
        token = _current_native_model_invocation.set(original)
        try:
            await asyncio.gather(broker._validate_current_async(), broker._validate_current_async())
        finally:
            _current_native_model_invocation.reset(token)
        self.assertEqual(observed, [True])
