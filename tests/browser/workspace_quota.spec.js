const {test, expect} = require('@playwright/test');

test('workspace inspector shows effective limits and refreshes without stale quota data', async ({page}) => {
  const quota = {max_active_per_identity:32, max_active_per_tenant:64,
    max_resources_per_workspace:16, max_requested_disk_bytes:1024};
  let available = true;
  await page.route('**/quota-fixture', route => route.fulfill({contentType:'text/html', body:`
    <!doctype html><html><body><details id="developer-panel" open>
    <button id="refresh-execution-workspaces">Refresh</button>
    <div id="execution-workspace-status" role="status"></div><div id="execution-workspace-list"></div>
    </details><script src="/static/execution_workspace_admin.js"></script></body></html>`}));
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/execution-workspaces/inspection') {
      return route.fulfill({json: {items:[], count:0, ...(available ? {quota} : {})}});
    }
    if (path === '/api/identity/me') return route.fulfill({json:{identity_id:'viewer',roles:['member']}});
    if (path === '/api/projects') return route.fulfill({json:[]});
    return route.fulfill({json:{items:[]}});
  });
  await page.goto('http://127.0.0.1:18766/quota-fixture');
  const status = page.locator('#execution-workspace-status');
  await expect(status).toContainText('32 active per identity · 64 active per tenant');
  await expect(status).toContainText('16 resources per workspace · 1024 requested bytes per workspace');
  available = false;
  await page.getByRole('button', {name:'Refresh'}).click();
  await expect(status).toContainText('Effective workspace limits unavailable.');
  await expect(status).not.toContainText('32 active per identity');
});
