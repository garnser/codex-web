import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {loadProjectUiStateForRefresh,applyProjectModels} from '../../static/project_ui_state.js';

// Exercise the actual production refresh, not a second initialization routine.
// Only network/render boundaries are substituted; no application/server import.
const app=fs.readFileSync(new URL('../../static/app.js',import.meta.url),'utf8');
const source=app.slice(app.indexOf('async function refresh({'),app.indexOf('function scheduleRefresh(',app.indexOf('async function refresh({')));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject};}
function harness({cached=false,pendingWorkspace=false,failedWorkspace=false}={}){
 const model=deferred(),workspaceReady=deferred(),calls=[],errors=[];
 const selected={value:'codex/existing',innerHTML:'unchanged'},search={value:''};
 const observation={catalog:0,restores:0};
 const workspace={project:{id:'home'},executionProfiles:{items:[{id:'orchestration-only'}]},threads:{data:[]},resources:{items:[]},bindings:{items:[]},channels:{items:[]}};
 const api=async(path,options)=>{calls.push({path,signal:options?.signal});if(path==='/api/models'){if(failedWorkspace)options.signal.addEventListener('abort',()=>model.reject(new DOMException('cancelled','AbortError')),{once:true});return model.promise;}if(path.includes('/ui-state?')){if(failedWorkspace)throw Error('workspace unavailable');return pendingWorkspace?workspaceReady.promise:workspace;}throw Error('Unexpected API '+path);};
 const state={projectId:'home',refreshGeneration:0,projectUiStatic:{},projects:[{id:'home'}],models:cached?[{id:'cached'}]:[],threadId:null,usageProjectId:'home',queuedDepthByThread:new Map()};
 const deps={state,$:id=>id==='thread-search'?search:id==='thread-model-setting'?selected:null,api,loadProjectUiStateForRefresh,applyProjectModels,renderProjects:()=>{},renderThreads:()=>{},clearMessages:()=>{},logEvent:(type)=>errors.push(type),refreshBindingQueueDepth:()=>{},ep:{setCatalog:()=>{observation.catalog++}},renderRepositoryTargets:()=>{},applyRunSettings:()=>{},renderGitLabIntegration:()=>{},renderAgentChannelPresence:()=>{},hydrateThreadListActivity:()=>{},threadRoute:{idFromLocation:()=> 'selected-existing',restore:async()=>{observation.restores++}},refreshTokenUsage:()=>{},markMilestone:()=>{},renderModelOptions:value=>JSON.stringify({value,models:state.models})};
 const refresh=new Function(...Object.keys(deps),source+';return refresh;')(...Object.values(deps));
 const task=refresh();
 return {model,workspaceReady,workspace,calls,errors,selected,search,observation,state,task,
  async join(){model.resolve({data:[{id:'discovered'}]});workspaceReady.resolve(workspace);await task;}};
}
test('ready workspace hydrates actual execution catalog and selected thread before pending models',async()=>{
 const h=harness();try{await tick();assert.deepEqual(h.observation,{catalog:1,restores:1});assert.equal(h.state.refreshInFlight!==null,true);assert.equal(h.calls.filter(x=>x.path==='/api/models').length,1);h.selected.value='user-choice';}finally{await h.join();}
 assert.equal(h.state.models[0].id,'discovered');assert.equal(JSON.parse(h.selected.innerHTML).value,'user-choice');assert.equal(h.state.refreshInFlight,null);
});
test('cached models do not issue another discovery',async()=>{
 const h=harness({cached:true});try{await h.task;assert.deepEqual(h.observation,{catalog:1,restores:1});assert.equal(h.calls.some(x=>x.path==='/api/models'),false);assert.equal(h.state.models[0].id,'cached');}finally{await h.join();}
});
test('model failure retains hydrated canonical context and reports actual metadata error',async()=>{
 const h=harness();try{await tick();h.model.reject(new Error('model unavailable'));await h.task;assert.deepEqual(h.observation,{catalog:1,restores:1});assert.deepEqual(h.state.models,[]);assert.equal(h.errors.filter(x=>x==='models.error').length,1);}finally{await h.join();}
});
for(const change of ['generation','project','search','abort'])test('late models cannot overwrite changed '+change+' context',async()=>{
 const h=harness();try{await tick();assert.equal(h.observation.restores,1);h.state.models=[{id:'newer'}];
  if(change==='generation')h.state.refreshGeneration++;
  if(change==='project')h.state.projectId='different';
  if(change==='search')h.search.value='different';
  if(change==='abort')h.state.refreshController.abort();
 }finally{await h.join();}
 assert.equal(h.state.models[0].id,'newer');assert.equal(h.selected.innerHTML,'unchanged');
});
test('early models cannot publish a workspace before canonical workspace response',async()=>{
 const h=harness({pendingWorkspace:true});try{h.model.resolve({data:[{id:'early'}]});await tick();assert.deepEqual(h.observation,{catalog:0,restores:0});h.workspaceReady.resolve(h.workspace);await h.task;assert.deepEqual(h.observation,{catalog:1,restores:1});assert.equal(h.state.models[0].id,'early');}finally{await h.join();}
});

test('failed workspace releases its optional model request through the same abort owner',async()=>{
 const h=harness({failedWorkspace:true});await assert.rejects(h.task,/workspace unavailable/);await tick();
 assert.deepEqual(h.observation,{catalog:0,restores:0});
 assert.equal(h.calls.find(x=>x.path==='/api/models').signal.aborted,true);
 assert.equal(h.state.refreshController,null);assert.equal(h.state.refreshInFlight,null);
});
