"""Shared trusted-local installer serialization and source readback."""
from contextlib import contextmanager
import fcntl
from pathlib import Path
import re
import subprocess

LOCAL_DEPLOY_LOCK = Path("/home/nbingester/codex-web-native-runtime/local-deploy.lock")


@contextmanager
def unchanged_local_installation(checkout: Path, running_revision: str, *, lock_path=LOCAL_DEPLOY_LOCK):
    if not re.fullmatch(r"[0-9a-f]{40}", running_revision):
        raise ValueError("running installation revision is unavailable")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("local installer is active") from exc
        try:
            observed = subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-C", str(checkout),
                 "rev-parse", "--verify", "HEAD"],
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("live checkout revision is unavailable") from exc
        if observed != running_revision:
            raise ValueError("live checkout differs from the running build")
        yield observed
