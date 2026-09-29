const fs = require("node:fs");
const path = require("node:path");
const { expect, test } = require("@playwright/test");

const root = path.resolve(__dirname, "..", "..");
const manifest = JSON.parse(
  fs.readFileSync(path.join(root, "docs", "screenshots", "manifest.json"), "utf8"),
);

for (const capture of manifest.captures) {
  test(`documentation fixture ${capture.name} has no uncaught page exceptions`, async ({ page }) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.stack || error.message));
    if (capture.name === "executive-management") {
      // This fixture imports its API client only after opening. Keep the smoke
      // assertion independent of module-cache and runner-speed differences.
      await page.route("**/static/api_client.js", async (route) => {
        await new Promise((resolve) => setTimeout(resolve, 150));
        await route.continue();
      });
    }

    await page.goto(
      `http://127.0.0.1:18766/${capture.fixture}`,
      { waitUntil: "networkidle" },
    );
    if (capture.open_event) {
      await page.evaluate((eventName) => {
        window.dispatchEvent(new CustomEvent(eventName));
      }, capture.open_event);
      await page.waitForTimeout(50);
    } else if (capture.open_selector) {
      await page.locator(capture.open_selector).click();
      // Match the documentation capture path: some fixtures populate
      // deterministic content on the next UI tick after opening.
      await page.waitForTimeout(50);
    }

    const body = page.locator("body");
    for (const landmark of capture.landmarks || []) {
      await expect(body, `${capture.name}: missing landmark ${landmark}`).toContainText(landmark);
    }
    expect(errors, `${capture.name}: uncaught page exceptions`).toEqual([]);
    const bodyText = await body.innerText();
    for (const marker of manifest.forbidden_markers || []) {
      expect(bodyText, `${capture.name}: sensitive marker ${marker}`).not.toContain(marker);
    }
  });
}
