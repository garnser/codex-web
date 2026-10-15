const { test, expect } = require('@playwright/test');
const {baseURL,openSettings}=require('./context_picker_fixture');

for (const outcome of ['success', 'failure']) {
  test(`project switch clears previous threads while the new list is pending (${outcome})`, async ({ page }) => {
    await mockChatApi(page);
    let pending;
    await page.route('**/api/projects/alpha/ui-state?*', route => { pending = route; });
    await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
    await expect(page.locator('#thread-title')).toHaveText('Home thread');
    await expect(page.locator('#threads .item')).toHaveCount(2);

    const visibleImmediately = await page.evaluate(() => {
      window.dispatchEvent(new CustomEvent('codex:project-select', { detail: { projectId: 'alpha' } }));
      return document.getElementById('threads').textContent;
    });
    expect(visibleImmediately).not.toContain('Home thread');
    expect(visibleImmediately).not.toContain('Background thread');
    await expect.poll(() => Boolean(pending)).toBe(true);
    await expect(page.locator('#threads .item')).toHaveCount(0);
    await expect(page.locator('#thread-title')).toHaveText('Select a thread');
    await expect(page.locator('#messages')).not.toContainText('Loaded home-thread');

    if (outcome === 'success') {
      await pending.fallback();
      await expect(page.locator('#threads')).toContainText('Alpha thread');
    } else {
      await pending.fulfill({ status: 503, json: { detail: 'Project list unavailable' } });
      await expect(page.locator('#messages')).toContainText('Project list unavailable');
      await expect(page.locator('#threads .item')).toHaveCount(0);
    }
    await expect(page.locator('#threads')).not.toContainText('Home thread');
  });
}

test('browser Back clears current threads and fences a late project response', async ({ page }) => {
  await mockChatApi(page);
  let pending;
  await page.route('**/api/projects/alpha/ui-state?*', route => { pending = route; });
  await page.goto(baseURL+'/projects/home/chat');
  await expect(page.locator('#threads')).toContainText('Home thread');
  await page.locator('#product-project-switcher').selectOption('alpha');
  await expect.poll(() => Boolean(pending)).toBe(true);
  await expect(page.locator('#threads .item')).toHaveCount(0);
  await page.goBack();
  await expect(page.locator('#threads')).toContainText('Home thread');
  await pending.fallback();
  await expect(page.locator('body')).toHaveAttribute('data-project-id', 'home');
  await expect(page.locator('#threads')).not.toContainText('Alpha thread');
});

test('same-project refresh retains its threads while a new list is pending', async ({ page }) => {
  await mockChatApi(page);
  await page.goto(baseURL+'/projects/home/chat');
  await expect(page.locator('#threads .item')).toHaveCount(2);
  let pending;
  await page.route('**/api/projects/home/ui-state?*', route => { pending = route; });
  await page.locator('#refresh').click();
  await expect.poll(() => Boolean(pending)).toBe(true);
  await expect(page.locator('#threads .item')).toHaveCount(2);
  await pending.fallback();
  await expect(page.locator('#threads')).toContainText('Home thread');
});

test('the thread list omits rows explicitly belonging to another Project', async ({ page }) => {
  const { threads } = await mockChatApi(page);
  threads.alpha.push(threads.home[0]);
  await page.goto(baseURL+'/projects/alpha/chat');
  await expect(page.locator('#threads')).toContainText('Alpha thread');
  await expect(page.locator('#threads .item')).toHaveCount(1);
  await expect(page.locator('#threads')).not.toContainText('Home thread');
});

async function mockChatApi(page, { degradeFirstThreadRead = false, simulateReply = false, queued = false, runners = {}, retainedTurns = {}, threadStatuses = {}, activities = {} } = {}) {
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
  for (const rows of Object.values(threads)) for (const thread of rows) {
    if (runners[thread.id]) thread.runner = runners[thread.id];
    if (threadStatuses[thread.id]) thread.status = {type:threadStatuses[thread.id]};
  }
  const threadTurns = {...retainedTurns};
  const reads = [];
  let replyCompleted = false;
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
      if (simulateReply) setTimeout(() => { replyCompleted = true; }, 100);
      threadTurns[threadId] = [{
        id: `turn-${threadId}`,
        status: 'inProgress',
        items: [{
          id: `user-${threadId}`,
          type: 'userMessage',
          content: [{ type: 'inputText', text: payload.message }],
        }],
      }];
      await route.fulfill({ json: { queued, queuedId: queued ? 'queued-care' : null, queueDepth: queued ? 1 : 0, turn: { id: `turn-${threadId}` } } });
      return;
    }
    const runnerMatch = path.match(/^\/api\/threads\/([^/]+)\/runner$/);
    if (runnerMatch) {
      const id = decodeURIComponent(runnerMatch[1]);
      await route.fulfill({json:{id,runner:runners[id] || null,activity:activities[id],
        status:{type:activities[id]?.state === 'active' ? 'active' : 'idle'}}});
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
      if (degradeFirstThreadRead && reads.length === 1) {
        await route.fulfill({ json: {
          timedOut: true,
          error: 'Thread temporarily unavailable',
          thread: { ...thread, turns: [], status: { type: 'notLoaded' }, readTimedOut: true },
        } });
        return;
      }
      const turns = threadTurns[threadId] || [{
        items: [{ type: 'agentMessage', text: (
          simulateReply && threadId === 'home-thread' && replyCompleted
            ? 'Reply completed while another thread was selected'
            : `Loaded ${threadId}`
        ) }],
      }];
      if (simulateReply && threadId === 'home-thread' && replyCompleted) {
        turns[0].items = [{
          type: 'agentMessage',
          text: 'Reply completed while another thread was selected',
        }];
      }
      await route.fulfill({ json: {
        thread: {
          ...thread,
          historySource: 'canonical',
          turns,
        },
      } });
      return;
    }
    await route.fulfill({ json: {} });
  });
  return { reads, threadTurns, threads };
}

for(const theme of ['light','dark']) test(`runner badges agree between real sidebar and selected header (${theme})`,async({page})=>{
  const now=Date.now()/1000;
  const runner=id=>({schemaVersion:'1.0',assignmentId:id,fence:2,assignmentCreatedAt:now-5,
    observedAt:now,updatedAt:now,state:'running',live:true,processRunning:true,leaseValid:true,leaseExpiresAt:now+3600});
  const activities={'home-thread':{source:'current_turn',state:'active',turnId:'current-turn'},
    'background-thread':{source:'current_turn',state:'idle',turnId:null}};
  const mock=await mockChatApi(page,{runners:{'home-thread':runner('home'),'background-thread':runner('background')},
    activities,threadStatuses:{'home-thread':'active','background-thread':'idle'},
    retainedTurns:{'background-thread':[{id:'retained',status:'inProgress',items:[]}]}});
  for(const thread of mock.threads.home)thread.activity=activities[thread.id];
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;},theme);
  await expect(page.locator('#thread-meta .runner-active')).toContainText('active work');
  const home=page.locator('#threads .item',{hasText:'Home thread'});
  await expect(home.locator('.runner-active')).toContainText('active work');
  const idle=page.locator('#threads .item',{hasText:'Background thread'});
  await expect(idle.locator('.runner-idle')).toContainText('idle');
  await idle.locator('.item-main').click();
  await expect(page.locator('#thread-meta .runner-idle')).toContainText('idle');
  await expect(page.locator('#thread-meta .runner-active')).toHaveCount(0);
  await expect(idle.locator('.runner-active')).toHaveCount(0);
  expect(await home.locator('[data-runner-state]').first().evaluate(node=>getComputedStyle(node).backgroundColor))
    .not.toBe(await idle.locator('[data-runner-state]').first().evaluate(node=>getComputedStyle(node).backgroundColor));
});

test('CLI reply completed while inactive is restored from canonical Thread history', async ({ page }) => {
  const { threadTurns } = await mockChatApi(page);
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#messages')).toContainText('Loaded home-thread');
  await page.locator('#prompt').fill('Keep this prompt visible.');
  await page.locator('#prompt').press('Enter');
  await expect.poll(() => threadTurns['home-thread']?.length || 0).toBe(1);
  await expect(page.locator('#messages')).toContainText('Keep this prompt visible.');

  await page.locator('#threads .item-main', {hasText:'Background thread'}).click();
  await expect(page.locator('#thread-title')).toHaveText('Background thread');

  threadTurns['home-thread'][0].status = 'completed';
  threadTurns['home-thread'][0].items.push({
    id: 'agent-1',
    type: 'agentMessage',
    text: 'The reply finished while this Thread was inactive.',
  });

  await page.locator('#threads .item-main', {hasText:'Home thread'}).click();
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page.locator('#messages')).toContainText('Keep this prompt visible.');
  await expect(page.locator('#messages')).toContainText(
    'The reply finished while this Thread was inactive.',
  );
});

test('usage panel renders provider resources and only meters authoritative denominators', async ({ page }) => {
  await mockChatApi(page);
  await page.route('**/api/account/rate-limits', route => route.fulfill({ json: {
    usage_resources: [{
      resource_id: 'codex:primary', label: '5 hour rate limit', kind: 'rate_limit',
      consumed: 64, limit: 100, remaining: 36, unit: 'percent', currency: null,
      source: 'provider_reported', authoritative: true, measurement_mode: 'snapshot',
      provider_id: 'openai', runtime_id: 'codex', reset_at: 2000000000,
    }],
  } }));
  await page.route('**/api/agent-runtime-usage?*', route => route.fulfill({ json: {
    items: [], count: 0, resources: [
      {
        resource_id: 'budget', label: 'Monthly spend', kind: 'money',
        consumed: 14.62, limit: 25, remaining: 10.38, unit: 'currency', currency: 'USD',
        source: 'provider_reported', authoritative: true, measurement_mode: 'snapshot',
        provider_id: 'mammouth', runtime_id: 'mammouth-code', model_id: 'large',
      },
      {
        resource_id: 'tokens', label: 'Usage this period', kind: 'token_usage',
        consumed: 184230, limit: null, remaining: null, unit: 'tokens', currency: null,
        source: 'provider_reported', authoritative: true, measurement_mode: 'increment',
        provider_id: 'another-provider', runtime_id: 'runtime', model_id: 'model-a',
      },
      {
        resource_id: 'balance', label: 'Mammouth balance', kind: 'money',
        consumed: null, limit: null, remaining: 10.38, unit: 'currency', currency: 'EUR',
        source: 'provider_reported', authoritative: true, measurement_mode: 'snapshot',
        provider_id: 'mammouth', runtime_id: 'mammouth-code', account_id: 'account-7',
      },
    ],
  } }));

  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await page.locator('.token-footer > summary').click();
  await expect(page.locator('#usage-resources .usage-resource')).toHaveCount(4);
  await expect(page.locator('[data-resource-id="codex:primary"] [role="progressbar"]')).toHaveAttribute('aria-valuenow', '64');
  await expect(page.locator('[data-resource-id="budget"] [role="progressbar"]')).toHaveAttribute('aria-valuenow', '58');
  await expect(page.locator('[data-resource-id="tokens"]')).toContainText('184k tokens');
  await expect(page.locator('[data-resource-id="tokens"] [role="progressbar"]')).toHaveCount(0);
  await expect(page.locator('[data-resource-id="balance"]')).toContainText(/€10\.38|10\.38\s*€/);
  await expect(page.locator('[data-resource-id="balance"] [role="progressbar"]')).toHaveCount(0);
  await expect(page.locator('[data-resource-id="budget"]')).toContainText('mammouth · large');
});

test('project-scoped Thread deep links, reload, and Back/Forward restore conversation selection', async ({ page }) => {
  const { reads } = await mockChatApi(page);
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
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

  await page.goto(baseURL+'/projects/alpha/chat?thread=alpha-thread');
  await page.evaluate(() => sessionStorage.clear());
  await page.reload();
  await expect(page.locator('#thread-title')).toHaveText('Alpha thread');
  await page.reload();
  await expect(page.locator('#thread-title')).toHaveText('Alpha thread');
  await expect.poll(() => reads.filter((id) => id === 'alpha-thread').length).toBeGreaterThanOrEqual(2);
});

test('Thread selection history is restored and Project switching drops the previous Thread', async ({ page }) => {
  const { reads } = await mockChatApi(page);
  await page.goto(baseURL+'/projects/home/chat');
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
  await page.goto(baseURL+'/projects/alpha/chat?thread=home-thread');
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
    await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
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
  await page.goto(baseURL+'/projects/home/chat');
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
    await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
    await expect(page.locator('#thread-title')).toHaveText('Home thread');
    await page.locator('#thread-settings-menu > summary').click();
    if (action === 'name') page.once('dialog', dialog => dialog.accept('Current rename'));
    await page.locator(action === 'name' ? '#rename-thread' : '#archive-thread').click();
    await expect(page.locator('#thread-title')).toHaveText(action === 'name' ? 'Current rename' : 'Select a thread');
    if (action === 'archive') await expect(page).not.toHaveURL(/thread=/);
  });
}

test('archived Thread offers canonical unarchive in its original Project visit', async ({ page }) => {
  await mockChatApi(page);
  const restored = [];
  await page.route('**/api/threads/home-thread/unarchive?*', route => {
    restored.push(new URL(route.request().url()).searchParams.get('project_id'));
    return route.fulfill({ json: {} });
  });
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await page.locator('#thread-settings-menu > summary').click();
  await page.locator('#archive-thread').click();
  await page.getByRole('button', { name: 'Unarchive Thread' }).click();
  await expect(page.locator('[data-thread-archive-recovery]')).toContainText('Restored Thread home-thread');
  expect(restored).toEqual(['home']);
  await selectProject(page, 'alpha');
  await expect(page.locator('[data-thread-archive-recovery]')).toHaveCount(0);
});

test('a previous conversation send failure does not appear in the current conversation', async ({ page }) => {
  await mockChatApi(page);
  let pending;
  await page.route('**/api/threads/home-thread/turns?*', route => { pending = route; });
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
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
    await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
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
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page.locator('#thread-history-control button')).toBeEnabled();
  await page.locator('#thread-history-control button').click();
  await expect.poll(() => readQueries.some(query => query.limit === '80' && query.project === 'home')).toBe(true);
  await expect(page.locator('#thread-history-control button')).toBeEnabled();
  await expect(page).toHaveURL(/projects\/home\/chat\?thread=home-thread/);
});

test('selecting a Thread again retries a detail read that did not render', async ({ page }) => {
  const { reads } = await mockChatApi(page, { degradeFirstThreadRead: true });
  await page.goto(baseURL+'/projects/home/chat');

  await page.locator('#threads .item-main', { hasText: 'Home thread' }).click({ force: true });
  await expect(page.locator('#messages')).toContainText('Thread temporarily unavailable');

  await page.locator('#threads .item-main', { hasText: 'Home thread' }).click({ force: true });
  await expect(page.locator('#messages')).toContainText('Loaded home-thread');
  expect(reads).toEqual(['home-thread', 'home-thread']);
});

test('a reply completed while another Thread is selected loads on return', async ({ page }) => {
  const { reads } = await mockChatApi(page, { simulateReply: true });
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#messages')).toContainText('Loaded home-thread');

  await page.locator('#prompt').fill('Reply after I switch Threads');
  await page.locator('#send').click();
  await page.goto(baseURL+'/projects/alpha/chat?thread=alpha-thread');
  await expect(page.locator('#thread-title')).toHaveText('Alpha thread');
  await page.waitForTimeout(150);

  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#messages')).toContainText('Reply completed while another thread was selected');
  expect(reads.filter((id) => id === 'home-thread').length).toBeGreaterThanOrEqual(2);
});

for(const queued of [false,true]) test(`structured preview enters existing Thread turn path with editable text and exact example reference (queued=${queued})`, async ({page}) => {
  const {threadTurns}=await mockChatApi(page,{queued});
  const reference={definition_id:'care.investigate-defect',revision:3,record_id:'skill-record',checksum:'fixture-checksum'};
  await page.route('**/api/projects/home/care/examples',route=>route.fulfill({json:{items:[{
    name:'Investigate a defect',allowed:true,reference,fields:{action:'Investigate defect',references:['issue:1370']}
  }]}}));
  await page.route('**/api/projects/home/care/preview',async route=>{
    const body=route.request().postDataJSON();
    expect(body.template_ref).toEqual(reference);
    expect(body.fields.references).toEqual(['issue:1370']);
    await route.fulfill({json:{message:'Action: Investigate defect\nReferences: issue:1370',template_ref:reference,suggestions:[]}});
  });
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await openSettings(page);await page.locator('#care-composer summary').click();
  await page.locator('#care-example').selectOption('0');
  await page.locator('#care-preview').click();
  await expect(page.locator('#prompt')).toHaveValue(/Investigate defect/);
  await page.locator('#prompt').fill('Edited investigation request');
  await page.locator('#send').click();
  await expect.poll(()=>threadTurns['home-thread']?.[0]?.items[0]?.content[0]?.text).toBe(
    'Edited investigation request\n\nStarting example: care.investigate-defect r3 · record skill-record · checksum fixture-checksum');
  await expect(page.locator('#care-action')).toHaveValue('');
  await expect(page.locator('#care-status')).toContainText('Request accepted');
  await expect(page.locator('#thread-settings-menu')).not.toHaveAttribute('open');
  await expect(page.locator('#care-composer')).not.toHaveAttribute('open');
  await page.locator('#prompt').fill('Ordinary follow-up after structured request');
  await page.locator('#send').click();
  await expect.poll(()=>threadTurns['home-thread']?.[0]?.items[0]?.content[0]?.text).toBe('Ordinary follow-up after structured request');
  await expect(page.locator('#thread-settings-menu')).not.toHaveAttribute('open');
});

test('denied structured admission keeps editable draft in its original Thread',async({page})=>{
  await mockChatApi(page);
  await page.route('**/api/projects/home/care/examples',route=>route.fulfill({json:{items:[]}}));
  await page.route('**/api/projects/home/care/preview',route=>route.fulfill({json:{message:'Retained structured request',suggestions:[]}}));
  await page.route('**/api/threads/home-thread/turns?*',route=>route.fulfill({status:403,json:{detail:'Admission denied'}}));
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await openSettings(page);await page.locator('#care-composer summary').click();
  await page.locator('#care-action').fill('Investigate denial');
  await page.locator('#care-preview').click();
  await expect(page.locator('#prompt')).toHaveValue('Retained structured request');
  await page.locator('#send').click();
  await expect(page.locator('#care-status')).toContainText('Submission failed');
  await expect(page.locator('#prompt')).toHaveValue('Retained structured request');
  await expect(page.locator('#care-action')).toHaveValue('Investigate denial');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
});

for (const width of [1280, 390]) test(`optional request preparation lives in keyboard-accessible settings (${width}px)`, async ({page}) => {
  await page.setViewportSize({width, height:844});
  const {threadTurns}=await mockChatApi(page);
  const reads=[];
  await page.route('**/api/projects/home/care/**', route=>{reads.push(route.request().url());return route.fulfill({json:{items:[]}});});
  await page.route('**/api/memory/projects/home/ux-briefs', route=>{reads.push(route.request().url());return route.fulfill({json:{items:[]}});});
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page.locator('.composer #care-composer, .composer #ux-brief-picker')).toHaveCount(0);
  await expect(page.locator('#thread-settings-menu #care-composer')).toHaveCount(1);
  await expect(page.locator('#thread-settings-menu #ux-brief-picker')).toHaveCount(1);
  await expect(page.locator('#care-composer')).not.toBeVisible();
  const summary=page.locator('#thread-settings-menu > summary');
  await summary.focus();await page.keyboard.press('Enter');
  await expect(page.locator('#care-composer')).toBeVisible();
  expect(reads).toEqual([]);expect(threadTurns).toEqual({});
  await page.keyboard.press('Escape');await expect(summary).toBeFocused();
  await page.keyboard.press('Enter');
  await page.locator('#thread-context-return').click();
  await expect(page.locator('#prompt')).toBeFocused();
  await expect(page.locator('#care-composer')).not.toBeVisible();
  await page.locator('#prompt').fill('Plain chat stays usable');await page.locator('#send').click();
  await expect.poll(()=>threadTurns['home-thread']?.[0]?.items[0]?.content[0]?.text).toBe('Plain chat stays usable');
  expect(reads).toEqual([]);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
});

test('missing structured preview reopens settings and focuses preparation without submitting',async({page})=>{
  const {threadTurns}=await mockChatApi(page);
  await page.route('**/api/projects/home/care/examples',route=>route.fulfill({json:{items:[]}}));
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await openSettings(page);await page.locator('#care-composer summary').click();
  await page.locator('#care-action').fill('Uncomposed task');
  await page.locator('#thread-context-return').click();await page.locator('#prompt').fill('Draft');
  await page.locator('#send').click();
  await expect(page.locator('#care-preview')).toBeFocused();
  await expect(page.locator('#care-status')).toContainText('Thread settings → Structured task');
  await expect(page.locator('#prompt')).toHaveValue('Draft');expect(threadTurns).toEqual({});
});

test('UX preview guidance opens settings and selected provenance reaches Thread admission',async({page})=>{
  const {threadTurns}=await mockChatApi(page);
  const selection={knowledge_id:'brief',version:1,sha256:'a'.repeat(64),sections:['observed_needs'],
    max_context_tokens:2000,sources:[{kind:'knowledge',record_id:'source',revision:'2',sha256:'b'.repeat(64)}]};
  await page.route('**/api/memory/projects/home/ux-briefs',route=>route.fulfill({json:{items:[{
    id:'brief',title:'User needs',version:1,content_sha256:selection.sha256,freshness:'fresh',
    content:JSON.stringify({sections:{observed_needs:{text:'Need a useful recovery view'}}})
  }]}}));
  await page.route('**/api/memory/projects/home/ux-briefs/preview',route=>route.fulfill({json:{
    context:'Need a useful recovery view',packed_tokens:20,selection,omitted:[]
  }}));
  let admitted;
  await page.route('**/api/threads/home-thread/turns?*',async route=>{
    if(route.request().method()==='POST')admitted=route.request().postDataJSON();
    await route.fallback();
  });
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await openSettings(page);await page.locator('#ux-brief-picker summary').click();
  await page.locator('#ux-picker-brief').selectOption('brief');
  await page.locator('#ux-picker-sections input[value="observed_needs"]').check();
  await page.locator('#thread-context-return').click();await page.locator('#prompt').fill('Build a recovery view');
  await page.locator('#send').click();
  await expect(page.locator('#ux-picker-preview')).toBeFocused();
  await expect(page.locator('#ux-picker-status')).toContainText('Thread settings → UX knowledge');
  expect(threadTurns).toEqual({});
  await page.locator('#ux-picker-preview').click();
  await expect(page.locator('#ux-picker-sources a')).toHaveAttribute('href','/api/memory/source');
  await page.locator('#thread-context-return').click();await page.locator('#send').click();
  await expect.poll(()=>admitted?.memory_context_selection).toEqual(selection);
  expect(admitted.message).toBe('Build a recovery view');
  await expect(page.locator('#ux-picker-brief')).toHaveValue('');
  await expect(page.locator('#thread-settings-menu')).not.toHaveAttribute('open');
});

test('cancelled predecessor history does not activate runner after reload or late reconnect events',async({page})=>{
  const now=Date.now()/1000;
  const cancelled={schemaVersion:'1.0',source:'assignment',assignmentId:'cancelled-assignment',
    assignmentCreatedAt:now-100,updatedAt:now,observedAt:now,fence:1,
    state:'cancelled',live:false,leaseValid:false,leaseExpiresAt:null};
  const running={...cancelled,assignmentId:'running-assignment',assignmentCreatedAt:now-50,
    state:'running',live:true,leaseValid:true,leaseExpiresAt:now+3600};
  let socket;
  await page.routeWebSocket('**/ws',ws=>{socket=ws;});
  await mockChatApi(page,{runners:{'home-thread':cancelled,'background-thread':running},threadStatuses:{'home-thread':'idle','background-thread':'active'},retainedTurns:{
    'home-thread':[{id:'retained-predecessor-turn',status:'inProgress',items:[{type:'userMessage',content:[{type:'inputText',text:'Retained original request'}]}]}],
    'background-thread':[{id:'current-turn',status:'inProgress',items:[{type:'agentMessage',text:'Current genuine work'}]}]
  }});
  const fs=require('fs'),path=require('path');
  expect(await (await page.request.get(baseURL+'/static/thread_runner_ui.js')).text())
    .toBe(fs.readFileSync(path.join(__dirname,'../../static/thread_runner_ui.js'),'utf8'));
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page.locator('#messages')).toContainText('Retained original request');
  await expect(page.locator('#threads')).toContainText('Runner cancelled');
  await expect(page.locator('#threads')).toContainText('Runner live');
  await expect(page.locator('#codex-status-text')).toHaveText('Idle');
  await expect.poll(()=>Boolean(socket)).toBe(true);
  socket.send(JSON.stringify({type:'codex.event',message:{method:'turn/started',params:{threadId:'home-thread',turn:{id:'retained-predecessor-turn'}}}}));
  await expect(page.locator('#codex-status-text')).toHaveText('Idle');
  await page.reload();await expect(page.locator('#thread-title')).toHaveText('Home thread');
  await expect(page.locator('#codex-status-text')).toHaveText('Idle');
  await page.locator('#threads .item-main', {hasText:'Background thread'}).click();
  await expect(page.locator('#thread-title')).toHaveText('Background thread');
  await expect(page.locator('#codex-status-text')).toHaveText('Waiting for Codex');
});

test('unavailable rename reports error while preserving the Thread name',async({page})=>{
  await mockChatApi(page);
  await page.route('**/api/threads/home-thread/name?*',route=>route.fulfill({status:503,json:{detail:'runtime session unavailable'}}));
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
  page.once('dialog',dialog=>dialog.accept('Changed name'));
  await openSettings(page);await page.locator('#rename-thread').click();
  await expect(page.locator('#messages')).toContainText('Rename failed');
  await expect(page.locator('#messages')).toContainText('runtime session unavailable');
  await expect(page.locator('#thread-title')).toHaveText('Home thread');
});


test('unknown detail clears cached busy state and preserves retained history',async({page})=>{
  const now=Date.now()/1000;
  const live={schemaVersion:'1.0',state:'running',live:true,leaseValid:true,
    assignmentId:'current',assignmentCreatedAt:now-100,updatedAt:now,observedAt:now,
    fence:2,leaseExpiresAt:now+3600};
  const runners={'home-thread':live};
  await mockChatApi(page,{runners,retainedTurns:{'home-thread':[{id:'retained',status:'inProgress',
    items:[{type:'agentMessage',text:'Historical unknown business outcome'}]}]}});
  await page.route('**/api/threads/home-thread?*',async route=>route.fulfill({json:{thread:{
    id:'home-thread',name:'Home thread',projectId:'home',status:{type:'notLoaded'},
    runner:{...live,state:'unknown',live:false,observedAt:now+1},
    turns:[{id:'retained',status:'inProgress',items:[{type:'agentMessage',text:'Historical unknown business outcome'}]}]
  }}}));
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#messages')).toContainText('Historical unknown business outcome');
  await expect(page.locator('#messages')).toContainText('Runner status unknown');
  await expect(page.locator('#threads')).toContainText('Unknown runner');
  await expect(page.locator('#codex-status-text')).toHaveText('Idle');
});

test('current refresh restores waiting runner and renewed lease while old events cannot override new fence',async({page})=>{
  const now=Date.now()/1000;
  const waiting={schemaVersion:'1.0',state:'waiting',live:false,leaseValid:false,
    assignmentId:'current',assignmentCreatedAt:now-100,updatedAt:now,observedAt:now,fence:1};
  const runners={'home-thread':waiting};
  let socket,runnerReads=0;
  await page.routeWebSocket('**/ws',ws=>{socket=ws;});
  await mockChatApi(page,{runners});
  await page.route('**/api/threads/home-thread/runner?*',route=>{
    runnerReads++;
    return route.fulfill({json:{id:'home-thread',runner:runners['home-thread'],
      status:{type:'active'},activity:{state:'active',turnId:'new-turn'}}});
  });
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#threads')).toContainText('Waiting for worker');
  runners['home-thread']={...waiting,state:'running',live:true,leaseValid:true,fence:2,
    observedAt:now+1,updatedAt:now+1,leaseExpiresAt:now+3600};
  await expect.poll(()=>Boolean(socket)).toBe(true);
  const event=method=>JSON.stringify({type:'codex.event',message:{method,params:{threadId:'home-thread',turn:{id:'old-turn'}}}});
  for(let n=0;n<10;n++)socket.send(event('turn/started'));
  await expect(page.locator('#threads')).toContainText('Runner live',{timeout:10000});
  await expect(page.locator('#codex-status-text')).toHaveText('Waiting for Codex');
  expect(runnerReads).toBe(1);
  socket.send(event('turn/completed'));
  await expect(page.locator('#codex-status-text')).toHaveText('Waiting for Codex');
  // The canonical current state, rather than the old turn event, wins again.
  await expect.poll(()=>runnerReads,{timeout:10000}).toBe(2);
  await expect(page.locator('#codex-status-text')).toHaveText('Waiting for Codex');
});


test('live renewed assignment stays idle after completion, next send is ordinary, old completion preserves new activity',async({page})=>{
  const now=Date.now()/1000;
  const live={schemaVersion:'1.0',state:'running',live:true,leaseValid:true,
    assignmentId:'persistent',assignmentCreatedAt:now-100,updatedAt:now,observedAt:now,
    fence:2,leaseExpiresAt:now+7200};
  const runners={'home-thread':live};
  const activities={'home-thread':{state:'idle',turnId:null}};
  let socket,posted;
  await page.routeWebSocket('**/ws',ws=>{socket=ws;});
  const mock=await mockChatApi(page,{runners,activities,threadStatuses:{'home-thread':'idle'},retainedTurns:{
    'home-thread':[{id:'completed-turn',status:'completed',items:[{type:'agentMessage',text:'Completed response'}]}]}});
  await page.route('**/api/threads/home-thread/turns?*',route=>{
    posted=JSON.parse(route.request().postData());
    mock.threads.home.find(t=>t.id==='home-thread').status={type:'active'};
    activities['home-thread']={state:'active',turnId:'new-turn'};
    return route.fulfill({json:{queued:false,turn:{id:'new-turn'}}});
  });
  await page.goto(baseURL+'/projects/home/chat?thread=home-thread');
  await expect(page.locator('#thread-meta')).toContainText('Runner live');
  await expect(page.locator('#messages')).toContainText('Completed response');
  await expect(page.locator('#codex-status-text')).toHaveText('Idle');
  await page.locator('#prompt').fill('Ordinary next request');
  await page.locator('#prompt').press('Enter');
  await expect.poll(()=>posted?.message).toBe('Ordinary next request');
  await expect(page.locator('#messages .role').last()).toHaveText('You');
  await expect(page.locator('#codex-status-text')).toHaveText('Waiting for Codex');
  await expect.poll(()=>Boolean(socket)).toBe(true);
  socket.send(JSON.stringify({type:'codex.event',message:{method:'turn/completed',params:{threadId:'home-thread',turn:{id:'completed-turn'}}}}));
  await page.waitForTimeout(5500);
  await expect(page.locator('#codex-status-text')).toHaveText('Waiting for Codex');
  activities['home-thread']={state:'idle',turnId:null};
  mock.threads.home.find(t=>t.id==='home-thread').status={type:'idle'};
  socket.send(JSON.stringify({type:'codex.event',message:{method:'turn/completed',params:{threadId:'home-thread',turn:{id:'new-turn'}}}}));
  await expect(page.locator('#codex-status-text')).toHaveText('Idle',{timeout:10000});
  await expect(page.locator('#thread-meta')).toContainText('Runner live');
});
