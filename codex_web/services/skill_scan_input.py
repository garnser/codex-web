"""Bounded data-only scanner bundle; never an execution or authority manifest."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.parse import quote

BUNDLE_FORMAT = 'skill-static-input/1.0'
MAX_BUNDLE_BYTES = 512_000
PROCEDURE_PATH = 'codex-web-procedure.json'
ASSET_METADATA_PATH = 'codex-web-assets.json'


@dataclass(frozen=True)
class SkillScanInput:
    files: tuple[tuple[str, bytes], ...]

    @property
    def manifest_sha256(self):
        manifest = [{'path': path, 'bytes': len(data),
                     'sha256': hashlib.sha256(data).hexdigest()}
                    for path, data in self.files]
        return hashlib.sha256(json.dumps(manifest, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()

    def write(self, root: Path):
        # root is a newly owned TemporaryDirectory, never a caller path.
        for relative, data in self.files:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
            target.chmod(0o600)  # Helpers are inspected bytes, never executed.


def build_skill_scan_input(view) -> SkillScanInput:
    skill = view['skill']
    primary = f"# {skill['name']}\n\n{skill.get('description', '')}\n\n{skill['instructions']}".strip()
    files = {}
    links = []
    assets = skill.get('assets', [])
    if not isinstance(assets, (list, tuple)) or len(assets) > 32:
        raise ValueError('invalid scanner asset count')
    for asset in assets:
        path, content = asset['path'], asset['content']
        if (not isinstance(path, str) or not path or len(path) > 240
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or '\\' in path or PurePosixPath(path).is_absolute()
                or any(part in ('', '.', '..') or part.startswith('.')
                       or len(part.encode('utf-8')) > 255 for part in path.split('/'))
                or path in ('SKILL.md', PROCEDURE_PATH, ASSET_METADATA_PATH) or path in files
                or not isinstance(content, str)):
            raise ValueError('invalid scanner asset path or content')
        files[path] = content.encode()
        links.append(f'- [Skill asset](<{quote(path, safe="/")}>)')
    if assets:
        # Preserve canonical asset roles without turning their metadata into
        # primary Markdown references or granting helper execution authority.
        files[ASSET_METADATA_PATH] = (json.dumps(
            [{'path': asset['path'], 'kind': asset.get('kind')} for asset in assets],
            sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode()
        links.append(f'- [Asset data roles]({ASSET_METADATA_PATH})')
    procedure = skill.get('operational_procedure')
    if procedure is not None:
        # The exact structured values remain inspected JSON. Flattening this
        # into primary Markdown invents file references from quoted method IDs.
        files[PROCEDURE_PATH] = (json.dumps(procedure, sort_keys=True,
            ensure_ascii=False, allow_nan=False) + '\n').encode()
        links.append(f'- [Operational procedure data]({PROCEDURE_PATH})')
    if links:
        primary += '\n\n## Bundled Skill data (untrusted)\n' + '\n'.join(links)
    files['SKILL.md'] = primary.encode()
    names = set(files)
    if any('/'.join(path.split('/')[:index]) in names
           for path in names for index in range(1, len(path.split('/')))):
        raise ValueError('scanner bundle file/directory collision')
    if sum(map(len, files.values())) > MAX_BUNDLE_BYTES:
        raise ValueError('scanner bundle exceeds input limit')
    return SkillScanInput(tuple(sorted(files.items())))
