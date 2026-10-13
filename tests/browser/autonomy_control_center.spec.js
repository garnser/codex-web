const { resolveAction } = require('./action_confirmation_helpers');
const { test, expect } = require("@playwright/test");
const fixtureURL = (process.env.CODEX_WEB_BROWSER_BASE_URL || "http://127.0.0.1:18766") + "/tests/browser/autonomy_control_center_fixture.html";

async function cancellationFixture(page) {
  await page.goto(fixtureURL);
  await page.evaluate(()=>window.__localInstallation={
    plan_id:'upgrade-1',source_revision:'a'.repeat(40),target_revision:'b'.repeat(40),
    plan_status:'draining',maintenance_mode:true,installation_verified:false,
    plan_href:'/api/upgrades/upgrade-1',release_href:'/api/releases/release-1',
    current_service_instance_id:'current-instance',admission_epoch:4,
    cancellation_request_available:true,reported_diagnostics:[]});
  await page.locator('[data-acc-installation="upgrade-1"]').click();
  await page.getByLabel('Cancellation reason').fill('Target superseded before qualified installation');
}

test('Unexecuted cancellation confirms exact scope and preserves independent pause controls', async ({page})=>{
  await cancellationFixture(page);
  const button=page.getByRole('button',{name:'Request cancellation of unexecuted plan'});
  await button.focus(); await page.keyboard.press('Enter');
  const message=await resolveAction(page, false);
  expect(message).toContain('upgrade-1');
  expect(message).toContain('Other holds and paused controls remain effective');
  let requests=await page.evaluate(()=>window.__accRequests);
  expect(requests.some(r=>r.method==='POST')).toBeFalsy();
  await button.click(); await resolveAction(page);
  await expect(page.locator('#autonomy-control-center-card')).toContainText('Unexecuted plan cancelled; inspect Cancellation Evidence evidence-cancel');
  requests=await page.evaluate(()=>window.__accRequests);
  const mutations=requests.filter(r=>r.method!=='GET');
  expect(mutations).toHaveLength(1);
  expect(mutations[0].path).toBe('/api/upgrades/upgrade-1/cancel-unexecuted');
  expect(JSON.parse(mutations[0].body)).toEqual({
    reason:'Target superseded before qualified installation',running_revision:'a'.repeat(40),
    service_instance_id:'current-instance',expected_admission_epoch:4});
});

test('Cancellation denial is visible and keeps the request editable', async ({page})=>{
  await cancellationFixture(page);
  await page.evaluate(()=>window.__cancellationDenied=true);
  const button=page.getByRole('button',{name:'Request cancellation of unexecuted plan'});
  await button.click(); await resolveAction(page);
  await expect(page.locator('#autonomy-control-center-card')).toContainText('Cancellation denied or unavailable: local installer is active');
  await expect(button).toBeEnabled();
  await expect(page.getByLabel('Cancellation reason')).toHaveValue('Target superseded before qualified installation');
});

test('Local installation separates reported materialization from canonical verification', async ({page}) => {
  await page.goto(fixtureURL);
  const button=page.locator('[data-acc-installation="upgrade-1"]');
  await button.focus(); await page.keyboard.press('Enter');
  const region=page.locator('[data-acc-installation-result="upgrade-1"]');
  await expect(region).toBeVisible();
  await expect(button).toHaveAttribute('aria-expanded','true');
  await expect(region).toContainText('Installation has no completed canonical verification');
  await expect(region).toContainText('reported fetched');
  await expect(region).toContainText('Installer process unknown');
  await expect(region).toContainText('They grant no permission');
  const requests=await page.evaluate(()=>window.__accRequests);
  expect(requests.some(r=>r.path.endsWith('/local-installation') && r.method==='GET')).toBeTruthy();
  expect(requests.some(r=>r.method!=='GET')).toBeFalsy();
});

test('Local installation exposes already-present, stale failures and canonical Evidence', async ({page}) => {
  await page.goto(fixtureURL);
  await page.evaluate(()=>window.__localInstallation={
    source_revision:'source',target_revision:'target',plan_status:'completed',maintenance_mode:false,
    installation_verified:true,plan_href:'/api/upgrades/upgrade-1',release_href:'/api/releases/release-1',
    canonical_verification:{href:'/api/evidence/post'},process:{status:'exited',exit_code:0},
    reported_diagnostics:[{phase:'failed',failure_code:'fetch_failed',stale_instance:true,
      fetched:false,producer_identity_id:'operator',href:'/api/evidence/report'}]});
  await page.locator('[data-acc-installation="upgrade-1"]').click();
  const region=page.locator('[data-acc-installation-result="upgrade-1"]');
  await expect(region).toContainText('Installation verified by canonical post-upgrade evidence');
  await expect(region).toContainText('reported already present');
  await expect(region).toContainText('stale instance');
  await expect(region).toContainText('Failure fetch_failed');
  await expect(region).toContainText('exit alone does not prove installation');
  await expect(region.getByRole('link',{name:'Post-upgrade Evidence'})).toHaveAttribute('href','/api/evidence/post');
});

test('Local installation keeps denied reads visible', async ({page}) => {
  await page.goto(fixtureURL);
  await page.evaluate(()=>window.__localInstallationDenied=true);
  await page.locator('[data-acc-installation="upgrade-1"]').click();
  await expect(page.locator('[data-acc-installation-result="upgrade-1"]')).toContainText('unavailable or denied: upgrade administrator with MFA required');
});

test("Autonomy Control Center renders canonical M11 readiness without triggering work", async ({ page }) => {
  await page.goto(fixtureURL);

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
  await expect(card).toContainText("service instance control-plane-test · covered scopes 2 · native admissions 1 · fenced admissions 2");
  await expect(card).toContainText("evidence evidence-upgrade-1 · source aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa · target bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
  await expect(card).toContainText("state-store:postgresql");
  await expect(card).toContainText("Audit integrity · verified");
  await expect(card).toContainText("Simulated actions 2 · executed actions 1 · evaluation records 3");

  const requests = await page.evaluate(() => window.__accRequests);
  expect(requests.filter((item) => item.path === "/api/autonomy/control-center")).toHaveLength(1);
  expect(requests.some((item) => item.method !== "GET")).toBeFalsy();
});

test("Explain Action follows exact canonical stored provenance without model calls", async ({ page }) => {
  await page.goto(fixtureURL);
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
  await expect(card).toContainText("HIGH IMPACT · 1 resource(s) · risk high · provider gitlab/prod");

  const requests = await page.evaluate(() => window.__accRequests);
  expect(requests.some((item) => item.path === "/api/autonomy/control-center/actions/intent-1/explain" && item.method === "GET")).toBeTruthy();
  expect(requests.some((item) => /model|reason/.test(item.path) && item.method !== "GET")).toBeFalsy();
});

test("Control Center mutations use canonical autonomy and approval APIs", async ({ page }) => {
  await page.goto(fixtureURL);
  const card = page.locator("#autonomy-control-center-card");

  await card.locator("[data-acc-pause]").click();
  await expect(card.locator("[data-acc-mode]")).toHaveText("paused");

  await card.locator("[data-acc-approval='approval-1'][data-acc-outcome='approve']").click(); await resolveAction(page);
  await expect(card).toContainText("No pending canonical ApprovalRequests.");

  const requests = await page.evaluate(() => window.__accRequests);
  expect(requests.some((item) => item.path === "/api/autonomy/pause" && item.method === "POST")).toBeTruthy();
  const approval = requests.find((item) => item.path === "/api/approval-requests/approval-1/decisions" && item.method === "POST");
  expect(approval).toBeTruthy();
  const body = JSON.parse(approval.body);
  expect(body.outcome).toBe("approve");
  expect(body.idempotency_key).toContain("control-center:approval-1:approve:");
});

test("Control Center can create and remove canonical scoped pauses", async ({ page }) => {
  await page.goto(fixtureURL);
  const card = page.locator("#autonomy-control-center-card");

  await card.locator("[data-acc-pause-scope]").selectOption("project");
  await card.locator("[data-acc-pause-id]").fill("project-alpha");
  await card.locator("[data-acc-pause-reason]").fill("migration window");
  await card.locator("[data-acc-add-pause]").click();

  await expect(card).toContainText("project · project-alpha");
  await expect(card).toContainText("migration window");

  await card.locator("[data-acc-unpause='pause-1']").click(); await resolveAction(page);
  await expect(card).toContainText("No active scoped pauses.");

  const requests = await page.evaluate(() => window.__accRequests);
  expect(requests.some((item) => item.path === "/api/autonomy/scoped-pauses" && item.method === "POST")).toBeTruthy();
  expect(requests.some((item) => item.path === "/api/autonomy/scoped-pauses/pause-1" && item.method === "DELETE")).toBeTruthy();
});

test("Autonomy Control Center remains usable at phone width", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(fixtureURL);

  const card = page.locator("#autonomy-control-center-card");
  await expect(card).toBeVisible();
  const box = await card.boundingBox();
  expect(box.width).toBeLessThanOrEqual(390);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  expect(overflow).toBeFalsy();

  await card.locator("[data-acc-intent]").fill("intent-1");
  await card.locator("[data-acc-explain]").click();
  await expect(card.locator(".acc-chain .acc-stage")).toHaveCount(9);
});
