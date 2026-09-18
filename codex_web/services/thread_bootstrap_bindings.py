from __future__ import annotations

from codex_web.identity import AuthenticationActor
from codex_web.storage.thread_bootstrap_bindings import (
    ThreadBootstrapBindingStore,
)
from codex_web.thread_bootstrap import (
    ThreadBootstrapBinding,
    deterministic_thread_bootstrap_binding_id,
)


class ThreadBootstrapBindingError(RuntimeError):
    pass


class ThreadBootstrapBindingConflictError(ThreadBootstrapBindingError):
    pass


class ThreadBootstrapBindingNotFoundError(ThreadBootstrapBindingError):
    pass


class ThreadBootstrapBindingService:
    """Persist immutable tenant-scoped bootstrap -> Codex thread bindings."""

    def __init__(self, store: ThreadBootstrapBindingStore) -> None:
        self.store = store

    @staticmethod
    def _value(value: str, label: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ThreadBootstrapBindingError(f"{label} is required")
        return normalized

    @staticmethod
    def _scope_matches(
        binding: ThreadBootstrapBinding,
        actor: AuthenticationActor,
    ) -> bool:
        return (
            binding.organization_id == actor.organization_id
            and binding.workspace_id == actor.workspace_id
        )

    def bind(
        self,
        *,
        bootstrap_id: str,
        thread_id: str,
        execution_id: str,
        assignment_id: str,
        execution_workspace_id: str,
        actor: AuthenticationActor,
    ) -> ThreadBootstrapBinding:
        bootstrap_id = self._value(bootstrap_id, "bootstrap_id")
        thread_id = self._value(thread_id, "thread_id")
        execution_id = self._value(execution_id, "execution_id")
        assignment_id = self._value(assignment_id, "assignment_id")
        execution_workspace_id = self._value(
            execution_workspace_id,
            "execution_workspace_id",
        )
        candidate = ThreadBootstrapBinding(
            id=deterministic_thread_bootstrap_binding_id(
                actor.organization_id,
                actor.workspace_id,
                bootstrap_id,
            ),
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            bootstrap_id=bootstrap_id,
            thread_id=thread_id,
            execution_id=execution_id,
            assignment_id=assignment_id,
            execution_workspace_id=execution_workspace_id,
            created_by=actor.identity_id,
        )

        def apply(state):
            scoped = [
                item
                for item in state.bindings
                if self._scope_matches(item, actor)
            ]
            existing_bootstrap = next(
                (
                    item
                    for item in scoped
                    if item.bootstrap_id == bootstrap_id
                ),
                None,
            )
            if existing_bootstrap is not None:
                immutable_values = (
                    "thread_id",
                    "execution_id",
                    "assignment_id",
                    "execution_workspace_id",
                )
                if all(
                    getattr(existing_bootstrap, field)
                    == getattr(candidate, field)
                    for field in immutable_values
                ):
                    return state
                raise ThreadBootstrapBindingConflictError(
                    "thread bootstrap is already bound to different canonical execution state"
                )

            existing_thread = next(
                (
                    item
                    for item in scoped
                    if item.thread_id == thread_id
                ),
                None,
            )
            if existing_thread is not None:
                raise ThreadBootstrapBindingConflictError(
                    "Codex thread is already bound to a different thread bootstrap"
                )

            state.bindings.append(candidate)
            return state

        state = self.store.update(apply)
        return next(item for item in state.bindings if item.id == candidate.id)

    def get_by_bootstrap(
        self,
        bootstrap_id: str,
        actor: AuthenticationActor,
    ) -> ThreadBootstrapBinding:
        normalized = self._value(bootstrap_id, "bootstrap_id")
        for item in self.store.load().bindings:
            if (
                self._scope_matches(item, actor)
                and item.bootstrap_id == normalized
            ):
                return item
        raise ThreadBootstrapBindingNotFoundError(
            "thread bootstrap binding not found"
        )

    def get_by_thread(
        self,
        thread_id: str,
        actor: AuthenticationActor,
    ) -> ThreadBootstrapBinding:
        normalized = self._value(thread_id, "thread_id")
        for item in self.store.load().bindings:
            if (
                self._scope_matches(item, actor)
                and item.thread_id == normalized
            ):
                return item
        raise ThreadBootstrapBindingNotFoundError(
            "thread bootstrap binding not found"
        )

    def list(
        self,
        actor: AuthenticationActor,
    ) -> tuple[ThreadBootstrapBinding, ...]:
        return tuple(
            item
            for item in self.store.load().bindings
            if self._scope_matches(item, actor)
        )
