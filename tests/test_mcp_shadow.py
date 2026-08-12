import unittest
from datetime import datetime, timezone

from finam_trading_bot.mcp_shadow import build_mcp_shadow_review, validate_mcp_tools


class FakeMcpClient:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return [
            {"name": "get-accounts-list", "description": "Покажи мои счета"},
            {"name": "get-account", "description": "Детали брокерского счёта и позиции"},
            {"name": "get-watchlists", "description": "Списки избранного"},
            {"name": "get-quote", "description": "Котировки инструментов"},
        ]

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments or {}))
        if name == "get-accounts-list":
            return _text_response({"accounts": [{"account_id": "DEMO-RU"}, {"account_id": "external"}]})
        if name == "get-account":
            return _text_response(
                {
                    "account_id": arguments["account_id"],
                    "cash": "100000",
                    "positions": [{"symbol": "SBER@MISX", "quantity": "10"}],
                }
            )
        if name == "get-watchlists":
            return _text_response({"watchlists": [{"name": "main", "symbols": ["SBER@MISX", "GAZP@MISX"]}]})
        if name == "get-quote":
            return _text_response({"quotes": [{"symbol": "SBER@MISX", "last_price": "310.1"}]})
        raise AssertionError(name)


def _text_response(payload):
    return {"result": {"content": [{"type": "text", "text": __import__("json").dumps(payload)}]}}


def _policy():
    return {
        "accounts": [
            {"account_id": "DEMO-RU", "universe": ["SBER@MISX"]},
            {"account_id": "DEMO-US", "universe": ["AAPL@XNGS"]},
        ]
    }


def _scan():
    return {
        "accounts": [
            {"account_id": "DEMO-RU", "cash": "100000", "positions": [{"symbol": "SBER@MISX"}]},
            {"account_id": "DEMO-US", "cash": "100000", "positions": []},
        ],
        "candidates": [{"account_id": "DEMO-RU", "symbol": "SBER@MISX"}],
    }


class McpShadowTests(unittest.TestCase):
    def test_validate_mcp_tools_blocks_unknown_or_mutating_tools(self):
        report = validate_mcp_tools(
            [
                {"name": "get-accounts-list", "description": "Счета"},
                {"name": "place_order", "description": "Submit order"},
            ]
        )

        self.assertFalse(report["ok"])
        self.assertEqual(report["unexpected_tools"][0]["name"], "place_order")

    def test_shadow_review_matches_accounts_and_omits_raw_content(self):
        review = build_mcp_shadow_review(
            _policy(),
            arena_scan=_scan(),
            client=FakeMcpClient(),
            now=datetime(2026, 7, 7, tzinfo=timezone.utc),
        )

        self.assertEqual(review["status"], "OK")
        self.assertTrue(review["raw_content_omitted"])
        self.assertEqual(review["matched_arena_accounts"][0]["account_id"], "DEMO-RU")
        self.assertEqual(review["unmatched_mcp_accounts"][0]["account_id"], "external")
        self.assertEqual(review["candidate_context_notes"][0]["already_exposed"], True)
        self.assertEqual(review["candidate_context_notes"][0]["watchlist_context"], "in_watchlist")
        self.assertEqual(review["candidate_context_notes"][0]["quote_context"], "available")
        self.assertNotIn("positions", review["matched_arena_accounts"][0])

    def test_missing_mcp_url_is_unavailable_without_trading_effect(self):
        review = build_mcp_shadow_review(_policy(), arena_scan=_scan(), env={}, now=datetime(2026, 7, 7, tzinfo=timezone.utc))

        self.assertEqual(review["status"], "MCP_UNAVAILABLE")
        self.assertEqual(review["trading_gate_effect"], "none")
        self.assertTrue(review["candidate_context_notes"][0]["mcp_unavailable"])


if __name__ == "__main__":
    unittest.main()
