const { test, expect } = require('@playwright/test');
const { resolveAction } = require('./action_confirmation_helpers');
const base = process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766';

test('typed editor visibly preserves required input protocol while inspecting or changing version', async ({page}) => {
  const schema={type:'object',required:['schema_version'],properties:{
    schema_version:{type:'string',enum:['1.0','1.1','1.2'],default:'1.0'},
    input_protocol:{anyOf:[{type:'string',const:'code-host-mr-procedure-input.v1'},{type:'null'}],default:null}}};
  await page.route('**/api/skills/procedure-schema',route=>route.fulfill({json:{schema}}));
  await page.route('**/protocol-editor-fixture',route=>route.fulfill({contentType:'text/html',body:`<!doctype html><body data-project-id="project-a"><div id="editor"></div><script type="module">
    import {installProcedureEditor} from '/static/skill_procedure_editor.js';
    window.editor=await installProcedureEditor(document.querySelector('#editor'),{schema_version:'1.2',input_protocol:'code-host-mr-procedure-input.v1'},()=>true);
  </script></body>`}));
  await page.goto(`${base}/protocol-editor-fixture`);
  const protocol=page.getByLabel('input_protocol',{exact:true});
  await expect(protocol).toHaveValue('code-host-mr-procedure-input.v1');
  await expect(page.getByLabel('schema_version',{exact:true})).toHaveValue('1.2');
  expect(await page.evaluate(()=>window.editor.read())).toEqual({schema_version:'1.2',input_protocol:'code-host-mr-procedure-input.v1'});
  await page.getByLabel('schema_version',{exact:true}).selectOption('1.1');
  // The editor never silently drops a requirement to make an old version pass.
  // Canonical backend validation refuses this explicit incompatible combination.
  expect(await page.evaluate(()=>window.editor.read())).toEqual({schema_version:'1.1',input_protocol:'code-host-mr-procedure-input.v1'});
});

test('explicit current-use witness is sent only to current advance and remains dirty on refusal', async ({page}) => {
  const pin={schema_version:'1.1',id:'procedure-run-'+'a'.repeat(32),revision:1,status:'ready',
    definition:{record_id:'definition-one',definition_id:'governed-readiness',revision:1,checksum:'a'.repeat(64)},
    actor_identity_id:'actor',input_snapshot_id:'original-input',input_snapshot_checksum:'b'.repeat(64),
    resource_context_checksum:'c'.repeat(64),started_at:100,deadline_at:2000000000,steps:{observe:{status:'pending',attempts:0,evidence:{}}},deviations:[]};
  const assignment={id:'assignment-one',project_id:'project-a',resource_ids:['repo'],skill_refs:[],procedure_history:[],procedure_checkpoint:pin};
  const requests=[];
  await page.route('**/api/**',route=>{
    requests.push({path:new URL(route.request().url()).pathname,body:route.request().postDataJSON()});
    return route.fulfill({status:409,json:{detail:{code:'procedure_governed_input_invalid'}}});
  });
  await page.route('**/witness-fixture',route=>route.fulfill({contentType:'text/html',body:`<!doctype html><body data-project-id="project-a"><section id="run"></section><script type="module">
    import {installProcedureRun} from '/static/procedure_run_ui.js';
    installProcedureRun(document.querySelector('#run'),${JSON.stringify(assignment)});
  </script></body>`}));
  await page.goto(`${base}/witness-fixture`);
  const witness=page.locator('[data-current-input-witness]');
  await witness.fill('fresh-equal-input');
  await page.locator('[data-procedure-action="advance"]').click();
  await resolveAction(page);
  await expect(page.locator('[data-procedure-run-status]')).toContainText('Operation blocked');
  expect(requests).toEqual([{path:'/api/execution-workers/assignments/assignment-one/procedure/advance',body:{run_id:pin.id,current_input_snapshot_id:'fresh-equal-input'}}]);
  await expect(witness).toHaveValue('fresh-equal-input');
  await expect(witness.locator('xpath=../..').locator('[data-dirty-editor-status]')).toContainText('Unsaved changes');
  await expect(page.locator('[data-procedure-checkpoint]')).toContainText('original-input');
});
