from __future__ import annotations

import time
from typing import Any

from codex_web.configuration import (
    ConfigurationContext,
    ConfigurationDraftCreate,
    ConfigurationLifecycle,
    ConfigurationPublishRequest,
    ConfigurationRecord,
    ConfigurationResetRequest,
    ConfigurationRollbackRequest,
    ConfigurationScope,
    ConfigurationSpec,
    EffectiveConfiguration,
    SCOPE_PRECEDENCE,
    feature_target_matches,
)
from codex_web.storage.configuration_registry import ConfigurationRegistryStore


class ConfigurationError(ValueError):
    pass


class ConfigurationNotFoundError(LookupError):
    pass


class ConfigurationConflictError(RuntimeError):
    pass


class ConfigurationSpecRegistry:
    """Code-owned typed configuration schema registry."""

    def __init__(self) -> None:
        self._specs: dict[str, ConfigurationSpec] = {}

    def register(self, spec: ConfigurationSpec) -> None:
        if spec.key in self._specs and self._specs[spec.key] != spec:
            raise ConfigurationConflictError(f"configuration spec already registered: {spec.key}")
        self._specs[spec.key] = spec

    def get(self, key: str) -> ConfigurationSpec:
        try:
            return self._specs[key]
        except KeyError as exc:
            raise ConfigurationNotFoundError(f"unknown configuration key: {key}") from exc

    def list(self) -> list[ConfigurationSpec]:
        return [self._specs[key] for key in sorted(self._specs)]


class ConfigurationService:
    """Canonical typed configuration lifecycle and deterministic resolver."""

    def __init__(
        self,
        store: ConfigurationRegistryStore,
        specs: ConfigurationSpecRegistry | None = None,
    ) -> None:
        self.store = store
        self.specs = specs or ConfigurationSpecRegistry()

    @staticmethod
    def _scope_key(
        key: str,
        scope_type: ConfigurationScope,
        scope_id: str | None,
    ) -> tuple[str, ConfigurationScope, str | None]:
        normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else None
        if scope_type in {ConfigurationScope.DEPLOYMENT, ConfigurationScope.GLOBAL}:
            normalized_scope_id = None
        return key, scope_type, normalized_scope_id

    @staticmethod
    def _same_scope(
        record: ConfigurationRecord,
        key: str,
        scope_type: ConfigurationScope,
        scope_id: str | None,
    ) -> bool:
        return (
            record.key,
            record.scope_type,
            record.scope_id,
        ) == ConfigurationService._scope_key(key, scope_type, scope_id)

    def register_spec(self, spec: ConfigurationSpec) -> ConfigurationSpec:
        self.specs.register(spec)
        return spec

    def list_specs(self) -> list[ConfigurationSpec]:
        return self.specs.list()

    def list_records(
        self,
        *,
        key: str | None = None,
        scope_type: ConfigurationScope | None = None,
        scope_id: str | None = None,
    ) -> list[ConfigurationRecord]:
        records = self.store.load()
        if key is not None:
            records = [record for record in records if record.key == key]
        if scope_type is not None:
            records = [record for record in records if record.scope_type == scope_type]
            if scope_type not in {ConfigurationScope.DEPLOYMENT, ConfigurationScope.GLOBAL}:
                records = [record for record in records if record.scope_id == scope_id]
        return sorted(
            records,
            key=lambda record: (
                record.key,
                SCOPE_PRECEDENCE[record.scope_type],
                record.scope_id or "",
                record.revision,
            ),
        )

    def get_record(self, record_id: str) -> ConfigurationRecord:
        for record in self.store.load():
            if record.id == record_id:
                return record
        raise ConfigurationNotFoundError(f"configuration record not found: {record_id}")

    def _validate_record_against_spec(
        self,
        record: ConfigurationRecord,
    ) -> ConfigurationRecord:
        spec = self.specs.get(record.key)
        if record.scope_type not in spec.allowed_scopes:
            raise ConfigurationError(
                f"{record.key} is not allowed at {record.scope_type.value} scope"
            )
        normalized = spec.validate_value(record.value)
        if normalized != record.value:
            record = record.model_copy(update={"value": normalized})
        if record.feature_targeting is not None and not spec.feature_flag:
            raise ConfigurationError("feature targeting requires a feature-flag spec")
        if record.force_disabled:
            if not spec.kill_switch_capable:
                raise ConfigurationError("force_disabled requires a kill-switch-capable spec")
            if record.feature_targeting is not None:
                raise ConfigurationError("kill switches cannot be cohort/percentage targeted")
        return record

    def create_draft(self, payload: ConfigurationDraftCreate) -> ConfigurationRecord:
        spec = self.specs.get(payload.key)
        if not spec.editable:
            raise ConfigurationError(
                f"{payload.key} is read-only and cannot be changed through configuration"
            )
        if payload.scope_type not in spec.allowed_scopes:
            raise ConfigurationError(
                f"{payload.key} is not allowed at {payload.scope_type.value} scope"
            )
        normalized_value = spec.validate_value(payload.value)
        if payload.feature_targeting is not None and not spec.feature_flag:
            raise ConfigurationError("feature targeting requires a feature-flag spec")
        if payload.force_disabled:
            if not spec.kill_switch_capable:
                raise ConfigurationError("force_disabled requires a kill-switch-capable spec")
            if normalized_value is not False:
                raise ConfigurationError("force_disabled requires value=false")
            if payload.feature_targeting is not None:
                raise ConfigurationError("kill switches cannot be cohort/percentage targeted")

        created: list[ConfigurationRecord] = []

        def update(records: list[ConfigurationRecord]) -> list[ConfigurationRecord]:
            revisions = [
                record.revision
                for record in records
                if self._same_scope(
                    record,
                    payload.key,
                    payload.scope_type,
                    payload.scope_id,
                )
            ]
            record = ConfigurationRecord(
                key=payload.key,
                scope_type=payload.scope_type,
                scope_id=payload.scope_id,
                revision=max(revisions, default=0) + 1,
                value=normalized_value,
                feature_targeting=payload.feature_targeting,
                force_disabled=payload.force_disabled,
                created_by=payload.actor,
                create_reason=payload.reason,
            )
            created.append(record)
            return [*records, record]

        self.store.update(update)
        return created[0]

    def validate_record(self, record_id: str) -> dict[str, Any]:
        record = self._validate_record_against_spec(self.get_record(record_id))
        return {
            "valid": True,
            "record": record.model_dump(mode="json"),
            "spec": self.specs.get(record.key).model_dump(mode="json"),
        }

    def _active_for_scope(
        self,
        records: list[ConfigurationRecord],
        record: ConfigurationRecord,
    ) -> ConfigurationRecord | None:
        active = [
            candidate
            for candidate in records
            if candidate.state == ConfigurationLifecycle.PUBLISHED
            and self._same_scope(
                candidate,
                record.key,
                record.scope_type,
                record.scope_id,
            )
        ]
        return max(active, key=lambda candidate: candidate.revision, default=None)

    def publish(
        self,
        record_id: str,
        request: ConfigurationPublishRequest,
    ) -> ConfigurationRecord:
        published: list[ConfigurationRecord] = []

        def update(records: list[ConfigurationRecord]) -> list[ConfigurationRecord]:
            selected = next((record for record in records if record.id == record_id), None)
            if selected is None:
                raise ConfigurationNotFoundError(
                    f"configuration record not found: {record_id}"
                )
            if selected.state != ConfigurationLifecycle.DRAFT:
                raise ConfigurationConflictError("only draft configuration can be published")
            if not self.specs.get(selected.key).editable:
                raise ConfigurationError(
                    f"{selected.key} is read-only and cannot be published through configuration"
                )
            selected = self._validate_record_against_spec(selected)
            active = self._active_for_scope(records, selected)
            active_revision = active.revision if active is not None else None
            if (
                request.expected_active_revision is not None
                and request.expected_active_revision != active_revision
            ):
                raise ConfigurationConflictError(
                    "active configuration revision changed before publication"
                )
            now = time.time()
            published_record = selected.model_copy(
                update={
                    "state": ConfigurationLifecycle.PUBLISHED,
                    "published_by": request.actor,
                    "publish_reason": request.reason,
                    "published_at": now,
                    "supersedes_id": active.id if active is not None else None,
                }
            )
            result: list[ConfigurationRecord] = []
            for record in records:
                if active is not None and record.id == active.id:
                    result.append(
                        record.model_copy(
                            update={
                                "state": ConfigurationLifecycle.SUPERSEDED,
                                "superseded_by_id": published_record.id,
                            }
                        )
                    )
                elif record.id == selected.id:
                    result.append(published_record)
                else:
                    result.append(record)
            published.append(published_record)
            return result

        self.store.update(update)
        return published[0]

    def rollback(self, request: ConfigurationRollbackRequest) -> ConfigurationRecord:
        target = next(
            (
                record
                for record in self.store.load()
                if record.key == request.key
                and record.scope_type == request.scope_type
                and record.scope_id
                == self._scope_key(
                    request.key,
                    request.scope_type,
                    request.scope_id,
                )[2]
                and record.revision == request.target_revision
            ),
            None,
        )
        if target is None:
            raise ConfigurationNotFoundError("rollback target revision not found")

        draft = self.create_draft(
            ConfigurationDraftCreate(
                key=target.key,
                scope_type=target.scope_type,
                scope_id=target.scope_id,
                value=target.value,
                actor=request.actor,
                reason=request.reason or f"rollback to revision {target.revision}",
                feature_targeting=target.feature_targeting,
                force_disabled=target.force_disabled,
            )
        )

        def mark_rollback(records: list[ConfigurationRecord]) -> list[ConfigurationRecord]:
            return [
                record.model_copy(update={"rollback_of_id": target.id})
                if record.id == draft.id
                else record
                for record in records
            ]

        self.store.update(mark_rollback)
        return self.publish(
            draft.id,
            ConfigurationPublishRequest(
                actor=request.actor,
                reason=request.reason or f"rollback to revision {target.revision}",
                expected_active_revision=request.expected_active_revision,
            ),
        )

    def reset_override(self, request: ConfigurationResetRequest) -> ConfigurationRecord:
        """Supersede the explicit value at one scope so resolution can inherit."""

        spec = self.specs.get(request.key)
        if not spec.editable:
            raise ConfigurationError(
                f"{request.key} is read-only and cannot be reset through configuration"
            )
        reset_records: list[ConfigurationRecord] = []
        slot = self._scope_key(request.key, request.scope_type, request.scope_id)

        def update(records: list[ConfigurationRecord]) -> list[ConfigurationRecord]:
            matching = [
                record
                for record in records
                if self._same_scope(
                    record,
                    request.key,
                    request.scope_type,
                    request.scope_id,
                )
            ]
            active = next(
                (
                    record
                    for record in matching
                    if record.state == ConfigurationLifecycle.PUBLISHED
                ),
                None,
            )
            if active is None:
                raise ConfigurationConflictError(
                    "configuration scope has no published override to reset"
                )
            if (
                request.expected_active_revision is not None
                and request.expected_active_revision != active.revision
            ):
                raise ConfigurationConflictError(
                    "active configuration revision changed before reset"
                )

            now = time.time()
            tombstone = ConfigurationRecord(
                key=active.key,
                scope_type=active.scope_type,
                scope_id=active.scope_id,
                revision=max(record.revision for record in matching) + 1,
                state=ConfigurationLifecycle.DISABLED,
                value=active.value,
                created_by=request.actor,
                create_reason=request.reason or "revert explicit override",
                created_at=now,
                published_by=request.actor,
                publish_reason=request.reason or "revert explicit override",
                published_at=now,
                supersedes_id=active.id,
            )
            result: list[ConfigurationRecord] = []
            for record in records:
                if record.id == active.id:
                    result.append(
                        record.model_copy(
                            update={
                                "state": ConfigurationLifecycle.SUPERSEDED,
                                "superseded_by_id": tombstone.id,
                            }
                        )
                    )
                else:
                    result.append(record)
            result.append(tombstone)
            reset_records.append(tombstone)
            return result

        self.store.update(update)
        return reset_records[0]

    @staticmethod
    def _record_matches_context(
        record: ConfigurationRecord,
        context: ConfigurationContext,
    ) -> bool:
        scope_ids = context.scope_ids()
        expected = scope_ids[record.scope_type]
        if record.scope_type in {ConfigurationScope.DEPLOYMENT, ConfigurationScope.GLOBAL}:
            return True
        return bool(expected and expected == record.scope_id)

    def resolve(
        self,
        key: str,
        context: ConfigurationContext | None = None,
        *,
        now: float | None = None,
    ) -> EffectiveConfiguration:
        spec = self.specs.get(key)
        context = context or ConfigurationContext()
        published = [
            record
            for record in self.store.load()
            if record.key == key
            and record.state == ConfigurationLifecycle.PUBLISHED
            and self._record_matches_context(record, context)
        ]

        if spec.kill_switch_capable:
            kills = [
                record
                for record in published
                if record.force_disabled
                and feature_target_matches(record, context, now=now)
            ]
            if kills:
                selected = max(
                    kills,
                    key=lambda record: (
                        SCOPE_PRECEDENCE[record.scope_type],
                        record.revision,
                    ),
                )
                return EffectiveConfiguration(
                    key=key,
                    value=False,
                    source="published",
                    record_id=selected.id,
                    revision=selected.revision,
                    scope_type=selected.scope_type,
                    scope_id=selected.scope_id,
                    reason="kill_switch",
                    hot_reloadable=spec.hot_reloadable,
                    startup_only=spec.startup_only,
                    feature_flag=spec.feature_flag,
                    schema_version=selected.schema_version,
                    published_by=selected.published_by,
                    published_at=selected.published_at,
                    publish_reason=selected.publish_reason,
                )

        candidates = [
            record
            for record in published
            if feature_target_matches(record, context, now=now)
        ]
        if candidates:
            selected = max(
                candidates,
                key=lambda record: (
                    SCOPE_PRECEDENCE[record.scope_type],
                    record.revision,
                ),
            )
            return EffectiveConfiguration(
                key=key,
                value=selected.value,
                source="published",
                record_id=selected.id,
                revision=selected.revision,
                scope_type=selected.scope_type,
                scope_id=selected.scope_id,
                reason="scope_precedence",
                hot_reloadable=spec.hot_reloadable,
                startup_only=spec.startup_only,
                feature_flag=spec.feature_flag,
                schema_version=selected.schema_version,
                published_by=selected.published_by,
                published_at=selected.published_at,
                publish_reason=selected.publish_reason,
            )

        if spec.default is None and spec.required:
            raise ConfigurationError(f"required configuration is unset: {key}")
        return EffectiveConfiguration(
            key=key,
            value=spec.validate_value(spec.default) if spec.default is not None else None,
            source="default",
            reason="default" if spec.default is not None else "unset",
            hot_reloadable=spec.hot_reloadable,
            startup_only=spec.startup_only,
            feature_flag=spec.feature_flag,
        )

    def impact(self, record_id: str) -> dict[str, Any]:
        record = self.get_record(record_id)
        more_specific = [
            candidate
            for candidate in self.store.load()
            if candidate.key == record.key
            and candidate.state == ConfigurationLifecycle.PUBLISHED
            and SCOPE_PRECEDENCE[candidate.scope_type]
            > SCOPE_PRECEDENCE[record.scope_type]
        ]
        return {
            "record_id": record.id,
            "key": record.key,
            "scope": {
                "type": record.scope_type.value,
                "id": record.scope_id,
            },
            "startup_only": self.specs.get(record.key).startup_only,
            "hot_reloadable": self.specs.get(record.key).hot_reloadable,
            "more_specific_overrides": [
                {
                    "record_id": item.id,
                    "scope_type": item.scope_type.value,
                    "scope_id": item.scope_id,
                    "revision": item.revision,
                }
                for item in sorted(
                    more_specific,
                    key=lambda item: (
                        SCOPE_PRECEDENCE[item.scope_type],
                        item.scope_id or "",
                        item.revision,
                    ),
                )
            ],
        }
