const { test, expect } = require('@playwright/test');

test('retained procedure runs keep original proof links and cannot advance the current run', async ({page}) => {
  const first = {schema_version:'1.1',id:'procedure-run-'+'a'.repeat(32),revision:4,
    definition:{record_id:'definition-original',definition_id:'readonly-mr',revision:1,checksum:'a'.repeat(64)},
    actor_identity_id:'actor',input_snapshot_id:'input-original',input_snapshot_checksum:'b'.repeat(64),
    resource_context_checksum:'c'.repeat(64),deadline_at:2000000000,started_at:100,status:'completed',
    deviations:[],steps:{observe:{status:'succeeded',attempts:1,action_intent_id:'intent-original',
      evidence:{receipt:'evidence-original'},output_snapshot_id:'output-original'}}};
  const second = {...first,id:'procedure-run-'+'b'.repeat(32),status:'ready',input_snapshot_id:'input-changed',
    steps:{observe:{status:'pending',attempts:0,evidence:{}}}};
  const assignment={id:'assignment-one',project_id:'project-a',resource_ids:['repo'],skill_refs:[],
    procedure_history:[first],procedure_checkpoint:second};
  const runs=[first,second].map(pin=>({id:pin.id,status:pin.status,definition:pin.definition,started_at:pin.started_at}));
  const fixture=`<!doctype html><body data-project-id="project-a"><section id="run"></section>
    <script type="module">import {installProcedureRun} from '/static/procedure_run_ui.js';
    installProcedureRun(document.querySelector('#run'),${JSON.stringify(assignment)});</script></body>`;
  const requests=[];
  await page.route('**/procedure-history-fixture',route=>route.fulfill({contentType:'text/html',body:fixture}));
  await page.route('**/api/**',route=>{
    const url=new URL(route.request().url());requests.push({method:route.request().method(),path:url.pathname,run:url.searchParams.get('run_id')});
    const pin=url.searchParams.get('run_id')===first.id?first:second;
    return route.fulfill({json:{assignment_id:assignment.id,current_run_id:second.id,procedure_checkpoint:pin,procedure_runs:runs}});
  });
  await page.goto(`${process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766'}/procedure-history-fixture`);
  const select=page.locator('[data-procedure-history]');
  await expect(select).toHaveValue(second.id);
  await select.selectOption(first.id);
  await expect(page.locator('[data-procedure-history-status]')).toContainText('Retained run');
  await expect(page.locator('[data-procedure-checkpoint]')).toContainText('intent-original');
  await expect(page.locator('[data-procedure-checkpoint]')).toContainText('evidence-original');
  await expect(page.locator('[data-procedure-action="advance"]')).toBeDisabled();
  await expect(page.locator('[data-procedure-run-link]')).toHaveAttribute('href',new RegExp('procedure_run='+first.id));
  expect(requests).toEqual([{method:'GET',path:'/api/execution-workers/assignments/assignment-one/procedure',run:first.id}]);
  await select.selectOption(second.id);
  await expect(page.locator('[data-procedure-action="advance"]')).toBeEnabled();
  await expect(page.locator('[data-procedure-checkpoint]')).not.toContainText('intent-original');
});
