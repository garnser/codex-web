const fs = require('fs');
const path = require('path');
const {expect} = require('@playwright/test');
const baseURL = process.env.CODEX_WEB_BROWSER_BASE_URL || 'http://127.0.0.1:18766';
const settingsHTML = fs.readFileSync(path.join(__dirname, '../../static/index.html'), 'utf8');
async function mountSettings(page, message = '') {
  // Fail before running assertions against assets from a different checkout.
  const response = await page.request.get(baseURL + '/static/care_composer.js');
  expect(await response.text()).toBe(fs.readFileSync(path.join(__dirname, '../../static/care_composer.js'), 'utf8'));
  await page.goto(baseURL + '/static/care_composer.js');
  await page.evaluate(async ({html, message}) => {
    const source = new DOMParser().parseFromString(html, 'text/html');
    // This focused fixture omits the app shell's sidebar/main grid. Keep its
    // settings anchor at the top, with the production popover CSS intact.
    document.body.style.display = 'block';
    const controls=document.createElement('div');controls.className='controls';
    controls.append(source.getElementById('thread-settings-menu'));
    document.body.replaceChildren(controls);
    const composer = document.createElement('div');composer.className = 'composer';
    const prompt = document.createElement('textarea');prompt.id = 'prompt';prompt.value = message;
    composer.append(prompt);document.body.append(composer);
    for (const href of ['design_tokens.css', 'workspace_components.css', 'styles.css', 'mobile_responsive.css', 'care_composer.css', 'ux_brief_editor.css']) {
      const link = document.createElement('link');link.rel = 'stylesheet';link.href = '/static/' + href;document.head.append(link);
    }
    const {install} = await import('/static/thread_settings_ui.js');
    install({byId: id => document.getElementById(id), activeThreadId: () => null,
      persistDefaults() {}, defaultProfileId: () => 'repository-write', renderProfile() {}, renderRepositories() {}});
  }, {html: settingsHTML, message});
}
async function openSettings(page) {
  const menu = page.locator('#thread-settings-menu');
  if (!await menu.evaluate(node => node.open)) await menu.locator(':scope > summary').click();
}
module.exports = {baseURL, mountSettings, openSettings};
