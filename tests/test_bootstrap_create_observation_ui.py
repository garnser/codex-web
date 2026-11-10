import json
from pathlib import Path
import subprocess
import unittest


class BootstrapCreateObservationUiTests(unittest.TestCase):
    def test_missing_creation_and_joined_cleanup_remain_distinct_and_escaped(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / 'static/bootstrap_create_observation_ui.js').read_text()
        helper = source[source.index('export function'):].replace('export function', 'function', 1)
        script = r'''
const assert = require('node:assert/strict');
const escapeHtml = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const timeText = value => `time:${value}`;
eval(HELPER);
assert.equal(bootstrapCreateObservationHtml({subject:{kind:'thread'}}), '');
const item = {subject:{kind:'thread_bootstrap'}};
assert.match(bootstrapCreateObservationHtml(item), /Native creation: unknown/);
assert.match(bootstrapCreateObservationHtml(item), /process cleanup: Unknown/);
item.bootstrap_create_observation = {outcome:'created', provider_native_session_id:'native<script>', rpc_request_id:'rpc<&', runtime_generation:'gen<&', fence:1};
let html = bootstrapCreateObservationHtml(item);
assert.match(html, /Native creation: created/);
assert.match(html, /native&lt;script&gt;/);
assert.match(html, /rpc&lt;&amp;/);
assert.match(html, /process cleanup: Unknown/);
item.bootstrap_create_observation.cleanup_joined_at = 123;
html = bootstrapCreateObservationHtml(item);
assert.match(html, /process cleanup: joined time:123/);
assert.match(html, /does not prove no effects/);
assert.match(html, /not a turn START or ACK/);
'''.replace('HELPER', json.dumps(helper))
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
