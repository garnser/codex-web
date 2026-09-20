const { test, expect } = require("@playwright/test");

test("complex operator surfaces receive contextual documentation links", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/contextual_help_fixture.html");

  const links = page.locator(".context-help-link");
  await expect(links).toHaveCount(12);

  await expect(page.locator("h2", { hasText: "Definition Registry" }).locator(".context-help-link"))
    .toHaveAttribute("href", /\/help-docs\/administration\/definition-registry\.md$/);
  await expect(page.locator("h2", { hasText: "Execution Workers" }).locator(".context-help-link"))
    .toHaveAttribute("href", /operations\/runbooks\.md#execution-worker-drain-quarantine-and-replacement$/);
  await expect(page.locator("h2", { hasText: "Execution Preflight" }).locator(".context-help-link"))
    .toHaveAttribute("href", /operations\/multi-repository-projects\.md$/);
  await expect(page.locator("h2", { hasText: "Legacy Project Migration" }).locator(".context-help-link"))
    .toHaveAttribute("href", /operations\/legacy-project-migration\.md$/);
  await expect(page.locator("h3", { hasText: "Recovery / DR" }).locator(".context-help-link"))
    .toHaveAttribute("href", /operations\/runbooks\.md#backup-restore-and-recovery-drill$/);
  await expect(page.locator("h3", { hasText: "Upgrade / migration" }).locator(".context-help-link"))
    .toHaveAttribute("href", /operations\/upgrade-and-rollback\.md$/);

  await page.evaluate(() => {
    const card = document.createElement("div");
    card.className = "developer-card";
    card.innerHTML = "<h2>Structured Logs</h2>";
    document.querySelector("#developer-panel .developer-grid").appendChild(card);
  });
  await expect(page.locator("h2", { hasText: "Structured Logs" }).locator(".context-help-link"))
    .toHaveCount(1);

  const releaseHeading = page.locator("h3", { hasText: "Release & promotion" });
  await expect(releaseHeading.locator(".context-help-link")).toHaveCount(1);
  await page.evaluate(() => {
    document.querySelector("h3").appendChild(document.createTextNode(" "));
  });
  await expect(releaseHeading.locator(".context-help-link")).toHaveCount(1);
});

test("contextual help links are keyboard-focusable", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/contextual_help_fixture.html");
  const first = page.locator(".context-help-link").first();
  await first.focus();
  await expect(first).toBeFocused();
  await expect(first).toHaveAttribute("target", "_blank");
  await expect(first).toHaveAttribute("rel", "noreferrer");
});
