const { defineConfig } = require('@playwright/test');
const base = require('./playwright.config');

// These files run in required visual/qualification jobs, or once on shard 1.
// File ownership avoids silently excluding unrelated tests by their titles.
module.exports = defineConfig({
  ...base,
  forbidOnly: true,
  testIgnore: [
    '**/visual_regression.spec.js',
    '**/routed_project_visual.spec.js',
    '**/accessibility_axe.spec.js',
    '**/repository_target_ui.spec.js',
    '**/execution_preflight.spec.js',
  ],
});
