"""Read the selected conversation's canonical Goal bindings; never resume them."""
from codex_web.services.home_checkpoint_telemetry import checkpoint_component
from codex_web.services.home_operator import HomeOperatorConflict
from codex_web.services.identity import IdentityService
from codex_web.services.goals import GoalNotFoundError
from codex_web.storage.agent_sessions import AgentSessionNotFoundError


class HomeOperatorGoalInspection:
    def __init__(self, operator, bindings, sessions):
        self.operator = operator
        self.bindings = bindings
        self.sessions = sessions

    @checkpoint_component('goal_inspection')
    def inspect(self, actor, *, expected_revision):
        IdentityService.require_admin(actor)
        selected = self.operator.status(actor)
        pointer = selected['binding']
        if (selected['status'] != 'configured' or not selected['thread'] or not pointer
                or pointer['revision'] != expected_revision):
            raise HomeOperatorConflict('Operator selection changed or is unavailable; refresh Home')
        thread_id = selected['thread']['id']
        rows = sorted((row for row in self.bindings.list_all(scope=actor.tenant)
                       if row.project_id == 'home' and row.thread_id == thread_id),
                      key=lambda row: (row.updated_at, row.id), reverse=True)
        # Deliberately project only bounded metadata, not objective context,
        # transcript, native storage, worker tokens or delegation authority.
        try:
            goals = {goal.id: goal for goal in self.bindings.goals.get_many(
                tuple(row.goal_id for row in rows[:50]), scope=actor.tenant, project_id='home')}
        except GoalNotFoundError as exc:
            raise HomeOperatorConflict('Goal unavailable; inspect canonical Goals') from exc
        items = []
        for row in rows[:50]:
            try:
                session = self.sessions.get(row.agent_session_id, actor)
                goal = goals[row.goal_id]
            except AgentSessionNotFoundError as exc:
                raise HomeOperatorConflict('Goal binding or session unavailable; inspect canonical Goals') from exc
            if (session.project_id != 'home' or session.provider_native_session_id != thread_id
                    or session.provider_id != row.provider_id or session.runtime_id != row.runtime_id):
                raise HomeOperatorConflict('Goal binding session changed; inspect canonical Goals')
            items.append({
                'goal_id': goal.id, 'title': goal.title, 'goal_status': goal.status,
                'goal_revision': goal.revision, 'binding_id': row.id,
                'binding_goal_revision': row.goal_revision, 'status': row.status,
                'execution_mode': row.execution_mode, 'checkpoint_ref': row.checkpoint_ref,
                'stop_reason': row.stop_reason, 'last_execution_id': row.last_execution_id,
                'updated_at': row.updated_at,
            })
        current = self.operator.status(actor)
        if current['status'] != 'configured' or current['binding'] != pointer or current['thread'] != selected['thread']:
            raise HomeOperatorConflict('Operator selection changed during inspection; refresh Home')
        return {'thread_id': thread_id, 'binding_revision': expected_revision,
                'observed_at': self.operator.clock(), 'items': items, 'has_more': len(rows) > 50}
