import fs from "node:fs/promises";
import path from "node:path";
import { expect, test } from "@playwright/test";

const root = path.resolve(import.meta.dirname, "..", "..");
const manifest = JSON.parse(
  await fs.readFile(path.join(root, "docs", "screenshots", "manifest.json"), "utf8"),
);

for (const capture of manifest.captures) {
  test(`documentation fixture ${capture.name} has no uncaught page exceptions`, async ({ page }) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.stack || error.message));

    await page.goto(
      `http://127.0.0.1:18766/${capture.fixture}`,
      { waitUntil: "networkidle" },
    );
    if (capture.open_selector) {
      await page.locator(capture.open_selector).click();
    }

    expect(errors, `${capture.name}: uncaught page exceptions`).toEqual([]);
    const body = await page.locator("body").innerText();
    for (const landmark of capture.landmarks || []) {
      expect(body, `${capture.name}: missing landmark ${landmark}`).toContain(landmark);
    }
    for (const marker of manifest.forbidden_markers || []) {
      expect(body, `${capture.name}: sensitive marker ${marker}`).not.toContain(marker);
    }
  });
}
