from __future__ import annotations

import hashlib
import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec


CONFIGURATION_CONTRACT = ContractSpec("configuration-record", "1.0", ("1.0",))


class ConfigurationScope(StrEnum):
    DEPLOYMENT = "deployment"
    GLOBAL = "global"
    ORGANIZATION = "organization"
    WORKSPACE = "workspace"
    PROJECT = "project"
    RESOURCE = "resource"


SCOPE_PRECEDENCE: dict[ConfigurationScope, int] = {
    ConfigurationScope.DEPLOYMENT: 0,
    ConfigurationScope.GLOBAL: 1,
    ConfigurationScope.ORGANIZATION: 2,
    ConfigurationScope.WORKSPACE: 3,
    ConfigurationScope.PROJECT: 4,
    ConfigurationScope.RESOURCE: 5,
}


class ConfigurationLifecycle(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    DISABLED = "disabled"


class ConfigurationValueKind(StrEnum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    STRING_LIST = "string_list"
    SECRET_REF = "secret_ref"
    DEFINITION_REF = "definition_ref"


class SecretReference(BaseModel):
    """Reference-only secret value. Raw secret material is never configuration."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["secret"] = "secret"
    secret_id: str = Field(min_length=1)


class DefinitionReference(BaseModel):
    """Stable Definition Registry reference without copying mutable definition data."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["definition"] = "definition"
    definition_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class FeatureTargeting(BaseModel):
    """Deterministic feature targeting attached to a published value."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    percentage: float = Field(default=100.0, ge=0.0, le=100.0)
    cohorts: list[str] = Field(default_factory=list)
    expires_at: float | None = None
    owner: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "FeatureTargeting":
        self.cohorts = sorted({value.strip() for value in self.cohorts if value.strip()})
        if self.owner is not None:
            self.owner = self.owner.strip() or None
        return self


class ConfigurationSpec(BaseModel):
    """Code-owned schema for one configuration key.

    Specs define type/validation and hard invariants. Mutable values are stored
    separately. Configuration is never an authorization grant.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    value_kind: ConfigurationValueKind
    default: Any = None
    required: bool = False
    allowed_scopes: list[ConfigurationScope] = Field(
        default_factory=lambda: list(ConfigurationScope)
    )
    description: str | None = None
    category: str = Field(default="Advanced", min_length=1, max_length=80)
    editable: bool = True
    sensitive: bool = False
    allowed_values: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    hot_reloadable: bool = True
    startup_only: bool = False
    feature_flag: bool = False
    kill_switch_capable: bool = False
    grants_authority: bool = False

    @model_validator(mode="after")
    def validate_invariants(self) -> "ConfigurationSpec":
        self.allowed_scopes = list(dict.fromkeys(self.allowed_scopes))
        if not self.allowed_scopes:
            raise ValueError("configuration spec must allow at least one scope")
        if self.startup_only and self.hot_reloadable:
            raise ValueError("startup-only configuration cannot be hot reloadable")
        if self.feature_flag and self.value_kind != ConfigurationValueKind.BOOLEAN:
            raise ValueError("feature flags must be boolean")
        if self.kill_switch_capable and not self.feature_flag:
            raise ValueError("kill switches are only valid for boolean feature flags")
        if self.grants_authority:
            raise ValueError("configuration cannot grant authority")
        self.category = self.category.strip()
        if self.value_kind == ConfigurationValueKind.SECRET_REF:
            self.sensitive = True
        if self.allowed_values and self.value_kind != ConfigurationValueKind.STRING:
            raise ValueError("allowed_values are supported only for string configuration")
        self.allowed_values = tuple(dict.fromkeys(self.allowed_values))
        if (
            (self.minimum is not None or self.maximum is not None)
            and self.value_kind
            not in {ConfigurationValueKind.INTEGER, ConfigurationValueKind.NUMBER}
        ):
            raise ValueError("minimum/maximum are supported only for numeric configuration")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("configuration minimum cannot exceed maximum")
        if self.default is None:
            if self.feature_flag:
                raise ValueError("feature flags require an explicit boolean default")
        else:
            self.default = self.validate_value(self.default)
        return self

    def validate_value(self, value: Any) -> Any:
        kind = self.value_kind
        if kind == ConfigurationValueKind.BOOLEAN:
            if type(value) is not bool:
                raise ValueError(f"{self.key} requires a boolean")
            return value
        if kind == ConfigurationValueKind.INTEGER:
            if type(value) is not int:
                raise ValueError(f"{self.key} requires an integer")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"{self.key} must be at least {self.minimum:g}")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"{self.key} must be at most {self.maximum:g}")
            return value
        if kind == ConfigurationValueKind.NUMBER:
            if type(value) not in {int, float}:
                raise ValueError(f"{self.key} requires a number")
            normalized = float(value)
            if self.minimum is not None and normalized < self.minimum:
                raise ValueError(f"{self.key} must be at least {self.minimum:g}")
            if self.maximum is not None and normalized > self.maximum:
                raise ValueError(f"{self.key} must be at most {self.maximum:g}")
            return normalized
        if kind == ConfigurationValueKind.STRING:
            if not isinstance(value, str):
                raise ValueError(f"{self.key} requires a string")
            if self.allowed_values and value not in self.allowed_values:
                raise ValueError(
                    f"{self.key} must be one of: {', '.join(self.allowed_values)}"
                )
            return value
        if kind == ConfigurationValueKind.STRING_LIST:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{self.key} requires a list of strings")
            return list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if kind == ConfigurationValueKind.SECRET_REF:
            return SecretReference.model_validate(value).model_dump(mode="json")
        if kind == ConfigurationValueKind.DEFINITION_REF:
            return DefinitionReference.model_validate(value).model_dump(mode="json")
        raise ValueError(f"unsupported configuration value kind: {kind}")


class ConfigurationRecord(BaseModel):
    """Immutable-revision configuration value with lifecycle/provenance."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = CONFIGURATION_CONTRACT.current
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    key: str = Field(min_length=1)
    scope_type: ConfigurationScope
    scope_id: str | None = None
    revision: int = Field(ge=1)
    state: ConfigurationLifecycle = ConfigurationLifecycle.DRAFT
    value: Any
    feature_targeting: FeatureTargeting | None = None
    force_disabled: bool = False
    created_by: str = Field(min_length=1)
    create_reason: str | None = None
    created_at: float = Field(default_factory=time.time)
    published_by: str | None = None
    publish_reason: str | None = None
    published_at: float | None = None
    supersedes_id: str | None = None
    superseded_by_id: str | None = None
    rollback_of_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "ConfigurationRecord":
        CONFIGURATION_CONTRACT.require(self.schema_version)
        if self.scope_type in {ConfigurationScope.DEPLOYMENT, ConfigurationScope.GLOBAL}:
            if self.scope_id not in {None, ""}:
                raise ValueError(f"{self.scope_type.value} configuration cannot have scope_id")
            self.scope_id = None
        elif not self.scope_id:
            raise ValueError(f"{self.scope_type.value} configuration requires scope_id")
        if self.force_disabled and self.value is not False:
            raise ValueError("force_disabled records must have value=false")
        return self


class ConfigurationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    resource_id: str | None = None
    subject_id: str | None = None
    cohort: str | None = None

    def scope_ids(self) -> dict[ConfigurationScope, str | None]:
        return {
            ConfigurationScope.DEPLOYMENT: None,
            ConfigurationScope.GLOBAL: None,
            ConfigurationScope.ORGANIZATION: self.organization_id,
            ConfigurationScope.WORKSPACE: self.workspace_id,
            ConfigurationScope.PROJECT: self.project_id,
            ConfigurationScope.RESOURCE: self.resource_id,
        }


class EffectiveConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any
    source: Literal["default", "published"]
    record_id: str | None = None
    revision: int | None = None
    scope_type: ConfigurationScope | None = None
    scope_id: str | None = None
    reason: str
    hot_reloadable: bool
    startup_only: bool
    feature_flag: bool
    schema_version: str | None = None
    published_by: str | None = None
    published_at: float | None = None
    publish_reason: str | None = None


class ConfigurationDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1)
    scope_type: ConfigurationScope
    scope_id: str | None = None
    value: Any
    actor: str = Field(min_length=1)
    reason: str | None = None
    feature_targeting: FeatureTargeting | None = None
    force_disabled: bool = False


class ConfigurationPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    actor: str = Field(min_length=1)
    reason: str | None = None
    expected_active_revision: int | None = None


class ConfigurationRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1)
    scope_type: ConfigurationScope
    scope_id: str | None = None
    target_revision: int = Field(ge=1)
    actor: str = Field(min_length=1)
    reason: str | None = None
    expected_active_revision: int | None = None


class ConfigurationResetRequest(BaseModel):
    """Remove the explicit value at one exact scope slot.

    Reset preserves history by superseding the current published revision with
    a disabled tombstone. Resolution then falls through to the next applicable
    scope or code-owned default.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1)
    scope_type: ConfigurationScope
    scope_id: str | None = None
    actor: str = Field(min_length=1)
    reason: str | None = None
    expected_active_revision: int | None = None


def feature_target_matches(
    record: ConfigurationRecord,
    context: ConfigurationContext,
    *,
    now: float | None = None,
) -> bool:
    targeting = record.feature_targeting
    if targeting is None:
        return True
    current = time.time() if now is None else now
    if targeting.expires_at is not None and current >= targeting.expires_at:
        return False
    if targeting.cohorts and (context.cohort or "") not in targeting.cohorts:
        return False
    if targeting.percentage >= 100.0:
        return True
    if targeting.percentage <= 0.0 or not context.subject_id:
        return False
    digest = hashlib.sha256(
        f"{record.key}:{record.id}:{context.subject_id}".encode("utf-8")
    ).digest()
    bucket = int.from_bytes(digest[:4], "big") % 10_000
    return bucket < int(targeting.percentage * 100)
