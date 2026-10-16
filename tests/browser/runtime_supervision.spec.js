const {test,expect}=require('@playwright/test');
const base=process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766';
for(const width of [1440,1024,390])for(const theme of ['light','dark'])for(const state of ['active','idle'])
test(`runner header remains readable beside the complete toolbar (${width}, ${theme}, ${state})`,async({page},testInfo)=>{
  await page.setViewportSize({width,height:844});
  await page.goto(`${base}/tests/browser/runner_header_layout_fixture.html?theme=${theme}&state=${state}`);
  await expect(page.locator('body')).toHaveAttribute('data-ready','true');
  await expect(page.locator('#thread-meta [data-runner-state]')).toHaveAttribute('data-runner-state',state);
  const layout=await page.evaluate(()=>{
    const rect=selector=>{const r=document.querySelector(selector).getBoundingClientRect();return{width:r.width,height:r.height,left:r.left,right:r.right};};
    return{heading:rect('.thread-heading'),title:rect('h1'),badge:rect('.runner-status'),viewport:innerWidth,document:document.documentElement.scrollWidth};
  });
  if(width>900)expect(layout.heading.width).toBeGreaterThanOrEqual(320);
  expect(layout.title.height).toBeLessThanOrEqual(50);
  expect(layout.badge.height).toBeLessThanOrEqual(48);
  expect(layout.badge.left).toBeGreaterThanOrEqual(0);
  expect(layout.badge.right).toBeLessThanOrEqual(width);
  expect(layout.document).toBeLessThanOrEqual(width);
  await testInfo.attach('header-layout',{body:JSON.stringify(layout),contentType:'application/json'});
  if(state==='active'&&width!==1024)await page.screenshot({path:testInfo.outputPath('runner-header.png'),fullPage:true});
});
test('lost recovering stopped and actual activity remain distinct with unavailable diagnostics',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${base}/tests/browser/runtime_supervision_fixture.html`);
  await expect(page.locator('#lost')).toContainText('Runner lost');
  await expect(page.locator('#recovering')).toContainText('waiting for worker');
  await expect(page.locator('#stopped')).toContainText('diagnostic evidence unavailable');
  await expect(page.locator('#stopped')).toContainText('validation report captured · outcome unverified');
  await expect(page.locator('#lost')).toContainText('validation evidence unavailable');
  for(const id of ['lost','recovering','stopped']) await expect(page.locator(`#${id}`)).toHaveAttribute('data-active','false');
  await expect(page.locator('#running')).toHaveAttribute('data-active','true');
  await expect(page.locator('#observation')).toContainText('observed fence 1 · current fence 2');
  await expect(page.locator('#observation')).toContainText('completion not asserted');
  await expect(page.locator('#observation img')).toHaveCount(0);
  await expect(page.locator('#observation')).toContainText('<img src=x onerror=alert(1)>');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

for(const theme of ['light','dark']) test(`runner indicators retain visible contrast and shared truthful states (${theme})`,async({page},testInfo)=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${base}/tests/browser/runner_status_contrast_fixture.html?theme=${theme}`);
  await expect(page.locator('body')).toHaveAttribute('data-ready','true');
  const expected={active:'active',idle:'idle',waiting:'waiting',queued:'idle',recovering:'recovering',
    stopped:'stopped',lost:'lost',failed:'failed',unknown:'unknown',expired:'unknown',stale:'unknown',retained:'unknown'};
  for(const [id,status] of Object.entries(expected)){
    const rows=page.locator(`[data-case="${id}"]`);
    await expect(rows.filter({has:page.locator(`[data-runner-state="${status}"]`)})).toHaveCount(2);
    await page.getByRole('button',{name:id,exact:true}).click();
    await expect(page.locator('#thread-meta [data-runner-state]').first()).toHaveAttribute('data-runner-state',status);
    expect(await page.locator('#thread-meta [data-runner-state]').first().textContent())
      .toBe(await rows.first().locator('[data-runner-state]').first().textContent());
  }
  await expect(page.locator('[data-case="queued"] [data-runner-state="queued"]')).toHaveCount(2);
  const colors=await page.locator('.runner-status').evaluateAll(nodes=>{
    const rgb=s=>s.match(/[\d.]+/g).slice(0,3).map(Number);
    const luminance=c=>rgb(c).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);
    const contrast=(a,b)=>{const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
    const background=node=>{
      let c=[0,0,0,0];
      for(let parent=node;parent && c[3]<1;parent=parent.parentElement){
        const b=getComputedStyle(parent).backgroundColor.match(/[\d.]+/g).map(Number);b[3]??=1;
        const alpha=c[3]+b[3]*(1-c[3]);
        c=[...c.slice(0,3).map((v,i)=>alpha?(v*c[3]+b[i]*b[3]*(1-c[3]))/alpha:0),alpha];
      }
      return `rgb(${c.slice(0,3).join(',')})`;
    };
    return nodes.map(node=>{const s=getComputedStyle(node);return {text:node.textContent,color:s.color,background:s.backgroundColor,
      border:s.borderTopColor,textContrast:contrast(s.color,s.backgroundColor),indicatorContrast:contrast(s.borderTopColor,s.backgroundColor),
      activeFillContrast:contrast(s.backgroundColor,background(node.parentElement)),
      active:s.color!==s.backgroundColor && node.dataset.runnerState==='active'};});
  });
  for(const color of colors){expect(color.textContrast,color.text).toBeGreaterThanOrEqual(4.5);
    if(!color.active)expect(color.indicatorContrast,color.text).toBeGreaterThanOrEqual(3);
    else expect(color.activeFillContrast,color.text).toBeGreaterThanOrEqual(3);}
  await testInfo.attach('computed-contrast',{body:JSON.stringify(colors,null,2),contentType:'application/json'});
  await page.getByRole('button',{name:'active',exact:true}).click();
  await page.screenshot({path:testInfo.outputPath(`runner-status-${theme}.png`),fullPage:true});
  // A local expiry invalidates both surfaces without waiting for a network read.
  await page.evaluate(()=>{const {ui,state,control,renderThreads}=window.runnerContrast;
    const now=Date.now()/1000;ui.applyRunner({id:'active',runner:{...ui.runnerFor('active'),observedAt:now,leaseExpiresAt:now+.15},
      activity:{source:'current_turn',state:'active',turnId:'exact-current-turn'}});
    state.threadId='active';renderThreads();control.updateRunnerMeta('active');});
  await expect(page.locator('[data-case="active"] .runner-active')).toHaveCount(0);
  await expect(page.locator('#thread-meta .runner-active')).toHaveCount(0);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
