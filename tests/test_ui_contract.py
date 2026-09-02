from __future__ import annotations

import re
import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


class UiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (BASE_DIR / "web" / "index.html").read_text(encoding="utf-8")
        cls.javascript = (BASE_DIR / "web" / "app.js").read_text(encoding="utf-8")

    def test_every_javascript_element_id_exists_in_html(self) -> None:
        html_ids = set(re.findall(r'\bid=["\']([^"\']+)', self.html))
        javascript_ids = set(re.findall(r'getElementById\(["\']([^"\']+)', self.javascript))
        self.assertEqual(javascript_ids - html_ids, set())

    def test_every_button_has_a_listener_or_inline_handler(self) -> None:
        button_tags = re.findall(r"<button\b[^>]*>", self.html)
        direct_listeners = set(re.findall(
            r'getElementById\(["\']([^"\']+)["\']\)\.addEventListener', self.javascript
        ))
        for tag in button_tags:
            button_id = re.search(r'\bid=["\']([^"\']+)', tag)
            dynamic_class = "band-period-btn" in tag
            inline_handler = "onclick=" in tag
            self.assertTrue(
                inline_handler or dynamic_class or (button_id and button_id.group(1) in direct_listeners),
                f"button has no handler: {tag}",
            )

    def test_relative_band_periods_are_not_hard_coded_dates(self) -> None:
        self.assertNotIn("全周期", self.html)
        self.assertNotIn('data-start="2010-01-01"', self.html)
        self.assertIn("initializeBandPeriods", self.javascript)
        self.assertIn("/api/band-analysis/periods", self.javascript)

    def test_write_controls_require_validated_token(self) -> None:
        for button_id in ("add-symbol", "trade-submit", "apply-correction", "delete-trade"):
            tag = re.search(rf'<button\b[^>]*\bid="{button_id}"[^>]*>', self.html)
            self.assertIsNotNone(tag)
            self.assertIn("data-write-action", tag.group(0))
            self.assertIn("disabled", tag.group(0))
        self.assertIn("initializeWriteAccess", self.javascript)
        self.assertIn("requireWriteAccess", self.javascript)
        self.assertIn("./scripts/show-write-token.sh", self.javascript)


if __name__ == "__main__":
    unittest.main()
