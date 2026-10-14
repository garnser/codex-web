const {test,expect}=require('@playwright/test');
const pin={kind:'knowledge',record_id:'knowledge-source',revision:'1',sha256:'a'.repeat(64)};
async function setup(page,{deny=false}={}){
  const writes=[];
  await page.route('**/api/memory/**',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.pathname.includes('/sources/'))return route.fulfill({json:{source:pin}});
    if(request.method()==='GET')return route.fulfill({json:{items:[]}});
    const payload=request.postDataJSON();writes.push({path:url.pathname,payload});
    if(deny)return route.fulfill({status:403,json:{detail:'Memory write authority or step-up required'}});
    return route.fulfill({json:{item:{...payload,id:'knowledge-brief-1',version:1,freshness:'fresh'}}});
  });
  await page.goto('http://127.0.0.1:18766/static/ux_brief_editor.js');
  await page.evaluate(async()=>{
    document.body.innerHTML='<div id="host"><div class="memory-header"></div></div>';
    window.project='project-a';
    const {installUxBriefEditor}=await import('/static/ux_brief_editor.js');
    window.editor=installUxBriefEditor({host:document.querySelector('#host'),getProjectId:()=>window.project});
  });
  await page.locator('#ux-brief-editor summary').click();
  return writes;
}
async function draft(page){
  await page.locator('#ux-brief-key').fill('ux.recovery');
  await page.locator('#ux-brief-title').fill('Recovery brief');
  await page.locator('#ux-brief-summary').fill('Curated recovery needs');
  await page.locator('[data-brief-text="observed_needs"]').fill('Show task recovery status');
  await page.locator('[data-brief-sources="observed_needs"]').fill('knowledge:knowledge-source');
  await page.locator('[data-brief-text="approved_copy"]').fill('Try again');
}
test('authoring pins exact sources and writes a canonical Memory brief',async({page})=>{
  const writes=await setup(page);await draft(page);
  await page.locator('#ux-brief-save').click();
  await expect(page.locator('#ux-brief-editor-status')).toContainText('Inspect and pin');
  expect(writes).toHaveLength(0);
  await page.locator('#ux-brief-pin').click();
  await expect(page.locator('#ux-brief-pins')).toContainText(pin.sha256);
  await page.locator('#ux-brief-save').click();
  await expect(page.locator('#ux-brief-editor-status')).toContainText('Saved Memory');
  expect(writes).toHaveLength(1);
  expect(writes[0].payload.object_type).toBe('ux_context_brief');
  expect(writes[0].payload.project_id).toBe('project-a');
  expect(JSON.parse(writes[0].payload.content).sections.observed_needs.sources).toEqual([pin]);
  expect(await page.evaluate(()=>localStorage.length)).toBe(0);
});
test('denied save retains fields and pinned source attribution',async({page})=>{
  await setup(page,{deny:true});await draft(page);await page.locator('#ux-brief-pin').click();
  await expect(page.locator('#ux-brief-pins')).toContainText(pin.sha256);
  await page.locator('#ux-brief-save').click();
  await expect(page.locator('#ux-brief-editor-status')).toContainText('Save failed');
  await expect(page.locator('#ux-brief-title')).toHaveValue('Recovery brief');
  await expect(page.locator('[data-brief-text="approved_copy"]')).toHaveValue('Try again');
  await expect(page.locator('#ux-brief-pins')).toContainText(pin.sha256);
});
test('Project switching preserves original scoped draft',async({page})=>{
  await setup(page);await draft(page);
  await page.evaluate(()=>{window.project='project-b';editor.sync();});
  await expect(page.locator('#ux-brief-key')).toHaveValue('');
  await expect(page.locator('.ux-brief-scope')).toContainText('project-b');
  await page.evaluate(()=>{window.project='project-a';editor.sync();});
  await expect(page.locator('#ux-brief-key')).toHaveValue('ux.recovery');
  await expect(page.locator('[data-brief-text="observed_needs"]')).toHaveValue('Show task recovery status');
});
test('mobile keyboard source pinning remains usable without overflow',async({page})=>{
  await page.setViewportSize({width:390,height:844});await setup(page);await draft(page);
  await page.locator('#ux-brief-pin').focus();await page.keyboard.press('Enter');
  await expect(page.locator('#ux-brief-pins')).toContainText(pin.sha256);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
