const { test, expect } = require('@playwright/test');

const configured = {
  can_configure: true, status: 'configured',
  binding: { thread_id: 'same-thread', revision: 4, selected_by: 'admin' },
  thread: { id: 'same-thread', name: 'Local operator', execution_profile_id: 'orchestration-admin',
    sandbox: 'workspace-write', approval_policy: 'on-request',
    href: '/projects/home?thread=same-thread#workspace/threads' },
};

test('Home shortcut opens the same thread and saves with canonical revision without changing authority', async ({ page }) => {
  const mutations = [];
  let state = structuredClone(configured);
  await page.route('**/api/home/operator', async route => {
    if (route.request().method() === 'PUT') {
      mutations.push(route.request().postDataJSON());
      state = { ...state, binding: { ...state.binding, revision: 5 } };
    }
    await route.fulfill({ json: state });
  });
  await page.route('**/api/threads?project_id=home&limit=50', route => route.fulfill({
    json: { data: [{ id: 'same-thread', name: 'Local operator' }] },
  }));
  await page.goto('http://127.0.0.1:18766/tests/browser/home_operator_fixture.html');
  await expect(page.getByRole('link', { name: 'Open operator conversation' })).toHaveAttribute(
    'href', '/projects/home?thread=same-thread#workspace/threads');
  await expect(page.getByText(/This shortcut grants no host access/)).toBeVisible();
  await page.getByRole('button', { name: 'Choose conversation' }).click();
  await page.getByLabel('Existing Home conversation').selectOption('same-thread');
  await page.getByRole('button', { name: 'Save Home shortcut' }).click();
  await expect.poll(() => mutations).toEqual([{ thread_id: 'same-thread', expected_revision: 4 }]);
});

test('unavailable saved identity stays explicit; non-admin cannot configure it', async ({ page }) => {
  await page.route('**/api/home/operator', route => route.fulfill({ json: {
    can_configure: false, status: 'unavailable', binding: configured.binding, thread: null,
  } }));
  await page.goto('http://127.0.0.1:18766/tests/browser/home_operator_fixture.html');
  await expect(page.getByText(/identity is retained; no replacement/)).toBeVisible();
  await expect(page.getByRole('link')).toHaveCount(0);
  await expect(page.getByRole('button')).toHaveCount(0);
});

test('unknown save is never retried and stale replacement requires refresh', async ({ page }) => {
  let calls = 0;
  await page.route('**/api/home/operator', async route => {
    if (route.request().method() === 'PUT') {
      calls += 1;
      await route.fulfill({ status: 409, json: { detail: 'Operator selection changed' } });
    } else await route.fulfill({ json: configured });
  });
  await page.route('**/api/threads?project_id=home&limit=50', route => route.fulfill({
    json: { data: [{ id: 'same-thread', name: 'Local operator' }] },
  }));
  await page.goto('http://127.0.0.1:18766/tests/browser/home_operator_fixture.html');
  await page.getByRole('button', { name: 'Choose conversation' }).click();
  await page.getByLabel('Existing Home conversation').selectOption('same-thread');
  await page.getByRole('button', { name: 'Save Home shortcut' }).click();
  await expect(page.getByRole('status')).toContainText('Refresh Home before trying again');
  await expect(page.getByRole('button', { name: 'Save Home shortcut' })).toBeDisabled();
  expect(calls).toBe(1);
});
