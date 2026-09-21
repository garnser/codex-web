from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from codex_web.canonical_materialization import (
    MaterializationDisposition,
)
from codex_web.identity import (
    DEFAULT_HUMAN_IDENTITY_ID,
    TenantScope,
)
from codex_web.project_bootstrap import (
    PROJECT_BOOTSTRAP_API_VERSION,
    PROJECT_BOOTSTRAP_CONTRACT_VERSION,
    ProjectBootstrapManifest,
    ProjectBootstrapManifestError,
    dump_project_bootstrap_yaml,
    load_project_bootstrap_manifest,
    resolve_bootstrap_repository_paths,
    scaffold_project_bootstrap_manifest,
)
from codex_web.workspaces import WorkspaceMapper


EXIT_READY = 0
EXIT_INVALID = 2
EXIT_WARNINGS = 10
EXIT_BLOCKED = 20
EXIT_APPLY_FAILED = 30


class ProjectBootstrapBlocked(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-web",
        description="Codex Web operator command line",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser(
        "bootstrap",
        help="validate, scaffold, or apply a ProjectBootstrap manifest",
    )
    bootstrap.add_argument("--project", required=True, help="Project ID")
    bootstrap.add_argument("--manifest", required=True, help="ProjectBootstrap YAML path")
    mode = bootstrap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="validate without mutation")
    mode.add_argument("--apply", action="store_true", help="apply supported bootstrap operations")
    mode.add_argument("--scaffold", action="store_true", help="write a minimal starter manifest")
    bootstrap.add_argument(
        "--migrate-legacy",
        action="store_true",
        help="allow canonical legacy-state materialization during apply",
    )
    bootstrap.add_argument(
        "--output",
        choices=("human", "json"),
        default="human",
        help="output format",
    )
    bootstrap.add_argument(
        "--repository",
        help="repository path used by --scaffold",
    )
    bootstrap.add_argument("--repository-id", default="repository")
    bootstrap.add_argument("--project-name")
    bootstrap.add_argument("--organization", default="local")
    bootstrap.add_argument("--workspace", default="default")
    bootstrap.add_argument("--force", action="store_true", help="replace scaffold output")
    return parser


def _base_result(
    *,
    project_id: str,
    manifest: ProjectBootstrapManifest,
    paths: dict[str, Path],
    migrate_legacy: bool,
) -> dict[str, Any]:
    return {
        "contractVersion": PROJECT_BOOTSTRAP_CONTRACT_VERSION,
        "manifestApiVersion": manifest.api_version,
        "projectId": project_id,
        "manifestDigest": manifest.digest(),
        "sourceVersion": "legacy-or-canonical",
        "targetVersion": PROJECT_BOOTSTRAP_API_VERSION,
        "migrateLegacy": migrate_legacy,
        "repositories": [
            {
                "id": repository.id,
                "path": str(paths[repository.id]),
                "default": repository.default,
            }
            for repository in manifest.repositories
        ],
    }


def _human(value: dict[str, Any]) -> str:
    lines = [
        f"ProjectBootstrap {value.get('manifestApiVersion', PROJECT_BOOTSTRAP_API_VERSION)} "
        f"for Project {value.get('projectId', 'unknown')}",
        f"status: {value.get('status', 'unknown')}",
    ]
    if value.get("bootstrapExecutionId"):
        lines.append(f"execution: {value['bootstrapExecutionId']}")
    if value.get("materializationPlanId"):
        lines.append(f"materialization plan: {value['materializationPlanId']}")
    counts = value.get("counts")
    if isinstance(counts, dict):
        lines.append(
            "materialization: "
            + ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
        )
    blockers = value.get("blockers") or []
    for blocker in blockers:
        if isinstance(blocker, dict):
            lines.append(
                f"- [{blocker.get('code', 'blocked')}] "
                f"{blocker.get('message', 'bootstrap blocked')}"
            )
    warnings = value.get("warnings") or []
    for warning in warnings:
        lines.append(f"- [warning] {warning}")
    if value.get("message"):
        lines.append(str(value["message"]))
    return "\n".join(lines)


def _emit(value: dict[str, Any], output: str) -> None:
    if output == "json":
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    else:
        print(_human(value))


def _actor_for_manifest(application: Any, manifest: ProjectBootstrapManifest):
    scope = TenantScope(
        organization_id=manifest.project.organization,
        workspace_id=manifest.project.workspace,
    )
    try:
        return application.identity_service.actor_for_identity(
            DEFAULT_HUMAN_IDENTITY_ID,
            scope=scope,
        )
    except Exception as exc:
        raise ProjectBootstrapBlocked(
            "bootstrap_actor_scope_unavailable",
            "the local administrator has no active membership in the manifest "
            "organization/workspace; create or select authorized tenant scope first",
        ) from exc


def _validate_apply_alignment(
    *,
    application: Any,
    project_id: str,
    manifest: ProjectBootstrapManifest,
    paths: dict[str, Path],
    plan: Any,
) -> None:
    project = next(
        (
            item
            for item in application.project_service.repository.load()
            if item.id == project_id
        ),
        None,
    )
    if project is None:
        raise ProjectBootstrapBlocked(
            "project_not_found",
            "the requested Project does not exist",
        )
    if project.name != manifest.project.name:
        raise ProjectBootstrapBlocked(
            "project_name_mismatch",
            "manifest Project name differs from canonical Project state; "
            "Project field reconciliation is handled by the bootstrap planner",
        )
    if project.sandbox != manifest.execution.sandbox:
        raise ProjectBootstrapBlocked(
            "sandbox_reconciliation_required",
            "manifest sandbox differs from canonical Project state; "
            "reconcile it before materialization",
        )

    manifest_paths = {path.resolve() for path in paths.values()}
    plan_paths = {
        Path(str(item.metadata["filesystem_path"])).resolve()
        for item in plan.operations
        if item.domain == "repository_resource"
        and item.metadata.get("filesystem_path")
    }
    if manifest_paths != plan_paths:
        raise ProjectBootstrapBlocked(
            "repository_topology_mismatch",
            "manifest repository topology differs from deterministic repository "
            "discovery; explicit reconciliation is required",
        )

    provider_ops = {
        item.domain
        for item in plan.operations
        if item.domain in {"secret_reference", "task_source"}
    }
    if provider_ops and manifest.task_source is None:
        raise ProjectBootstrapBlocked(
            "task_source_manifest_missing",
            "legacy TaskSource state exists but taskSource is absent from the manifest",
        )
    if manifest.task_source is not None:
        if manifest.task_source.type.casefold() != "gitlab":
            raise ProjectBootstrapBlocked(
                "task_source_materializer_unsupported",
                "the current canonical legacy materializer supports GitLab TaskSource "
                "conversion only",
            )
        current = project.authoritative_task_source
        if (
            current is not None
            and manifest.task_source.secret_ref
            and current.credential_secret_id
            and manifest.task_source.secret_ref != current.credential_secret_id
        ):
            raise ProjectBootstrapBlocked(
                "secret_reference_mismatch",
                "manifest secretRef differs from the canonical TaskSource credential "
                "reference; explicit reconciliation is required",
            )

    if manifest.integrations.slack is not None:
        raise ProjectBootstrapBlocked(
            "integration_reconciliation_required",
            "Slack desired-state reconciliation is handled by the bootstrap planner; "
            "the legacy materializer will not silently change it",
        )


def apply_project_bootstrap(
    *,
    project_id: str,
    manifest: ProjectBootstrapManifest,
    paths: dict[str, Path],
    migrate_legacy: bool,
) -> dict[str, Any]:
    if not migrate_legacy:
        raise ProjectBootstrapBlocked(
            "bootstrap_planner_required",
            "apply without --migrate-legacy requires the canonical reconciliation "
            "planner; use dry-run or request legacy materialization",
        )

    # Importing application composes mutable runtime state, so this path is
    # deliberately lazy and is never reached by --dry-run or --scaffold.
    from codex_web import application

    actor = _actor_for_manifest(application, manifest)
    service = application.canonical_materialization_service
    confirm_generic = (
        manifest.project.organization == "local"
        and manifest.project.workspace == "default"
    )
    plan = service.plan(
        project_id,
        actor=actor,
        confirm_generic_target=confirm_generic,
    )
    _validate_apply_alignment(
        application=application,
        project_id=project_id,
        manifest=manifest,
        paths=paths,
        plan=plan,
    )

    if plan.blockers:
        raise ProjectBootstrapBlocked(
            "canonical_materialization_blocked",
            "canonical materialization contains unresolved or operator-action-required "
            "records",
        )

    execution = service.apply(plan, actor=actor)
    return {
        "status": "ready",
        "bootstrapExecutionId": execution.id,
        "materializationPlanId": plan.id,
        "materializationVersion": execution.version,
        "counts": execution.counts(),
        "warnings": [],
        "blockers": [],
    }


def _scaffold(args: argparse.Namespace) -> int:
    if not args.repository:
        result = {
            "status": "invalid",
            "projectId": args.project,
            "message": "--repository is required with --scaffold",
        }
        _emit(result, args.output)
        return EXIT_INVALID
    target = Path(args.manifest)
    if target.exists() and not args.force:
        result = {
            "status": "invalid",
            "projectId": args.project,
            "message": "manifest already exists; use --force to replace it",
        }
        _emit(result, args.output)
        return EXIT_INVALID
    try:
        manifest = scaffold_project_bootstrap_manifest(
            project_name=args.project_name or args.project,
            organization=args.organization,
            workspace=args.workspace,
            repository_path=args.repository,
            repository_id=args.repository_id,
        )
        resolve_bootstrap_repository_paths(
            manifest,
            workspace_mapper=WorkspaceMapper.from_environment(),
        )
    except ProjectBootstrapManifestError as exc:
        _emit(
            {
                "status": "invalid",
                "projectId": args.project,
                "message": str(exc),
            },
            args.output,
        )
        return EXIT_INVALID
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        dump_project_bootstrap_yaml(manifest),
        encoding="utf-8",
    )
    _emit(
        {
            "status": "ready",
            "projectId": args.project,
            "manifestApiVersion": manifest.api_version,
            "manifestDigest": manifest.digest(),
            "message": f"wrote bootstrap manifest to {target}",
        },
        args.output,
    )
    return EXIT_READY


def _bootstrap(
    args: argparse.Namespace,
    *,
    apply_runner: Callable[..., dict[str, Any]] = apply_project_bootstrap,
) -> int:
    if args.scaffold:
        return _scaffold(args)
    try:
        manifest = load_project_bootstrap_manifest(args.manifest)
        paths = resolve_bootstrap_repository_paths(
            manifest,
            workspace_mapper=WorkspaceMapper.from_environment(),
        )
    except ProjectBootstrapManifestError as exc:
        _emit(
            {
                "status": "invalid",
                "projectId": args.project,
                "message": str(exc),
            },
            args.output,
        )
        return EXIT_INVALID

    result = _base_result(
        project_id=args.project,
        manifest=manifest,
        paths=paths,
        migrate_legacy=args.migrate_legacy,
    )
    if args.dry_run:
        result.update(
            {
                "status": "ready",
                "phase": "manifest-validation",
                "message": (
                    "manifest and approved workspace paths are valid; "
                    "no persistent state or provider action was performed"
                ),
                "warnings": [],
                "blockers": [],
            }
        )
        _emit(result, args.output)
        return EXIT_READY

    try:
        applied = apply_runner(
            project_id=args.project,
            manifest=manifest,
            paths=paths,
            migrate_legacy=args.migrate_legacy,
        )
    except ProjectBootstrapBlocked as exc:
        result.update(
            {
                "status": "blocked",
                "blockers": [
                    {
                        "code": exc.code,
                        "message": exc.message,
                    }
                ],
            }
        )
        _emit(result, args.output)
        return EXIT_BLOCKED
    except Exception as exc:
        # Do not interpolate exception text here: provider/backend failures can
        # contain credential-bearing request details.
        result.update(
            {
                "status": "apply-failed",
                "message": (
                    "bootstrap apply failed; inspect server-side audit/diagnostics "
                    f"for {type(exc).__name__}"
                ),
            }
        )
        _emit(result, args.output)
        return EXIT_APPLY_FAILED

    result.update(applied)
    _emit(result, args.output)
    return EXIT_WARNINGS if result.get("warnings") else EXIT_READY


def main(
    argv: list[str] | None = None,
    *,
    apply_runner: Callable[..., dict[str, Any]] = apply_project_bootstrap,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "bootstrap":
        return _bootstrap(args, apply_runner=apply_runner)
    return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
