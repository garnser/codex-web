const { resolveAction } = require('./action_confirmation_helpers');
const {test,expect}=require('@playwright/test');
test.beforeEach(async({page})=>{
 await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
 await page.setContent('<form id="editor"><label>Name<input name="name" required aria-describedby="hint"></label><p id="hint">A name</p><label>Callback<input name="url" type="url"></label></form>');
 await page.evaluate(async()=>{const {formValidation}=await import('/static/form_validation.js');window.validation=formValidation(document.querySelector('form'));});
});
test('invalid controls have inline descriptions, focused summary actions and preserved entries',async({page})=>{
 await page.locator('[name=url]').fill('broken');
 await page.evaluate(() => {
   const label = document.querySelector('[name=url]').closest('label');
   const details = document.createElement('details');
   details.innerHTML = '<summary>Advanced</summary>';
   label.before(details); details.append(label);
 });
 expect(await page.evaluate(()=>validation.validate())).toBe(false);
 await expect(page.locator('[data-field-error]')).toHaveCount(2);
 await expect(page.locator('[name=name]')).toBeFocused();
 await expect(page.locator('[name=name]')).toHaveAttribute('aria-invalid','true');
 await page.locator('[data-validation-summary] button').nth(1).click();
 await expect(page.locator('[name=url]')).toBeFocused();
 await expect(page.locator('[name=url]')).toHaveValue('broken');
 await page.locator('[name=name]').fill('Valid');await page.locator('[name=url]').fill('https://example.test');
 expect(await page.evaluate(()=>validation.validate())).toBe(true);
 await expect(page.locator('[data-validation-summary]')).toHaveCount(0);
 await expect(page.locator('[name=name]')).toHaveAttribute('aria-describedby','hint');
 await expect(page.locator('[name=name]')).not.toHaveAttribute('aria-invalid');
});
test('server errors map nested canonical fields without rendering input payloads or markup',async({page})=>{
 await page.evaluate(()=>validation.server({detail:[{loc:['body','definition','name'],msg:'Use another name',input:'secret-marker'},{loc:['body','unknown'],msg:'<img src=x onerror=alert(1)>'}]},{'definition.name':'name'}));
 await expect(page.locator('[name=name]')).toHaveAttribute('aria-invalid','true');
 await expect(page.locator('[data-validation-summary]')).not.toContainText('secret-marker');
 await expect(page.locator('[data-validation-summary] img')).toHaveCount(0);
});
test('unmapped codes show actionable general errors and retain values',async({page})=>{
 await page.locator('[name=name]').fill('Keep me');
 await page.evaluate(()=>validation.server({status:403,detail:{code:'scope_denied'}}));
 await expect(page.locator('[data-validation-summary]')).toContainText('Check your access');
 await expect(page.locator('[data-validation-summary]')).toBeFocused();
 await expect(page.locator('[name=name]')).toHaveValue('Keep me');
});

test('Automation required fields, malformed IDs, cross-field and server failures retain correct focus and values', async ({ page }) => {
  const writes = [];
  await page.route('**/api/**', async route => {
    if (route.request().method() === 'POST' && route.request().url().includes('/automations/drafts')) {
      writes.push(JSON.parse(route.request().postData()));
      return route.fulfill({ status: 422, json: { detail: [{ loc: ['body', 'definition', 'target', 'id'], msg: 'Choose an available Agent Profile.' }] } });
    }
    return route.fulfill({ json: { items: [] } });
  });
  await page.goto('http://127.0.0.1:18766/tests/browser/product_workspaces_fixture.html');
  await page.evaluate(() => window.CodexProductUI.openWorkspace('autonomy'));
  await page.locator('[data-automation-new]').click();
  const form = page.locator('[data-automation-editor]');
  await form.locator('[name=automation_id]').fill('BAD ID');
  await form.locator('[name=trigger_type]').selectOption('provider_event');
  await form.locator('button[type=submit]').click();
  await expect(form.locator('[name=automation_id]')).toHaveAttribute('aria-invalid', 'true');
  await expect(form.locator('[name=automation_id]')).toBeFocused();
  await expect(form.locator('[name=event_type]')).toHaveAttribute('aria-invalid', 'true');
  await expect(form.locator('[name=provider_id]')).toHaveAttribute('aria-invalid', 'true');
  expect(writes).toEqual([]);
  for (const [name,value] of Object.entries({ automation_id:'daily', name:'Daily review', target_id:'reviewer', instructions:'Review changes', event_type:'ci.completed', provider_id:'github' })) {
    await form.locator(`[name=${name}]`).fill(value);
  }
  await form.locator('button[type=submit]').click();
  await resolveAction(page);
  await expect(form.getByRole('alert')).toContainText('Choose an available Agent Profile.');
  await expect(form.locator('[name=target_id]')).toBeFocused();
  await expect(form.locator('[name=instructions]')).toHaveValue('Review changes');
  expect(writes).toHaveLength(1);
});

test('Configuration validates target and bounds together and preserves valid entries after canonical rejection', async ({ page }) => {
  const fs = require('node:fs');
  const path = require('node:path');
  const html = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
  await page.evaluate(markup => {
    const doc = new DOMParser().parseFromString(markup, 'text/html');
    document.body.innerHTML = doc.querySelector('#configuration-management-panel').closest('.developer-card').outerHTML;
    document.querySelector('#configuration-management-panel').open = true;
  }, html);
  let fail = true;
  const writes = [];
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/api/identity/me') return route.fulfill({ json: {
      identity_id:'operator', principal_kind:'human', assurance:'local_trusted', roles:['owner'], workspace_id:'default', organization_id:'local',
    } });
    if (url.pathname === '/api/configuration/drafts') {
      writes.push(JSON.parse(route.request().postData()));
      return route.fulfill(fail ? {status:422,json:{detail:[{loc:['body','value'],msg:'This value is incompatible with the current policy.'}]}}
        : {json:{record:{revision:2}}});
    }
    return route.fulfill({json:{items:[]}});
  });
  await page.evaluate(async () => { await import('/static/configuration_management.js'); });
  await expect.poll(() => page.evaluate(() => {
    window.dispatchEvent(new CustomEvent('codex:configuration-state-rendered', { detail: {
      specs:[{key:'test.limit',value_kind:'integer',editable:true,allowed_scopes:['project'],minimum:1,maximum:10}],
      projects:[{id:'home',name:'Home'}],
    } }));
    return document.querySelector('#configuration-draft-key').options.length;
  })).toBe(1);
  const root = page.locator('#configuration-management-panel');
  await root.locator('#configuration-draft-value').fill('25');
  await root.locator('#configuration-draft-reason').fill('Keep this reason');
  await root.locator('#create-configuration-draft').click();
  await expect(root.locator('[data-field-error]')).toHaveCount(2);
  expect(writes).toEqual([]);
  await root.locator('#configuration-draft-project').selectOption('home');
  await root.locator('#configuration-draft-value').fill('5');
  page.once('dialog', dialog => dialog.accept());
  await root.locator('#create-configuration-draft').click();
  await expect(root.getByRole('alert')).toContainText('incompatible with the current policy');
  await expect(root.locator('#configuration-draft-value')).toBeFocused();
  await expect(root.locator('#configuration-draft-reason')).toHaveValue('Keep this reason');
  expect(writes).toHaveLength(1);
  fail = false;
  page.once('dialog', dialog => dialog.accept());
  await root.locator('#create-configuration-draft').click();
  await expect(root.locator('#configuration-management-status')).toContainText('Created test.limit draft r2');
  await expect(root.locator('[data-validation-summary]')).toHaveCount(0);
});
