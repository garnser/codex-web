#!/usr/bin/python3
"""Observe canonical current bindings and progress; never start/replay work."""
from concurrent.futures import ThreadPoolExecutor
import argparse,fcntl,hashlib,json,math,os,re,shutil,subprocess,time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError


def atomic(path, data):
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w') as stream:
        json.dump(data,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())
    temp.replace(path)


def api(path, payload=None, *, timeout=12):
    request=Request('http://127.0.0.1:8765'+path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=timeout) as response:
        body=response.read(8*1024*1024+1)
        if len(body)>8*1024*1024:raise ValueError('inspection_response_too_large')
        return json.loads(body)


def bindings_for_project(rows, project):
    if not isinstance(rows,list):raise ValueError('invalid_binding_inventory')
    found={}
    for row in rows:
        if row.get('project_id')!=project:continue
        tid=row.get('thread_id')
        if not isinstance(tid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',tid):
            raise ValueError('invalid_bound_thread_id')
        found.setdefault(tid,set()).add(str(row.get('route_prefix') or row.get('thread_name') or tid)[:120])
    if not found:raise ValueError('no_current_project_bindings')
    if len(found)>32:raise ValueError('bound_thread_inspection_limit_exceeded')
    return {tid:sorted(names) for tid,names in found.items()}


def public_chunks(value):
    if isinstance(value,str):return [value]
    if isinstance(value,list):return [part for item in value for part in public_chunks(item)]
    if isinstance(value,dict) and value.get('type')!='reasoning':
        return [part for key in ('text','output','aggregatedOutput','content','result')
                if key in value for part in public_chunks(value[key])]
    return []


def progress(document):
    thread=document.get('thread',document);turns=thread.get('turns') or []
    if not turns:return None
    turn=turns[-1];items=[]
    for item in turn.get('items',[]):
        if item.get('type')=='reasoning':continue
        # Hash only public output fields, retaining no command/message bodies.
        chunks=public_chunks(item)
        raw='\n'.join(chunks).encode('utf-8',errors='replace')
        items.append({'id':item.get('id'),'type':item.get('type'),'status':item.get('status'),
                      'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    error=turn.get('error') or {};code=error.get('codexErrorInfo') if isinstance(error,dict) else None
    if isinstance(code,dict):code=code.get('type')
    if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,80}',code):code=None
    return {'turn_id':turn.get('id'),'status':turn.get('status'),'error_code':code,'items':items[-12:]}


def runner_failure(runner, activity):
    if runner.get('state') in {'lost','failed','error','expired'}:return True
    requires_live=runner.get('assignmentStatus') in {'running','claimed'} or activity=='active'
    return requires_live and (runner.get('leaseValid') is False or runner.get('processRunning') is False)


def live_workers_idle(samples):
    # Unstarted/cancelled roles do not determine whether admitted workers stalled.
    live=[sample for sample in samples.values()
          if sample.get('runner',{}).get('assignmentStatus') in {'claimed','running'}
          and sample.get('runner',{}).get('live') is True]
    return bool(live) and all(not sample.get('read_error_type')
        and sample.get('activity')=='idle'
        and sample.get('runner',{}).get('pendingExecutorWork') in (None,0)
        for sample in live)


def current_stall(sample, age):
    turn=(sample.get('progress') or {}).get('turn_id')
    return bool(sample.get('activity')=='active' and turn and
                turn==sample.get('activity_turn_id') and age>600)


def _sample_thread_current(tid, project):
    try:
        query=urlencode({'project_id':project})
        runner=api('/api/threads/'+tid+'/runner?'+query)
        queue=api('/api/threads/'+tid+'/queue?'+query)
        # Keep successful canonical absence distinct from an unavailable or
        # malformed wrapper before the legacy flattened projection loses it.
        projection=runner.get('runner') if isinstance(runner,dict) else None
        states=('unknown','cancelled','idle','failed','lost','waiting','running','recovering','stopped')
        projection_status=('absent' if isinstance(runner,dict) and 'runner' in runner and projection is None
            else 'present' if isinstance(projection,dict) and projection.get('state') in states else 'invalid')
        raw=projection if isinstance(projection,dict) else {}
        activity=runner.get('activity') or {} if isinstance(runner,dict) else {}
        raw=dict(raw)
        reason_code=raw.get('reason')
        if not isinstance(reason_code,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}',reason_code):raw['reason']=None
        reason=dict(queue.get('automationWaitReason') or {})
        code=reason.get('code')
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}',code):reason['code']=None
        queued=queue.get('queued') or []
        created=[x['createdAt'] for x in queued if isinstance(x.get('createdAt'),(int,float))]
        return tid,{'runner_projection_status':projection_status,
                    'runner':{k:raw.get(k) for k in ('state','assignmentId','assignmentStatus','fence',
                    'leaseValid','leaseExpiresAt','updatedAt','processRunning','live','reason','pendingExecutorWork')},
                    'activity':activity.get('state'),'activity_turn_id':activity.get('turnId'),'progress':None,
                    'progress_observation':'unknown_canonical_metadata_only',
                    'queue_depth':queue.get('queueDepth',queue.get('count',queue.get('depth'))),
                    'automation_wait_code':reason.get('code'),
                    'oldest_queued_age_seconds':max(0,time.time()-min(created)) if created else None}
    except HTTPError as exc:
        # Follow only the server's scoped canonical replacement response.
        # Never infer replacement from names, history or a failed runner read.
        if exc.code == 409:
            try:
                body=exc.read(8193)
                exc.close()
                detail=json.loads(body)['detail'] if len(body)<=8192 else {}
                target=detail.get('newThreadId')
                if (detail.get('code')=='thread_replaced' and detail.get('oldThreadId')==tid
                        and isinstance(target,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',target)
                        and target!=tid):
                    return tid,{'canonical_replacement':target}
            except (ValueError,KeyError,TypeError,AttributeError):
                pass
        exc.close()
        return tid,{'read_error_type':'HTTPError'}
    except Exception as exc:return tid,{'read_error_type':type(exc).__name__}


def sample_thread(tid, project):
    original=tid;seen=set();chain=[]
    for _ in range(8):
        if tid in seen:
            return original,{'read_error_type':'CanonicalReplacementCycle'}
        seen.add(tid)
        _,sample=_sample_thread_current(tid,project)
        target=sample.get('canonical_replacement')
        if target:
            chain.append({'from':tid,'to':target});tid=target;continue
        sample['observed_thread_id']=tid
        if chain:sample['canonical_replacement_chain']=chain
        return original,sample
    return original,{'read_error_type':'CanonicalReplacementDepthExceeded'}


USAGE_ROW_CAP = 2048
USAGE_COUNTS = ('input_tokens','output_tokens','total_tokens','reasoning_output_tokens',
    'tool_call_count','shell_command_count','file_edit_count','git_operation_count',
    'subagent_count','model_call_count','context_compaction_count')
USAGE_REFS = ('id','assignment_id','execution_id','agent_session_id',
    'provider_native_session_id','provider_native_turn_id')


def closed_project_usage(value, project):
    rows=value.get('items') if isinstance(value,dict) else None
    if not isinstance(rows,list) or len(rows)>USAGE_ROW_CAP:
        raise ValueError('bounded_usage_inventory_required')
    if type(value.get('count')) is not int or value['count']!=len(rows):
        raise ValueError('usage_inventory_count_mismatch')
    index={}
    for row in rows:
        if not isinstance(row,dict) or row.get('project_id')!=project or row.get('provider_id')!='openai':
            raise ValueError('usage_scope_mismatch')
        result={}
        for key in USAGE_REFS:
            item=row.get(key)
            if item is not None and (not isinstance(item,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',item)):
                raise ValueError('invalid_usage_identifier')
            result[key]=item
        for key in USAGE_COUNTS:
            item=row.get(key)
            if item is not None and (type(item) is not int or item<0):
                raise ValueError('invalid_usage_measurement')
            result[key]=item
        for key in ('started_at','completed_at','observed_at','runtime_duration_seconds'):
            item=row.get(key)
            if item is not None and (type(item) not in (int,float) or not math.isfinite(item) or item<0):
                raise ValueError('invalid_usage_time')
            result[key]=item
        terminal=row.get('terminal_outcome');complete=row.get('telemetry_completeness')
        if terminal not in {'succeeded','failed','interrupted','unknown'} or complete not in {'exact','partial','unavailable'}:
            raise ValueError('invalid_usage_disposition')
        result.update(terminal_outcome=terminal,telemetry_completeness=complete)
        evidence=row.get('evidence_ids',[])
        if not isinstance(evidence,list) or len(evidence)>32 or any(not isinstance(x,str) or not re.fullmatch(r'evidence-[A-Za-z0-9_-]{1,160}',x) for x in evidence):
            raise ValueError('invalid_usage_evidence_refs')
        result['evidence_ids']=evidence
        index.setdefault(result['provider_native_session_id'],[]).append(result)
    return index,len(rows)


def usage_for_lane(sample, index):
    def unknown(reason):return {'state':'unknown','reason':reason}
    if sample.get('read_error_type'):return unknown('runner_unavailable')
    native=sample.get('observed_thread_id');turn=sample.get('activity_turn_id')
    if not native:return unknown('native_session_unresolved')
    rows=index.get(native,[])
    if turn:
        rows=[row for row in rows if row['provider_native_turn_id']==turn]
    else:return unknown('current_turn_unresolved')
    if not rows:return unknown('usage_missing')
    if len(rows)!=1:return unknown('usage_ambiguous')
    row=rows[0];assignment=(sample.get('runner')or{}).get('assignmentId')
    if not row['id'] or not row['provider_native_turn_id']:return unknown('usage_identity_incomplete')
    if turn and assignment and row['assignment_id']!=assignment:return unknown('assignment_mismatch')
    return {'state':'observed','scope':'current_native_turn' if turn else 'latest_observed_native_turn',
        'record':row,'semantic_task_completion_inferred':False}


START_RECEIPT_GET_CAP = 5


def usage_binding(project, sample):
    runner=sample.get('runner') or {}
    values=(project,sample.get('observed_thread_id'),runner.get('assignmentId'))
    fence=runner.get('fence')
    if sample.get('read_error_type') or not all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',v) for v in values) or type(fence)is not int or fence<1:
        return None
    chain=sample.get('canonical_replacement_chain',[])
    if not isinstance(chain,list) or len(chain)>8:return None
    return dict(project_id=project,thread_id=values[1],assignment_id=values[2],fence=fence,
                replacement_chain_sha256=hashlib.sha256(json.dumps(chain,sort_keys=True).encode()).hexdigest())


def start_receipt_pair(value, binding):
    # Only the latest bounded canonical attempt; never scan older originals or
    # select a turn by observation time. The API itself authorizes tenant scope.
    def need(v):
        if not v:raise ValueError('canonical_START_attribution_mismatch')
    def identifier(v):return isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',v) is not None
    rows=value.get('items') if isinstance(value,dict) else None
    need(isinstance(rows,list) and len(rows)==1)
    attempt=rows[0];need(isinstance(attempt,dict))
    receipt=attempt.get('queued_start_receipt');correlation=attempt.get('queued_start_correlation')
    need(isinstance(receipt,dict) and isinstance(correlation,dict))
    need(value.get('threadId')==binding['thread_id'] and attempt.get('project_id')==binding['project_id'])
    need(binding['thread_id'] in (attempt.get('thread_id'),attempt.get('replacement_thread_id')))
    for key in ('organization_id','workspace_id','project_id','execution_profile_id','agent_profile_id','agent_profile_actor_id'):
        need(identifier(attempt.get(key)) and attempt[key]==correlation.get(key))
    need(type(attempt.get('agent_profile_revision'))is int and attempt['agent_profile_revision']>0 and attempt['agent_profile_revision']==correlation.get('agent_profile_revision'))
    for key in ('repository_resource_id','writable_repository_resource_ids','read_only_repository_resource_ids','resource_context'):
        left=attempt.get(key);right=correlation.get(key)
        if key.endswith('_resource_ids'):left=left or [];right=right or []
        need(left==right)
    need(correlation.get('thread_id')==receipt.get('thread_id')==receipt.get('provider_native_session_id')==correlation.get('provider_native_session_id')==binding['thread_id'])
    need(correlation.get('assignment_id')==binding['assignment_id'] and type(correlation.get('fence'))is int and correlation['fence']==binding['fence'])
    need(identifier(correlation.get('actor_identity_id')) and identifier(correlation.get('worker_id')))
    need(identifier(attempt.get('execution_id')) and attempt['execution_id']==receipt.get('requested_execution_id')==correlation.get('requested_execution_id'))
    need(identifier(receipt.get('execution_id')) and receipt['execution_id']==correlation.get('execution_id'))
    need(identifier(receipt.get('admission_claim_id')) and receipt['admission_claim_id']==correlation.get('admission_claim_id'))
    if attempt.get('queued_admission_claim_id') is not None:need(attempt['queued_admission_claim_id']==receipt['admission_claim_id'])
    need(receipt.get('provider_id')==correlation.get('provider_id')=='openai')
    runtime=receipt.get('runtime_id');need(runtime==correlation.get('runtime_id') and runtime in {'codex','codex-reasoning'})
    need(correlation.get('schema_version','1.0')==('1.1' if runtime=='codex-reasoning' else '1.0'))
    need(identifier(receipt.get('provider_native_turn_id')))
    need(isinstance(correlation.get('snapshot_sha256'),str) and re.fullmatch(r'[0-9a-f]{64}',correlation['snapshot_sha256']) is not None)
    need(identifier(correlation.get('runtime_generation')) and identifier(correlation.get('rpc_request_id')))
    need(type(correlation.get('prepared_at'))in (int,float) and math.isfinite(correlation['prepared_at']) and 0<=correlation['prepared_at']<=receipt.get('observed_at',-1))
    need(type(receipt.get('observed_at'))in (int,float) and math.isfinite(receipt['observed_at']) and receipt['observed_at']>=0)
    return receipt['provider_native_turn_id'],runtime


def select_usage_pairs(project,samples,state,now):
    if not isinstance(samples,dict) or len(samples)>40:raise ValueError('bounded_lane_inventory_required')
    cache=state.setdefault('observed_usage_pairs',{})
    if not isinstance(cache,dict):cache={};state['observed_usage_pairs']=cache
    selected={};eligible=[]
    for tid in list(cache):
        if tid not in samples:del cache[tid]
    for tid,sample in samples.items():
        binding=usage_binding(project,sample);old=cache.get(tid)
        if binding is None:
            cache.pop(tid,None);continue
        if not isinstance(old,dict) or old.get('binding')!=binding:
            cache.pop(tid,None);old=None
        turn=sample.get('activity_turn_id')
        if isinstance(turn,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',turn):
            if old is None or old.get('turn_id')!=turn:
                old=dict(binding=binding,turn_id=turn,source='runner_activity',first_observed_at=now)
            old['last_observed_at']=now;cache[tid]=old
            selected[tid]=(binding['thread_id'],turn,'current_native_turn')
        elif old and isinstance(old.get('turn_id'),str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',old['turn_id']):
            selected[tid]=(binding['thread_id'],old['turn_id'],'last_observed_turn_for_current_assignment')
        # Also inspect idle lanes with a remembered terminal pair: a new short
        # turn can begin/end between cycles under the SAME assignment/fence.
        runner=sample.get('runner') or {}
        if not turn and sample.get('activity')=='idle' and runner.get('live')is True and runner.get('assignmentStatus')in {'claimed','running'}:
            eligible.append((tid,binding))
    eligible.sort();calls=0
    if eligible:
        cursor=state.get('usage_receipt_cursor',0)
        if type(cursor)is not int or cursor<0:cursor=0
        for offset in range(min(START_RECEIPT_GET_CAP,len(eligible))):
            tid,binding=eligible[(cursor+offset)%len(eligible)];calls+=1
            try:
                value=api('/api/threads/'+binding['thread_id']+'/preflight-attempts?'+urlencode({'project_id':project,'limit':1}))
                turn,runtime=start_receipt_pair(value,binding)
                old=cache.get(tid)
                if not old or old.get('turn_id')!=turn:
                    old=dict(binding=binding,turn_id=turn,source='canonical_START_receipt',first_observed_at=now)
                old.update(last_observed_at=now,runtime_id=runtime);cache[tid]=old
                selected[tid]=(binding['thread_id'],turn,'last_observed_turn_for_current_assignment')
                samples[tid]['start_receipt_observation']={'state':'observed','scope':'current_assignment_only'}
            except Exception as exc:
                samples[tid]['start_receipt_observation']={'state':'unknown','error_type':type(exc).__name__}
        state['usage_receipt_cursor']=(cursor+calls)%len(eligible)
    return selected,calls


def observe_project_usage(project, samples, state, now, condition):
    selected,receipt_calls=select_usage_pairs(project,samples,state,now)
    pairs=sorted({(v[0],v[1]) for v in selected.values()})
    if not pairs or len(pairs)>40:
        for sample in samples.values():
            sample['canonical_usage']={'state':'unknown','reason':'current_turn_unresolved'}
        state.pop('usage_progress',None)
        return {'state':'unknown','reason':'bounded_current_turns_required','GET_count':0,'START_receipt_GET_count':receipt_calls,'output_progress':'unknown'}
    try:
        query=[('project_id',project),('provider_id','openai'),('include_items','true')]
        for native,turn in pairs:
            query.extend((('native_session_id',native),('native_turn_id',turn)))
        value=api('/api/agent-runtime-usage?'+urlencode(query))
        entries=value.get('items') if isinstance(value,dict) else None
        if not isinstance(entries,list) or len(entries)!=len(pairs) or value.get('count')!=len(pairs):
            raise ValueError('exact_bounded_usage_response_required')
        seen=set();observed=[];unknown={};runtime_by_pair={}
        for entry in entries:
            pair=(entry.get('native_session_id'),entry.get('native_turn_id'))
            if pair not in pairs or pair in seen:raise ValueError('usage_pair_response_mismatch')
            seen.add(pair)
            if entry.get('state')=='unknown' and entry.get('usage') is None:
                reason=entry.get('reason')
                if reason not in {'attribution_unavailable','usage_unavailable','stale_attribution','projection_too_large'}:
                    raise ValueError('closed_usage_unknown_reason_required')
                unknown[pair]=reason
            elif entry.get('state')=='observed' and isinstance(entry.get('usage'),dict):
                row=entry['usage']
                if (row.get('provider_native_session_id'),row.get('provider_native_turn_id'))!=pair:
                    raise ValueError('usage_pair_attribution_mismatch')
                observed.append(row);runtime_by_pair[pair]=row.get('runtime_id')
            else:raise ValueError('closed_usage_observation_required')
        index,count=closed_project_usage({'items':observed,'count':len(observed)},project)
    except Exception as exc:
        for sample in samples.values():
            sample['canonical_usage']={'state':'unknown','reason':'usage_batch_unavailable'}
        state.pop('usage_progress',None)
        return {'state':'unknown','error_type':type(exc).__name__,'output_progress':'unknown'}
    history=state.setdefault('usage_progress',{})
    for tid,sample in samples.items():
        choice=selected.get(tid)
        pair=choice[:2] if choice else None
        projected={**sample,'activity_turn_id':choice[1]} if choice else sample
        observation=usage_for_lane(projected,index) if pair in pairs else {'state':'unknown','reason':'current_turn_unresolved'}
        if observation.get('state')=='observed':
            observation['scope']=choice[2]
            observation['active_turn_observed']=choice[2]=='current_native_turn' and sample.get('activity')=='active'
        expected_runtime=state.get('observed_usage_pairs',{}).get(tid,{}).get('runtime_id')
        if observation.get('state')=='observed' and expected_runtime and runtime_by_pair.get(pair)!=expected_runtime:
            observation={'state':'unknown','reason':'runtime_mismatch'}
        if pair in unknown:observation={'state':'unknown','reason':unknown[pair]}
        sample['canonical_usage']=observation
        if observation['state']!='observed':
            history.pop(tid,None);continue
        row=observation['record']
        # Observation timestamps and fingerprints are not output measurements.
        measured={k:row[k] for k in (*USAGE_REFS,*USAGE_COUNTS,'terminal_outcome')}
        digest=hashlib.sha256(json.dumps(measured,sort_keys=True).encode()).hexdigest()
        old=history.get(tid,{})
        changed=old.get('changed_at',now) if old.get('digest')==digest else now
        history[tid]={'digest':digest,'changed_at':changed}
        observation['measurement_unchanged_seconds']=max(0,now-changed)
        terminal=row['terminal_outcome']
        if terminal!='unknown':state['observed_usage_pairs'][tid]['terminal_summary']=row
        if terminal in {'failed','interrupted'}:
            condition('usage_terminal:'+tid+':'+row['provider_native_turn_id'],'runtime_terminal_review',
                thread_id=tid,turn_id=row['provider_native_turn_id'],terminal_outcome=terminal,
                usage_id=row['id'],note='Canonical runtime terminal only; not Work/MR completion or replay authority.')
        elif terminal=='unknown' and sample.get('activity')=='active' and now-changed>600:
            condition('usage_unchanged:'+tid+':'+row['provider_native_turn_id'],'telemetry_review',
                thread_id=tid,turn_id=row['provider_native_turn_id'],age_seconds=int(now-changed),
                note='Measured counters unchanged; output progress remains UNKNOWN. Not proof of a model or task stall.')
    for tid in set(history)-set(samples):del history[tid]
    return {'state':'observed','rows':count,'requested_pairs':len(pairs),'pair_cap':40,'GET_count':1,'START_receipt_GET_count':receipt_calls,
        'output_progress':'unknown','semantic_task_completion_inferred':False}


def work_metrics(project, now, state):
    # Bound each observation to two pages; a slow inventory must not prevent
    # current worker/output monitoring or create a false project-wide failure.
    pending=state.setdefault('work_metrics_pending', {'started_at':now,
        'seen':[], 'closed':[], 'oldest':None, 'warnings':0, 'blocked':0, 'cursor':None})
    seen=set(pending['seen']);closed=set(pending['closed'])
    for _ in range(2):
        query={'project_id':project,'limit':200}
        if pending['cursor']:query['cursor']=pending['cursor']
        page=api('/api/work-items?'+urlencode(query),timeout=30)
        for item in page['items']:
            if item['ref'] in seen:raise ValueError('duplicate_work_item_during_inventory')
            if item.get('project_id')!=project:raise ValueError('foreign_work_item_during_inventory')
            seen.add(item['ref']);pending['warnings']+=bool(item.get('routingError'))
            if item.get('current_stage')=='closed':closed.add(item['ref'])
            else:
                pending['blocked']+=bool(item.get('blocker') or item.get('blocking_findings') or item.get('routingError'))
                created=item.get('created_at')
                if isinstance(created,(int,float)):
                    pending['oldest']=created if pending['oldest'] is None else min(pending['oldest'],created)
        pending.update(seen=sorted(seen),closed=sorted(closed),cursor=page.get('nextCursor'))
        if not pending['cursor']:
            state.pop('work_metrics_pending',None)
            return {'sampled_at':time.time(),'inventory_started_at':pending['started_at'],
                'total':len(seen),'open':len(seen)-len(closed),'closed':len(closed),
                'closed_refs':sorted(closed),
                'oldest_open_age_seconds':max(0,now-pending['oldest']) if pending['oldest'] is not None else None,
                'routing_warning_count':pending['warnings'],'blocked_open_count':pending['blocked'],
                'successful_completion_count':None,
                'note':'Incremental canonical lifecycle inventory. Closure includes failed/cancelled outcomes; merge/provider verification and actionable age are not inferred.'}
    return None


def report_pending(root, state, conditions, project):
    sent=state.setdefault('reported',{});pending=state.setdefault('pending_alerts',{})
    for key in list(sent):
        if key not in conditions:del sent[key]
    for key,item in list(pending.items()):
        if key not in conditions and not item['attempted']:del pending[key]
    for key,detail in conditions.items():
        if key in sent or key in pending:continue
        pending[key]={'detail':detail,'attempted':False,
            'dedupe_key':'vdo-thread-observer:v1:'+project+':'+hashlib.sha256(key.encode()).hexdigest()}
    atomic(root/'state.json',state)
    existing={x['dedupe_key']:x['id'] for x in api('/api/incidents')['items'] if x.get('project_id')==project}
    for key,item in list(pending.items()):
        detail=item['detail']
        receipt=existing.get(item['dedupe_key'])
        if not receipt and item['attempted']:continue  # Unknown POST: inspect, never blindly repeat.
        if not receipt:
            item['attempted']=True;atomic(root/'state.json',state)
            budget={'max_reasoning_attempts':0,'max_model_tokens':0,'max_model_cost_usd':0,'max_agent_handoffs':0}
            response=api('/api/incidents',{'project_id':project,'title':'VeridataOps observer: '+key[:160],
                'severity':'sev3' if detail['kind'] in {'native_failure','runner_failure','host_pressure','inspection_failed'} else 'sev4',
                'source':'systemd:codex-web-veridataops-monitor','dedupe_key':item['dedupe_key'],
                'impact_summary':json.dumps(detail,sort_keys=True)[:7000],
                'owner_identity_ids':['local-admin'],'goal_ids':[],'work_item_refs':[],
                'policy':{'reasoning_budgets':{s:budget for s in ('sev1','sev2','sev3','sev4')}}})
            receipt=response.get('item',response).get('id')
            if not receipt:raise ValueError('incident_outcome_requires_readback')
        sent[key]={'incident_id':receipt,'at':time.time()};del pending[key];atomic(root/'state.json',state)


def run(project, root, publish, include_usage=False):
    os.umask(0o077);root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (root/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=root/'state.json';state=json.loads(path.read_text()) if path.exists() else {}
        if state.get('project_id',project)!=project:raise ValueError('observer_state_project_mismatch')
        state['project_id']=project;now=time.time();conditions={};samples={};paused=None
        usage_batch={'state':'disabled','output_progress':'unknown'}
        def condition(key,kind,**fields):conditions[key]={'kind':kind,**fields}
        try:
            paused=api('/api/autonomy')['control']['mode']=='paused'
            bindings=bindings_for_project(api('/api/bots/bindings'),project)
            extra_path=root/'additional-lanes.json'
            if extra_path.exists():
                extra=json.loads(extra_path.read_text())
                if extra.get('project_id')!=project:raise ValueError('additional_lane_project_mismatch')
                for tid,name in extra.get('threads',{}).items():
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',tid):raise ValueError('invalid_additional_lane')
                    if not isinstance(name,str) or not name or len(name)>120:raise ValueError('invalid_additional_lane_name')
                    bindings.setdefault(tid,[])
                    if name not in bindings[tid]:bindings[tid].append(name)
                if len(bindings)>40:raise ValueError('thread_inspection_limit_exceeded')
            with ThreadPoolExecutor(max_workers=3) as pool:
                samples=dict(pool.map(lambda tid:sample_thread(tid,project),bindings))
            for tid,sample in samples.items():
                sample['binding_names']=bindings[tid]
                failures=state.setdefault('inspection_failures',{})
                if sample.get('read_error_type'):
                    failures[tid]=failures.get(tid,0)+1
                    if failures[tid]>=2:condition('inspection:'+tid,'inspection_failed',thread_id=tid,
                        error_type=sample['read_error_type'],count=failures[tid])
                    continue
                failures[tid]=0;runner=sample['runner'];latest=sample.get('progress') or {}
                if latest.get('status')=='failed':condition('turn_failed:'+tid+':'+str(latest.get('turn_id')),
                    'native_failure',thread_id=tid,turn_id=latest.get('turn_id'),error_code=latest.get('error_code'))
                if not paused and runner_failure(runner,sample.get('activity')):
                    condition('runner:'+tid+':'+str(runner.get('assignmentId'))+':'+str(runner.get('fence')),
                        'runner_failure',thread_id=tid,runner=runner)
                code=sample.get('automation_wait_code')
                if code and not paused:condition('admission:'+tid+':'+str(code),'admission_review',
                    thread_id=tid,code=code,note='Inspect the declared gate; retain every uncertain input without replay.')
                digest=hashlib.sha256(json.dumps(latest,sort_keys=True).encode()).hexdigest()
                history=state.setdefault('progress',{});old=history.get(tid,{})
                changed_at=old.get('changed_at',now) if old.get('digest')==digest else now
                history[tid]={'digest':digest,'changed_at':changed_at}
                sample['progress_age_seconds']=max(0,now-changed_at)
                if current_stall(sample,now-changed_at):
                    condition('stalled:'+tid+':'+str(latest.get('turn_id')),'progress_review',thread_id=tid,
                        age_seconds=int(now-changed_at),note='Review current tools and declared waits; no automatic interruption/replay.')
            if include_usage:
                usage_batch=observe_project_usage(project,samples,state,now,condition)
            if state.get('work_metrics_pending') or now-state.get('work_metrics',{}).get('sampled_at',0)>=900:
                try:
                    metrics=work_metrics(project,now,state);old=state.get('work_metrics',{})
                    if metrics is not None:
                        metrics['newly_closed_refs']=sorted(set(metrics['closed_refs'])-set(old.get('closed_refs',metrics['closed_refs'])))
                        state['work_metrics']=metrics
                except Exception as exc:
                    changed = False
                    if isinstance(exc,HTTPError) and exc.code==409:
                        try:
                            detail=json.loads(exc.read(4096)).get('detail',{})
                            changed=detail.get('code')=='work_item_cursor_stale'
                        except (ValueError,AttributeError):pass
                    if changed:
                        state.pop('work_metrics_pending',None)
                        state['work_metrics_change_observed_at']=now
                        # A versioned list changed between pages; retain the
                        # prior completed sample and begin a fresh scan later.
                    else:
                        condition('work_metrics','inventory_inspection_failed',error_type=type(exc).__name__)
                        if isinstance(exc,ValueError):state.pop('work_metrics_pending',None)
            idle=live_workers_idle(samples)
            state['idle_since']=state.get('idle_since',now) if idle else now
            if not paused and idle and now-state['idle_since']>=900:
                condition('all_idle','idle_review',seconds=int(now-state['idle_since']),
                    note='Review actionable original work and real external waits; idleness alone is not a product defect.')
            state['api_failures']=0
        except Exception as exc:
            state['api_failures']=state.get('api_failures',0)+1
            condition('project_inspection','inspection_failed',error_type=type(exc).__name__,count=state['api_failures'])
        result=subprocess.run(['systemctl','show','codex-web.service','-p','ActiveState','-p','MainPID','-p','NRestarts'],
            text=True,capture_output=True,timeout=5,check=True)
        service=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        previous=state.get('service');state['service']=service
        if service.get('ActiveState')!='active':condition('service_inactive','runner_failure',service=service)
        if previous and (previous.get('MainPID'),previous.get('NRestarts'))!=(service.get('MainPID'),service.get('NRestarts')):
            condition('service_restart:'+str(service.get('MainPID')),'runner_failure',service=service,previous=previous)
        free=shutil.disk_usage(root).free
        available=next(int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
        pressure=available<1024**3 or free<5*1024**3
        state['host_pressure_samples']=state.get('host_pressure_samples',0)+1 if pressure else 0
        if state['host_pressure_samples']>=2:condition('host_pressure','host_pressure',available_memory_bytes=available,free_disk_bytes=free)
        state['checked_at']=now;atomic(path,state)
        alert_error=None
        if publish and (conditions or state.get('pending_alerts')):
            try:report_pending(root,state,conditions,project)
            except Exception as exc:alert_error=type(exc).__name__
        result={'checked_at':now,'project_id':project,'autonomy_paused':paused,'service':service,
            'threads':samples,'conditions':conditions,'work_metrics':state.get('work_metrics'),'canonical_usage_batch':usage_batch,
            'inspection_complete':not state.get('api_failures') and bool(samples) and not any(
                x.get('read_error_type') or x.get('runner_projection_status')=='invalid' for x in samples.values()),
            'free_disk_bytes':free,'available_memory_bytes':available,'alert_error_type':alert_error,
            'pending_alerts':list(state.get('pending_alerts',{})),
            'note':'Canonical runner/queue metadata only: no native transcript RPC, starts, replay, restarts, ACKs, queue/accounting or pause changes. Output progress is UNKNOWN; runner activity is not task completion.'}
        atomic(root/'latest.json',result)
        print(json.dumps({'at':now,'project_id':project,'threads':len(samples),'conditions':list(conditions),'alert_error_type':alert_error}))
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',required=True)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--publish-incidents',action='store_true')
    parser.add_argument('--observe-canonical-usage',action='store_true')
    args=parser.parse_args();run(args.project,args.state_dir,args.publish_incidents,args.observe_canonical_usage)
