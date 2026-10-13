import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

function list(extra) {
  const run = spawnSync('npx', ['playwright', 'test', '--list', '--reporter=json', ...extra], {
    encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
  });
  assert.equal(run.status, 0, run.stderr || run.error?.message || run.stdout);
  const report = JSON.parse(run.stdout);
  assert.equal(report.errors.length, 0, JSON.stringify(report.errors));
  const keys = [];
  function visit(suite, parents = []) {
    const names = [...parents, suite.title];
    for (const spec of suite.specs || []) {
      for (const test of spec.tests) {
        keys.push(JSON.stringify([spec.file, ...names, spec.title, test.projectName]));
      }
    }
    for (const child of suite.suites || []) visit(child, names);
  }
  for (const suite of report.suites) visit(suite);
  assert.ok(keys.length, `Empty test selection: ${extra.join(' ')}`);
  assert.equal(new Set(keys).size, keys.length, 'Duplicate test identities');
  return keys;
}

const original = list([]);
const owned = list([
  'tests/browser/visual_regression.spec.js', 'tests/browser/routed_project_visual.spec.js',
  'tests/browser/accessibility_axe.spec.js', 'tests/browser/repository_target_ui.spec.js',
  'tests/browser/execution_preflight.spec.js',
]);
const shards = [1, 2, 3].map(shard => list(['--config=playwright.ci.config.js', `--shard=${shard}/3`]));
const selected = [...owned, ...shards.flat()];
assert.equal(new Set(selected).size, selected.length, 'Overlapping browser ownership/shards');
assert.deepEqual([...selected].sort(), [...original].sort(), 'Browser coverage changed');
console.log(`Coverage preserved: ${original.length} unique tests; ${owned.length} dedicated tests; functional shards ${shards.map(x => x.length).join('/')}.`);
