from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import (
    StateStoreMigrationResult,
    StateStoreMigrator,
    state_documents_checksum,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate codex-web canonical document state from SQLite to PostgreSQL"
    )
    parser.add_argument(
        "command",
        choices=("migrate", "verify", "rollback-target"),
    )
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--postgres-dsn", required=True)
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Migration result/rollback manifest JSON path",
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="SQLite backup path; required for migrate",
    )
    return parser


def _write_manifest(
    path: Path,
    result: StateStoreMigrationResult,
    *,
    backup: Path | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **result.model_dump(mode="json"),
        "sqlite_backup": str(backup) if backup is not None else None,
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _load_manifest(path: Path) -> StateStoreMigrationResult:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("sqlite_backup", None)
    return StateStoreMigrationResult.model_validate(payload)


def _verify(source: SQLiteStateStore, target: PostgresStateStore) -> dict[str, Any]:
    source_docs = source.documents()
    target_docs = target.documents()
    missing = sorted(set(source_docs) - set(target_docs))
    mismatched = sorted(
        namespace
        for namespace in source_docs
        if namespace in target_docs
        and json.dumps(
            source_docs[namespace],
            sort_keys=True,
            separators=(",", ":"),
        )
        != json.dumps(
            target_docs[namespace],
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    subset = {
        namespace: target_docs[namespace]
        for namespace in source_docs
        if namespace in target_docs
    }
    source_checksum = state_documents_checksum(source_docs)
    target_checksum = state_documents_checksum(subset)
    return {
        "source_documents": len(source_docs),
        "target_documents": len(target_docs),
        "missing": missing,
        "mismatched": mismatched,
        "source_checksum": source_checksum,
        "target_checksum": target_checksum,
        "verified": (
            not missing
            and not mismatched
            and source_checksum == target_checksum
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source = SQLiteStateStore(args.sqlite)
    target = PostgresStateStore(args.postgres_dsn)
    migrator = StateStoreMigrator()

    if args.command == "migrate":
        if args.backup is None:
            raise SystemExit("--backup is required for migrate")
        backup = source.backup_to(args.backup)
        result = migrator.migrate(source, target)
        _write_manifest(args.manifest, result, backup=backup)
        print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
        return 0 if result.verified else 2

    if args.command == "verify":
        result = _verify(source, target)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["verified"] else 2

    manifest = _load_manifest(args.manifest)
    removed = migrator.rollback_destination(
        source,
        target,
        manifest,
    )
    print(
        json.dumps(
            {
                "removed_namespaces": list(removed),
                "count": len(removed),
                "source_unchanged": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
