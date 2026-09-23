import { expect, test } from "@playwright/test";

const fixture = "http://127.0.0.1:18766/tests/browser/attention_ui_fixture.html";

function item(index) {
  return {
    id: `attention-${index}`,
    organization_id: "local",
    workspace_id: "default",
    project_id: index % 2 ? "project-b" : "project-a",
    type: index % 2 ? "approval.required" : "runtime.remediation",
    severity: index === 0 ? "critical" : "high",
    source: {
      object_type: index % 2 ? "approval_request" : "work_item",
      object_id: index % 2 ? `approval-${index}` : `work-${index}`,
      event_id: null,
    },
    reason: `Human action required ${index}`,
    dedupe_key: `dedupe-${index}`,
    owner_identity_id: index % 2 ? "operator-b" : "operator-a",
    recipient_identity_ids: [],
    recipient_team_ids: [],
    due_at: null,
    expires_at: null,
    deep_link: `/?work_item=work-${index}`,
    escalation: null,
    escalation_schedule_id: null,
    status: "open",
    acknowledged_by_identity_id: null,
    acknowledged_at: null,
    snoozed_until: null,
    resolved_by_identity_id: null,
    resolved_at: null,
    resolution_reason: null,
    escalation_count: 0,
    revision: 1,
    created_at: 1,
    created_by: "test",
    updated_at: 1,
    updated_by: "test",
  };
}

async function installRoutes(page) {
  const requests = [];
  const approvalDecisions = [];
  await page.route("**/api/approval-requests/*/decisions", async (route) => {
    approvalDecisions.push({
      path: new URL(route.request().url()).pathname,
      payload: JSON.parse(route.request().postData() || "{}"),
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ approval_request: { status: "approved" } }),
    });
  });
  await page.route("**/api/attention?**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(url.search);
    const limit = Number(url.searchParams.get("limit") || 50);
    const cursor = Number(url.searchParams.get("cursor") || 0);
    if (limit === 1) {
      await route.fulfill({ json: {
        attention_items: [item(0)],
        next_cursor: 1,
        has_more: true,
        total: 30,
      }});
      return;
    }
    const all = Array.from({ length: 30 }, (_, index) => item(index));
    const pageItems = all.slice(cursor, cursor + limit);
    const next = cursor + pageItems.length < all.length ? cursor + pageItems.length : null;
    await route.fulfill({ json: {
      attention_items: pageItems,
      next_cursor: next,
      has_more: next !== null,
      total: all.length,
    }});
  });
  requests.approvalDecisions = approvalDecisions;
  return requests;
}

test("Inbox consumes bounded pages and supports keyboard queue navigation", async ({ page }) => {
  const requests = await installRoutes(page);
  await page.goto(fixture);

  await page.getByRole("button", { name: /Inbox/ }).click();
  const dialog = page.locator(".attention-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".attention-card")).toHaveCount(25);
  await expect(dialog.locator(".attention-card").first()).toBeFocused();

  await page.keyboard.press("ArrowDown");
  await expect(dialog.locator(".attention-card").nth(1)).toBeFocused();
  await page.keyboard.press("End");
  await expect(dialog.locator(".attention-card").nth(24)).toBeFocused();

  await dialog.getByRole("button", { name: "Load more" }).click();
  await expect(dialog.locator(".attention-card")).toHaveCount(30);
  await expect(dialog.locator("[data-attention-status]")).toContainText("30 of 30");
  expect(requests.some((query) => query.includes("limit=25") && query.includes("cursor=25"))).toBeTruthy();
  await expect(dialog.getByRole("button", { name: "Load more" })).toBeHidden();
});

test("Inbox remains usable at phone width without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installRoutes(page);
  await page.goto(fixture);
  await page.getByRole("button", { name: /Inbox/ }).click();

  const dialog = page.locator(".attention-dialog");
  await expect(dialog).toBeVisible();
  const bounds = await dialog.boundingBox();
  expect(bounds.width).toBeLessThanOrEqual(390);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await expect(dialog.locator(".attention-card").first()).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Acknowledge" }).first()).toBeVisible();
});


test("Inbox sends canonical project type and assignee filters", async ({ page }) => {
  const requests = await installRoutes(page);
  await page.goto(fixture);
  await page.getByRole("button", { name: /Inbox/ }).click();

  const dialog = page.locator(".attention-dialog");
  await dialog.getByLabel("Project").fill("project-a");
  await dialog.getByLabel("Project").press("Tab");
  await expect.poll(() => requests.some((query) => query.includes("project_id=project-a"))).toBeTruthy();

  await dialog.getByLabel("Type").fill("approval.required");
  await dialog.getByLabel("Type").press("Tab");
  await expect.poll(() => requests.some((query) => query.includes("type=approval.required"))).toBeTruthy();

  await dialog.getByLabel("Assignee").fill("me");
  await dialog.getByLabel("Assignee").press("Tab");
  await expect.poll(() => requests.some((query) => query.includes("assignee=me"))).toBeTruthy();

  await expect(dialog.locator(".attention-card").first()).toContainText("Project:");
});


test("approval Attention uses canonical approve/reject actions and hides local resolve", async ({ page }) => {
  const requests = await installRoutes(page);
  await page.goto(fixture);
  await page.getByRole("button", { name: /Inbox/ }).click();

  const dialog = page.locator(".attention-dialog");
  const approvalCard = dialog.locator('[data-attention-id="attention-1"]');
  await expect(approvalCard).toBeVisible();
  await expect(approvalCard.getByRole("button", { name: "Approve" })).toBeVisible();
  await expect(approvalCard.getByRole("button", { name: "Reject" })).toBeVisible();
  await expect(approvalCard.getByRole("button", { name: "Resolve" })).toHaveCount(0);

  await approvalCard.getByRole("button", { name: "Approve" }).click();
  await expect.poll(() => requests.approvalDecisions.length).toBe(1);
  expect(requests.approvalDecisions[0].path).toBe(
    "/api/approval-requests/approval-1/decisions",
  );
  expect(requests.approvalDecisions[0].payload.outcome).toBe("approve");
  expect(requests.approvalDecisions[0].payload.reason).toBe("Attention Inbox decision");
  expect(requests.approvalDecisions[0].payload.idempotency_key).toContain(
    "attention:attention-1:approve:",
  );
});
