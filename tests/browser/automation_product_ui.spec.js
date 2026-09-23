const { test, expect } = require("@playwright/test");

test("Automation workspace renders canonical definitions, provenance and run history", async ({ page }) => {
  const launches = [];
  const manualRuns = [];
  await page.route("**/api/automations**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/automations") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{
            id: "nightly-review",
            definition: {
              name: "Nightly review",
              description: "Review current Project health.",
              lifecycle: "enabled",
              trigger: { type: "canonical_event", event_type: "ci.pipeline" },
              target: { kind: "agent_profile", id: "agent-james" },
              work_item_policy: "reuse_or_create",
              approval_required: false,
              owner_identity_id: "operator",
              skill_refs: [{ definition_id: "review-skill", revision: 2 }],
              budget: { max_concurrency: 1, max_input_tokens: 12000, max_output_tokens: 2500, max_cost_usd: 1.5 },
              retry: { max_attempts: 2, backoff_seconds: 30 },
            },
            definitionRef: { record_id: "definition-record-9", revision: 4 },
          }],
        }),
      });
      return;
    }
    if (url.pathname === "/api/automations/nightly-review/runs" && request.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{
            id: "automation-run-old",
            status: "succeeded",
            created_at: 1790180000,
            work_item_ref: "group/app#42",
            execution_ids: ["exec-42"],
          }],
        }),
      });
      return;
    }
    if (url.pathname === "/api/automations/nightly-review/runs/manual" && request.method() === "POST") {
      manualRuns.push(JSON.parse(request.postData() || "{}"));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run: { id: "automation-run-new", status: "admitted" },
          inserted: true,
          launchAllowed: true,
        }),
      });
      return;
    }
    await route.continue();
  });
  await page.route("**/api/automation-runs/**", async (route) => {
    launches.push({
      path: new URL(route.request().url()).pathname,
      body: JSON.parse(route.request().postData() || "{}"),
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ run: { id: "automation-run-new", status: "running" } }),
    });
  });

  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");
  await page.evaluate(() => window.CodexProductUI.openWorkspace("autonomy"));

  const card = page.locator("[data-automation-product]");
  await expect(card).toBeVisible();
  await expect(card).toContainText("Nightly review");
  await expect(card).toContainText("Event · ci.pipeline");
  await expect(card).toContainText("agent_profile · agent-james");
  await expect(card).toContainText("reuse_or_create");
  await expect(card).toContainText("r4 · definition-record-9");
  await expect(card).toContainText("Work Item group/app#42");
  await expect(card).toContainText("Execution exec-42");

  await card.locator("[data-automation-run-now]").click();
  await expect.poll(() => manualRuns.length).toBe(1);
  expect(manualRuns[0].project_id).toBe("home");
  await expect.poll(() => launches.length).toBe(1);
  expect(launches[0].path).toBe("/api/automation-runs/automation-run-new/launch");
});

test("Automation workspace remains usable on phone layout", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/automations**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/automations") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [] }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ items: [] }) });
  });
  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html#workspace/autonomy");
  const card = page.locator("[data-automation-product]");
  await expect(card).toBeVisible();
  const box = await card.boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.width).toBeLessThanOrEqual(390);
});
