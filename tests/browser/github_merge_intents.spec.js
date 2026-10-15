const { test, expect } = require('@playwright/test');

const A = 'a'.repeat(40), B = 'b'.repeat(40), C = 'c'.repeat(40);
const fixture = `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body data-project-id="project-a"><details id="developer-panel" open>
<button id="refresh-action-intents">Refresh</button><div id="action-intent-status" aria-live="polite"></div>
<div id="action-intent-list"></div></details><script type="module" src="/static/action_intent_admin.js"></script></body></html>`;

for (const status of ['failed', 'uncertain', 'requires_reconciliation', 'succeeded']) {
  test(`merge inspector preserves canonical ${status} and exact head evidence`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const calls = [];
    const verified = status === 'succeeded';
    await page.route('**/merge-intent-fixture', route => route.fulfill({ contentType: 'text/html', body: fixture }));
    await page.route('**/api/**', route => {
      const request = route.request();
      calls.push(request.method());
      const path = new URL(request.url()).pathname;
      if (path === '/api/identity/me') return route.fulfill({ json: { identity_id: 'operator', assurance: 'mfa' } });
      if (path === '/api/action-intents') return route.fulfill({ json: { items: [{
        id: 'merge-1', action_id: 'code-host.pull-request.merge', provider_type: 'github', provider_instance: 'github.com',
        status, project_id: 'project-a', request: { parameters: { expected_head_sha: A, secret: 'never-render-this' } },
        last_error: status === 'failed' ? 'GitHub refused the pull request merge (HTTP 409)' : null,
        retry_policy: { max_attempts: 1 }, attempt: 1,
      }] } });
      if (path === '/api/action-intents/merge-1/history') return route.fulfill({ json: {
        receipts: [{ outcome: verified ? 'completed' : status === 'failed' ? 'failed' : 'unknown', attempt: 1,
          result: verified ? { action_id: 'code-host.pull-request.merge', status: 'succeeded',
            output: { head_sha: A, merge_commit_sha: C, secret: 'never-render-this' } } : null }],
        verifications: status === 'failed' ? [] : [{ verified, provider_verification: { verified,
          evidence: [{ evidence_type: 'code-host-pull-request-merge', metadata: {
            expected_head_sha: A, observed_head_sha: verified ? A : B,
            merge_commit_sha: verified ? C : '<img src=x onerror=alert(1)>',
          } }], findings: verified ? [] : ['GitHub provider state does not match the action receipt'] } }],
      } });
      return route.fulfill({ status: 404, json: { detail: 'fixture route missing' } });
    });
    await page.goto('http://127.0.0.1:18766/merge-intent-fixture');
    const row = page.locator('[data-action-intent-row="merge-1"]');
    await row.locator('summary').focus();
    await page.keyboard.press('Enter');
    await expect(row).toContainText(`Requested merge head: ${A}`);
    await row.getByRole('button', { name: 'Load side-effect timeline' }).click();
    await expect(row).toContainText(`Provider receipt · attempt 1 · ${verified ? 'completed' : status === 'failed' ? 'failed' : 'unknown'}`);
    if (status === 'failed') await expect(row).toContainText('HTTP 409');
    else {
      await expect(row).toContainText(`Verification receipt · ${verified ? 'verified' : 'not verified'}`);
      await expect(row).toContainText(`observed head ${verified ? A : B}`);
      await expect(row).toContainText(`observed merge commit ${verified ? C : 'not recorded'}`);
    }
    await expect(page.locator('body')).not.toContainText('never-render-this');
    await expect(row.locator('img')).toHaveCount(0);
    expect(calls.every(method => method === 'GET')).toBeTruthy();
  });
}

test('old merge records and omitted pins display missing facts without invented success', async ({ page }) => {
  await page.route('**/merge-intent-fixture', route => route.fulfill({ contentType: 'text/html', body: fixture }));
  await page.route('**/api/**', route => route.fulfill({ json: {
    items: [{ id: 'old', project_id: 'project-a', action_id: 'code-host.pull-request.merge', status: 'uncertain', request: { parameters: {} } }],
  } }));
  await page.goto('http://127.0.0.1:18766/merge-intent-fixture');
  const row = page.locator('[data-action-intent-row="old"]');
  await row.locator('summary').click();
  await expect(row).toContainText('Requested merge head: not recorded');
  await expect(row).toContainText('An omitted pin has no mutation-time head guarantee');
});

for (const status of ['uncertain', 'succeeded']) test(`readiness inspector preserves ${status} without approval or merge claims`, async ({page}) => {
  const calls = [];
  const verified = status === 'succeeded';
  await page.setViewportSize({width:390,height:844});
  await page.route('**/merge-intent-fixture', route => route.fulfill({contentType:'text/html',body:fixture}));
  await page.route('**/api/**', route => {
    calls.push(route.request().method());
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/history')) return route.fulfill({json:{
      receipts:[{outcome:verified?'completed':'unknown',attempt:1,
        result:verified?{action_id:'code-host.pull-request.ready',status:'succeeded',
          output:{head_sha:A,base_ref:'<img src=x onerror=alert(1)>',secret:'never-render-this'}}:null}],
      verifications:[{verified,provider_verification:{verified,evidence:[{
        evidence_type:'code-host-pull-request-ready',metadata:{expected_head_sha:A,
          observed_head_sha:verified?A:B,ready_for_review:verified}}]}}],
    }});
    return route.fulfill({json:{items:[{id:'ready-1',action_id:'code-host.pull-request.ready',
      provider_type:'github',provider_instance:'github.com',status,project_id:'project-a',
      request:{parameters:{pull_request_number:17,expected_head_sha:A,secret:'never-render-this'}},
      retry_policy:{max_attempts:1},attempt:1}]}});
  });
  await page.goto('http://127.0.0.1:18766/merge-intent-fixture');
  const row = page.locator('[data-action-intent-row="ready-1"]');
  await row.locator('summary').focus();
  await page.keyboard.press('Enter');
  await expect(row).toContainText(`Review target PR 17 · expected head: ${A}`);
  await expect(row).toContainText('Independent approval and merge gates still apply');
  await row.getByRole('button',{name:'Load side-effect timeline'}).click();
  await expect(row).toContainText(`Readiness verification: expected head ${A} · observed head ${verified?A:B}`);
  await expect(row).toContainText(`provider ready: ${verified?'yes':'no'}`);
  await expect(row.locator('img')).toHaveCount(0);
  await expect(row).not.toContainText('never-render-this');
  expect(calls.every(method=>method==='GET')).toBeTruthy();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
