const { expect } = require('@playwright/test');
async function resolveAction(page, accept = true) {
  const dialog = page.locator('[data-action-confirmation]');
  await expect(dialog).toBeVisible();
  const message = await dialog.innerText();
  if (accept) {
    const check = dialog.locator('[data-action-ack]');
    if (await check.isVisible()) await check.check();
    await dialog.locator('[data-action-apply]').click();
  } else await dialog.locator('[data-action-cancel]').click();
  await expect(dialog).toHaveCount(0);
  return message;
}
module.exports = { resolveAction };
