import './page_editor.js';
import { projectPath, referenceLink, referencePath } from './reference_navigation.js';
import { request } from './api_client.js';
import { captureProjectView, currentProjectId } from './project_view_scope.js';
import { esc, secretLinks, bindWhenReady } from './reference_links.js';
import { trackDirtyEditor, confirmDiscard } from './dirty_editor.js';
import { formValidation } from './form_validation.js';
import { bindingForm, readBinding } from './provider_binding_form.js';

const style = document.createElement('link'); style.rel = 'stylesheet'; style.href = new URL('./provider_binding_management.css', import.meta.url).href; document.head.appendChild(style);
let generation = 0; let editor = null; let selected = null;
export function providerBindingPath(id) {
  return referencePath('agent_provider', id) || '#provider-binding-content';
}
function operation() {
  const view = captureProjectView(); const project = currentProjectId(); const opened = generation;
  const current = () => view.current() && opened === generation;
  return { current, async request(path, options) {
    if (!current()) throw new DOMException('View changed', 'AbortError');
    const url = new URL(path, location.origin); if (project) url.searchParams.set('project_id', project);
    const result = await request(url.pathname + url.search, options);
    if (!current()) throw new DOMException('View changed', 'AbortError'); return result;
  } };
}
function status(text) { const node = document.getElementById('provider-binding-status'); if (node) node.textContent = text; }
function ownerLink(page, label, query = {}) {
  const project = currentProjectId(); if (!project) return esc(label);
  const path = projectPath(page, query, project);
  return `<a href="${esc(path)}">${esc(label)}</a>`;
}
function impactHTML(impact) {
  const kinds = { profile: 'agent_profile', agent_provider: 'agent_provider', definition: 'definition_family', configuration: 'configuration_key' };
  return `<p>${esc(impact.effect)}</p><p>${esc(impact.limitations)}</p><p>${impact.available ? `${esc(impact.total)} explicit retained references${impact.truncated ? '; first 100 displayed' : ''}.` : esc((impact.blockers || []).join(' '))}</p><ul>${(impact.consumers || []).map(item => `<li>${esc(item.kind)} ${referenceLink(kinds[item.kind] || item.kind, item.id, { projectId: item.project_id || currentProjectId(), revision: item.revision })} · ${esc(item.status || '')}${item.revision ? ` · revision ${esc(item.revision)}` : ''}${item.project_id ? ` · Project ${esc(item.project_id)}` : ''}</li>`).join('')}</ul>`;
}
async function load(force = false) {
  const root = document.getElementById('provider-binding-content'); if (!root) return;
  if (!force && !confirmDiscard(editor)) return;
  generation++; editor?.dispose(); editor = null; root.replaceChildren(); const op = operation();
  status('Loading canonical provider bindings…');
  try {
    const [catalog, models] = await Promise.all([op.request('/api/agent-providers/administration'), op.request('/api/model-gateway/provider-administration')]);
    if (!op.current()) return;
    root.innerHTML = `<p>Shared workspace ${esc(catalog.organization_id)}/${esc(catalog.workspace_id)}. ${esc(catalog.runtime_ownership)}</p><div data-binding-list class="route-test"></div><div data-binding-detail></div>`;
    const list = root.querySelector('[data-binding-list]'); const detail = root.querySelector('[data-binding-detail]');
    let selection = 0;
    async function open(id, kind = 'agent') {
      if (!confirmDiscard(editor)) return;
      editor?.dispose(); editor = null; detail.replaceChildren(); selected = { id, kind }; const selectionAtStart = ++selection;
      const current = () => op.current() && selection === selectionAtStart;
      const endpoint = kind === 'model' ? '/api/model-gateway/providers/' : '/api/agent-providers/';
      const schema = kind === 'model' ? models.schema : catalog.schema;
      const canManage = kind === 'model' ? models.can_manage : catalog.can_manage;
      try {
        const impact = await op.request(`${endpoint}${encodeURIComponent(id)}/impact`);
        if (!current()) return;
        const provider = impact.provider;
        detail.innerHTML = `<h3 tabindex="-1">${esc(provider.display_name)} · revision / fingerprint ${esc(provider.revision || impact.expected_revision)}</h3><p>Owner: ${esc(impact.owner)} · lifecycle ${esc(provider.lifecycle || provider.status)} · health ${esc(provider.health || "reported by runtime/capacity surfaces")} · compatibility ${esc(provider.compatibility || "adapter-owned")}.</p><p>Credentials: ${secretLinks(provider.credential_refs || [provider.credential_ref])}</p><p>Linked model providers: ${(provider.model_provider_ids || []).map(model => ownerLink('agents', model, { consumer_type: 'model_provider', consumer_id: model })).join(', ') || 'none'}. Extension: ${provider.extension_installation_id ? ownerLink('integrations', provider.extension_installation_id, { consumer_type: 'extension', consumer_id: provider.extension_installation_id }) : 'none'}.</p>${impactHTML(impact)}`;
        if (impact.owner === 'model_gateway') {
          detail.insertAdjacentHTML('beforeend', `<p>This synthesized view is read-only. ${ownerLink('operations', 'Manage owning ModelGateway binding', { model_provider_id: provider.model_provider_ids?.[0] || provider.id })}. Its status is controlled by ModelGateway; no remote account state is changed here.</p>`);
        } else {
          detail.insertAdjacentHTML('beforeend', bindingForm(provider, schema, canManage && impact.can_manage && impact.available));
          const form = detail.querySelector('form'); const validation = formValidation(form);
          editor = trackDirtyEditor(form, { label: 'Provider binding' }); const owned = editor;
          const save = form.querySelector('[data-binding-save]'); let reviewed = null; let busy = false;
          form.addEventListener('input', () => { reviewed = null; save.disabled = true; });
          form.querySelector('[data-binding-discard]').onclick = () => { if (confirmDiscard(owned)) void load(true); };
          form.querySelector('[data-binding-preview]').onclick = async () => {
            if (busy || !validation.validate()) return;
            const payload = readBinding(form, schema); const submitted = JSON.stringify(payload);
            const invalidGrants = (payload.granted_capabilities || []).filter(value => !payload.declared_capabilities.includes(value));
            if (invalidGrants.length) { validation.show([{ field: 'granted_capabilities', message: 'Granted capabilities must also be declared.' }]); return; }
            busy = true; reviewed = null; save.disabled = true;
            try {
              const latest = await op.request(`${endpoint}${encodeURIComponent(id)}/impact`);
              if (!current() || JSON.stringify(readBinding(form, schema)) !== submitted) return;
              if (!latest.available || !latest.can_manage) { validation.show([{ message: 'Impact or administration authority is unavailable; no change can be applied.' }]); return; }
              if (latest.expected_revision !== impact.expected_revision) { validation.show([{ message: 'Binding changed; reload and review current state before editing.' }]); return; }
              const changed = Object.keys(payload).filter(key => JSON.stringify(payload[key]) !== JSON.stringify(provider[key]));
              form.querySelector('[data-binding-impact]').innerHTML = `<p>Change ${esc(changed.join(', ') || 'none')} for ${esc(id)} in ${esc(catalog.organization_id)}/${esc(catalog.workspace_id)}. Lifecycle: ${esc(provider.lifecycle || provider.status)} → ${esc(payload.lifecycle || payload.status)}. References are validated by the canonical API when saving.</p>${impactHTML(latest)}`;
              reviewed = { payload, submitted, revision: latest.expected_revision }; save.disabled = false;
            } catch (error) { if (current()) validation.server(error); }
            finally { busy = false; }
          };
          form.onsubmit = async event => {
            event.preventDefault(); if (busy || !reviewed || !validation.validate() || JSON.stringify(readBinding(form, schema)) !== reviewed.submitted) return;
            if (!confirm(`Apply binding changes to ${id} in ${catalog.organization_id}/${catalog.workspace_id}? Lifecycle ${provider.lifecycle || provider.status} → ${reviewed.payload.lifecycle || reviewed.payload.status}. Future routing changes; running sessions and remote accounts are unchanged.`)) return;
            busy = true; save.disabled = true; const submitted = owned.snapshot();
            try {
              await op.request(`${endpoint}${encodeURIComponent(id)}?expected_revision=${encodeURIComponent(reviewed.revision)}`, { method: 'PUT', body: reviewed.submitted });
              owned.markSaved(submitted);
              if (current() && !owned.dirty()) await load(true);
              else if (current()) status('Saved; newer unsaved edits remain. Reload current state before applying them.');
            } catch (error) { if (current()) validation.server(error); }
            finally { busy = false; reviewed = null; }
          };
        }
        detail.querySelector('h3').focus();
        status(impact.can_manage ? 'Binding loaded. Preview impact before applying changes.' : `Read-only binding. ${impact.denial_reason || 'Use its owning control surface or repair dependency inventory.'}`);
      } catch (error) { if (current()) status(`Provider binding unavailable: ${error.message}`); }
    }
    for (const [kind, records] of [['agent', catalog.items], ['model', models.items]]) for (const provider of records) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = `${provider.display_name} · ${kind} binding · ${provider.lifecycle || provider.status}`;
      button.dataset.bindingId = provider.id; button.dataset.bindingKind = kind; button.onclick = () => void open(provider.id, kind); list.append(button);
    }
    const query = new URLSearchParams(location.search);
    const requested = query.has('model_provider_id') ? { id: query.get('model_provider_id'), kind: 'model' } : query.has('provider_id') ? { id: query.get('provider_id'), kind: 'agent' } : selected;
    if (requested && (requested.kind === 'model' ? models.items : catalog.items).some(item => item.id === requested.id)) await open(requested.id, requested.kind);
    else status(catalog.items.length + models.items.length ? 'Select a provider binding to inspect ownership and consumers.' : 'No canonical provider bindings in this workspace.');
  } catch (error) { if (op.current()) status(`Provider binding inventory unavailable: ${error.message}`); }
}
bindWhenReady(() => {
  document.getElementById('refresh-provider-bindings')?.addEventListener('click', () => void load());
  window.addEventListener('codex:project-changed', () => { selected = null; void load(true); });
  window.addEventListener('codex:project-workspace-page', event => { if (event.detail?.workspace === 'operations' && !editor?.dirty()) void load(true); });
});
