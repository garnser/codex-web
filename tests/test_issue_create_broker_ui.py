"""Import the real inspector module graph and render its canonical broker metadata."""
import json
from pathlib import Path
import subprocess
import unittest

from codex_web.services.control_plane_broker import ControlPlaneBrokerService


class IssueCreateBrokerUiTests(unittest.TestCase):
    def test_actual_module_renders_canonical_request_schema_and_preserves_legacy_views(self):
        root = Path(__file__).resolve().parents[1]
        operation = next(item for item in ControlPlaneBrokerService.operations()
                         if item.id == "repository.issue.create")
        descriptor = ControlPlaneBrokerService.public_operation(operation)
        script = r'''
import assert from 'node:assert/strict';
globalThis.location = new URL('http://fixture.invalid/');
globalThis.window = Object.assign(new EventTarget(), {location});
globalThis.document = {body:{dataset:{}}, addEventListener(){}};
const {brokerDeclarationHtml} = await import(process.argv[1]);
const operation = JSON.parse(process.argv[2]);
const item = {id:'assignment', project_id:'project', execution_profile_id:'orchestration-admin',
  brokered_control_plane:{enabled:true, credential_exposed:false, operations:[operation], assignment_context:{}}};
let html = brokerDeclarationHtml(item, 'project');
assert.ok(html.includes('<summary>Accepted request formats</summary>'));
assert.match(html, /repository.issue.create/);
assert.match(html, /&quot;additionalProperties&quot;: false/);
assert.match(html, /&quot;title&quot;/);
assert.match(html, /&quot;body&quot;/);
assert.match(html, /&quot;maxLength&quot;: 256/);
assert.match(html, /&quot;maxLength&quot;: 65536/);
assert.match(html, /current scope and authority still apply/);
assert.doesNotMatch(html, /<input|<textarea/);
assert.equal(operation.request_body.parameters.additionalProperties, false);
assert.deepEqual(operation.request_body.parameters.required, ['title']);
assert.equal(operation.request_body.parameters.properties.title.pattern, String.fromCharCode(92) + 'S');
operation.id = 'repository.issue.create<img src=x onerror=alert(1)>';
operation.request_body.description = '</pre><script>alert(1)</script>';
html = brokerDeclarationHtml(item, 'project');
assert.match(html, /repository.issue.create&lt;img/);
assert.ok(html.includes('&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;'));
assert.doesNotMatch(html, /<img|<script/);
item.brokered_control_plane.operations = [{id:'legacy.operation'}];
html = brokerDeclarationHtml(item, 'project');
assert.match(html, /legacy.operation/);
assert.match(html, /Load broker audit/);
assert.doesNotMatch(html, /Accepted request formats/);
delete item.brokered_control_plane;
assert.match(brokerDeclarationHtml(item, 'project'), /Brokered control plane/);
'''
        result = subprocess.run([
            "node", "--experimental-default-type=module", "--input-type=module", "-e", script,
            (root / "static/control_plane_broker_view.js").as_uri(), json.dumps(descriptor),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
