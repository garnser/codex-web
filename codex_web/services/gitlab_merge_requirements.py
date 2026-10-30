"""Bounded GitLab native merge gates, not an invented complete policy inventory."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import re
import time
from urllib.parse import quote

import httpx

from codex_web.code_host_merge_requirements import (
    CodeHostMergeCheckFact, CodeHostMergeRequirementsFact, CodeHostMergeReviewRequirements,
    CodeHostMergeSourceFact, MergeFactAvailability as Availability, MergeFactSource as Source,
)
from codex_web.code_hosts import CodeHostError
from codex_web.services.code_host_action_contract import CodeHostActionContract
from codex_web.integrations.gitlab_client import GitLabAPIError, GitLabReadIncompleteError


def obj(value):
    if not isinstance(value, dict):
        raise GitLabReadIncompleteError('object_required')
    return value


def count(value):
    if type(value) is not int or not 0 <= value <= 100_000:
        raise GitLabReadIncompleteError('count_required')
    return value


def native_id(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise GitLabReadIncompleteError('native_identity_required')
    return value


def text(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,1024}', value):
        raise GitLabReadIncompleteError('invalid_reference')
    return value


def branch_name(value):
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise GitLabReadIncompleteError('invalid_target_branch')
    try:
        branch = CodeHostActionContract.branch(value, field='target_branch')
    except ValueError:
        raise GitLabReadIncompleteError('invalid_target_branch') from None
    if branch != value:
        raise GitLabReadIncompleteError('invalid_target_branch')
    return branch


def revision(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) is None:
        raise GitLabReadIncompleteError('invalid_revision')
    return value


class _GuardRejected(Exception):
    def __init__(self, error):
        self.error = error


class GitLabMergeRequirementsReader:
    """Legacy whole-30s or trusted-wall read, at most eight GETs/128KiB."""

    def __init__(self, client, binding, resource, project, credential, *, checkpoint=None, clock=time.time,
                 observation_seconds=30, execution_budget=None):
        if type(observation_seconds) is not int or not 1 <= observation_seconds <= 300:
            raise ValueError('invalid observation validity')
        self.observation_seconds = observation_seconds
        self.execution_budget = execution_budget
        if not credential:
            raise CodeHostError('Authenticated GitLab merge-requirement read required')
        if not isinstance(project, str) or not project or len(project) > 1024:
            raise CodeHostError('GitLab repository locator is invalid')
        self.client, self.binding, self.resource = client, binding, resource
        self.project, self.credential, self.checkpoint, self.clock = project, credential, checkpoint, clock
        self.root = 'projects/' + quote(project, safe='')
        self.calls, self.used, self.sources = 0, False, {}
        self.pr = self.base = self.approvals = None
        self.changed = False

    def source(self, source, availability, reason=None, records=None):
        self.sources[source] = CodeHostMergeSourceFact(source=source, availability=availability,
            provider_source='gitlab:' + source.value, observed_at=float(self.clock()),
            reason_code=reason, record_count=records)

    async def guard(self):
        try:
            if self.execution_budget is not None:
                self.execution_budget.require_current()
            if self.checkpoint is not None:
                await self.checkpoint()
            if self.execution_budget is not None:
                self.execution_budget.require_current()
        except Exception as error:
            raise _GuardRejected(error) from None

    async def get(self, path):
        await self.guard()
        self.calls += 1
        if self.calls > 8:
            raise GitLabReadIncompleteError('request_limit')
        try:
            request = self.client.read_json_bounded(self.binding.base_url, path, token=self.credential)
            value = await request if self.execution_budget is None else await self.execution_budget.request(request)
        except Exception:
            await self.guard()
            raise
        await self.guard()
        return obj(value)

    def project_identity(self, raw):
        identifier = native_id(raw.get('id'))
        if identifier == 0:
            raise GitLabReadIncompleteError('repository_identity')
        external = self.resource.provenance.external_id if self.resource.provenance else None
        if external and external.isdigit() and str(identifier) != external:
            raise GitLabReadIncompleteError('repository_identity')
        if not self.project.isdigit() and raw.get('path_with_namespace') != self.project:
            raise GitLabReadIncompleteError('repository_identity')
        if self.project.isdigit() and str(identifier) != self.project:
            raise GitLabReadIncompleteError('repository_identity')
        return identifier

    def pull(self, raw):
        if (type(raw.get('iid')) is not int or raw['iid'] != self.number
                or type(raw.get('project_id')) is not int or raw['project_id'] != self.project_id):
            raise GitLabReadIncompleteError('merge_request_identity')
        if type(raw.get('draft')) is not bool:
            raise GitLabReadIncompleteError('draft_required')
        # Keep only gate and scope fields. Provider descriptions, note bodies,
        # names and reflected error messages never enter the projection.
        return dict(head=revision(raw.get('sha')), branch=branch_name(raw.get('target_branch')),
            state=text(raw.get('state')), draft=raw['draft'], gate=text(raw.get('detailed_merge_status')),
            pipeline=raw.get('head_pipeline'), author=native_id(obj(raw.get('author')).get('id')),
            discussions=raw.get('blocking_discussions_resolved'))

    def branch(self, raw):
        if raw.get('name') != self.pr['branch']:
            raise GitLabReadIncompleteError('target_branch_identity')
        return revision(obj(raw.get('commit')).get('id'))

    def approval(self, raw):
        if type(raw.get('approved')) is not bool:
            raise GitLabReadIncompleteError('approval_decision_required')
        # Optional EE fields are never defaulted from CE absence or HTTP404.
        required = count(raw['approvals_required']) if 'approvals_required' in raw else None
        left = count(raw['approvals_left']) if 'approvals_left' in raw else None
        if (required is not None and left is not None and left > required
                or raw['approved'] and left is not None and left != 0):
            raise GitLabReadIncompleteError('approval_counts_disagree')
        rows = raw.get('approved_by')
        if not isinstance(rows, list) or len(rows) > 500:
            raise GitLabReadIncompleteError('approval_list_incomplete')
        users = [native_id(obj(obj(row).get('user')).get('id')) for row in rows]
        if 0 in users or len(set(users)) != len(users):
            raise GitLabReadIncompleteError('approval_identity')
        return dict(approved=raw['approved'], required=required, left=left, users=tuple(sorted(users)))

    async def read(self, external_id):
        if self.execution_budget is None:
            return await self._read_once(external_id)
        async with asyncio.timeout(self.execution_budget.remaining_seconds()):
            result = await self._read_once(external_id)
            self.execution_budget.require_current()
            return result

    async def _read_once(self, external_id):
        if self.used:
            raise CodeHostError('A merge-requirement reader owns exactly one observation')
        self.used = True
        if not re.fullmatch(r'[1-9][0-9]{0,9}', str(external_id)) or int(external_id) > 2**31-1:
            raise CodeHostError('GitLab merge requirements require an exact positive MR IID')
        self.number, self.started = int(external_id), float(self.clock())
        self.source(Source.LEGACY_PROTECTION, Availability.UNKNOWN, 'native_gate_is_not_branch_policy_inventory')
        self.source(Source.EFFECTIVE_RULES, Availability.UNKNOWN, 'native_gate_is_not_effective_rule_inventory')
        self.source(Source.CONVERSATIONS, Availability.UNKNOWN, 'aggregate_resolution_is_not_thread_inventory')
        try:
            async with (asyncio.timeout(30) if self.execution_budget is None else nullcontext()):
                self.project_id = self.project_identity(await self.get(self.root))
                path = self.root + f'/merge_requests/{self.number}'
                self.pr = self.pull(await self.get(path))
                self.base = self.branch(await self.get(self.root+'/repository/branches/'+quote(self.pr['branch'], safe='')))
                self.source(Source.PULL_REQUEST, Availability.COMPLETE, records=1)
                try:
                    self.approvals = self.approval(await self.get(path+'/approvals'))
                except (GitLabAPIError, GitLabReadIncompleteError, httpx.HTTPError) as error:
                    self.unavailable(Source.REVIEWS, error)
                else:
                    self.source(Source.REVIEWS, Availability.COMPLETE, records=len(self.approvals['users']))
                # All observed native gates and target/head identities must be
                # stable across the bounded read. This does not claim atomicity.
                if self.approvals is not None:
                    if self.approval(await self.get(path+'/approvals')) != self.approvals:
                        self.changed = True
                        self.source(Source.REVIEWS, Availability.CHANGED, 'approval_decision_changed')
                after = self.pull(await self.get(path))
                base = self.branch(await self.get(self.root+'/repository/branches/'+quote(self.pr['branch'], safe='')))
                if after != self.pr or base != self.base:
                    self.changed = True
                    self.source(Source.PULL_REQUEST, Availability.CHANGED, 'merge_request_or_target_changed')
        except _GuardRejected as error:
            raise error.error from None
        except (GitLabAPIError, GitLabReadIncompleteError, httpx.HTTPError, TimeoutError, ValueError, TypeError):
            if self.pr is None or self.base is None:
                raise CodeHostError('GitLab could not establish exact repository, MR and target scope') from None
            self.source(Source.PULL_REQUEST, Availability.UNKNOWN, 'read_did_not_complete')
        await self.guard_final()
        return self.result()

    async def guard_final(self):
        try:
            await self.guard()
        except _GuardRejected as error:
            raise error.error from None

    def unavailable(self, source, error):
        if isinstance(error, GitLabAPIError):
            availability = (Availability.DENIED if error.status_code in {401, 403} else
                Availability.UNSUPPORTED if error.status_code in {404, 405, 410, 501} else Availability.UNKNOWN)
            self.source(source, availability, f'http_{error.status_code}')
        else:
            reason = getattr(error, 'reason', 'provider_unavailable')
            self.source(source, Availability.TRUNCATED if reason in {'byte_limit', 'request_limit', 'approval_list_incomplete'}
                else Availability.UNKNOWN, reason)

    def result(self):
        checks = ()
        raw = self.pr['pipeline']
        try:
            pipeline = obj(raw)
            sha = revision(pipeline.get('sha'))
            if sha != self.pr['head']:
                raise GitLabReadIncompleteError('pipeline_head_unproven')
            identifier = native_id(pipeline.get('id'))
            state = text(pipeline.get('status'))
            checks = (CodeHostMergeCheckFact(external_id=str(identifier), context='GitLab head pipeline',
                revision=sha, kind='pipeline', provider_state=state,
                state='success' if state == 'success' else 'failure' if state in {'failed', 'canceled'} else
                    'pending' if state in {'created', 'waiting_for_resource', 'preparing', 'pending', 'running', 'scheduled'} else 'unknown'),)
            self.source(Source.CHECKS, Availability.COMPLETE, records=1)
        except GitLabReadIncompleteError as error:
            self.source(Source.CHECKS, Availability.UNKNOWN, error.reason)
        for source in Source:
            if source not in self.sources:
                self.source(source, Availability.UNKNOWN, 'not_observed')
        approval = self.approvals
        blockers = [f'{s.source.value}:{s.availability.value}' for s in self.sources.values()
                    if s.availability != Availability.COMPLETE]
        if self.changed:
            blockers.append('observation_changed')
        if not self.started <= float(self.clock()) < self.started + self.observation_seconds:
            blockers.append('observation_stale')
        if self.pr['state'] != 'opened' or self.pr['draft']:
            blockers.append('pull_request_not_open_ready')
        if self.pr['gate'] != 'mergeable':
            blockers.append('provider_merge_gate_not_clean')
        review_required = self.pr['gate'] == 'not_approved' or (approval is not None and approval['left'] is not None and approval['left'] > 0)
        if review_required:
            blockers.append('provider_review_gate_not_approved')
        if self.pr['discussions'] is False:
            blockers.append('unresolved_conversations')
        independent = None if approval is None else sum(user != self.pr['author'] for user in approval['users'])
        result = CodeHostMergeRequirementsFact(resource_id=self.resource.id, binding_id=self.binding.id,
            provider_type='gitlab', provider_instance=self.binding.provider_instance, external_id=str(self.number),
            head_revision=self.pr['head'], base_revision=self.base, target_branch=self.pr['branch'],
            observed_at=self.started, expires_at=self.started + self.observation_seconds, sources=tuple(self.sources.values()),
            complete=False, changed=self.changed, pull_request_state=self.pr['state'].upper(), draft=self.pr['draft'],
            mergeable='MERGEABLE' if self.pr['gate'] == 'mergeable' else 'UNKNOWN', merge_state=self.pr['gate'],
            review_decision=None if approval is None else 'APPROVED' if approval['approved'] else
                'REVIEW_REQUIRED' if review_required else 'NOT_APPROVED',
            reviews=CodeHostMergeReviewRequirements(approving_count=approval['required'] if approval else None),
            independent_approval_count=independent,
            # GitLab approved_by does not attribute approvals to a commit SHA.
            current_head_approval_count=0 if approval is not None and not approval['users'] else None,
            observed_checks=checks, blockers=tuple(blockers))
        if len(result.model_dump_json().encode()) > 128 * 1024:
            raise CodeHostError('GitLab merge-requirement projection exceeds its byte limit')
        return result
