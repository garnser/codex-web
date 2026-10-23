"""Pure, explicitly registered projections of existing provider receipts.

These adapters neither execute nor verify an action. The procedure owner creates
canonical Evidence from the original receipt and, separately, its verification.
"""
from __future__ import annotations

import re

from codex_web.services.code_host_action_contract import (
    CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID,
    CODE_HOST_PULL_REQUEST_MERGE_EVIDENCE,
    CODE_HOST_PULL_REQUEST_READY_ACTION_ID,
    CODE_HOST_PULL_REQUEST_READY_EVIDENCE,
    CodeHostActionContract,
)


class CodeHostProcedureResultAdapter:
    provider_type = "gitlab"
    contract_version = "1.2"
    action_ids = (CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID, CODE_HOST_PULL_REQUEST_READY_ACTION_ID)

    def project(self, request, result):
        """Return only the supported, typed output fields; preserve legacy output."""
        if (result.status != "succeeded" or result.action_id != request.action_id
            or request.action_id not in self.action_ids or len(request.resource_ids) != 1
            or result.output.get("resource_id") != request.resource_ids[0]
            or result.idempotency_key != request.idempotency_key):
            raise ValueError("procedure_code_host_result_identity_mismatch")
        raw = result.output
        number = raw.get("pull_request_number")
        if type(number) is not int or number < 1 or str(number) != result.external_id:
            raise ValueError("procedure_code_host_result_number_invalid")
        if request.action_id == CODE_HOST_PULL_REQUEST_MERGE_ACTION_ID:
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
