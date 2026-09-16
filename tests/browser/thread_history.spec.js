const { test, expect } = require('@playwright/test');


test('thread history uses explicit controller without fetch or scrollTop monkey patches', async ({ page }) => {
  await page.goto('http://127.0.0.1:18766/tests/browser/thread_history_fixture.html');

  await expect(page.locator('#thread-history-control button')).toHaveText('Load earlier activity (60 hidden)');

  const monkeyPatchState = await page.evaluate(() => ({
    fetchUnchanged: window.fetch === window.__fetchBeforeThreadHistory,
    hasOwnScrollTop: Object.prototype.hasOwnProperty.call(document.getElementById('messages'), 'scrollTop'),
    initialLimit: window.codexThreadHistory.messageLimit('thread-1'),
  }));

  expect(monkeyPatchState.fetchUnchanged).toBe(true);
  expect(monkeyPatchState.hasOwnScrollTop).toBe(false);
  expect(monkeyPatchState.initialLimit).toBe(40);

  const before = await page.evaluate(() => {
    const messages = document.getElementById('messages');
    messages.scrollTop = 240;
    messages.dispatchEvent(new Event('scroll'));
    return {
      scrollTop: messages.scrollTop,
      scrollHeight: messages.scrollHeight,
    };
  });

  await page.locator('#thread-history-control button').click();
  await expect.poll(() => page.evaluate(() => window.__reloadCalls.length)).toBe(1);

  const after = await page.evaluate(() => {
    const messages = document.getElementById('messages');
    return {
      scrollTop: messages.scrollTop,
      scrollHeight: messages.scrollHeight,
      reload: window.__reloadCalls[0],
      nextLimit: window.codexThreadHistory.messageLimit('thread-1'),
      controlText: document.querySelector('#thread-history-control button')?.textContent || '',
    };
  });

  expect(after.reload.threadId).toBe('thread-1');
  expect(after.reload.limit).toBe(80);
  expect(after.nextLimit).toBe(80);
  expect(after.controlText).toBe('Load earlier activity (20 hidden)');
  expect(after.scrollHeight - before.scrollHeight).toBe(after.reload.addedHeight);
  expect(Math.abs((after.scrollTop - before.scrollTop) - after.reload.addedHeight)).toBeLessThanOrEqual(2);
});
