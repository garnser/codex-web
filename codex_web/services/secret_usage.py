from __future__ import annotations

from collections import defaultdict

from codex_web.configuration import ConfigurationValueKind
from codex_web.services.configuration import ConfigurationNotFoundError


class SecretUsageService:
    """Metadata projection over canonical references; never resolves material."""

    MAX_SCAN = 5000
    MAX_DISPLAY = 100

    def __init__(self, *, projects, resources, configuration, providers, models,
                 agents, extensions, actions, assignments, connections):
        self.projects = projects
        self.resources = resources
        self.configuration = configuration
        self.providers = providers
        self.models = models
        self.agents = agents
        self.extensions = extensions
        self.actions = actions
        self.assignments = assignments
        self.connections = connections

    def index(self, project_id, actor):
        project = self.projects.get(project_id, actor.tenant)
        projects = {item.id for item in self.projects.list(actor.tenant)}
        resources = {item.id for item in self.resources.list(actor)}
        project_resources = {item.id for item in self.resources.project_resources(project, actor=actor)}
        rows = defaultdict(list)
        scanned = 0

        def scan():
            nonlocal scanned
            scanned += 1
            if scanned > self.MAX_SCAN:
                raise ValueError('Secret consumer scan exceeds its bounded limit; impact is unavailable')

        def add(secret_id, kind, object_id, label, *, target_project=None,
                shared=False, page='configuration', state=None, revision=None):
            if not secret_id:
                return
            scan()
            if target_project is not None and target_project not in projects:
                return
            visible = shared or target_project == project.id
            # Other Project references are counted without exposing their IDs,
            # labels or configuration. A shared secret mutation affects them too.
            rows[secret_id].append({
                'object_type': kind if visible else 'outside_view',
                'object_id': object_id if visible else None,
                'label': label if visible else 'Consumer outside this Project view',
                'project_id': target_project if visible else None,
                'scope': 'shared_workspace' if shared else 'project',
                'state': str(state) if visible and state is not None else None,
                'revision': revision if visible else None,
                'page': page if visible else None, 'visible': visible,
            })

        for item in self.projects.list(actor.tenant):
            scan()
            source = item.authoritative_task_source
            if source:
                add(source.credential_secret_id, 'task_source', item.id,
                    'Authoritative TaskSource credential', target_project=item.id,
                    page='work-items', state=source.source_type)

        for item in self.configuration.list_records():
            scan()
            scope = str(item.scope_type)
            if scope == 'organization' and item.scope_id != actor.organization_id:
                continue
            if scope == 'workspace' and item.scope_id != actor.workspace_id:
                continue
            if scope == 'project' and item.scope_id not in projects:
                continue
            if scope == 'resource' and item.scope_id not in resources:
                continue
            try:
                spec = self.configuration.specs.get(item.key)
            except ConfigurationNotFoundError:
                raise ValueError('A configuration schema is unavailable; secret impact cannot be established') from None
            if spec.value_kind != ConfigurationValueKind.SECRET_REF:
                continue
            shared = scope not in {'project', 'resource'}
            target = item.scope_id if scope == 'project' else project.id if scope == 'resource' and item.scope_id in project_resources else None
            add(item.value.get('secret_id'), 'configuration', item.id,
                f'Configuration · {item.key} · {scope}', target_project=target,
                shared=shared, state=item.state, revision=item.revision)

        for item in self.providers.list_bindings(actor):
            scan()
            add(item.credential_ref, 'action_provider', item.id,
                'Action Provider credential', target_project=item.project_id,
                shared=item.project_id is None, page='integrations', state='enabled' if item.enabled else 'disabled')
        for item in self.models.list_providers(actor):
            scan()
            add(item.credential_ref, 'model_provider', item.id,
                'Model Provider credential', shared=True, page='agents', state=item.status)
        for item in self.agents.list(actor):
            scan()
            for reference in item.credential_refs:
                add(reference, 'agent_provider', item.id, 'Agent runtime/provider credential',
                    shared=True, page='operations', state=item.lifecycle)
        for item in self.extensions.list(actor):
            scan()
            for reference in item.secret_bindings.values():
                add(reference, 'extension', item.id, 'Extension credential binding',
                    shared=True, page='integrations', state=item.lifecycle)
        for item in self.connections():
            scan()
            if item.project_id not in projects:
                continue
            for field in ('bot_token_secret_id', 'slack_app_token_secret_id',
                          'signing_secret_secret_id', 'webhook_secret_secret_id'):
                add(getattr(item, field), 'bot_connection', item.id,
                    'Bot integration credential', target_project=item.project_id, page='integrations')
        for item in self.assignments(actor):
            scan()
            if (item.organization_id != actor.organization_id or item.workspace_id != actor.workspace_id):
                continue
            for reference in item.secret_refs:
                add(reference, 'execution', item.id, 'Execution assignment credential reference',
                    target_project=item.project_id, state=item.status, page='runs')
        for item in self.actions.list(actor):
            scan()
            add(item.credential_ref, 'action_intent', item.id, 'ActionIntent credential reference',
                target_project=item.project_id, shared=item.project_id is None,
                state=item.status, page='automations')
        return dict(rows)

    def snapshot(self, secret_id, project_id, actor):
        rows = self.index(project_id, actor).get(secret_id, [])
        visible = [row for row in rows if row['visible']]
        return {'schema_version': '1.0', 'available': True, 'secret_id': secret_id,
                'project_id': project_id, 'count': len(rows),
                'outside_view_count': len(rows) - len(visible),
                'items': visible[:self.MAX_DISPLAY], 'truncated': len(visible) > self.MAX_DISPLAY,
                'coverage': ['task_sources', 'configuration_revisions', 'action_providers',
                             'model_providers', 'agent_providers', 'extensions',
                             'bot_connections', 'execution_assignments', 'action_intents'],
                'limitations': ['Legacy environment/file credentials are outside canonical SecretReference usage.',
                                'Revision and execution history references are labeled; counts do not imply active use.']}
