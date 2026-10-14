import {revealSetting,returnToPrompt} from './thread_settings_ui.js';
const fields = ['context','action','rules','examples','references'];
const key = scope => JSON.stringify([scope.projectId,scope.threadId,scope.repositoryResourceId,
  scope.writableRepositoryResourceIds||[],scope.readOnlyRepositoryResourceIds||[]]);

export function installCareComposer({prompt,getScope,request,onResize=()=>{}}) {
  const host=prompt?.ownerDocument.getElementById('thread-context-controls');
  if(!host)return {prepare:()=>null,valid:()=>true,sync:()=>{},accepted:()=>{},rejected:()=>{}};
  const root=document.createElement('details');root.id='care-composer';root.className='thread-profile-details';
  root.innerHTML=`<summary>Structured task (optional)</summary>
    <p>Use context, action, rules and examples for complex work. Ordinary chat is sufficient for a short request. Rules are task guidance; existing permissions and approvals still apply.</p>
    <div class="care-scope" role="status"></div>
    <label>Starting example <select id="care-example"><option value="">Custom request</option></select></label>
    <div class="care-fields">
      <label>Context <textarea id="care-context" maxlength="4000" rows="2" placeholder="Relevant situation and user needs"></textarea></label>
      <label>Action / desired output <textarea id="care-action" maxlength="4000" rows="2" placeholder="What should be done; preferred output format"></textarea></label>
      <label>Rules / constraints <textarea id="care-rules" maxlength="4000" rows="2" placeholder="Constraints and task guidance"></textarea></label>
      <label>Examples <textarea id="care-examples" maxlength="4000" rows="2" placeholder="A useful example of the desired result"></textarea></label>
      <label>References <textarea id="care-references" maxlength="4104" rows="2" placeholder="One source reference per line, up to eight"></textarea></label>
    </div><button id="care-preview" type="button">Compose editable preview</button>
    <div id="care-status" role="status" aria-live="polite"></div>
    <output id="care-provenance"></output>
    <p>Compose a preview, then review and edit it in the main message box before sending. The displayed starting-example reference is included with the request.</p>`;
  host.append(root);
  const node=id=>root.querySelector('#care-'+id);
  const drafts=new Map();let scope=getScope(),scopeKey=key(scope),generation=0,catalog=[],busy=false,active=false;
  const empty=()=>({values:{},preview:'',ready:false,reference:null,footer:''});
  let draft=empty();draft.preview=prompt.value;
  const status=text=>{node('status').textContent=text;};
  function save(){draft.values=Object.fromEntries(fields.map(name=>[name,node(name).value]));draft.preview=prompt.value;drafts.set(scopeKey,draft);}
  function render(){for(const name of fields)node(name).value=draft.values[name]||'';
    prompt.value=draft.preview;node('provenance').textContent=draft.footer;
    root.querySelector('.care-scope').textContent=`Project: ${scope.projectName||scope.projectId||'Select a Project'} · Repository: ${scope.repositoryLabel||scope.repositoryResourceId||'Selected execution target'} · Thread: ${scope.threadId||'New Thread'}`;
    onResize();}
  function sync(){const next=getScope(),nextKey=key(next);if(nextKey===scopeKey)return;
    if(root.open){if(active)save();generation++;catalog=[];node('example').innerHTML='<option value="">Custom request</option>';
      scope=next;scopeKey=nextKey;draft=drafts.get(scopeKey)||empty();render();
      status('Scope changed. Previous draft is retained in memory for its original scope.');loadExamples();
    }}
  async function loadExamples(){if(!root.open||!scope.projectId)return;
    const captured=scopeKey,turn=++generation;status('Loading published examples…');
    try{const result=await request(`/api/projects/${encodeURIComponent(scope.projectId)}/care/examples`);
      if(captured!==key(getScope())||turn!==generation)return;
      catalog=result.items||[];node('example').replaceChildren(new Option('Custom request',''));
      catalog.forEach((item,index)=>{const option=new Option(item.name,String(index));option.disabled=!item.allowed;node('example').append(option);});
      const selected=catalog.findIndex(item=>item.reference.record_id===draft.reference?.record_id);
      node('example').value=selected<0?'':String(selected);status('Choose an example or write any useful fields. All fields are optional.');
    }catch(error){if(captured===key(getScope())&&turn===generation)status('Examples unavailable: '+error.message+'. Your draft is retained.');}}
  root.addEventListener('toggle',()=>{if(root.open){sync();if(!drafts.has(scopeKey)){draft.preview=prompt.value;render();}active=true;loadExamples();}else{save();active=false;generation++;}});
  root.addEventListener('input',event=>{if(fields.some(name=>event.target===node(name))){generation++;draft.ready=false;save();status('Fields changed. Compose a new preview before sending.');}});
  node('example').addEventListener('change',()=>{const selected=catalog[Number(node('example').value)];
    if(node('example').value===''||!selected){generation++;draft.reference=null;draft.footer='';draft.ready=false;node('provenance').textContent='';save();return;}
    generation++;draft.reference=selected.reference;for(const name of fields)node(name).value=name==='references'?(selected.fields[name]||[]).join('\n'):(selected.fields[name]||'');
    draft.ready=false;save();status('Example loaded. Edit the fields, then compose a preview.');});
  node('preview').addEventListener('click',async()=>{sync();if(busy)return;save();
    const captured=scopeKey,turn=++generation;busy=true;node('preview').disabled=true;status('Composing preview…');
    try{const values={...draft.values,references:draft.values.references.split('\n').map(ref=>ref.trim()).filter(Boolean)};
      const result=await request(`/api/projects/${encodeURIComponent(scope.projectId)}/care/preview`,{method:'POST',body:JSON.stringify({fields:values,template_ref:draft.reference})});
      if(captured!==key(getScope())||turn!==generation)return;
      draft.footer=result.template_ref?`Starting example: ${result.template_ref.definition_id} r${result.template_ref.revision} · record ${result.template_ref.record_id} · checksum ${result.template_ref.checksum}`:'';
      draft.preview=result.message;draft.ready=true;prompt.value=draft.preview;node('provenance').textContent=draft.footer;
      save();onResize();status((result.suggestions||[]).join(' ')||'Preview ready. Review and edit the main message before sending.');returnToPrompt(root,prompt);
    }catch(error){if(captured===key(getScope())&&turn===generation)status('Preview failed: '+error.message+'. Your draft is retained.');}
    finally{busy=false;node('preview').disabled=false;}});
  prompt.addEventListener('input',()=>{if(root.open){generation++;draft.preview=prompt.value;save();}});
  window.addEventListener('codex:project-changed',sync);
  function prepare(){if(!root.open)return null;sync();save();
    if(!draft.ready){status('Compose a preview in Thread settings → Structured task before sending.');revealSetting(root,node('preview'));return {blocked:true};}
    const message=[prompt.value.trim(),draft.footer].filter(Boolean).join('\n\n');
    if(!prompt.value.trim()||message.length>16000){status('Provide a request of at most 16,000 characters including its reference.');revealSetting(root,node('preview'));return {blocked:true};}
    return {message,scope:{...getScope()},scopeKey,draft:structuredClone(draft),preview:prompt.value};}
  function valid(ticket,createdThreadId){if(!ticket)return true;const current=getScope();
    const expected={...ticket.scope,threadId:createdThreadId||ticket.scope.threadId};
    if(key(current)!==key(expected)){status('Scope changed before submission. Draft retained; select its original scope to continue.');revealSetting(root,node('preview'));return false;}
    if(createdThreadId){drafts.delete(ticket.scopeKey);ticket.scope=expected;ticket.scopeKey=key(expected);
      drafts.set(ticket.scopeKey,ticket.draft);scope=current;scopeKey=ticket.scopeKey;draft=ticket.draft;render();}
    return true;}
  function rejected(ticket){if(!ticket)return;ticket.draft.preview=ticket.preview;const retained=drafts.get(ticket.scopeKey);
    if(retained&&JSON.stringify(retained.values)!==JSON.stringify(ticket.draft.values))return;
    if(retained?.preview&&retained.preview!==ticket.preview)return;
    drafts.set(ticket.scopeKey,ticket.draft);
    if(valid(ticket)&&!prompt.value.trim()){draft=ticket.draft;prompt.value=ticket.preview;
      status('Submission failed. Draft retained; inspect Thread/queue status before retrying an uncertain outcome.');onResize();}}
  function accepted(ticket){if(!ticket)return;
    if(key(getScope())!==ticket.scopeKey||prompt.value.trim())return;
    const values=Object.fromEntries(fields.map(name=>[name,node(name).value]));
    if(JSON.stringify(values)!==JSON.stringify(ticket.draft.values))return;
    drafts.delete(ticket.scopeKey);draft=empty();drafts.set(ticket.scopeKey,draft);
    for(const name of fields)node(name).value='';node('example').value='';draft.ready=false;node('provenance').textContent='';
    // Admission completed this optional mode. Keep a hidden control from
    // requiring another preview for the next ordinary message.
    generation++;active=false;root.open=false;
    status('Request accepted. Add refinements in this Thread or use ordinary chat.');}
  return {prepare,valid,sync,accepted,rejected};
}

export function careScope(state,settings,repositories){
  return {projectId:state.projectId,threadId:state.threadId,
    projectName:state.projects.find(project=>project.id===state.projectId)?.name,
    repositoryResourceId:settings.repositoryResourceId||null,
    writableRepositoryResourceIds:repositories.writable_repository_resource_ids||[],
    readOnlyRepositoryResourceIds:settings.readOnlyRepositoryResourceIds||[]};
}
