from __future__ import annotations

import json

import server


def _support_issue_payload() -> dict:
    return {
        "object_kind": "issue",
        "event_name": "issue",
        "project": {"id": 5, "path_with_namespace": "veridataops/support"},
        "object_attributes": {
            "id": 1001,
            "iid": 5,
            "title": "Customer cannot sign in",
            "state": "opened",
            "action": "open",
            "url": "https://dev.veridataops.com/gitlab/veridataops/support/-/issues/5",
        },
        "labels": [],
    }


def test_support_servicedesk_ticket_detection_is_disabled_without_project_config(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH", raising=False)
    monkeypatch.delenv("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATHS", raising=False)

    assert not server._is_support_servicedesk_ticket_payload(_support_issue_payload())


def test_support_servicedesk_ticket_detection_uses_configured_project(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH", "veridataops/support")

    assert server._is_support_servicedesk_ticket_payload(_support_issue_payload())


def test_support_servicedesk_ticket_detection_ignores_closed_and_other_projects(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH", "veridataops/support")
    closed = _support_issue_payload()
    closed["object_attributes"]["state"] = "closed"
    other_project = _support_issue_payload()
    other_project["project"]["path_with_namespace"] = "veridataops/saas-app"

    assert not server._is_support_servicedesk_ticket_payload(closed)
    assert not server._is_support_servicedesk_ticket_payload(other_project)


def test_support_servicedesk_ticket_state_is_idempotent(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "support_servicedesk_intake.json"
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "SUPPORT_SERVICEDESK_STATE_FILE", state_file)
    payload = _support_issue_payload()

    assert server._remember_support_servicedesk_ticket(payload, "webhook", "event-1")
    assert not server._remember_support_servicedesk_ticket(payload, "sweep", "event-2")

    state = json.loads(state_file.read_text())
    ticket = state["tickets"]["5:5"]
    assert ticket["first_source"] == "webhook"
    assert ticket["last_source"] == "sweep"
    assert ticket["event_id"] == "event-1"


def test_issue_to_support_servicedesk_payload_preserves_ticket_identity(monkeypatch) -> None:
    monkeypatch.setenv("CODEX_WEB_SUPPORT_SERVICEDESK_PROJECT_PATH", "veridataops/support")
    issue = {
        "id": 1001,
        "iid": 5,
        "title": "Customer cannot sign in",
        "description": "Login loop after password reset.",
        "state": "opened",
        "created_at": "2026-06-11T09:00:00Z",
        "updated_at": "2026-06-11T09:05:00Z",
        "web_url": "https://dev.veridataops.com/gitlab/veridataops/support/-/issues/5",
        "labels": ["priority::1"],
    }

    payload = server._issue_to_support_servicedesk_payload(issue, "veridataops/support", 5)

    assert payload["object_attributes"]["iid"] == 5
    assert payload["object_attributes"]["action"] == "sweep"
    assert payload["labels"] == [{"title": "priority::1"}]
    assert server._is_support_servicedesk_ticket_payload(payload)
