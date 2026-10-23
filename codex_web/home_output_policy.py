"""Definition data for explicit Home command-output release, never shell inference."""
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

HOME_OUTPUT_POLICY_KIND = 'home-host-output-release'
HOME_OUTPUT_POLICY_VERSION = '1.0'


class HomeOutputCommandRule(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    exit_codes: tuple[StrictInt, ...] = Field(default=(0,), min_length=1, max_length=16)
    streams: tuple[Literal['stdout', 'stderr'], ...] = Field(default=('stdout', 'stderr'), min_length=1, max_length=2)

    @model_validator(mode='after')
    def exact_command(self):
        if (not Path(self.argv[0]).is_absolute() or any(not s or '\x00' in s or len(s) > 8192 for s in self.argv)
                or sum(len(s.encode()) for s in self.argv) > 32768
                or any(type(code) is not int or not 0 <= code <= 255 for code in self.exit_codes)
                or len(set(self.exit_codes)) != len(self.exit_codes)
                or len(set(self.streams)) != len(self.streams)):
            raise ValueError('Output rules require bounded exact argv, exit codes and streams')
        return self


class HomeOutputReleasePolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    organization_id: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(min_length=1, max_length=128)
    administrator_id: str = Field(min_length=1, max_length=128)
    agent_profile_id: str = Field(min_length=1, max_length=128)
    agent_profile_revision: int = Field(ge=1, strict=True)
    host_resource_id: str = Field(min_length=1, max_length=128)
    trusted_worker_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    max_excerpt_bytes: int = Field(default=16384, ge=1, le=65536, strict=True)
    classification: Literal['internal', 'confidential'] = 'confidential'
    commands: tuple[HomeOutputCommandRule, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def distinct(self):
        if (any(not value.strip() or value != value.strip() for value in self.trusted_worker_ids)
                or len(set(self.trusted_worker_ids)) != len(self.trusted_worker_ids)
                or len({rule.argv for rule in self.commands}) != len(self.commands)):
            raise ValueError('Output policy requires distinct workers and exact commands')
        return self


def validate_home_output_policy(payload):
    return HomeOutputReleasePolicy.model_validate(payload).model_dump(mode='json')
