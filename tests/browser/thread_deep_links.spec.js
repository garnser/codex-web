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
    home: [
      { id: 'home-thread', name: 'Home thread', projectId: 'home', cwd: '/workspace/home' },
      { id: 'background-thread', name: 'Background thread', projectId: 'home', cwd: '/workspace/home' },
    ],
    alpha: [{ id: 'alpha-thread', name: 'Alpha thread', projectId: 'alpha', cwd: '/workspace/alpha' }],
  };
  const threadTurns = {};
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
    const turnMatch = path.match(/^\/api\/threads\/([^/]+)\/turns$/);
    if (turnMatch && request.method() === 'POST') {
      const threadId = decodeURIComponent(turnMatch[1]);
      const payload = JSON.parse(request.postData() || '{}');
      threadTurns[threadId] = [{
        id: `turn-${threadId}`,
        status: 'inProgress',
        items: [{
          id: `user-${threadId}`,
          type: 'userMessage',
          content: [{ type: 'inputText', text: payload.message }],
        }],
      }];
      await route.fulfill({ json: { queued: false } });
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
      await route.fulfill({ json: {
        thread: {
          ...thread,
          historySource: 'canonical',
          turns: threadTurns[threadId] || [],
        },
      } });
      return;
    }
    await route.fulfill({ json: {} });
  });
  return { reads, threadTurns, threads };
}

test('CLI reply completed while inactive is restored from canonical Thread history', async ({ page }) => {
  const { threadTurns } = await mockChatApi(page);
  await page.goto('http://127.0.0.1:18766/projects/home/chat?thread=home-thread');
  await page.locator('#prompt').fill('Keep this prompt visible.');
  await page.locator('#prompt').press('Enter');
  await expect.poll(() => threadTurns['home-thread']?.length || 0).toBe(1);
  await expect(page.locator('#messages')).toContainText('Keep this prompt visible.');

  await page.getByText('Background thread', { exact: true }).click({ force: true });
  await expect(page.locator('#thread-title')).toHaveText('Background thread');

  threadTurns['home-thread'][0].status = 'completed';
  threadTurns['home-thread'][0].items.push({
    id: 'agent-1',
    type: 'agentMessage',
    text: 'The reply finished while this Thread was inactive.',
  });

  await page.getByText('Home thread', { exact: true }).click({ force: true });
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page.locator('#messages')).toContainText('Keep this prompt visible.');
  await expect(page.locator('#messages')).toContainText(
    'The reply finished while this Thread was inactive.',
  );
});

test('project-scoped Thread deep links, reload, and Back/Forward restore conversation selection', async ({ page }) => {
  const { reads } = await mockChatApi(page);
  await page.goto('http://127.0.0.1:18766/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page).toHaveURL(/projects\/home\/chat\?thread=home-thread/);

  await page.locator('#threads .item-main', { hasText: 'Home thread' }).click({ force: true });
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
  await page.locator('#threads .item-main', { hasText: 'Home thread' }).click({ force: true });
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

async function selectProject(page, projectId) {
  await page.evaluate(id => window.dispatchEvent(new CustomEvent('codex:project-select', {
    detail: { projectId: id },
  })), projectId);
  await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/chat`));
  await expect(page.locator('#thread-title')).toHaveText('Select a thread');
}

for (const action of ['name', 'archive']) {
  test(`late ${action} completion cannot mutate a subsequent visit to the same Thread`, async ({ page }) => {
    await mockChatApi(page);
    let pending;
    await page.route(`**/api/threads/home-thread/${action}?*`, route => { pending = route; });
    await page.goto('http://127.0.0.1:18766/projects/home/chat?thread=home-thread');
    await expect(page.locator('#thread-title')).toHaveText('Home thread');
    await page.locator('#thread-settings-menu > summary').click();
    if (action === 'name') page.once('dialog', dialog => dialog.accept('Old visit rename'));
    await page.locator(action === 'name' ? '#rename-thread' : '#archive-thread').click();
    await expect.poll(() => Boolean(pending)).toBe(true);
    expect(new URL(pending.request().url()).searchParams.get('project_id')).toBe('home');
    await selectProject(page, 'alpha');
    await selectProject(page, 'home');
    await page.locator('#threads .item-main', { hasText: 'Home thread' }).click({ force: true });
    await expect(page.locator('#thread-title')).toHaveText('Home thread');
    await pending.fulfill({ json: {} });
    await page.waitForTimeout(150);
    await expect(page.locator('#thread-title')).toHaveText('Home thread');
    await expect(page).toHaveURL(/thread=home-thread/);
  });
}

test('sending while Thread creation is pending cannot deliver the prompt into another Project', async ({ page }) => {
  const { threadTurns } = await mockChatApi(page);
  let pending;
  await page.route('**/api/threads?**', route => {
    if (route.request().method() === 'POST') pending = route;
    else return route.fallback();
  });
  await page.goto('http://127.0.0.1:18766/projects/home/chat');
  await expect(page.locator('#threads')).toContainText('Home thread');
  await page.locator('#prompt').fill('Original Project prompt');
  await page.locator('#prompt').press('Enter');
  await expect.poll(() => Boolean(pending)).toBe(true);
  expect(new URL(pending.request().url()).searchParams.get('project_id')).toBe('home');
  await selectProject(page, 'alpha');
  await page.locator('#threads .item-main', { hasText: 'Alpha thread' }).click({ force: true });
  await expect(page.locator('#thread-title')).toHaveText('Alpha thread');
  await page.locator('#prompt').fill('New Project draft');
  await pending.fulfill({ json: { thread: { id: 'created-home', projectId: 'home', name: 'Created at Home' } } });
  await page.waitForTimeout(200);
  await expect(page.locator('#thread-title')).toHaveText('Alpha thread');
  await expect(page.locator('#prompt')).toHaveValue('New Project draft');
  await expect(page).toHaveURL(/projects\/alpha\/chat\?thread=alpha-thread/);
  expect(threadTurns).toEqual({});
});

for (const action of ['name', 'archive']) {
  test(`current Thread ${action} completion still updates the selected conversation`, async ({ page }) => {
    await mockChatApi(page);
    await page.goto('http://127.0.0.1:18766/projects/home/chat?thread=home-thread');
    await expect(page.locator('#thread-title')).toHaveText('Home thread');
    await page.locator('#thread-settings-menu > summary').click();
    if (action === 'name') page.once('dialog', dialog => dialog.accept('Current rename'));
    await page.locator(action === 'name' ? '#rename-thread' : '#archive-thread').click();
    await expect(page.locator('#thread-title')).toHaveText(action === 'name' ? 'Current rename' : 'Select a thread');
    if (action === 'archive') await expect(page).not.toHaveURL(/thread=/);
  });
}

test('a previous conversation send failure does not appear in the current conversation', async ({ page }) => {
  await mockChatApi(page);
  let pending;
  await page.route('**/api/threads/home-thread/turns?*', route => { pending = route; });
  await page.goto('http://127.0.0.1:18766/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await page.locator('#prompt').fill('Old conversation message');
  await page.locator('#prompt').press('Enter');
  await expect.poll(() => Boolean(pending)).toBe(true);
  await selectProject(page, 'alpha');
  await page.locator('#threads .item-main', { hasText: 'Alpha thread' }).click({ force: true });
  await expect(page.locator('#thread-title')).toHaveText('Alpha thread');
  await pending.fulfill({ status: 503, json: { detail: 'Old conversation unavailable' } });
  await page.waitForTimeout(150);
  await expect(page.locator('#messages')).not.toContainText('Old conversation unavailable');
});

for (const outcome of ['success', 'failure']) {
  test(`late context compaction ${outcome} cannot overwrite another Project's context status`, async ({ page }) => {
    await mockChatApi(page);
    let pending;
    await page.route('**/api/threads/*/context?*', route => route.fulfill({ json: {
      eligible: true, autoEnabled: true, autoThresholdPercent: 80,
    } }));
    await page.route('**/api/threads/home-thread/compact?*', route => { pending = route; });
    await page.goto('http://127.0.0.1:18766/projects/home/chat?thread=home-thread');
    await expect(page.locator('#compact-context')).toBeEnabled();
    await page.locator('.token-footer > summary').click();
    await page.locator('#compact-context').click();
    await expect.poll(() => Boolean(pending)).toBe(true);
    expect(new URL(pending.request().url()).searchParams.get('project_id')).toBe('home');
    await selectProject(page, 'alpha');
    await page.locator('#threads .item-main', { hasText: 'Alpha thread' }).click({ force: true });
    await expect(page.locator('#context-compact-status')).toHaveText('Auto-compacts at 80%');
    await pending.fulfill({ status: outcome === 'success' ? 200 : 503, json: { detail: 'Old compaction failure' } });
    await page.waitForTimeout(150);
    await expect(page.locator('#context-compact-status')).toHaveText('Auto-compacts at 80%');
  });
}

test('loading earlier activity performs a scoped read for the already selected Thread', async ({ page }) => {
  const { threads } = await mockChatApi(page);
  threads.home[0].messagesTruncated = true;
  threads.home[0].messagesOmitted = 120;
  const readQueries = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.pathname === '/api/threads/home-thread') readQueries.push({
      limit: url.searchParams.get('message_limit'), project: url.searchParams.get('project_id'),
    });
  });
  await page.goto('http://127.0.0.1:18766/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page.locator('#thread-history-control button')).toBeEnabled();
  await page.locator('#thread-history-control button').click();
  await expect.poll(() => readQueries.some(query => query.limit === '80' && query.project === 'home')).toBe(true);
  await expect(page.locator('#thread-history-control button')).toBeEnabled();
  await expect(page).toHaveURL(/projects\/home\/chat\?thread=home-thread/);
});
