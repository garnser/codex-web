from __future__ import annotations

from typing import Any, Callable

from codex_web.compatibility import MigrationRegistry
from codex_web.model_gateway import MODEL_GATEWAY_CONTRACT, ModelGatewayState
from codex_web.storage.sqlite_state import SQLiteStateStore


MODEL_GATEWAY_MIGRATIONS = MigrationRegistry("model-gateway-state")
MODEL_GATEWAY_MIGRATIONS.register(
    "0.0",
    "1.0",
    lambda payload: {
        "schema_version": "1.0",
        "providers": list(payload.get("providers", [])),
        "models": list(payload.get("models", [])),
        "prompt_templates": list(payload.get("prompt_templates", [])),
        "policies": list(payload.get("policies", [])),
        "invocations": list(payload.get("invocations", [])),
    },
)

MODEL_GATEWAY_MIGRATIONS.register(
    "1.3",
    "1.4",
    lambda payload: {
        **payload,
        "schema_version": "1.4",
        "catalogs": list(payload.get("catalogs", [])),
        "providers": [
            {
                **dict(item),
                "catalog_discovery_enabled": bool(
                    dict(item).get("catalog_discovery_enabled", False)
                ),
                "catalog_ttl_seconds": int(
                    dict(item).get("catalog_ttl_seconds", 300)
                ),
            }
            for item in payload.get("providers", [])
        ],
        "models": [
            {
                **dict(item),
                "availability_source": dict(item).get(
                    "availability_source", "static"
                ),
                "upstream_provider_id": dict(item).get(
                    "upstream_provider_id"
                ),
                "upstream_model_id": dict(item).get("upstream_model_id"),
            }
            for item in payload.get("models", [])
        ],
        "invocations": [
            {
                **dict(item),
                "selected_upstream_provider_id": dict(item).get(
                    "selected_upstream_provider_id"
                ),
                "selected_upstream_model_id": dict(item).get(
                    "selected_upstream_model_id"
                ),
                "selected_catalog_revision": dict(item).get(
                    "selected_catalog_revision"
                ),
                "selected_catalog_discovered_at": dict(item).get(
                    "selected_catalog_discovered_at"
                ),
            }
            for item in payload.get("invocations", [])
        ],
    },
)
MODEL_GATEWAY_MIGRATIONS.register(
    "1.0",
    "1.1",
    lambda payload: {
        **payload,
        "schema_version": "1.1",
        "invocations": [
            {
                **item,
                "input_plugin_provenance": list(
                    item.get("input_plugin_provenance", [])
                ),
                "input_gated_proposals": list(
                    item.get("input_gated_proposals", [])
                ),
            }
            for item in payload.get("invocations", [])
        ],
    },
)

MODEL_GATEWAY_MIGRATIONS.register(
    "1.1",
    "1.2",
    lambda payload: {
        **payload,
        "schema_version": "1.2",
        "invocations": [
            {
                **dict(item),
                "attempts": [
                    {**dict(attempt), "failure": dict(attempt).get("failure")}
                    for attempt in dict(item).get("attempts", [])
                ],
            }
            for item in payload.get("invocations", [])
        ],
    },
)

MODEL_GATEWAY_MIGRATIONS.register(
    "1.2",
    "1.3",
    lambda payload: {
        **payload,
        "schema_version": "1.3",
        "models": [
            {
                **dict(item),
                "workload_classes": list(
                    dict(item).get("workload_classes", [])
                ),
            }
            for item in payload.get("models", [])
        ],
        "invocations": [
            {
                **dict(item),
                "workload_class": dict(item).get("workload_class"),
                "pinned_model_id": dict(item).get("pinned_model_id"),
                "preferred_latency_classes": list(
                    dict(item).get("preferred_latency_classes", [])
                ),
                "prefer_lower_cost": bool(
                    dict(item).get("prefer_lower_cost", False)
                ),
            }
            for item in payload.get("invocations", [])
        ],
    },
)


class ModelGatewayStore:
    namespace = "model_gateway"

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def _decode(self, payload: Any) -> ModelGatewayState:
        if payload is None:
            return ModelGatewayState()
        if not isinstance(payload, dict):
            raise ValueError("model gateway state must be an object")
        version = str(payload.get("schema_version") or "0.0")
        if version != MODEL_GATEWAY_CONTRACT.current:
            payload = MODEL_GATEWAY_MIGRATIONS.migrate(
                payload,
                from_version=version,
                to_version=MODEL_GATEWAY_CONTRACT.current,
            )
        MODEL_GATEWAY_CONTRACT.require(payload.get("schema_version", ""))
        return ModelGatewayState.model_validate(payload)

    @staticmethod
    def _encode(state: ModelGatewayState) -> dict[str, Any]:
        return state.model_dump(mode="json")

    def load(self) -> ModelGatewayState:
        return self._decode(self.store.get(self.namespace))

    def update(
        self,
        updater: Callable[[ModelGatewayState], ModelGatewayState],
    ) -> ModelGatewayState:
        def apply(raw: Any) -> dict[str, Any]:
            current = self._decode(raw)
            return self._encode(updater(current))

        payload = self.store.update(
            self.namespace,
            apply,
            default=ModelGatewayState().model_dump(mode="json"),
        )
        return self._decode(payload)
