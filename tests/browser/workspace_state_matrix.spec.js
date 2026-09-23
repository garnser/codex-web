const { test, expect } = require("@playwright/test");

const url = "http://127.0.0.1:18766/tests/browser/workspace_state_matrix_fixture.html";

test("shared workspace states expose deterministic loading empty degraded error and blocked semantics", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.stack || error.message));
  await page.goto(url, { waitUntil: "networkidle" });

  await expect(page.locator("#state-loading")).toHaveAttribute("data-state", "loading");
  await expect(page.locator("#state-loading")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator("#state-loading")).toHaveAttribute("role", "status");

  await expect(page.locator("#state-empty")).toHaveAttribute("data-state", "empty");
  await expect(page.locator("#state-empty")).toContainText("No records");

  await expect(page.locator("#state-degraded")).toHaveAttribute("data-state", "degraded");
  await expect(page.locator("#state-degraded")).toContainText("Provider degraded");

  await expect(page.locator("#state-error")).toHaveAttribute("role", "alert");
  await expect(page.locator("#state-error button")).toHaveText("Retry");

  await expect(page.locator("#state-blocked [data-status='blocked']")).toHaveCount(2);
  await expect(page.locator("#state-blocked")).toContainText("worker_capability_missing");
  expect(errors).toEqual([]);
});

test("workspace state matrix remains bounded at phone width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(url, { waitUntil: "networkidle" });

  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    body: document.body.scrollWidth,
    root: document.documentElement.scrollWidth,
  }));
  expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport);
  expect(dimensions.root).toBeLessThanOrEqual(dimensions.viewport);

  const cards = page.locator("#states > *");
  await expect(cards).toHaveCount(5);
  for (let index = 0; index < await cards.count(); index += 1) {
    const box = await cards.nth(index).boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
  }
});
