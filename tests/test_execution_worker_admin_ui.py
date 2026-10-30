from __future__ import annotations

import unittest
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ExecutionWorkerAdminUiTests(unittest.TestCase):
    def test_native_custody_rendering_distinguishes_records_and_escapes_references(self):
        javascript = (ROOT / "static" / "execution_worker_admin.js").read_text(encoding="utf-8")
        presenter = (ROOT / "static" / "native_credential_custody_ui.js").read_text(encoding="utf-8")
        helper = "function nativeCredentialCustodyHtml(item) {" + presenter.split(
            "export function nativeCredentialCustodyHtml(item) {", 1)[1]
        self.assertIn("/static/native_credential_custody_ui.js", javascript)
        self.assertNotIn("function nativeCredentialCustodyHtml", javascript)
        check = r'''
const assert = require('node:assert/strict');
const escapeHtml = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const referenceLink = (kind, id, options = {}) => id ? escapeHtml(options.label ?? id) : '—';
const timeText = value => `time:${value}`;
eval(HELPER);
assert.match(nativeCredentialCustodyHtml({}), /legacy unknown/);
assert.match(nativeCredentialCustodyHtml({native_credential_custody: null}), /legacy unknown/);
const pending = nativeCredentialCustodyHtml({native_credential_custody: {schema_version:'1.0', admitted_at:null}});
assert.match(pending, /custody: pending/);
assert.match(pending, /no credential grant inferred/);
assert.match(pending, /Admission: not established/);
const html = nativeCredentialCustodyHtml({native_credential_custody: {
 schema_version:'1.0', admitted_at:123, runtime_secret_ref:'runtime<&',
 procedure_secret_refs:['procedure<&'], requester_identity_id:'requester<&', worker_identity_id:'worker<&', fence:7,
 procedures:[{record_id:'record', definition_id:'skill<&', kind:'agent.skill', revision:3, definition_schema_version:'1.2', checksum:'hash<&'}],
 provider_bindings:{'binding<&':'pin<&'}, secret_value:'NEVER-RENDER', lease_token:'NEVER-RENDER'
}});
assert.match(html, /admitted \(recorded\)/);
assert.match(html, /Runtime authentication reference: runtime&lt;&amp;/);
assert.match(html, /Procedure credential references: procedure&lt;&amp;/);
assert.match(html, /requester&lt;&amp;/); assert.match(html, /worker&lt;&amp;/);
assert.match(html, /fence: 7/); assert.match(html, /skill&lt;&amp;/);
assert.match(html, /revision 3/); assert.match(html, /schema 1.2/);
assert.match(html, /hash&lt;&amp;/); assert.match(html, /binding&lt;&amp;/); assert.match(html, /pin&lt;&amp;/);
assert.match(html, /does not grant current authority/);
assert.doesNotMatch(html, /NEVER-RENDER|<script|runtime<&|skill<&/);
for (const custody of [{schema_version:'future', admitted_at:123}, {schema_version:'1.0', admitted_at:'123'}]) {
 const unknown = nativeCredentialCustodyHtml({native_credential_custody:custody});
 assert.match(unknown, /custody: unknown/); assert.match(unknown, /Admission: not established/);
}
'''
        subprocess.run(["node", "-e", check.replace("HELPER", json.dumps(helper))],
                       check=True, timeout=10, capture_output=True, text=True)
        self.assertIn("${nativeCredentialCustodyHtml(item)}", javascript)

    def test_worker_browser_exposes_control_execution_split_and_redacted_leases(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (ROOT / "static" / "execution_worker_admin.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('/static/execution_worker_presenters.js', javascript)
        javascript += (ROOT / "static" / "execution_worker_presenters.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="execution-worker-list"', html)
        self.assertIn('id="execution-assignment-list"', html)
        self.assertIn('id="execution-worker-events"', html)
        self.assertIn('apiRequest("/api/execution-workers")', javascript)
        self.assertIn('apiRequest("/api/execution-workers/assignments")', javascript)
        self.assertIn('apiRequest("/api/execution-workers/events")', javascript)
        self.assertIn("service_identity_id", javascript)
        self.assertIn("capabilities", javascript)
        self.assertIn("max_concurrency", javascript)
        self.assertIn("last_heartbeat_at", javascript)
        self.assertIn("quarantine_reason", javascript)
        self.assertIn("execution_workspace_id", javascript)
        self.assertIn("work_item_ref", javascript)
        self.assertIn("subjectText", javascript)
        self.assertIn("item.subject?.kind", javascript)
        self.assertIn("item.subject?.ref", javascript)
        self.assertIn("execution_id", javascript)
        self.assertIn("required_capabilities", javascript)
        self.assertIn("sandbox", javascript)
        self.assertIn("approval_policy", javascript)
        self.assertIn("allowed_hosts", javascript)
        self.assertIn("memory_bytes", javascript)
        self.assertIn("secret_refs", javascript)
        self.assertIn("Raw secret material is never present", javascript)
        self.assertIn("lease_token", javascript)
        self.assertIn("fence", javascript)
        self.assertIn("artifact_ids", javascript)
        self.assertIn("evidence_ids", javascript)
        self.assertIn("control-plane state is distinct from execution lease authority", javascript)
        self.assertIn("codex:execution-worker-state-rendered", javascript)
        self.assertNotIn("/drain", javascript)
        self.assertNotIn("/quarantine", javascript)
        self.assertNotIn("/revoke", javascript)
        self.assertNotIn("/recover-expired", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("fetch(", javascript)


if __name__ == "__main__":
    unittest.main()
