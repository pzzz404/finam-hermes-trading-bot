import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.arena_learning import (  # noqa: E402
    build_learning_proposal,
    build_learning_report,
    decision_snapshots_from_scan,
    outcome_records_from_execution_ledger,
    validate_learning_policy,
)


class ArenaLearningTests(unittest.TestCase):
    def sample_policy(self):
        return {
            "learning": {
                "enabled": True,
                "mode": "propose_only",
                "objective": "risk_adjusted_growth",
                "horizons_minutes": [60, 240, 1440, 4320],
                "min_samples_for_suggestion": 2,
                "max_score_weight_delta_per_week": 5,
                "max_universe_rank_shift_per_week": 5,
            }
        }

    def test_decision_snapshot_preserves_gates_and_score_components(self):
        scan = {
            "run_id": "run-1",
            "accounts": [{"account_id": "DEMO-RU", "label": "РФ"}],
            "candidates": [
                {
                    "account_id": "DEMO-RU",
                    "symbol": "SBER@MISX",
                    "side": "BUY",
                    "execution_allowed": False,
                    "gate_reasons": ["candidate_score_below_min"],
                    "entry_price": "300",
                    "arena_growth_score": {
                        "score": 45,
                        "label": "LOW",
                        "research_verdict": "RISK",
                        "components": {"trend_momentum": 10, "volatility_risk": 4},
                    },
                }
            ],
        }

        events = decision_snapshots_from_scan(scan, self.sample_policy(), now=datetime(2026, 6, 4, tzinfo=timezone.utc))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["decision_status"], "BLOCKED")
        self.assertEqual(events[0]["score_components"]["trend_momentum"], 10)
        self.assertEqual(events[0]["research_verdict"], "RISK")
        self.assertFalse(events[0]["protected_gate_hit"])

    def test_execution_ledger_outcome_pairs_buy_and_sell(self):
        ledger = [
            {
                "timestamp": "2026-06-04T10:00:00+00:00",
                "account_id": "DEMO-RU",
                "symbol": "SBER@MISX",
                "side": "BUY",
                "quantity": "10",
                "price": "300",
                "estimated_commission": "1",
                "order_id": "buy-1",
            },
            {
                "timestamp": "2026-06-04T14:00:00+00:00",
                "account_id": "DEMO-RU",
                "symbol": "SBER@MISX",
                "side": "SELL",
                "quantity": "10",
                "price": "310",
                "estimated_commission": "1",
                "order_id": "sell-1",
            },
        ]

        outcomes = outcome_records_from_execution_ledger(ledger, self.sample_policy(), now=datetime(2026, 6, 4, tzinfo=timezone.utc))

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["net_pnl_rub"], "98")
        self.assertEqual(outcomes[0]["side"], "LONG_ROUND_TRIP")

    def test_execution_ledger_outcome_preserves_mae_mfe_horizons(self):
        policy = self.sample_policy()
        policy["portfolio"] = {"weak_exit_min_mae_samples": 1}
        ledger = [
            {
                "timestamp": "2026-06-04T10:00:00+00:00",
                "account_id": "DEMO-RU",
                "symbol": "SBER@MISX",
                "side": "BUY",
                "quantity": "10",
                "price": "300",
                "order_id": "buy-1",
            },
            {
                "timestamp": "2026-06-04T14:00:00+00:00",
                "account_id": "DEMO-RU",
                "symbol": "SBER@MISX",
                "side": "SELL",
                "quantity": "10",
                "price": "310",
                "order_id": "sell-1",
                "mae_mfe_horizons": {
                    "60": {"mae_r": "-0.20", "mfe_r": "0.80", "source": "quote_replay"},
                    "240": {"mae_r": "-0.30", "mfe_r": "1.10", "source": "quote_replay"},
                },
            },
        ]

        outcomes = outcome_records_from_execution_ledger(ledger, policy, now=datetime(2026, 6, 4, tzinfo=timezone.utc))
        report = build_learning_report(policy, [], outcomes)

        self.assertEqual(outcomes[0]["mae_mfe_horizons"]["60"]["mae_r"], "-0.20")
        self.assertEqual(outcomes[0]["mae_mfe_horizons"]["240"]["mfe_r"], "1.10")
        self.assertEqual(report["weak_exit_mae_samples_count"], 1)
        self.assertTrue(report["weak_exit_threshold_ready"])

    def test_unmatched_exit_weak_marks_attribution_uncertain_without_pnl_bias(self):
        ledger = [
            {
                "timestamp": "2026-06-01T19:00:00+00:00",
                "account_id": "DEMO-AI",
                "symbol": "LKOH@MISX",
                "side": "SELL",
                "quantity": "50",
                "price": "4912",
                "action": "EXIT_WEAK",
                "order_id": "sell-lkoh",
            }
        ]

        outcomes = outcome_records_from_execution_ledger(ledger, self.sample_policy(), now=datetime(2026, 6, 4, tzinfo=timezone.utc))
        proposal = build_learning_proposal(
            self.sample_policy(),
            [{"decision_id": str(i), "symbol": "SBER@MISX", "decision_status": "EXECUTABLE", "gate_reasons": []} for i in range(3)],
            outcomes,
        )

        self.assertEqual(outcomes[0]["source"], "arena_execution_ledger_unmatched_exit")
        self.assertTrue(outcomes[0]["learning_attribution_uncertain_symbol"])
        self.assertNotIn("net_pnl_rub", outcomes[0])
        self.assertIn("LKOH@MISX", proposal["patch"]["learning"]["attribution_uncertain_symbols"])
        self.assertNotIn("deprioritize_symbols", proposal["patch"]["learning"])
        self.assertEqual(proposal["report"]["closed_trades_count"], 0)

    def test_learning_report_tracks_growth_and_repeat_pattern_blocks(self):
        decisions = [
            {
                "decision_id": "blocked-repeat",
                "symbol": "META@XNGS",
                "decision_status": "BLOCKED",
                "gate_reasons": ["repeat_pattern_loss_cooldown"],
            }
        ]
        outcomes = [
            {"symbol": "AAPL@XNGS", "net_pnl_rub": "-100"},
            {"symbol": "MSFT@XNGS", "net_pnl_rub": "200"},
        ]

        report = build_learning_report(self.sample_policy(), decisions, outcomes)

        self.assertEqual(report["avoided_repeat_loss_setups"], 1)
        self.assertEqual(report["blocked_loss_recurrence"], 1)
        self.assertEqual(report["net_pnl_rub"], "100")
        self.assertEqual(report["average_closed_trade_pnl_rub"], "50")
        self.assertEqual(report["equity_slope_rub_per_trade"], "200")
        self.assertEqual(report["research_cost_source"], "research-budget-report")

    def test_learning_proposal_explains_repeat_pattern_as_growth_guard(self):
        decisions = [
            {"decision_id": "1", "symbol": "META@XNGS", "decision_status": "BLOCKED", "gate_reasons": ["repeat_pattern_loss_cooldown"]},
            {"decision_id": "2", "symbol": "AAPL@XNGS", "decision_status": "PROPOSE_ONLY", "gate_reasons": []},
            {"decision_id": "3", "symbol": "MSFT@XNGS", "decision_status": "PROPOSE_ONLY", "gate_reasons": []},
        ]

        proposal = build_learning_proposal(self.sample_policy(), decisions, [])

        self.assertEqual(proposal["patch"]["learning"]["repeat_pattern_loss_status"], "active")
        self.assertTrue(
            any("repeat-pattern guard" in explanation for explanation in proposal["explanations"])
        )

    def test_learning_proposal_does_not_change_protected_gates(self):
        decisions = [
            {"decision_id": str(i), "symbol": "SBER@MISX", "decision_status": "BLOCKED", "gate_reasons": ["candidate_score_below_min"]}
            for i in range(3)
        ]
        outcomes = [{"symbol": "SBER@MISX", "net_pnl_rub": "-10"}]

        proposal = build_learning_proposal(self.sample_policy(), decisions, outcomes)

        self.assertEqual(proposal["status"], "PROPOSED")
        self.assertTrue(proposal["protected_hard_gates_unchanged"])
        self.assertFalse(proposal["broker_mutation"])
        self.assertFalse(proposal["policy_write"])


    def test_learning_report_ignores_excluded_records(self):
        report = build_learning_report(
            self.sample_policy(),
            [
                {"decision_status": "EXECUTABLE", "symbol": "SBER@MISX", "gate_reasons": []},
                {"decision_status": "BLOCKED", "symbol": "GAZP@MISX", "gate_reasons": ["candidate_score_below_min"], "exclude_from_learning": True},
            ],
            [
                {"symbol": "SBER@MISX", "net_pnl_rub": "15"},
                {"symbol": "GAZP@MISX", "net_pnl_rub": "-1000", "exclude_from_learning": True},
            ],
        )

        self.assertEqual(report["decisions_count"], 1)
        self.assertEqual(report["closed_trades_count"], 1)
        self.assertEqual(report["net_pnl_rub"], "15")
        self.assertEqual(report["top_gate_reasons"], [])

    def test_learning_proposal_ignores_excluded_records(self):
        decisions = [
            {"decision_id": str(i), "symbol": "GAZP@MISX", "decision_status": "BLOCKED", "gate_reasons": ["candidate_score_below_min"], "exclude_from_learning": True}
            for i in range(3)
        ]
        outcomes = [{"symbol": "GAZP@MISX", "net_pnl_rub": "-1000", "exclude_from_learning": True}]

        proposal = build_learning_proposal(self.sample_policy(), decisions, outcomes)

        self.assertEqual(proposal["status"], "NO_SUGGESTION")
        self.assertEqual(proposal["samples"], 0)
        self.assertEqual(proposal["patch"], {})

    def test_validate_requires_default_protected_gates(self):
        policy = self.sample_policy()
        policy["learning"]["protected_hard_gates"] = ["daily_loss"]

        validation = validate_learning_policy(policy)

        self.assertIn("learning.protected_hard_gates must include default hard gates", validation["errors"])


    def test_learning_report_marks_weak_exit_threshold_provisional_without_mae_samples(self):
        policy = self.sample_policy()
        policy["portfolio"] = {"weak_exit_min_mae_samples": 2, "weak_exit_threshold_source": "provisional_hysteresis_v1"}

        report = build_learning_report(
            policy,
            [{"decision_status": "EXECUTABLE", "symbol": "SBER@MISX", "gate_reasons": []}],
            [{"symbol": "SBER@MISX", "net_pnl_rub": "15"}],
        )

        self.assertEqual(report["weak_exit_threshold_source"], "provisional_hysteresis_v1")
        self.assertEqual(report["weak_exit_mae_samples_count"], 0)
        self.assertFalse(report["weak_exit_threshold_ready"])

    def test_learning_proposal_keeps_weak_exit_threshold_provisional_until_mae_sample_minimum(self):
        policy = self.sample_policy()
        policy["portfolio"] = {"weak_exit_min_mae_samples": 2, "weak_exit_threshold_source": "provisional_hysteresis_v1"}
        decisions = [
            {"decision_id": str(i), "symbol": "SBER@MISX", "decision_status": "EXECUTABLE", "gate_reasons": []}
            for i in range(3)
        ]
        outcomes = [{"symbol": "SBER@MISX", "net_pnl_rub": "15", "mae_r": "-0.10"}]

        proposal = build_learning_proposal(policy, decisions, outcomes)

        self.assertEqual(proposal["status"], "PROPOSED")
        self.assertEqual(proposal["patch"]["learning"]["weak_exit_threshold_status"], "provisional_pending_mae_samples")
        self.assertFalse(proposal["report"]["weak_exit_threshold_ready"])

    def test_learning_report_counts_samples(self):
        report = build_learning_report(
            self.sample_policy(),
            [{"decision_status": "EXECUTABLE", "symbol": "SBER@MISX", "gate_reasons": []}],
            [{"symbol": "SBER@MISX", "net_pnl_rub": "15"}],
        )

        self.assertEqual(report["decisions_count"], 1)
        self.assertEqual(report["closed_trades_count"], 1)
        self.assertEqual(report["win_rate_pct"], "100")


if __name__ == "__main__":
    unittest.main()
