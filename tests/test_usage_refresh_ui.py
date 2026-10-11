from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class UsageRefreshUiTests(unittest.TestCase):
    def test_bursts_full_refresh_project_changes_and_provider_snapshots(self):
        scripts = Path(__file__).resolve().parents[1] / 'static'
        javascript = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
const resourceCode = fs.readFileSync(process.argv[1] + '/usage_resources.js');
const resourceUrl = 'data:text/javascript;base64,' + Buffer.from(resourceCode).toString('base64');
const load = async name => import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync(process.argv[1] + '/' + name, 'utf8').replace('"./usage_resources.js"', JSON.stringify(resourceUrl))).toString('base64'));
const {createUsageRefresher, createHeaderUsageController} = await load('usage_refresh.js');
const {normalizeCodexRateLimitResources} = await load('usage_resources.js');
let timers = [], calls = [], release;
const scheduled = cb => { timers.push(cb); return cb; };
const canceled = cb => { timers = timers.filter(timer => timer !== cb); };
const reader = createUsageRefresher({refresh: options => {
  calls.push(options); return new Promise(resolve => { release = resolve; });
}, schedule: scheduled, cancel: canceled});
const tick = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
for (let i = 0; i < 100; i++) reader.request({event: true});
assert.equal(timers.length, 1);
assert.equal(calls.length, 0);
timers.shift()(); await tick();
assert.deepEqual(calls, [{includeRateLimits: false}]);
for (let i = 0; i < 100; i++) reader.request({event: true});
assert.equal(calls.length, 1);
reader.request(); // A project switch or manual request must not get lost.
release(); await tick();
assert.deepEqual(calls, [{includeRateLimits: false}, {includeRateLimits: true}]);
release(); await tick();
assert.equal(timers.length, 0);
reader.request({event: true}); assert.equal(timers.length, 1);
reader.request(); await tick(); assert.equal(timers.length, 0);
assert.equal(calls.at(-1).includeRateLimits, true);
release(); await tick();
const resources = normalizeCodexRateLimitResources({rateLimits: {limitId: 'codex', primary: {
  usedPercent: 25, windowDurationMins: 300, resetsAt: 99}, secondary: {usedPercent: 10, windowDurationMins: 10080}}});
assert.equal(resources[0].label, '5 hour rate limit');
assert.equal(resources[0].consumed, 25); assert.equal(resources[0].remaining, 75);
assert.equal(resources[0].reset_at, 99); assert.equal(resources[1].label, 'Weekly rate limit');
assert.deepEqual(normalizeCodexRateLimitResources({primary: {usedPercent: true}}), []);
assert.deepEqual(normalizeCodexRateLimitResources({primary: {usedPercent: null}}), []);
assert.equal(normalizeCodexRateLimitResources({rateLimitsByLimitId: {codex: {primary: {usedPercent: 30}}}})[0].consumed, 30);
let finishUsage;
const state = {projectId: 'p', accountUsageResources: []};
const controller = createHeaderUsageController({state, schedule: scheduled, cancel: canceled, render() {}, log() {}, api: async path => {
  if (path.includes('rate-limits')) return {usage_resources: [{kind: 'rate_limit', consumed: 1}]};
  return new Promise(resolve => { finishUsage = resolve; });
}});
const initial = controller.request(); await tick();
controller.handleRateLimits({rateLimits: {primary: {usedPercent: 70}}});
finishUsage({resources: []}); await initial; await tick();
assert.equal(state.accountUsageResources[0].consumed, 70, 'in-flight response must not overwrite a newer event');

'''
        result = subprocess.run(['node', '--input-type=module', '-e', javascript, str(scripts)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_header_uses_summary_and_notifications_do_not_read_provider_again(self):
        app = (Path(__file__).resolve().parents[1] / 'static/app.js').read_text()
        event_handler = app.split('message.method === "account/rateLimits/updated"', 1)[1].split('if (!isActiveThreadEvent', 1)[0]
        self.assertIn('usageController.handleRateLimits', event_handler)
        self.assertNotIn('refreshTokenUsage()', event_handler)
        controller = (Path(__file__).resolve().parents[1] / 'static/usage_refresh.js').read_text()
        self.assertIn('/api/agent-runtime-usage?include_items=false', controller)
        self.assertIn('refresher.request({event: true})', controller)
