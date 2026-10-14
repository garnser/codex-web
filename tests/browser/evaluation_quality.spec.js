const { test, expect } = require('@playwright/test');
const fs = require('fs');
const fixture = 'http://127.0.0.1:18766/tests/browser/evaluation_quality_fixture.html';

async function open(page) {
  await page.goto(fixture);
  await page.getByRole('button', {name:'Review A',exact:true}).click();
  const dialog = page.getByRole('dialog', {name:'Human semantic evaluation'});
  await expect(dialog).toContainText('project-run-a');
  return dialog;
}

test('inspection is read-only and escapes annotated findings', async ({page}) => {
  const dialog = await open(page);
  await expect(dialog).toContainText('Deterministic replay: passed');
  await expect(dialog).toContainText('semantic_fail');
  await expect(dialog).toContainText('<img src=x');
  await expect(dialog.locator('img')).toHaveCount(0);
  expect(await page.evaluate(()=>window.unsafe)).toBeUndefined();
  expect(await page.evaluate(()=>window.requests.every(item=>item.method==='GET'))).toBeTruthy();
});

test('manual annotation carries exact pins and criterion finding to canonical API', async ({page}) => {
  const dialog = await open(page);
  await dialog.getByLabel('Rating (0–4), optional').fill('0');
  await dialog.getByLabel('Criterion finding').fill('Synthetic unsupported recommendation');
  await dialog.getByRole('button',{name:'Record manual annotation'}).click();
  await expect(dialog).toContainText('Annotation retained as canonical Evidence');
  const writes=await page.evaluate(()=>window.requests.filter(item=>item.method==='POST'));
  expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe('/api/evaluations/runs/run-a/quality');
  expect(writes[0].body.rubric.record_id).toBe('rubric-1');
  expect(writes[0].body.artifact_revision).toBe('v1');
  expect(writes[0].body.ratings[0].finding).toBe('Synthetic unsupported recommendation');
  expect(writes[0].body.rater_ref).toBeUndefined();
});

test('denied submission retains draft and unchanged retry idempotency', async ({page}) => {
  const dialog = await open(page);
  await page.evaluate(()=>{window.deny=true;});
  await dialog.getByLabel('Rating (0–4), optional').fill('2');
  await dialog.getByLabel('Criterion finding').fill('Keep this draft');
  await dialog.getByRole('button',{name:'Record manual annotation'}).click();
  await expect(dialog).toContainText('MFA required');
  await expect(dialog.getByLabel('Criterion finding')).toHaveValue('Keep this draft');
  await page.evaluate(()=>{window.deny=false;});
  await dialog.getByRole('button',{name:'Record manual annotation'}).click();
  await expect(dialog).toContainText('Annotation retained');
  const writes=await page.evaluate(()=>window.requests.filter(item=>item.method==='POST'));
  expect(writes[0].body.idempotency_key).toBe(writes[1].body.idempotency_key);
});

test('late reply after closing does not replace another run view', async ({page}) => {
  await page.goto(fixture);
  await page.evaluate(()=>{window.hold=true;});
  await page.getByRole('button',{name:'Review A',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog).toContainText('Loading pinned review options');
  await dialog.getByRole('button',{name:'Close',exact:true}).click();
  await page.getByRole('button',{name:'Review B',exact:true}).click();
  await expect(dialog).toContainText('project-run-b');
  await page.evaluate(()=>{window.release.forEach(resolve=>resolve());});
  await expect(dialog).not.toContainText('project-run-a');
});

test('named baseline comparison renders aggregate and criterion deltas', async ({page}) => {
  const dialog=await open(page);
  await dialog.getByLabel('Named baseline annotation Evidence ID').fill('evidence-baseline');
  await dialog.getByRole('button',{name:'Compare baseline'}).click();
  await expect(dialog).toContainText('Named baseline fixture-baseline');
  await expect(dialog).toContainText('support delta: 0.5');
});

test('missing linked content blocks submission visibly', async ({page}) => {
  await page.goto(fixture);
  await page.evaluate(()=>{window.empty=true;});
  await page.getByRole('button',{name:'Review A',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog).toContainText('No active revision/digest-pinned output Artifact');
  await expect(dialog.getByRole('button',{name:'Record manual annotation'})).toBeDisabled();
});

test('review remains usable on mobile and Escape closes it', async ({page}) => {
  await page.setViewportSize({width:390,height:844});
  const dialog=await open(page);
  expect((await dialog.boundingBox()).width).toBeLessThanOrEqual(390);
  await dialog.getByLabel('Criterion finding').focus();
  await expect(dialog.getByLabel('Criterion finding')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).not.toBeVisible();
});

test('mounted deployment uses its prefix for review requests and provenance links', async ({page}) => {
  await page.route('**/codex/tests/browser/evaluation_quality_fixture.html', route => route.fulfill({
    contentType:'text/html', body:fs.readFileSync(__dirname+'/evaluation_quality_fixture.html','utf8'),
  }));
  await page.goto(fixture.replace('/tests/', '/codex/tests/'));
  await page.getByRole('button',{name:'Review A',exact:true}).click();
  const dialog=page.getByRole('dialog');
  await expect(dialog).toContainText('project-run-a');
  await expect(dialog.getByRole('link',{name:'Output Artifact'})).toHaveAttribute('href','/codex/api/artifacts/artifact-a');
  expect(await page.evaluate(()=>window.requests.every(item=>item.path.startsWith('/codex/api/')))).toBeTruthy();
});
