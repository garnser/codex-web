from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from codex_web.services.skill_scanner_configuration import validate_scanner_runtime
from codex_web.services.skill_scanners import SkillScannerError


class ScannerPublicCaBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Offline scanner fixture')])
        now = datetime.now(timezone.utc)
        builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(cls.key.public_key()).serial_number(1)
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1)))
        cls.ca = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(
            cls.key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        cls.leaf = builder.add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True).sign(
            cls.key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        cls.no_constraints = builder.sign(cls.key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name) / 'runtimes'
        self.root = base / 'scanner'
        (self.root / 'bin').mkdir(parents=True)
        launcher = self.root / 'bin' / 'skillspector'
        launcher.write_text('#!/usr/bin/python3\n')
        launcher.chmod(0o700)
        self.bundle = self.root / 'lib' / 'python3.14' / 'site-packages' / 'certifi' / 'cacert.pem'
        self.bundle.parent.mkdir(parents=True)
        setting = patch('codex_web.services.skill_scanner_configuration.SCANNER_RUNTIME_BASE', str(base))
        setting.start()
        self.addCleanup(setting.stop)

    def test_real_x509_ca_bundle_with_comments_is_admitted(self):
        self.bundle.write_bytes(b'# Public offline trust anchors\n\n' + self.ca + b'\n# Second anchor\n' + self.ca)
        self.assertEqual(validate_scanner_runtime(self.root), self.root)

    def test_legacy_public_root_without_basic_constraints_is_admitted(self):
        # Public VeriSign root from botocore 1.43.114's distributed CA bundle;
        # X.509 v1 predates extensions. This fixture contains no private key.
        legacy = Path(__file__).with_name('fixtures') / 'skill_scanner_legacy_ca.pem'
        self.bundle.write_bytes(self.ca + legacy.read_bytes())
        self.assertEqual(validate_scanner_runtime(self.root), self.root)

    def test_mixed_or_malformed_material_is_refused(self):
        key = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
        for data in (key, self.ca + key, self.ca + b'credential=synthetic-secret\n',
                     self.ca + b'-----BEGIN CERTIFICATE-----\nINVALID\n-----END CERTIFICATE-----\n',
                     b'# No certificates\n', self.ca + b'\n# PRIVATE KEY marker\n'):
            with self.subTest(data=data[:30]):
                self.bundle.write_bytes(data)
                with self.assertRaises(SkillScannerError):
                    validate_scanner_runtime(self.root)

    def test_leaf_and_unconstrained_certificates_are_refused(self):
        for data in (self.leaf, self.no_constraints, self.ca + self.leaf):
            self.bundle.write_bytes(data)
            with self.assertRaises(SkillScannerError):
                validate_scanner_runtime(self.root)

    def test_only_the_ca_bundle_filename_is_eligible(self):
        for name in ('identity.pem', 'cacert.key', 'cacert.p12', 'cacert.pfx', '.cacert.pem', 'cacert.keystore'):
            path = self.bundle.with_name(name)
            path.write_bytes(self.ca)
            try:
                with self.subTest(name=name), self.assertRaises(SkillScannerError):
                    validate_scanner_runtime(self.root)
            finally:
                path.unlink()

    def test_oversized_and_excessive_certificate_bundles_are_refused(self):
        for data in (b'#' + b'x' * 512_000, self.ca * 513):
            self.bundle.write_bytes(data)
            with self.assertRaises(SkillScannerError):
                validate_scanner_runtime(self.root)

    def test_certificate_symlinks_cannot_exempt_external_files(self):
        target = self.root.parent / 'external.pem'
        target.write_bytes(self.ca)
        self.bundle.symlink_to(target)
        with self.assertRaises(SkillScannerError):
            validate_scanner_runtime(self.root)

    def test_trust_anchors_do_not_exempt_other_credentials(self):
        self.bundle.write_bytes(self.ca)
        (self.root / 'lib' / 'operator.key').write_text('synthetic credential fixture')
        with self.assertRaises(SkillScannerError):
            validate_scanner_runtime(self.root)


if __name__ == '__main__':
    unittest.main()
