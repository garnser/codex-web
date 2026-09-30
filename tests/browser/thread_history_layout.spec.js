const { test, expect } = require('@playwright/test');

async function expectSeparated(page) {
  const geometry = await page.evaluate(() => {
    const button = document.querySelector('#thread-history-control button');
    const control = button.getBoundingClientRect();
    const messages = document.getElementById('messages').getBoundingClientRect();
    const composer = document.querySelector('.composer').getBoundingClientRect();
    return {
      hit: button.contains(document.elementFromPoint(control.x + control.width / 2, control.y + control.height / 2)),
      controlBottom: control.bottom,
      messagesTop: messages.top,
      messagesBottom: messages.bottom,
      messagesHeight: messages.height,
      composerTop: composer.top,
      composerBottom: composer.bottom,
      viewportHeight: innerHeight,
    };
  });
  expect(geometry.hit).toBe(true);
  expect(geometry.controlBottom).toBeLessThanOrEqual(geometry.messagesTop + 1);
  expect(geometry.messagesHeight).toBeGreaterThan(80);
  expect(geometry.messagesBottom).toBeLessThanOrEqual(geometry.composerTop + 1);
  expect(geometry.composerBottom).toBeLessThanOrEqual(geometry.viewportHeight + 1);
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 1024, height: 768 }, { width: 390, height: 844 }]) {
  test(`history remains clickable with long/dynamic content at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('http://127.0.0.1:18766/tests/browser/thread_history_layout_fixture.html');
    const button = page.locator('#thread-history-control button');
    await expect(button).toBeVisible();
    await expectSeparated(page);
    // A real pointer click, not forced or evaluated, must reach the control.
    await button.click();
    await expect.poll(() => page.evaluate(() => window.reloadCount)).toBe(1);
    await expectSeparated(page);

    await button.focus();
    await page.evaluate(() => {
      document.getElementById('messages').append(window.makeMessage('live'));
      document.getElementById('approvals').textContent = 'Approval requested';
    });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expectSeparated(page);
    await expect(button).toBeFocused();
    await page.keyboard.press('Enter');
    await expect.poll(() => page.evaluate(() => window.reloadCount)).toBe(2);
    await expectSeparated(page);

    // Switching to a short/empty history cannot move the stream to another row.
    await page.evaluate(() => window.showHistory(false));
    await expect(page.locator('#thread-history-control')).toBeHidden();
    await expect(page.locator('.composer')).toBeInViewport();
    await page.evaluate(() => window.showHistory(true));
    await expect(button).toBeVisible();
    await expectSeparated(page);
  });
}
