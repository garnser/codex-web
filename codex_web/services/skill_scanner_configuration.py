"""Operator configuration; installation and worker authority remain separate."""
from codex_web.configuration import ConfigurationScope, ConfigurationSpec, ConfigurationValueKind

SCANNER_ROOT = 'skills.scanner.runtime_root'
SCANNER_PROJECT = 'skills.scanner.project_id'


def install_skill_scanner_configuration(service):
    for key, description in (
        (SCANNER_ROOT, 'Dedicated operator-installed SkillSpector runtime directory containing bin/skillspector. Mounted read-only into the isolated worker; never a credential directory.'),
        (SCANNER_PROJECT, 'Canonical Project owning request-driven scanner scratch workspaces and evidence. Must belong to the requesting tenant.'),
    ):
        service.register_spec(ConfigurationSpec(key=key, value_kind=ConfigurationValueKind.STRING,
            required=False, default=None, allowed_scopes=[ConfigurationScope.ORGANIZATION, ConfigurationScope.WORKSPACE],
            category='Advanced', description=description, hot_reloadable=True, startup_only=False, grants_authority=False))


# Deployment contract, not an operator-selectable authority or filesystem root.
SCANNER_RUNTIME_BASE = '/opt/codex-skill-runtimes'


def validate_scanner_runtime(value):
    from pathlib import Path
    import os
    import re
    from codex_web.services.skill_scanners import SkillScannerError
    root = Path(value)
    base = Path(SCANNER_RUNTIME_BASE)
    if (not root.is_absolute() or root.parent != base or root.resolve() != root
            or not root.is_dir() or root.name.startswith('.')):
        raise SkillScannerError('Scanner runtime must be a dedicated direct child of /opt/codex-skill-runtimes.')
    allowed = {'bin', 'lib', 'lib64', 'pyvenv.cfg'}
    if any(p.name not in allowed for p in root.iterdir()):
        raise SkillScannerError('Scanner runtime contains files outside the dedicated Python runtime contract.')
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in (*dirs, *files):
            count += 1
            path = Path(directory) / name
            if count > 100_000 or name.startswith('.') or name.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.keystore')):
                raise SkillScannerError('Scanner runtime contains a forbidden credential/hidden path or exceeds its inventory bound.')
            if path.is_symlink():
                target = path.resolve()
                system_python = path.parent == root / 'bin' and (name == 'python' or re.fullmatch(r'python3(?:\.[0-9]{1,2})?', name)) and target.parent == Path('/usr/bin') and re.fullmatch(r'python3(?:\.[0-9]{1,2})?', target.name)
                if not target.is_relative_to(root) and not system_python:
                    raise SkillScannerError('Scanner runtime symlink escapes its dedicated directory.')
            elif not path.is_file() and not path.is_dir():
                raise SkillScannerError('Scanner runtime contains a non-regular entry.')
    executable = root / 'bin' / 'skillspector'
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise SkillScannerError('Install executable bin/skillspector in the dedicated runtime.')
    return root
