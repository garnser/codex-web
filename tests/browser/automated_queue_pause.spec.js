const {test, expect} = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const app = fs.readFileSync(path.join(__dirname, '../../static/app.js'), 'utf8');
const queueUi = fs.readFileSync(path.join(__dirname, '../../static/queue_admission_status.js'), 'utf8');
// Extract these top-level declarations without depending on a neighboring
// feature's location: queue refresh now belongs to the runner UI module.
const functions = ['setWaiting', 'updateWaitingFromState'].map(name => {
  const match = app.match(new RegExp('^function ' + name + '\\([\\s\\S]*?^}', 'm'));
  if (!match) throw new Error(`Missing status function: ${name}`);
  return match[0];
}).join('\n');

test('conversation status shows paused automation while preserving active and approval status', async ({page}) => {
  await page.setContent('<div id="codex-status"><span id="codex-status-text"></span></div><p id="queue-admission-status" role="status" hidden></p>');
  await page.evaluate(async source => {
    const ui = await import('data:text/javascript;base64,' + btoa(source));
    window.renderQueueAdmissionStatus = ui.renderQueueAdmissionStatus;
    window.applyQueueAdmissionStatus = status => ui.applyQueueAdmissionStatus(window.state, status);
  }, queueUi);
  await page.addScriptTag({content: functions + `
    window.state = {threadId: 'thread', queueAdmissionMessageByThread: new Map()};
    window.$ = id => document.getElementById(id);
    window.busy = false;
    window.approval = false;
    window.isThreadBusy = () => window.busy;
    window.activeApprovalForThread = () => window.approval;
  `});
  await page.evaluate(() => {
    applyQueueAdmissionStatus({threadId:'thread', automationWaitReason:{message:'Automated work is waiting for autonomy to be enabled and resumed'}});
    updateWaitingFromState();
  });
  await expect(page.locator('#codex-status-text')).toHaveText('Idle');
  await expect(page.locator('#queue-admission-status')).toHaveText('Automated work is waiting for autonomy to be enabled and resumed');
  await expect(page.locator('#queue-admission-status')).toBeVisible();
  await page.evaluate(() => {window.busy = true; updateWaitingFromState();});
  await expect(page.locator('#codex-status-text')).toHaveText('Waiting for Codex');
  await expect(page.locator('#queue-admission-status')).toBeVisible();
  await expect(page.locator('#queue-admission-status')).toHaveText('Automated work is waiting for autonomy to be enabled and resumed');
  await page.evaluate(() => {window.approval = true; updateWaitingFromState();});
  await expect(page.locator('#codex-status-text')).toHaveText('Waiting for approval');
  await expect(page.locator('#queue-admission-status')).toBeVisible();
  await page.evaluate(() => {
    window.busy = window.approval = false;
    applyQueueAdmissionStatus({threadId:'thread', automationWaitReason:null});
    updateWaitingFromState();
  });
  await expect(page.locator('#codex-status-text')).toHaveText('Idle');
  await expect(page.locator('#queue-admission-status')).toBeHidden();
});

test('Equal human priority notice is scoped to the selected thread and coexists with automation pause', async ({page}) => {
  await page.setContent('<p id="queue-admission-status" role="status" hidden></p>');
  await page.evaluate(async source => {
    window.queueUi = await import('data:text/javascript;base64,' + btoa(source));
    window.queueState = {threadId:'selected', queueAdmissionMessageByThread:new Map()};
  }, queueUi);
  await page.evaluate(() => {
    queueUi.applyQueueAdmissionStatus(queueState, {threadId:'other',humanPriorityCount:1,
      humanPriorityReason:'Human messages have priority over automated work when this thread is ready.'});
    queueUi.renderQueueAdmissionStatus(queueState);
  });
  await expect(page.locator('#queue-admission-status')).toBeHidden();
  await page.evaluate(() => {
    queueUi.applyQueueAdmissionStatus(queueState, {threadId:'selected',humanPriorityCount:3,humanSlackPriorityCount:1,
      humanPriorityReason:'Human messages have priority over automated work when this thread is ready.',
      humanSlackPriorityReason:'Informational Slack-only count',
      automationWaitReason:{message:'Automated work is waiting for autonomy to be enabled and resumed'}});
    queueUi.renderQueueAdmissionStatus(queueState);
  });
  await expect(page.locator('#queue-admission-status')).toBeVisible();
  await expect(page.locator('#queue-admission-status')).toHaveText(
    'Human messages have priority over automated work when this thread is ready. Automated work is waiting for autonomy to be enabled and resumed');
  await page.evaluate(() => {
    queueUi.applyQueueAdmissionStatus(queueState, {threadId:'selected',humanPriorityCount:0,humanSlackPriorityCount:0,automationWaitReason:null});
    queueUi.renderQueueAdmissionStatus(queueState);
  });
  await expect(page.locator('#queue-admission-status')).toBeHidden();
});
