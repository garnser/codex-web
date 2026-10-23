"""Explicit durable START wire compatibility and private reasoning admission."""
import unittest
from dataclasses import replace
from typing import Literal
from types import SimpleNamespace as NS
from unittest.mock import Mock

from pydantic import ValidationError
from codex_web.models import QueuedNativeStartCorrelation
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.services.queued_native_start import QueuedNativeStartCapture
from codex_web.services.native_model_provider import (NativeModelInvocationContext,NativeProcessOwnership,
    _current_native_model_invocation)
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError


class LegacyCodexCorrelation(QueuedNativeStartCorrelation):
    # The unchanged old reader's exact discriminants, rather than a decoder
    # that ignores an unknown extension field and could silently drop proof.
    schema_version:Literal['1.0']='1.0'
    runtime_id:Literal['codex']='codex'


class NativeReasoningCaptureTests(unittest.TestCase):
    def fields(self):
        return dict(snapshot_sha256='a'*64,admission_claim_id='claim',requested_execution_id='executive:a:cto',
            organization_id='org',workspace_id='workspace',project_id='project',actor_identity_id='human',
            assignment_resource_ids=(),assignment_id='assignment',execution_id='bootstrap',worker_id='worker',
            fence=1,thread_id='thread',provider_native_session_id='thread',runtime_generation='generation',
            rpc_request_id='rpc',prepared_at=1.)

    def test_codex_ordinary_wire_is_unchanged_and_old_reader_rejects_reasoning_variant(self):
        ordinary=QueuedNativeStartCorrelation(**self.fields())
        self.assertEqual(ordinary.model_dump(mode='json'),LegacyCodexCorrelation(**self.fields()).model_dump(mode='json'))
        reasoning=QueuedNativeStartCorrelation(**self.fields(),schema_version='1.1',runtime_id='codex-reasoning')
        with self.assertRaises(ValidationError) as caught:
            LegacyCodexCorrelation.model_validate(reasoning.model_dump(mode='json'))
        self.assertEqual({e['loc'] for e in caught.exception.errors()},{('schema_version',),('runtime_id',)})
        for changed in ({'runtime_id':'codex-reasoning'},{'schema_version':'1.1'}):
            with self.assertRaises(ValidationError):QueuedNativeStartCorrelation(**self.fields(),**changed)

    def test_runtime_label_alone_cannot_reserve_reasoning_start(self):
        service=NS(store=Mock())
        assignment=NS(id='assignment',runtime_binding=ExecutionRuntimeBinding(provider_id='openai',
            runtime_id='codex-reasoning',capability_revision=1),organization_id='org',workspace_id='workspace',project_id='project')
        capture=QueuedNativeStartCapture(service,'attempt','claim','executive:a:cto','thread','human',assignment=assignment)
        with self.assertRaises(NativeReasoningPolicyError):
            capture.prepare(generation='generation',rpc_request_id='rpc',native_session_id='thread')
        context=NativeModelInvocationContext(organization_id='org',workspace_id='workspace',actor_id='human',
            project_id='project',activation_id='a',role_id='cto',execution_id='executive:a:cto',
            max_input_tokens=100,max_output_tokens=100,validate_current=lambda:None)
        for changed in ({},{'process_owner':NativeProcessOwnership(assignment_id='other')},
                        {'actor_id':'other','process_owner':NativeProcessOwnership(assignment_id='assignment')}):
            token=_current_native_model_invocation.set(replace(context,**changed))
            try:
                with self.assertRaises(ValueError):
                    capture.prepare(generation='generation',rpc_request_id='rpc',native_session_id='thread')
            finally:_current_native_model_invocation.reset(token)
        service.store.update.assert_not_called()
