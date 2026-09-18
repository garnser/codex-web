"""Reference TaskSource extension payload.

This sample demonstrates the provider-neutral TaskSource contract. The current
local package catalog treats payload.cwext as opaque; executable loading belongs
to the isolated extension execution boundary rather than this example.
"""

from codex_web.compatibility import TASK_SOURCE_CONTRACT
from codex_web.models import TaskSourceIdentity, WorkItemStage
from codex_web.services.task_sources import (
    TaskSourceCapabilities,
    TaskSourceCapability,
    TaskSourceCanonicalProjection,
    TaskSourceEvent,
    TaskSourceSnapshot,
)


class ReferenceTaskSource:
    source_type = "reference"
    source_instance = "reference://local"
    contract_version = TASK_SOURCE_CONTRACT.current
    capabilities = TaskSourceCapabilities(
        frozenset({TaskSourceCapability.DISCOVERY, TaskSourceCapability.READ})
    )

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        return []

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        return TaskSourceSnapshot(identity=identity)

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        return None

    def project(
        self,
        snapshot: TaskSourceSnapshot,
        *,
        current_stage: WorkItemStage | None = None,
    ) -> TaskSourceCanonicalProjection:
        return TaskSourceCanonicalProjection(
            identity=snapshot.identity,
            stage=current_stage,
            source_state=snapshot.source_state,
        )

    async def write_owner(self, identity: TaskSourceIdentity, owner: str | None) -> TaskSourceSnapshot:
        raise NotImplementedError("reference source is read-only")

    async def write_state(self, identity: TaskSourceIdentity, state: str) -> TaskSourceSnapshot:
        raise NotImplementedError("reference source is read-only")

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        raise NotImplementedError("reference source is read-only")

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        raise NotImplementedError("reference source is read-only")


def create() -> ReferenceTaskSource:
    return ReferenceTaskSource()
