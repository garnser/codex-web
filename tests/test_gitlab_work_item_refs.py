from __future__ import annotations

from codex_web.services.work_item_dependencies import gitlab_project_issue_ref


def _payload(kind: str, iid: int) -> dict[str, object]:
    return {
        "object_kind": kind,
        "project": {"path_with_namespace": "group/project"},
        "object_attributes": {"iid": iid},
    }


def test_gitlab_work_item_refs_are_kind_qualified() -> None:
    assert gitlab_project_issue_ref(_payload("issue", 278)) == "group/project#278"
    assert gitlab_project_issue_ref(_payload("merge_request", 278)) == "group/project!278"
    assert (
        gitlab_project_issue_ref(_payload("pipeline", 278))
        == "group/project@pipeline:278"
    )


def test_gitlab_work_item_ref_requires_project_and_iid() -> None:
    assert gitlab_project_issue_ref({"object_kind": "issue"}) is None
