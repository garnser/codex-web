import assert from 'node:assert/strict';
import test from 'node:test';
import {createWorkspacePresenters, workspaceQuotaText} from '../../static/execution_workspace_presenters.js';

test('quota diagnostics use supplied effective limits, including configured values above eight', () => {
  assert.equal(workspaceQuotaText({max_active_per_identity:32, max_active_per_tenant:64,
    max_resources_per_workspace:16, max_requested_disk_bytes:1024}),
  'Effective limits: 32 active per identity · 64 active per tenant · 16 resources per workspace · 1024 requested bytes per workspace.');
  assert.equal(workspaceQuotaText(null), 'Effective workspace limits unavailable.');
  assert.match(workspaceQuotaText({max_active_per_identity:'<untrusted>'}), /unknown active per identity/);
});

const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g,
  character => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[character]));
const resourceNames = ids => ids.map(id => `Repository ${id}`).join(', ');
const timeText = () => 'not observed';

test('nonempty canonical repository members render caller-formatted disk sizes and escaped provenance', () => {
  const sizes = [];
  const formatted = new Map([[1536, '1.50 KiB'], [0, '0 B']]);
  const bytes = value => { sizes.push(value); return formatted.get(value); };
  const {repositoryMembersHtml} = createWorkspacePresenters({escapeHtml, resourceNames, timeText, bytes});
  const revision = '5dbf312378b8461011b05607baf1f88be05b6c83';
  const html = repositoryMembersHtml({repository_members:[{
    resource_id:'repository-main', access_mode:'write',
    source_path:'/public/source', workspace_path:'/public/workspace',
    sandbox_path:'/workspace', branch_name:'source/<untrusted>',
    base_revision:revision, head_revision:revision, disk_bytes:1536,
  }, {
    resource_id:'repository-reference', access_mode:'read',
    source_path:'/public/reference', workspace_path:'/public/reference-copy',
    sandbox_path:'/reference', branch_name:null,
    base_revision:revision, head_revision:revision, disk_bytes:0,
  }]});
  assert.deepEqual(sizes, [1536, 0]);
  assert.match(html, /disk 1\.50 KiB/);
  assert.match(html, /disk 0 B/);
  assert.match(html, /Repository repository-main.*write/);
  assert.match(html, /Repository repository-reference.*read/);
  assert.match(html, /source\/&lt;untrusted&gt;/);
  assert.doesNotMatch(html, /<untrusted>/);
  assert.match(html, /detached\/read-only/);
  assert.ok(html.includes(`Revision: ${revision} → ${revision}`));
});

test('legacy empty repository projection does not call the size formatter', () => {
  const {repositoryMembersHtml} = createWorkspacePresenters({escapeHtml, resourceNames, timeText,
    bytes: () => { throw new Error('no size exists for the legacy projection'); }});
  assert.match(repositoryMembersHtml({}), /legacy\/single-repository projection/);
  assert.match(repositoryMembersHtml({repository_members:[]}), /legacy\/single-repository projection/);
});

test('nonrepository READ context renders canonical provenance without a database mount', () => {
  const {repositoryMembersHtml} = createWorkspacePresenters({escapeHtml, resourceNames, timeText, bytes: String});
  const html = repositoryMembersHtml({resource_context: {schema_version:'1.0',
    read_only_resource_ids:['database<unsafe>'],requester_identity_id:'original-requester',work_item_ref:'scope#1530'},
    repository_members:[]});
  assert.match(html, /database&lt;unsafe&gt;.*READ/);
  assert.match(html, /original requester original-requester/);
  assert.match(html, /Work Item scope#1530/);
  assert.match(html, /no credentials, network or data mount/);
  assert.doesNotMatch(html, /database<unsafe>/);
});
