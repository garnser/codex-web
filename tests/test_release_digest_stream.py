"""Exercise the actual workflow digest pipelines against a large CLI response."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


class ReleaseDigestStreamTests(unittest.TestCase):
    def test_all_digest_readers_consume_the_complete_cli_response(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/publish-release.yml").read_text())
        lines = [line.strip()
                 for job in workflow["jobs"].values()
                 for step in job.get("steps", [])
                 for line in step.get("run", "").splitlines()
                 if "digest=" in line and "docker buildx imagetools inspect" in line]
        self.assertEqual(len(lines), 3, "candidate, promotion and promoted-alias readers are required")
        expected = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            docker = Path(directory) / "docker"
            docker.write_text(f"#!{sys.executable}\n" + '''import os, sys
assert sys.argv[1:4] == ["buildx", "imagetools", "inspect"]
try:
    sys.stdout.write("Name: fixture\\nDigest: sha256:" + "a" * 64 + "\\n")
    sys.stdout.flush()
    # Attestation manifests can exceed the pipe buffer. A reader that exits
    # after Digest leaves the actual CLI reporting a failed write under
    # pipefail, even though the registry push already succeeded.
    sys.stdout.write("Attestation manifest data\\n" * 200000)
    sys.stdout.write("Digest: sha256:" + "b" * 64 + "\\n")
    sys.stdout.flush()
except BrokenPipeError:
    os._exit(255)
''')
            docker.chmod(0o700)
            environment = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"]}
            for line in lines:
                with self.subTest(pipeline=line):
                    command = ('set -euo pipefail\ncandidate_tag=fixture\npromoted=fixture\n'
                               + line + '\nprintf "%s" "$digest"\n')
                    result = subprocess.run(["bash", "-c", command], env=environment,
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)
