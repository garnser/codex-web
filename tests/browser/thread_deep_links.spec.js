const { test, expect } = require('@playwright/test');

async function mockChatApi(page) {
  const fs = require('fs');
  const path = require('path');
  const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8')
    .replace('<head>', '<head><base href="/">');
  await page.route('**/projects/**', async (route) => {
    if (route.request().resourceType() !== 'document') return route.continue();
    await route.fulfill({ status: 200, contentType: 'text/html', body: html });
  });
  const projects = [
    { id: 'home', name: 'Home', path: '/workspace/home' },
    { id: 'alpha', name: 'Alpha', path: '/workspace/alpha' },
  ];
  const threads = {
    home: [{ id: 'home-thread', name: 'Home thread', projectId: 'home', cwd: '/workspace/home' }],
    alpha: [{ id: 'alpha-thread', name: 'Alpha thread', projectId: 'alpha', cwd: '/workspace/alpha' }],
  };
  const reads = [];
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/projects') {
      await route.fulfill({ json: projects });
      return;
    }
    const stateMatch = path.match(/^\/api\/projects\/([^/]+)\/ui-state$/);
    if (stateMatch) {
      const projectId = decodeURIComponent(stateMatch[1]);
      await route.fulfill({ json: {
        project: projects.find((project) => project.id === projectId),
        executionProfiles: [],
        resources: { items: [] },
        bindings: { items: [] },
        threadSettings: {},
        channels: { items: [] },
        threads: { data: threads[projectId] || [] },
      } });
      return;
    }
    if (path === '/api/models') {
      await route.fulfill({ json: { data: [] } });
      return;
    }
    if (path === '/api/threads' && request.method() === 'GET') {
      const projectId = url.searchParams.get('project_id');
      const term = url.searchParams.get('search') || '';
      await route.fulfill({ json: { data: (threads[projectId] || []).filter((thread) => thread.id.includes(term)) } });
      return;
    }
    const threadMatch = path.match(/^\/api\/threads\/([^/]+)$/);
    if (threadMatch) {
      const threadId = decodeURIComponent(threadMatch[1]);
      reads.push(threadId);
      const thread = Object.values(threads).flat().find((item) => item.id === threadId);
      if (!thread) {
        await route.fulfill({ status: 404, json: { detail: 'Thread not found' } });
        return;
      }
      await route.fulfill({ json: { ...thread, turns: [] } });
      return;
    }
    await route.fulfill({ json: {} });
  });
  return { reads };
}

test('project-scoped Thread deep links, reload, and Back/Forward restore conversation selection', async ({ page }) => {
  const { reads } = await mockChatApi(page);
  await page.goto('http://127.0.0.1:18766/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page).toHaveURL(/projects\/home\/chat\?thread=home-thread/);

  await page.locator('#threads .item-main').click({ force: true });
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page).toHaveURL(/thread=home-thread/);
  await page.evaluate(() => window.CodexProductUI.openWorkspace('overview'));
  await expect(page).toHaveURL(/projects\/home\/overview$/);
  await page.goBack();
  await expect(page).toHaveURL(/projects\/home\/chat\?thread=home-thread/);
  await expect(page.locator('#thread-title')).toHaveText('Home thread');

  await page.goto('http://127.0.0.1:18766/projects/alpha/chat?thread=alpha-thread');
  await page.evaluate(() => sessionStorage.clear());
  await page.reload();
  await expect(page.locator('#thread-title')).toHaveText('Alpha thread');
  await page.reload();
  await expect(page.locator('#thread-title')).toHaveText('Alpha thread');
  await expect.poll(() => reads.filter((id) => id === 'alpha-thread').length).toBeGreaterThanOrEqual(2);
});

test('Thread selection history is restored and Project switching drops the previous Thread', async ({ page }) => {
  const { reads } = await mockChatApi(page);
  await page.goto('http://127.0.0.1:18766/projects/home/chat');
  await page.locator('#threads .item-main').click({ force: true });
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await page.goBack();
  await expect(page).not.toHaveURL(/thread=home-thread/);
  await expect(page.locator('#thread-title')).toHaveText('Select a thread');
  await page.goForward();
  await expect(page).toHaveURL(/thread=home-thread/);
  await expect(page.locator('#thread-title')).toHaveText('Home thread');

  await page.evaluate(() => window.dispatchEvent(new CustomEvent('codex:project-select', {
    detail: { projectId: 'alpha' },
  })));
  await expect(page).toHaveURL(/projects\/alpha\/chat(?:\?.*)?$/);
  await expect(page).not.toHaveURL(/thread=home-thread/);
  await expect(page.locator('#thread-title')).toHaveText('Select a thread');
  expect(reads.at(-1)).toBe('home-thread');
});

test('a Thread from another Project is rejected visibly without loading its conversation', async ({ page }) => {
  const { reads } = await mockChatApi(page);
  await page.goto('http://127.0.0.1:18766/projects/alpha/chat?thread=home-thread');
  await expect(page.locator('#messages')).toContainText('This Thread is unavailable in the active Project.');
  await expect(page).not.toHaveURL(/thread=home-thread/);
  expect(reads).not.toContain('home-thread');
});
