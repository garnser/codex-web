from __future__ import annotations

import unittest

from codex_web.input_plugins import (
    InputContextBlock,
    InputEnvelope,
    InputFailurePolicy,
    InputMessage,
    InputPatch,
    InputPhase,
    InputPluginBudgetError,
    InputPluginExecutionError,
    InputPluginPipeline,
    InputPluginRegistration,
    InputPluginSecurityError,
    NormalizeWhitespaceInputPlugin,
)


def envelope(**overrides) -> InputEnvelope:
    payload = {
        "request_id": "request-1",
        "organization_id": "org-a",
        "workspace_id": "ws-a",
        "actor_id": "actor-a",
        "model_class": "primary-coding",
        "messages": (InputMessage(role="user", content="  build it  "),),
        "system_prompt": "  system instructions  ",
        "context_blocks": (
            InputContextBlock(
                id="context-1",
                source="work-item",
                content="bounded context",
            ),
        ),
        "required_capabilities": ("text",),
        "max_output_tokens": 2048,
        "max_cost_usd": 2.0,
        "work_item_ref": "group/app#1",
        "purpose": "implementation",
        "authority_refs": ("authority-1",),
        "policy_refs": ("policy-1",),
    }
    payload.update(overrides)
    return InputEnvelope(**payload)


class _AppendPlugin:
    transport = "builtin"

    def __init__(
        self,
        plugin_id: str,
        *,
        version: str = "1.0.0",
        phase: InputPhase = InputPhase.COMPOSE,
        suffix: str = "",
        changes: dict | None = None,
    ) -> None:
        self.id = plugin_id
        self.version = version
        self.phase = phase
        self.suffix = suffix
        self.changes = changes

    async def transform(self, current, context):
        if self.changes is not None:
            changes = dict(self.changes)
        else:
            changes = {"system_prompt": current.system_prompt + self.suffix}
        return InputPatch(
            plugin_id=self.id,
            plugin_version=self.version,
            phase=self.phase,
            changes=changes,
        )


class _BrokenPlugin:
    id = "broken"
    version = "1.0.0"
    transport = "builtin"

    async def transform(self, current, context):
        del current, context
        raise RuntimeError("TOP SECRET plugin failure body")


class InputPluginPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_builtin_normalizer_is_deterministic_and_content_free_in_provenance(self) -> None:
        pipeline = InputPluginPipeline(
            [
                InputPluginRegistration(
                    plugin=NormalizeWhitespaceInputPlugin(),
                    phase=InputPhase.NORMALIZE,
                )
            ]
        )

        result = await pipeline.execute(envelope())

        self.assertEqual(result.envelope.system_prompt, "system instructions")
        self.assertEqual(result.envelope.messages[0].content, "build it")
        self.assertEqual(len(result.provenance), 1)
        provenance = result.provenance[0]
        self.assertEqual(provenance.plugin_id, "builtin.normalize-whitespace")
        self.assertEqual(provenance.outcome, "applied")
        self.assertEqual(provenance.applied_fields, ("messages", "system_prompt"))
        serialized = provenance.model_dump_json()
        self.assertNotIn("system instructions", serialized)
        self.assertNotIn("build it", serialized)
        self.assertEqual(len(provenance.input_sha256), 64)
        self.assertEqual(len(provenance.patch_sha256 or ""), 64)
        self.assertEqual(len(provenance.output_sha256), 64)
        self.assertLess(
            provenance.estimated_tokens_after,
            provenance.estimated_tokens_before,
        )

    async def test_phase_then_order_is_deterministic(self) -> None:
        pipeline = InputPluginPipeline(
            [
                InputPluginRegistration(
                    plugin=_AppendPlugin(
                        "compose-late",
                        phase=InputPhase.COMPOSE,
                        suffix="|compose-late",
                    ),
                    phase=InputPhase.COMPOSE,
                    order=20,
                ),
                InputPluginRegistration(
                    plugin=_AppendPlugin(
                        "normalize",
                        phase=InputPhase.NORMALIZE,
                        suffix="|normalize",
                    ),
                    phase=InputPhase.NORMALIZE,
                    order=999,
                ),
                InputPluginRegistration(
                    plugin=_AppendPlugin(
                        "compose-first",
                        phase=InputPhase.COMPOSE,
                        suffix="|compose-first",
                    ),
                    phase=InputPhase.COMPOSE,
                    order=10,
                ),
            ]
        )

        result = await pipeline.execute(envelope(system_prompt="base"))

        self.assertEqual(
            result.envelope.system_prompt,
            "base|normalize|compose-first|compose-late",
        )
        self.assertEqual(
            [item.plugin_id for item in result.provenance],
            ["normalize", "compose-first", "compose-late"],
        )

    async def test_protected_and_unknown_fields_always_fail_closed(self) -> None:
        for changes in (
            {"organization_id": "other-org"},
            {"secret_refs": ("secret-1",)},
            {"not_a_real_field": "value"},
        ):
            with self.subTest(changes=changes):
                plugin = _AppendPlugin(
                    "attacker",
                    changes=changes,
                )
                pipeline = InputPluginPipeline(
                    [
                        InputPluginRegistration(
                            plugin=plugin,
                            phase=InputPhase.COMPOSE,
                            failure_policy=InputFailurePolicy.FAIL_OPEN,
                        )
                    ]
                )
                with self.assertRaises(InputPluginSecurityError):
                    await pipeline.execute(envelope())

    async def test_gated_fields_are_proposals_until_core_validator_accepts_them(self) -> None:
        plugin = _AppendPlugin(
            "budget-proposer",
            changes={
                "max_output_tokens": 4096,
                "reasoning_effort": "high",
                "system_prompt": "optimized",
            },
        )
        registration = InputPluginRegistration(
            plugin=plugin,
            phase=InputPhase.COMPOSE,
        )

        rejected = await InputPluginPipeline([registration]).execute(envelope())
        self.assertEqual(rejected.envelope.system_prompt, "optimized")
        self.assertEqual(rejected.envelope.max_output_tokens, 2048)
        self.assertIsNone(rejected.envelope.reasoning_effort)
        self.assertEqual(
            rejected.provenance[0].proposed_gated_fields,
            ("max_output_tokens", "reasoning_effort"),
        )
        self.assertEqual(
            rejected.provenance[0].rejected_fields,
            ("max_output_tokens", "reasoning_effort"),
        )
        self.assertEqual(len(rejected.gated_proposals), 2)
        self.assertNotIn("4096", str(rejected.gated_proposals))

        def validator(field, value, current, context):
            del value, current, context
            return field == "reasoning_effort"

        accepted = await InputPluginPipeline(
            [registration],
            gated_validator=validator,
        ).execute(envelope())
        self.assertEqual(accepted.envelope.reasoning_effort, "high")
        self.assertEqual(accepted.envelope.max_output_tokens, 2048)
        self.assertEqual(
            accepted.provenance[0].applied_fields,
            ("reasoning_effort", "system_prompt"),
        )

    async def test_plugin_warning_content_is_hashed_before_provenance(self) -> None:
        class _WarningPlugin:
            id = "warning-plugin"
            version = "1.0.0"
            transport = "builtin"

            async def transform(self, current, context):
                del context
                return InputPatch(
                    plugin_id=self.id,
                    plugin_version=self.version,
                    phase=InputPhase.COMPOSE,
                    changes={"system_prompt": current.system_prompt},
                    warnings=("TOP SECRET WARNING BODY",),
                )

        result = await InputPluginPipeline(
            [
                InputPluginRegistration(
                    plugin=_WarningPlugin(),
                    phase=InputPhase.COMPOSE,
                )
            ]
        ).execute(envelope())

        serialized = result.provenance[0].model_dump_json()
        self.assertNotIn("TOP SECRET WARNING BODY", serialized)
        self.assertIn("plugin_warning_sha256:", serialized)

    async def test_failure_policy_never_persists_exception_message(self) -> None:
        fail_open = await InputPluginPipeline(
            [
                InputPluginRegistration(
                    plugin=_BrokenPlugin(),
                    phase=InputPhase.COMPOSE,
                    failure_policy=InputFailurePolicy.FAIL_OPEN,
                )
            ]
        ).execute(envelope())
        self.assertEqual(fail_open.provenance[0].outcome, "failed_open")
        self.assertNotIn(
            "TOP SECRET",
            fail_open.provenance[0].model_dump_json(),
        )

        skipped = await InputPluginPipeline(
            [
                InputPluginRegistration(
                    plugin=_BrokenPlugin(),
                    phase=InputPhase.COMPOSE,
                    failure_policy=InputFailurePolicy.SKIP,
                )
            ]
        ).execute(envelope())
        self.assertEqual(skipped.provenance[0].outcome, "skipped_after_error")

        with self.assertRaisesRegex(
            InputPluginExecutionError,
            "RuntimeError",
        ) as raised:
            await InputPluginPipeline(
                [
                    InputPluginRegistration(
                        plugin=_BrokenPlugin(),
                        phase=InputPhase.COMPOSE,
                        failure_policy=InputFailurePolicy.FAIL_CLOSED,
                    )
                ]
            ).execute(envelope())
        self.assertNotIn("TOP SECRET", str(raised.exception))

    async def test_patch_and_added_context_budgets_fail_closed(self) -> None:
        large_patch = _AppendPlugin(
            "large-patch",
            changes={"system_prompt": "x" * 5000},
        )
        with self.assertRaisesRegex(InputPluginExecutionError, "InputPluginBudgetError"):
            await InputPluginPipeline(
                [
                    InputPluginRegistration(
                        plugin=large_patch,
                        phase=InputPhase.COMPOSE,
                        max_patch_bytes=512,
                    )
                ]
            ).execute(envelope(system_prompt=""))

        context_growth = _AppendPlugin(
            "context-growth",
            changes={
                "context_blocks": (
                    InputContextBlock(
                        id="large",
                        source="test",
                        content="x" * 100,
                    ),
                )
            },
        )
        with self.assertRaisesRegex(InputPluginExecutionError, "InputPluginBudgetError"):
            await InputPluginPipeline(
                [
                    InputPluginRegistration(
                        plugin=context_growth,
                        phase=InputPhase.COMPOSE,
                        max_added_characters=10,
                    )
                ]
            ).execute(envelope(context_blocks=()))

    async def test_no_plugins_preserve_the_exact_envelope(self) -> None:
        initial = envelope()
        result = await InputPluginPipeline().execute(initial)

        self.assertEqual(result.envelope, initial)
        self.assertEqual(result.provenance, ())
        self.assertEqual(result.gated_proposals, ())


if __name__ == "__main__":
    unittest.main()
