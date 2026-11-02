from __future__ import annotations

import unittest
import subprocess
from pathlib import Path


class FrontendHistoryContractTests(unittest.TestCase):
    def test_thread_history_does_not_patch_global_fetch_or_scroll_accessor(self) -> None:
        history = Path("static/thread_history.js").read_text()

        self.assertNotIn("window.fetch =", history)
        self.assertNotIn("Object.defineProperty(messages, \"scrollTop\"", history)
        self.assertIn("window.codexThreadHistory", history)
        self.assertIn("requestBottomScroll", history)

    def test_app_requests_thread_history_explicitly(self) -> None:
        app = Path("static/app.js").read_text()

        self.assertIn('history?.readQuery?.(threadId) || ""', app)
        self.assertIn('history?.renderReadNotice?.(thread, addMessage)', app)
        self.assertIn("history?.recordThread?.(threadId, thread)", app)
        self.assertIn("history?.afterThreadRendered?.(threadId, $(\"messages\"))", app)
        self.assertNotIn('$("messages").scrollTop = $("messages").scrollHeight;', app)

    def test_history_controller_owns_query_and_truthful_read_notice(self) -> None:
        subprocess.run(['node', '-e', '''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
let click;
const control = {contains:()=>false, replaceChildren(){}, style:{}, appendChild(){}};
const document = {
  getElementById: id => id === 'thread-meta' ? {textContent:'home-new · Test'} : id === 'thread-history-control' ? control : null,
  createElement: () => ({addEventListener: (_, handler) => {click=handler;}}),
};
const window = {addEventListener() {}};
vm.runInNewContext(fs.readFileSync('static/thread_history.js', 'utf8'), {window, document, URLSearchParams});
const history = window.codexThreadHistory, messages = [];
assert.equal(history.readQuery('home-new'), 'message_limit=40');
assert.equal(history.messageLimit('home-new'), 40);
const expression = fs.readFileSync('static/app.js', 'utf8').split('const query = ')[1].split(';')[0];
const query = value => vm.runInNewContext(expression, {history:value, threadId:'home-new'});
assert.equal(query(undefined), '');
assert.equal(query({}), '');
assert.equal(query(history), 'message_limit=40');
history.renderReadNotice({historyRead:{state:'unsupported'}}, (...args) => messages.push(args));
assert.equal(messages.length, 1);
assert.equal(messages[0][0], 'Native history unavailable');
assert.ok(messages[0][1].includes('native history completeness is unknown'));
assert.equal(messages[0][2], 'tool');
assert.equal(typeof messages[0][3].toISOString, 'function');
history.renderReadNotice({}, () => assert.fail('Supported history must not show limitation'));
(async () => {
  const requests=[];
  history.recordThread('home-new', {messagesTruncated:true});
  history.configure({reloadThread:async () => requests.push(query(history))});
  await click();
  assert.equal(history.messageLimit('home-new'), 80);
  assert.deepEqual(requests, ['message_limit=80']);
})().catch(error => {console.error(error); process.exitCode=1;});
'''], check=True, capture_output=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
