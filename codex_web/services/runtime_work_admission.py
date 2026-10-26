"""Read-only explanations of known holds; never authorize or claim work."""
from codex_web.identity import TenantScope
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.turn_queue_policy import TurnQueuePolicy


def actionable_owner_health(projects_by_owner, *, owners, admission,
                            binding_for, thread_active, queue_depth):
    """Project the existing watchdog's expected activity without dispatching it."""
    idle, held, unknown = [], [], set()
    for owner in owners:
        available = False
        busy = False
        for project_id in projects_by_owner.get(owner, ()):
            observed = admission.project(project_id)
            if observed['state'] == 'held':
                held.append({'owner': owner, 'projectId': project_id,
                             'reason': observed['code']})
                continue
            if observed['state'] != 'eligible':
                unknown.add(owner)
            available = True
            binding = binding_for(owner, project_id)
            if binding is not None and binding.thread_id and (
                    thread_active(binding.thread_id) or queue_depth(binding.thread_id)):
                busy = True
        if available and not busy:
            idle.append(owner)
    return {'idleActionableOwners': idle, 'heldActionableProjects': held,
            'unknownAdmissionOwners': sorted(unknown)}


class RuntimeWorkAdmission:
    # Bound additional point reads per health refresh. Overflow is UNKNOWN,
    # never silently treated as held or healthy. Queue depth remains unfiltered.
    MAX_OBSERVATIONS = 128

    def __init__(self, *, control, runtime_enabled, projects, gates, preflights):
        self.control, self.runtime_enabled = control, runtime_enabled
        self.projects, self.gates, self.preflights = projects, gates, preflights
        self._projects = {}
        self._pauses = {}
        self._observations = 0

    @staticmethod
    def result(state, code):
        return {'state': state, 'code': code}

    def _bounded(self):
        self._observations += 1
        return self._observations <= self.MAX_OBSERVATIONS

    def _project(self, project_id):
        if project_id not in self._projects:
            project = self.projects.get(project_id)
            if project.id != project_id:
                raise ValueError('health project identity changed')
            self._projects[project_id] = project
        return self._projects[project_id]

    def _control_hold(self, project_id):
        if not self.runtime_enabled:
            return self.result('unknown', 'autonomy_disabled')
        reason = AutonomyController._scoped_pause_reason(self.control, actor=None,
            project_id=project_id)
        if reason is not None:
            return self.result('held', 'autonomy_scoped_pause')
        if self.control.mode != 'active':
            return self.result('held', 'autonomy_' + self.control.mode.value)
        project = self._project(project_id)
        if project_id not in self._pauses:
            self._pauses[project_id] = self.gates.operator_paused('bot-runtime', project_id,
                scope=TenantScope(organization_id=project.organization_id,
                                  workspace_id=project.workspace_id))
        if self._pauses[project_id]:
            return self.result('held', 'bot_runtime_operator_paused')
        if self.control.exclusive_goal_scope is not None:
            return self.result('unknown', 'exclusive_scope_admission_unobserved')
        return self.result('eligible', 'no_known_hold')

    def project(self, project_id):
        if not self._bounded():
            return self.result('unknown', 'admission_observation_limit')
        self._project(project_id)
        return self._control_hold(project_id)

    def queued(self, queued):
        if not self._bounded():
            return self.result('unknown', 'admission_observation_limit')
        project = self._project(queued.project_id)
        retained = self.preflights.get('preflight-queued-' + queued.id)
        if retained is not None:
            if not self.preflights.queued_retention_matches(retained,
                    organization_id=project.organization_id, workspace_id=project.workspace_id,
                    project_id=project.id, snapshot=queued.model_dump(mode='json')):
                return self.result('held', 'queued_retention_refused')
            if retained.status in {'reconciliation_required', 'failed', 'started', 'blocked'}:
                return self.result('held', 'retained_' + retained.status)
            if retained.operator_admission is not None:
                return self.result('unknown', 'operator_admission_unobserved')
            if retained.status != 'queued':
                return self.result('unknown', 'retained_admission_in_progress')
        # Governed exceptions can legitimately admit work while ordinary
        # automation is paused. Do not invoke their validators from health:
        # they can own mutable admission witnesses. Unknown must stay visible.
        if queued.project_coordination_dispatch is not None or queued.owner_lane_recovery is not None:
            return self.result('unknown', 'governed_admission_unobserved')
        if TurnQueuePolicy.is_human_source(queued.source):
            return self.result('eligible', 'human_origin')
        if not self.runtime_enabled:
            return self.result('unknown', 'autonomy_disabled')
        # Match ordinary TurnExecutionService.automation_wait_reason exactly:
        # Project/bot pauses gate watchdog/new bot dispatch, but are not a
        # blanket admission gate for an already queued ordinary native input.
        if self.control.mode != 'active':
            return self.result('held', 'autonomy_' + self.control.mode.value)
        return self.result('eligible', 'no_known_hold')
