from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer


EXECUTION_ROLE_CATALOG_KIND = "execution-role-catalog"
EXECUTION_ROLE_CATALOG_ID = "execution-roles.default"
EXECUTION_ROLE_CATALOG_SCHEMA_VERSION = "1.0"


class MRCIReadinessQualificationPolicy(BaseModel):
    """Explicit workflow execution target, not a runtime grant or fallback."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    execution_target_kind: Literal["canonical-native-assignment", "external-ci-job"]


class MRReviewQualificationPolicy(BaseModel):
    """Semantic review qualification only; never an authority/independence grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    basis: Literal["principal", "assignment_role"] = "principal"
    implementation_role_ids: tuple[str, ...] = ()
    validation_role_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def separated_roles(self):
        if self.basis == "assignment_role" and (
                not self.implementation_role_ids or not self.validation_role_ids
                or set(self.implementation_role_ids) & set(self.validation_role_ids)):
            raise ValueError("assignment review requires explicit disjoint implementation/validation Roles")
        for values in (self.implementation_role_ids, self.validation_role_ids):
            if len(values) != len(set(values)) or any(not value.strip() for value in values):
                raise ValueError("review Role ids must be nonempty and unique")
        return self


class ExecutionRoleContract(BaseModel):
    """Code-owned schema for one mutable execution-role definition."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    description: str = Field(min_length=1)
    lifecycle: Literal["active", "deprecated", "disabled"] = "active"
    expected_work: tuple[str, ...]
    must_refuse: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    hands_to: tuple[str, ...]
    failure_conditions: tuple[str, ...]
    keywords: tuple[str, ...] = ()
    auto_select: bool = True
    execution_profile_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )

    @model_validator(mode="after")
    def normalize_lists(self) -> "ExecutionRoleContract":
        for field_name in (
            "expected_work",
            "must_refuse",
            "required_artifacts",
            "hands_to",
            "failure_conditions",
            "keywords",
        ):
            values = getattr(self, field_name)
            if any(not value.strip() for value in values):
                raise ValueError(f"{field_name} cannot contain empty values")
        if self.execution_profile_id is None:
            object.__setattr__(
                self,
                "execution_profile_id",
                (
                    "orchestration-only"
                    if self.lane.strip().casefold() == "coordination"
                    else "repository-write"
                ),
            )
        return self

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "lane": self.lane,
            "description": self.description,
            "lifecycle": self.lifecycle,
            "expectedWork": list(self.expected_work),
            "mustRefuse": list(self.must_refuse),
            "requiredArtifacts": list(self.required_artifacts),
            "handsTo": list(self.hands_to),
            "failureConditions": list(self.failure_conditions),
            "keywords": list(self.keywords),
            "autoSelect": self.auto_select,
            "executionProfileId": self.execution_profile_id,
        }


class ExecutionRoleCatalogDefinition(BaseModel):
    """Validated data payload for the execution-role catalog definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mr_ci_readiness_policy: MRCIReadinessQualificationPolicy | None = None
    mr_review_policy: MRReviewQualificationPolicy | None = None
    change_classifications: tuple[str, ...]
    shared_execution_rules: tuple[str, ...]
    roles: tuple[ExecutionRoleContract, ...]
    executive_default_execution_role: dict[str, str]
    owner_to_execution_role: dict[str, str]

    @model_serializer(mode="wrap")
    def preserve_legacy_policy_omission(self, handler):
        value = handler(self)
        for name in ("mr_review_policy", "mr_ci_readiness_policy"):
            if name not in self.model_fields_set and getattr(self, name) is None:
                value.pop(name, None)
        return value

    @model_validator(mode="after")
    def validate_catalog(self) -> "ExecutionRoleCatalogDefinition":
        role_ids = [role.id for role in self.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("execution role ids must be unique")
        required = {"orchestrator", "quinn", "release-manager"}
        missing = required - set(role_ids)
        if missing:
            raise ValueError(
                "execution role catalog missing required structural roles: "
                + ", ".join(sorted(missing))
            )
        targets = set(self.executive_default_execution_role.values()) | set(
            self.owner_to_execution_role.values()
        )
        unknown = targets - set(role_ids)
        if unknown:
            raise ValueError(
                "execution role mappings reference unknown roles: "
                + ", ".join(sorted(unknown))
            )
        disabled_targets = {
            role.id
            for role in self.roles
            if role.lifecycle == "disabled" and role.id in targets
        }
        if disabled_targets:
            raise ValueError(
                "execution role mappings reference disabled roles: "
                + ", ".join(sorted(disabled_targets))
            )
        structural_disabled = {
            role.id
            for role in self.roles
            if role.id in required and role.lifecycle == "disabled"
        }
        if structural_disabled:
            raise ValueError(
                "required structural execution roles cannot be disabled: "
                + ", ".join(sorted(structural_disabled))
            )
        if any(not value.strip() for value in self.change_classifications):
            raise ValueError("change classifications cannot contain empty values")
        if any(not value.strip() for value in self.shared_execution_rules):
            raise ValueError("shared execution rules cannot contain empty values")
        if self.mr_review_policy is not None:
            review_ids = set(self.mr_review_policy.implementation_role_ids + self.mr_review_policy.validation_role_ids)
            if review_ids - set(role_ids) or any(role.lifecycle != "active" for role in self.roles if role.id in review_ids):
                raise ValueError("review policy requires current active catalog Roles")
        return self

    @property
    def role_map(self) -> dict[str, ExecutionRoleContract]:
        return {role.id: role for role in self.roles}


def validate_execution_role_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    """Definition Registry validator/normalizer for execution-role catalogs."""

    if {"mr_review_policy", "mr_ci_readiness_policy"} & set(payload):
        raise ValueError("MR review qualification requires execution-role-catalog schema 1.1")
    return _normalize_execution_role_catalog(payload)


def validate_execution_role_catalog_v11(payload: dict[str, Any]) -> dict[str, Any]:
    return _normalize_execution_role_catalog(payload)


def _normalize_execution_role_catalog(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = ExecutionRoleCatalogDefinition.model_validate(payload).model_dump(
        mode="json"
    )
    if "mr_review_policy" not in payload:
        normalized.pop("mr_review_policy", None)
    if "mr_ci_readiness_policy" not in payload:
        normalized.pop("mr_ci_readiness_policy", None)
    original_roles = {
        str(item.get("id")): item
        for item in payload.get("roles", [])
        if isinstance(item, dict)
    }
    for role in normalized["roles"]:
        original = original_roles.get(role["id"], {})
        if "lifecycle" not in original:
            role.pop("lifecycle", None)
        if "execution_profile_id" not in original:
            role.pop("execution_profile_id", None)
    return normalized
