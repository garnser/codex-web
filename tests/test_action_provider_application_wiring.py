from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ActionProviderApplicationWiringTests(unittest.TestCase):
    def test_gitlab_actions_do_not_inherit_read_side_http_proxy(self) -> None:
        source = (ROOT / "codex_web" / "application.py").read_text(encoding="utf-8")
        start = source.index("gitlab_action_api_base =")
        end = source.index("control_plane_broker_factory", start)
        wiring = source[start:end]

        self.assertIn("CODEX_WEB_GITLAB_ACTION_API_BASE", wiring)
        self.assertNotIn('"CODEX_WEB_GITLAB_API_BASE"', wiring)
        self.assertIn("https://dev.veridataops.com/gitlab/api/v4", wiring)


if __name__ == "__main__":
    unittest.main()
