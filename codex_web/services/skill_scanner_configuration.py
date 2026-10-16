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


def _public_ca_bundle(path):
    """Recognize bounded public trust-anchor data, never private credentials.

    SkillSpector's Python dependencies load their bundled CA certificates even
    for offline imports. A filename alone cannot exempt a PEM from the runtime
    credential filter; every block must decode to an X.509 CA certificate.
    """
    import re
    from cryptography import x509

    if path.name != 'cacert.pem' or path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open('rb') as stream:
            data = stream.read(512_001)
        if len(data) > 512_000:
            return False
        blocks = list(re.finditer(
            rb'-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----', data))
        if not blocks or len(blocks) > 512:
            return False
        remainder = re.sub(
            rb'-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----', b'', data)
        if any(line.strip() and not line.lstrip().startswith(b'#') for line in remainder.splitlines()):
            return False
        if b'PRIVATE KEY' in data:
            return False
        for block in blocks:
            certificate = x509.load_pem_x509_certificate(block.group())
            try:
                if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                    return False
            except x509.ExtensionNotFound:
                # Legacy public root bundles also contain X.509 v1 roots,
                # which predate Basic Constraints. This checks public data type,
                # not TLS trust or signature-policy eligibility for that root.
                if certificate.version != x509.Version.v1 or certificate.issuer != certificate.subject:
                    return False
        return True
    except (OSError, ValueError, x509.ExtensionNotFound, x509.DuplicateExtension):
        return False


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
            credential_file = name.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.keystore'))
            if (count > 100_000 or name.startswith('.')
                    or (credential_file and not _public_ca_bundle(path))):
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
