import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.risk import OrderProposal, RiskConfig, RiskViolation, validate_order


class RiskTests(unittest.TestCase):
    def test_validate_order_accepts_small_allowed_dry_run_order(self):
        config = RiskConfig(
            max_order_value=10_000,
            max_position_value=50_000,
            max_daily_loss=5_000,
            allowed_tickers={"SBER"},
        )
        proposal = OrderProposal(ticker="SBER", side="buy", quantity=10, price=250)

        decision = validate_order(proposal, config, current_position_value=0, realized_pnl_today=0)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "ok")

    def test_validate_order_rejects_unallowed_ticker(self):
        config = RiskConfig(allowed_tickers={"SBER"})
        proposal = OrderProposal(ticker="GAZP", side="buy", quantity=1, price=100)

        decision = validate_order(proposal, config, current_position_value=0, realized_pnl_today=0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, RiskViolation.TICKER_NOT_ALLOWED.value)

    def test_validate_order_rejects_order_value_and_position_limits(self):
        config = RiskConfig(max_order_value=1_000, max_position_value=1_500, allowed_tickers={"SBER"})
        proposal = OrderProposal(ticker="SBER", side="buy", quantity=10, price=200)

        decision = validate_order(proposal, config, current_position_value=0, realized_pnl_today=0)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, RiskViolation.ORDER_VALUE_LIMIT.value)

        proposal = OrderProposal(ticker="SBER", side="buy", quantity=5, price=200)
        decision = validate_order(proposal, config, current_position_value=1_000, realized_pnl_today=0)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, RiskViolation.POSITION_VALUE_LIMIT.value)

    def test_validate_order_rejects_daily_loss_limit(self):
        config = RiskConfig(max_daily_loss=1_000, allowed_tickers={"SBER"})
        proposal = OrderProposal(ticker="SBER", side="sell", quantity=1, price=100)

        decision = validate_order(proposal, config, current_position_value=0, realized_pnl_today=-1_001)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, RiskViolation.DAILY_LOSS_LIMIT.value)


if __name__ == "__main__":
    unittest.main()
