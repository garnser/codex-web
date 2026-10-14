const {test,expect}=require('@playwright/test');
const {mountSettings,openSettings}=require('./context_picker_fixture');
async function setup(page,{omitted=false}={}){
  await mountSettings(page,'Build a recovery view');
  await page.evaluate(async({omitted})=>{

    window.scope={projectId:'a',threadId:'t',repositoryResourceId:'repo'};
    const {installUxBriefPicker}=await import('/static/ux_brief_picker.js');
    window.picker=installUxBriefPicker({prompt:document.querySelector('#prompt'),getScope:()=>window.scope,
      request:async(path,options)=>options?{context:omitted?'':'Selected observations',packed_tokens:80,delivery_proven:false,
        omitted:omitted?[{section:'observed_needs',reason:'source_revision_changed'}]:[],
        selection:{...JSON.parse(options.body),sources:[{kind:'knowledge',record_id:'source-exact',revision:'2',sha256:'b'.repeat(64)}]}}:
        {items:[{id:'brief-1',title:'Recovery brief',version:1,content_sha256:'a'.repeat(64),freshness:'fresh',
          content:JSON.stringify({sections:{observed_needs:{text:'User needs'},approved_copy:{text:'Try again'}}})}]}});
  },{omitted});
  await openSettings(page);await page.locator('#ux-brief-picker summary').click();
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

test('late UX preview is fenced while settings are closed and original scope selection survives',async({page})=>{
  await setup(page);
  await page.evaluate(async()=>{
    document.querySelector('#ux-brief-picker').remove();
    const {installUxBriefPicker}=await import('/static/ux_brief_picker.js');
    window.picker=installUxBriefPicker({prompt:document.querySelector('#prompt'),getScope:()=>scope,
      request:async(path,options)=>options?new Promise(resolve=>window.pending=resolve):
        {items:[{id:'brief-1',title:'Brief',version:1,content_sha256:'a'.repeat(64),freshness:'fresh',
          content:JSON.stringify({sections:{observed_needs:{text:'Need'}}})}]}});
  });
  await page.locator('#ux-brief-picker summary').click();
  await page.locator('#ux-picker-brief').selectOption('brief-1');await page.locator('#ux-picker-sections input').check();
  await page.locator('#ux-picker-preview').click();await expect(page.locator('#ux-picker-status')).toContainText('Checking context');
  await page.locator('#thread-context-return').click();await expect(page.locator('#prompt')).toBeFocused();
  await page.evaluate(()=>{scope={...scope,repositoryResourceId:'other'};picker.sync();pending({context:'Wrong scope',selection:{knowledge_id:'brief-1'}});});
  expect(await page.evaluate(()=>picker.prepare())).toBeNull();await expect(page.locator('#ux-picker-context')).toHaveText('');
  await page.evaluate(()=>{scope={...scope,repositoryResourceId:'repo'};picker.sync();});
  expect(await page.evaluate(()=>picker.prepare().blocked)).toBe(true);
  await expect(page.locator('#ux-picker-preview')).toBeFocused();
  await expect(page.locator('#ux-picker-status')).toContainText('Thread settings → UX knowledge');
  await expect(page.locator('#prompt')).toHaveValue('Build a recovery view');
});
