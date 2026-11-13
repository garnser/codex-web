"""Real inspector ESM graph renders escaped role metadata; no mutation controls."""
import subprocess
import unittest

from tests.js_module_fixture import static_module_fixture


class ExecutiveRoleDiscoveryUiTests(unittest.TestCase):
    def test_current_role_context_unavailable_escaping_and_legacy_views(self):
        script = r'''
import assert from 'node:assert/strict';
globalThis.location = new URL('http://fixture.invalid/');
globalThis.window = Object.assign(new EventTarget(), {location});
globalThis.document = {body:{dataset:{}}, addEventListener(){}};
globalThis.fetch = () => { throw new Error('Rendering must not send requests'); };
const {brokerDeclarationHtml} = await import(process.argv[1]);
const context = {status:'available', definition:{record_id:'current-role-record',
  definition_id:'executive.roles.default', revision:7}, max_roles_per_activation:1,
  effective_authority_requires_operation_checks:true, authorizes_mutation:false,
  roles:[{id:'cto', name:'CTO', title:'Current scoped risk owner',
    description:'Evaluate guarded data reuse', responsibilities:['Preserve fresh authority']}]};
const operation = {id:'executive.activation.create', request_body:{type:'object'},
  role_selection_context:context};
const item = {id:'assignment', project_id:'project', execution_profile_id:'coordinator',
  brokered_control_plane:{enabled:true, credential_exposed:false, operations:[operation]}};
let html = brokerDeclarationHtml(item, 'project');
assert.ok(html.includes('<summary>Accepted request formats</summary>'));
assert.match(html, /Executive role selection: available/);
assert.match(html, /executive.roles.default@r7/);
assert.match(html, /current-role-record/);
assert.match(html, /&quot;id&quot;: &quot;cto&quot;/);
assert.match(html, /Current scoped risk owner/);
assert.match(html, /Preserve fresh authority/);
assert.match(html, /Selection data grants no operation authority/);
assert.doesNotMatch(html, /<input|<textarea/);
context.roles[0].description = '</pre><script>alert(1)</script>';
context.roles[0].id = '<img src=x onerror=alert(1)>';
html = brokerDeclarationHtml(item, 'project');
assert.ok(html.includes('&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;'));
assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
assert.doesNotMatch(html, /<script|<img/);
operation.role_selection_context = {status:'unavailable',
  reason:'Current published catalog unavailable <safe>', authorizes_mutation:false};
html = brokerDeclarationHtml(item, 'project');
assert.match(html, /Executive role selection: unavailable/);
assert.match(html, /Current published catalog unavailable &lt;safe&gt;/);
assert.match(html, /Load broker audit/);
assert.doesNotMatch(html, /Current scoped risk owner/);
delete operation.role_selection_context;
html = brokerDeclarationHtml(item, 'project');
assert.match(html, /Accepted request formats/);
assert.match(html, /Load broker audit/);
assert.doesNotMatch(html, /Executive role selection/);
'''
        with static_module_fixture('control_plane_broker_view.js') as entry:
            result = subprocess.run(['node', '--input-type=module', '-e', script, entry],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
