const { test, expect } = require("@playwright/test");

test("Swagger browser lazy-loads the live FastAPI docs and can be dismissed", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/swagger_browser_fixture.html");

  const launch = page.locator("#swagger-browser-launch");
  const drawer = page.locator("#swagger-browser-drawer");
  const frame = page.locator("#swagger-browser-frame");

  await expect(launch).toBeVisible();
  await expect(drawer).toHaveAttribute("aria-hidden", "true");
  await expect(frame).toHaveAttribute("src", "about:blank");

  await launch.click();
  await expect(drawer).toHaveClass(/open/);
  await expect(drawer).toHaveAttribute("aria-hidden", "false");
  await expect(launch).toHaveAttribute("aria-expanded", "true");
  await expect(frame).toHaveAttribute("src", "/docs");
  await expect(page.locator("#swagger-openapi-json")).toHaveAttribute("href", "/openapi.json");
  await expect(page.locator("#swagger-open-tab")).toHaveAttribute("href", "/docs");

  await page.keyboard.press("Escape");
  await expect(drawer).not.toHaveClass(/open/);
  await expect(launch).toHaveAttribute("aria-expanded", "false");
  await expect(launch).toBeFocused();
});

test("Swagger browser honors the /codex reverse-proxy base path", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/swagger_browser_fixture.html?proxied=1");

  await page.locator("#swagger-browser-launch").click();

  await expect(page.locator("#swagger-browser-frame")).toHaveAttribute("src", "/codex/docs");
  await expect(page.locator("#swagger-openapi-json")).toHaveAttribute("href", "/codex/openapi.json");
  await expect(page.locator("#swagger-open-tab")).toHaveAttribute("href", "/codex/docs");
});

test("Swagger browser stays usable on a narrow mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("http://127.0.0.1:18766/tests/browser/swagger_browser_fixture.html");

  await page.locator("#swagger-browser-launch").click();

  const drawer = page.locator("#swagger-browser-drawer");
  await expect(drawer).toBeVisible();
  const box = await drawer.boundingBox();
  expect(box.x).toBeLessThanOrEqual(1);
  expect(box.width).toBeLessThanOrEqual(390);
  await expect(page.locator("#swagger-browser-close")).toBeVisible();
  await expect(page.locator("#swagger-openapi-json")).toBeVisible();
});

test("Swagger browser installer is idempotent", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/swagger_browser_fixture.html");

  await page.evaluate(async () => {
    const moduleUrl = "/static/swagger_browser.js?second-install";
    await import(moduleUrl);
  });

  await expect(page.locator("#swagger-browser-launch")).toHaveCount(1);
  await expect(page.locator("#swagger-browser-drawer")).toHaveCount(1);
});
