import unittest

from pydantic import ValidationError

from codex_web.resources import (
    RepositoryExecutionScope,
    RepositoryExecutionTarget,
    RepositoryTargetSource,
    RepositoryWriteMode,
)


class RepositoryExecutionScopeTests(unittest.TestCase):
    def test_single_target_converts_without_changing_existing_semantics(self) -> None:
        target = RepositoryExecutionTarget(
            organization_id="org",
            workspace_id="workspace",
            project_id="project",
            mutable_repository_id="repo-a",
            read_only_repository_ids=("repo-b",),
            source=RepositoryTargetSource.EXPLICIT,
            source_ref="turn-1",
        )

        scope = RepositoryExecutionScope.from_target(target)

        self.assertEqual(scope.write_mode, RepositoryWriteMode.SINGLE)
        self.assertEqual(scope.writable_repository_ids, ("repo-a",))
        self.assertEqual(scope.read_only_repository_ids, ("repo-b",))
        self.assertEqual(scope.project_id, target.project_id)

    def test_coordinated_scope_requires_two_or_more_explicit_writable_repositories(self) -> None:
        scope = RepositoryExecutionScope(
            organization_id="org",
            workspace_id="workspace",
            project_id="project",
            writable_repository_ids=("repo-a", "repo-b", "repo-a"),
            read_only_repository_ids=("repo-c",),
            write_mode=RepositoryWriteMode.COORDINATED,
            source=RepositoryTargetSource.EXPLICIT,
            source_ref="work-item-42",
        )

        self.assertEqual(scope.writable_repository_ids, ("repo-a", "repo-b"))
        self.assertEqual(scope.read_only_repository_ids, ("repo-c",))

        with self.assertRaises(ValidationError):
            RepositoryExecutionScope(
                organization_id="org",
                workspace_id="workspace",
                project_id="project",
                writable_repository_ids=("repo-a",),
                write_mode=RepositoryWriteMode.COORDINATED,
                source=RepositoryTargetSource.EXPLICIT,
            )

    def test_default_single_mode_never_silently_allows_multiple_writable_repositories(self) -> None:
        with self.assertRaises(ValidationError):
            RepositoryExecutionScope(
                organization_id="org",
                workspace_id="workspace",
                project_id="project",
                writable_repository_ids=("repo-a", "repo-b"),
                source=RepositoryTargetSource.EXPLICIT,
            )

    def test_writable_and_read_only_sets_must_be_disjoint(self) -> None:
        with self.assertRaises(ValidationError):
            RepositoryExecutionScope(
                organization_id="org",
                workspace_id="workspace",
                project_id="project",
                writable_repository_ids=("repo-a", "repo-b"),
                read_only_repository_ids=("repo-b",),
                write_mode=RepositoryWriteMode.COORDINATED,
                source=RepositoryTargetSource.EXPLICIT,
            )

    def test_orchestration_only_scope_cannot_gain_write_authority(self) -> None:
        scope = RepositoryExecutionScope(
            organization_id="org",
            workspace_id="workspace",
            project_id="project",
            read_only_repository_ids=("repo-context",),
            source=RepositoryTargetSource.ORCHESTRATION_ONLY,
        )
        self.assertEqual(scope.writable_repository_ids, ())

        with self.assertRaises(ValidationError):
            RepositoryExecutionScope(
                organization_id="org",
                workspace_id="workspace",
                project_id="project",
                writable_repository_ids=("repo-a", "repo-b"),
                write_mode=RepositoryWriteMode.COORDINATED,
                source=RepositoryTargetSource.ORCHESTRATION_ONLY,
            )


if __name__ == "__main__":
    unittest.main()
