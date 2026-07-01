from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import server


class DevStatusRouteTests(unittest.TestCase):
    def test_devstatus_renders_release_and_health_sections(self) -> None:
        payload = {
            "generated_at": "2026-06-19T10:11:12+00:00",
            "overall_state": {
                "label": "Needs Attention",
                "tone": "warning",
                "detail": "1 split-brain item; 1 open main-targeted MR",
            },
            "summary": [
                {"label": "Canonical Findings", "value": 2},
                {"label": "Split Brain", "value": 1},
            ],
            "release": {
                "aligned_count": 2,
                "drift_count": 0,
                "items": [
                    {
                        "component": "saas-app",
                        "latest_valid_tag": "app-v2026.06.19-2",
                        "latest_any_tag": "app-v2026.06.19-2",
                        "main_sha": "691c08d12345",
                        "tag_sha": "691c08d12345",
                        "main_ahead_of_valid_tag": 0,
                        "valid_tag_ahead_of_main": 0,
                        "status": {"label": "Aligned", "tone": "ok"},
                    }
                ],
            },
            "release_validation": {
                "available": True,
                "label": "Passed",
                "tone": "ok",
                "generated_at": "2026-06-19T09:00:00+00:00",
                "release_tag": "app-v2026.06.19-2",
                "base_url": "https://app.veridataops.com",
                "steps": [
                    {
                        "name": "public_saas_smoke",
                        "status": "pass",
                        "tone": "ok",
                        "duration_seconds": 15.0,
                        "detail": "SaaS deployment verification passed.",
                    }
                ],
                "images": [{"name": "app", "tag": "app-v2026.06.19-2"}],
                "pressure_points": [],
            },
            "canonical_items": [
                {
                    "ref": "veridataops/saas-app#859",
                    "owner": "quinn",
                    "stage": "ready_for_validation",
                    "findings": ["canonical next_action is missing"],
                }
            ],
            "split_brain_items": [
                {
                    "ref": "veridataops/saas-app#67",
                    "owner": "dana",
                    "stage": "ready_for_validation",
                    "findings": ["accepted handoff recipient mismatches canonical owner"],
                }
            ],
            "routing_items": [
                {
                    "ref": "veridataops/data-packs#39",
                    "owner": "dana",
                    "stage": "implementation_active",
                }
            ],
            "checkout_items": [
                {
                    "component": "saas-app",
                    "branch": "feature/devstatus",
                    "dirty": True,
                    "stale_merged_branch": False,
                    "messages": ["checkout is dirty"],
                }
            ],
            "merge_requests": [
                {
                    "ref": "veridataops/marketing!22",
                    "merge_status": "rebase_needed",
                    "pipeline": "failed",
                    "updated_at": "2026-06-19T08:00:00Z",
                    "title": "Rebase marketing release lane",
                }
            ],
            "errors": [],
        }
        with patch("server.build_devstatus_context", return_value=payload):
            response = asyncio.run(server.devstatus())

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("Development Health and Release Status", body)
        self.assertIn("app-v2026.06.19-2", body)
        self.assertIn("public_saas_smoke", body)
        self.assertIn("veridataops/marketing!22", body)
