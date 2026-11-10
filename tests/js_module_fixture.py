"""Run real static ES modules inside an explicit, isolated package boundary."""
from contextlib import contextmanager
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory


@contextmanager
def static_module_fixture(entry):
    """Yield an entry URL while its unchanged complete static tree exists.

    Node resolves the actual relative dependency graph. Missing imports and
    exports still fail; no source rewriting, evaluator or import stubs are used.
    """
    source = Path(__file__).resolve().parents[1] / "static"
    with TemporaryDirectory(prefix="codex-static-module-") as directory:
        package = Path(directory)
        (package / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
        shutil.copytree(source, package / "static")
        yield (package / "static" / entry).as_uri()
