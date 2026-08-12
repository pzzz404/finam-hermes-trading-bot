from decimal import Decimal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.contract import (
    assert_broker_buy_allowed,
    normalize_price,
    normalize_quantity_to_lot,
    rules_from_finam,
)


class ContractTests(unittest.TestCase):
    def test_rules_from_finam_calculates_price_step_and_tradeability(self):
        rules = rules_from_finam(
            "MTSS@MISX",
            {"lot_size": {"value": "10.0"}, "decimals": 2, "min_step": "5"},
            {"is_tradable": {"value": True}, "longable": {"value": "AVAILABLE"}},
        )

        self.assertEqual(rules.lot_size, Decimal("10.0"))
        self.assertEqual(rules.price_step, Decimal("0.05"))
        self.assertTrue(rules.broker_buy_allowed)

    def test_quantity_and_price_normalization_follow_finam_lot_and_tick(self):
        rules = rules_from_finam(
            "MTSS@MISX",
            {"lot_size": {"value": "10.0"}, "decimals": 2, "min_step": "5"},
            {"is_tradable": {"value": True}, "longable": {"value": "AVAILABLE"}},
        )

        self.assertEqual(normalize_quantity_to_lot(Decimal("831"), rules), Decimal("830.0"))
        self.assertEqual(normalize_price("233.52", rules, direction="floor"), Decimal("233.50"))
        self.assertEqual(normalize_price("230.51", rules, direction="ceil"), Decimal("230.55"))

    def test_not_longable_blocks_broker_buy(self):
        rules = rules_from_finam(
            "SBER@MISX",
            {"lot_size": {"value": "1.0"}, "decimals": 2, "min_step": "1"},
            {"is_tradable": {"value": True}, "longable": {"value": "NOT_AVAILABLE"}},
        )

        with self.assertRaises(Exception) as context:
            assert_broker_buy_allowed(rules)
        self.assertIn("longable", str(context.exception))


if __name__ == "__main__":
    unittest.main()
