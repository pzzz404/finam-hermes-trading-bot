import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.execution import GuardedOrderExecutor, OrderGuardConfig, OrderNotAllowed
from finam_trading_bot.risk import OrderProposal, RiskConfig


class GuardedOrderExecutorTests(unittest.TestCase):
    def test_dry_run_never_calls_broker_and_returns_planned_order(self):
        calls = []
        executor = GuardedOrderExecutor(
            risk_config=RiskConfig(max_order_value=10_000, allowed_tickers={"SBER"}),
            guard_config=OrderGuardConfig(dry_run=True, allow_orders=False),
            broker_submit=lambda proposal: calls.append(proposal),
        )

        result = executor.submit(OrderProposal(ticker="SBER", side="buy", quantity=1, price=250))

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(calls, [])

    def test_live_order_requires_allow_orders_and_confirmation(self):
        executor = GuardedOrderExecutor(
            risk_config=RiskConfig(max_order_value=10_000, allowed_tickers={"SBER"}),
            guard_config=OrderGuardConfig(dry_run=False, allow_orders=False),
            broker_submit=lambda proposal: {"order_id": "x"},
        )

        with self.assertRaises(OrderNotAllowed):
            executor.submit(OrderProposal(ticker="SBER", side="buy", quantity=1, price=250), confirmation="CONFIRM_ORDER")

        executor = GuardedOrderExecutor(
            risk_config=RiskConfig(max_order_value=10_000, allowed_tickers={"SBER"}),
            guard_config=OrderGuardConfig(dry_run=False, allow_orders=True),
            broker_submit=lambda proposal: {"order_id": "x"},
        )
        with self.assertRaises(OrderNotAllowed):
            executor.submit(OrderProposal(ticker="SBER", side="buy", quantity=1, price=250), confirmation="wrong")

    def test_live_order_calls_broker_only_when_all_guards_pass(self):
        calls = []
        executor = GuardedOrderExecutor(
            risk_config=RiskConfig(max_order_value=10_000, allowed_tickers={"SBER"}),
            guard_config=OrderGuardConfig(dry_run=False, allow_orders=True),
            broker_submit=lambda proposal: calls.append(proposal) or {"order_id": "demo"},
        )

        result = executor.submit(OrderProposal(ticker="SBER", side="buy", quantity=1, price=250), confirmation="CONFIRM_ORDER")

        self.assertEqual(result, {"order_id": "demo"})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
