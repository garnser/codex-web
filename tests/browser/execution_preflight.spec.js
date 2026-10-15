const { test, expect } = require('@playwright/test');

for (const phase of ['captured', 'reconciled']) {
  test(`retired scheduling preserves later ${phase} positive native outcome without replay`, async ({page}) => {
    const status = phase === 'reconciled' ? 'started' : 'reconciliation_required';
    await page.goto(`http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=${status}&retired&nativeCorrelation&positiveEvidence`);
    await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
    const card = page.locator('.message.tool');
    await expect(card).toContainText('Queue scheduling remains retired.');
    await expect(card).not.toContainText('The earlier native start outcome remains unknown');
    await expect(card).toContainText(phase === 'reconciled'
      ? 'Scheduling retired · native start confirmed' : 'Scheduling retired · native receipt pending');
    await expect(page.getByRole('button', {name:'Retry', exact:true})).toHaveCount(0);
    await expect(page.getByRole('button', {name:'Reconcile captured native start', exact:true}))
      .toHaveCount(phase === 'captured' ? 1 : 0);
  });
}

for (const state of ['unknown', 'captured', 'reconciled']) {
  test(`exact native attribution shows ${state} without replay`, async ({ page }) => {
    const disposition = state === 'reconciled' ? 'started' : 'reconciliation_required';
    await page.goto(`http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=${disposition}&nativeCorrelation=1${state === 'unknown' ? '' : '&positiveEvidence=1'}`);
    await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
    const card = page.locator('.message.tool');
    await expect(card).toContainText('generation-original / rpc-original');
    await expect(card).toContainText('org-original/workspace-original · Project project-original · actor original-actor');
    await expect(card).toContainText('original-assignment · worker original-worker · send fence 7');
    await expect(card).toContainText('Resources at send: repo-app, repo-docs');
    await expect(page.getByRole('button', {name:'Retry', exact:true})).toHaveCount(0);
    const reconcile = page.getByRole('button', {name:'Reconcile captured native start', exact:true});
    if (state === 'captured') {
      await expect(card).toContainText('Authoritative native response captured');
      await expect(reconcile).toBeVisible();
      await reconcile.click();
      expect(await page.evaluate(() => window.__apiCalls.filter(x => x.method === 'POST').map(x => x.path)))
        .toEqual(['/api/threads/thread-1/preflight-attempts/preflight-thread-turn-1/reconcile-prestart']);
    } else {
      await expect(reconcile).toHaveCount(0);
      await expect(card).toContainText(state === 'unknown' ? 'native outcome remains unknown' : 'Exact positive evidence reconciled');
    }
  });
}

for (const disposition of ['queued', 'reconciliation_required']) {
  test(`retained ${disposition} exposes original provenance without an unsafe retry`, async ({ page }) => {
    await page.goto(`http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=${disposition}`);
    await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
    await expect(page.locator('.message.tool')).toContainText('Original queued request: original-queue-id');
    await expect(page.locator('.message.tool')).toContainText('Agent profile: original-profile · revision 6');
    await expect(page.getByRole('button', {name:'Retry', exact:true})).toHaveCount(0);
    if (disposition === 'reconciliation_required') {
      await expect(page.locator('.message.tool')).toContainText('Native start outcome is unknown');
    }
  });
}

test('retained execution preflight renders blocker and authorized retry', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);

  await expect(page.locator('.message.user')).toContainText('make the requested change');
  await expect(page.locator('.message.tool')).toContainText('worker_capability_missing');
  await expect(page.locator('.message.tool')).toContainText('Restore a qualified worker.');
  await expect(page.locator('.message.tool')).toContainText('Correlation: thread-turn-1');
  await expect(page.locator('.message.tool')).toContainText('Profile: repository-write');
  await expect(page.locator('.message.tool')).toContainText('Repository: repo-app');
  await expect(page.locator('.message.tool')).toContainText('Read-only repositories: repo-docs');
  await expect(page.locator('.message.tool')).toContainText('Sandbox: workspace-write');
  await expect(page.locator('.message.tool')).toContainText('Approval: on-request');

  const retry = page.getByRole('button', { name: 'Retry' });
  await expect(retry).toBeVisible();
  await retry.click();

  await expect.poll(() => page.evaluate(() => window.__apiCalls.filter(
    (item) => item.method === 'POST'
  ).length)).toBe(1);
  expect(await page.evaluate(() => window.__reloadCalls)).toEqual(['thread-1']);
  expect(await page.evaluate(() => window.__refreshCalls)).toEqual([0]);
});

test('preflight handler reloads retained state instead of showing a dead turn', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);

  const handled = await page.evaluate(() => window.__ui.handleError({
    detail: { code: 'execution_preflight_blocked' },
  }, 'thread-1'));

  expect(handled).toBe(true);
  expect(await page.evaluate(() => window.__reloadCalls)).toEqual(['thread-1']);
  expect(await page.evaluate(() => window.__refreshCalls)).toEqual([0]);
});


test('repository target authorization and stale-state blockers remain canonical', async ({ page }) => {
  for (const scenario of [
    {
      query: 'unauthorized',
      code: 'repository_target_unauthorized',
      message: 'not bound to this Project',
      target: 'repository/repo-app',
    },
    {
      query: 'stale',
      code: 'repository_target_unauthorized',
      message: 'repository is not active',
      target: 'repository/repo-stale',
    },
  ]) {
    await page.goto(
      'http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html'
      + `?repositoryBlocker=${scenario.query}`
    );
    await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
    const card = page.locator('.message.tool').last();
    await expect(card).toContainText(scenario.code);
    await expect(card).toContainText(scenario.message);
    await expect(card).toContainText(`Target: ${scenario.target}`);
    await expect(card).toContainText('Repository: repo-app');
  }
});

for (const outcome of ['resolve', 'reject']) {
  test(`late preflight retry ${outcome} does not reload or show errors in a later Project visit`, async ({ page }) => {
    await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?holdRetry');
    await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: 'a' } })));
    await page.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect.poll(() => page.evaluate(() => Boolean(window.__finishRetry))).toBe(true);
    await page.evaluate(outcome => {
      for (const projectId of ['b', 'a']) window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId } }));
      window.__finishRetry[outcome](outcome === 'reject' ? new Error('Old retry failed') : {});
    }, outcome);
    await page.waitForTimeout(100);
    expect(await page.evaluate(() => window.__reloadCalls)).toEqual([]);
    expect(await page.evaluate(() => window.__refreshCalls)).toEqual([]);
    await expect(page.locator('#messages')).not.toContainText('Old retry failed');
  });
}

 test('replacement retry follows canonical current target and exposes original provenance', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?replacement');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await expect(page.locator('.message.tool')).toContainText('Original thread: thread-1; current thread: replacement-thread');
  await page.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.__reloadCalls)).toEqual(['replacement-thread']);
});

test('confirmed retained start shows actual native receipt without a replay action', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=started');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await expect(page.locator('.message.tool')).toContainText('Confirmed native turn: actual-native-turn');
  await expect(page.locator('.message.tool')).toContainText('Requested execution: original-request-execution; admitted execution: actual-bootstrap-execution');
  await expect(page.getByRole('button', {name:'Retry', exact:true})).toHaveCount(0);
});

test('known pre-start refusal offers reconciliation without automatically replaying the request', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=reconciliation_required&knownRefusal');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await expect(page.getByRole('button', {name:'Retry', exact:true})).toHaveCount(0);
  await page.getByRole('button', {name:'Reconcile unstarted refusal', exact:true}).click();
  await expect.poll(() => page.evaluate(() => window.__apiCalls.filter(item => item.method === 'POST').length)).toBe(1);
  expect(await page.evaluate(() => window.__apiCalls.find(item => item.method === 'POST').path)).toContain('/reconcile-prestart');
  expect(await page.evaluate(() => window.__reloadCalls)).toEqual(['thread-1']);
});

test('terminal scheduling retirement is distinct from the unknown native outcome', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=reconciliation_required&retired');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await expect(page.locator('.message.tool')).toContainText('Scheduling retired · native outcome unknown');
  await expect(page.locator('.message.tool')).toContainText('earlier native start outcome remains unknown');
  await expect(page.locator('.message.tool')).toContainText('provider merged · canonical closed');
  await expect(page.getByRole('button', {name:'Retry', exact:true})).toHaveCount(0);
  await expect(page.getByRole('button', {name:'Retire terminal scheduling', exact:true})).toHaveCount(0);
});

test('eligible unknown scheduling exposes the bounded admin retirement action', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=reconciliation_required&retireEligible');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await page.getByRole('button', {name:'Retire terminal scheduling', exact:true}).click();
  await expect.poll(() => page.evaluate(() => window.__apiCalls.filter(item => item.method === 'POST').length)).toBe(1);
  expect(await page.evaluate(() => window.__apiCalls.find(item => item.method === 'POST').path)).toContain('/retire-scheduling');
  expect(await page.evaluate(() => window.__reloadCalls)).toEqual(['thread-1']);
});


test('existing retained inspector attaches reviewed single-original admission', async ({page}) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/execution_preflight_fixture.html?disposition=queued&operatorAdmission');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
  await page.getByRole('button', {name:'Review single original admission'}).click();
  await expect(page.locator('.message.tool')).toContainText('Actor: original-admin');
  await expect(page.locator('.message.tool')).toContainText('Read-only repositories: repo-docs');
  await expect(page.getByRole('button', {name:'Admit this original once'})).toBeDisabled();
  expect(await page.evaluate(() => window.__apiCalls.filter(call => call.method === 'POST').length)).toBe(0);
});
