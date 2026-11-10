"""Bounded target-Work discovery of governed INFO, not repository access (#2008)."""
import base64
import binascii
import hashlib
import json
import re

from codex_web.artifact_sharing_policy import METHOD
from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel
from codex_web.control_plane_broker import ControlPlaneBrokerOperation
from codex_web.services.artifact_sharing import scoped_work
from codex_web.services.worker_verifications import WorkerVerificationAdapter

OPERATION = ControlPlaneBrokerOperation(id='verification.shared-inputs.read',method='GET',
    path_template='/api/artifacts/shared-inputs',capability='api.artifacts.read',authority_level=AuthorityLevel.READ)
MAX_CANDIDATES = 32
MAX_RESPONSE_BYTES = 32768


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def public_contract():
    return {'required_query_parameters':['work_item_ref'],
        'optional_query_parameters':['limit','cursor'], 'request_body':{'allowed':False},
        'query_parameters':{'work_item_ref':'Exact current canonical target Work',
            'limit':{'minimum':1,'maximum':16,'default':8},'cursor':'Opaque same-scope continuation'},
        'bounds':{'returned_items':16,'candidate_records':MAX_CANDIDATES,'metadata_bytes':MAX_RESPONSE_BYTES},
        'effect':'Read governed INFO IDs and digest only; not acceptance, independent QA, or Source repository execution authority',
        'scope':'Current owning WRITE assignment; both requester and worker need target Artifact/Evidence READ authority',
        'pagination':'Exact tenant/Project/Work/provider query before limit, newest recorded first; complete=false requires continuation, not an absence conclusion; database scan cost is not bounded'}


class WorkerSharedInputsAdapter:
    def __init__(self,broker):
        self.broker=broker
        self.owner=WorkerVerificationAdapter(broker)

    def denied(self,reason):
        self.owner.denied(reason)

    def current(self,assignment,worker,resolved,validator,ref):
        # Check locally before scope(), whose legacy target helper expects an
        # existing Work. Missing/foreign Work must be a typed refusal, not 500.
        work=scoped_work(self.broker,assignment,ref)
        actual=validator() if validator is not None else None
        keys=('id','organization_id','workspace_id','project_id','assigned_worker_id','execution_id',
            'execution_workspace_id','fence','created_by','subject','work_item_ref','resource_ids',
            'repository_scope','repository_target','sandbox','network','execution_profile_id',
            'execution_profile_definition','agent_profile','resource_context')
        if actual is None or any(getattr(actual,key)!=getattr(assignment,key) for key in keys):
            self.denied('Shared-input reader custody changed')
        principal,requester,_=self.owner.principals(assignment,worker,resolved,validator,{})
        self.owner.scope(assignment,ref)
        registered=self.broker.execution_workers.store.worker(assignment.assigned_worker_id)
        if getattr(registered,'revoked_at',None) is not None:
            self.denied('Shared-input reader worker revoked')
        if not work.resource_ids or len(work.resource_ids)>64:
            self.denied('Shared-input target requires bounded canonical Resources')
        decisions=[]
        for actor in (principal,requester):
            for capability in ('api.artifacts.read','api.evidence.read'):
                decision=self.broker.authority.evaluate(AuthorityEvaluationRequest(
                    capability=capability,level=AuthorityLevel.READ,project_id=assignment.project_id,
                    resource_ids=tuple(work.resource_ids)),actor=actor,now=self.broker._clock())
                if decision.outcome!='allow' or decision.definition_ref is None:
                    self.denied('Both current principals require scoped Artifact and Evidence read authority')
                decisions.append(decision.definition_ref)
        policies=getattr(self.broker,'artifact_sharing_policies',None)
        if policies is None:
            self.denied('Current shared-input policy unavailable')
        from codex_web.services.definitions import (DefinitionError,DefinitionNotFoundError,
            DefinitionConflictError,DefinitionCompatibilityError)
        try:
            _,reference=policies.resolve(assignment,requester,now=self.broker._clock())
        except (DefinitionError,DefinitionNotFoundError,DefinitionConflictError,DefinitionCompatibilityError,ValueError):
            self.denied('Current shared-input policy unavailable')
        # Compare actual canonical scope/policy again after all metadata I/O.
        return principal,requester,work,reference,tuple(decisions)

    def query(self,query):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerRequestError
        def bad():
            raise ControlPlaneBrokerRequestError('Shared-input lookup requires exact Work, limit 1..16 and same-scope cursor')
        if set(query)-{'work_item_ref','limit','cursor'} or any(len(v)!=1 or not v[0] for v in query.values()):
            bad()
        ref=(query.get('work_item_ref') or [''])[0]
        value=(query.get('limit') or ['8'])[0]
        if not 1<=len(ref)<=512 or not re.fullmatch(r'[0-9]{1,2}',value) or not 1<=int(value)<=16:
            bad()
        return ref,int(value),(query.get('cursor') or [None])[0]

    def cursor(self,value,scope):
        if value is None:
            return None
        try:
            if len(value)>512 or not re.fullmatch(r'[A-Za-z0-9_-]+',value):
                raise ValueError()
            decoded=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
            if (set(decoded)!={'v','scope','before'} or type(decoded['v']) is not int or decoded['v']!=1
                    or decoded['scope']!=scope or type(decoded['before']) is not int
                    or not 1<=decoded['before']<=2**61-1):
                raise ValueError()
            return decoded['before']
        except (ValueError,TypeError,binascii.Error):
            from codex_web.services.control_plane_broker import ControlPlaneBrokerRequestError
            raise ControlPlaneBrokerRequestError('Invalid or foreign shared-input cursor') from None

    def pair(self,identifier,assignment,worker,work):
        from codex_web.services.worker_artifact_sharing import DELIVERY_SOURCE
        from codex_web.identity import TenantScope
        service=self.owner.service()
        artifact=service.get_artifact(identifier,actor=worker)
        if (artifact.provider!=METHOD or artifact.source!=DELIVERY_SOURCE or artifact.work_item_ref!=work.ref
                or artifact.project_id!=assignment.project_id or set(artifact.resource_ids)!=set(work.resource_ids)
                or artifact.artifact_type!='report' or artifact.lifecycle!='active'
                or artifact.digest is None or artifact.digest.algorithm!='sha256'
                or artifact.content is None or artifact.content.size_bytes>262144):
            self.denied('Shared-input candidate identity or target changed')
        self.owner.visible(artifact,assignment,worker)
        producer=self.broker.identity.actor_for_identity(artifact.producer_identity_id,scope=TenantScope(
            organization_id=assignment.organization_id,workspace_id=assignment.workspace_id))
        proof=service.find_idempotent_evidence(actor=producer,source=DELIVERY_SOURCE,idempotency_key=artifact.external_id)
        if proof is None:
            self.denied('Shared-input canonical INFO unavailable')
        self.owner.visible(proof,assignment,worker)
        return artifact,proof

    def read(self,assignment,worker,resolved,validator):
        from codex_web.services.artifact_evidence import ArtifactEvidenceError
        from codex_web.services.execution_workers import WorkerConflictError,WorkerLeaseError
        from codex_web.services.execution_workspaces import ExecutionWorkspaceConflictError
        from codex_web.services.control_plane_broker import ControlPlaneBrokerRequestError
        try:
            return self._read(assignment,worker,resolved,validator)
        except (ArtifactEvidenceError,WorkerConflictError,WorkerLeaseError,ExecutionWorkspaceConflictError,ValueError) as exc:
            raise ControlPlaneBrokerRequestError('Canonical shared-input observation unavailable',
                status_code=409,code='shared_inputs_unavailable') from exc

    def _read(self,assignment,worker,resolved,validator):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        ref,limit,cursor=self.query(resolved.query)
        original=self.current(assignment,worker,resolved,validator,ref)
        principal,_,work,_,_=original
        scope=hashlib.sha256(canonical([assignment.organization_id,assignment.workspace_id,
            assignment.project_id,ref,sorted(work.resource_ids)])).hexdigest()
        before=self.cursor(cursor,scope)
        page=self.owner.service().shared_input_candidates(principal,project_id=assignment.project_id,
            work_item_ref=ref,before=before)
        pairs=[]
        examined=0
        last=None
        for candidate in page['candidates']:
            last=candidate['ordinal']
            examined+=1
            try:
                pair=self.pair(candidate['id'],assignment,principal,work)
            except ControlPlaneBrokerDeniedError:
                # Never leak IDs of revoked/invisible pairs. An unavailable
                # global policy or changed reader is refused by current() below.
                continue
            pairs.append(pair)
            if len(pairs)==limit:
                break
        truncated=page['more'] or examined<len(page['candidates'])
        current=self.current(assignment,worker,resolved,validator,ref)
        if (not current[0].has_same_authority(original[0])
                or not current[1].has_same_authority(original[1]) or current[2:]!=original[2:]):
            self.denied('Shared-input current Work, policy or authority changed during lookup')
        items=[]
        for artifact,proof in pairs:
            if self.pair(artifact.id,assignment,current[0],current[2])!=(artifact,proof):
                self.denied('Shared-input changed during lookup')
            items.append({'artifact_id':artifact.id,'evidence_id':proof.id,'sha256':artifact.digest.value,
                'size_bytes':artifact.content.size_bytes,'result':'info'})
        final=self.current(assignment,worker,resolved,validator,ref)
        if (not final[0].has_same_authority(original[0])
                or not final[1].has_same_authority(original[1]) or final[2:]!=original[2:]):
            self.denied('Shared-input authority changed during final visibility checks')
        next_cursor=(base64.urlsafe_b64encode(canonical({'v':1,'scope':scope,'before':last})).decode().rstrip('=')
            if truncated and last is not None else None)
        result={'work_item_ref':ref,'items':items,'complete':not truncated,'truncated':truncated,
            'next_cursor':next_cursor,'source_information_only':True,'independent_qa_or_acceptance':False,
            'repository_execution_scope_changed':False}
        if len(canonical(result))>MAX_RESPONSE_BYTES:
            self.denied('Shared-input metadata response exceeds bounded size')
        return result
