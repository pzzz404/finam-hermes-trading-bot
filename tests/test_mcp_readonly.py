import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.mcp_readonly import (
    build_analytics_assessment,
    build_tool_safety_report,
    classify_tool,
    compare_snapshot_shape,
)


class McpReadonlyTests(unittest.TestCase):
    def test_classify_blocks_mutating_order_tools(self):
        tool = classify_tool({"name": "place_order", "description": "Submit a market order"})

        self.assertEqual(tool["safety"], "mutating_blocked")
        self.assertIn("place_order", tool["mutating_reasons"])

    def test_classify_allows_readonly_order_listing(self):
        tool = classify_tool({"name": "list_orders", "description": "Return active and historical orders"})

        self.assertEqual(tool["safety"], "read_only_candidate")
        self.assertIn("orders", tool["categories"])
        self.assertEqual(tool["mutating_reasons"], [])

    def test_tool_safety_report_requires_core_broker_coverage(self):
        report = build_tool_safety_report(
            [
                {"name": "get_accounts"},
                {"name": "get_positions"},
                {"name": "list_orders"},
                {"name": "list_trades"},
                {"name": "list_transactions"},
                {"name": "cancel_order"},
            ]
        )

        self.assertEqual(report["status"], "READY_FOR_READ_ONLY_TEST")
        self.assertEqual(len(report["blocked_mutating_tools"]), 1)
        self.assertFalse(report["safety"]["broker_mutation"])

    def test_classifies_finam_russian_tool_descriptions(self):
        report = build_tool_safety_report(
            [
                {
                    "name": "get-account",
                    "description": "Возвращает детальную информацию о брокерском счёте, текущие позиции, тикер, количество, цена входа и остатки по валютам.",
                },
                {"name": "get-quote", "description": "Возвращает котировки по списку инструментов из MarketData."},
                {"name": "get-watchlists", "description": "Возвращает все списки избранного текущего пользователя."},
            ]
        )

        self.assertEqual(report["status"], "READY_FOR_READ_ONLY_TEST")
        self.assertIn("positions", report["coverage"])
        self.assertIn("market_data", report["coverage"])
        self.assertIn("watchlists", report["coverage"])

    def test_snapshot_shape_comparison_redacts_sensitive_keys(self):
        diff = compare_snapshot_shape(
            {"account": {"token": "secret", "positions": []}},
            {"account": {"token": "other", "positions": [], "commission": "1.00"}},
        )

        self.assertIn("account.commission", diff["mcp_only_paths"])
        self.assertNotIn("other", "\n".join(diff["mcp_only_paths"]))

    def test_analytics_assessment_recommends_adapter_when_mutating_tools_exist(self):
        assessment = build_analytics_assessment(
            [
                {"name": "get_accounts"},
                {"name": "get_positions"},
                {"name": "list_orders"},
                {"name": "list_trades"},
                {"name": "list_transactions"},
                {"name": "get_commissions"},
                {"name": "place_order"},
            ]
        )

        self.assertEqual(assessment["status"], "PROMISING_READ_ONLY")
        self.assertIn("allowlist", assessment["recommendation"])
        self.assertTrue(assessment["analytics_improvements"])


if __name__ == "__main__":
    unittest.main()
