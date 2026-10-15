const {test,expect}=require('@playwright/test');
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
let service, output, diagnostics, base;
const root=path.resolve(__dirname,'../..');
test.beforeEach(async({page})=>{
  output='';diagnostics='';
  const python=fs.existsSync(path.join(root,'.venv/bin/python'))?path.join(root,'.venv/bin/python'):(process.env.PYTHON||'python3');
  const env=Object.fromEntries(['PATH','HOME','TMPDIR','LANG','LC_ALL','VIRTUAL_ENV','PYTHONPATH'].filter(key=>process.env[key]).map(key=>[key,process.env[key]]));
  service=spawn(python,['tests/browser/fixtures/runner_failure_server.py'],{cwd:root,env,stdio:['ignore','pipe','pipe']});
  service.stderr.on('data',chunk=>{diagnostics+=chunk;});
  const port=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error(`Runner fixture timed out: ${diagnostics}`)),15000);
    service.once('error',error=>{clearTimeout(timer);reject(error);});
    service.once('exit',code=>{clearTimeout(timer);reject(Error(`Runner fixture exited ${code}: ${diagnostics}`));});
    service.stdout.on('data',chunk=>{output+=chunk;const match=output.match(/\{"port":\s*(\d+)\}/);if(match){clearTimeout(timer);resolve(Number(match[1]));}});
  });
  base=`http://127.0.0.1:${port}`;
  await expect.poll(async()=>{try{return (await page.request.get(`${base}/fixture/metadata`)).ok();}catch{return false;}}).toBe(true);
});
test.afterEach(async({},testInfo)=>{
  if(!service)return;
  let requestedStop=false;
  if(service.exitCode===null&&service.signalCode===null){
    const stopped=new Promise(resolve=>service.once('exit',resolve));
    requestedStop=true;service.kill('SIGTERM');await stopped;
  }
  const custody={fixture_pid:service.pid,exit_code:service.exitCode,signal:service.signalCode,requested_stop:requestedStop,
    shutdown_observed:output.includes('"fixture_terminal": true'),
    process_reaped:output.includes('"process_reaped": true'),
    executor_work_remaining_zero:output.includes('"executor_work_remaining": 0')};
  await testInfo.attach('runner-lifecycle-custody',{body:Buffer.from(JSON.stringify(custody)),contentType:'application/json'});
  console.log('RUNNER_FIXTURE_CUSTODY '+JSON.stringify(custody));
  // Uvicorn may re-raise the requested SIGTERM after its lifespan completes.
  // Preserve the actual signal and require cleanup evidence, not a fabricated0.
  expect(service.exitCode===0||(requestedStop&&service.signalCode==='SIGTERM'),diagnostics).toBe(true);
  expect(custody.shutdown_observed).toBe(true);
  expect(custody.process_reaped).toBe(true);
  expect(custody.executor_work_remaining_zero).toBe(true);
});
test('real supervised child loss shows scoped failure evidence and unknown outcome without replay',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto(`${base}/projects/home/threads`);
  const metadata=await (await page.request.get(`${base}/fixture/metadata`)).json();
  const retired=await (await page.request.post(`${base}/fixture/fail`)).json();
  expect(retired).toEqual({process_reaped:true,executor_work_remaining:0,source_retained:true,git_status_retained:true});
  await page.getByRole('button',{name:'Observe current custody'}).click();
  await expect(page.locator('#thread-meta')).toContainText('Runner stopped · diagnostic evidence unavailable');
  await expect(page.locator('#thread-meta')).toContainText('resource_limit · phase validation · cleanup stopped');
  await expect(page.locator('#thread-meta')).toContainText('unfinished work outcome unknown');
  await expect(page.locator('#thread-meta')).toContainText('reconcile provider receipts before recovery');
  await expect(page.locator('#thread-meta')).toHaveAttribute('data-active','false');
  const assignment=page.getByRole('link',{name:'View assignment',exact:true});
  await expect(assignment).toHaveAttribute('href',`/projects/home/workers?consumer_type=assignment&consumer_id=${metadata.assignment_id}`);
  await expect(page.locator('#history')).toContainText('inProgress');
  await page.request.post(`${base}/fixture/expire`);
  await page.getByRole('button',{name:'Observe current custody'}).click();
  await expect(page.locator('#thread-meta')).toContainText('Runner lost · recovery required');
  await expect(page.locator('#thread-meta')).toContainText('unfinished work outcome unknown');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button',{name:'Select other Project'}).click();
  await expect(page.getByRole('alert')).toContainText('Current Project custody unavailable');
  await expect(page.locator('[data-runner-failure]')).toHaveCount(0);
  await expect(page.getByRole('link',{name:'View assignment',exact:true})).toHaveCount(0);
});
