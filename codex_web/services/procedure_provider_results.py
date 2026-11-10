"""Pure, explicitly registered projections of existing provider receipts.

These adapters neither execute nor verify an action. The procedure owner creates
canonical Evidence from the original receipt and, separately, its verification.
"""
from __future__ import annotations

import re

from codex_web.services.code_host_ci_observation import (
    CI_OBSERVATION_ACTION_ID, CI_OBSERVATION_EVIDENCE, project_observation, parameters as ci_parameters,
)

from codex_web.services.code_host_action_contract import (
    READINESS_OBSERVATION_ACTION_IDS,
    CODE_HOST_BRANCH_PUBLISH_ACTION_ID, CODE_HOST_BRANCH_EVIDENCE,
    CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_EVIDENCE,
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE,
    CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
    CODE_HOST_PULL_REQUEST_READY_EVIDENCE,
    CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_VERIFY_MERGE_EVIDENCE,
    CodeHostActionContract,
)


class CodeHostProcedureResultAdapter:
    provider_type = "gitlab"
    # Match the existing producer and immutable positive-only procedure references.
    contract_version = "1.2"
    action_ids = (CODE_HOST_BRANCH_PUBLISH_ACTION_ID, CI_OBSERVATION_ACTION_ID, CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID, CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
                  CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID, *READINESS_OBSERVATION_ACTION_IDS)

    def __init__(self, *, provider_instance=None):
        self.provider_instance = provider_instance

    def project(self, request, result):
        """Return only the supported, typed output fields; preserve legacy output."""
        if (result.status != "succeeded" or result.action_id != request.action_id
            or request.action_id not in self.action_ids or len(request.resource_ids) != 1
            or result.output.get("resource_id") != request.resource_ids[0]
            or result.idempotency_key != request.idempotency_key):
            raise ValueError("procedure_code_host_result_identity_mismatch")
        raw = result.output
        if request.action_id == CODE_HOST_BRANCH_PUBLISH_ACTION_ID:
            return self._published_branch(request, result)
        number = raw.get("pull_request_number")
        if type(number) is not int or number < 1 or str(number) != result.external_id:
            raise ValueError("procedure_code_host_result_number_invalid")
        if request.action_id == CI_OBSERVATION_ACTION_ID:
            parameters = ci_parameters(request).model_dump(mode="json")
            outputs = project_observation(raw, request=request, binding_id=result.provider_binding_id,
                provider_instance=self.provider_instance)
            evidence_type = CI_OBSERVATION_EVIDENCE
        elif request.action_id in READINESS_OBSERVATION_ACTION_IDS:
            from codex_web.services.code_host_readiness_observation import observation_outputs
            parameters = CodeHostActionContract.pull_request_observe_readiness(request)
            outputs = observation_outputs(raw, binding_id=result.provider_binding_id,
                resource_id=request.resource_ids[0], provider_instance=self.provider_instance,
                parameters=parameters, observed_at=result.completed_at)
            evidence_type = CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_EVIDENCE
        elif request.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
            parameters = CodeHostActionContract.pull_request_merge(request)
            fields = ("head_sha", "merge_commit_sha")
            outputs = {"pull_request_number": number}
            for field in fields:
                value = raw.get(field)
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
                    raise ValueError("procedure_code_host_revision_invalid")
                outputs[field] = value
            if parameters["expected_head_sha"] is None or outputs["head_sha"] != parameters["expected_head_sha"]:
                raise ValueError("procedure_code_host_observed_head_mismatch")
            evidence_type = CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE
        elif request.action_id == CODE_HOST_PULL_REQUEST_VERIFY_MERGE_ACTION_ID:
            parameters = CodeHostActionContract.pull_request_verify_merge(request)
            if raw.get("merge_verified") is not True or any(raw.get(k) != v for k,v in parameters.items()):
                raise ValueError("procedure_code_host_merge_observation_invalid")
            outputs = {**parameters,"merge_verified":True}
            evidence_type = CODE_HOST_PULL_REQUEST_VERIFY_MERGE_EVIDENCE
        else:
            parameters = CodeHostActionContract.pull_request_ready(request)
            if (raw.get("draft") is not False
                or any(raw.get(k) != v for k, v in parameters.items())
                or type(raw.get("provider_mutation_performed")) is not bool
                or not re.fullmatch(r"[0-9a-f]{64}", str(raw.get("metadata_sha256", "")))):
                raise ValueError("procedure_code_host_readiness_result_invalid")
            outputs = {**parameters, "draft": False}
            evidence_type = CODE_HOST_PULL_REQUEST_READY_EVIDENCE
        if number != parameters.get("number", parameters.get("pull_request_number")):
            raise ValueError("procedure_code_host_request_number_mismatch")
        if not any(e.evidence_type == evidence_type
                   and e.metadata.get("resource_id") == request.resource_ids[0]
                   for e in result.evidence):
            raise ValueError("procedure_code_host_receipt_evidence_missing")
        return outputs

    @staticmethod
    def _published_branch(request, result):
        """Project the original publication receipt, never re-attest or push."""
        parameters, raw = request.parameters, result.output
        if set(parameters) - {"execution_workspace_id", "branch", "head_revision",
                "expected_remote_revision", "expected_remote_absent", "existing_change_request_number"}:
            raise ValueError("procedure_branch_request_invalid")
        workspace = str(parameters.get("execution_workspace_id") or "").strip()
        branch = CodeHostActionContract.branch(parameters.get("branch"), field="branch")
        head = str(parameters.get("head_revision") or "").strip()
        absent = parameters.get("expected_remote_absent", False)
        remote = str(parameters.get("expected_remote_revision") or "").strip()
        number = parameters.get("existing_change_request_number")
        if number is not None and (type(number) is not int or number < 1 or not remote):
            raise ValueError("procedure_branch_request_invalid")
        if (not workspace or re.fullmatch(r"[0-9a-f]{40,64}", head) is None
                or type(absent) is not bool
                or remote and re.fullmatch(r"[0-9a-f]{40,64}", remote) is None
                or absent and ("expected_remote_revision" in parameters or not branch.startswith("codex/") or number is not None)
                or result.external_id != head
                or raw.get("head_revision") != head or raw.get("branch") != branch
                or raw.get("execution_workspace_id") != workspace
                or type(raw.get("expected_remote_absent")) is not bool
                or raw["expected_remote_absent"] != absent
                or raw.get("existing_change_request_number") != number
                or number is not None and type(raw.get("existing_change_request_number")) is not int
                or not any(e.evidence_type == CODE_HOST_BRANCH_EVIDENCE
                    and e.metadata.get("resource_id") == request.resource_ids[0] for e in result.evidence)):
            raise ValueError("procedure_branch_receipt_invalid")
        # The provider's existing verification owns fresh remote-ref equality.
        # A later read-only MR step may consume this exact original head value.
        return {"expected_head_sha":head, "published_branch":branch,
            "published_execution_workspace_id":workspace, "expected_remote_absent":absent}
