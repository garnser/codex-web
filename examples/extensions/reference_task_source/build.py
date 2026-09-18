from __future__ import annotations

import json
from pathlib import Path

from codex_web.extension_builder import build_extension_package


HERE = Path(__file__).resolve().parent


if __name__ == "__main__":
    manifest = json.loads((HERE / "manifest.template.json").read_text(encoding="utf-8"))
    built = build_extension_package(manifest, HERE / "reference_task_source.py", HERE / "dist")
    print(f"built {built.manifest.id}@{built.manifest.version} in {built.directory}")
