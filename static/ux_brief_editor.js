import {request,apiBase} from './api_client.js';
const names=['target_users','observed_needs','journeys','design_constraints','approved_copy','uncertainties','evaluation_criteria'];
const label=name=>name.replaceAll('_',' ').replace(/^./,letter=>letter.toUpperCase());
const sourceKey=(kind,id)=>JSON.stringify([kind,id]);

export function installUxBriefEditor({host,getProjectId,onSaved=()=>{}}){
  const style=document.createElement('link');style.rel='stylesheet';style.href=apiBase()+'/static/ux_brief_editor.css';document.head.append(style);
  const root=document.createElement('details');root.id='ux-brief-editor';
  root.innerHTML=`<summary>Create or revise a UX context brief</summary>
    <p>Curate relevant user needs, journeys, constraints and examples. Reference supporting Knowledge, Artifact or Evidence records. Source origin and review metadata do not establish verified user research. Saving requires Memory write access and the existing identity step-up.</p>
    <div class="ux-brief-scope" role="status"></div>
    <label>Existing brief <select id="ux-brief-existing"><option value="">New brief</option></select></label>
    <button type="button" id="ux-brief-refresh">Load Project briefs</button>
    <div class="ux-brief-fields"><label>Logical key <input id="ux-brief-key" maxlength="500" placeholder="ux.recovery" /></label>
    <label>Title <input id="ux-brief-title" maxlength="1000" /></label>
    <label>Summary <textarea id="ux-brief-summary" maxlength="8000" rows="2"></textarea></label>
    <label>Classification <select id="ux-brief-classification"><option>internal</option><option>confidential</option><option>secret</option><option>public</option></select></label>
    <label>Review after <input id="ux-brief-review" type="datetime-local" /></label>
    <label>Valid until <input id="ux-brief-valid" type="datetime-local" /></label>
    ${names.map(name=>`<div class="ux-brief-section"><label>${label(name)} <textarea data-brief-text="${name}" maxlength="2000" rows="2"></textarea></label>
      <label>Supporting sources for ${label(name).toLowerCase()} <textarea data-brief-sources="${name}" rows="2" placeholder="One per line: knowledge:ID, artifact:ID or evidence:ID"></textarea></label></div>`).join('')}
    <label>Revision reason <input id="ux-brief-reason" maxlength="4000" /></label></div>
    <button type="button" id="ux-brief-pin">Inspect and pin current source revisions</button>
    <pre id="ux-brief-pins"></pre><button type="button" id="ux-brief-save">Save brief version</button>
    <div id="ux-brief-editor-status" role="status" aria-live="polite"></div>`;
  host.querySelector('.memory-header').after(root);
  const node=id=>root.querySelector('#ux-brief-'+id);
  const text=name=>root.querySelector(`[data-brief-text="${name}"]`);
  const sources=name=>root.querySelector(`[data-brief-sources="${name}"]`);
  const drafts=new Map();let projectId=getProjectId(),generation=0,records=[],pins=new Map(),selected=null;
  const status=value=>node('editor-status').textContent=value;
  function snapshot(){return {values:Object.fromEntries(['key','title','summary','classification','review','valid','reason'].map(id=>[id,node(id).value])),
    sections:Object.fromEntries(names.map(name=>[name,{text:text(name).value,sources:sources(name).value}])),pins:new Map(pins),selected};}
  function retain(){drafts.set(projectId,snapshot());}
  function render(draft){selected=draft?.selected||null;pins=new Map(draft?.pins||[]);
    for(const id of ['key','title','summary','classification','review','valid','reason'])node(id).value=draft?.values[id]||(id==='classification'?'internal':'');
    for(const name of names){text(name).value=draft?.sections[name]?.text||'';sources(name).value=draft?.sections[name]?.sources||'';}
    node('key').disabled=Boolean(selected);node('existing').value=selected?.id||'';showPins();
    root.querySelector('.ux-brief-scope').textContent=`Project: ${projectId||'Select a Project'}${selected?` · Memory ${selected.id} v${selected.version}`:''}`;}
  function sync(){const next=getProjectId();if(next===projectId)return;
    retain();projectId=next;generation++;records=[];node('existing').replaceChildren(new Option('New brief',''));render(drafts.get(next));
    status('Project changed. Draft retained in memory for its original Project.');}
  const current=(scope,turn)=>scope===getProjectId()&&turn===generation;
  async function load(){sync();const scope=projectId,turn=++generation;if(!scope){status('Select a Project first.');return;}
    status('Loading governed briefs…');try{const response=await request(`/api/memory/projects/${encodeURIComponent(scope)}/ux-briefs`);
      if(!current(scope,turn))return;records=response.items||[];node('existing').replaceChildren(new Option('New brief',''));
      for(const record of records){const option=new Option(`${record.title} · v${record.version} · ${record.freshness}`,record.id);option.disabled=!record.content;node('existing').append(option);}
      node('existing').value=selected?.id||'';status('Choose a brief or create one. Sources keep exact revisions until explicitly repinned.');
    }catch(error){if(current(scope,turn))status('Briefs unavailable: '+error.message+'. Draft retained.');}}
  function showPins(){node('pins').replaceChildren();for(const pin of pins.values()){
    const link=document.createElement('a');link.href=apiBase()+(pin.kind==='knowledge'?`/api/memory/${encodeURIComponent(pin.record_id)}`:`/api/${pin.kind==='artifact'?'artifacts':'evidence'}/${encodeURIComponent(pin.record_id)}`);
    link.target='_blank';link.rel='noopener';link.textContent=`${pin.kind}:${pin.record_id} @${pin.revision} · sha256:${pin.sha256}`;node('pins').append(link,document.createTextNode('\n'));}}

  function parsedSources(){const result=new Map();for(const name of names){const parsed=sources(name).value.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{
      const separator=line.indexOf(':');const kind=line.slice(0,separator),id=line.slice(separator+1).trim();
      if(!['knowledge','artifact','evidence'].includes(kind)||!id)throw Error('Use kind:ID for each supporting source.');return {kind,id};});result.set(name,parsed);}
    if(new Set([...result.values()].flat().map(source=>sourceKey(source.kind,source.id))).size>16)throw Error('Use at most 16 distinct sources.');return result;}
  root.addEventListener('input',()=>{generation++;retain();});
  node('refresh').addEventListener('click',load);
  root.addEventListener('toggle',()=>{if(root.open){sync();load();}});
  node('existing').addEventListener('change',()=>{generation++;const record=records.find(item=>item.id===node('existing').value);
    if(!record){render();retain();return;}
    try{const brief=JSON.parse(record.content);pins=new Map();for(const section of Object.values(brief.sections))for(const pin of section.sources||[])pins.set(sourceKey(pin.kind,pin.record_id),pin);
      const date=value=>{if(!value)return '';const date=new Date(value*1000);return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);};
      render({selected:record,pins,values:{key:record.logical_key,title:record.title,summary:record.summary,classification:record.classification,
        review:date(record.review_after),valid:date(record.valid_until),reason:''},sections:Object.fromEntries(names.map(name=>[name,
          {text:brief.sections[name]?.text||'',sources:(brief.sections[name]?.sources||[]).map(pin=>`${pin.kind}:${pin.record_id}`).join('\n')}]))});retain();
      status('Exact sources loaded. Review findings and explicitly repin only when updating their attribution.');
    }catch(error){status('Brief format unavailable: '+error.message);}});
  node('pin').addEventListener('click',async()=>{sync();retain();const scope=projectId,turn=++generation;status('Inspecting canonical source revisions…');
    try{const unique=new Map([...parsedSources().values()].flat().map(source=>[sourceKey(source.kind,source.id),source]));const next=new Map();
      for(const [key,source] of unique){const response=await request(`/api/memory/projects/${encodeURIComponent(scope)}/ux-briefs/sources/${source.kind}/${encodeURIComponent(source.id)}`);
        if(!current(scope,turn))return;next.set(key,response.source);}
      pins=next;showPins();retain();status('Current revisions pinned. Review them before saving.');
    }catch(error){if(current(scope,turn))status('Source inspection failed: '+error.message+'. Draft retained.');}});
  node('save').addEventListener('click',async()=>{sync();retain();const scope=projectId,turn=++generation;const original=selected;status('Validating brief version…');
    try{if(!scope)throw Error('Select a Project first.');const references=parsedSources();const sections={};
      for(const name of names){if(!text(name).value.trim()&&!references.get(name).length)continue;
        sections[name]={text:text(name).value,sources:references.get(name).map(source=>{const pin=pins.get(sourceKey(source.kind,source.id));if(!pin)throw Error('Inspect and pin sources before saving.');return pin;})};}
      const timestamp=id=>node(id).value?new Date(node(id).value).getTime()/1000:null;
      const payload={title:node('title').value,summary:node('summary').value,content:JSON.stringify({schema_version:'ux-context-brief/1.0',sections}),
        classification:node('classification').value,review_after:timestamp('review'),valid_until:timestamp('valid'),
        provenance:{source_kind:'manual',source_ref:`ux-brief:${node('key').value}`}};
      if(original)payload.reason=node('reason').value;else Object.assign(payload,{logical_key:node('key').value,object_type:'ux_context_brief',project_id:scope});
      const response=await request(`/api/memory/projects/${encodeURIComponent(scope)}/ux-briefs${original?`/${encodeURIComponent(original.id)}/revise`:''}`,
        {method:'POST',body:JSON.stringify(payload)});
      if(!current(scope,turn)){
        const retained=drafts.get(scope);if(retained)retained.lastSaved={id:response.item.id,version:response.item.version};
        if(scope===getProjectId())status(`Saved prior draft as ${response.item.id} v${response.item.version}. Newer edits retained; load the saved version before revising.`);
        return;
      }
      selected=response.item;node('key').disabled=true;retain();await onSaved();
      if(current(scope,turn))status(`Saved Memory ${selected.id} v${selected.version}. Select this exact brief for a relevant task.`);
    }catch(error){if(current(scope,turn))status('Save failed: '+error.message+'. Draft retained; inspect Memory before retrying an uncertain outcome.');}});
  window.addEventListener('codex:project-changed',sync);render();return {sync};
}
