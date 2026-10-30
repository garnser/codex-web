"""Bounded, read-only GitHub observations for the CodeHost facade."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import re
import time
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from codex_web.code_host_merge_requirements import (
    CodeHostMergeCheckFact, CodeHostMergeRequirementsFact, CodeHostMergeReviewRequirements, CodeHostMergeSourceFact,
    MergeFactAvailability as Availability, MergeFactSource as Source,
)
from codex_web.code_hosts import CodeHostError
from codex_web.integrations.github_client import GitHubAPIError, GitHubReadIncompleteError
from codex_web.services.github_merge_policy import array, boolean, count, native_id, normalize_policy, object_, rule_source, text


_IDENTITY = "id number headRefOid baseRefOid baseRefName"
_GATES = """state isDraft mergeable mergeStateStatus reviewDecision
baseRef { name branchProtectionRule { id requiresApprovingReviews
requiredApprovingReviewCount requiresCodeOwnerReviews requireLastPushApproval
dismissesStaleReviews requiresConversationResolution requiresStatusChecks requiresStrictStatusChecks
requiresCommitSignatures requiresDeployments requiresLinearHistory restrictsPushes lockBranch isAdminEnforced } }"""


def _query(operation, fields, *, cursor=False):
    args = ", $cursor: String" if cursor else ""
    return (f"query {operation}($owner: String!, $name: String!, $number: Int!{args}) {{"
            f" repository(owner: $owner, name: $name) {{ databaseId pullRequest(number: $number) {{"
            f" {_IDENTITY} {fields} }} }} }}")


_GATE_QUERY = _query("MergeRequirementsGates", _GATES)
_REVIEW_QUERY = _query("MergeRequirementsReviews", """reviews(first: 100, after: $cursor) {
totalCount nodes { id state submittedAt author { login ... on Node { id } } commit { oid } }
pageInfo { hasNextPage endCursor } }""", cursor=True)
_THREAD_QUERY = _query("MergeRequirementsThreads", """reviewThreads(first: 100, after: $cursor) {
totalCount nodes { id isResolved } pageInfo { hasNextPage endCursor } }""", cursor=True)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise GitHubReadIncompleteError("revision")
    return value


class _Changed(GitHubReadIncompleteError):
    pass


class _GuardRejected(Exception):
    def __init__(self, error):
        self.error = error
        super().__init__("canonical read guard rejected")


class GitHubMergeRequirementsReader:
    """One expiring observation; no saved controller, polling or mutation."""

    MAX_PROJECTION_BYTES = 128 * 1024

    def __init__(self, client, binding, resource, repository, credential, *, clock=time.time, checkpoint=None,
                 observation_seconds=30, execution_budget=None):
        if type(observation_seconds) is not int or not 1 <= observation_seconds <= 300:
            raise ValueError('invalid observation validity')
        self.observation_seconds = observation_seconds
        self.execution_budget = execution_budget
        if not credential:
            raise CodeHostError("authenticated merge-requirement reads require a credential reference")
        if (not isinstance(repository, str) or len(repository.split('/')) != 2
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", item) or item in {'.', '..'}
                       for item in repository.split('/'))):
            raise CodeHostError("GitHub merge-requirement repository locator is invalid")
        self.client, self.binding, self.resource = client, binding, resource
        self.repository, self.credential, self.clock = repository, credential, clock
        self.root = f"repos/{repository}"
        parts = urlsplit(binding.base_url.rstrip('/'))
        if parts.path in {'', '/'}:
            graphql_path = '/graphql'
        elif parts.path == '/api/v3':
            graphql_path = '/api/graphql'
        else:
            raise CodeHostError("configured GitHub API base does not identify a supported GraphQL route")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise CodeHostError("configured GitHub API base is invalid")
        self.graphql_base = urlunsplit((parts.scheme, parts.netloc, '', '', ''))
        self.graphql_path = graphql_path
        self.calls = 0
        self.sources = {}
        self.changed = False
        self.pr = None
        self.legacy_rule_ids, self.unsupported_legacy = set(), set()
        self.admin_enforced, self.initial_gate_rule = None, None
        self.used = False
        self.checkpoint = checkpoint

    def _source(self, source, availability, reason=None, records=None):
        previous = self.sources.get(source)
        self.sources[source] = CodeHostMergeSourceFact(source=source, availability=availability,
            provider_source=f"github:{source.value}", observed_at=float(self.clock()),
            reason_code=reason, record_count=records, rule_ids=previous.rule_ids if previous else ())

    async def _json(self, path, *, params=None, query=None):
        async def guard():
            try:
                if self.execution_budget is not None:
                    self.execution_budget.require_current()
                if self.checkpoint is not None:
                    await self.checkpoint()
                if self.execution_budget is not None:
                    self.execution_budget.require_current()
            except Exception as exc:
                raise _GuardRejected(exc) from None
        await guard()
        self.calls += 1
        if self.calls > 40:
            raise GitHubReadIncompleteError("request_limit")
        try:
            request = self.client.read_json_bounded(
                self.graphql_base if query is not None else self.binding.base_url,
                self.graphql_path if query is not None else path, token=self.credential,
                params=params, query_body=query)
            result = await request if self.execution_budget is None else await self.execution_budget.request(request)
        except Exception:
            await guard()
            raise
        await guard()
        return result

    async def _facet(self, source, operation):
        try:
            result = await operation()
        except _Changed as exc:
            self.changed = True
            self._source(source, Availability.CHANGED, exc.reason)
        except GitHubAPIError as exc:
            status = Availability.DENIED if exc.status_code in {401, 403} else (
                Availability.UNSUPPORTED if exc.status_code in {405, 410, 501} else Availability.UNKNOWN)
            self._source(source, status, f"http_{exc.status_code}")
        except GitHubReadIncompleteError as exc:
            status = Availability.TRUNCATED if exc.reason in {'byte_limit', 'page_limit', 'request_limit'} else Availability.UNKNOWN
            self._source(source, status, exc.reason)
        except (ValueError, TypeError, KeyError, httpx.HTTPError):
            self._source(source, Availability.UNKNOWN, "invalid_or_unavailable_provider_data")
        else:
            self._source(source, Availability.COMPLETE, records=len(result) if isinstance(result, list) else None)
            return result
        return None

    def _pr(self, raw):
        raw = object_(raw)
        if (type(raw.get('number')) is not int or raw['number'] != self.number
                or native_id(object_(object_(raw.get('base')).get('repo')).get('id')) != self.repository_id):
            raise GitHubReadIncompleteError("pull_request_identity")
        web_url = raw.get('html_url')
        try:
            parsed = urlsplit(web_url) if isinstance(web_url, str) and len(web_url) <= 2048 else None
            if (parsed is None or parsed.scheme != 'https' or parsed.netloc != self.binding.provider_instance
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path != f'/{self.repository}/pull/{self.number}'):
                web_url = None
        except ValueError:
            web_url = None
        return dict(id=text(raw.get('node_id')), number=self.number,
            web_url=web_url,
            head=_revision(object_(raw.get('head')).get('sha')),
            base=_revision(raw['base'].get('sha')), branch=text(raw['base'].get('ref')),
            state=text(raw.get('state')).upper(), draft=boolean(raw.get('draft')),
            author=text(object_(raw.get('user')).get('login')),
            author_id=text(raw['user'].get('node_id')))

    def _identity(self, raw):
        if type(raw.get('number')) is not int or (raw.get('id'), raw.get('number'), raw.get('headRefOid'), raw.get('baseRefOid'), raw.get('baseRefName')) != (
                self.pr['id'], self.number, self.pr['head'], self.pr['base'], self.pr['branch']):
            raise _Changed("pull_request_changed")

    async def _graphql(self, query, cursor=None):
        owner, name = self.repository.split('/')
        variables = dict(owner=owner, name=name, number=self.number)
        if query != _GATE_QUERY:
            variables['cursor'] = cursor
        raw = object_(await self._json('', query={'query': query, 'variables': variables}))
        if raw.get('errors'):
            raise GitHubReadIncompleteError("graphql_errors")
        repository = object_(object_(raw.get('data')).get('repository'))
        if type(repository.get('databaseId')) is not int or repository['databaseId'] != self.repository_id:
            raise GitHubReadIncompleteError("repository_identity")
        pr = object_(repository.get('pullRequest'))
        self._identity(pr)
        return pr

    async def _gates(self):
        pr = await self._graphql(_GATE_QUERY)
        for field in ('state', 'mergeable', 'mergeStateStatus'):
            text(pr.get(field))
        if pr['state'] != self.pr['state'] or boolean(pr.get('isDraft')) != self.pr['draft']:
            raise _Changed("pull_request_state_changed")
        base = object_(pr.get('baseRef'))
        if base.get('name') != self.pr['branch'] or 'branchProtectionRule' not in base:
            raise GitHubReadIncompleteError("base_ref")
        if pr.get('reviewDecision') is not None:
            text(pr['reviewDecision'])
        return pr

    async def _pages(self, path, *, key=None, total=False, extra=None):
        values, expected, identities = [], None, set()
        for page in range(1, 6):
            raw = await self._json(path, params={'per_page': 100, 'page': page, **(extra or {})})
            if key is not None:
                raw = object_(raw)
                if 'sha' in raw and raw['sha'] != self.pr['head']:
                    raise _Changed("check_revision_changed")
                if total:
                    current_total = count(raw.get('total_count'))
                    if current_total > 500:
                        raise GitHubReadIncompleteError("page_limit")
                    if expected is not None and expected != current_total:
                        raise _Changed("page_count_changed")
                    expected = current_total
                rows = array(raw.get(key))
                if key == 'statuses' and _revision(raw.get('sha')) != self.pr['head']:
                    raise _Changed("check_revision_changed")
            else:
                rows = array(raw)
            if len(rows) > 100:
                raise GitHubReadIncompleteError("page_size")
            for row in rows:
                object_(row)
                identity = _digest(row) if key is None else row.get('id')
                if identity is None:
                    raise GitHubReadIncompleteError("missing_page_record")
                if str(identity) in identities:
                    raise _Changed("duplicate_page_record")
                identities.add(str(identity))
            values.extend(rows)
            if len(rows) < 100:
                if expected is not None and expected != len(values):
                    raise GitHubReadIncompleteError("page_count_incomplete")
                return values
        raise GitHubReadIncompleteError("page_limit")

    async def _connection(self, query, field):
        values, seen, cursor, cursors, expected = [], set(), None, set(), None
        for _ in range(5):
            pr = await self._graphql(query, cursor)
            connection = object_(pr.get(field))
            current_total = count(connection.get('totalCount'))
            if current_total > 500:
                raise GitHubReadIncompleteError("page_limit")
            if expected is not None and current_total != expected:
                raise _Changed("connection_count_changed")
            expected = current_total
            rows = array(connection.get('nodes'))
            if len(rows) > 100:
                raise GitHubReadIncompleteError("page_size")
            for row in rows:
                object_(row)
                identity = text(row.get('id'))
                if identity in seen:
                    raise _Changed("duplicate_page_record")
                seen.add(identity)
            values.extend(rows)
            info = object_(connection.get('pageInfo'))
            if not boolean(info.get('hasNextPage')):
                if len(values) != expected:
                    raise GitHubReadIncompleteError("connection_incomplete")
                return values
            next_cursor = text(info.get('endCursor'))
            if not rows or next_cursor in cursors:
                raise GitHubReadIncompleteError("cursor_incomplete")
            cursors.add(next_cursor)
            cursor = next_cursor
        raise GitHubReadIncompleteError("page_limit")

    async def _checks(self):
        runs = await self._pages(f"{self.root}/commits/{self.pr['head']}/check-runs", key='check_runs',
                                 total=True, extra={'filter': 'latest'})
        statuses = await self._pages(f"{self.root}/commits/{self.pr['head']}/status", key='statuses', total=True)
        result = []
        for kind, rows in [('check', runs), ('status', statuses)]:
            for row in rows:
                application = None
                if kind == 'check':
                    if row.get('head_sha') != self.pr['head']:
                        raise _Changed("check_revision_changed")
                    application = object_(row.get('app')).get('id')
                    if type(application) is not int or application <= 0:
                        raise GitHubReadIncompleteError("check_app")
                    native = text(row.get('status'))
                    conclusion = row.get('conclusion')
                    if native in {'queued', 'in_progress', 'waiting', 'requested', 'pending'}:
                        state = 'pending'
                    elif native == 'completed' and conclusion in {'success', 'neutral', 'skipped'}:
                        state = 'success'
                    elif native == 'completed' and conclusion in {'failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure', 'stale'}:
                        state = 'failure'
                    else:
                        state = 'unknown'
                    context, timestamp = row.get('name'), row.get('completed_at') or row.get('started_at')
                else:
                    native = text(row.get('state'))
                    state = {'success': 'success', 'failure': 'failure', 'error': 'failure', 'pending': 'pending'}.get(native, 'unknown')
                    context, timestamp = row.get('context'), row.get('updated_at')
                when = None
                if timestamp is not None:
                    try:
                        date = datetime.fromisoformat(text(timestamp).replace('Z', '+00:00'))
                        if date.tzinfo is None:
                            raise ValueError("timezone missing")
                        when = date.timestamp()
                    except ValueError:
                        raise GitHubReadIncompleteError("check_time") from None
                result.append(CodeHostMergeCheckFact(external_id=str(native_id(row.get('id'))), context=text(context),
                    revision=self.pr['head'], kind=kind, state=state, provider_state=native,
                    provider_conclusion=conclusion if kind == 'check' else None,
                    provider_app_id=str(application) if application else None, publisher_known=application is not None,
                    observed_at=when, web_url=row.get('html_url') or row.get('target_url')))
        return result

    async def _legacy(self):
        try:
            return object_(await self._json(f"{self.root}/branches/{quote(self.pr['branch'], safe='')}/protection"))
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return None if exc.provider_reason == 'branch_not_protected' else False
            raise

    def _confirm_legacy(self, legacy, gates):
        if legacy is None and self.sources[Source.LEGACY_PROTECTION].availability == Availability.COMPLETE:
            if gates is not None and gates['baseRef']['branchProtectionRule'] is None:
                self._source(Source.LEGACY_PROTECTION, Availability.ABSENT, "branch_not_protected_and_graphql_no_rule", 0)
                return None
            self._source(Source.LEGACY_PROTECTION, Availability.UNKNOWN, "legacy_absence_unconfirmed")
        elif legacy is False:
            self._source(Source.LEGACY_PROTECTION, Availability.UNKNOWN, "legacy_absence_unconfirmed")
        elif isinstance(legacy, dict):
            try:
                if gates is None:
                    raise GitHubReadIncompleteError("legacy_confirmation_unavailable")
                rule = gates['baseRef']['branchProtectionRule']
                if rule is None:
                    raise _Changed("legacy_sources_disagree")
                rule = object_(rule)
                self.legacy_rule_ids.add(text(rule.get('id')))
                self.admin_enforced = boolean(rule.get('isAdminEnforced'))
                for field in ('requiresCommitSignatures', 'requiresDeployments', 'requiresLinearHistory', 'restrictsPushes', 'lockBranch'):
                    if boolean(rule.get(field)):
                        self.unsupported_legacy.add(f"legacy.{field}")
                reviews, checks = legacy.get('required_pull_request_reviews'), legacy.get('required_status_checks')
                pairs = [(boolean(rule.get('requiresApprovingReviews')), reviews is not None),
                         (boolean(rule.get('requiresStatusChecks')), checks is not None),
                         (boolean(rule.get('requiresConversationResolution')),
                          boolean(object_(legacy.get('required_conversation_resolution', {'enabled': False})).get('enabled')))]
                if legacy.get('enforce_admins') is not None:
                    pairs.append((self.admin_enforced, boolean(object_(legacy['enforce_admins']).get('enabled'))))
                if reviews is not None:
                    object_(reviews)
                    pairs.extend([(count(rule.get('requiredApprovingReviewCount')), count(reviews.get('required_approving_review_count'))),
                        (boolean(rule.get('requiresCodeOwnerReviews')), boolean(reviews.get('require_code_owner_reviews'))),
                        (boolean(rule.get('requireLastPushApproval')), boolean(reviews.get('require_last_push_approval'))),
                        (boolean(rule.get('dismissesStaleReviews')), boolean(reviews.get('dismiss_stale_reviews')))])
                if checks is not None:
                    pairs.append((boolean(rule.get('requiresStrictStatusChecks')), boolean(object_(checks).get('strict'))))
                if any(left != right for left, right in pairs):
                    raise _Changed("legacy_sources_disagree")
            except _Changed as exc:
                self.changed = True
                self._source(Source.LEGACY_PROTECTION, Availability.CHANGED, exc.reason)
            except (GitHubReadIncompleteError, KeyError):
                self._source(Source.LEGACY_PROTECTION, Availability.UNKNOWN, "legacy_confirmation_incomplete")
        return legacy

    async def _observe(self):
        gates = await self._facet(Source.PULL_REQUEST, self._gates)
        legacy = await self._facet(Source.LEGACY_PROTECTION, self._legacy)
        rules = await self._facet(Source.EFFECTIVE_RULES, lambda: self._pages(
            f"{self.root}/rules/branches/{quote(self.pr['branch'], safe='')}"))
        legacy = self._confirm_legacy(legacy, gates)
        self.legacy, self.rules, self.gates = legacy, rules, gates
        self.initial_gate_rule = gates['baseRef']['branchProtectionRule'] if gates is not None else None
        before_sources = dict(self.sources)
        self.checks = await self._facet(Source.CHECKS, self._checks)
        self.reviews = await self._facet(Source.REVIEWS, lambda: self._connection(_REVIEW_QUERY, 'reviews'))
        self.threads = await self._facet(Source.CONVERSATIONS, lambda: self._connection(_THREAD_QUERY, 'reviewThreads'))
        # Re-read both policy sources, not merely PR commits. An unrelated policy
        # edit invalidates an otherwise unchanged head/base observation.
        after_legacy = await self._facet(Source.LEGACY_PROTECTION, self._legacy)
        after_rules = await self._facet(Source.EFFECTIVE_RULES, lambda: self._pages(
            f"{self.root}/rules/branches/{quote(self.pr['branch'], safe='')}"))
        after_gates = await self._facet(Source.PULL_REQUEST, self._gates)
        after_legacy = self._confirm_legacy(after_legacy, after_gates)
        after_pr = self._pr(await self._json(f"{self.root}/pulls/{self.number}"))
        if after_pr != self.pr:
            raise _Changed("pull_request_changed")
        for source, before, after in [(Source.LEGACY_PROTECTION, legacy, after_legacy),
                                      (Source.EFFECTIVE_RULES, rules, after_rules)]:
            known = {Availability.COMPLETE, Availability.ABSENT}
            if before_sources[source].availability not in known:
                self.sources[source] = before_sources[source]
            elif self.sources[source].availability in known and (
                    sorted(before, key=_digest) != sorted(after, key=_digest) if source == Source.EFFECTIVE_RULES
                    else before != after):
                self.changed = True
                self._source(source, Availability.CHANGED, "policy_changed")
        if gates is None or after_gates is None:
            if before_sources[Source.PULL_REQUEST].availability != Availability.COMPLETE:
                self.sources[Source.PULL_REQUEST] = before_sources[Source.PULL_REQUEST]
            elif self.sources[Source.PULL_REQUEST].availability == Availability.COMPLETE:
                self._source(Source.PULL_REQUEST, Availability.UNKNOWN, "gate_observation_incomplete")
        elif gates['baseRef']['branchProtectionRule'] != after_gates['baseRef']['branchProtectionRule']:
            self.changed = True
            self._source(Source.LEGACY_PROTECTION, Availability.CHANGED, "graphql_policy_changed")
        if after_gates is not None:
            self.gates = after_gates

    async def read(self, external_id):
        if self.execution_budget is None:
            return await self._read_once(external_id)
        async with asyncio.timeout(self.execution_budget.remaining_seconds()):
            result = await self._read_once(external_id)
            self.execution_budget.require_current()
            return result

    async def _read_once(self, external_id):
        if self.used:
            raise CodeHostError("A merge-requirement reader owns exactly one observation")
        self.used = True
        if not re.fullmatch(r"[1-9][0-9]{0,9}", str(external_id)):
            raise CodeHostError("merge requirements require an exact positive pull-request number")
        if int(external_id) > 2 ** 31 - 1:
            raise CodeHostError("pull-request number exceeds the provider query range")
        self.number, self.started = int(external_id), float(self.clock())
        self.legacy = self.rules = self.gates = self.checks = self.reviews = self.threads = None
        try:
            async with (asyncio.timeout(30) if self.execution_budget is None else nullcontext()):
                repository = object_(await self._json(self.root))
                self.repository_id = native_id(repository.get('id'))
                if not self.repository_id or str(repository.get('full_name', '')).casefold() != self.repository.casefold():
                    raise GitHubReadIncompleteError("repository_identity")
                external = self.resource.provenance.external_id if self.resource.provenance else None
                if external and external.isdigit() and str(self.repository_id) != external:
                    raise GitHubReadIncompleteError("repository_identity")
                self.pr = self._pr(await self._json(f"{self.root}/pulls/{self.number}"))
                await self._observe()
        except _GuardRejected as exc:
            raise exc.error from None
        except _Changed as exc:
            self.changed = True
            self._source(Source.PULL_REQUEST, Availability.CHANGED, exc.reason)
        except GitHubAPIError as exc:
            if self.pr is None:
                error = CodeHostError(f"GitHub could not establish repository/PR scope (HTTP {exc.status_code})")
                error.status_code = exc.status_code
                raise error from None
            self._source(Source.PULL_REQUEST, Availability.DENIED if exc.status_code in {401, 403}
                         else Availability.UNKNOWN, f"http_{exc.status_code}")
        except (GitHubReadIncompleteError, httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError):
            if self.pr is None:
                raise CodeHostError("GitHub could not establish the exact repository and pull-request scope") from None
            self._source(Source.PULL_REQUEST, Availability.UNKNOWN, "read_did_not_complete")
        if self.execution_budget is not None:
            self.execution_budget.require_current()
        if self.checkpoint is not None:
            await self.checkpoint()
        if self.execution_budget is not None:
            self.execution_budget.require_current()
        try:
            result = self._result()
        except (ValueError, TypeError, KeyError):
            # Never expose native validation payloads through a credential
            # consumer's exception; they can contain reflected provider text.
            raise CodeHostError("GitHub merge-requirement projection is invalid") from None
        if len(result.model_dump_json().encode('utf-8')) > self.MAX_PROJECTION_BYTES:
            result = result.model_copy(update={
                'complete': False, 'required_checks': (), 'observed_checks': (),
                'strict_checks_required': None, 'reviews': CodeHostMergeReviewRequirements(),
                'independent_approval_count': None, 'current_head_approval_count': None,
                'conversation_resolution_required': None, 'conversation_count': None,
                'unresolved_conversation_count': None, 'unsupported_rules': (),
                'blockers': ('projection_byte_limit',),
                'sources': tuple(item.model_copy(update={'availability': Availability.TRUNCATED,
                    'reason_code': 'projection_byte_limit', 'rule_ids': ()}) for item in result.sources),
            })
        return result

    def _result(self):
        for source in Source:
            if source not in self.sources:
                self._source(source, Availability.UNKNOWN, "not_observed")
        known = {Availability.COMPLETE, Availability.ABSENT}
        policy_known = all(self.sources[source].availability in known for source in
                           (Source.LEGACY_PROTECTION, Source.EFFECTIVE_RULES))
        try:
            policy = normalize_policy(self.legacy if isinstance(self.legacy, dict) else None,
                                      self.rules or [], legacy_known=policy_known)
            policy = replace(policy, unsupported=tuple(sorted(set(policy.unsupported) | self.unsupported_legacy)))
            if len(policy.unsupported) > 100:
                raise GitHubReadIncompleteError("policy_limit")
            if self.rules is not None:
                self.sources[Source.EFFECTIVE_RULES] = self.sources[Source.EFFECTIVE_RULES].model_copy(update={
                    'rule_ids': tuple(sorted({rule_source(rule) for rule in self.rules}))})
        except (GitHubReadIncompleteError, ValueError, TypeError):
            self._source(Source.EFFECTIVE_RULES, Availability.UNKNOWN, "policy_schema_incomplete")
            policy = normalize_policy(None, [], legacy_known=False)
            policy_known = False
        blockers = []
        self.sources[Source.LEGACY_PROTECTION] = self.sources[Source.LEGACY_PROTECTION].model_copy(
            update={'rule_ids': tuple(sorted(self.legacy_rule_ids))})
        if policy.unsupported:
            for source, present in [(Source.LEGACY_PROTECTION, any(item.startswith('legacy.') for item in policy.unsupported)),
                                    (Source.EFFECTIVE_RULES, any(not item.startswith('legacy.') for item in policy.unsupported))]:
                if present and self.sources[source].availability in known:
                    self._source(source, Availability.UNSUPPORTED, "uninterpreted_policy_rules")
        approvals = current_approvals = unresolved = None
        try:
            if self.reviews is not None:
                opinions = {}
                for review in self.reviews:
                    state = text(review.get('state'))
                    if state not in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED', 'COMMENTED', 'PENDING'}:
                        raise GitHubReadIncompleteError("review_state")
                    if state in {'PENDING', 'COMMENTED'}:
                        continue
                    author = text(object_(review.get('author')).get('id'))
                    submitted = text(review.get('submittedAt'))
                    if state in {'APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'}:
                        date = datetime.fromisoformat(submitted.replace('Z', '+00:00'))
                        if date.tzinfo is None:
                            raise GitHubReadIncompleteError("review_time")
                        stamp = date.timestamp()
                        commit = _revision(object_(review.get('commit')).get('oid'))
                        if author in opinions and stamp == opinions[author][0] and (state, commit) != opinions[author][1:]:
                            raise GitHubReadIncompleteError("ambiguous_latest_review")
                        if author not in opinions or stamp > opinions[author][0]:
                            opinions[author] = (stamp, state, commit)
                independent = [item for author, item in opinions.items() if author != self.pr['author_id'] and item[1] == 'APPROVED']
                approvals = len(independent)
                current_approvals = sum(item[2] == self.pr['head'] for item in independent)
        except (GitHubReadIncompleteError, ValueError, TypeError):
            approvals = current_approvals = None
            self._source(Source.REVIEWS, Availability.UNKNOWN, "review_schema")
        try:
            if self.threads is not None:
                unresolved = sum(not boolean(item.get('isResolved')) for item in self.threads)
        except GitHubReadIncompleteError:
            self._source(Source.CONVERSATIONS, Availability.UNKNOWN, "conversation_schema")
        gates = self.gates or {}
        for source in Source:
            if self.sources[source].availability not in known:
                blockers.append(f"{source.value}:{self.sources[source].availability.value}")
        if self.changed:
            blockers.append("observation_changed")
        if float(self.clock()) >= self.started + self.observation_seconds:
            blockers.append("observation_stale")
        if float(self.clock()) < self.started:
            blockers.append("observation_clock_changed")
        if self.pr['state'] != 'OPEN' or self.pr['draft']:
            blockers.append("pull_request_not_open_ready")
        if gates.get('mergeable') != 'MERGEABLE' or gates.get('mergeStateStatus') != 'CLEAN':
            blockers.append("provider_merge_gate_not_clean")
        if (policy.reviews.approving_count is not None and approvals is not None
                and approvals < policy.reviews.approving_count):
            blockers.append("independent_approvals_missing")
        if (gates.get('reviewDecision') not in {None, 'APPROVED'}
                or (any((policy.reviews.approving_count, policy.reviews.code_owner_required, policy.reviews.last_push_approval_required))
                    and gates.get('reviewDecision') != 'APPROVED')):
            blockers.append("provider_review_gate_not_approved")
        if policy.conversations and unresolved != 0:
            blockers.append("unresolved_conversations")
        for required in policy.checks:
            observations = [item for item in (self.checks or []) if item.context == required.context]
            matching = [item for item in observations if required.provider_app_id is None
                        or item.provider_app_id == required.provider_app_id]
            if (not matching or any(item.state != 'success' for item in matching)
                    or len({(item.kind, item.provider_app_id) for item in matching}) != len(matching)
                    or (required.provider_app_id is not None and any(not item.publisher_known for item in observations))):
                blockers.append("required_checks_unmet_or_ambiguous")
        complete = (all(item.availability in known for item in self.sources.values())
                    and not self.changed and not policy.unsupported and approvals is not None and unresolved is not None
                    and self.started <= float(self.clock()) < self.started + self.observation_seconds)
        return CodeHostMergeRequirementsFact(resource_id=self.resource.id, binding_id=self.binding.id,
            provider_type='github', provider_instance=self.binding.provider_instance, external_id=str(self.number),
            web_url=self.pr.get('web_url'),
            head_revision=self.pr['head'], base_revision=self.pr['base'], target_branch=self.pr['branch'],
            observed_at=self.started, expires_at=self.started + self.observation_seconds,
            requirements_fingerprint=_digest({'legacy': self.legacy, 'legacy_graphql': self.initial_gate_rule,
                'rules': sorted(self.rules, key=_digest)}) if policy_known else None,
            sources=tuple(self.sources[source] for source in Source), complete=complete, changed=self.changed,
            pull_request_state=self.pr['state'], draft=self.pr['draft'], mergeable=gates.get('mergeable'),
            merge_state=gates.get('mergeStateStatus'), review_decision=gates.get('reviewDecision'),
            required_checks=policy.checks, observed_checks=tuple(self.checks or []), strict_checks_required=policy.strict,
            legacy_admin_enforced=self.admin_enforced if self.sources[Source.LEGACY_PROTECTION].availability == Availability.COMPLETE else None,
            reviews=policy.reviews, independent_approval_count=approvals, current_head_approval_count=current_approvals,
            conversation_resolution_required=policy.conversations, conversation_count=len(self.threads) if self.threads is not None else None,
            unresolved_conversation_count=unresolved, unsupported_rules=policy.unsupported,
            blockers=tuple(dict.fromkeys(blockers)))
