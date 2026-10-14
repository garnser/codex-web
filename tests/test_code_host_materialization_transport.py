"""Real Git HTTPS transport regressions; all credentials and repositories are fixtures."""
from __future__ import annotations

import base64
import http.server
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_web.code_hosts import CodeHostError, CodeHostProviderBinding, CodeHostRepositoryFact
from codex_web.services.code_hosts import CodeHostService


class MaterializationTransportTests(unittest.TestCase):
    def test_attested_gitlab_prefix_port_and_foreign_endpoints(self):
        binding = CodeHostProviderBinding(
            id="gitlab", organization_id="org", workspace_id="workspace",
            provider_type="gitlab", provider_instance="git.example:8443",
            base_url="http://127.0.0.1:8081/gitlab/api/v4",
            capabilities=(),
        )
        fact = CodeHostRepositoryFact(
            resource_id="repo", provider_type="gitlab",
            provider_instance="git.example:8443", external_id="42",
            name="worker", full_name="team/worker", default_branch="main",
            web_url="https://git.example:8443/gitlab/team/worker",
        )
        self.assertEqual(CodeHostService._verified_remote_url(binding, fact),
                         "https://git.example:8443/gitlab/team/worker.git")
        for url in (None, "https://foreign.example:8443/gitlab/team/worker",
                    "https://git.example/gitlab/team/worker",
                    "https://user:secret@git.example:8443/gitlab/team/worker",
                    "https://git.example:8443/gitlab/../team/worker",
                    "https://git.example:8443/gitlab/team/worker?token=secret"):
            with self.subTest(url=url), self.assertRaises(CodeHostError):
                CodeHostService._verified_remote_url(binding, fact.model_copy(update={"web_url": url}))

    def test_real_https_ignores_checkout_config_and_refuses_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, checkout = root / "upstream", root / "checkout"
            run = subprocess.run

            def git(*args, cwd=None):
                return run(["git", *args], cwd=cwd, check=True, capture_output=True,
                           text=True).stdout.strip()

            git("init", "--initial-branch=main", str(upstream))
            (upstream / "file").write_text("verified content\n")
            git("add", "file", cwd=upstream)
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-m", "fixture", cwd=upstream)
            revision = git("rev-parse", "HEAD", cwd=upstream)
            git("init", str(checkout))
            (checkout / "dirty-file").write_text("preserve me")
            cert, key = root / "cert.pem", root / "key.pem"
            run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-days", "1", "-subj", "/CN=localhost", "-addext",
                 "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
                check=True, capture_output=True)
            requests = []
            redirect = False
            expected_auth = "Basic " + base64.b64encode(b"x-access-token:fixture-token").decode()

            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_GET(self):
                    requests.append((self.path, self.headers.get("Authorization")))
                    if self.path.startswith("/foreign"):
                        self.send_error(500)
                        return
                    if redirect:
                        self.send_response(302)
                        self.send_header("Location", "/foreign/info/refs?service=git-upload-pack")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if self.headers.get("Authorization") != expected_auth:
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    output = run(["git", "upload-pack", "--stateless-rpc",
                                  "--advertise-refs", str(upstream)],
                                 check=True, capture_output=True).stdout
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
                    self.send_header("Content-Length", str(len(output) + 34))
                    self.end_headers()
                    self.wfile.write(b"001e# service=git-upload-pack\n0000" + output)

                def do_POST(self):
                    requests.append((self.path, self.headers.get("Authorization")))
                    data = self.rfile.read(int(self.headers["Content-Length"]))
                    output = run(["git", "upload-pack", "--stateless-rpc", str(upstream)],
                                 input=data, check=True, capture_output=True).stdout
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-git-upload-pack-result")
                    self.send_header("Content-Length", str(len(output)))
                    self.end_headers()
                    self.wfile.write(output)

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host = f"https://localhost:{server.server_port}"
            git("config", f"url.{host}/foreign/.insteadOf", host + "/", cwd=checkout)
            included = root / "included-config"
            included.write_text(f"[http]\n\textraHeader = X-Poison: local-config\n"
                                f"[url \"{host}/foreign/\"]\n\tinsteadOf = {host}/\n")
            git("config", "include.path", str(included), cwd=checkout)
            marker = root / "helper-executed"
            git("config", "credential.helper", f"!touch {marker}", cwd=checkout)
            git("config", "http.followRedirects", "true", cwd=checkout)
            config_before = (checkout / ".git/config").read_bytes()

            def trusted_ca(command, **kwargs):
                # Only supply our fixture CA; all Git operations and transport
                # remain real, including the hostile checkout configuration.
                if "fetch" in command:
                    command = [command[0], "-c", f"http.sslCAInfo={cert}", *command[1:]]
                return run(command, **kwargs)

            try:
                with patch("codex_web.services.code_hosts.subprocess.run", side_effect=trusted_ca):
                    CodeHostService._materialize_git_sync(
                        checkout, host + "/repo.git", "main", revision,
                        "fixture-token", "github", "resource",
                    )
                    self.assertTrue(CodeHostService._git_has_commit(checkout, revision))
                    redirect = True
                    with self.assertRaises(CodeHostError):
                        CodeHostService._materialize_git_sync(
                            checkout, host + "/repo.git", "main", revision,
                            "fixture-token", "github", "resource",
                        )
                self.assertTrue(any(auth == expected_auth for _, auth in requests))
                self.assertFalse(any(path.startswith("/foreign") for path, _ in requests))
                self.assertFalse(marker.exists())
                self.assertEqual((checkout / ".git/config").read_bytes(), config_before)
                self.assertEqual((checkout / "dirty-file").read_text(), "preserve me")
                self.assertFalse((checkout / "file").exists())
                self.assertEqual(git("for-each-ref", cwd=checkout), "")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
