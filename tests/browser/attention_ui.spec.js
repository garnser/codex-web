import { expect, test } from "@playwright/test";

const fixture = "http://127.0.0.1:18766/tests/browser/attention_ui_fixture.html";

async function switchAttentionProject(page, projectId) {
  await page.evaluate(id => {
    document.body.dataset.activeProject = id;
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: id } }));
  }, projectId);
}

test('Project Inbox fences late lists and badges and clears bulk selection', async ({ page }) => {
  let releaseA;
  let delayA = false;
  let pending = 0;
  let completed = 0;
  const held = new Promise(resolve => { releaseA = resolve; });
  const calls = [];
  const actions = [];
  await page.route(/\/api\/attention(?:[/?].*)?$/, async route => {
    const request = route.request();
    const url = new URL(request.url());
    const project = url.searchParams.get('project_id');
    calls.push(project);
    if (request.method() === 'POST') {
      actions.push({ project, body: JSON.parse(request.postData() || '{}') });
      return route.fulfill({ json: { attention_items: [], skipped_item_ids: [] } });
    }
    if (delayA && project === 'project-a') {
      pending += 1;
      await held;
      completed += 1;
    }
    const rows = project === 'project-a' ? [item(0)] : project === 'project-b' ? [item(1)] : [];
    await route.fulfill({ json: { attention_items: rows, total: rows.length, next_cursor: null } });
  });
  await page.goto(fixture);
  await switchAttentionProject(page, 'project-a');
  await page.getByRole('button', { name: /Inbox/ }).click();
  const dialog = page.locator('.attention-dialog');
  await expect(dialog.locator('[data-attention-id="attention-0"]')).toBeVisible();
  await dialog.locator('[data-attention-select]').check();
  await expect(dialog.locator('[data-attention-bulk-ack]')).toBeEnabled();
  await expect(dialog.getByLabel('Project', { exact: true })).toHaveAttribute('readonly', '');
  delayA = true;
  await switchAttentionProject(page, 'project-a');
  await expect.poll(() => pending).toBe(2);
  await switchAttentionProject(page, 'project-b');
  await expect(dialog.locator('[data-attention-id="attention-1"]')).toBeVisible();
  await expect(dialog.locator('[data-attention-id="attention-0"]')).toHaveCount(0);
  await expect(dialog.locator('[data-attention-bulk-ack]')).toBeDisabled();
  await dialog.locator('[data-attention-select]').check();
  await dialog.locator('[data-attention-bulk-ack]').click();
  await expect.poll(() => actions.length).toBe(1);
  expect(actions[0]).toEqual({ project: 'project-b', body: { item_ids: ['attention-1'] } });
  await switchAttentionProject(page, 'empty');
  await expect(dialog.locator('[data-attention-status]')).toContainText('0 of 0');
  releaseA();
  await expect.poll(() => completed).toBe(2);
  await expect(dialog.locator('.attention-card')).toHaveCount(0);
  await expect(page.locator('[data-attention-count]')).toBeHidden();
  delayA = false;
  await switchAttentionProject(page, 'project-a');
  await expect(dialog.locator('[data-attention-id="attention-0"]')).toBeVisible();
  await expect(page.locator('[data-attention-count]')).toHaveText('1');
  const beforeClear = calls.length;
  await switchAttentionProject(page, '');
  await expect(dialog.locator('.attention-card')).toHaveCount(0);
  await expect(dialog.locator('[data-attention-status]')).toContainText('Select a Project');
  await dialog.locator('[data-attention-refresh]').click();
  expect(calls.length).toBe(beforeClear);
});

test('Project switch stops a Work Item response chain after the pending comment', async ({ page }) => {
  let releaseComment;
  let commented = false;
  let settled = false;
  const held = new Promise(resolve => { releaseComment = resolve; });
  const actions = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === 'POST') {
      actions.push(path);
      if (path.endsWith('/comment')) {
        commented = true;
        await held;
        await route.fulfill({ json: { ok: true } });
        settled = true;
        return;
      }
      return route.fulfill({ json: { ok: true } });
    }
    const scope = new URL(route.request().url()).searchParams.get('project_id');
    const rows = scope === 'project-a' ? [item(0)] : [];
    return route.fulfill({ json: { attention_items: rows, total: rows.length } });
  });
  await page.goto(fixture);
  await switchAttentionProject(page, 'project-a');
  await page.getByRole('button', { name: /Inbox/ }).click();
  page.once('dialog', prompt => prompt.accept('Approved information'));
  await page.getByRole('button', { name: 'Provide info & retry' }).click();
  await expect.poll(() => commented).toBe(true);
  await switchAttentionProject(page, 'project-b');
  await expect(page.locator('[data-attention-status]')).toContainText('0 of 0');
  releaseComment();
  await expect.poll(() => settled).toBe(true);
  await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 50)));
  expect(actions).toEqual(['/api/work-items/work-0/comment']);
  await expect(page.locator('[data-attention-status]')).toContainText('0 of 0');
});

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
      event_id: index === 0 ? "event-1" : null,
    },
    reason: `Human action required ${index}`,
    dedupe_key: `dedupe-${index}`,
    owner_identity_id: index % 2 ? "operator-b" : "operator-a",
    recipient_identity_ids: [],
    recipient_team_ids: [],
    due_at: null,
    expires_at: null,
    deep_link: `/?work_item=work-${index}`,
    requesting_agent_profile_id: index === 0 ? "agent-profile-a" : null,
    requesting_agent_team_id: index === 0 ? "team-a" : null,
    evidence_ids: index === 0 ? ["evidence-1", "evidence-2"] : [],
    diagnostic_refs: index === 0 ? ["run:exec-1"] : [],
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
  const attentionActions = [];
  const workItemActions = [];
  const bulkAcknowledge = [];
  await page.route("**/api/attention/bulk/acknowledge", async (route) => {
    bulkAcknowledge.push(JSON.parse(route.request().postData() || "{}"));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ attention_items: [], skipped_item_ids: [] }),
    });
  });
  await page.route("**/api/work-items/*/comment", async (route) => {
    workItemActions.push({
      action: "comment",
      path: new URL(route.request().url()).pathname,
      payload: JSON.parse(route.request().postData() || "{}"),
    });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
  });
  await page.route("**/api/work-items/*/retry", async (route) => {
    workItemActions.push({
      action: "retry",
      path: new URL(route.request().url()).pathname,
      payload: JSON.parse(route.request().postData() || "{}"),
    });
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true }) });
  });
  await page.route("**/api/attention/*/resolve", async (route) => {
    workItemActions.push({
      action: "resolve",
      path: new URL(route.request().url()).pathname,
      payload: JSON.parse(route.request().postData() || "{}"),
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ attention_item: { status: "resolved" } }),
    });
  });
  await page.route("**/api/attention/*/reassign", async (route) => {
    attentionActions.push({
      action: "reassign",
      path: new URL(route.request().url()).pathname,
      payload: JSON.parse(route.request().postData() || "{}"),
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ attention_item: { status: "open" } }),
    });
  });
  await page.route("**/api/attention/*/escalate", async (route) => {
    attentionActions.push({
      action: "escalate",
      path: new URL(route.request().url()).pathname,
      payload: null,
    });
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ attention_item: { status: "escalated" } }),
    });
  });
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
  requests.attentionActions = attentionActions;
  requests.workItemActions = workItemActions;
  requests.bulkAcknowledge = bulkAcknowledge;
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

test("Inbox bulk acknowledge submits only explicitly selected actionable items", async ({ page }) => {
  const requests = await installRoutes(page);
  await page.goto(fixture);
  await page.getByRole("button", { name: /Inbox/ }).click();

  const dialog = page.locator(".attention-dialog");
  const bulk = dialog.getByRole("button", { name: "Acknowledge selected" });
  await expect(bulk).toBeDisabled();

  await dialog.locator('[data-attention-id="attention-0"] [data-attention-select]').check();
  await dialog.locator('[data-attention-id="attention-1"] [data-attention-select]').check();
  await expect(bulk).toHaveText("Acknowledge selected (2)");
  await bulk.click();

  await expect.poll(() => requests.bulkAcknowledge.length).toBe(1);
  expect(requests.bulkAcknowledge[0]).toEqual({
    item_ids: ["attention-0", "attention-1"],
  });
});

test("Inbox renders canonical requester and evidence provenance", async ({ page }) => {
  await installRoutes(page);
  await page.goto(fixture);
  await page.getByRole("button", { name: /Inbox/ }).click();

  const card = page.locator('[data-attention-id="attention-0"]');
  await expect(card).toContainText("Dedupe: dedupe-0");
  await expect(card).toContainText("Source event: event-1");
  await expect(card).toContainText("Requester: agent-profile-a");
  await expect(card).toContainText("Evidence: evidence-1, evidence-2");
  await expect(card).toContainText("Diagnostics: run:exec-1");
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


test("Inbox reassign and escalate use canonical Attention actions", async ({ page }) => {
  const requests = await installRoutes(page);
  await page.goto(fixture);
  await page.getByRole("button", { name: /Inbox/ }).click();

  const dialog = page.locator(".attention-dialog");
  const card = dialog.locator('[data-attention-id="attention-0"]');
  await expect(card.getByRole("button", { name: "Reassign" })).toBeVisible();
  await expect(card.getByRole("button", { name: "Escalate" })).toBeVisible();

  await card.getByRole("button", { name: "Escalate" }).click();
  await expect.poll(() => requests.attentionActions.length).toBe(1);
  expect(requests.attentionActions[0]).toMatchObject({
    action: "escalate",
    path: "/api/attention/attention-0/escalate",
  });

  page.once("dialog", (prompt) => prompt.accept("operator-c"));
  await card.getByRole("button", { name: "Reassign" }).click();
  await expect.poll(() => requests.attentionActions.length).toBe(2);
  expect(requests.attentionActions[1]).toMatchObject({
    action: "reassign",
    path: "/api/attention/attention-0/reassign",
    payload: { owner_identity_id: "operator-c" },
  });
});


test("Work Item Attention records human information and retries canonical work", async ({ page }) => {
  const requests = await installRoutes(page);
  await page.goto(fixture);
  await page.getByRole("button", { name: /Inbox/ }).click();

  const dialog = page.locator(".attention-dialog");
  const card = dialog.locator('[data-attention-id="attention-0"]');
  const respond = card.getByRole("button", { name: "Provide info & retry" });
  await expect(respond).toBeVisible();

  page.once("dialog", (prompt) => prompt.accept("Use the approved production endpoint."));
  await respond.click();

  await expect.poll(() => requests.workItemActions.length).toBe(3);
  expect(requests.workItemActions[0]).toEqual({
    action: "comment",
    path: "/api/work-items/work-0/comment",
    payload: { body: "Use the approved production endpoint." },
  });
  expect(requests.workItemActions[1]).toMatchObject({
    action: "retry",
    path: "/api/work-items/work-0/retry",
  });
  expect(requests.workItemActions[1].payload.reason).toContain("attention-0");
  expect(requests.workItemActions[2]).toMatchObject({
    action: "resolve",
    path: "/api/attention/attention-0/resolve",
  });
});
