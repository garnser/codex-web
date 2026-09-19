const { test, expect } = require("@playwright/test");

const definition = {
  id: "business-kpi-arr",
  key: "arr",
  name: "Annual recurring revenue",
  description: "Explicit recurring revenue sum.",
  domain: "revenue",
  metric_id: "metric-arr",
  revision: 3,
  unit: "usd",
  currency: "USD",
  operands: [{ key: "revenue", fact_key: "arr", aggregation: "sum" }],
  expression: { kind: "operand", operand_key: "revenue" },
};

const operating = {
  id: "business-operating-1",
  captured_by: "admin",
  captured_at: 100,
  items: [{
    kpi_id: "business-kpi-arr",
    kpi_revision: 3,
    name: "Annual recurring revenue",
    domain: "revenue",
    metric_id: "metric-arr",
    metric_revision: 3,
    metric_snapshot_id: "metric-snapshot-arr",
    observation_ids: ["metric-observation-arr"],
    value: 125000,
    unit: "usd",
    freshness: "partial",
    reasons: ["customer-1: conflicting providers"],
    selected_fact_ids: ["company-fact-arr"],
    source_external_record_ref_ids: ["external-record-crm"],
    goal_ids: ["goal-growth"],
    decision_ids: ["decision-plan"],
    target: {
      label: "ARR target",
      operator: "gte",
      target_value: 120000,
      passed: true,
      variance: 5000,
      variance_percent: 4.1667,
    },
    trend: {
      previous_observation_id: "metric-observation-old",
      previous_value: 120000,
      previous_partial: false,
      absolute_delta: 5000,
      percent_delta: 4.1667,
    },
  }],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/business-kpis", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: { items: [definition], count: 1 } });
      return;
    }
    await route.fallback();
  });
  await page.route("**/api/business-kpis/operating-snapshots?limit=1", async (route) => {
    await route.fulfill({ json: { items: [operating], count: 1 } });
  });
  await page.route("**/api/business-kpis/operating-snapshots", async (route) => {
    await route.fulfill({ json: { item: operating } });
  });
});

test("company KPI view shows exact operating provenance and partial state", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/business_kpis_fixture.html");
  await page.getByRole("button", { name: "Company KPIs" }).click();

  await expect(page.getByRole("heading", { name: "Company operating metrics" })).toBeVisible();
  await expect(page.locator(".business-kpi-freshness.partial").first()).toHaveText("partial");
  await expect(page.locator(".business-kpi-detail")).toContainText("KPI r3 / Metric r3");
  await expect(page.locator(".business-kpi-detail")).toContainText("customer-1: conflicting providers");
  await expect(page.locator(".business-kpi-detail")).toContainText("company-fact-arr");
  await expect(page.locator(".business-kpi-detail")).toContainText("external-record-crm");
  await expect(page.locator(".business-kpi-detail")).toContainText("goal-growth");
  await expect(page.locator(".business-kpi-detail")).toContainText("decision-plan");
  await expect(page.locator(".business-kpi-detail pre")).toContainText('"kind": "operand"');
});

test("company KPI drilldowns target existing canonical workspaces", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/business_kpis_fixture.html");
  await page.evaluate(() => {
    window.__drilldowns = [];
    for (const type of ["codex:open-metric", "codex:open-goal", "codex:open-decision"]) {
      window.addEventListener(type, (event) => {
        window.__drilldowns.push({ type, detail: event.detail });
      });
    }
  });
  await page.getByRole("button", { name: "Company KPIs" }).click();

  await page.locator(".business-kpi-open-metric").click();
  await page.locator(".business-kpi-open-goal").click();
  await page.locator(".business-kpi-open-decision").click();

  const drilldowns = await page.evaluate(() => window.__drilldowns);
  expect(drilldowns).toEqual([
    { type: "codex:open-metric", detail: { metricId: "metric-arr" } },
    { type: "codex:open-goal", detail: { goalId: "goal-growth" } },
    { type: "codex:open-decision", detail: { decisionId: "decision-plan" } },
  ]);
});

test("company KPI view captures a new deterministic snapshot", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/business_kpis_fixture.html");
  await page.getByRole("button", { name: "Company KPIs" }).click();
  const capture = page.waitForRequest((request) =>
    request.url().endsWith("/api/business-kpis/operating-snapshots")
    && request.method() === "POST"
  );
  await page.getByRole("button", { name: "Capture current" }).click();
  await capture;
  await expect(page.locator(".business-kpis-status")).toHaveText("Operating snapshot captured");
});

test("company KPI dialog remains usable on mobile width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("http://127.0.0.1:18766/tests/browser/business_kpis_fixture.html");
  await page.getByRole("button", { name: "Company KPIs" }).click();
  await expect(page.locator("#business-kpis-dialog")).toBeVisible();
  const box = await page.locator("#business-kpis-dialog").boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
  await expect(page.locator(".business-kpi-detail")).toBeVisible();
});
