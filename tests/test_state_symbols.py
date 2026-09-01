from __future__ import annotations

import unittest
from threading import Lock
from unittest.mock import patch

from src.state import AppState


class StateSymbolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = AppState.__new__(AppState)
        self.state._lock = Lock()
        self.state.symbols = [{"code": "002179", "name": "中航光电"}]
        self.state.current_symbol = "002179"
        self.state.config = {"symbols": list(self.state.symbols)}
        self.state.latest = {"002179": {"1d": {"close": 35}}}
        self.state.series = {"002179": {"1d": []}}

    def test_add_switch_and_remove_symbol(self) -> None:
        with patch("src.state.save_config") as save_config:
            added = self.state.add_symbol("600498", "烽火通信")
            self.assertEqual(added, {"code": "600498", "name": "烽火通信"})
            self.assertEqual(self.state.current_symbol, "600498")
            self.assertIn(added, self.state.config["symbols"])

            self.state.switch_symbol("002179")
            self.assertEqual(self.state.current_symbol, "002179")

            self.state.remove_symbol("600498")
            self.assertNotIn("600498", [item["code"] for item in self.state.symbols])
            self.assertNotIn("600498", [item["code"] for item in self.state.config["symbols"]])
            self.assertEqual(save_config.call_count, 2)

    def test_switch_unknown_symbol_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "股票不在观察列表中"):
            self.state.switch_symbol("999999")


if __name__ == "__main__":
    unittest.main()
