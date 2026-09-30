const { resolveAction } = require('./action_confirmation_helpers');
const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const index = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const marker = index.indexOf('id="refresh-work-graph"');
const start = index.lastIndexOf('<div class="developer-card">', marker);
const card = index.slice(start, index.indexOf('<div class="developer-card">', marker));
function graph(project) {
  return { project_id: project, nodes: project === 'empty' ? [] : [1, 2].map(n => ({
    ref: `${project}-${n}`, title: `${project} item ${n}`, project_id: project,
    stage: 'implementation_active', readiness: { status: 'runnable' },
  })), edges: [], progress: {}, critical_path: { refs: [] }, runnable_refs: [], failure_impacts: [] };
}
async function mount(page, onRequest) {
  await page.route('**/projects/project-a/work-items', route => route.fulfill({
    contentType: 'text/html', body: `<meta charset="utf-8"><body data-project-id="project-a">
      <details id="developer-panel"></details><section data-product-workspace-panel="work">${card}</section>
      <script src="/static/work_graph_admin.js"></script></body>`,
  }));
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (onRequest && await onRequest(route, url)) return;
    if (url.pathname === '/api/identity/me') return route.fulfill({ json: {
      identity_id: 'operator', principal_kind: 'human', assurance: 'mfa', roles: ['admin'],
    } });
    if (url.pathname.startsWith('/api/projects/')) return route.fulfill({ json: {
      id: url.pathname.split('/').pop(), name: 'Selected Project',
    } });
    if (url.pathname.startsWith('/api/work-graph/projects/')) return route.fulfill({ json: { graph: graph(url.pathname.split('/').pop()) } });
    return route.fulfill({ json: { items: [], refs: [] } });
  });
  await page.goto('http://127.0.0.1:18766/projects/project-a/work-items');
  await expect(page.locator('#work-graph-status')).toContainText('2 of 2');
  await page.locator('#work-graph-panel').evaluate(node => { node.open = true; });
}
async function changeProject(page, projectId) {
  await page.evaluate(id => {
    history.replaceState({}, '', `/projects/${id}/work-items`);
    document.body.dataset.projectId = id;
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: id } }));
  }, projectId);
}
test('Work Graph follows Project context and clears filters, focus, counts and mutation targets', async ({ page }) => {
  await mount(page, async (route, url) => {
    if (url.pathname.endsWith('/foreign')) {
      await route.fulfill({ status: 404, json: { detail: 'Project not found' } }); return true;
    }
  });
  await expect(page.locator('#work-graph-project')).toHaveValue('project-a');
  await expect(page.locator('#work-graph-project')).toBeDisabled();
  await page.locator('#work-graph-search').fill('project-a');
  await changeProject(page, 'project-c');
  await expect(page.locator('#work-graph-svg')).toContainText('project-c item 1');
  await expect(page.locator('#work-graph-svg')).not.toContainText('project-a');
  await expect(page.locator('#work-graph-search')).toHaveValue('');
  await expect(page.locator('#work-graph-source')).toHaveValue('project-c-1');
  await changeProject(page, 'empty');
  await expect(page.locator('#work-graph-status')).toContainText('0 of 0');
  await expect(page.locator('#work-graph-source option')).toHaveCount(0);
  await expect(page.locator('#work-graph-svg')).toContainText('No nodes');
  await changeProject(page, 'project-a');
  await expect(page.locator('#work-graph-source')).toHaveValue('project-a-1');
  await changeProject(page, 'foreign');
  await expect(page.locator('#work-graph-status')).toContainText('Project not found');
  await expect(page.locator('#add-work-graph-edge')).toBeDisabled();
  await expect(page.locator('#work-graph-source option')).toHaveCount(0);
});
test('delayed graph response cannot overwrite a newer Project', async ({ page }) => {
  await page.addInitScript(() => {
    const original = fetch;
    window.fetch = (url, options = {}) => original(url, { ...options, signal: undefined });
  });
  let hold = false, requested = false, release;
  const pending = new Promise(resolve => { release = resolve; });
  await mount(page, async (route, url) => {
    if (hold && url.pathname === '/api/work-graph/projects/project-a') {
      requested = true; await pending;
      await route.fulfill({ json: { graph: graph('project-a') } }); return true;
    }
  });
  hold = true;
  await page.locator('#refresh-work-graph').click();
  await expect.poll(() => requested).toBe(true);
  await changeProject(page, 'project-c');
  await expect(page.locator('#work-graph-svg')).toContainText('project-c item 1');
  const response = page.waitForResponse(url => url.url().endsWith('/work-graph/projects/project-a'));
  release(); await response;
  await expect(page.locator('#work-graph-svg')).not.toContainText('project-a');
  await expect(page.locator('#work-graph-source')).toHaveValue('project-c-1');
});
test('delayed traversal cannot filter the new Project graph', async ({ page }) => {
  let requested = false, release;
  const params = [];
  const pending = new Promise(resolve => { release = resolve; });
  await mount(page, async (route, url) => {
    if (url.pathname === '/api/work-graph/traverse') {
      params.push(Object.fromEntries(url.searchParams));
      requested = true; await pending;
      await route.fulfill({ json: { refs: ['project-a-1'] } }); return true;
    }
  });
  await page.locator('[data-work-graph-node="project-a-1"]').click();
  await expect.poll(() => requested).toBe(true);
  await changeProject(page, 'project-c');
  await expect(page.locator('#work-graph-svg')).toContainText('project-c item 1');
  const response = page.waitForResponse(url => url.url().includes('/work-graph/traverse'));
  release(); await response;
  await page.waitForTimeout(100);
  expect(params).toHaveLength(2);
  expect(params.every(value => value.project_id === 'project-a')).toBe(true);
  await expect(page.locator('#work-graph-status')).toContainText('2 of 2');
  await expect(page.locator('#work-graph-node-detail')).not.toContainText('project-a');
});
test('edge mutation uses explicit Project context and its late receipt cannot refresh another Project', async ({ page }) => {
  let requested = false, release;
  const writes = [];
  const pending = new Promise(resolve => { release = resolve; });
  await mount(page, async (route, url) => {
    if (route.request().method() === 'POST') {
      writes.push({ project: url.searchParams.get('project_id'), body: route.request().postDataJSON() });
      requested = true; await pending;
      await route.fulfill({ json: { edge: { id: 'edge-a' } } }); return true;
    }
  });
  await page.locator('#work-graph-management-panel').evaluate(node => { node.open = true; });
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#add-work-graph-edge').click(); await resolveAction(page);
  await expect.poll(() => requested).toBe(true);
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ project: 'project-a', body: { source_ref: 'project-a-1', target_ref: 'project-a-2' } });
  await changeProject(page, 'project-c');
  await expect(page.locator('#work-graph-source')).toHaveValue('project-c-1');
  const response = page.waitForResponse(url => url.url().includes('/work-graph/edges'));
  release(); await response;
  await expect(page.locator('#work-graph-management-status')).toBeEmpty();
  await expect(page.locator('#work-graph-source')).toHaveValue('project-c-1');
});
