from __future__ import annotations

import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "example-business-data-source.zip"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "manifest.template.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        archive.write(ROOT / "adapter.py", "adapter.py")
        archive.write(ROOT / "README.md", "README.md")
    print(OUT)


if __name__ == "__main__":
    main()
