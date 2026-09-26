import { expect, test } from "@playwright/test";

const fixture = "http://127.0.0.1:18766/tests/browser/fixtures/ux-telemetry.html";

async function openFixture(page) {
  await page.goto(fixture);
  await page.waitForFunction(() => window.uxTelemetryReady === true);
}

test("enabled collector emits only the closed content-free contract", async ({ page }) => {
  let emitted = null;
  await page.route("**/api/ux-telemetry/policy", async (route) => {
    await route.fulfill({ json: { enabled: true, retention_days: 30, schema_version: "1.0" } });
  });
  await page.route("**/api/ux-telemetry/events", async (route) => {
    emitted = JSON.parse(route.request().postData() || "{}");
    await route.fulfill({ json: { accepted: true, event_id: "ux-test", schema_version: "1.0" } });
  });
  await openFixture(page);

  const result = await page.evaluate(async () => window.uxTelemetryFixture.trackUx(
    "workflow_completed",
    {
      workflow: "automation",
      step: "run_now",
      durationMs: 123.7,
      retryCount: 1,
      prompt: "TOP SECRET prompt",
      repository_content: "private source",
      metadata: { secret: "never emit" },
      form_value: "private form value",
    },
  ));

  expect(result).toBeTruthy();
  expect(emitted).not.toBeNull();
  expect(Object.keys(emitted).sort()).toEqual([
    "duration_ms",
    "event_name",
    "journey_id",
    "retry_count",
    "route_group",
    "step",
    "workflow",
  ]);
  expect(emitted.event_name).toBe("workflow_completed");
  expect(emitted.workflow).toBe("automation");
  expect(emitted.step).toBe("run_now");
  expect(emitted.duration_ms).toBe(124);
  expect(JSON.stringify(emitted)).not.toContain("TOP SECRET");
  expect(JSON.stringify(emitted)).not.toContain("private source");
  expect(JSON.stringify(emitted)).not.toContain("private form value");
});

test("disabled or unavailable policy fails closed without emitting events", async ({ page }) => {
  let eventRequests = 0;
  await page.route("**/api/ux-telemetry/policy", async (route) => {
    await route.fulfill({ json: { enabled: false, retention_days: 30, schema_version: "1.0" } });
  });
  await page.route("**/api/ux-telemetry/events", async (route) => {
    eventRequests += 1;
    await route.fulfill({ json: { accepted: true } });
  });
  await openFixture(page);

  const result = await page.evaluate(async () => window.uxTelemetryFixture.trackUx(
    "workflow_started",
    { workflow: "project_setup", step: "apply_plan" },
  ));
  expect(result).toBeFalsy();
  expect(eventRequests).toBe(0);
});

test("coarse route classification never emits resource identifiers", async ({ page }) => {
  await openFixture(page);
  const routes = await page.evaluate(() => {
    const classify = window.uxTelemetryFixture.coarseRoute;
    return [
      classify({ pathname: "/projects/private-project-123/overview", hash: "" }),
      classify({ pathname: "/projects/private-project-123/threads/secret-thread", hash: "" }),
      classify({ pathname: "/projects/private-project-123/overview", hash: "#workspace/autonomy" }),
      classify({ pathname: "/administration/secrets", hash: "" }),
    ];
  });
  expect(routes).toEqual([
    "project_overview",
    "project_threads",
    "project_overview",
    "administration",
  ]);
  expect(JSON.stringify(routes)).not.toContain("private-project-123");
  expect(JSON.stringify(routes)).not.toContain("secret-thread");
});
