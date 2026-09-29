const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

// Exercise the actual chat header/control count and layout, not a smaller copy.
const source = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
const main = source.match(/<main class="main">[\s\S]*?<\/main>/)[0];

for (const width of [1440, 1024, 768, 390, 320]) {
  test(`chat content stays inside its viewport at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.route('http://127.0.0.1:18766/chat-width-fixture', route => route.fulfill({
      contentType: 'text/html',
      body: `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="stylesheet" href="/static/design_tokens.css">
        <link rel="stylesheet" href="/static/workspace_components.css">
        <link rel="stylesheet" href="/static/styles.css">
        <link rel="stylesheet" href="/static/mobile_responsive.css">
        </head><body><aside class="sidebar" aria-label="Projects"></aside>${main}</body></html>`,
    }));
    await page.goto('http://127.0.0.1:18766/chat-width-fixture');
    await page.evaluate(() => {
      document.getElementById('thread-title').textContent = 'long-thread-name-'.repeat(10);
      document.getElementById('thread-meta').textContent = 'project/reference/'.repeat(50);
      const article = document.createElement('article');
      article.className = 'message agent';
      const header = document.createElement('div');
      header.className = 'message-header';
      const role = document.createElement('span');
      role.className = 'role';
      role.textContent = 'LongProviderIdentifier'.repeat(15);
      header.append(role);
      const body = document.createElement('div');
      body.className = 'body';
      body.textContent = 'https://example.invalid/' + 'unbroken'.repeat(250);
      const pre = document.createElement('pre');
      pre.textContent = 'const preserveCodeColumns = '.repeat(100);
      body.append(pre);
      const table = document.createElement('table');
      table.style.whiteSpace = 'nowrap';
      const row = table.insertRow();
      for (let index = 0; index < 30; index++) row.insertCell().textContent = `Column ${index}`;
      body.append(table);
      const image = document.createElement('img');
      image.alt = 'Wide attachment';
      image.width = 4000;
      image.height = 100;
      image.src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="4000" height="100"></svg>';
      body.append(image);
      article.append(header, body);
      document.getElementById('messages').append(article);
    });

    for (const collapsed of [false, true]) {
      await page.evaluate(value => document.body.classList.toggle('sidebar-collapsed', value), collapsed);
      const bounds = await page.evaluate(() => ({
        viewport: innerWidth,
        root: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        elements: [...document.querySelectorAll('.main, .topbar, .controls, .messages, .composer, #prompt, #send, .message, .message .body, .message img')]
          .map(node => ({ name: node.id || node.className, left: node.getBoundingClientRect().left, right: node.getBoundingClientRect().right })),
      }));
      expect(bounds.root).toBeLessThanOrEqual(width);
      expect(bounds.body).toBeLessThanOrEqual(width);
      for (const element of bounds.elements) {
        expect(element.left, element.name).toBeGreaterThanOrEqual(-1);
        expect(element.right, element.name).toBeLessThanOrEqual(width + 1);
      }
      for (const selector of ['.message .body pre', '.message .body table']) {
        const scrolling = await page.locator(selector).evaluate(node => {
          node.scrollLeft = 50;
          return { overflow: getComputedStyle(node).overflowX, moved: node.scrollLeft, width: node.clientWidth, content: node.scrollWidth };
        });
        expect(scrolling.overflow).toBe('auto');
        expect(scrolling.content).toBeGreaterThan(scrolling.width);
        expect(scrolling.moved).toBeGreaterThan(0);
      }
      await page.locator('#prompt').fill('Composer remains editable');
      await expect(page.locator('#prompt')).toHaveValue('Composer remains editable');
    }
  });
}
