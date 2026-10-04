const { expect, test } = require("@playwright/test");
const { expectNoBlockingViolations } = require("./accessibility_axe");

const staticHost = "http://127.0.0.1:18766";

test("axe accessibility gate: application shell", async ({ page }) => {
  // Qualify production markup without requiring a live control-plane API. App
  // modules are covered through their routed workspace fixtures below.
  await page.route("**/static/*.js*", (route) => route.abort());
  await page.goto(`${staticHost}/static/index.html`);
  await expectNoBlockingViolations(expect, page, {
    surface: "application shell",
    state: "initial static render",
  });
});

const workspaceStates = [
  ["overview", "Overview"],
  ["goals", "Goals"],
  ["resources", "Resources"],
  ["operations", "Operations"],
];

for (const [workspace, title] of workspaceStates) {
  test(`axe accessibility gate: ${title} workspace`, async ({ page }) => {
    await page.goto(`${staticHost}/tests/browser/product_workspaces_fixture.html`);
    await page.evaluate((destination) => {
      window.CodexProductUI.openWorkspace(destination, { page: destination });
    }, workspace);
    const surface = page.locator("#product-workspace-page");
    await expect(surface).toBeVisible();
    await expect(surface.locator("[data-product-workspace-title]")).toHaveText(title);
    await expectNoBlockingViolations(expect, page, {
      surface: `${title} workspace`,
      state: "loaded canonical workspace",
      include: "#product-workspace-page",
    });
  });
}
