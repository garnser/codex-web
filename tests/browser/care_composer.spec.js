const {test,expect}=require('@playwright/test');
const {mountSettings,openSettings}=require('./context_picker_fixture');
async function setup(page){
  await mountSettings(page);
  await page.evaluate(async()=>{

    window.scope={projectId:'home',threadId:'thread',repositoryResourceId:'repo'};
    window.calls=[];window.pending=null;
    const {installCareComposer}=await import('/static/care_composer.js');
    window.care=installCareComposer({prompt:document.querySelector('#prompt'),getScope:()=>window.scope,
      request:async(url,options)=>{window.calls.push({url,options});if(!options)return {items:[]};
        const fields=JSON.parse(options.body).fields;
        if(window.defer)return new Promise(resolve=>window.pending=resolve);
        return {message:'Action: '+fields.action,suggestions:[]};}});
  });
  await openSettings(page);await page.locator('#care-composer summary').click();
}
async function preview(page,text='Investigate defect'){
  await openSettings(page);
  if(!await page.locator('#care-composer').evaluate(node=>node.open))await page.locator('#care-composer summary').click();
  await page.locator('#care-action').fill(text);await page.locator('#care-preview').click();
  await expect(page.locator('#prompt')).toHaveValue('Action: '+text);
}
test('free text stays unchanged and structured preview permits editing',async({page})=>{
  await setup(page);await preview(page);await page.locator('#prompt').fill('Edited preview');
  expect(await page.evaluate(()=>care.prepare().message)).toBe('Edited preview');
  await openSettings(page);await page.locator('#care-composer summary').click();
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
  await openSettings(page);await page.locator('#care-action').fill('New work');
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

test('accepted callback clears optional mode before ordinary follow-up',async({page})=>{
  await setup(page);await preview(page);await page.locator('#prompt').fill('Edited task');
  await page.evaluate(()=>{window.ticket=care.prepare();document.querySelector('#prompt').value='';care.accepted(ticket);});
  await expect(page.locator('#care-composer')).not.toHaveAttribute('open');
  await page.locator('#prompt').fill('Ordinary follow-up');
  expect(await page.evaluate(()=>care.prepare())).toBeNull();
  await expect(page.locator('#thread-settings-menu')).not.toHaveAttribute('open');
});

test('late acceptance preserves newer fields and another scope',async({page})=>{
  await setup(page);await preview(page);
  await page.evaluate(()=>window.ticket=care.prepare());
  await openSettings(page);await page.locator('#care-action').fill('Newer task');
  await page.evaluate(()=>{document.querySelector('#prompt').value='';care.accepted(ticket);});
  await expect(page.locator('#care-action')).toHaveValue('Newer task');
  await expect(page.locator('#care-composer')).toHaveAttribute('open');
  await page.evaluate(()=>{scope={...scope,repositoryResourceId:'other'};care.sync();});
  await page.locator('#care-action').fill('Other repository task');
  await page.evaluate(()=>{document.querySelector('#prompt').value='';care.accepted(ticket);});
  await expect(page.locator('#care-action')).toHaveValue('Other repository task');
  await expect(page.locator('#care-composer')).toHaveAttribute('open');
});

test('mobile fields and keyboard preview remain usable',async({page})=>{
  await page.setViewportSize({width:390,height:844});await setup(page);
  await page.locator('#care-action').fill('Keyboard task');
  await page.locator('#care-preview').focus();await page.keyboard.press('Enter');
  await expect(page.locator('#prompt')).toBeFocused();
  await expect(page.locator('#prompt')).toHaveValue('Action: Keyboard task');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test('closed settings retain active composition and late preview cannot cross scope',async({page})=>{
  await setup(page);await page.locator('#care-action').fill('Original task');
  await page.evaluate(()=>window.defer=true);await page.locator('#care-preview').click();
  await expect(page.locator('#care-status')).toContainText('Composing preview');
  await page.locator('#thread-context-return').click();
  await page.evaluate(()=>{scope={...scope,projectId:'other'};care.sync();pending({message:'Wrong scope'});});
  await expect(page.locator('#prompt')).toHaveValue('');
  expect(await page.evaluate(()=>care.prepare().blocked)).toBe(true);
  await expect(page.locator('#care-preview')).toBeFocused();
  await page.evaluate(()=>{scope={...scope,projectId:'home'};care.sync();});
  await expect(page.locator('#care-action')).toHaveValue('Original task');
});

test('denied preview keeps fields and guidance visible in settings',async({page})=>{
  await mountSettings(page);
  await page.evaluate(async()=>{
    const {installCareComposer}=await import('/static/care_composer.js');
    window.care=installCareComposer({prompt:document.querySelector('#prompt'),getScope:()=>({projectId:'home'}),
      request:async(url,options)=>{if(options)throw Error('Permission denied');return {items:[]};}});
  });
  await openSettings(page);await page.locator('#care-composer summary').click();
  await page.locator('#care-action').fill('Retain this');await page.locator('#care-preview').click();
  await expect(page.locator('#care-status')).toContainText('Permission denied');
  await expect(page.locator('#care-action')).toHaveValue('Retain this');
  await expect(page.locator('#care-preview')).toBeEnabled();
});
