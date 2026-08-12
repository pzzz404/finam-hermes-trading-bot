import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.paper import PaperBroker, PaperFill
from finam_trading_bot.risk import OrderProposal, RiskConfig


class PaperBrokerTests(unittest.TestCase):
    def test_paper_broker_records_accepted_fill_and_cash_position(self):
        broker = PaperBroker(cash=100_000, risk_config=RiskConfig(max_order_value=10_000, max_position_value=20_000, allowed_tickers={"SBER"}))
        proposal = OrderProposal(ticker="SBER", side="buy", quantity=10, price=250)

        result = broker.submit(proposal)

        self.assertTrue(result.accepted)
        self.assertIsInstance(result.fill, PaperFill)
        self.assertEqual(broker.cash, 97_500)
        self.assertEqual(broker.positions["SBER"], 10)
        self.assertEqual(len(broker.journal), 1)
        self.assertEqual(broker.journal[0]["mode"], "paper")

    def test_paper_broker_rejects_risk_violation_without_state_change(self):
        broker = PaperBroker(cash=100_000, risk_config=RiskConfig(max_order_value=1_000, allowed_tickers={"SBER"}))
        proposal = OrderProposal(ticker="SBER", side="buy", quantity=10, price=250)

        result = broker.submit(proposal)

        self.assertFalse(result.accepted)
        self.assertIsNone(result.fill)
        self.assertEqual(broker.cash, 100_000)
        self.assertEqual(broker.positions, {})
        self.assertEqual(broker.journal[0]["accepted"], False)
        self.assertEqual(broker.journal[0]["reason"], "order_value_limit")


if __name__ == "__main__":
    unittest.main()
