const {test,expect}=require('@playwright/test');
const base=process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766';
const fixture='/tests/browser/artifact_sharing_fixture.html';

test('canonical Artifact and Evidence rows expose bounded INFO sharing provenance without mutations',async({page})=>{
  await page.goto(base+fixture);
  await expect(page.locator('#artifact-list')).toContainText('Governed shared source information');
  await expect(page.locator('#evidence-list')).toContainText('INFO only; no independent QA or repository execution grant');
  await expect(page.locator('#artifact-list')).toContainText('policy-creation r2');
  const link=page.locator('#artifact-list a').filter({hasText:'artifact-original'});
  await expect(link).toHaveAttribute('href',/project-a\/operations\?consumer_type=artifact&consumer_id=artifact-original/);
  await expect(page.locator('[data-reference-id="artifact-ordinary"]')).not.toContainText('Governed shared source');
  await expect(page.locator('button')).toHaveCount(1);
  expect(await page.evaluate(()=>window.requests.every(r=>r.method==='GET'))).toBe(true);
});

test('untrusted metadata cannot create arbitrary HTML links or policy claims',async({page})=>{
  await page.goto(base+fixture);
  await expect(page.locator('#artifact-list')).toContainText('policy-creation r2');
  await page.evaluate(()=>{
    window.records.artifacts[0].metadata={source_artifact_id:'<img src=x onerror="window.injected=true">',
      sharing_policy_ref:'{"record_id":"<script>"}',sharing_rule_id:'javascript:alert(1)<script>'};
  });
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  await expect(page.locator('#artifact-list')).toContainText('Creation policy: unavailable');
  await expect(page.locator('#artifact-list img,#artifact-list script')).toHaveCount(0);
  expect(await page.evaluate(()=>window.injected)).toBeUndefined();
  expect(await page.evaluate(()=>window.requests.every(r=>r.method==='GET'))).toBe(true);
});

test('project change removes prior shared-input inspection rows',async({page})=>{
  await page.goto(base+fixture);
  await expect(page.locator('#artifact-list')).toContainText('Shared source');
  await page.evaluate(()=>window.dispatchEvent(new CustomEvent('codex:project-changed',{detail:{projectId:'project-b'}})));
  await expect(page.locator('#artifact-list')).toBeEmpty();
  await expect(page.locator('#evidence-list')).toBeEmpty();
  await expect(page.getByRole('status')).toContainText('Scope changed');
});
