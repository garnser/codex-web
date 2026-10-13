const {test, expect} = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const app = fs.readFileSync(path.join(__dirname, '../../static/app.js'), 'utf8');
const functions = app.slice(app.indexOf('function setWaiting('), app.indexOf('function queuedDepth('))
  + app.slice(app.indexOf('function updateWaitingFromState('), app.indexOf('async function refreshQueueStatus('));

test('conversation status shows paused automation while preserving active and approval status', async ({page}) => {
  await page.setContent('<div id="codex-status"><span id="codex-status-text"></span></div><p id="queue-admission-status" role="status" hidden></p>');
  await page.addScriptTag({content: functions + `
    window.state = {threadId: 'thread', automationWaitByThread: new Map()};
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
  await expect(page.locator('#codex-status-text')).toHaveText('Automated work is waiting for autonomy to be enabled and resumed');
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
