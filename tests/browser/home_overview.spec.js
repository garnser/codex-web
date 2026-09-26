const { test, expect } = require('@playwright/test');

const current = (items = []) => ({
  status: 'current',
  fresh_at: 1800000000,
  items,
  count: items.length,
  detail: null,
});

const populated = {
  project: { id: 'project-a', name: 'Project A', path: '/workspace/project-a' },
  generated_at: 1800000000,
  status: 'current',
  degraded_sections: [],
  sections: {
    project_readiness: current([{ id: 'project-a', title: 'Project A', semantic_ready: true, execution_ready: true, status: 'ready', checks: [] }]),
    attention: current([{ id: 'attention-1', title: 'Human review needed', status: 'open', severity: 'high', owner: 'human-1', updated_at: 1800000000, href: '#workspace/inbox' }]),
    approvals: current([{ id: 'approval-1', title: 'Approve deployment', status: 'pending', owner: 'human-2', updated_at: 1800000000, href: '#approvals' }]),
    active_work: current([{ id: 'work-1', title: 'Ship release', status: 'implementation', stage: 'implementation', owner: 'agent-1', next_action: 'Run release checks', updated_at: 1800000000, href: '#workspace/work' }]),
    blocked_work: current([{ id: 'work-2', title: 'Blocked migration', status: 'blocked', stage: 'implementation', owner: 'agent-2', blocked_reason: 'Awaiting credentials', updated_at: 1800000000, href: '#workspace/work' }]),
    recently_completed: current([{ id: 'work-3', title: 'Closed item', status: 'closed', updated_at: 1799999000, href: '#workspace/work' }]),
    agents: current([{ id: 'session-1', title: 'gpt-5.6', status: 'running', owner: 'worker-1', updated_at: 1800000000, href: '#workspace/agents' }]),
    incidents: current([{ id: 'incident-1', title: 'API degradation', status: 'mitigating', severity: 'high', owner: 'human-3', updated_at: 1800000000, href: '#workspace/operations' }]),
    goals: current([{ id: 'goal-1', title: 'Release safely', status: 'active', health: 'on_track', owner: 'human-4', completion_fraction: 0.5, updated_at: 1800000000, href: '#workspace/goals' }]),
    automations: current([{ id: 'schedule-1', title: 'Nightly verification', status: 'active', updated_at: 1800000000, href: '#workspace/autonomy' }]),
  },
};

async function routeHome(page, payload) {
  await page.route('**/api/home?project_id=project-a', route => route.fulfill({ json: payload }));
  await page.goto('http://127.0.0.1:18766/tests/browser/home_overview_fixture.html');
  await expect.poll(() => page.evaluate(() => Boolean(window.__ready))).toBe(true);
}

test('populated Home identifies Project, current work, owners, next actions and authoritative links', async ({ page }) => {
  await routeHome(page, populated);
  const home = page.locator('[data-home-overview]');
  await expect(home.getByRole('heading', { name: 'Project A' })).toBeVisible();
  await expect(home).toContainText('Human review needed');
  await expect(home).toContainText('Ship release');
  await expect(home).toContainText('Run release checks');
  await expect(home).toContainText('Awaiting credentials');
  await expect(home).toContainText('API degradation');
  await expect(home).toContainText('Release safely');
  await expect(home).toContainText('Nightly verification');
  await expect(home.locator('[data-home-section="project_readiness"]')).toContainText('Start a first task');
  await expect(home.locator('[data-home-section="project_readiness"] a')).toHaveAttribute('href', '#workspace/threads');

  const work = page.locator('[data-home-section="active_work"]');
  await expect(work.getByRole('link', { name: 'Open' })).toHaveAttribute('href', '#workspace/work');
});

test('empty Home intentionally renders empty states without inventing actions', async ({ page }) => {
  const payload = structuredClone(populated);
  for (const key of Object.keys(payload.sections)) payload.sections[key] = current([]);
  payload.sections.project_readiness = current([{ id: 'project-a', title: 'Project A', semantic_ready: true, execution_ready: true, status: 'ready', checks: [] }]);
  await routeHome(page, payload);
  await expect(page.getByText('Nothing here right now')).toHaveCount(Object.keys(payload.sections).length - 1);
  await expect(page.locator('[data-home-overview] a.ghost-button')).toHaveCount(1);
  await expect(page.locator('[data-home-section="project_readiness"]')).toContainText('does not itself mean that useful work has completed');
});

test('degraded source is explicit and suppresses stale actions while current sections stay usable', async ({ page }) => {
  const payload = structuredClone(populated);
  payload.status = 'partial';
  payload.degraded_sections = ['approvals'];
  payload.sections.approvals = {
    status: 'degraded',
    fresh_at: 1799990000,
    items: [{ id: 'stale', title: 'Stale approval', href: '#approvals' }],
    count: 1,
    detail: 'approval source unavailable',
  };
  await routeHome(page, payload);
  await expect(page.getByText('Home is partially degraded')).toBeVisible();
  const approvals = page.locator('[data-home-section="approvals"]');
  await expect(approvals).toContainText('Source degraded');
  await expect(approvals).toContainText('approval source unavailable');
  await expect(approvals.getByRole('link', { name: 'Open' })).toHaveCount(0);
  await expect(page.locator('[data-home-section="active_work"]').getByRole('link', { name: 'Open' })).toBeVisible();
});

test('Home uses a single-column section layout on mobile', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await routeHome(page, populated);
  const columns = await page.locator('.home-overview-grid').evaluate(node => getComputedStyle(node).gridTemplateColumns);
  expect(columns.split(' ').length).toBe(1);
});

test('blocked Project readiness links to setup without treating a click as progress', async ({ page }) => {
  const payload = structuredClone(populated);
  payload.sections.project_readiness = current([{
    id: 'project-a',
    title: 'Project A',
    semantic_ready: false,
    execution_ready: false,
    status: 'blocked',
    checks: [{ id: 'worker', status: 'blocked', message: 'A qualified worker is required.' }],
  }]);
  await routeHome(page, payload);
  const readiness = page.locator('[data-home-section="project_readiness"]');
  await expect(readiness).toContainText('Project setup needs attention');
  await expect(readiness).toContainText('A qualified worker is required.');
  await readiness.getByRole('link', { name: 'Review Project setup' }).click();
  await expect(page).toHaveURL(/#workspace\/setup$/);
  await expect(readiness).toContainText('Project setup needs attention');
});

test('unavailable readiness suppresses next actions', async ({ page }) => {
  const payload = structuredClone(populated);
  payload.sections.project_readiness = {
    status: 'degraded', fresh_at: 1800000000, items: [], count: 0,
    detail: 'Readiness source unavailable.',
  };
  await routeHome(page, payload);
  const readiness = page.locator('[data-home-section="project_readiness"]');
  await expect(readiness).toContainText('Project readiness unavailable');
  await expect(readiness.getByRole('link')).toHaveCount(0);
});

test('permission-limited Home section is explicit without degrading current data', async ({ page }) => {
  const payload = structuredClone(populated);
  payload.sections.automations = {
    status: 'denied',
    fresh_at: 1800000000,
    items: [],
    count: 0,
    detail: 'Scheduler visibility requires an administrator role.',
  };
  await routeHome(page, payload);
  const automations = page.locator('[data-home-section="automations"]');
  await expect(automations).toContainText('Not available to this identity');
  await expect(automations).toContainText('administrator role');
  await expect(page.getByText('Home is partially degraded')).toHaveCount(0);
});
