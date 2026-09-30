const { resolveAction } = require('./action_confirmation_helpers');
const { test, expect } = require("@playwright/test");

function scopedAutomation(project) {
  return { id: `automation-${project}`, definition: {
    name: `Automation ${project}`, lifecycle: 'enabled', trigger: { type: 'manual' },
    target: { kind: 'agent_profile', id: 'agent-a' }, instructions: 'Bounded review',
    budget: { max_concurrency: 1 },
  }, definitionRef: { record_id: `definition-${project}`, revision: 1 } };
}

async function switchAutomationProject(page, projectId) {
  await page.evaluate(id => {
    document.body.dataset.activeProject = id;
    window.dispatchEvent(new CustomEvent('codex:project-changed', { detail: { projectId: id } }));
  }, projectId);
}

for (const delayed of ['list', 'history']) {
  test(`Automation ${delayed} responses cannot cross Project switches`, async ({ page }) => {
    let release;
    let pending = false;
    let settled = false;
    const held = new Promise(resolve => { release = resolve; });
    const calls = [];
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const project = url.searchParams.get('project_id');
      calls.push(project);
      if (project === 'project-a' && ((delayed === 'list' && url.pathname === '/api/automations')
        || (delayed === 'history' && url.pathname.endsWith('/runs')))) {
        pending = true;
        await held;
        settled = true;
      }
      const items = project && project !== 'empty'
        ? url.pathname.endsWith('/runs')
          ? [{ id: `run-${project}`, status: 'succeeded', project_id: project }]
          : [scopedAutomation(project)] : [];
      await route.fulfill({ json: { items } });
    });
    await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
    await page.evaluate(() => window.CodexProductUI.openWorkspace('autonomy'));
    const card = page.locator('[data-automation-product]');
    await switchAutomationProject(page, 'project-a');
    await expect.poll(() => pending).toBe(true);
    await switchAutomationProject(page, 'project-b');
    await expect(card).toContainText('run-project-b');
    release();
    await expect.poll(() => settled).toBe(true);
    await expect(card).not.toContainText('Automation project-a');
    await expect(card).not.toContainText('run-project-a');
    await switchAutomationProject(page, 'empty');
    await expect(card).toContainText('No effective Automations');
    await expect(card.locator('[data-automation-run]')).toHaveCount(0);
    await switchAutomationProject(page, 'project-a');
    await expect(card).toContainText('run-project-a');
    const beforeClear = calls.length;
    await switchAutomationProject(page, '');
    await expect(card).toContainText('Select a Project');
    await expect(card.locator('[data-automation-new]')).toBeDisabled();
    await card.locator('[data-automation-refresh-list]').click();
    expect(calls.length).toBe(beforeClear);
  });
}

for (const action of ['manual', 'draft', 'unmounted-draft']) {
  test(`Project switch stops pending Automation ${action} before launch or publish`, async ({ page }) => {
    let release;
    let pending = false;
    let settled = false;
    const held = new Promise(resolve => { release = resolve; });
    const mutations = [];
    await page.route('**/api/**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === 'POST') {
        mutations.push({ path: url.pathname, body: JSON.parse(request.postData() || '{}') });
        pending = true;
        await held;
        await route.fulfill({ json: action.endsWith('draft') ? { record: { record_id: 'draft-a' } }
          : { run: { id: 'run-a' }, launchAllowed: true } });
        settled = true;
        return;
      }
      const project = url.searchParams.get('project_id');
      await route.fulfill({ json: { items: url.pathname === '/api/automations' && project
        ? [scopedAutomation(project)] : [] } });
    });
    await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
    await page.evaluate(() => window.CodexProductUI.openWorkspace('autonomy'));
    await switchAutomationProject(page, 'project-a');
    const card = page.locator('[data-automation-product]');
    await expect(card).toContainText('Automation project-a');
    if (action.endsWith('draft')) {
      await card.locator('[data-automation-edit]').click();
      await card.locator('[data-automation-editor] button[type="submit"]').click(); await resolveAction(page);
    } else {
      await card.locator('[data-automation-run-now]').click();
    }
    await expect.poll(() => pending).toBe(true);
    if (action === 'unmounted-draft') {
      await page.evaluate(() => {
        const host = document.querySelector('[data-product-workspace-host="autonomy"]');
        window.__detachedAutomation = { host, parent: host.parentElement };
        host.remove();
      });
    }
    await switchAutomationProject(page, 'project-b');
    if (action !== 'unmounted-draft') {
      await expect(card).toContainText('Automation project-b');
      await expect(card.locator('[data-automation-editor]')).toHaveCount(0);
    }
    release();
    await expect.poll(() => settled).toBe(true);
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 50)));
    expect(mutations).toHaveLength(1);
    expect(mutations[0].body.project_id).toBe('project-a');
    if (action === 'unmounted-draft') {
      await page.evaluate(() => {
        const { host, parent } = window.__detachedAutomation;
        parent.appendChild(host);
      });
      await switchAutomationProject(page, 'project-b');
      await expect(card).toContainText('Automation project-b');
    }
    await expect(card).not.toContainText('Automation project-a');
    await expect(card.locator('[data-automation-run-now]')).toBeEnabled();
  });
}

test("Automation workspace renders canonical definitions, provenance and run history", async ({ page }) => {
  const launches = [];
  const manualRuns = [];
  await page.route("**/api/**", async (route) => {
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
  await expect(card.locator('[data-action-state="succeeded"]')).toContainText("Automation run started");
});


test("Automation editor drafts and publishes through canonical APIs", async ({ page }) => {
  const drafts = [];
  const publishes = [];
  let published = false;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/automations" && request.method() === "GET") {
      const definition = {
        name: published ? "Edited review" : "Nightly review",
        description: "Review current Project health.",
        lifecycle: published ? "paused" : "enabled",
        trigger: { type: "canonical_event", event_type: "ci.pipeline", event_filter: {} },
        target: { kind: "agent_profile", id: "agent-james" },
        instructions: "Inspect current health.",
        skill_refs: [{ kind: "skill", definition_id: "review-skill", revision: 2, record_id: "skill-record", checksum: "abc", definition_schema_version: "1.0" }],
        execution_profile_ref: null,
        authority_ref: null,
        approval_required: false,
        owner_identity_id: "operator",
        budget: {
          max_concurrency: published ? 2 : 1,
          max_input_tokens: 12000,
          max_output_tokens: 2500,
          max_cost_usd: 1.5,
          max_duration_seconds: 600,
        },
        retry: { max_attempts: 2, backoff_seconds: 30 },
        dedupe_key_template: null,
        work_item_policy: "reuse_or_create",
        failure_attention: true,
      };
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [{
            id: "nightly-review",
            definition,
            definitionRef: {
              record_id: published ? "definition-record-10" : "definition-record-9",
              revision: published ? 5 : 4,
            },
          }],
        }),
      });
      return;
    }
    if (url.pathname === "/api/automations/nightly-review/runs") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
      return;
    }
    if (url.pathname === "/api/automations/drafts" && request.method() === "POST") {
      drafts.push(JSON.parse(request.postData() || "{}"));
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ record: { record_id: "draft-record-10" } }),
      });
      return;
    }
    if (url.pathname === "/api/automations/drafts/draft-record-10/publish" && request.method() === "POST") {
      publishes.push(JSON.parse(request.postData() || "{}"));
      published = true;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          record: { record_id: "definition-record-10", revision: 5 },
          schedule: null,
          scheduleError: null,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    });
  });

  await page.goto("http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html");
  await page.evaluate(() => window.CodexProductUI.openWorkspace("autonomy"));

  const card = page.locator("[data-automation-product]");
  await expect(card).toContainText("Nightly review");
  await card.locator("[data-automation-edit]").click();
  const editor = card.locator("[data-automation-editor]");
  await expect(editor).toBeVisible();
  await editor.locator('[name="name"]').fill("Edited review");
  await editor.locator('[name="lifecycle"]').selectOption("paused");
  await editor.locator('[name="max_concurrency"]').fill("2");
  await editor.locator('button[type="submit"]').click(); await resolveAction(page);

  await expect.poll(() => drafts.length).toBe(1);
  expect(drafts[0].automation_id).toBe("nightly-review");
  expect(drafts[0].project_id).toBe("home");
  expect(drafts[0].derived_from_record_id).toBe("definition-record-9");
  expect(drafts[0].definition.name).toBe("Edited review");
  expect(drafts[0].definition.lifecycle).toBe("paused");
  expect(drafts[0].definition.budget.max_concurrency).toBe(2);
  expect(drafts[0].definition.skill_refs[0].definition_id).toBe("review-skill");
  await expect.poll(() => publishes.length).toBe(1);
  expect(publishes[0].expected_active_revision).toBe(4);
  await expect(card).toContainText("Edited review");
  await expect(card).toContainText("paused");
});

test("Automation workspace remains usable on phone layout", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/**", async (route) => {
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
