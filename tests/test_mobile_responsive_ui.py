from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MobileResponsiveUiTests(unittest.TestCase):
    def test_production_index_loads_shared_mobile_assets(self) -> None:
        text = (ROOT / "static/index.html").read_text(encoding="utf-8")
        self.assertIn("static/mobile_responsive.css", text)
        self.assertIn("static/mobile_shell.js", text)

    def test_shared_mobile_layer_declares_required_phone_and_touch_contracts(self) -> None:
        css = (ROOT / "static/mobile_responsive.css").read_text(encoding="utf-8")
        js = (ROOT / "static/mobile_shell.js").read_text(encoding="utf-8")
        for width in ("900px", "640px", "420px"):
            self.assertIn(width, css)
        for term in (
            "--touch-target: 44px",
            "safe-area-inset",
            "100dvh",
            "overflow-x: auto",
            "orientation: landscape",
            "prefers-reduced-motion",
        ):
            self.assertIn(term, css)
        for term in (
            "aria-expanded",
            "aria-hidden",
            "inert",
            "visualViewport",
            "Escape",
        ):
            self.assertIn(term, js)


if __name__ == "__main__":
    unittest.main()
