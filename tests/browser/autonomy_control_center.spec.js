const { test, expect } = require("@playwright/test");

test("Autonomy Control Center renders canonical M11 readiness without triggering work", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/autonomy_control_center_fixture.html");

  const card = page.locator("#autonomy-control-center-card");
  await expect(card).toBeVisible();
  await expect(card).toContainText("execute_bounded");
  await expect(card).toContainText("definition_incompatible:policy.old");
  await expect(card).toContainText("SEV1 incident: API unavailable");
  await expect(card).toContainText("web 1.2.3");
  await expect(card).toContainText("rto_not_satisfied");
  await expect(card).toContainText("Load-shed mode");
  await expect(card).toContainText("openai/codex · throttled");
  await expect(card).toContainText("1.0.0 → 1.1.0");
  await expect(card).toContainText("state-store:postgresql");
  await expect(card).toContainText("Audit integrity · verified");

  const requests = await page.evaluate(() => window.__accRequests);
  expect(requests.filter((item) => item.path === "/api/autonomy/control-center")).toHaveLength(1);
  expect(requests.some((item) => item.method !== "GET")).toBeFalsy();
});

test("Explain Action follows exact canonical stored provenance without model calls", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/autonomy_control_center_fixture.html");
  const card = page.locator("#autonomy-control-center-card");

  await card.locator("[data-acc-intent]").fill("intent-1");
  await card.locator("[data-acc-explain]").click();

  await expect(card).toContainText("ci.pipeline · gitlab:prod · event-1");
  await expect(card).toContainText("goal goal-1 · decision decision-1");
  await expect(card).toContainText("role release-manager authorized");
  await expect(card).toContainText("canonical:autonomy-policy");
  await expect(card).toContainText("execution-1 · action deploy.release");
  await expect(card).toContainText("gitlab/prod · deploy.release · receipts 1");
  await expect(card).toContainText("1 evidence · 1 verification(s) · succeeded");

  const requests = await page.evaluate(() => window.__accRequests);
  expect(requests.some((item) => item.path === "/api/autonomy/control-center/actions/intent-1/explain" && item.method === "GET")).toBeTruthy();
  expect(requests.some((item) => /model|reason/.test(item.path) && item.method !== "GET")).toBeFalsy();
});

test("Control Center mutations use canonical autonomy and approval APIs", async ({ page }) => {
  await page.goto("http://127.0.0.1:18766/tests/browser/autonomy_control_center_fixture.html");
  const card = page.locator("#autonomy-control-center-card");

  await card.locator("[data-acc-pause]").click();
  await expect(card.locator("[data-acc-mode]")).toHaveText("paused");

  await card.locator("[data-acc-approval='approval-1'][data-acc-outcome='approve']").click();
  await expect(card).toContainText("No pending canonical ApprovalRequests.");

  const requests = await page.evaluate(() => window.__accRequests);
  expect(requests.some((item) => item.path === "/api/autonomy/pause" && item.method === "POST")).toBeTruthy();
  const approval = requests.find((item) => item.path === "/api/approval-requests/approval-1/decisions" && item.method === "POST");
  expect(approval).toBeTruthy();
  const body = JSON.parse(approval.body);
  expect(body.outcome).toBe("approve");
  expect(body.idempotency_key).toContain("control-center:approval-1:approve:");
});

test("Autonomy Control Center remains usable at phone width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("http://127.0.0.1:18766/tests/browser/autonomy_control_center_fixture.html");

  const card = page.locator("#autonomy-control-center-card");
  await expect(card).toBeVisible();
  const box = await card.boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflow).toBeFalsy();

  await card.locator("[data-acc-intent]").fill("intent-1");
  await card.locator("[data-acc-explain]").click();
  await expect(card.locator(".acc-chain .acc-stage")).toHaveCount(8);
});