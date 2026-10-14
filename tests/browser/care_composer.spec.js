const {test,expect}=require('@playwright/test');
async function setup(page){
  await page.goto('http://127.0.0.1:18766/static/care_composer.js');
  await page.evaluate(async()=>{
    document.body.innerHTML='<link rel="stylesheet" href="/static/care_composer.css"><div class="composer"><textarea id="prompt"></textarea></div>';
    window.scope={projectId:'home',threadId:'thread',repositoryResourceId:'repo'};
    window.calls=[];window.pending=null;
    const {installCareComposer}=await import('/static/care_composer.js');
    window.care=installCareComposer({prompt:document.querySelector('#prompt'),getScope:()=>window.scope,
      request:async(url,options)=>{window.calls.push({url,options});if(!options)return {items:[]};
        const fields=JSON.parse(options.body).fields;
        if(window.defer)return new Promise(resolve=>window.pending=resolve);
        return {message:'Action: '+fields.action,suggestions:[]};}});
  });
  await page.locator('summary').click();
}
async function preview(page,text='Investigate defect'){
  await page.locator('#care-action').fill(text);await page.locator('#care-preview').click();
  await expect(page.locator('#prompt')).toHaveValue('Action: '+text);
}
test('free text stays unchanged and structured preview permits editing',async({page})=>{
  await setup(page);await preview(page);await page.locator('#prompt').fill('Edited preview');
  expect(await page.evaluate(()=>care.prepare().message)).toBe('Edited preview');
  await page.locator('summary').click();
  expect(await page.evaluate(()=>care.prepare())).toBeNull();
  await expect(page.locator('#prompt')).toHaveValue('Edited preview');
  expect(await page.evaluate(()=>localStorage.length)).toBe(0);
});
test('late preview cannot overwrite field edits',async({page})=>{
  await setup(page);await page.evaluate(()=>window.defer=true);
  await page.locator('#care-action').fill('Old');await page.locator('#care-preview').click();
  await expect.poll(()=>page.evaluate(()=>Boolean(window.pending))).toBe(true);
  await page.locator('#care-action').fill('New');
  await page.evaluate(()=>pending({message:'Old result'}));
  await expect(page.locator('#care-preview')).toBeEnabled();
  await expect(page.locator('#prompt')).toHaveValue('');
  expect(await page.evaluate(()=>care.prepare().blocked)).toBe(true);
});
test('scope switch preserves drafts and fences stale response',async({page})=>{
  await setup(page);await preview(page,'Home task');
  await page.evaluate(()=>{scope={...scope,repositoryResourceId:'other'};care.sync();});
  await expect(page.locator('#care-action')).toHaveValue('');
  expect(await page.evaluate(()=>care.prepare().blocked)).toBe(true);
  await preview(page,'Other task');
  await page.evaluate(()=>{scope={...scope,repositoryResourceId:'repo'};care.sync();});
  await expect(page.locator('#prompt')).toHaveValue('Action: Home task');
  expect(await page.evaluate(()=>care.prepare().message)).toBe('Action: Home task');
});
test('failed submission restores original request but preserves newer draft',async({page})=>{
  await setup(page);await preview(page);
  await page.evaluate(()=>{window.ticket=care.prepare();document.querySelector('#prompt').value='';care.rejected(ticket);});
  await expect(page.locator('#prompt')).toHaveValue('Action: Investigate defect');
  await page.locator('#care-action').fill('New work');
  await page.evaluate(()=>{document.querySelector('#prompt').value='';care.rejected(ticket);});
  await expect(page.locator('#care-action')).toHaveValue('New work');
  await page.evaluate(()=>{scope={...scope,threadId:'other'};care.sync();scope={...scope,threadId:'thread'};care.sync();});
  await expect(page.locator('#care-action')).toHaveValue('New work');
});
test('new Thread binds ticket to same scope and accepted request allows refinement',async({page})=>{
  await setup(page);await page.evaluate(()=>{scope={...scope,threadId:null};care.sync();});await preview(page);
  expect(await page.evaluate(()=>{window.ticket=care.prepare();scope={...scope,threadId:'created'};care.sync();return care.valid(ticket,'created');})).toBe(true);
  await page.evaluate(()=>{document.querySelector('#prompt').value='';care.accepted(ticket);});
  await expect(page.locator('#care-action')).toHaveValue('');
  await preview(page,'Refine this result');
  expect(await page.evaluate(()=>care.prepare().scope.threadId)).toBe('created');
});

test('mobile fields and keyboard preview remain usable',async({page})=>{
  await page.setViewportSize({width:390,height:844});await setup(page);
  await page.locator('#care-action').fill('Keyboard task');
  await page.locator('#care-preview').focus();await page.keyboard.press('Enter');
  await expect(page.locator('#prompt')).toBeFocused();
  await expect(page.locator('#prompt')).toHaveValue('Action: Keyboard task');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
