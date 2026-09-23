const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const manifest = JSON.parse(
  fs.readFileSync(path.join(root, "docs/screenshots/manifest.json"), "utf8")
);

test("documentation screenshot manifest uses sanitized reachable fixtures", async ({ page }) => {
  const names = new Set();

  for (const capture of manifest.captures) {
    expect(names.has(capture.name), `duplicate capture name: ${capture.name}`).toBeFalsy();
    names.add(capture.name);

    const fixturePath = path.join(root, capture.fixture);
    expect(fs.existsSync(fixturePath), `missing fixture: ${capture.fixture}`).toBeTruthy();

    await page.setViewportSize(capture.viewport || { width: 1440, height: 1000 });
    await page.goto(`http://127.0.0.1:18766/${capture.fixture}`, {
      waitUntil: "networkidle",
    });
    if (capture.open_selector) {
      const trigger = page.locator(capture.open_selector);
      await expect(trigger, `${capture.name} missing open selector`).toBeVisible();
      await trigger.click();
      await page.waitForTimeout(50);
    }
    const text = await page.locator("body").innerText();

    for (const marker of manifest.forbidden_markers || []) {
      expect(text, `${capture.name} contains forbidden marker ${marker}`).not.toContain(marker);
    }
    for (const landmark of capture.landmarks || []) {
      expect(text, `${capture.name} missing landmark ${landmark}`).toContain(landmark);
    }
  }

  expect(manifest.captures.length).toBeGreaterThanOrEqual(8);
});
