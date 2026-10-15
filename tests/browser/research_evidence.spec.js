const { test, expect } = require('@playwright/test');

for (const kind of ['artifact', 'evidence', 'verification']) test(`${kind} playbook reference focuses the exact canonical inspection row`, async ({page}) => {
  await page.route('**/playbook-reference?**', route => route.fulfill({contentType:'text/html',body:`<body data-project-id="project-a"><details id="developer-panel" open><button id="refresh-artifact-evidence">Refresh</button><div id="artifact-evidence-status"></div><div id="artifact-list"></div><div id="evidence-list"></div><div id="verification-list"></div></details><script src="/static/artifact_evidence_admin.js"></script></body>`}));
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/artifacts') return route.fulfill({json:{items:[{id:'artifact-exact',artifact_type:'input',name:'Pinned input',lifecycle:'active',project_id:'project-a'}]}});
    if (path === '/api/evidence') return route.fulfill({json:{items:[{id:'evidence-exact',evidence_type:'verification',result:'pass',lifecycle:'valid',artifact_ids:['artifact-exact'],project_id:'project-a'}]}});
    if (path === '/api/verifications') return route.fulfill({json:{items:[{id:'verification-exact',result:'verified',independent:true,method:'notification-original-outcome/1.0',project_id:'project-a',evidence_ids:['evidence-exact']}]}});
    if (path === '/api/identity/me') return route.fulfill({json:{identity_id:'viewer',roles:['member']}});
    return route.fulfill({json:{items:[]}});
  });
  await page.goto(`http://127.0.0.1:18766/playbook-reference?consumer_type=${kind}&consumer_id=${kind}-exact`);
  const row = page.locator(`[data-reference-kind="${kind}"][data-reference-id="${kind}-exact"]`);
  await expect(row).toHaveAttribute('open', ''); await expect(row).toBeFocused();
  if (kind === 'evidence') await expect(row.getByRole('link',{name:'View artifact: artifact-exact'})).toHaveAttribute('href','/projects/project-a/operations?consumer_type=artifact&consumer_id=artifact-exact');
});

test('research evidence inspection shows origin, exact Memory source links and gate denial', async ({ page }) => {
  await page.route('**/research_fixture.html', route => route.fulfill({contentType:'text/html',body:`
    <body data-project-id="project-a"><details id="developer-panel" open>
      <button id="refresh-artifact-evidence">Refresh</button>
      <div id="artifact-evidence-status"></div>
      <div id="artifact-list"></div><div id="evidence-list"></div><div id="verification-list"></div>
      <input id="artifact-evidence-work-item" value="group/app#42">
      <button id="evaluate-evidence-gate">Evaluate</button><div id="evidence-gate-result"></div>
      </details><script src="/static/artifact_evidence_admin.js"></script>
    </body>`}));
  await page.route('**/api/**', route => {
    const path=new URL(route.request().url()).pathname;
    if(path==='/api/artifacts')return route.fulfill({json:{items:[{
      id:'report-a',project_id:'project-a',artifact_type:'report',name:'Research report',lifecycle:'active',
      research_origin:'mixed',knowledge_ids:['observed-source','synthetic-source'],
      research_sources:[{knowledge_id:'observed-source',version:1,origin:'human_observed'},
        {knowledge_id:'synthetic-source',version:2,origin:'ai_inference'}],
    }]}});
    if(path==='/api/evidence')return route.fulfill({json:{items:[{
      id:'evidence-a',project_id:'project-a',evidence_type:'review',result:'pass',lifecycle:'valid',
      research_origin:'ai_inference',knowledge_ids:['synthetic-source'],
    }]}});
    if(path==='/api/identity/me')return route.fulfill({json:{identity_id:'viewer',roles:['member'],assurance:'mfa'}});
    if(path.startsWith('/api/evidence-requirements/work-items/'))return route.fulfill({json:{requirements:[{
      id:'real-users',evidence_type:'review',observed_research:true,
    }]}});
    if(path.startsWith('/api/evidence-evaluations/work-items/'))return route.fulfill({json:{
      work_item_ref:'group/app#42',satisfied:false,outcomes:[{requirement_id:'real-users',satisfied:false,
        reason:'requires current observed sources and independent human observed-user-research verification'}],
    }});
    return route.fulfill({json:{items:[]}});
  });
  await page.goto('http://127.0.0.1:18766/research_fixture.html');
  await page.locator('#refresh-artifact-evidence').click();
  await expect(page.locator('#artifact-list')).toContainText('Research origin: mixed');
  await expect(page.locator('#evidence-list')).toContainText('Research origin: ai_inference');
  await page.locator('#artifact-list details').evaluate(node=>{node.open=true});
  await expect(page.locator('#artifact-list').getByRole('link',{name:'observed-source@v1: human_observed'})).toHaveAttribute('href','/projects/project-a/memory?knowledge_id=observed-source');
  await page.locator('#evaluate-evidence-gate').click();
  await expect(page.locator('#evidence-gate-result')).toContainText('independent human observed-user-research verification');
  await expect(page.locator('#evidence-gate-result')).toContainText('Observed-source human proof required: yes');
});
