import {revealSetting} from './thread_settings_ui.js';
import {installCareComposer} from "./care_composer.js";
import {apiBase} from "./api_client.js";
const scopeKey=scope=>JSON.stringify([scope.projectId,scope.threadId,scope.repositoryResourceId,
  scope.writableRepositoryResourceIds||[],scope.readOnlyRepositoryResourceIds||[]]);
const sourcePath=source=>source.kind==='knowledge'?`/api/memory/${encodeURIComponent(source.record_id)}`:
  `/api/${source.kind==='artifact'?'artifacts':'evidence'}/${encodeURIComponent(source.record_id)}`;

export function installUxBriefPicker({prompt,getScope,request}){
  const host=prompt?.ownerDocument.getElementById('thread-context-controls');
  if(!host)return {sync(){},prepare:()=>null,valid:()=>true,accepted(){},rejected(){}};
  const root=document.createElement('details');root.id='ux-brief-picker';root.className='thread-profile-details';
  root.innerHTML='<summary>UX knowledge (optional)</summary><p>Select relevant observations and constraints from this Project. Preview checks current sources and permissions; the turn checks them again when it starts.</p><label>Brief <select id="ux-picker-brief"><option value="">No brief</option></select></label><fieldset id="ux-picker-sections"><legend>Sections to include</legend></fieldset><button id="ux-picker-preview" type="button">Preview selected context</button><div id="ux-picker-status" role="status" aria-live="polite"></div><pre id="ux-picker-context"></pre><div id="ux-picker-sources"></div>';
  host.append(root);
  const style=document.createElement('link');style.rel='stylesheet';style.href=apiBase()+'/static/ux_brief_editor.css';document.head.append(style);
  const node=id=>root.querySelector('#ux-picker-'+id),drafts=new Map();
  let key=scopeKey(getScope()),draft={id:'',sections:[],selection:null},items=[],generation=0;
  const status=text=>{node('status').textContent=text;};
  function save(){drafts.set(key,draft);}
  function clearPreview(){draft.selection=null;node('context').textContent='';node('sources').replaceChildren();save();}
  function sections(){node('sections').replaceChildren(Object.assign(document.createElement('legend'),{textContent:'Sections to include'}));
    const item=items.find(item=>item.id===draft.id);let body;
    try{body=JSON.parse(item?.content||'{}');}catch{body={};}
    for(const [name,section] of Object.entries(body.sections||{})){if(!section.text)continue;
      const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=name;input.checked=draft.sections.includes(name);
      input.addEventListener('change',()=>{generation++;draft.sections=[...node('sections').querySelectorAll('input:checked')].map(input=>input.value);clearPreview();status('Selection changed. Preview before sending.');});
      label.append(input,document.createTextNode(' '+name.replaceAll('_',' ')));node('sections').append(label);}}
  async function load(){const scope=getScope(),captured=key,turn=++generation;
    if(!scope.projectId)return;
    status('Loading Project briefs…');
    try{const result=await request(`/api/memory/projects/${encodeURIComponent(scope.projectId)}/ux-briefs`);
      if(captured!==scopeKey(getScope())||turn!==generation)return;
      items=result.items||[];node('brief').replaceChildren(new Option('No brief',''));
      for(const item of items){const option=new Option(`${item.title} · v${item.version} · ${item.freshness}`,item.id);option.disabled=item.freshness!=='fresh'||!item.content;node('brief').append(option);}
      node('brief').value=draft.id;sections();status('Choose only the sections useful for this task.');
    }catch{if(captured===scopeKey(getScope())&&turn===generation)status('Briefs unavailable. Selection retained.');}}
  function sync(){const next=scopeKey(getScope());if(next===key)return;save();generation++;key=next;draft=drafts.get(key)||{id:'',sections:[],selection:null};items=[];
    node('brief').replaceChildren(new Option('No brief',''));node('context').textContent='';node('sources').replaceChildren();sections();
    status('Scope changed. Previous selection retained for its original scope.');if(root.open)load();}
  root.addEventListener('toggle',()=>{sync();if(root.open)load();});
  node('brief').addEventListener('change',()=>{generation++;draft={id:node('brief').value,sections:[],selection:null};clearPreview();sections();status('Select sections, then preview.');});
  node('preview').addEventListener('click',async()=>{sync();const item=items.find(item=>item.id===draft.id);if(!item||!draft.sections.length){status('Select a brief and at least one section.');return;}
    const captured=key,turn=++generation;draft.selection=null;save();status('Checking context…');
    const selection={knowledge_id:item.id,version:item.version,sha256:item.content_sha256,sections:[...draft.sections],max_context_tokens:2000};
    try{const result=await request(`/api/memory/projects/${encodeURIComponent(getScope().projectId)}/ux-briefs/preview`,{method:'POST',body:JSON.stringify(selection)});
      if(captured!==scopeKey(getScope())||turn!==generation)return;
      node('context').textContent=result.context||'';node('sources').replaceChildren();
      for(const source of result.selection?.sources||[]){const link=document.createElement('a');link.href=apiBase()+sourcePath(source);link.target='_blank';link.rel='noopener';link.textContent=`${source.kind}:${source.record_id}@${source.revision} · sha256:${source.sha256}`;node('sources').append(link,document.createElement('br'));}
      if(result.omitted?.length||!result.context){status('Context unavailable: '+(result.omitted||[]).map(row=>`${row.section}: ${row.reason}`).join('; ')+'. Adjust the selection and preview again.');return;}
      draft.selection=result.selection;save();status(`Preview ready · ${result.packed_tokens} context tokens · delivery not yet proven. Sources and permissions are checked again at turn start.`);
    }catch{if(captured===scopeKey(getScope())&&turn===generation)status('Preview failed. Selection retained.');}});
  function prepare(){sync();if(!draft.id)return null;if(!draft.selection){status('Preview selected UX knowledge in Thread settings → UX knowledge before sending.');revealSetting(root,node('preview'));return {blocked:true};}
    return {scope:getScope(),key,selection:draft.selection};}
  function valid(ticket,createdThreadId){if(!ticket)return true;sync();const now=getScope();
    if(scopeKey(now)===ticket.key)return true;
    if(createdThreadId&&!ticket.scope.threadId&&now.threadId===createdThreadId&&scopeKey({...now,threadId:null})===scopeKey({...ticket.scope,threadId:null})){
      ticket.key=key;draft={id:ticket.selection.knowledge_id,sections:[...ticket.selection.sections],selection:ticket.selection};save();return true;}
    status('Scope changed before sending. Inspect the selection in Thread settings → UX knowledge again.');revealSetting(root,node('preview'));return false;}
  function accepted(ticket){if(!ticket||scopeKey(getScope())!==ticket.key||draft.selection!==ticket.selection)return;
    draft={id:'',sections:[],selection:null};save();node('brief').value='';clearPreview();sections();status('Selection delivered to turn admission. Inspect Memory retrieval history for native delivery evidence.');}
  function rejected(ticket){if(ticket&&scopeKey(getScope())===ticket.key)status('Submission failed. Selection retained; inspect turn status before retrying an uncertain outcome.');}
  window.addEventListener('codex:project-changed',sync);
  return {sync,prepare,valid,accepted,rejected};
}

export function installContextPickers(options){
  return {care:installCareComposer(options),ux:installUxBriefPicker(options)};
}
