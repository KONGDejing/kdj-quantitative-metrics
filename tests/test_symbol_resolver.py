from __future__ import annotations

import unittest

from src.symbol_resolver import _parse_sina_suggestions, resolve_symbol_input


class SymbolResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = {
            "symbols": [{"code": "002179", "name": "中航光电"}],
            "price_alerts": {"600498": {"name": "烽火通信"}},
        }

    def test_direct_code_uses_known_name(self) -> None:
        self.assertEqual(
            resolve_symbol_input("002179", None, self.config),
            {"code": "002179", "name": "中航光电"},
        )

    def test_name_only_resolves_from_local_config(self) -> None:
        self.assertEqual(
            resolve_symbol_input("", "烽火通信", self.config),
            {"code": "600498", "name": "烽火通信"},
        )

    def test_name_in_code_box_resolves_from_remote_exact_match(self) -> None:
        lookup = lambda _query: [
            {"code": "600584", "name": "长电科技"},
            {"code": "000001", "name": "上证指数"},
        ]
        self.assertEqual(
            resolve_symbol_input("长电科技", None, {"symbols": []}, remote_lookup=lookup),
            {"code": "600584", "name": "长电科技"},
        )

    def test_fuzzy_result_is_not_silently_added(self) -> None:
        lookup = lambda _query: [{"code": "002179", "name": "中航光电"}]
        with self.assertRaisesRegex(ValueError, "未找到完全匹配"):
            resolve_symbol_input("中航", None, {"symbols": []}, remote_lookup=lookup)

    def test_sina_parser_ignores_funds_and_keeps_supported_markets(self) -> None:
        text = (
            'var suggestvalue="中航光电,11,002179,sz002179,中航光电;'
            '某基金,201,010364,of010364,某基金;贵州茅台,11,600519,sh600519,贵州茅台";'
        )
        self.assertEqual(
            _parse_sina_suggestions(text),
            [
                {"code": "002179", "name": "中航光电"},
                {"code": "600519", "name": "贵州茅台"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
