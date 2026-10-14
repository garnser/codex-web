const {test,expect}=require('@playwright/test');
async function setup(page,{omitted=false}={}){
  await page.goto('http://127.0.0.1:18766/static/ux_brief_picker.js');
  await page.evaluate(async({omitted})=>{
    document.body.innerHTML='<div class="composer"><textarea id="prompt">Build a recovery view</textarea></div>';
    window.scope={projectId:'a',threadId:'t',repositoryResourceId:'repo'};
    const {installUxBriefPicker}=await import('/static/ux_brief_picker.js');
    window.picker=installUxBriefPicker({prompt:document.querySelector('#prompt'),getScope:()=>window.scope,
      request:async(path,options)=>options?{context:omitted?'':'Selected observations',packed_tokens:80,delivery_proven:false,
        omitted:omitted?[{section:'observed_needs',reason:'source_revision_changed'}]:[],
        selection:{...JSON.parse(options.body),sources:[{kind:'knowledge',record_id:'source-exact',revision:'2',sha256:'b'.repeat(64)}]}}:
        {items:[{id:'brief-1',title:'Recovery brief',version:1,content_sha256:'a'.repeat(64),freshness:'fresh',
          content:JSON.stringify({sections:{observed_needs:{text:'User needs'},approved_copy:{text:'Try again'}}})}]}});
  },{omitted});
  await page.locator('#ux-brief-picker summary').click();
  await page.locator('#ux-picker-brief').selectOption('brief-1');
  await page.locator('#ux-picker-sections input[value="observed_needs"]').check();
}
test('optional scoped picker sends exact selection without modifying free text',async({page})=>{
  await setup(page);
  expect(await page.evaluate(()=>window.picker.prepare().blocked)).toBe(true);
  await page.locator('#ux-picker-preview').click();
  await expect(page.locator('#ux-picker-status')).toContainText('delivery not yet proven');
  const ticket=await page.evaluate(()=>window.picker.prepare());
  expect(ticket.selection.sections).toEqual(['observed_needs']);
  expect(ticket.selection.sources[0].record_id).toBe('source-exact');
  await expect(page.locator('#ux-picker-sources a')).toHaveAttribute('href','/api/memory/source-exact');
  await expect(page.locator('#prompt')).toHaveValue('Build a recovery view');
  expect(await page.evaluate(()=>localStorage.length)).toBe(0);
});
test('stale or denied sources explain omissions and block submission',async({page})=>{
  await setup(page,{omitted:true});await page.locator('#ux-picker-preview').click();
  await expect(page.locator('#ux-picker-status')).toContainText('source_revision_changed');
  expect(await page.evaluate(()=>window.picker.prepare().blocked)).toBe(true);
});
test('scope changes preserve the original selection and reject stale tickets',async({page})=>{
  await setup(page);await page.locator('#ux-picker-preview').click();
  await page.evaluate(()=>{window.ticket=window.picker.prepare();window.scope.projectId='b';window.picker.sync();});
  expect(await page.evaluate(()=>window.picker.valid(window.ticket))).toBe(false);
  expect(await page.evaluate(()=>window.picker.prepare())).toBeNull();
  await page.evaluate(()=>{window.scope.projectId='a';window.picker.sync();});
  expect(await page.evaluate(()=>window.picker.prepare().selection.knowledge_id)).toBe('brief-1');
});
test('mobile keyboard selection fits viewport',async({page})=>{
  await page.setViewportSize({width:390,height:844});await setup(page);
  await page.locator('#ux-picker-preview').focus();await page.keyboard.press('Enter');
  await expect(page.locator('#ux-picker-status')).toContainText('Preview ready');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});
