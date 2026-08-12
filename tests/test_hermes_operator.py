import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("hermes_operator_script", ROOT / "scripts" / "hermes_operator.py")
hermes_operator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hermes_operator)


class HermesOperatorTests(unittest.TestCase):
    def setUp(self):
        self._runtime_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._runtime_tmp.cleanup)
        self._safety_patch = mock.patch.object(
            hermes_operator,
            "DEFAULT_SAFETY_STATE_PATH",
            Path(self._runtime_tmp.name) / "safety.json",
        )
        self._safety_patch.start()
        self.addCleanup(self._safety_patch.stop)
        self._operator_env_patch = mock.patch.object(
            hermes_operator,
            "DEFAULT_OPERATOR_ENV_FILES",
            (
                Path(self._runtime_tmp.name) / "missing-hermes.env",
                Path(self._runtime_tmp.name) / "missing-runtime.env",
            ),
        )
        self._operator_env_patch.start()
        self.addCleanup(self._operator_env_patch.stop)
        self._arena_ledger_patch = mock.patch.object(
            hermes_operator,
            "DEFAULT_ARENA_EXECUTION_LEDGER_PATH",
            Path(self._runtime_tmp.name) / "arena_execution_ledger.jsonl",
        )
        self._arena_ledger_patch.start()
        self.addCleanup(self._arena_ledger_patch.stop)

    def sample_policy(self):
        return {
            "mode": "supervised",
            "risk": {
                "risk_per_trade_pct": "1.0",
                "atr_period": 14,
                "stop_atr_multiplier": "2",
                "take_profit_r": "2",
                "max_open_positions": 5,
                "max_new_trades_per_run": 1,
                "min_target_r": "1.5",
                "max_total_open_risk_pct": "3.0",
            },
            "permissions": {
                "protective_stops_auto": True,
                "new_buys_require_telegram_confirmation": True,
                "manual_sells_require_confirmation": True,
                "shorts_require_separate_confirmation": True,
                "second_tier_requires_confirmation": True,
                "unknown_target_requires_confirmation": True,
                "incomplete_candidate_blocks_trade": True,
                "propose_only_when_manual_confirmation_required": True,
            },
            "universe": ["SBER@MISX", "GAZP@MISX"],
            "research": {
                "enabled": True,
                "provider": "codex_review",
                "timeout_seconds": 20,
                "max_candidates": 3,
                "context_max_bytes": 4096,
                "codex_review_ttl_minutes": 240,
                "recent_outcomes_limit": 5,
                "rss_flags_limit": 5,
                "review_dir": tempfile.mkdtemp(),
                "finam_rss_enabled": False,
                "failure_policy": {
                    "allow_reduced_risk_trade_if_research_unavailable": False,
                    "reduced_risk_per_trade_pct": "0.5",
                    "reduced_risk_max_trades_per_day": 1,
                    "explicit_avoid_blocks_trade": True,
                    "research_unavailable_requires_confirmation": True,
                },
            },
        }

    def test_load_default_operator_env_fills_missing_values_without_overriding_process_env(self):
        hermes_env = Path(self._runtime_tmp.name) / "hermes.env"
        runtime_env = Path(self._runtime_tmp.name) / "runtime.env"
        hermes_env.write_text(
            "OPENROUTER_API_KEY=from-hermes\nFINAM_TOKEN=from-hermes\n",
            encoding="utf-8",
        )
        runtime_env.write_text(
            "OPENROUTER_API_KEY=from-runtime\nFINAM_ARENA_AUTO_TRADE_ENABLED=true\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            hermes_operator,
            "DEFAULT_OPERATOR_ENV_FILES",
            (hermes_env, runtime_env),
        ), mock.patch.dict(os.environ, {"FINAM_TOKEN": "from-process"}, clear=True):
            loaded = hermes_operator._load_default_operator_env()
            self.assertEqual(loaded, [str(hermes_env), str(runtime_env)])
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "from-runtime")
            self.assertEqual(os.environ["FINAM_TOKEN"], "from-process")
            self.assertEqual(os.environ["FINAM_ARENA_AUTO_TRADE_ENABLED"], "true")

    def sample_arena_policy(self):
        return {
            "mode": "approval",
            "emergency_stop": False,
            "base_url": "https://arena.finam.ru",
            "approval_until": "2026-06-03T23:59:59+03:00",
            "session_secret_env": "FINAM_ARENA_API",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "strategy": "russian_equities_h1_h4",
                    "markets": ["MISX"],
                    "universe": ["SBER@MISX"],
                    "allow_long": True,
                    "allow_short": True,
                    "paused": False,
                    "trade_mode": "manual",
                    "risk_multiplier": "1.0",
                },
                {
                    "account_id": "DEMO-US",
                    "label": "США",
                    "strategy": "us_equities_h1_h4",
                    "markets": ["XNGS"],
                    "universe": ["AAPL@XNGS"],
                    "allow_long": True,
                    "allow_short": True,
                    "paused": False,
                    "trade_mode": "manual",
                    "risk_multiplier": "1.0",
                },
                {
                    "account_id": "DEMO-AI",
                    "label": "AI",
                    "strategy": "experimental_ai_cross_market",
                    "markets": ["MISX"],
                    "universe": ["SBER@MISX"],
                    "allow_long": True,
                    "allow_short": True,
                    "paused": False,
                    "trade_mode": "manual",
                    "risk_multiplier": "1.0",
                    "experimental": True,
                },
            ],
            "risk": {
                "risk_per_trade_pct": "1",
                "max_position_notional_pct": "25",
                "max_daily_loss_pct": "3",
                "max_account_drawdown_pct": "8",
                "max_open_risk_pct": "5",
                "max_open_positions": 8,
                "max_new_trades_per_account_per_run": 1,
            },
        }

    def _arena_scan_with_candidate(
        self,
        *,
        account_id: str,
        symbol: str = "SBER@MISX",
        side: str = "BUY",
        quantity: str = "10",
        stop: str = "290.00",
    ):
        accounts = [
            {"account_id": "DEMO-RU", "label": "РФ", "equity": "1000000", "paused": False, "trade_mode": "manual"},
            {"account_id": "DEMO-US", "label": "США", "equity": "1000000", "paused": False, "trade_mode": "manual"},
            {"account_id": "DEMO-AI", "label": "AI", "equity": "1000000", "paused": False, "trade_mode": "manual"},
        ]
        return {
            "status": "OK",
            "mode": "approval",
            "accounts": accounts,
            "candidates": [
                {
                    "account_id": account_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "entry_price": "300.00" if side == "BUY" else "190.00",
                    "stop_price": stop,
                    "take_profit_price": "320.00" if side == "BUY" else "178.00",
                    "entry_timeframe": "H1",
                    "execution_allowed": True,
                    "gate_reasons": [],
                }
            ],
            "errors": [],
            "warnings": [],
        }

    def sample_report(self):
        return {
            "status": "OK",
            "time_msk": "2026-05-22 10:00",
            "operator_mode": "supervised",
            "account": {"account_id": "DEMO-ACCOUNT", "equity": "400000"},
            "positions": [],
            "zero_positions": [],
            "orders": {"orders_count": 0},
            "candidates": [
                {
                    "symbol": "PLZL@MISX",
                    "status": "BLOCKED",
                    "gate_reasons": ["target_r_below_min_1.5"],
                    "decision_record": {"action": "do_not_buy"},
                }
            ],
            "research": {"status": "skipped"},
            "errors": [],
            "warnings": [],
        }

    def sample_proposal_report(self):
        report = self.sample_report()
        report["candidates"] = [
            {
                "symbol": "SBER@MISX",
                "status": "PROPOSE_ONLY",
                "requires_confirmation": True,
                "current_price": "323.49",
                "quantity": "601",
                "notional": "194417.49",
                "atr14": "1.44",
                "stop": "320.60",
                "tp_2r": "329.27",
                "nearest_target": "330.00",
                "nearest_target_r": "2.25",
                "risk_rub": "1737.75",
                "gate_reasons": [],
                "decision_record": {
                    "status": "PROPOSE_ONLY",
                    "action": "manual_confirmation_required",
                    "requires_confirmation": True,
                    "gate_reasons": [],
                },
            }
        ]
        report["research"] = {
            "status": "ok",
            "provider": "codex_review",
            "verdict": "RISK",
            "symbols": ["SBER@MISX"],
            "summary": "SBER@MISX — RISK: корпоративный фон смешанный.",
        }
        return report

    def sample_review(self, verdict="RISK"):
        return {
            "schema_version": 1,
            "created_at": "2026-05-22T07:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "source": "hermes_codex",
            "provider": "openai-codex",
            "symbol": "SBER@MISX",
            "context_hash": "testhash",
            "review": {
                "verdict": verdict,
                "confidence": 78,
                "reasons": ["Фон смешанный, но блокирующих факторов нет"],
                "blocking_flags": [] if verdict != "AVOID" else ["bad_news"],
                "summary": "Компактная Codex-проверка допускает только ручное подтверждение.",
            },
        }

    def test_morning_plan_separates_watchlist_from_buy_candidates(self):
        output = hermes_operator._morning_plan_output(self.sample_report())

        self.assertEqual(output["command"], "morning-plan")
        self.assertEqual(output["buy_candidates_now"], [])
        self.assertEqual(output["watchlist_now"][0]["symbol"], "PLZL@MISX")

    def test_trade_proposal_builds_confirmation_card_without_mutations(self):
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_proposal_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
            )

        self.assertEqual(output["status"], "PROPOSE_ONLY")
        self.assertEqual(output["command"], "trade-proposal")
        proposal = output["proposal"]
        self.assertEqual(proposal["symbol"], "SBER@MISX")
        self.assertEqual(proposal["side"], "BUY")
        self.assertEqual(proposal["quantity"], 601)
        self.assertEqual(proposal["entry"]["type"], "LIMIT")
        self.assertEqual(proposal["entry"]["limit_price"], "323.49")
        self.assertEqual(proposal["protective_stop"]["side"], "SELL")
        self.assertEqual(proposal["protective_stop"]["stop_price"], "320.60")
        self.assertTrue(proposal["protective_stop"]["must_place_immediately_after_fill"])
        self.assertEqual(proposal["take_profit"]["price"], "329.27")
        self.assertEqual(proposal["risk"]["risk_rub"], "1737.75")
        self.assertEqual(proposal["codex_review"]["verdict"], "RISK")
        self.assertEqual(proposal["execution"]["confirmation_phrase"], "CONFIRM_BUY SBER@MISX")
        self.assertFalse(proposal["execution"]["broker_mutation"])
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertIn("Для подтверждения напиши: CONFIRM_BUY SBER@MISX", output["confirmation_text"])

    def test_trade_proposal_default_selection_ignores_growth_display_score(self):
        report = self.sample_proposal_report()
        first = report["candidates"][0]
        first["growth_score"] = {"score": 20, "label": "LOW"}
        second = dict(first)
        second["symbol"] = "TATN@MISX"
        second["growth_score"] = {"score": 90, "label": "HIGH"}
        report["candidates"] = [first, second]

        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_proposal_output(report, policy=self.sample_policy())

        self.assertEqual(output["proposal"]["symbol"], "SBER@MISX")

    def test_trade_proposal_refuses_blocked_candidates(self):
        output = hermes_operator._trade_proposal_output(self.sample_report(), policy=self.sample_policy(), symbol="PLZL@MISX")

        self.assertEqual(output["status"], "NO_PROPOSAL")
        self.assertEqual(output["reason"], "no_unblocked_candidates")
        self.assertEqual(output["blocked_count"], 1)
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_proposal_requires_fresh_codex_review(self):
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=None):
            output = hermes_operator._trade_proposal_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
            )

        self.assertEqual(output["status"], "CODEX_REVIEW_REQUIRED")
        self.assertEqual(output["reason"], "fresh_codex_review_missing")
        self.assertIn("context_hash", output)

    def test_trade_proposal_blocks_codex_avoid(self):
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review("AVOID")):
            output = hermes_operator._trade_proposal_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
            )

        self.assertEqual(output["status"], "NO_PROPOSAL")
        self.assertEqual(output["reason"], "codex_review_avoid")

    def test_codex_review_context_is_compact_and_hashed(self):
        output = hermes_operator._codex_review_context_output(
            self.sample_proposal_report(),
            policy=self.sample_policy(),
            symbol="SBER@MISX",
        )

        self.assertEqual(output["status"], "OK")
        self.assertLessEqual(output["bytes"], 4096)
        self.assertEqual(output["context"]["symbol"], "SBER@MISX")
        self.assertIn("context_hash", output["context"])
        self.assertNotIn("arena_execution_ledger", json.dumps(output["context"].get("recent_outcomes"), ensure_ascii=False))

    def test_codex_review_parser_rejects_markdown_and_accepts_schema(self):
        valid = hermes_operator._parse_codex_review_json(
            '{"verdict":"OK","confidence":91,"reasons":["нет блокирующих факторов"],"blocking_flags":[],"summary":"Можно продолжить risk checks."}'
        )

        self.assertEqual(valid["verdict"], "OK")
        with self.assertRaises(ValueError):
            hermes_operator._parse_codex_review_json("```json\n{}\n```")

    def test_trade_proposal_cli_does_not_send_or_submit(self):
        with mock.patch.object(hermes_operator.h4_monitor, "build_report", return_value=self.sample_proposal_report()), mock.patch.object(
            hermes_operator.h4_monitor, "load_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()
        ), mock.patch.object(
            hermes_operator, "write_event"
        ) as write_event, mock.patch.object(
            sys, "argv", ["hermes_operator.py", "trade-proposal", "--symbol", "SBER@MISX"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["command"], "trade-proposal")
        self.assertEqual(payload["status"], "PROPOSE_ONLY")
        self.assertFalse(payload["safety"]["trading_mutations"])
        self.assertFalse(payload["safety"]["production_send"])
        write_event.assert_called_once()

    def test_trade_confirm_requires_exact_confirmation_phrase(self):
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_confirm_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="CONFIRM_BUY GAZP@MISX",
            )

        self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(output["required_confirmation"], "CONFIRM_BUY SBER@MISX")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_confirm_returns_dry_run_order_and_stop_plan(self):
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_confirm_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="CONFIRM_BUY SBER@MISX",
            )

        self.assertEqual(output["status"], "DRY_RUN")
        self.assertEqual(output["planned_order"]["status"], "dry_run")
        self.assertEqual(output["planned_order"]["proposal"]["ticker"], "SBER@MISX")
        self.assertEqual(output["planned_order"]["proposal"]["side"], "buy")
        self.assertEqual(output["planned_order"]["proposal"]["quantity"], 601)
        self.assertEqual(output["protective_stop_plan"]["side"], "SELL")
        self.assertEqual(output["protective_stop_plan"]["quantity"], 601)
        self.assertEqual(output["protective_stop_plan"]["stop_price"], "320.60")
        self.assertFalse(output["protective_stop_plan"]["live_submission_enabled"])
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertFalse(output["safety"]["production_send"])

    def test_trade_confirm_refuses_when_no_active_proposal(self):
        output = hermes_operator._trade_confirm_output(
            self.sample_report(),
            policy=self.sample_policy(),
            symbol="PLZL@MISX",
            confirmation="CONFIRM_BUY PLZL@MISX",
        )

        self.assertEqual(output["status"], "NO_ORDER")
        self.assertEqual(output["reason"], "no_active_trade_proposal")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_confirm_cli_is_dry_run_only(self):
        with mock.patch.object(hermes_operator.h4_monitor, "build_report", return_value=self.sample_proposal_report()), mock.patch.object(
            hermes_operator.h4_monitor, "load_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()
        ), mock.patch.object(
            hermes_operator, "write_event"
        ) as write_event, mock.patch.object(
            sys,
            "argv",
            ["hermes_operator.py", "trade-confirm", "--symbol", "SBER@MISX", "--confirmation", "CONFIRM_BUY SBER@MISX"],
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["command"], "trade-confirm")
        self.assertEqual(payload["status"], "DRY_RUN")
        self.assertFalse(payload["safety"]["trading_mutations"])
        self.assertFalse(payload["safety"]["production_send"])
        write_event.assert_called_once()

    def test_trade_execute_demo_requires_live_gate(self):
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_execute_demo_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="CONFIRM_BUY SBER@MISX",
                live=False,
                env={"FINAM_TOKEN": "secret", "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT"},
            )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["reason"], "missing_--live")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_execute_demo_requires_env_gate(self):
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_execute_demo_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="CONFIRM_BUY SBER@MISX",
                live=True,
                env={"FINAM_TOKEN": "secret", "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT"},
            )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["reason"], "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS_not_true")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_execute_demo_refuses_wrong_confirmation(self):
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_execute_demo_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="купить сбер",
                live=True,
                env={
                    "FINAM_TOKEN": "secret",
                    "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                    "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
                },
            )

        self.assertEqual(output["status"], "NO_ORDER")
        self.assertEqual(output["confirm_status"], "CONFIRMATION_REQUIRED")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_execute_demo_refuses_blocked_candidate(self):
        output = hermes_operator._trade_execute_demo_output(
            self.sample_report(),
            policy=self.sample_policy(),
            symbol="PLZL@MISX",
            confirmation="CONFIRM_BUY PLZL@MISX",
            live=True,
            env={
                "FINAM_TOKEN": "secret",
                "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
            },
        )

        self.assertEqual(output["status"], "NO_ORDER")
        self.assertEqual(output["confirm_status"], "NO_ORDER")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_execute_demo_blocks_held_scale_in_live_execution(self):
        class FakeFinamClient:
            def create_session(self, token):
                raise AssertionError("scale-in block must happen before broker session")

        report = self.sample_proposal_report()
        report["candidates"][0]["symbol"] = "MOEX@MISX"
        report["candidates"][0]["candidate_source"] = "held_scale_in"
        report["candidates"][0]["scale_in"] = {
            "current_quantity": "710",
            "add_quantity": "580",
            "projected_total_quantity": "1290",
        }

        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_execute_demo_output(
                report,
                policy=self.sample_policy(),
                symbol="MOEX@MISX",
                confirmation="CONFIRM_BUY MOEX@MISX",
                live=True,
                client=FakeFinamClient(),
                env={
                    "FINAM_TOKEN": "secret",
                    "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                    "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
                },
            )

        self.assertEqual(output["status"], "BLOCKED")
        self.assertEqual(output["reason"], "held_scale_in_live_execution_not_supported")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_execute_demo_places_buy_stop_and_verifies_watching_stop(self):
        class FakeFinamClient:
            def __init__(self):
                self.buy_payloads = []
                self.stop_payloads = []

            def create_session(self, token):
                self.token = token
                return "jwt"

            def place_order(self, jwt, account_id, payload):
                self.buy_payloads.append((jwt, account_id, payload))
                return {"order_id": "buy-1", "status": "ORDER_STATUS_NEW"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_FILLED", "executed_quantity": {"value": "601.0"}}

            def place_sltp_order(self, jwt, account_id, payload):
                self.stop_payloads.append((jwt, account_id, payload))
                return {"order_id": "sl-1", "status": "ORDER_STATUS_WATCHING"}

            def asset(self, jwt, symbol, *, account_id):
                return {"lot_size": {"value": "1.0"}, "decimals": 2, "min_step": "1"}

            def asset_params(self, jwt, symbol, *, account_id):
                return {"is_tradable": {"value": True}, "longable": {"value": "AVAILABLE"}}

            def orders(self, jwt, account_id):
                return {
                    "orders": [
                        {
                            "order_id": "sl-1",
                            "status": "ORDER_STATUS_WATCHING",
                            "symbol": "SBER@MISX",
                            "side": "SIDE_SELL",
                            "quantity_sl": {"value": "601"},
                            "sl_price": {"value": "320.60"},
                        }
                    ]
                }

        client = FakeFinamClient()
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_execute_demo_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="CONFIRM_BUY SBER@MISX",
                live=True,
                client=client,
                env={
                    "FINAM_TOKEN": "secret",
                    "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                    "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
                },
            )

        self.assertEqual(output["status"], "EXECUTED_DEMO")
        self.assertEqual(client.buy_payloads[0][2]["side"], "SIDE_BUY")
        self.assertEqual(client.buy_payloads[0][2]["type"], "ORDER_TYPE_LIMIT")
        self.assertEqual(client.buy_payloads[0][2]["time_in_force"], "TIME_IN_FORCE_DAY")
        self.assertEqual(client.buy_payloads[0][2]["quantity"], {"value": "601.0"})
        self.assertEqual(client.buy_payloads[0][2]["limit_price"], {"value": "323.49"})
        self.assertIn("client_order_id", client.buy_payloads[0][2])
        self.assertNotIn("price", client.buy_payloads[0][2])
        self.assertEqual(client.stop_payloads[0][2]["side"], "SIDE_SELL")
        self.assertEqual(client.stop_payloads[0][2]["quantity_sl"], {"value": "601.0"})
        self.assertEqual(client.stop_payloads[0][2]["sl_price"], {"value": "320.60"})
        self.assertIn("client_order_id", client.stop_payloads[0][2])
        self.assertTrue(output["stop_verification"]["verified"])
        self.assertIn("telegram_summary", output)
        self.assertIn("✅ BUY выполнен / DEMO", output["telegram_summary"])
        self.assertIn("🛡️ SL: 320.60", output["telegram_summary"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertFalse(output["safety"]["production_send"])
        json.dumps(output, ensure_ascii=False)

    def test_trade_buy_accepts_direct_russian_intent_and_executes_demo_flow(self):
        class FakeFinamClient:
            def __init__(self):
                self.buy_payloads = []

            def create_session(self, token):
                return "jwt"

            def place_order(self, jwt, account_id, payload):
                self.buy_payloads.append(payload)
                return {"order_id": "buy-1", "status": "ORDER_STATUS_NEW"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_FILLED", "executed_quantity": {"value": "601.0"}}

            def place_sltp_order(self, jwt, account_id, payload):
                return {"order_id": "sl-1", "status": "ORDER_STATUS_WATCHING"}

            def asset(self, jwt, symbol, *, account_id):
                return {"lot_size": {"value": "1.0"}, "decimals": 2, "min_step": "1"}

            def asset_params(self, jwt, symbol, *, account_id):
                return {"is_tradable": {"value": True}, "longable": {"value": "AVAILABLE"}}

            def orders(self, jwt, account_id):
                return {
                    "orders": [
                        {
                            "order_id": "sl-1",
                            "status": "ORDER_STATUS_WATCHING",
                            "symbol": "SBER@MISX",
                            "side": "SIDE_SELL",
                            "quantity_sl": {"value": "601"},
                            "sl_price": {"value": "320.60"},
                        }
                    ]
                }

        client = FakeFinamClient()
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_buy_output(
                self.sample_proposal_report(),
                symbol="SBER@MISX",
                intent="купи SBER",
                confirmation="CONFIRM_BUY SBER@MISX",
                live=True,
                client=client,
                env={
                    "FINAM_TOKEN": "secret",
                    "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                    "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
                },
            )

        self.assertEqual(output["status"], "EXECUTED_DEMO")
        self.assertEqual(output["command"], "trade-buy")
        self.assertEqual(output["delegated_command"], "trade-execute-demo")
        self.assertEqual(client.buy_payloads[0]["limit_price"], {"value": "323.49"})
        self.assertTrue(output["safety"]["trading_mutations"])

    def test_trade_buy_rejects_ambiguous_or_mismatched_intent(self):
        output = hermes_operator._trade_buy_output(
            self.sample_proposal_report(),
            symbol="SBER@MISX",
            intent="купи GAZP",
            live=True,
            env={
                "FINAM_TOKEN": "secret",
                "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
            },
        )

        self.assertEqual(output["status"], "INTENT_REQUIRED")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_buy_does_not_call_broker_after_provider_client_failure(self):
        report = self.sample_proposal_report()
        report["research"] = {
            "status": "failed",
            "provider": "openrouter",
            "classification": "provider_client_failure",
            "reason": "provider_client_typeerror",
            "error": "Provider client failed before producing a response; no trading signal was generated.",
            "symbols": ["SBER@MISX"],
        }
        client = mock.Mock()

        output = hermes_operator._trade_buy_output(
            report,
            symbol="SBER@MISX",
            intent="купи SBER",
            live=True,
            client=client,
            env={
                "FINAM_TOKEN": "secret",
                "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
            },
        )

        self.assertEqual(output["status"], "PROVIDER_FAILURE_NO_BROKER_COMMAND")
        self.assertFalse(output["safety"]["trading_mutations"])
        client.create_session.assert_not_called()

    def test_autonomous_run_requires_autonomous_policy_mode(self):
        policy = self.sample_policy()
        report = self.sample_proposal_report()

        output = hermes_operator._autonomous_run_output(
            report,
            policy=policy,
            live=True,
            env={"FINAM_H4_AUTONOMOUS_DEMO_ENABLED": "true"},
            runtime_check=lambda: {"status": "OK", "errors": [], "warnings": [], "checks": {}},
        )

        self.assertEqual(output["status"], "BLOCKED")
        self.assertEqual(output["reason"], "policy_mode_not_autonomous_demo")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_autonomous_run_requires_autonomous_env_gate(self):
        policy = self.sample_policy()
        policy["mode"] = "autonomous_demo"

        output = hermes_operator._autonomous_run_output(
            self.sample_proposal_report(),
            policy=policy,
            live=True,
            env={},
            runtime_check=lambda: {"status": "OK", "errors": [], "warnings": [], "checks": {}},
        )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["reason"], "FINAM_H4_AUTONOMOUS_DEMO_ENABLED_not_true")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_autonomous_run_blocks_on_runtime_preflight(self):
        policy = self.sample_policy()
        policy["mode"] = "autonomous_demo"

        output = hermes_operator._autonomous_run_output(
            self.sample_proposal_report(),
            policy=policy,
            live=True,
            env={"FINAM_H4_AUTONOMOUS_DEMO_ENABLED": "true"},
            runtime_check=lambda: {
                "status": "NO_TRADE",
                "errors": ["trade safety state blocks new buys"],
                "warnings": [],
                "checks": {},
            },
        )

        self.assertEqual(output["status"], "BLOCKED")
        self.assertEqual(output["reason"], "runtime_preflight_not_ok")
        self.assertIn("trade safety state blocks new buys", output["runtime_errors"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_autonomous_run_does_not_buy_when_research_unavailable(self):
        policy = self.sample_policy()
        policy["mode"] = "autonomous_demo"
        report = self.sample_proposal_report()
        report["research"] = {"status": "unavailable", "verdict": "UNAVAILABLE", "symbols": ["SBER@MISX"]}
        client = mock.Mock()

        output = hermes_operator._autonomous_run_output(
            report,
            policy=policy,
            live=True,
            client=client,
            env={"FINAM_H4_AUTONOMOUS_DEMO_ENABLED": "true"},
            runtime_check=lambda: {"status": "OK", "errors": [], "warnings": [], "checks": {}},
        )

        self.assertEqual(output["status"], "NO_ORDER")
        self.assertEqual(output["reason"], "research_not_autonomous_eligible")
        self.assertFalse(output["safety"]["trading_mutations"])
        client.create_session.assert_not_called()

    def test_autonomous_run_rejects_dynamic_and_returns_promote_command(self):
        policy = self.sample_policy()
        policy["mode"] = "autonomous_demo"
        report = self.sample_proposal_report()
        report["candidates"][0]["symbol"] = "AFLT@MISX"
        report["candidates"][0]["candidate_source"] = "dynamic"
        report["candidates"][0]["actionable_this_run"] = True
        report["research"]["symbols"] = ["AFLT@MISX"]

        output = hermes_operator._autonomous_run_output(
            report,
            policy=policy,
            live=True,
            env={"FINAM_H4_AUTONOMOUS_DEMO_ENABLED": "true"},
            runtime_check=lambda: {"status": "OK", "errors": [], "warnings": [], "checks": {}},
        )

        self.assertEqual(output["status"], "NO_ORDER")
        self.assertEqual(output["reason"], "no_autonomous_candidate")
        self.assertEqual(output["dynamic_promotions"][0]["symbol"], "AFLT@MISX")
        self.assertIn("--add-universe AFLT@MISX", output["dynamic_promotions"][0]["apply_command"])

    def test_autonomous_run_executes_clean_static_candidate_via_guarded_demo_path(self):
        class FakeFinamClient:
            def __init__(self):
                self.buy_payloads = []

            def create_session(self, token):
                return "jwt"

            def place_order(self, jwt, account_id, payload):
                self.buy_payloads.append(payload)
                return {"order_id": "buy-1", "status": "ORDER_STATUS_NEW"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_FILLED", "executed_quantity": {"value": "601.0"}}

            def place_sltp_order(self, jwt, account_id, payload):
                return {"order_id": "sl-1", "status": "ORDER_STATUS_WATCHING"}

            def asset(self, jwt, symbol, *, account_id):
                return {"lot_size": {"value": "1.0"}, "decimals": 2, "min_step": "1"}

            def asset_params(self, jwt, symbol, *, account_id):
                return {"is_tradable": {"value": True}, "longable": {"value": "AVAILABLE"}}

            def orders(self, jwt, account_id):
                return {
                    "orders": [
                        {
                            "order_id": "sl-1",
                            "status": "ORDER_STATUS_WATCHING",
                            "symbol": "SBER@MISX",
                            "side": "SIDE_SELL",
                            "quantity_sl": {"value": "601"},
                            "sl_price": {"value": "320.60"},
                        }
                    ]
                }

        policy = self.sample_policy()
        policy["mode"] = "autonomous_demo"
        report = self.sample_proposal_report()
        report["candidates"][0]["candidate_source"] = "static"
        report["candidates"][0]["actionable_this_run"] = True
        report["research"]["verdict"] = "OK"
        report["research"]["symbols"] = ["SBER@MISX"]
        client = FakeFinamClient()

        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review("OK")):
            output = hermes_operator._autonomous_run_output(
                report,
                policy=policy,
                live=True,
                client=client,
                env={
                    "FINAM_TOKEN": "secret",
                    "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                    "FINAM_H4_AUTONOMOUS_DEMO_ENABLED": "true",
                    "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
                },
                runtime_check=lambda: {"status": "OK", "errors": [], "warnings": [], "checks": {}},
            )

        self.assertEqual(output["status"], "EXECUTED_DEMO")
        self.assertEqual(output["command"], "autonomous-run")
        self.assertEqual(output["delegated_command"], "trade-execute-demo")
        self.assertEqual(output["autonomous"]["selected_symbol"], "SBER@MISX")
        self.assertEqual(client.buy_payloads[0]["limit_price"], {"value": "323.49"})
        self.assertTrue(output["safety"]["trading_mutations"])

    def test_trade_execute_demo_halts_when_stop_not_watching(self):
        class FakeFinamClient:
            def create_session(self, token):
                return "jwt"

            def place_order(self, jwt, account_id, payload):
                return {"order_id": "buy-1", "status": "ORDER_STATUS_NEW"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_FILLED", "executed_quantity": {"value": "601.0"}}

            def place_sltp_order(self, jwt, account_id, payload):
                return {"order_id": "sl-1", "status": "ORDER_STATUS_NEW"}

            def asset(self, jwt, symbol, *, account_id):
                return {"lot_size": {"value": "1.0"}, "decimals": 2, "min_step": "1"}

            def asset_params(self, jwt, symbol, *, account_id):
                return {"is_tradable": {"value": True}, "longable": {"value": "AVAILABLE"}}

            def orders(self, jwt, account_id):
                return {"orders": []}

        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_execute_demo_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="CONFIRM_BUY SBER@MISX",
                live=True,
                client=FakeFinamClient(),
                env={
                    "FINAM_TOKEN": "secret",
                    "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                    "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
                },
            )

        self.assertEqual(output["status"], "STOP_NOT_VERIFIED")
        self.assertTrue(output["halt_new_buys"])
        self.assertTrue(output["safety"]["trading_mutations"])

    def test_trade_execute_demo_does_not_place_stop_until_buy_is_filled(self):
        class FakeFinamClient:
            def __init__(self):
                self.stop_calls = 0

            def create_session(self, token):
                return "jwt"

            def asset(self, jwt, symbol, *, account_id):
                return {"lot_size": {"value": "1.0"}, "decimals": 2, "min_step": "1"}

            def asset_params(self, jwt, symbol, *, account_id):
                return {"is_tradable": {"value": True}, "longable": {"value": "AVAILABLE"}}

            def place_order(self, jwt, account_id, payload):
                return {"order_id": "buy-1", "status": "ORDER_STATUS_NEW"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_NEW", "executed_quantity": {"value": "0.0"}}

            def place_sltp_order(self, jwt, account_id, payload):
                self.stop_calls += 1
                return {}

        client = FakeFinamClient()
        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_execute_demo_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="CONFIRM_BUY SBER@MISX",
                live=True,
                client=client,
                env={
                    "FINAM_TOKEN": "secret",
                    "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                    "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                    "FINAM_BUY_FILL_CHECKS": "1",
                    "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
                    "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
                },
            )

        self.assertEqual(output["status"], "BUY_PENDING_NO_SL")
        self.assertEqual(client.stop_calls, 0)
        self.assertTrue(output["halt_new_buys"])

    def test_trade_execute_demo_blocks_when_finam_params_not_longable(self):
        class FakeFinamClient:
            def create_session(self, token):
                return "jwt"

            def asset(self, jwt, symbol, *, account_id):
                return {"lot_size": {"value": "1.0"}, "decimals": 2, "min_step": "1"}

            def asset_params(self, jwt, symbol, *, account_id):
                return {"is_tradable": {"value": True}, "longable": {"value": "NOT_AVAILABLE"}}

        with mock.patch.object(hermes_operator, "_load_fresh_codex_review", return_value=self.sample_review()):
            output = hermes_operator._trade_execute_demo_output(
                self.sample_proposal_report(),
                policy=self.sample_policy(),
                symbol="SBER@MISX",
                confirmation="CONFIRM_BUY SBER@MISX",
                live=True,
                client=FakeFinamClient(),
                env={
                    "FINAM_TOKEN": "secret",
                    "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
                    "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(Path(tempfile.mkdtemp()) / "safety.json"),
                },
            )

        self.assertEqual(output["status"], "BROKER_ERROR")
        self.assertEqual(output["reason"], "finam_contract_validation_failed")
        self.assertIn("longable", output["error"])


    def test_arena_learning_update_writes_decision_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision_path = Path(tmp) / "decisions.jsonl"
            outcome_path = Path(tmp) / "outcomes.jsonl"
            suggestion_path = Path(tmp) / "suggestions.jsonl"
            policy = self.sample_arena_policy() | {"learning": {"enabled": True, "mode": "propose_only", "objective": "risk_adjusted_growth", "min_samples_for_suggestion": 20}}
            scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
            with mock.patch.object(hermes_operator, "DEFAULT_ARENA_LEARNING_EVENTS_PATH", decision_path), mock.patch.object(
                hermes_operator, "DEFAULT_ARENA_LEARNING_OUTCOMES_PATH", outcome_path
            ), mock.patch.object(hermes_operator, "DEFAULT_ARENA_LEARNING_SUGGESTIONS_PATH", suggestion_path), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(hermes_operator, "_read_arena_execution_ledger", return_value=[]):
                output = hermes_operator._arena_learning_update_output(policy, policy_path=Path("arena.json"))

            self.assertEqual(output["status"], "OK")
            self.assertEqual(output["decisions_written"], 1)
            self.assertFalse(output["safety"]["trading_mutations"])
            item = json.loads(decision_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(item["symbol"], "SBER@MISX")

    def test_arena_learning_propose_is_report_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision_path = Path(tmp) / "decisions.jsonl"
            outcome_path = Path(tmp) / "outcomes.jsonl"
            suggestion_path = Path(tmp) / "suggestions.jsonl"
            for index in range(3):
                with decision_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"decision_id": str(index), "symbol": "SBER@MISX", "decision_status": "BLOCKED", "gate_reasons": ["candidate_score_below_min"]}) + "\n")
            outcome_path.write_text(json.dumps({"symbol": "SBER@MISX", "net_pnl_rub": "-25"}) + "\n", encoding="utf-8")
            policy = self.sample_arena_policy() | {"learning": {"enabled": True, "mode": "propose_only", "objective": "risk_adjusted_growth", "min_samples_for_suggestion": 2}}
            with mock.patch.object(hermes_operator, "DEFAULT_ARENA_LEARNING_EVENTS_PATH", decision_path), mock.patch.object(
                hermes_operator, "DEFAULT_ARENA_LEARNING_OUTCOMES_PATH", outcome_path
            ), mock.patch.object(hermes_operator, "DEFAULT_ARENA_LEARNING_SUGGESTIONS_PATH", suggestion_path):
                output = hermes_operator._arena_learning_propose_output(policy, policy_path=Path("arena.json"))

            self.assertEqual(output["status"], "PROPOSED")
            self.assertFalse(output["safety"]["policy_write"])
            self.assertTrue(suggestion_path.exists())

    def test_arena_learning_apply_requires_confirmation(self):
        output = hermes_operator._arena_learning_apply_output(
            self.sample_arena_policy(),
            policy_path=Path("arena.json"),
            confirm="",
            backup_dir=None,
        )

        self.assertEqual(output["status"], "BLOCKED")
        self.assertEqual(output["required_confirmation"], "APPLY_ARENA_LEARNING")
        self.assertFalse(output["safety"]["policy_write"])

    def test_arena_learning_apply_reports_policy_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "arena.json"
            policy = self.sample_arena_policy()
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            proposal = {
                "status": "PROPOSED",
                "patch": {"learning": {"last_objective": "risk_adjusted_growth"}},
                "report": {},
            }
            with mock.patch.object(hermes_operator, "build_learning_proposal", return_value=proposal):
                output = hermes_operator._arena_learning_apply_output(
                    policy,
                    policy_path=policy_path,
                    confirm="APPLY_ARENA_LEARNING",
                    backup_dir=Path(tmp) / "backups",
                )

        self.assertEqual(output["status"], "APPLIED")
        self.assertTrue(output["write_applied"])
        self.assertTrue(output["safety"]["policy_write"])
        self.assertEqual(output["applied_policy"]["learning"]["last_objective"], "risk_adjusted_growth")

    def test_arena_llm_context_includes_learning_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision_path = Path(tmp) / "decisions.jsonl"
            outcome_path = Path(tmp) / "outcomes.jsonl"
            suggestion_path = Path(tmp) / "suggestions.jsonl"
            policy = self.sample_arena_policy() | {"learning": {"enabled": True, "mode": "propose_only", "objective": "risk_adjusted_growth", "min_samples_for_suggestion": 20}}
            status = {"status": "NO_TRADE", "mode": "approval", "accounts": [], "errors": [], "warnings": []}
            with mock.patch.object(hermes_operator, "DEFAULT_ARENA_LEARNING_EVENTS_PATH", decision_path), mock.patch.object(
                hermes_operator, "DEFAULT_ARENA_LEARNING_OUTCOMES_PATH", outcome_path
            ), mock.patch.object(hermes_operator, "DEFAULT_ARENA_LEARNING_SUGGESTIONS_PATH", suggestion_path), mock.patch.object(
                hermes_operator, "build_arena_status", return_value=status
            ):
                output = hermes_operator._arena_llm_context_output(policy, policy_path=Path("arena.json"))

            self.assertIn("learning_summary", output)
            self.assertEqual(output["learning_summary"]["mode"], "propose_only")

    def test_arena_llm_context_does_not_use_mcp_shadow_by_default(self):
        policy = self.sample_arena_policy()
        status = {"status": "OK", "mode": "approval", "accounts": [], "errors": [], "warnings": []}
        with mock.patch.object(hermes_operator, "build_arena_status", return_value=status), mock.patch.object(
            hermes_operator, "build_mcp_shadow_review", side_effect=AssertionError("MCP shadow must be opt-in")
        ):
            output = hermes_operator._arena_llm_context_output(policy, policy_path=Path("arena.json"))

        self.assertNotIn("mcp_shadow", output)

    def test_arena_llm_context_can_include_mcp_shadow_without_trading_effect(self):
        policy = self.sample_arena_policy()
        status = {"status": "OK", "mode": "approval", "accounts": [], "errors": [], "warnings": []}
        scan = {"status": "OK", "accounts": [], "candidates": [{"account_id": "DEMO-RU", "symbol": "SBER@MISX"}]}
        shadow = {
            "status": "MCP_UNAVAILABLE",
            "trading_gate_effect": "none",
            "candidate_context_notes": [{"account_id": "DEMO-RU", "symbol": "SBER@MISX", "mcp_unavailable": True}],
            "raw_content_omitted": True,
        }
        with mock.patch.object(hermes_operator, "build_arena_status", return_value=status), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(hermes_operator, "build_mcp_shadow_review", return_value=shadow):
            output = hermes_operator._arena_llm_context_output(policy, policy_path=Path("arena.json"), include_mcp_shadow=True)

        self.assertEqual(output["mcp_shadow"]["status"], "MCP_UNAVAILABLE")
        self.assertEqual(output["mcp_shadow"]["trading_gate_effect"], "none")

    def test_arena_mcp_shadow_review_cli_routes_to_readonly_output(self):
        shadow = {
            "status": "OK",
            "command": "arena-mcp-shadow-review",
            "safety": {"trading_mutations": False},
            "raw_content_omitted": True,
        }
        with mock.patch.object(hermes_operator, "load_arena_policy", return_value=self.sample_arena_policy()), mock.patch.object(
            hermes_operator, "_arena_mcp_shadow_review_output", return_value=shadow
        ) as route, mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "--arena-policy", "arena.json", "arena-mcp-shadow-review", "--max-bytes", "4000"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        self.assertEqual(result, 0)
        route.assert_called_once()
        self.assertEqual(route.call_args.kwargs["max_bytes"], 4000)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["command"], "arena-mcp-shadow-review")
        self.assertFalse(payload["safety"]["trading_mutations"])

    def test_policy_check_cli_is_read_only_and_writes_journal(self):
        with mock.patch.object(hermes_operator.h4_monitor, "load_policy", return_value={"mode": "supervised"}), mock.patch.object(
            hermes_operator.h4_monitor, "_public_policy", return_value={"mode": "supervised"}
        ), mock.patch.object(hermes_operator, "write_event") as write_event, mock.patch.object(
            sys, "argv", ["hermes_operator.py", "--policy", "policy.json", "policy-check"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["command"], "policy-check")
        self.assertTrue(payload["safety"]["read_only_operator_cli"])
        self.assertFalse(payload["safety"]["trading_mutations"])
        write_event.assert_called_once()

    def test_arena_cli_journal_uses_arena_policy_path(self):
        with mock.patch.object(hermes_operator, "load_arena_policy", return_value=self.sample_arena_policy()), mock.patch.object(
            hermes_operator,
            "_arena_status_output",
            return_value={"status": "OK", "command": "arena-status", "safety": {"trading_mutations": False}},
        ), mock.patch.object(hermes_operator, "write_event") as write_event, mock.patch.object(
            sys, "argv", ["hermes_operator.py", "--policy", "h4.json", "--arena-policy", "arena.json", "arena-status"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        self.assertEqual(result, 0)
        event = write_event.call_args.args[0]
        self.assertEqual(event["policy_path"], "arena.json")

    def test_report_preview_cli_does_not_send_telegram(self):
        with mock.patch.object(hermes_operator.h4_monitor, "build_report", return_value=self.sample_report()), mock.patch.object(
            hermes_operator.h4_monitor_notify, "format_telegram_report", return_value="report text"
        ), mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "report-preview"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["telegram_delivery"], "dry_run")
        self.assertEqual(payload["telegram_report"], "report text")

    def test_arena_status_price_enrichment_adds_current_prices_for_button_snapshot(self):
        report = {
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "positions": [
                        {"symbol": "SBER@MISX", "quantity": "1553", "average_price": "322.22", "current_price": None}
                    ],
                }
            ],
            "warnings": [],
        }

        class FakeFinamClient:
            def create_session(self, token):
                self.token = token
                return "jwt"

            def last_quote(self, jwt, symbol):
                self.jwt = jwt
                self.symbol = symbol
                return {"last": {"value": "324.40"}}

        with mock.patch.object(hermes_operator, "FinamClient", FakeFinamClient):
            hermes_operator._enrich_arena_status_position_prices(report, env={"FINAM_TOKEN": "token"})

        self.assertEqual(report["accounts"][0]["positions"][0]["current_price"], "324.40")

    def test_arena_status_cli_returns_compact_status_without_telegram_payload(self):
        arena_report = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "label": "РФ", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-US", "label": "США", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-AI", "label": "AI", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
            ],
            "errors": [],
            "warnings": [],
        }
        with mock.patch.object(hermes_operator, "load_arena_policy", return_value={"mode": "approval"}), mock.patch.object(
            hermes_operator, "build_arena_status", return_value=arena_report
        ), mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "arena-status"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["command"], "arena-status")
        self.assertFalse(payload["full_json"])
        self.assertEqual(len(payload["accounts"]), 3)
        self.assertIn("generated_at", payload)
        self.assertIn("full_detail_commands", payload)
        self.assertNotIn("telegram_pulse", payload)
        self.assertNotIn("telegram_reply_markup", payload)
        self.assertFalse(payload["safety"]["trading_mutations"])

    def test_arena_status_cli_full_json_keeps_telegram_payload(self):
        arena_report = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "label": "РФ", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-US", "label": "США", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-AI", "label": "AI", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
            ],
            "errors": [],
            "warnings": [],
        }
        snapshot_path = Path(self._runtime_tmp.name) / "telegram_snapshot_full.json"
        with mock.patch.dict(os.environ, {"FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH": str(snapshot_path)}), mock.patch.object(
            hermes_operator, "load_arena_policy", return_value={"mode": "approval"}
        ), mock.patch.object(
            hermes_operator, "build_arena_status", return_value=arena_report
        ), mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "arena-status", "--full-json"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertTrue(payload["full_json"])
        self.assertIn("Finam Arena Pulse", payload["telegram_pulse"])
        self.assertIn("telegram_reply_markup", payload)
        self.assertEqual(payload["snapshot_status"], "written")
        self.assertTrue(snapshot_path.exists())

    def test_arena_status_does_not_overwrite_snapshot_without_accounts(self):
        arena_report = {"status": "NO_TRADE", "mode": "approval", "accounts": [], "errors": ["Arena unavailable"], "warnings": []}
        snapshot_path = Path(self._runtime_tmp.name) / "telegram_snapshot_empty.json"
        with mock.patch.dict(os.environ, {"FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH": str(snapshot_path)}), mock.patch.object(
            hermes_operator, "load_arena_policy", return_value={"mode": "approval"}
        ), mock.patch.object(
            hermes_operator, "build_arena_status", return_value=arena_report
        ), mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "arena-status", "--full-json"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["snapshot_status"], "skipped_invalid_payload")
        self.assertFalse(snapshot_path.exists())

    def test_arena_status_cli_can_send_telegram_with_buttons(self):
        arena_report = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "label": "РФ", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-US", "label": "США", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-AI", "label": "AI", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
            ],
            "errors": [],
            "warnings": [],
        }
        snapshot_path = Path(self._runtime_tmp.name) / "telegram_snapshot.json"
        with mock.patch.dict(
            os.environ,
            {
                "FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH": str(snapshot_path),
                "TELEGRAM_BOT_TOKEN": "secret-token-that-must-not-be-written",
            },
        ), mock.patch.object(hermes_operator, "load_arena_policy", return_value={"mode": "approval"}), mock.patch.object(
            hermes_operator, "build_arena_status", return_value=arena_report
        ), mock.patch.object(hermes_operator.h4_monitor_notify, "send_telegram_message") as send, mock.patch.object(
            hermes_operator, "write_event"
        ), mock.patch.object(sys, "argv", ["hermes_operator.py", "arena-status", "--send-telegram"]):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["telegram_delivery"], "ok")
        self.assertEqual(payload["snapshot_status"], "written")
        send.assert_called_once()
        self.assertIn("reply_markup", send.call_args.kwargs)
        self.assertIn("arena:overview", json.dumps(send.call_args.kwargs["reply_markup"], ensure_ascii=False))
        snapshot_text = snapshot_path.read_text(encoding="utf-8")
        snapshot = json.loads(snapshot_text)
        self.assertEqual(snapshot["source_command"], "arena-status")
        self.assertEqual(len(snapshot["accounts"]), 3)
        self.assertNotIn("secret-token-that-must-not-be-written", snapshot_text)

    def test_arena_llm_context_is_compact_and_drops_telegram_fields(self):
        policy = self.sample_arena_policy()
        status = {
            "status": "OK",
            "accounts": [
                {"account_id": "DEMO-RU", "positions": [{"symbol": "SBER@MISX", "note": "x" * 1000}]},
                {"account_id": "DEMO-US", "positions": []},
                {"account_id": "DEMO-AI", "positions": []},
            ],
            "telegram_pulse": "heavy telegram text",
            "errors": [],
            "warnings": [],
        }
        scan = {
            "status": "OK",
            "candidates": [{"symbol": f"SBER{i}@MISX", "why": "y" * 1000} for i in range(10)],
            "telegram_reply_markup": {"inline_keyboard": []},
        }
        review = {
            "status": "OK",
            "actions": [{"symbol": f"GAZP{i}@MISX", "thesis": "z" * 1000} for i in range(10)],
            "telegram_text": "heavy report text",
        }
        with mock.patch.object(hermes_operator, "build_arena_status", return_value=status), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(hermes_operator, "build_arena_portfolio_review", return_value=review), mock.patch.object(
            hermes_operator, "_read_arena_execution_ledger", return_value=[]
        ), mock.patch.object(
            hermes_operator, "_arena_soft_stop_records", return_value=[]
        ), mock.patch.object(
            hermes_operator, "_arena_pending_approval_summaries", return_value=[]
        ):
            output = hermes_operator._arena_llm_context_output(policy, policy_path=Path("arena.json"), max_bytes=8000)

        payload = json.dumps(output, ensure_ascii=False)
        self.assertEqual(output["command"], "arena-llm-context")
        self.assertEqual(len(output["accounts"]), 3)
        self.assertLessEqual(len(payload.encode("utf-8")), 8500)
        self.assertNotIn('"telegram_pulse":', payload)
        self.assertNotIn('"telegram_text":', payload)
        self.assertNotIn('"telegram_reply_markup":', payload)
        self.assertIn("telegram_pulse", output["omitted_fields"])
        self.assertIn("execution_route", output)
        self.assertIn("live_gate", output)
        self.assertEqual(output["pending_approvals_count"], 0)
        self.assertTrue(output["execution_route"]["arena_confirm_requires_pending_snapshot"])

    def test_arena_llm_context_reports_live_gate_enabled(self):
        policy = self.sample_arena_policy()
        status = {"status": "OK", "accounts": [], "errors": [], "warnings": []}
        with mock.patch.dict(os.environ, {"FINAM_ARENA_AUTO_TRADE_ENABLED": "true"}):
            output = hermes_operator._arena_compact_status_output(
                status,
                policy=policy,
                policy_path=Path("arena.json"),
                max_bytes=8000,
            )

        self.assertEqual(output["live_gate"]["required_env"], "FINAM_ARENA_AUTO_TRADE_ENABLED")
        self.assertTrue(output["live_gate"]["visible_to_hermes"])
        self.assertTrue(output["live_gate"]["enabled"])
        self.assertNotIn("next_action", output["live_gate"])

    def test_arena_llm_context_reports_live_gate_missing(self):
        policy = self.sample_arena_policy()
        status = {"status": "OK", "accounts": [], "errors": [], "warnings": []}
        with mock.patch.dict(os.environ, {}, clear=True):
            output = hermes_operator._arena_compact_status_output(
                status,
                policy=policy,
                policy_path=Path("arena.json"),
                max_bytes=8000,
            )

        self.assertFalse(output["live_gate"]["visible_to_hermes"])
        self.assertFalse(output["live_gate"]["enabled"])
        self.assertIn("FINAM_ARENA_AUTO_TRADE_ENABLED=true", output["live_gate"]["next_action"])

    def test_arena_llm_context_reports_active_pending_and_execution_route(self):
        policy = self.sample_arena_policy()
        pending = [
            {
                "confirmation": "CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                "account_id": "DEMO-RU",
                "symbol": "SBER@MISX",
                "side": "BUY",
            }
        ]
        status = {"status": "OK", "accounts": [], "pending_approvals": pending, "errors": [], "warnings": []}
        with mock.patch.object(hermes_operator, "build_arena_status", return_value=status):
            output = hermes_operator._arena_compact_status_output(
                status,
                policy=policy,
                policy_path=Path("arena.json"),
                max_bytes=8000,
            )

        self.assertEqual(output["pending_approvals_count"], 1)
        self.assertEqual(output["execution_route"]["route"], "approval_snapshot_required")
        self.assertEqual(output["execution_route"]["active_confirmation_phrases"], ["CONFIRM_ARENA_BUY SBER@MISX DEMO-RU"])


    def test_arena_assets_filters_symbols_without_mutations(self):
        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def assets(self, jwt):
                return {
                    "assets": [
                        {"symbol": "AAPL@XNGS", "ticker": "AAPL", "mic": "XNGS", "isin": "US0378331005", "type": "TYPE_STOCK", "name": "Apple Inc"},
                        {"symbol": "AAPL@SPBXM", "ticker": "AAPL", "mic": "SPBXM", "isin": "US0378331005", "type": "TYPE_STOCK", "name": "Apple Inc"},
                        {"symbol": "MSFT@XNGS", "ticker": "MSFT", "mic": "XNGS", "isin": "US5949181045", "type": "TYPE_STOCK", "name": "Microsoft"},
                    ]
                }

        output = hermes_operator._arena_assets_output(
            query="aapl",
            mics=["XNGS"],
            limit=10,
            client=FakeMarketClient(),
            env={"FINAM_TOKEN": "secret"},
        )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["matches_count"], 1)
        self.assertEqual(output["matches"][0]["symbol"], "AAPL@XNGS")
        self.assertEqual(output["matches"][0]["mic"], "XNGS")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_assets_falls_back_to_market_data_probe(self):
        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def assets(self, jwt):
                raise RuntimeError("assets unavailable")

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                if symbol == "AAPL@XNGS":
                    return {"bars": [{"close": {"value": "200"}}]}
                raise RuntimeError("not found")

        output = hermes_operator._arena_assets_output(
            query="aapl",
            mics=["XNGS"],
            limit=10,
            client=FakeMarketClient(),
            env={"FINAM_TOKEN": "secret"},
        )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["source"], "market_data_probe")
        self.assertEqual(output["matches"][0]["symbol"], "AAPL@XNGS")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_assets_requires_market_data_token(self):
        output = hermes_operator._arena_assets_output(
            query="AAPL",
            mics=[],
            limit=10,
            env={},
        )

        self.assertEqual(output["status"], "NO_DATA")
        self.assertEqual(output["reason"], "FINAM_TOKEN_not_set")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_blocks_without_scan_candidate(self):
        policy = {
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "markets": ["MISX"]},
                {"account_id": "DEMO-US", "markets": ["XNGS"]},
                {"account_id": "DEMO-AI", "markets": ["MISX"], "experimental": True},
            ],
            "risk": {
                "risk_per_trade_pct": "1",
                "max_position_notional_pct": "25",
                "max_daily_loss_pct": "3",
                "max_account_drawdown_pct": "8",
                "max_open_risk_pct": "5",
                "max_open_positions": 8,
                "max_new_trades_per_account_per_run": 1,
            },
        }
        with mock.patch.object(hermes_operator, "load_arena_policy", return_value=policy), mock.patch.object(
            hermes_operator, "build_arena_status", return_value={"status": "OK", "accounts": [], "errors": [], "warnings": []}
        ), mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "arena-run", "--account", "DEMO-RU", "--live"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["command"], "arena-run")
        self.assertEqual(payload["status"], "NO_ORDER")
        self.assertEqual(payload["reason"], "no_active_arena_proposal")
        self.assertFalse(payload["safety"]["trading_mutations"])

    def test_arena_run_all_aggregates_execution_status_and_safety(self):
        policy = self.sample_arena_policy()
        runs = [
            {"status": "EXECUTED_ARENA", "account_id": "DEMO-RU", "safety": {"trading_mutations": True}},
            {"status": "NO_ORDER", "account_id": "DEMO-US", "safety": {"trading_mutations": False}},
            {"status": "NO_ORDER", "account_id": "DEMO-AI", "safety": {"trading_mutations": False}},
        ]

        scan = {"status": "OK", "accounts": [], "candidates": [], "research": {"status": "skipped"}}
        with mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ) as build_scan, mock.patch.object(
            hermes_operator,
            "_arena_portfolio_run_output",
            return_value={"status": "NO_ACTION", "safety": {"trading_mutations": False}},
        ) as portfolio_run, mock.patch.object(hermes_operator, "_arena_run_output", side_effect=runs) as arena_run:
            output = hermes_operator._arena_run_all_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
            )

        self.assertEqual(output["status"], "EXECUTED_ARENA")
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertEqual([run["account_id"] for run in output["runs"]], ["DEMO-RU", "DEMO-US", "DEMO-AI"])
        build_scan.assert_called_once()
        self.assertIs(portfolio_run.call_args.kwargs["scan"], scan)
        self.assertTrue(all(call.kwargs["scan"] is scan for call in arena_run.call_args_list))

    def test_arena_run_all_promotes_unresolved_execution_to_halt(self):
        policy = self.sample_arena_policy()
        runs = [
            {"status": "NO_ORDER", "account_id": "DEMO-RU", "safety": {"trading_mutations": False}},
            {"status": "ENTRY_PENDING_NO_STOP", "account_id": "DEMO-US", "safety": {"trading_mutations": True}},
            {"status": "HALT", "account_id": "DEMO-AI", "safety": {"trading_mutations": False}},
        ]

        scan = {"status": "OK", "accounts": [], "candidates": [], "research": {"status": "skipped"}}
        with mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(
            hermes_operator,
            "_arena_portfolio_run_output",
            return_value={"status": "NO_ACTION", "safety": {"trading_mutations": False}},
        ), mock.patch.object(hermes_operator, "_arena_run_output", side_effect=runs):
            output = hermes_operator._arena_run_all_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
            )

        self.assertEqual(output["status"], "HALT")
        self.assertTrue(output["safety"]["trading_mutations"])

    def test_arena_run_all_executes_triggered_stops_before_other_actions(self):
        policy = self.sample_arena_policy()
        stop_check = {
            "status": "ARENA_SOFT_STOP_TRIGGERED",
            "command": "arena-check-stops",
            "triggered": [{"account_id": "DEMO-US", "symbol": "KO@XNYS", "check_status": "EXIT_SUBMITTED"}],
            "safety": {"trading_mutations": True},
        }

        with mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value=stop_check) as check_stops, mock.patch.object(
            hermes_operator, "build_arena_scan", side_effect=AssertionError("new scan must wait for triggered stop execution")
        ), mock.patch.object(
            hermes_operator,
            "_arena_portfolio_run_output",
            side_effect=AssertionError("portfolio actions must wait for triggered stop execution"),
        ), mock.patch.object(
            hermes_operator, "_arena_run_output", side_effect=AssertionError("new entries must wait for triggered stop execution")
        ):
            output = hermes_operator._arena_run_all_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
            )

        self.assertEqual(output["status"], "ARENA_SOFT_STOP_TRIGGERED")
        self.assertEqual(output["runs"], [stop_check])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertTrue(check_stops.call_args.kwargs["execute_triggered_stops"])

    def test_arena_run_all_keeps_portfolio_confirmation_required(self):
        policy = self.sample_arena_policy()
        scan = {"status": "OK", "accounts": [], "candidates": [], "research": {"status": "skipped"}}
        portfolio_run = {
            "status": "CONFIRMATION_REQUIRED",
            "command": "arena-portfolio-run",
            "results": [
                {
                    "status": "CONFIRMATION_REQUIRED",
                    "command": "arena-portfolio-run",
                    "account_id": "DEMO-RU",
                    "action": "REPLACE",
                    "required_confirmation": "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                    "replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                    "safety": {"trading_mutations": False},
                }
            ],
            "safety": {"trading_mutations": False},
        }

        with mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(
            hermes_operator, "_arena_portfolio_run_output", return_value=portfolio_run
        ), mock.patch.object(
            hermes_operator, "_arena_run_output", side_effect=AssertionError("new entries must wait for portfolio approval")
        ):
            output = hermes_operator._arena_run_all_output(policy, policy_path=Path("config/finam_arena_policy.json"), live=True)

        self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(output["runs"], [portfolio_run])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_all_defers_new_entries_after_auto_portfolio_mutation(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["accounts"][2]["trade_mode"] = "auto"
        scan = {"status": "OK", "accounts": [], "candidates": [], "research": {"status": "skipped"}}
        portfolio_run = {
            "status": "EXECUTED_ARENA_PORTFOLIO",
            "command": "arena-portfolio-run",
            "results": [
                {
                    "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
                    "command": "arena-portfolio-run",
                    "account_id": "DEMO-AI",
                    "action": "EXIT_WEAK",
                    "symbol": "LKOH@MISX",
                    "broker_mutation": True,
                    "safety": {"trading_mutations": True},
                }
            ],
            "broker_mutation": True,
            "safety": {"trading_mutations": True},
        }

        with mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(
            hermes_operator, "_arena_portfolio_run_output", return_value=portfolio_run
        ), mock.patch.object(
            hermes_operator, "_arena_run_output", side_effect=AssertionError("new entries must wait after portfolio mutation")
        ):
            output = hermes_operator._arena_run_all_output(policy, policy_path=Path("config/finam_arena_policy.json"), live=True)

        self.assertEqual(output["status"], "EXECUTED_ARENA_PORTFOLIO")
        self.assertEqual(output["reason"], "portfolio_mutation_completed_new_entries_deferred")
        self.assertEqual(output["runs"], [portfolio_run])
        self.assertTrue(output["safety"]["trading_mutations"])

    def test_arena_scan_cli_defaults_to_cache_only_and_fresh_research_overrides(self):
        policy = self.sample_arena_policy()
        scan = {"status": "OK", "command": "arena-scan", "accounts": [], "candidates": [], "warnings": [], "errors": []}

        with mock.patch.object(hermes_operator, "load_arena_policy", return_value=policy), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ) as build_scan, mock.patch.object(hermes_operator.h4_monitor_notify, "format_arena_pulse", return_value="pulse"), mock.patch.object(
            hermes_operator, "write_event"
        ), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "arena-scan"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "OK")
        self.assertEqual(build_scan.call_args.kwargs["research_mode"], "cache_only")

        with mock.patch.object(hermes_operator, "load_arena_policy", return_value=policy), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ) as build_scan, mock.patch.object(hermes_operator.h4_monitor_notify, "format_arena_pulse", return_value="pulse"), mock.patch.object(
            hermes_operator, "write_event"
        ), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "arena-scan", "--fresh-research"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        self.assertEqual(result, 0)
        self.assertEqual(build_scan.call_args.kwargs["research_mode"], "normal")

    def test_arena_opportunity_auction_defaults_to_cache_only_and_writes_shadow_ledger(self):
        policy = self.sample_arena_policy()
        scan = {"status": "OK", "accounts": [], "candidates": [], "research": {"status": "skipped", "provider_call": False}}
        review = {"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "winner": {"opportunity_type": "hold_cash", "account_id": "", "symbol": "", "risk_adjusted_score": 60},
            "rejected": [],
            "score_model": {"version": "test", "kind": "provisional_heuristic_not_ev_or_probability"},
            "shadow_decision": {
                "current_contour_action": {"action": "HOLD"},
                "auction_winner": {"opportunity_type": "hold_cash"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ) as build_scan, mock.patch.object(
            hermes_operator, "build_arena_portfolio_review", return_value=review
        ), mock.patch.object(
            hermes_operator, "build_arena_opportunity_auction", return_value=auction
        ), mock.patch.object(
            hermes_operator, "pretrade_check", side_effect=AssertionError("auction must not call Sonar Pro")
        ):
            output = hermes_operator._arena_opportunity_auction_output(
                policy,
                policy_path=Path("arena.json"),
                event_path=Path(tmp) / "events.jsonl",
                ledger_path=Path(tmp) / "auction.jsonl",
            )

        self.assertEqual(build_scan.call_args.kwargs["research_mode"], "cache_only")
        self.assertEqual(output["ledger_records_written"], 1)
        self.assertTrue(output["safety"]["read_only_operator_cli"])
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertFalse(output["safety"]["policy_write"])

    def test_arena_opportunity_auction_fresh_research_is_explicit(self):
        policy = self.sample_arena_policy()
        scan = {"status": "OK", "accounts": [], "candidates": [], "research": {"status": "skipped"}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "load_arena_policy", return_value=policy
        ), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ) as build_scan, mock.patch.object(
            hermes_operator,
            "build_arena_portfolio_review",
            return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
        ), mock.patch.object(
            hermes_operator,
            "build_arena_opportunity_auction",
            return_value={
                "status": "OK",
                "command": "arena-opportunity-auction",
                "winner": {"opportunity_type": "hold_cash"},
                "rejected": [],
                "score_model": {"version": "test"},
                "shadow_decision": {"current_contour_action": {}, "auction_winner": {}},
            },
        ), mock.patch.object(
            hermes_operator, "write_event"
        ), mock.patch.object(
            sys,
            "argv",
            [
                "hermes_operator.py",
                "arena-opportunity-auction",
                "--fresh-research",
                "--event-path",
                str(Path(tmp) / "events.jsonl"),
                "--ledger-path",
                str(Path(tmp) / "auction.jsonl"),
            ],
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["command"], "arena-opportunity-auction")
        self.assertEqual(build_scan.call_args.kwargs["research_mode"], "normal")

    def test_arena_news_scan_writes_rss_event_candidates_without_sonar(self):
        policy = self.sample_arena_policy()
        policy["research"] = {"finam_rss_url": "https://example.invalid/rss", "timeout_seconds": 1}
        rss = b"""<?xml version="1.0"?><rss><channel><item><title>AAPL catalyst</title><link>https://n/1</link><description>AAPL news</description><pubDate>Mon, 08 Jun 2026 12:00:00 GMT</pubDate></item></channel></rss>"""

        class Response:
            def read(self):
                return rss

        with tempfile.TemporaryDirectory() as tmp:
            output = hermes_operator._arena_news_scan_output(
                policy,
                policy_path=Path("arena.json"),
                event_path=Path(tmp) / "events.jsonl",
                limit=10,
                opener=lambda *args, **kwargs: Response(),
                now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
            )

            records = hermes_operator.read_jsonl(Path(tmp) / "events.jsonl")

        self.assertEqual(output["status"], "OK")
        self.assertFalse(output["sonar_provider_call"])
        self.assertEqual(output["records_written"], 1)
        self.assertEqual(records[0]["symbol"], "AAPL@XNGS")
        self.assertEqual(records[0]["account_ids"], ["DEMO-US"])

    def test_arena_news_propose_returns_policy_proposal_without_write(self):
        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            hermes_operator.append_jsonl(
                event_path,
                [
                    {
                        "timestamp": "2026-06-08T12:00:00+00:00",
                        "symbol": "AAPL@XNGS",
                        "account_ids": ["DEMO-US"],
                        "decision": "event_candidate_unclassified",
                        "title": "AAPL catalyst",
                    },
                    {
                        "timestamp": "2026-06-08T13:00:00+00:00",
                        "symbol": "MSFT@XNGS",
                        "account_ids": ["DEMO-US"],
                        "decision": "positive_catalyst",
                        "title": "MSFT catalyst",
                    },
                ],
            )

            output = hermes_operator._arena_news_propose_output(
                policy,
                policy_path=Path("arena.json"),
                event_path=event_path,
                limit=10,
                now=datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["command"], "arena-news-propose")
        self.assertFalse(output["write_applied"])
        self.assertFalse(output["safety"]["policy_write"])
        self.assertEqual(output["policy_proposal"]["manual_review"][0]["symbol"], "AAPL@XNGS")
        self.assertEqual(output["policy_proposal"]["add_universe"][0]["symbol"], "MSFT@XNGS")
        self.assertIn("--add-universe MSFT@XNGS", output["policy_proposal"]["add_universe"][0]["apply_command"])

    def test_arena_confirm_many_sorts_multiple_confirmations_by_policy_accounts(self):
        policy = self.sample_arena_policy()
        with mock.patch.object(
            hermes_operator,
            "_arena_confirm_output",
            side_effect=[
                {"status": "DRY_RUN", "account_id": "DEMO-RU", "safety": {"trading_mutations": False}, "broker_mutation": False},
                {"status": "DRY_RUN", "account_id": "DEMO-AI", "safety": {"trading_mutations": False}, "broker_mutation": False},
            ],
        ) as confirm_output:
            output = hermes_operator._arena_confirm_many_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation_text="CONFIRM_ARENA_BUY LKOH@MISX DEMO-AI CONFIRM_ARENA_BUY LKOH@MISX DEMO-RU",
                live=True,
            )

        self.assertEqual(output["status"], "NO_ORDER")
        self.assertEqual(
            output["confirmations"],
            ["CONFIRM_ARENA_BUY LKOH@MISX DEMO-RU", "CONFIRM_ARENA_BUY LKOH@MISX DEMO-AI"],
        )
        self.assertEqual(confirm_output.call_args_list[0].kwargs["confirmation"], "CONFIRM_ARENA_BUY LKOH@MISX DEMO-RU")
        self.assertEqual(confirm_output.call_args_list[1].kwargs["confirmation"], "CONFIRM_ARENA_BUY LKOH@MISX DEMO-AI")

    def test_arena_confirm_many_extracts_replace_and_exit_confirmations(self):
        policy = self.sample_arena_policy()
        with mock.patch.object(
            hermes_operator,
            "_arena_confirm_output",
            side_effect=[
                {"status": "DRY_RUN", "account_id": "DEMO-RU", "safety": {"trading_mutations": False}, "broker_mutation": False},
                {"status": "DRY_RUN", "account_id": "DEMO-US", "safety": {"trading_mutations": False}, "broker_mutation": False},
            ],
        ) as confirm_output:
            output = hermes_operator._arena_confirm_many_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation_text="CONFIRM_ARENA_EXIT NVDA@XNGS DEMO-US и CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                live=True,
            )

        self.assertEqual(output["status"], "NO_ORDER")
        self.assertEqual(
            output["confirmations"],
            ["CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU", "CONFIRM_ARENA_EXIT NVDA@XNGS DEMO-US"],
        )
        self.assertEqual(confirm_output.call_args_list[0].kwargs["confirmation"], "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU")
        self.assertEqual(confirm_output.call_args_list[1].kwargs["confirmation"], "CONFIRM_ARENA_EXIT NVDA@XNGS DEMO-US")

    def test_arena_confirm_many_halts_when_post_trade_stop_check_missing(self):
        policy = self.sample_arena_policy()
        executed_run = {
            "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
            "account_id": "DEMO-RU",
            "proposal": {"proposal": {"account_id": "DEMO-RU", "symbol": "SBER@MISX"}},
            "soft_stop": {"account_id": "DEMO-RU", "symbol": "SBER@MISX"},
            "safety": {"trading_mutations": True},
            "broker_mutation": True,
        }
        with mock.patch.object(
            hermes_operator,
            "_arena_confirm_output",
            return_value=executed_run,
        ) as confirm_output, mock.patch.object(
            hermes_operator,
            "_arena_check_stops_output",
            return_value={"status": "NO_STOPS", "checks": [], "safety": {"trading_mutations": False}},
        ):
            output = hermes_operator._arena_confirm_many_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation_text="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU CONFIRM_ARENA_BUY LKOH@MISX DEMO-AI",
                live=True,
            )

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["runs"][0]["post_trade_protection_status"], "POST_TRADE_PROTECTION_UNVERIFIED")
        self.assertEqual(confirm_output.call_count, 1)

    def test_arena_confirm_single_halts_when_post_trade_stop_check_missing(self):
        policy = self.sample_arena_policy()
        executed_run = {
            "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
            "account_id": "DEMO-RU",
            "proposal": {"proposal": {"account_id": "DEMO-RU", "symbol": "SBER@MISX"}},
            "soft_stop": {"account_id": "DEMO-RU", "symbol": "SBER@MISX"},
            "safety": {"trading_mutations": True},
            "broker_mutation": True,
        }
        with mock.patch.object(
            hermes_operator,
            "_arena_confirm_output",
            return_value=executed_run,
        ), mock.patch.object(
            hermes_operator,
            "_arena_check_stops_output",
            return_value={"status": "NO_STOPS", "checks": [], "safety": {"trading_mutations": False}},
        ):
            output = hermes_operator._arena_confirm_many_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation_text="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                live=True,
            )

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["execution_status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(output["post_trade_protection_status"], "POST_TRADE_PROTECTION_UNVERIFIED")

    def test_arena_executor_systemd_templates_default_to_non_mutating_paper_mode(self):
        service = (ROOT / "ops/systemd/user/finam-arena-executor.service").read_text(encoding="utf-8")
        timer = (ROOT / "ops/systemd/user/finam-arena-executor.timer").read_text(encoding="utf-8")
        pulse_service = (ROOT / "ops/systemd/user/finam-arena-pulse.service").read_text(encoding="utf-8")
        pulse_timer = (ROOT / "ops/systemd/user/finam-arena-pulse.timer").read_text(encoding="utf-8")
        growth_service = (ROOT / "ops/systemd/user/finam-arena-growth-loop.service").read_text(encoding="utf-8")
        growth_timer = (ROOT / "ops/systemd/user/finam-arena-growth-loop.timer").read_text(encoding="utf-8")

        self.assertFalse((ROOT / "ops/systemd/user/finam-arena-callbacks.service").exists())
        self.assertFalse((ROOT / "ops/systemd/user/finam-arena-callbacks.timer").exists())
        self.assertIn("Environment=TRADING_MODE=paper", service)
        self.assertIn("EnvironmentFile=-%h/.config/finam-hermes-trading-bot/runtime.env", service)
        self.assertIn("Environment=TRADING_MODE=paper", pulse_service)
        self.assertIn("EnvironmentFile=-%h/.config/finam-hermes-trading-bot/runtime.env", pulse_service)
        self.assertIn("WorkingDirectory=%h/finam-hermes-trading-bot", growth_service)
        self.assertIn("arena-growth-loop --dry-run --daily --max-reviews 3", growth_service)
        self.assertIn("scripts/arena_executor_notify.py --once --dry-run", service)
        self.assertNotIn("--live", service)
        self.assertIn("OnCalendar=Mon..Fri *-*-* 10..23:07:00 Europe/Moscow", timer)
        self.assertIn("OnCalendar=Mon..Fri *-*-* 10..23:37:00 Europe/Moscow", timer)
        self.assertIn("Unit=finam-arena-executor.service", timer)
        self.assertIn("OnCalendar=Mon..Fri *-*-* 09:45:00 Europe/Moscow", growth_timer)
        self.assertIn("Unit=finam-arena-growth-loop.service", growth_timer)
        self.assertIn("OnCalendar=*-*-* 10,14,18,22:12:00 Europe/Moscow", pulse_timer)
        self.assertIn("RandomizedDelaySec=20s", pulse_timer)
        self.assertNotIn("Timezone=", timer)
        self.assertNotIn("Timezone=", pulse_timer)
        self.assertNotIn("Timezone=", growth_timer)

    def test_arena_run_live_requires_confirmation_during_approval_window(self):
        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                live=True,
                confirmation="",
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
        self.assertIn("CONFIRM_ARENA_BUY SBER@MISX DEMO-RU", output["required_confirmation"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_live_requires_env_gate_after_confirmation(self):
        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                live=True,
                confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                env={"FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["required_env"], "FINAM_ARENA_AUTO_TRADE_ENABLED")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_blocked_buy_reports_exact_override_confirmation(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-03T23:59:59+03:00"
        policy["accounts"][1]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AVGO@XNGS")
        scan["mode"] = "autonomous"
        scan["accounts"][1]["trade_mode"] = "auto"
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_risk_requires_manual_review", "candidate_score_below_min"]

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
                live=True,
                confirmation="",
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "BLOCKED")
        self.assertTrue(output["override_allowed"])
        self.assertEqual(output["override_confirmation_phrase"], "CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US")
        self.assertFalse(output["override_confirmation_matches"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_buy_override_reaches_live_env_gate(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-03T23:59:59+03:00"
        policy["accounts"][1]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AVGO@XNGS")
        scan["mode"] = "autonomous"
        scan["accounts"][1]["trade_mode"] = "auto"
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_risk_requires_manual_review", "candidate_score_below_min"]

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
                live=True,
                confirmation="CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US",
                env={"FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["required_env"], "FINAM_ARENA_AUTO_TRADE_ENABLED")
        self.assertTrue(output["proposal"]["override"]["applied"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_buy_override_rejects_non_overrideable_gate(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-03T23:59:59+03:00"
        policy["accounts"][1]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AVGO@XNGS")
        scan["mode"] = "autonomous"
        scan["accounts"][1]["trade_mode"] = "auto"
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["same_symbol_position_open"]

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
                live=True,
                confirmation="CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US",
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "BLOCKED")
        self.assertFalse(output["override_allowed"])
        self.assertEqual(output["override_rejected_reason"], "gate_reasons_not_overrideable")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_buy_override_rejects_symbol_mismatch_without_mutation(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-03T23:59:59+03:00"
        policy["accounts"][1]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AVGO@XNGS")
        scan["mode"] = "autonomous"
        scan["accounts"][1]["trade_mode"] = "auto"
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_risk_requires_manual_review", "candidate_score_below_min"]

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
                live=True,
                confirmation="CONFIRM_ARENA_BUY_OVERRIDE NVDA@XNGS DEMO-US",
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "BLOCKED")
        self.assertTrue(output["override_allowed"])
        self.assertFalse(output["override_confirmation_matches"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_confirm_does_not_treat_override_as_pending_confirmation(self):
        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(Path(tmp) / "pending.json")}
            output = hermes_operator._arena_confirm_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation="CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US",
                live=True,
                env=env,
        )

        self.assertEqual(output["status"], "APPROVAL_NOT_FOUND")
        self.assertEqual(output["parsed_confirmation"]["kind"], "BUY_OVERRIDE")
        self.assertIn("arena-run --account DEMO-US --live", output["next_actions"][0]["command"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_proposal_exposes_buy_override_hint_for_soft_blocked_candidate(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-03T23:59:59+03:00"
        policy["accounts"][1]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AVGO@XNGS")
        scan["mode"] = "autonomous"
        scan["accounts"][1]["trade_mode"] = "auto"
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_risk_requires_manual_review", "candidate_score_below_min"]

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_proposal_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
            )

        self.assertEqual(output["status"], "PROPOSE_ONLY")
        self.assertTrue(output["proposal"]["execution"]["override_allowed"])
        self.assertEqual(output["execution_route"]["override_confirmation_phrase"], "CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US")
        self.assertIn("arena-run --account DEMO-US --live", output["proposal"]["execution"]["override_next_live_command"])
        self.assertFalse(output["broker_mutation"])

    def test_arena_run_after_approval_window_can_reach_env_gate_without_confirmation(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-03T23:59:59+03:00"
        policy["accounts"][0]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        scan["mode"] = "autonomous"
        scan["accounts"][0]["trade_mode"] = "auto"

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                live=True,
                confirmation="",
                env={"FINAM_ARENA_API": "secret"},
                now=datetime(2026, 6, 4, 21, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["required_env"], "FINAM_ARENA_AUTO_TRADE_ENABLED")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_approval_until_can_be_overridden_by_env(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-10T23:59:59+03:00"
        policy["accounts"][0]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        scan["mode"] = "autonomous"
        scan["accounts"][0]["trade_mode"] = "auto"

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                live=True,
                confirmation="",
                env={
                    "FINAM_ARENA_API": "secret",
                    "FINAM_ARENA_APPROVAL_UNTIL": "2026-06-03T23:59:59+03:00",
                },
                now=datetime(2026, 6, 4, 21, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["required_env"], "FINAM_ARENA_AUTO_TRADE_ENABLED")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_live_executes_triggered_stops_before_entry_revalidation(self):
        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        stop_check = {
            "status": "ARENA_SOFT_STOP_TRIGGERED",
            "triggered": [{"account_id": "DEMO-RU", "symbol": "SBER@MISX", "check_status": "EXIT_SUBMITTED"}],
            "safety": {"trading_mutations": True},
        }

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan), mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value=stop_check
        ) as check_stops, mock.patch.object(
            hermes_operator, "_arena_revalidate_live_price_output", side_effect=AssertionError("entry revalidation must wait for triggered stop exit")
        ):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                live=True,
                confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["reason"], "arena_stop_check_not_clear")
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertTrue(check_stops.call_args.kwargs["execute_triggered_stops"])

    def test_arena_run_live_blocks_us_mic_before_regular_session_without_broker_submit(self):
        class ArenaClient:
            def create_session(self, secret):
                raise AssertionError("market-session gate must block before broker session/order submit")

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("market-session gate must not call /orders while US market is closed")

        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["accounts"][1]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AAPL@XNGS")
        scan["mode"] = "autonomous"
        scan["accounts"][1]["trade_mode"] = "auto"

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
                live=True,
                confirmation="",
                client=ArenaClient(),
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
                now=datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(output["status"], "WAIT_MARKET_CLOSED")
        self.assertEqual(output["reason"], "us_regular_session_closed")
        self.assertEqual(output["market_session"]["mic"], "XNGS")
        self.assertEqual(output["market_session"]["opens_at_msk"], "2026-06-04T16:30:00+03:00")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_live_blocks_us_market_holiday_during_regular_hours_without_broker_submit(self):
        class ArenaClient:
            def create_session(self, secret):
                raise AssertionError("holiday gate must block before broker session/order submit")

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("holiday gate must not call /orders")

        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["accounts"][1]["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="ISRG@XNGS")
        scan["mode"] = "autonomous"
        scan["accounts"][1]["trade_mode"] = "auto"

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
                live=True,
                confirmation="",
                client=ArenaClient(),
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
                now=datetime(2026, 7, 3, 16, 37, tzinfo=timezone.utc),
            )

        self.assertEqual(output["status"], "WAIT_MARKET_CLOSED")
        self.assertEqual(output["reason"], "us_market_holiday")
        self.assertTrue(output["market_session"]["holiday"])
        self.assertEqual(output["market_session"]["opens_at_msk"], "2026-07-06T16:30:00+03:00")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_live_uses_arena_market_order_and_soft_stop(self):
        class ArenaClient:
            def __init__(self):
                self.entry_payloads = []

            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                self.entry_payloads.append(payload)
                return {
                    "order_id": "entry-1",
                    "order": {
                        "symbol": "SBER@MISX",
                        "side": "SIDE_BUY",
                        "quantity": {"value": "10.0"},
                        "execution_price": {"value": "300.01"},
                    },
                }

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("Arena live flow must not call /sltp-orders")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        scan["candidates"][0] |= {
            "notional": "3000.00",
            "risk_rub": "100.00",
            "risk_per_share": "10.00",
        }
        client = ArenaClient()

        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            ledger = Path(tmp) / "ledger.jsonl"
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                output = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                    client=client,
                    env={
                        "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                        "FINAM_ARENA_API": "secret",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                        "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(ledger),
                    },
                )
            safety_state = json.loads(safety.read_text(encoding="utf-8"))
            ledger_record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(output["status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(client.entry_payloads[0], {"symbol": "SBER@MISX", "side": "SIDE_BUY", "quantity": {"value": "10.0"}})
        self.assertEqual(output["soft_stop"]["side"], "SELL")
        self.assertEqual(safety_state["halt_new_buys"], False)
        self.assertEqual(safety_state["arena_soft_stops"][0]["symbol"], "SBER@MISX")
        self.assertEqual(ledger_record["symbol"], "SBER@MISX")
        self.assertEqual(ledger_record["side"], "BUY")
        self.assertEqual(ledger_record["quantity"], "10.0")
        self.assertEqual(ledger_record["price"], "300.01")
        self.assertEqual(ledger_record["notional"], "3000.100")
        self.assertEqual(ledger_record["proposal_snapshot"]["symbol"], "SBER@MISX")
        self.assertEqual(ledger_record["proposal_snapshot"]["gates"]["execution_allowed"], True)
        self.assertEqual(ledger_record["proposal_snapshot"]["risk"]["notional"], "3000.00")
        self.assertEqual(ledger_record["proposal_snapshot"]["protective_stop"]["stop_price"], "290.00")
        self.assertTrue(output["safety"]["trading_mutations"])

    def test_arena_attribution_backfill_extracts_portfolio_exit_from_journal(self):
        payload = {
            "status": "EXECUTED_ARENA_PORTFOLIO",
            "command": "arena-executor-notify",
            "run_all": {
                "status": "EXECUTED_ARENA_PORTFOLIO",
                "command": "arena-run-all",
                "runs": [
                    {
                        "status": "EXECUTED_ARENA_PORTFOLIO",
                        "command": "arena-portfolio-run",
                        "results": [
                            {
                                "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
                                "command": "arena-portfolio-run",
                                "account_id": "DEMO-US",
                                "symbol": "MSFT@XNGS",
                                "action": "EXIT_WEAK",
                                "exit_order_payload": {"symbol": "MSFT@XNGS", "side": "SIDE_SELL", "quantity": {"value": "540.0"}},
                                "exit_order_response": {
                                    "order_id": "exit-msft",
                                    "order": {"execution_price": {"value": "461.00"}},
                                },
                                "exit_fill_state": {
                                    "status": "filled",
                                    "executed_quantity": "540.0",
                                    "execution_price": "461.00",
                                    "order_id": "exit-msft",
                                },
                            }
                        ],
                    }
                ],
            },
        }
        journal = "\n".join(
            json.dumps({"__REALTIME_TIMESTAMP": "1780340423000000", "MESSAGE": line})
            for line in json.dumps(payload, ensure_ascii=False, indent=2).splitlines()
        )

        policy = self.sample_arena_policy()
        policy["fees"] = {
            "commission_pct_per_side": "0.035",
            "commission_pct_by_mic": {"XNGS": "0.1"},
        }

        records, warnings = hermes_operator._arena_execution_ledger_records_from_journal(
            journal,
            policy=policy,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["symbol"], "MSFT@XNGS")
        self.assertEqual(records[0]["side"], "SELL")
        self.assertEqual(records[0]["quantity"], "540.0")
        self.assertEqual(records[0]["estimated_commission"], "248.9400")

    def test_arena_confirm_executes_saved_snapshot_without_rescanning_top_candidate(self):
        class ArenaClient:
            def __init__(self):
                self.entry_payloads = []

            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                self.entry_payloads.append(payload)
                return {
                    "order_id": "entry-1",
                    "order": {
                        "symbol": payload["symbol"],
                        "side": payload["side"],
                        "quantity": payload["quantity"],
                        "execution_price": {"value": "300.10"},
                    },
                }

        class MarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def last_quote(self, jwt, symbol):
                return {"last": "300.10"}

        policy = self.sample_arena_policy()
        policy["accounts"][2]["universe"] = ["LKOH@MISX"]
        scan = self._arena_scan_with_candidate(account_id="DEMO-AI", symbol="LKOH@MISX")
        arena_client = ArenaClient()

        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            safety = Path(tmp) / "safety.json"
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_TOKEN": "market-secret",
                "FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending),
                "HERMES_TRADE_SAFETY_STATE": str(safety),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                run = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-AI",
                    live=True,
                    confirmation="",
                    env=env,
                )
            hermes_operator.persist_arena_pending_approvals_from_run_all(
                {"runs": [run]},
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                env=env,
            )
            with mock.patch.object(hermes_operator, "build_arena_scan", side_effect=AssertionError("arena-confirm must not rescan top candidate")):
                output = hermes_operator._arena_confirm_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    confirmation="CONFIRM_ARENA_BUY LKOH@MISX DEMO-AI",
                    live=True,
                    env=env,
                    client=arena_client,
                    market_client=MarketClient(),
                )

            store = json.loads(pending.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(output["command"], "arena-confirm")
        self.assertEqual(arena_client.entry_payloads[0]["symbol"], "LKOH@MISX")
        self.assertEqual(store["approvals"][0]["status"], "executed")

    def test_arena_confirm_rejects_expired_pending_approval_without_mutation(self):
        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            env = {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending)}
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                run = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="",
                    env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
                )
            hermes_operator.persist_arena_pending_approvals_from_run_all(
                {"runs": [run]},
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                now=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
                env=env,
            )
            output = hermes_operator._arena_confirm_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                live=True,
                env=env,
                now=datetime(2026, 6, 1, 9, 20, tzinfo=timezone.utc),
            )

        self.assertEqual(output["status"], "APPROVAL_EXPIRED")
        self.assertIn("next_actions", output)
        self.assertIn("active_pending_approvals", output)
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_persist_pending_approvals_includes_portfolio_replace_and_exit_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            env = {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending)}
            snapshots = hermes_operator.persist_arena_pending_approvals_from_run_all(
                {
                    "runs": [
                        {
                            "status": "CONFIRMATION_REQUIRED",
                            "command": "arena-portfolio-run",
                            "results": [
                                {
                                    "status": "CONFIRMATION_REQUIRED",
                                    "command": "arena-portfolio-run",
                                    "account_id": "DEMO-RU",
                                    "action": "REPLACE",
                                    "required_confirmation": "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                                    "replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                                },
                                {
                                    "status": "CONFIRMATION_REQUIRED",
                                    "command": "arena-portfolio-run",
                                    "account_id": "DEMO-US",
                                    "action": "EXIT_WEAK",
                                    "symbol": "NVDA@XNGS",
                                    "quantity": "3",
                                    "required_confirmation": "CONFIRM_ARENA_EXIT NVDA@XNGS DEMO-US",
                                },
                            ],
                        }
                    ]
                },
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                now=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
                env=env,
            )
            store = json.loads(pending.read_text(encoding="utf-8"))

        self.assertEqual([item["side"] for item in snapshots], ["REPLACE", "EXIT"])
        self.assertEqual(store["approvals"][0]["confirmation"], "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU")
        self.assertEqual(store["approvals"][0]["action"], "REPLACE")
        self.assertEqual(store["approvals"][1]["confirmation"], "CONFIRM_ARENA_EXIT NVDA@XNGS DEMO-US")
        self.assertEqual(store["approvals"][1]["action"], "EXIT_WEAK")

    def test_arena_confirm_rejects_stale_portfolio_approval_without_mutation(self):
        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            env = {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending)}
            hermes_operator.persist_arena_pending_approvals_from_run_all(
                {
                    "runs": [
                        {
                            "status": "CONFIRMATION_REQUIRED",
                            "command": "arena-portfolio-run",
                            "account_id": "DEMO-RU",
                            "action": "REPLACE",
                            "required_confirmation": "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                            "replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                        }
                    ]
                },
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                now=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
                env=env,
            )
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value={"status": "OK", "accounts": [], "candidates": []}), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "replacement_proposals": [], "exit_proposals": []},
            ):
                output = hermes_operator._arena_confirm_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    confirmation="CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                    live=True,
                    env=env,
                    now=datetime(2026, 6, 1, 9, 1, tzinfo=timezone.utc),
                )

        self.assertEqual(output["status"], "APPROVAL_REVALIDATION_FAILED")
        self.assertEqual(output["reason"], "portfolio_confirmation_action_not_current")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_confirm_missing_pending_reports_auto_direct_route_without_rescan(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-03T23:59:59+03:00"
        for account in policy["accounts"]:
            account["trade_mode"] = "auto"
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            env = {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending)}
            with mock.patch.object(hermes_operator, "build_arena_scan", side_effect=AssertionError("arena-confirm must not rescan")):
                output = hermes_operator._arena_confirm_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    confirmation="CONFIRM_ARENA_BUY META@XNGS DEMO-US",
                    live=True,
                    env=env,
                    now=datetime(2026, 6, 4, 13, 40, tzinfo=timezone.utc),
                )

        self.assertEqual(output["status"], "APPROVAL_NOT_FOUND")
        self.assertEqual(output["reason"], "not_found")
        self.assertEqual(output["parsed_confirmation"]["account_id"], "DEMO-US")
        self.assertEqual(output["execution_route"]["route"], "auto_direct")
        self.assertEqual(output["active_pending_approvals"], [])
        self.assertIn("arena-run --account DEMO-US --live", output["next_actions"][0]["command"])
        self.assertFalse(output["broker_mutation"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_confirm_missing_pending_routes_portfolio_override_and_recover(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        for account in policy["accounts"]:
            account["trade_mode"] = "auto"
        cases = [
            (
                "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                "REPLACE",
                "python scripts/arena_executor_notify.py --once --live --execute-live",
            ),
            (
                "CONFIRM_ARENA_EXIT NVDA@XNGS DEMO-US",
                "EXIT",
                "python scripts/arena_executor_notify.py --once --live --execute-live",
            ),
            (
                "CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US",
                "BUY_OVERRIDE",
                'python scripts/hermes_operator.py arena-run --account DEMO-US --live --confirmation "CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US"',
            ),
            (
                "CONFIRM_ARENA_RECOVER SBER@MISX DEMO-RU",
                "RECOVER",
                'python scripts/hermes_operator.py arena-recover-protection --account DEMO-RU --symbol SBER@MISX --live --confirmation "CONFIRM_ARENA_RECOVER SBER@MISX DEMO-RU"',
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            env = {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(Path(tmp) / "pending.json")}
            with mock.patch.object(hermes_operator, "build_arena_scan", side_effect=AssertionError("missing approval diagnostics must not rescan")):
                outputs = [
                    hermes_operator._arena_confirm_output(
                        policy,
                        policy_path=Path("config/finam_arena_policy.json"),
                        confirmation=confirmation,
                        live=True,
                        env=env,
                        now=datetime(2026, 6, 4, 13, 40, tzinfo=timezone.utc),
                    )
                    for confirmation, _kind, _command in cases
                ]

        for output, (_confirmation, kind, command) in zip(outputs, cases):
            self.assertEqual(output["status"], "APPROVAL_NOT_FOUND")
            self.assertEqual(output["parsed_confirmation"]["kind"], kind)
            self.assertEqual(output["next_actions"][0]["command"], command)
            self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_pending_approval_summaries_hide_expired_approvals_from_pulse(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            pending.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "approvals": [
                            {
                                "status": "pending",
                                "confirmation": "CONFIRM_ARENA_BUY OLD@MISX DEMO-RU",
                                "account_id": "DEMO-RU",
                                "symbol": "OLD@MISX",
                                "side": "BUY",
                                "expires_at": "2026-06-01T09:05:00+00:00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summaries = hermes_operator._arena_pending_approval_summaries(
                path=pending,
                now=datetime(2026, 6, 1, 9, 20, tzinfo=timezone.utc),
            )
            store = json.loads(pending.read_text(encoding="utf-8"))

        self.assertEqual(summaries, [])
        self.assertEqual(store["approvals"][0]["status"], "expired")

    def test_arena_confirm_rejects_price_drift_without_mutation(self):
        class MarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def last_quote(self, jwt, symbol):
                return {"last": "305.00"}

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            safety = Path(tmp) / "safety.json"
            env = {
                "FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending),
                "HERMES_TRADE_SAFETY_STATE": str(safety),
                "FINAM_TOKEN": "market-secret",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
            }
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                run = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="",
                    env=env,
                )
            hermes_operator.persist_arena_pending_approvals_from_run_all(
                {"runs": [run]},
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                env=env,
            )
            output = hermes_operator._arena_confirm_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                live=True,
                env=env,
                market_client=MarketClient(),
            )
            store = json.loads(pending.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "APPROVAL_REVALIDATION_FAILED")
        self.assertEqual(output["reason"], "approval_price_drift_exceeded")
        self.assertFalse(output["retryable"])
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertEqual(store["approvals"][0]["status"], "failed")
        self.assertFalse(store["approvals"][0]["retryable"])
        self.assertEqual(store["approvals"][0]["last_reason"], "approval_price_drift_exceeded")
        self.assertIn("drift_pct=", store["approvals"][0]["last_error"])

    def test_arena_run_live_rejects_direct_confirmation_price_drift_without_mutation(self):
        class MarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def last_quote(self, jwt, symbol):
                return {"last": "305.00"}

        class ArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("price drift must block before broker mutation")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                live=True,
                confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                client=ArenaClient(),
                market_client=MarketClient(),
                env={
                    "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                    "FINAM_ARENA_API": "secret",
                    "FINAM_TOKEN": "market-secret",
                },
            )

        self.assertEqual(output["status"], "APPROVAL_REVALIDATION_FAILED")
        self.assertEqual(output["reason"], "approval_price_drift_exceeded")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_confirm_price_timeout_is_retryable_and_keeps_pending(self):
        class MarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def last_quote(self, jwt, symbol):
                raise RuntimeError("The read operation timed out")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            safety = Path(tmp) / "safety.json"
            env = {
                "FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending),
                "HERMES_TRADE_SAFETY_STATE": str(safety),
                "FINAM_TOKEN": "market-secret",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_PRICE_REVALIDATION_RETRIES": "2",
                "FINAM_ARENA_PRICE_REVALIDATION_SLEEP_SECONDS": "0",
            }
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                run = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="",
                    env=env,
                )
            hermes_operator.persist_arena_pending_approvals_from_run_all(
                {"runs": [run]},
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                env=env,
            )
            output = hermes_operator._arena_confirm_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                live=True,
                env=env,
                market_client=MarketClient(),
            )
            store = json.loads(pending.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "RETRYABLE_REVALIDATION_FAILED")
        self.assertEqual(output["reason"], "approval_price_revalidation_failed")
        self.assertTrue(output["retryable"])
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertEqual(store["approvals"][0]["status"], "pending")
        self.assertTrue(store["approvals"][0]["retryable"])
        self.assertEqual(store["approvals"][0]["last_reason"], "approval_price_revalidation_failed")
        self.assertIn("timed out", store["approvals"][0]["last_error"])

    def test_arena_run_live_blocks_on_unresolved_safety_state(self):
        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(json.dumps({"halt_new_buys": True, "status": "ENTRY_PENDING_NO_STOP"}), encoding="utf-8")

            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                output = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                    env={
                        "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                        "FINAM_ARENA_API": "secret",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                    },
                )

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["reason"], "unresolved_trade_safety_state")
        self.assertEqual(output["safety_state"]["status"], "ENTRY_PENDING_NO_STOP")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_live_writes_safety_state_when_entry_pending(self):
        class PendingArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                return {"order_id": "entry-1", "status": "ORDER_STATUS_ACCEPTED"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_ACCEPTED", "executed_quantity": "0"}

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                output = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                    client=PendingArenaClient(),
                    env={
                        "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                        "FINAM_ARENA_API": "secret",
                        "FINAM_BUY_FILL_CHECKS": "1",
                        "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                    },
                )
            safety_state = json.loads(safety.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "ENTRY_PENDING_NO_STOP")
        self.assertTrue(safety_state["halt_new_buys"])
        self.assertEqual(safety_state["status"], "ENTRY_PENDING_NO_STOP")
        self.assertEqual(safety_state["account_id"], "DEMO-RU")
        self.assertEqual(safety_state["entry_order_response"]["order_id"], "entry-1")

    def test_arena_run_live_places_entry_then_records_soft_stop(self):
        class FakeArenaClient:
            def __init__(self):
                self.entry_payloads = []

            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                self.entry_payloads.append((jwt, account_id, payload))
                return {"order_id": "entry-1", "status": "ORDER_STATUS_ACCEPTED"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_FILLED", "executed_quantity": "10"}

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("Arena live flow must not call /sltp-orders")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        client = FakeArenaClient()

        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                output = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                    client=client,
                    env={
                        "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                        "FINAM_ARENA_API": "secret",
                        "FINAM_BUY_FILL_CHECKS": "1",
                        "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                    },
                )
            safety_state = json.loads(safety.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(client.entry_payloads[0][2]["side"], "SIDE_BUY")
        self.assertNotIn("type", client.entry_payloads[0][2])
        self.assertEqual(output["soft_stop"]["stop_price"], "290.00")
        self.assertEqual(safety_state["arena_soft_stops"][0]["stop_price"], "290.00")
        self.assertFalse(safety_state["halt_new_buys"])
        self.assertTrue(output["safety"]["trading_mutations"])

    def test_arena_breakeven_stop_uses_round_trip_commission(self):
        policy = self.sample_arena_policy()
        policy["fees"] = {
            "commission_pct_per_side": "0.035",
            "commission_pct_by_mic": {"MISX": "0.035"},
            "slippage_pct_per_side": "0.0",
        }
        account_review = {
            "positions": [
                {
                    "symbol": "SBER@MISX",
                    "side": "LONG",
                    "quantity": "10.0",
                    "average_price": "300.00",
                    "current_price": "306.00",
                    "stop_price": "290.00",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "arena_soft_stops": [
                            {
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "10.0",
                                "stop_price": "290.00",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            output = hermes_operator._arena_update_soft_stop_action(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                action={"account_id": "DEMO-RU", "symbol": "SBER@MISX", "action": "BREAKEVEN_STOP"},
                account_review=account_review,
                env={"HERMES_TRADE_SAFETY_STATE": str(safety)},
            )
            safety_state = json.loads(safety.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "SOFT_STOP_UPDATED")
        self.assertEqual(output["new_stop_price"], "300.210000")
        self.assertEqual(output["breakeven_basis"], "entry_plus_round_trip_cost")
        self.assertEqual(safety_state["arena_soft_stops"][0]["stop_price"], "300.210000")

    def test_arena_run_live_preserves_existing_soft_stops_when_adding_new_one(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                if account_id != "DEMO-AI":
                    return {"positions": []}
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "775.0"}, "current_price": "330.00"}]}

            def place_order(self, jwt, account_id, payload):
                return {
                    "order_id": "entry-lkoh",
                    "order": {
                        "symbol": "LKOH@MISX",
                        "side": "SIDE_BUY",
                        "quantity": {"value": "50.0"},
                        "execution_price": {"value": "4932.00"},
                    },
                }

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("Arena live flow must not call /sltp-orders")

        policy = self.sample_arena_policy()
        policy["accounts"][2]["universe"] = ["LKOH@MISX"]
        scan = self._arena_scan_with_candidate(account_id="DEMO-AI", symbol="LKOH@MISX", quantity="50", stop="4864.35")
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-AI",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "775.0",
                                "stop_price": "320.6714",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                output = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-AI",
                    live=True,
                    confirmation="CONFIRM_ARENA_BUY LKOH@MISX DEMO-AI",
                    client=FakeArenaClient(),
                    env={
                        "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                        "FINAM_ARENA_API": "secret",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                    },
                )
            safety_state = json.loads(safety.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(
            sorted(item["symbol"] for item in safety_state["arena_soft_stops"]),
            ["LKOH@MISX", "SBER@MISX"],
        )
        self.assertFalse(safety_state["halt_new_buys"])

    def test_arena_run_live_blocks_undercovered_same_symbol_soft_stop_before_scale_in(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                return {"order_id": "entry-scale", "status": "ORDER_STATUS_ACCEPTED"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_FILLED", "executed_quantity": "10"}

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "20.0"}}]}

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("Arena live flow must not call /sltp-orders")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU", symbol="SBER@MISX", quantity="10", stop="290.00")
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "10.0",
                                "stop_price": "295.00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                output = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                    client=FakeArenaClient(),
                    env={
                        "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                        "FINAM_ARENA_API": "secret",
                        "FINAM_BUY_FILL_CHECKS": "1",
                        "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                    },
                )
            safety_state = json.loads(safety.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["reason"], "arena_stop_check_not_clear")
        self.assertEqual(output["stop_check"]["status"], "SOFT_STOP_UNDER_COVERED")
        self.assertEqual(output["stop_check"]["checks"][0]["coverage_status"], "under_covered")
        self.assertEqual(safety_state["arena_soft_stops"][0]["quantity"], "10.0")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_live_uses_submit_response_fill_when_get_order_404s(self):
        class ArenaClientWith404OrderLookup:
            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                return {
                    "order_id": "entry-1",
                    "order": {
                        "symbol": "SBER@MISX",
                        "side": "SIDE_BUY",
                        "quantity": {"value": "10.0"},
                        "execution_price": {"value": "300.01"},
                    },
                }

            def get_order(self, jwt, account_id, order_id):
                raise RuntimeError("Finam HTTP 404 while calling order")

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("Arena live flow must not call /sltp-orders")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        client = ArenaClientWith404OrderLookup()

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                live=True,
                confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                client=client,
                env={
                    "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                    "FINAM_ARENA_API": "secret",
                    "FINAM_BUY_FILL_CHECKS": "1",
                    "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
                },
            )

        self.assertEqual(output["status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(output["entry_fill_state"]["source"], "entry_submit_response")
        self.assertEqual(output["soft_stop"]["quantity"], "10.0")

    def test_arena_run_live_recovers_fill_from_trades_after_get_order_404(self):
        class ArenaClientWithTradeFallback:
            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                return {"order_id": "entry-1", "status": "ORDER_STATUS_ACCEPTED"}

            def get_order(self, jwt, account_id, order_id):
                raise RuntimeError("Finam HTTP 404 while calling order")

            def trades(self, jwt, account_id, limit=10):
                return {"trades": [{"symbol": "SBER@MISX", "side": "SIDE_BUY", "quantity": {"value": "10.0"}}]}

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("Arena live flow must not call /sltp-orders")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                live=True,
                confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                client=ArenaClientWithTradeFallback(),
                env={
                    "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                    "FINAM_ARENA_API": "secret",
                    "FINAM_BUY_FILL_CHECKS": "1",
                    "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
                },
            )

        self.assertEqual(output["status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(output["entry_fill_state"]["source"], "trades_fallback")
        self.assertIn("primary_check_error", output["entry_fill_state"])

    def test_arena_run_live_does_not_call_native_sltp_after_fill(self):
        class StopFailingArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                return {
                    "order_id": "entry-1",
                    "order": {
                        "symbol": "SBER@MISX",
                        "side": "SIDE_BUY",
                        "quantity": {"value": "10.0"},
                        "execution_price": {"value": "300.01"},
                    },
                }

            def place_sltp_order(self, jwt, account_id, payload):
                raise RuntimeError("Finam HTTP 404 while calling /sltp-orders")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")

        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                output = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                    client=StopFailingArenaClient(),
                    env={
                        "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                        "FINAM_ARENA_API": "secret",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                    },
                )
            safety_state = json.loads(safety.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(output["soft_stop"]["symbol"], "SBER@MISX")
        self.assertFalse(safety_state["halt_new_buys"])
        self.assertEqual(safety_state["status"], "ARENA_SOFT_STOPS_ACTIVE")

    def test_arena_check_stops_halts_on_missing_soft_stop_for_position(self):
        class CheckClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "300.00"}]}

        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            output = hermes_operator._arena_check_stops_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                live=False,
                client=CheckClient(),
                env={"FINAM_ARENA_API": "secret", "HERMES_TRADE_SAFETY_STATE": str(safety)},
            )

        self.assertEqual(output["status"], "MISSING_SOFT_STOP")
        self.assertEqual(output["reason"], "arena_soft_stop_missing_for_position")
        self.assertEqual(output["checks"][0]["coverage_status"], "missing")
        self.assertEqual(output["checks"][0]["required_recovery_confirmation"], "CONFIRM_ARENA_RECOVER SBER@MISX DEMO-RU")

    def test_arena_check_stops_halts_on_wrong_side_soft_stop(self):
        class CheckClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "300.00"}]}

        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "BUY",
                                "quantity": "10.0",
                                "stop_price": "290.00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = hermes_operator._arena_check_stops_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                live=False,
                client=CheckClient(),
                env={"FINAM_ARENA_API": "secret", "HERMES_TRADE_SAFETY_STATE": str(safety)},
            )

        self.assertEqual(output["status"], "SOFT_STOP_SIDE_MISMATCH")
        self.assertEqual(output["reason"], "arena_soft_stop_side_mismatch")
        self.assertEqual(output["checks"][0]["coverage_status"], "side_mismatch")

    def test_arena_recover_protection_reports_manual_payload_without_live(self):
        class RecoveryClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}}]}

            def orders(self, jwt, account_id):
                return {"orders": []}

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("dry recovery must not submit")

        safety_state = {
            "halt_new_buys": True,
            "account_id": "DEMO-AI",
            "proposal": {
                "proposal": {
                    "account_id": "DEMO-AI",
                    "symbol": "SBER@MISX",
                    "protective_stop": {"side": "SELL", "stop_price": "290.00"},
                }
            },
        }
        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(json.dumps(safety_state), encoding="utf-8")
            output = hermes_operator._arena_recover_protection_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-AI",
                symbol="SBER@MISX",
                live=False,
                client=RecoveryClient(),
                env={"FINAM_ARENA_API": "secret", "HERMES_TRADE_SAFETY_STATE": str(safety)},
            )

        self.assertEqual(output["status"], "SOFT_PROTECTION_REQUIRED")
        self.assertEqual(output["manual_protection"]["stop_price"], "290.00")
        self.assertEqual(output["protective_stop_payload"]["quantity"], {"value": "10.0"})
        self.assertEqual(output["required_confirmation"], "CONFIRM_ARENA_RECOVER SBER@MISX DEMO-AI")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_recover_protection_is_idempotent_when_stop_exists(self):
        class ProtectedRecoveryClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}}]}

            def orders(self, jwt, account_id):
                raise AssertionError("Arena recovery must not read /orders")

        safety_state = {
            "halt_new_buys": True,
            "account_id": "DEMO-AI",
            "proposal": {
                "proposal": {
                    "account_id": "DEMO-AI",
                    "symbol": "SBER@MISX",
                    "protective_stop": {"side": "SELL", "stop_price": "290.00"},
                }
            },
            "arena_soft_stops": [
                {
                    "status": "ACTIVE",
                    "mode": "arena_soft_stop",
                    "account_id": "DEMO-AI",
                    "symbol": "SBER@MISX",
                    "side": "SELL",
                    "quantity": "10.0",
                    "stop_price": "290.00",
                }
            ],
        }
        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(json.dumps(safety_state), encoding="utf-8")
            output = hermes_operator._arena_recover_protection_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-AI",
                symbol="SBER@MISX",
                live=True,
                confirmation="CONFIRM_ARENA_RECOVER SBER@MISX DEMO-AI",
                client=ProtectedRecoveryClient(),
                env={
                    "FINAM_ARENA_API": "secret",
                    "HERMES_TRADE_SAFETY_STATE": str(safety),
                },
            )
            safety_exists = safety.exists()

        self.assertEqual(output["status"], "PROTECTED_SOFT")
        self.assertTrue(safety_exists)
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_recover_protection_reports_undercovered_existing_soft_stop(self):
        class UndercoveredRecoveryClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "20.0"}}]}

            def orders(self, jwt, account_id):
                raise AssertionError("Arena recovery must not read /orders")

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("dry recovery must not submit")

        safety_state = {
            "halt_new_buys": True,
            "account_id": "DEMO-AI",
            "proposal": {
                "proposal": {
                    "account_id": "DEMO-AI",
                    "symbol": "SBER@MISX",
                    "protective_stop": {"side": "SELL", "stop_price": "290.00"},
                }
            },
            "arena_soft_stops": [
                {
                    "status": "ACTIVE",
                    "mode": "arena_soft_stop",
                    "account_id": "DEMO-AI",
                    "symbol": "SBER@MISX",
                    "side": "SELL",
                    "quantity": "10.0",
                    "stop_price": "290.00",
                }
            ],
        }
        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(json.dumps(safety_state), encoding="utf-8")
            output = hermes_operator._arena_recover_protection_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-AI",
                symbol="SBER@MISX",
                live=False,
                client=UndercoveredRecoveryClient(),
                env={"FINAM_ARENA_API": "secret", "HERMES_TRADE_SAFETY_STATE": str(safety)},
            )

        self.assertEqual(output["status"], "SOFT_PROTECTION_REQUIRED")
        self.assertEqual(output["reason"], "arena_soft_stop_under_covered")
        self.assertEqual(output["protective_stop_payload"]["quantity"], {"value": "20.0"})
        self.assertEqual(output["existing_soft_stop"]["quantity"], "10.0")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_run_live_verifies_buy_stop_for_short_entry(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                return {"order_id": "entry-1", "status": "ORDER_STATUS_ACCEPTED"}

            def get_order(self, jwt, account_id, order_id):
                return {"order_id": order_id, "status": "ORDER_STATUS_FILLED", "executed_quantity": "5"}

            def place_sltp_order(self, jwt, account_id, payload):
                raise AssertionError("Arena live flow must not call /sltp-orders")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AAPL@XNGS", side="SELL", quantity="5", stop="196.00")
        client = FakeArenaClient()

        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
                live=True,
                confirmation="CONFIRM_ARENA_SELL AAPL@XNGS DEMO-US",
                client=client,
                env={
                    "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                    "FINAM_ARENA_API": "secret",
                    "FINAM_BUY_FILL_CHECKS": "1",
                    "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
                },
                now=datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(output["status"], "EXECUTED_ARENA_SOFT_STOP_ACTIVE")
        self.assertEqual(output["soft_stop"]["side"], "BUY")
        self.assertEqual(output["soft_stop"]["stop_price"], "196.00")

    def test_arena_proposal_uses_h1_h4_scan_candidate_and_payloads(self):
        policy = {
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "markets": ["MISX"], "universe": ["SBER@MISX"]},
                {"account_id": "DEMO-US", "markets": ["XNGS"], "universe": ["AAPL@XNGS"]},
                {"account_id": "DEMO-AI", "markets": ["MISX"], "universe": ["SBER@MISX"], "experimental": True},
            ],
            "risk": {
                "risk_per_trade_pct": "1",
                "max_position_notional_pct": "25",
                "max_daily_loss_pct": "3",
                "max_account_drawdown_pct": "8",
                "max_open_risk_pct": "5",
                "max_open_positions": 8,
                "max_new_trades_per_account_per_run": 1,
            },
        }
        scan = {
            "status": "OK",
            "mode": "approval",
            "accounts": [{"account_id": "DEMO-RU", "label": "РФ", "equity": "1000000"}],
            "candidates": [
                {
                    "account_id": "DEMO-RU",
                    "symbol": "SBER@MISX",
                    "side": "BUY",
                    "quantity": "10",
                    "entry_price": "300.00",
                    "stop_price": "290.00",
                    "take_profit_price": "320.00",
                    "entry_timeframe": "H1",
                    "execution_allowed": True,
                    "gate_reasons": [],
                }
            ],
            "errors": [],
            "warnings": [],
        }
        with mock.patch.object(hermes_operator, "load_arena_policy", return_value=policy), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "arena-proposal", "--account", "DEMO-RU"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "PROPOSE_ONLY")
        self.assertEqual(payload["proposal"]["symbol"], "SBER@MISX")
        self.assertEqual(payload["proposal"]["execution"]["route"], "approval_snapshot_required")
        self.assertEqual(payload["proposal"]["execution"]["confirmation_kind"], "pending_snapshot_confirmation")
        self.assertEqual(payload["execution_route"]["route"], "approval_snapshot_required")
        self.assertFalse(payload["execution_route"]["arena_confirm_allowed"])
        self.assertEqual(payload["order_payloads"]["entry_order"]["side"], "SIDE_BUY")
        self.assertFalse(payload["broker_mutation"])

    def test_arena_proposal_auto_account_marks_confirmation_as_live_authorization(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2026-06-03T23:59:59+03:00"
        for account in policy["accounts"]:
            account["trade_mode"] = "auto"
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="META@XNGS", quantity="387")
        scan["mode"] = "autonomous"
        for account in scan["accounts"]:
            account["trade_mode"] = "auto"
        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan), mock.patch.object(
            hermes_operator, "_read_arena_execution_ledger", return_value=[]
        ), mock.patch.object(
            hermes_operator, "_arena_soft_stop_records", return_value=[]
        ):
            output = hermes_operator._arena_proposal_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
            )

        self.assertEqual(output["status"], "PROPOSE_ONLY")
        self.assertEqual(output["proposal"]["symbol"], "META@XNGS")
        self.assertEqual(output["execution_route"]["route"], "auto_direct")
        self.assertEqual(output["execution_route"]["confirmation_kind"], "live_authorization")
        self.assertEqual(output["proposal"]["execution"]["confirmation_kind"], "live_authorization")
        self.assertIn("arena-run --account DEMO-US --live", output["execution_route"]["next_live_command"])
        self.assertFalse(output["execution_route"]["arena_confirm_allowed"])
        self.assertFalse(output["broker_mutation"])

    def test_arena_proposal_prefers_executable_candidate_after_blocked_short(self):
        policy = self.sample_arena_policy()
        scan = {
            "status": "OK",
            "mode": "approval",
            "accounts": [{"account_id": "DEMO-US", "label": "США", "equity": "1000000"}],
            "candidates": [
                {
                    "account_id": "DEMO-US",
                    "symbol": "AAPL@XNGS",
                    "side": "SELL",
                    "quantity": "5",
                    "entry_price": "190.00",
                    "stop_price": "196.00",
                    "take_profit_price": "178.00",
                    "entry_timeframe": "H1",
                    "execution_allowed": False,
                    "gate_reasons": ["arena_margin_trading_not_supported"],
                    "short_availability": {"available": False, "gate_reason": "arena_margin_trading_not_supported"},
                },
                {
                    "account_id": "DEMO-US",
                    "symbol": "NVDA@XNGS",
                    "side": "BUY",
                    "quantity": "10",
                    "entry_price": "220.00",
                    "stop_price": "214.00",
                    "take_profit_price": "232.00",
                    "entry_timeframe": "H1",
                    "execution_allowed": True,
                    "gate_reasons": [],
                },
            ],
            "errors": [],
            "warnings": [],
        }
        with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
            output = hermes_operator._arena_proposal_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-US",
            )

        self.assertEqual(output["status"], "PROPOSE_ONLY")
        self.assertEqual(output["candidate"]["symbol"], "NVDA@XNGS")
        self.assertEqual(output["proposal"]["symbol"], "NVDA@XNGS")
        self.assertTrue(output["proposal"]["gates"]["execution_allowed"])
        self.assertEqual(output["order_payloads"]["entry_order"]["side"], "SIDE_BUY")

    def test_arena_view_renders_account_callback_without_mutations(self):
        policy = {
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "markets": ["MISX"], "universe": ["SBER@MISX"], "strategy": "russian_equities_h1_h4"},
                {"account_id": "DEMO-US", "markets": ["XNGS"], "universe": ["AAPL@XNGS"]},
                {"account_id": "DEMO-AI", "markets": ["MISX"], "universe": ["SBER@MISX"], "experimental": True},
            ],
            "risk": {
                "risk_per_trade_pct": "1",
                "max_position_notional_pct": "25",
                "max_daily_loss_pct": "3",
                "max_account_drawdown_pct": "8",
                "max_open_risk_pct": "5",
                "max_open_positions": 8,
                "max_new_trades_per_account_per_run": 1,
            },
        }
        snapshot = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_command": "arena-status",
            "policy_path": str(ROOT / "config" / "finam_arena_policy.json"),
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "strategy": "russian_equities_h1_h4",
                    "equity": "1000000",
                    "cash": "900000",
                    "pnl_rub": "0",
                    "pnl_pct": "0",
                    "positions": [],
                }
            ],
            "candidates": [{"account_id": "DEMO-RU", "symbol": "SBER@MISX", "side": "BUY"}],
            "errors": [],
            "warnings": [],
            "telegram_reply_markup": hermes_operator.h4_monitor_notify.arena_reply_markup(),
        }
        snapshot_path = Path(self._runtime_tmp.name) / "telegram_snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "FINAM_ARENA_POLICY_PATH": str(ROOT / "config" / "finam_arena_policy.json"),
                "FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH": str(snapshot_path),
                "FINAM_ARENA_TELEGRAM_SNAPSHOT_TTL_SECONDS": "900",
            },
        ), mock.patch.object(
            hermes_operator, "load_arena_policy", return_value=policy
        ), mock.patch.object(
            hermes_operator, "build_arena_scan", side_effect=AssertionError("account callback must use snapshot")
        ), mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "arena-view", "--callback", "arena:account:DEMO-RU"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["command"], "arena-view")
        self.assertEqual(payload["snapshot_status"], "fresh")
        self.assertEqual(payload["snapshot_source"], "arena-status")
        self.assertIsInstance(payload["snapshot_age_seconds"], int)
        self.assertIn("DEMO-RU", payload["telegram_text"])
        self.assertIn("SBER: кандидат — купить", payload["telegram_text"])
        self.assertNotIn("SBER@MISX BUY", payload["telegram_text"])
        self.assertFalse(payload["safety"]["trading_mutations"])

    def test_arena_view_overlays_fresh_pending_approvals_without_rewriting_snapshot(self):
        policy = self.sample_arena_policy()
        snapshot = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_command": "arena-status",
            "policy_path": str(ROOT / "config" / "finam_arena_policy.json"),
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "equity": "1000000",
                    "cash": "900000",
                    "pnl_rub": "0",
                    "pnl_pct": "0",
                    "positions_count": 0,
                }
            ],
            "errors": [],
            "warnings": [],
            "telegram_reply_markup": hermes_operator.h4_monitor_notify.arena_reply_markup(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / "telegram_snapshot.json"
            pending_path = Path(tmp) / "pending.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            pending_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "approvals": [
                            {
                                "status": "pending",
                                "confirmation": "CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "BUY",
                                "quantity": "10",
                                "created_at": "2026-06-16T10:00:00+00:00",
                                "expires_at": "2999-01-01T00:00:00+00:00",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            before = snapshot_path.read_text(encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {
                    "FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH": str(snapshot_path),
                    "FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending_path),
                },
            ), mock.patch.object(hermes_operator, "build_arena_scan", side_effect=AssertionError("snapshot callback must not scan")):
                output = hermes_operator._arena_view_output(
                    policy,
                    policy_path=ROOT / "config" / "finam_arena_policy.json",
                    callback="arena:overview",
                )
            after = snapshot_path.read_text(encoding="utf-8")

        self.assertEqual(output["status"], "OK")
        self.assertIn("CONFIRM_ARENA_BUY SBER@MISX DEMO-RU", output["telegram_text"])
        self.assertEqual(before, after)
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_view_missing_snapshot_fails_fast_without_scan(self):
        policy = {
            "mode": "approval",
            "accounts": [{"account_id": "DEMO-RU", "markets": ["MISX"], "universe": ["SBER@MISX"]}],
        }
        missing_snapshot = Path(self._runtime_tmp.name) / "missing_snapshot.json"
        with mock.patch.dict(os.environ, {"FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH": str(missing_snapshot)}), mock.patch.object(
            hermes_operator, "build_arena_scan", side_effect=AssertionError("missing snapshot must not scan")
        ):
            output = hermes_operator._arena_view_output(
                policy,
                policy_path=ROOT / "config" / "finam_arena_policy.json",
                callback="arena:account:DEMO-RU",
            )

        self.assertEqual(output["status"], "STALE_SNAPSHOT")
        self.assertEqual(output["snapshot_status"], "missing")
        self.assertEqual(output["reason"], "snapshot_missing")
        self.assertIn("Запроси новый обзор", output["telegram_text"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_view_stale_snapshot_fails_fast_without_scan(self):
        policy = {
            "mode": "approval",
            "accounts": [{"account_id": "DEMO-RU", "markets": ["MISX"], "universe": ["SBER@MISX"]}],
        }
        snapshot_path = Path(self._runtime_tmp.name) / "stale_snapshot.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "source_command": "arena-status",
                    "policy_path": str(ROOT / "config" / "finam_arena_policy.json"),
                    "status": "OK",
                    "accounts": [],
                    "errors": [],
                    "warnings": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {"FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH": str(snapshot_path), "FINAM_ARENA_TELEGRAM_SNAPSHOT_TTL_SECONDS": "900"},
        ), mock.patch.object(
            hermes_operator, "build_arena_scan", side_effect=AssertionError("stale snapshot must not scan")
        ):
            output = hermes_operator._arena_view_output(
                policy,
                policy_path=ROOT / "config" / "finam_arena_policy.json",
                callback="arena:overview",
            )

        self.assertEqual(output["status"], "STALE_SNAPSHOT")
        self.assertEqual(output["snapshot_status"], "stale")
        self.assertEqual(output["reason"], "snapshot_stale")
        self.assertGreater(output["snapshot_age_seconds"], 900)

    def test_arena_view_overview_risks_and_attribution_use_snapshot(self):
        policy = {
            "mode": "approval",
            "accounts": [{"account_id": "DEMO-RU", "markets": ["MISX"], "universe": ["SBER@MISX"], "label": "РФ"}],
            "fees": {"commission_pct_by_mic": {"MISX": "0.035"}},
        }
        snapshot = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_command": "arena-status",
            "policy_path": str(ROOT / "config" / "finam_arena_policy.json"),
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "equity": "1000000",
                    "cash": "900000",
                    "pnl_rub": "0",
                    "pnl_pct": "0",
                    "open_risk_pct": "0",
                    "positions": [],
                    "recent_trades": [
                        {
                            "symbol": "SBER@MISX",
                            "side": "SIDE_BUY",
                            "quantity": "10",
                            "price": "300",
                            "time": datetime.now(timezone.utc).isoformat(),
                            "trade_id": "trade-1",
                        }
                    ],
                }
            ],
            "errors": [],
            "warnings": [],
            "telegram_pulse": "cached pulse text",
            "telegram_reply_markup": hermes_operator.h4_monitor_notify.arena_reply_markup(),
        }
        snapshot_path = Path(self._runtime_tmp.name) / "telegram_snapshot.json"
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH": str(snapshot_path)}), mock.patch.object(
            hermes_operator, "build_arena_scan", side_effect=AssertionError("snapshot callbacks must not scan")
        ), mock.patch.object(
            hermes_operator, "build_arena_status", side_effect=AssertionError("attribution must use snapshot status")
        ):
            overview = hermes_operator._arena_view_output(
                policy,
                policy_path=ROOT / "config" / "finam_arena_policy.json",
                callback="arena:overview",
            )
            risks = hermes_operator._arena_view_output(
                policy,
                policy_path=ROOT / "config" / "finam_arena_policy.json",
                callback="arena:risks",
            )
            attribution = hermes_operator._arena_view_output(
                policy,
                policy_path=ROOT / "config" / "finam_arena_policy.json",
                callback="arena:attribution",
            )

        for output in (overview, risks, attribution):
            self.assertEqual(output["status"], "OK")
            self.assertEqual(output["snapshot_status"], "fresh")
            self.assertEqual(output["snapshot_source"], "arena-status")
            self.assertIsInstance(output["snapshot_age_seconds"], int)
        self.assertIn("Finam Arena Pulse", overview["telegram_text"])
        self.assertIn("Arena Risks", risks["telegram_text"])
        self.assertIn("Arena Attribution", attribution["telegram_text"])

    def test_arena_strategy_propose_returns_safe_diff_without_write(self):
        policy = self.sample_arena_policy()

        output = hermes_operator._arena_strategy_propose_output(
            policy,
            policy_path=Path("config/finam_arena_policy.json"),
            account_id="DEMO-RU",
            mode="autonomous",
            risk_multiplier="1.25",
            pause=True,
            resume=False,
            trade_mode="auto",
            add_universe=["GAZP@MISX"],
            remove_universe=[],
        )

        self.assertEqual(output["status"], "OK")
        self.assertFalse(output["write_applied"])
        self.assertFalse(output["safety"]["policy_write"])
        self.assertEqual(output["proposed_policy"]["mode"], "autonomous")
        self.assertEqual(output["proposed_policy"]["accounts"][0]["risk_multiplier"], "1.25")
        self.assertTrue(output["proposed_policy"]["accounts"][0]["paused"])
        self.assertEqual(output["proposed_policy"]["accounts"][0]["trade_mode"], "auto")
        self.assertIn("GAZP@MISX", output["proposed_policy"]["accounts"][0]["universe"])

    def test_arena_universe_refresh_propose_us_account_returns_review_only_strategy_diff(self):
        policy = self.sample_arena_policy()
        policy["accounts"][1]["markets"] = ["XNGS", "XNYS"]
        policy["accounts"][1]["universe"] = ["AAPL@XNGS", "META@XNGS", "UNH@XNYS"]
        policy["learning"] = {
            "deprioritize_symbols": ["AAPL@XNGS", "META@XNGS", "UNH@XNYS", "NVTK@MISX"],
            "attribution_uncertain_symbols": ["MSFT@XNGS", "NVDA@XNGS"],
        }
        decisions = [
            {
                "account_id": "DEMO-US",
                "symbol": "AAPL@XNGS",
                "score": 52,
                "gate_reasons": ["daily_new_notional_limit_exceeded", "candidate_score_below_min"],
            },
            {
                "account_id": "DEMO-US",
                "symbol": "AAPL@XNGS",
                "score": 55,
                "gate_reasons": ["candidate_score_below_min"],
            },
        ]
        outcomes = [
            {"account_id": "DEMO-US", "symbol": "META@XNGS", "net_pnl_rub": "-2500"},
        ]

        output = hermes_operator._arena_universe_refresh_propose_output(
            policy,
            policy_path=Path("config/finam_arena_policy.json"),
            account_id="DEMO-US",
            decisions=decisions,
            outcomes=outcomes,
            execution_ledger=[],
        )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["role"], "us_universe_rotation_first")
        self.assertFalse(output["write_applied"])
        self.assertFalse(output["safety"]["policy_write"])
        self.assertIn("META@XNGS", output["remove_universe"])
        self.assertLessEqual(len(output["add_universe"]), 5)
        self.assertIn("--confirm APPLY_ARENA_STRATEGY", output["apply_commands"]["strategy"]["apply"])
        self.assertTrue(output["learning_cleanup"]["changes"])

    def test_arena_universe_refresh_propose_ru_account_diagnoses_exposure_first(self):
        policy = self.sample_arena_policy()
        policy["accounts"][0]["universe"] = ["SBER@MISX", "ALRS@MISX"]
        decisions = [
            {
                "account_id": "DEMO-RU",
                "symbol": "ALRS@MISX",
                "score": 35,
                "gate_reasons": ["account_gross_exposure_limit_reached", "candidate_score_below_min"],
            }
        ]

        output = hermes_operator._arena_universe_refresh_propose_output(
            policy,
            policy_path=Path("config/finam_arena_policy.json"),
            account_id="DEMO-RU",
            decisions=decisions,
            outcomes=[],
            execution_ledger=[],
        )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["role"], "ru_anchor_rotation_first")
        self.assertEqual(output["diagnosis"]["primary_growth_blocker"], "account_gross_exposure_limit_reached")
        self.assertEqual(output["diagnosis"]["universe_refresh_priority"], "secondary_after_exposure_rotation")
        self.assertEqual(output["add_universe"], [])
        self.assertEqual(output["remove_universe"], [])

    def test_arena_universe_refresh_propose_ai_account_keeps_cleanup_separate_from_risk(self):
        policy = self.sample_arena_policy()
        policy["accounts"][2]["markets"] = ["MISX", "XNGS"]
        policy["accounts"][2]["universe"] = ["SBER@MISX", "AAPL@XNGS", "META@XNGS"]
        policy["learning"] = {
            "deprioritize_symbols": ["AAPL@XNGS", "META@XNGS", "SNGSP@MISX"],
            "attribution_uncertain_symbols": ["SBER@MISX", "LKOH@MISX", "NVDA@XNGS"],
        }
        decisions = [
            {
                "account_id": "DEMO-AI",
                "symbol": "AAPL@XNGS",
                "score": 50,
                "gate_reasons": ["daily_new_notional_limit_exceeded", "primary_daily_limit_reached", "candidate_score_below_min"],
            }
        ]

        output = hermes_operator._arena_universe_refresh_propose_output(
            policy,
            policy_path=Path("config/finam_arena_policy.json"),
            account_id="DEMO-AI",
            decisions=decisions,
            outcomes=[],
            execution_ledger=[],
        )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["role"], "ai_cross_market_cleanup_quality_first")
        self.assertEqual(output["diagnosis"]["universe_refresh_priority"], "cleanup_quality_first_no_risk_expansion")
        self.assertEqual(output["add_universe"], [])
        self.assertEqual(output["remove_universe"], [])
        self.assertIsNone(output["apply_commands"]["strategy"]["apply"])
        self.assertTrue(output["learning_cleanup"]["changes"])

    def test_arena_growth_loop_records_valid_codex_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "arena.json"
            review_dir = Path(tmp) / "reviews"
            state_path = Path(tmp) / "growth_state.json"
            audit_path = Path(tmp) / "growth_audit.jsonl"
            policy = self.sample_arena_policy()
            policy["portfolio"] = {
                "min_candidate_score_for_buy": 75,
                "min_candidate_score_for_buy_by_account": {"DEMO-RU": 75, "DEMO-US": 80, "DEMO-AI": 80},
            }
            policy["research"] = {"provider": "codex_review", "review_dir": str(review_dir), "codex_review_ttl_minutes": 240}
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
            scan["candidates"][0]["status"] = "BLOCKED"
            scan["candidates"][0]["gate_reasons"] = ["research_unavailable_below_exceptional_score"]
            scan["candidates"][0]["arena_growth_score"] = {"score": 72}
            review_json = json.dumps(
                {
                    "verdict": "RISK",
                    "confidence": 82,
                    "reasons": ["mixed but tradable"],
                    "blocking_flags": [],
                    "summary": "Review captured for guarded retry.",
                }
            )
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan), mock.patch.object(
                hermes_operator, "_run_codex_review_command", return_value={"status": "OK", "review_json": review_json}
            ), mock.patch.object(hermes_operator, "_arena_news_scan_output", return_value={"status": "OK"}), mock.patch.object(
                hermes_operator, "_arena_news_propose_output", return_value={"status": "OK"}
            ), mock.patch.object(
                hermes_operator, "build_learning_proposal", return_value={"status": "NO_SUGGESTION", "reason": "not_enough_samples", "patch": {}}
            ), mock.patch.object(
                hermes_operator, "build_arena_status", return_value={"accounts": [{"account_id": "DEMO-RU", "positions": [], "orders": []}]}
            ):
                output = hermes_operator._arena_growth_loop_output(
                    policy,
                    policy_path=policy_path,
                    apply=True,
                    daily=False,
                    max_reviews=3,
                    review_timeout_seconds=5,
                    env={
                        "FINAM_ARENA_GROWTH_LOOP_STATE_PATH": str(state_path),
                        "FINAM_ARENA_GROWTH_LOOP_AUDIT_PATH": str(audit_path),
                    },
                )

            self.assertEqual(output["codex_reviews"]["records_written"], 1)
            self.assertTrue(list(review_dir.glob("*.json")))
            self.assertTrue(audit_path.exists())
            self.assertFalse(output["safety"]["trading_mutations"])

    def test_codex_review_ok_updates_near_executable_scan_candidate(self):
        policy = self.sample_arena_policy()
        policy["portfolio"] = {
            "min_candidate_score_for_buy": 75,
            "min_candidate_score_for_buy_by_account": {"DEMO-RU": 75, "DEMO-US": 80, "DEMO-AI": 80},
        }
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        candidate = scan["candidates"][0]
        candidate["execution_allowed"] = False
        candidate["gate_reasons"] = ["research_unavailable_below_exceptional_score", "candidate_score_below_min"]
        candidate["arena_growth_score"] = {"score": 70, "label": "MEDIUM", "components": {"research_regime": 0}}
        reviews = {
            "status": "OK",
            "records_written": 1,
            "records": [
                {
                    "symbol": "SBER@MISX",
                    "status": "OK",
                    "context_hash": "abc",
                    "review": {"verdict": "OK"},
                }
            ],
        }

        updated = hermes_operator._arena_scan_with_codex_reviews(policy, scan, reviews)

        updated_candidate = updated["candidates"][0]
        self.assertTrue(updated_candidate["execution_allowed"])
        self.assertEqual(updated_candidate["gate_reasons"], [])
        self.assertEqual(updated_candidate["arena_growth_score"]["score"], 85)
        self.assertEqual(updated_candidate["codex_review"]["verdict"], "OK")

    def test_arena_growth_codex_review_invalid_json_fails_closed(self):
        completed = hermes_operator.subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="not-json", stderr="")
        with mock.patch.object(hermes_operator.subprocess, "run", return_value=completed):
            output = hermes_operator._run_codex_review_command(
                {"context_hash": "abc"},
                timeout_seconds=5,
                env={"FINAM_ARENA_CODEX_REVIEW_COMMAND": "codex exec --json"},
            )

        self.assertEqual(output["status"], "FAILED")
        self.assertEqual(output["reason"], "codex_review_json_missing")

    def test_arena_growth_codex_review_extracts_codex_exec_json_event_text(self):
        review = {
            "verdict": "UNAVAILABLE",
            "confidence": 88,
            "reasons": ["insufficient evidence"],
            "blocking_flags": ["research_unavailable"],
            "summary": "fail closed",
            "context_hash": "abc",
        }
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(review)}}
        completed = hermes_operator.subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout='{"type":"thread.started"}\n' + json.dumps(event),
            stderr="",
        )
        with mock.patch.object(hermes_operator.subprocess, "run", return_value=completed):
            output = hermes_operator._run_codex_review_command(
                {"context_hash": "abc"},
                timeout_seconds=5,
                env={"FINAM_ARENA_CODEX_REVIEW_COMMAND": "codex exec --json"},
            )

        self.assertEqual(output["status"], "OK")
        parsed = json.loads(output["review_json"])
        self.assertEqual(parsed["verdict"], "UNAVAILABLE")
        self.assertEqual(parsed["context_hash"], "abc")

    def test_arena_growth_learning_rejects_non_learning_patch(self):
        policy = self.sample_arena_policy()
        proposal = {
            "status": "PROPOSED",
            "patch": {"portfolio": {"min_candidate_score_for_buy": "review:lower_by_5_or_recalibrate_score"}},
            "report": {},
        }
        with mock.patch.object(hermes_operator, "build_learning_proposal", return_value=proposal):
            output = hermes_operator._arena_growth_learning_step(policy, policy_path=Path("arena.json"), apply=True)

        self.assertEqual(output["status"], "BLOCKED")
        self.assertEqual(output["reason"], "non_learning_patch_rejected")
        self.assertFalse(output["safety"]["policy_write"])

    def test_arena_growth_propose_is_read_only_and_queues_missing_research(self):
        policy = self.sample_arena_policy()
        policy["learning"] = {"mode": "propose_only", "min_samples_for_promotion": 3}
        policy["research"] = {"provider": "codex_review", "review_dir": tempfile.mkdtemp(), "codex_review_ttl_minutes": 240}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="KO@XNYS")
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_unavailable_below_exceptional_score"]
        scan["candidates"][0]["arena_growth_score"] = {"score": 84, "research_verdict": "UNAVAILABLE"}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [
                {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
                {
                    "opportunity_type": "entry",
                    "account_id": "DEMO-US",
                    "symbol": "KO@XNYS",
                    "decision": "blocked",
                    "risk_adjusted_score": 84,
                },
            ],
            "research": {"provider_call": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = {
                "decisions": Path(tmp) / "decisions.jsonl",
                "outcomes": Path(tmp) / "outcomes.jsonl",
                "suggestions": Path(tmp) / "suggestions.jsonl",
            }
            paths["decisions"].write_text("", encoding="utf-8")
            paths["outcomes"].write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        {"account_id": "DEMO-US", "symbol": "META@XNGS", "net_pnl_rub": "-1000"},
                        {"account_id": "DEMO-US", "symbol": "AAPL@XNGS", "net_pnl_rub": "-500"},
                        {"account_id": "DEMO-AI", "symbol": "LKOH@MISX", "net_pnl_rub": "-700"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(hermes_operator, "_arena_learning_paths", return_value=paths), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ) as build_scan, mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ), mock.patch.object(
                hermes_operator, "append_jsonl", side_effect=AssertionError("growth-propose must not write ledgers")
            ), mock.patch.object(
                hermes_operator, "_write_policy_file", side_effect=AssertionError("growth-propose must not write policy")
            ), mock.patch.object(
                hermes_operator, "pretrade_check", side_effect=AssertionError("growth-propose must not call providers")
            ), mock.patch.object(
                hermes_operator, "_run_codex_review_command", side_effect=AssertionError("growth-propose must not call Codex provider")
            ):
                output = hermes_operator._arena_growth_propose_output(policy, policy_path=Path("arena.json"))

        self.assertEqual(output["command"], "arena-growth-propose")
        self.assertEqual(output["growth_mode"], "shadow_propose")
        self.assertEqual(build_scan.call_args.kwargs["research_mode"], "cache_only")
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertFalse(output["safety"]["policy_write"])
        self.assertFalse(output["safety"]["provider_call"])
        self.assertEqual(output["research_queue"][0]["symbol"], "KO@XNYS")
        self.assertEqual(output["research_queue"][0]["reason"], "top_candidate_research_missing")
        self.assertEqual(output["positive_learning"]["promotion_candidates"], [])
        self.assertEqual(output["capital_allocation"]["recommendation"], "no risk expansion; research-only growth")
        self.assertIn("no positive edge yet", [item.get("blocked_by") for item in output["blocked_actions"]])

    def test_arena_growth_propose_promotes_positive_setup_without_execution(self):
        policy = self.sample_arena_policy()
        policy["learning"] = {"mode": "propose_only", "min_samples_for_promotion": 3}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="MSFT@XNGS")
        scan["candidates"][0]["execution_allowed"] = True
        scan["candidates"][0]["gate_reasons"] = []
        scan["candidates"][0]["arena_growth_score"] = {"score": 88, "research_verdict": "OK"}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = {
                "decisions": Path(tmp) / "decisions.jsonl",
                "outcomes": Path(tmp) / "outcomes.jsonl",
                "suggestions": Path(tmp) / "suggestions.jsonl",
            }
            paths["decisions"].write_text("", encoding="utf-8")
            outcomes = [
                {"account_id": "DEMO-US", "symbol": "MSFT@XNGS", "side": "LONG_ROUND_TRIP", "net_pnl_rub": "1500", "mae_r": "0.4"},
                {"account_id": "DEMO-US", "symbol": "MSFT@XNGS", "side": "LONG_ROUND_TRIP", "net_pnl_rub": "2200", "mae_r": "0.6"},
                {"account_id": "DEMO-US", "symbol": "MSFT@XNGS", "side": "LONG_ROUND_TRIP", "net_pnl_rub": "1800", "mae_r": "0.5"},
            ]
            paths["outcomes"].write_text("\n".join(json.dumps(item) for item in outcomes) + "\n", encoding="utf-8")
            with mock.patch.object(hermes_operator, "_arena_learning_paths", return_value=paths), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ):
                output = hermes_operator._arena_growth_propose_output(policy, policy_path=Path("arena.json"))

        self.assertEqual(output["positive_learning"]["promotion_candidates"][0]["symbol"], "MSFT@XNGS")
        self.assertEqual(output["capital_allocation"]["next_slot_priority"][0]["symbol"], "MSFT@XNGS")
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertFalse(output["safety"]["risk_expansion"])
        self.assertTrue(
            any(item.get("action") == "promote_setup_priority_shadow" for item in output["recommended_actions"])
        )

    def test_arena_growth_propose_flags_demo_ai_duplicate_candidate(self):
        policy = self.sample_arena_policy()
        policy["learning"] = {"mode": "propose_only", "min_samples_for_promotion": 3}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AAPL@XNGS")
        ai_candidate = dict(scan["candidates"][0])
        ai_candidate["account_id"] = "DEMO-AI"
        ai_candidate["symbol"] = "AAPL@XNGS"
        ai_candidate["arena_growth_score"] = {"score": 86}
        scan["candidates"][0]["arena_growth_score"] = {"score": 87}
        scan["candidates"].append(ai_candidate)
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = {
                "decisions": Path(tmp) / "decisions.jsonl",
                "outcomes": Path(tmp) / "outcomes.jsonl",
                "suggestions": Path(tmp) / "suggestions.jsonl",
            }
            paths["decisions"].write_text("", encoding="utf-8")
            paths["outcomes"].write_text("", encoding="utf-8")
            with mock.patch.object(hermes_operator, "_arena_learning_paths", return_value=paths), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ):
                output = hermes_operator._arena_growth_propose_output(policy, policy_path=Path("arena.json"))

        duplicates = output["ai_account_DEMO-AI"]["duplicate_candidates"]
        self.assertEqual(duplicates[0]["symbol"], "AAPL@XNGS")
        self.assertTrue(
            any(item.get("blocked_by") == "duplicates_DEMO-US_same_symbol_setup" for item in output["blocked_actions"])
        )

    def test_arena_growth_propose_flags_demo_ai_symbol_overlap_separately_from_duplicate(self):
        policy = self.sample_arena_policy()
        policy["learning"] = {"mode": "propose_only", "min_samples_for_promotion": 3}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="KO@XNYS")
        scan["candidates"][0]["strategy"] = "US_EQUITIES_H1_H4"
        ai_candidate = dict(scan["candidates"][0])
        ai_candidate["account_id"] = "DEMO-AI"
        ai_candidate["strategy"] = "EXPERIMENTAL_AI_CROSS_MARKET"
        ai_candidate["arena_growth_score"] = {"score": 80}
        scan["candidates"][0]["arena_growth_score"] = {"score": 81}
        scan["candidates"].append(ai_candidate)
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            paths = {
                "decisions": Path(tmp) / "decisions.jsonl",
                "outcomes": Path(tmp) / "outcomes.jsonl",
                "suggestions": Path(tmp) / "suggestions.jsonl",
            }
            paths["decisions"].write_text("", encoding="utf-8")
            paths["outcomes"].write_text("", encoding="utf-8")
            with mock.patch.object(hermes_operator, "_arena_learning_paths", return_value=paths), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ):
                output = hermes_operator._arena_growth_propose_output(policy, policy_path=Path("arena.json"))

        ai_review = output["ai_account_DEMO-AI"]
        self.assertEqual(ai_review["status"], "SYMBOL_OVERLAP_REVIEW")
        self.assertEqual(ai_review["duplicate_candidates"], [])
        self.assertEqual(ai_review["symbol_overlaps_with_DEMO-US"][0]["symbol"], "KO@XNYS")
        self.assertTrue(
            any(item.get("action") == "review_ai_account_symbol_overlap" for item in output["recommended_actions"])
        )

    def test_arena_growth_research_calls_provider_records_cache_and_growth_propose_reuses_it(self):
        policy = self.sample_arena_policy()
        policy["portfolio"] = {"min_candidate_score_for_buy": 75}
        policy["research"] = {"provider": "codex_review", "review_dir": "", "codex_review_ttl_minutes": 240}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="KO@XNYS")
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_unavailable_below_exceptional_score"]
        scan["candidates"][0]["arena_growth_score"] = {"score": 80, "components": {"research_regime": 0}}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        review_json = json.dumps(
            {
                "verdict": "OK",
                "confidence": 82,
                "reasons": ["acceptable setup"],
                "blocking_flags": [],
                "summary": "Acceptable for next gated review.",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp) / "reviews"
            research_root = Path(tmp) / "research"
            policy["research"]["review_dir"] = str(review_dir)
            paths = {
                "decisions": Path(tmp) / "decisions.jsonl",
                "outcomes": Path(tmp) / "outcomes.jsonl",
                "suggestions": Path(tmp) / "suggestions.jsonl",
            }
            paths["decisions"].write_text("", encoding="utf-8")
            paths["outcomes"].write_text("", encoding="utf-8")
            with mock.patch.object(hermes_operator, "DEFAULT_RESEARCH_ROOT", research_root), mock.patch.object(
                hermes_operator, "_arena_learning_paths", return_value=paths
            ), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ), mock.patch.object(
                hermes_operator, "_run_codex_review_command", return_value={"status": "OK", "review_json": review_json}
            ) as provider:
                research_output = hermes_operator._arena_growth_research_output(policy, policy_path=Path("arena.json"), top_n=1)
                propose_output = hermes_operator._arena_growth_propose_output(policy, policy_path=Path("arena.json"), top_n=1)
                review_artifacts = list(review_dir.glob("*.json"))
                accounting_artifacts = list((research_root / "arena_growth_research").glob("*.json"))

        self.assertEqual(research_output["status"], "OK")
        self.assertEqual(research_output["research_calls"][0]["symbol"], "KO@XNYS")
        self.assertTrue(research_output["research_calls"][0]["provider_call"])
        self.assertFalse(research_output["research_calls"][0]["cache_hit"])
        self.assertEqual(research_output["research_calls"][0]["verdict"], "OK")
        self.assertEqual(research_output["research_calls"][0]["growth_verdict"], "BUY")
        self.assertTrue(review_artifacts)
        self.assertTrue(accounting_artifacts)
        provider.assert_called_once()
        self.assertEqual(propose_output["research_queue"], [])
        self.assertEqual(propose_output["auction"]["top_candidates"][0]["gate_reasons"], [])

    def test_arena_growth_research_uses_cache_without_provider_call(self):
        policy = self.sample_arena_policy()
        policy["portfolio"] = {"min_candidate_score_for_buy": 75}
        policy["research"] = {"provider": "codex_review", "review_dir": "", "codex_review_ttl_minutes": 240}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="KO@XNYS")
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_unavailable_below_exceptional_score"]
        scan["candidates"][0]["arena_growth_score"] = {"score": 80, "components": {"research_regime": 0}}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        review_json = json.dumps(
            {
                "verdict": "RISK",
                "confidence": 76,
                "reasons": ["mixed setup"],
                "blocking_flags": ["manual_review"],
                "summary": "Needs manual review.",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            policy["research"]["review_dir"] = str(Path(tmp) / "reviews")
            research_root = Path(tmp) / "research"
            with mock.patch.object(hermes_operator, "DEFAULT_RESEARCH_ROOT", research_root), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ), mock.patch.object(
                hermes_operator, "_run_codex_review_command", return_value={"status": "OK", "review_json": review_json}
            ):
                first = hermes_operator._arena_growth_research_output(policy, policy_path=Path("arena.json"), top_n=1)
            with mock.patch.object(hermes_operator, "DEFAULT_RESEARCH_ROOT", research_root), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ), mock.patch.object(
                hermes_operator, "_run_codex_review_command", side_effect=AssertionError("cache hit must not call provider")
            ):
                second = hermes_operator._arena_growth_research_output(policy, policy_path=Path("arena.json"), top_n=1)

        self.assertTrue(first["research_calls"][0]["provider_call"])
        self.assertFalse(second["research_calls"][0]["provider_call"])
        self.assertTrue(second["research_calls"][0]["cache_hit"])
        self.assertEqual(second["research_calls"][0]["verdict"], "RISK")

    def test_arena_growth_research_provider_unavailable_degrades_and_redacts_secrets(self):
        policy = self.sample_arena_policy()
        policy["research"] = {"provider": "codex_review", "review_dir": tempfile.mkdtemp(), "codex_review_ttl_minutes": 240}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="KO@XNYS")
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_unavailable_below_exceptional_score"]
        scan["candidates"][0]["arena_growth_score"] = {"score": 80}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "DEFAULT_RESEARCH_ROOT", Path(tmp) / "research"
        ), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(
            hermes_operator,
            "build_arena_portfolio_review",
            return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
        ), mock.patch.object(
            hermes_operator, "build_arena_opportunity_auction", return_value=auction
        ), mock.patch.object(
            hermes_operator,
            "_run_codex_review_command",
            return_value={"status": "FAILED", "reason": "codex_review_command_failed", "error": "bad token secret-12345"},
        ):
            output = hermes_operator._arena_growth_research_output(
                policy,
                policy_path=Path("arena.json"),
                top_n=1,
                env={"OPENAI_API_KEY": "secret-12345"},
            )

        self.assertEqual(output["status"], "DEGRADED")
        self.assertTrue(output["research_calls"][0]["provider_call"])
        self.assertEqual(output["research_calls"][0]["verdict"], "UNAVAILABLE")
        self.assertNotIn("secret-12345", json.dumps(output))
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertFalse(output["safety"]["policy_write"])
        self.assertFalse(output["safety"]["risk_expansion"])

    def test_arena_growth_research_budget_exhausted_blocks_provider_call(self):
        policy = self.sample_arena_policy()
        policy["research"] = {
            "provider": "codex_review",
            "review_dir": tempfile.mkdtemp(),
            "codex_review_ttl_minutes": 240,
            "arena_growth_research_daily_budget": 0,
        }
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="KO@XNYS")
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_unavailable_below_exceptional_score"]
        scan["candidates"][0]["arena_growth_score"] = {"score": 80}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "DEFAULT_RESEARCH_ROOT", Path(tmp) / "research"
        ), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(
            hermes_operator,
            "build_arena_portfolio_review",
            return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
        ), mock.patch.object(
            hermes_operator, "build_arena_opportunity_auction", return_value=auction
        ), mock.patch.object(
            hermes_operator, "_run_codex_review_command", side_effect=AssertionError("budget exhausted must not call provider")
        ):
            output = hermes_operator._arena_growth_research_output(policy, policy_path=Path("arena.json"), top_n=1)

        self.assertEqual(output["status"], "DEGRADED")
        self.assertEqual(output["research_calls"], [])
        self.assertFalse(output["skipped_research_items"][0]["provider_call"])
        self.assertEqual(output["skipped_research_items"][0]["reason"], "arena_growth_research_budget_exhausted")
        self.assertFalse(output["safety"]["trading_mutations"])
        self.assertFalse(output["safety"]["policy_write"])
        self.assertFalse(output["safety"]["risk_expansion"])

    def test_arena_growth_research_unavailable_cache_prevents_repeat_provider_and_requeues_with_explicit_reason(self):
        policy = self.sample_arena_policy()
        policy["portfolio"] = {"min_candidate_score_for_buy": 75}
        policy["research"] = {"provider": "codex_review", "review_dir": "", "codex_review_ttl_minutes": 240}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="MCD@XNYS")
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_unavailable_below_exceptional_score"]
        scan["candidates"][0]["arena_growth_score"] = {"score": 80, "components": {"research_regime": 0}}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        review_json = json.dumps(
            {
                "verdict": "UNAVAILABLE",
                "confidence": 90,
                "reasons": ["insufficient evidence"],
                "blocking_flags": ["research_unavailable"],
                "summary": "Provider could not establish a useful edge.",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            policy["research"]["review_dir"] = str(Path(tmp) / "reviews")
            research_root = Path(tmp) / "research"
            paths = {
                "decisions": Path(tmp) / "decisions.jsonl",
                "outcomes": Path(tmp) / "outcomes.jsonl",
                "suggestions": Path(tmp) / "suggestions.jsonl",
            }
            paths["decisions"].write_text("", encoding="utf-8")
            paths["outcomes"].write_text("", encoding="utf-8")
            with mock.patch.object(hermes_operator, "DEFAULT_RESEARCH_ROOT", research_root), mock.patch.object(
                hermes_operator, "_arena_learning_paths", return_value=paths
            ), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ), mock.patch.object(
                hermes_operator, "_run_codex_review_command", return_value={"status": "OK", "review_json": review_json}
            ) as provider:
                first = hermes_operator._arena_growth_research_output(policy, policy_path=Path("arena.json"), top_n=1)
            with mock.patch.object(hermes_operator, "DEFAULT_RESEARCH_ROOT", research_root), mock.patch.object(
                hermes_operator, "_arena_learning_paths", return_value=paths
            ), mock.patch.object(
                hermes_operator, "build_arena_scan", return_value=scan
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            ), mock.patch.object(
                hermes_operator, "build_arena_opportunity_auction", return_value=auction
            ), mock.patch.object(
                hermes_operator, "_run_codex_review_command", side_effect=AssertionError("UNAVAILABLE cache must suppress repeated provider call")
            ):
                second = hermes_operator._arena_growth_research_output(policy, policy_path=Path("arena.json"), top_n=1)
                proposal = hermes_operator._arena_growth_propose_output(policy, policy_path=Path("arena.json"), top_n=1)

        self.assertTrue(first["research_calls"][0]["provider_call"])
        provider.assert_called_once()
        self.assertFalse(second["research_calls"][0]["provider_call"])
        self.assertTrue(second["research_calls"][0]["cache_hit"])
        self.assertEqual(second["research_calls"][0]["reason"], "provider_returned_unavailable_cached")
        self.assertEqual(proposal["research_queue"], [])
        gates = proposal["auction"]["top_candidates"][0]["gate_reasons"]
        self.assertIn("research_provider_returned_unavailable", gates)
        self.assertNotIn("research_unavailable_below_exceptional_score", gates)

    def test_arena_growth_research_unsupported_mic_is_skipped_before_provider(self):
        policy = self.sample_arena_policy()
        policy["research"] = {"provider": "codex_review", "review_dir": tempfile.mkdtemp(), "codex_review_ttl_minutes": 240}
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="QQQ@XNMS")
        scan["candidates"][0]["execution_allowed"] = False
        scan["candidates"][0]["gate_reasons"] = ["research_unavailable_below_exceptional_score"]
        scan["candidates"][0]["arena_growth_score"] = {"score": 80}
        auction = {
            "status": "OK",
            "command": "arena-opportunity-auction",
            "score_model": {"live_use_allowed": False},
            "winner": {"opportunity_type": "hold_cash", "risk_adjusted_score": 60},
            "opportunities": [{"opportunity_type": "hold_cash", "risk_adjusted_score": 60}],
            "research": {"provider_call": False},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "DEFAULT_RESEARCH_ROOT", Path(tmp) / "research"
        ), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=scan
        ), mock.patch.object(
            hermes_operator,
            "build_arena_portfolio_review",
            return_value={"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
        ), mock.patch.object(
            hermes_operator, "build_arena_opportunity_auction", return_value=auction
        ), mock.patch.object(
            hermes_operator, "_run_codex_review_command", side_effect=AssertionError("unsupported MIC must not call provider")
        ):
            output = hermes_operator._arena_growth_research_output(policy, policy_path=Path("arena.json"), top_n=1)

        self.assertEqual(output["status"], "DEGRADED")
        self.assertEqual(output["research_calls"], [])
        self.assertEqual(output["skipped_research_items"][0]["symbol"], "QQQ@XNMS")
        self.assertEqual(output["skipped_research_items"][0]["reason"], "unsupported_symbol_or_mic")

    def test_arena_growth_propose_cli_outputs_json(self):
        policy = self.sample_arena_policy()
        with mock.patch.object(hermes_operator, "load_arena_policy", return_value=policy), mock.patch.object(
            hermes_operator,
            "_arena_growth_propose_output",
            return_value={"status": "NO_ACTION", "command": "arena-growth-propose", "safety": {"trading_mutations": False, "policy_write": False}},
        ) as growth, mock.patch.object(sys, "argv", ["hermes_operator.py", "arena-growth-propose", "--top-n", "3"]):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["command"], "arena-growth-propose")
        self.assertEqual(growth.call_args.kwargs["top_n"], 3)

    def test_arena_growth_universe_rejects_removing_active_symbol(self):
        proposal = {"add_universe": ["MS@XNYS"], "remove_universe": ["META@XNGS"]}

        output = hermes_operator._arena_growth_universe_guard(
            "DEMO-US",
            proposal,
            {"DEMO-US": {"META@XNGS"}},
        )

        self.assertFalse(output["ok"])
        self.assertEqual(output["reason"], "cannot_remove_held_or_open_symbol")

    def test_arena_growth_loop_dry_run_plans_without_policy_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "arena.json"
            policy = self.sample_arena_policy()
            policy["portfolio"] = {
                "min_candidate_score_for_buy": 75,
                "min_candidate_score_for_buy_by_account": {"DEMO-RU": 75, "DEMO-US": 80, "DEMO-AI": 80},
            }
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
            scan["candidates"][0]["status"] = "BLOCKED"
            scan["candidates"][0]["gate_reasons"] = ["entry_strength_confirmation_missing"]
            scan["candidates"][0]["arena_growth_score"] = {"score": 70}
            proposal = {
                "status": "PROPOSED",
                "patch": {"learning": {"last_objective": "risk_adjusted_growth"}},
                "report": {},
            }
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan), mock.patch.object(
                hermes_operator, "_arena_news_scan_output", return_value={"status": "OK"}
            ), mock.patch.object(hermes_operator, "_arena_news_propose_output", return_value={"status": "OK"}), mock.patch.object(
                hermes_operator, "build_learning_proposal", return_value=proposal
            ), mock.patch.object(
                hermes_operator, "build_arena_status", return_value={"accounts": []}
            ):
                output = hermes_operator._arena_growth_loop_output(
                    policy,
                    policy_path=policy_path,
                    apply=False,
                    daily=True,
                    max_reviews=3,
                    review_timeout_seconds=5,
                    env={
                        "FINAM_ARENA_GROWTH_LOOP_AUDIT_PATH": str(Path(tmp) / "audit.jsonl"),
                        "FINAM_ARENA_GROWTH_LOOP_STATE_PATH": str(Path(tmp) / "state.json"),
                    },
                )

            self.assertEqual(output["mode"], "dry_run")
            self.assertFalse(output["write_applied"])
            self.assertFalse(output["safety"]["policy_write"])
            self.assertEqual(output["learning"]["status"], "PLANNED")

    def test_arena_strategy_callback_returns_proposal_without_write(self):
        policy = self.sample_arena_policy()

        output = hermes_operator._arena_view_output(
            policy,
            policy_path=Path("config/finam_arena_policy.json"),
            callback="arena:st:DEMO-RU:auto",
        )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["command"], "arena-view")
        self.assertIn("Arena Strategy Proposal", output["telegram_text"])
        self.assertEqual(output["strategy_proposal"]["proposed_policy"]["accounts"][0]["trade_mode"], "auto")
        self.assertFalse(output["strategy_proposal"]["write_applied"])
        self.assertFalse(output["strategy_proposal"]["safety"]["policy_write"])
        self.assertFalse(output["safety"]["policy_write"])

    def test_arena_strategy_risk_callback_clamps_safe_multiplier(self):
        policy = self.sample_arena_policy()
        policy["accounts"][0]["risk_multiplier"] = "2.0"

        output = hermes_operator._arena_view_output(
            policy,
            policy_path=Path("config/finam_arena_policy.json"),
            callback="arena:st:DEMO-RU:risk_up",
        )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["strategy_proposal"]["proposed_policy"]["accounts"][0]["risk_multiplier"], "2")
        self.assertFalse(output["strategy_proposal"]["write_applied"])

    def test_arena_medium_high_overrides_are_scoped(self):
        from finam_trading_bot import arena

        portfolio = {
            "opportunity_exceptional_base_score": 84,
            "opportunity_exceptional_base_score_by_account": {"DEMO-RU": 73},
            "opportunity_exceptional_base_score_by_mic": {"MISX": 74},
        }
        self.assertEqual(
            arena._research_unavailable_exceptional_score(
                portfolio,
                {"account_id": "DEMO-RU", "symbol": "SBER@MISX"},
            ),
            73,
        )
        self.assertEqual(
            arena._research_unavailable_exceptional_score(
                portfolio,
                {"account_id": "DEMO-US", "symbol": "MSFT@XNGS"},
            ),
            84,
        )

        risk = {
            "max_position_notional_pct": "25",
            "max_position_notional_pct_by_account": {"DEMO-RU": "37.5"},
            "max_symbol_exposure_pct": "25",
            "max_symbol_exposure_pct_by_account": {"DEMO-RU": "37.5"},
        }
        self.assertEqual(str(arena._arena_max_position_notional_pct(risk, account_id="DEMO-RU", symbol="SBER@MISX")), "37.5")
        self.assertEqual(str(arena._arena_max_position_notional_pct(risk, account_id="DEMO-US", symbol="MSFT@XNGS")), "25")
        self.assertEqual(
            str(arena._arena_risk_limit_pct(risk, "max_symbol_exposure_pct", account_id="DEMO-RU", symbol="SBER@MISX", default="100")),
            "37.5",
        )

    def test_arena_strategy_apply_requires_confirm_and_writes_backup(self):
        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "arena.json"
            backup_dir = Path(tmp) / "backups"
            policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")

            blocked = hermes_operator._arena_strategy_apply_output(
                policy,
                policy_path=policy_path,
                account_id="DEMO-RU",
                mode="autonomous",
                risk_multiplier=None,
                pause=True,
                resume=False,
                trade_mode=None,
                add_universe=[],
                remove_universe=[],
                confirm="",
                backup_dir=backup_dir,
            )
            applied = hermes_operator._arena_strategy_apply_output(
                policy,
                policy_path=policy_path,
                account_id="DEMO-RU",
                mode="autonomous",
                risk_multiplier=None,
                pause=True,
                resume=False,
                trade_mode=None,
                add_universe=[],
                remove_universe=[],
                confirm="APPLY_ARENA_STRATEGY",
                backup_dir=backup_dir,
            )

            written = json.loads(policy_path.read_text(encoding="utf-8"))
            backup_exists = Path(applied["backup_path"]).exists()

        self.assertEqual(blocked["status"], "CONFIRMATION_REQUIRED")
        self.assertFalse(blocked["write_applied"])
        self.assertEqual(applied["status"], "OK")
        self.assertTrue(applied["write_applied"])
        self.assertTrue(backup_exists)
        self.assertEqual(written["mode"], "autonomous")
        self.assertTrue(written["accounts"][0]["paused"])
        self.assertTrue(applied["safety"]["policy_write"])

    def test_arena_emergency_stop_confirm_writes_halt_policy(self):
        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["accounts"][1]["trade_mode"] = "auto"
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "arena.json"
            backup_dir = Path(tmp) / "backups"
            policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")

            preview = hermes_operator._arena_emergency_stop_output(
                policy,
                policy_path=policy_path,
                confirm="",
                backup_dir=backup_dir,
            )
            applied = hermes_operator._arena_emergency_stop_output(
                policy,
                policy_path=policy_path,
                confirm="ARENA_EMERGENCY_STOP",
                backup_dir=backup_dir,
            )
            written = json.loads(policy_path.read_text(encoding="utf-8"))

        self.assertEqual(preview["status"], "CONFIRMATION_REQUIRED")
        self.assertFalse(preview["write_applied"])
        self.assertEqual(applied["status"], "OK")
        self.assertTrue(applied["write_applied"])
        self.assertEqual(written["mode"], "approval")
        self.assertTrue(written["emergency_stop"])
        self.assertTrue(all(account["paused"] for account in written["accounts"]))
        self.assertTrue(all(account["trade_mode"] == "manual" for account in written["accounts"]))

    def test_research_daily_cli_writes_artifact_without_trading_mutations(self):
        result_payload = {
            "status": "ok",
            "date": "2026-05-25",
            "provider": "openrouter",
            "priority_watchlist": ["SBER@MISX"],
            "expires_at": "2026-05-25T23:00:00+00:00",
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator.h4_monitor,
            "load_policy",
            return_value=self.sample_policy(),
        ), mock.patch.object(
            hermes_operator,
            "daily_digest",
            return_value=result_payload,
        ), mock.patch.object(
            hermes_operator,
            "write_research_artifact",
            wraps=lambda kind, payload: {
                "kind": kind,
                "json_path": str(Path(tmp) / "daily.json"),
                "md_path": str(Path(tmp) / "daily.md"),
                "payload": payload,
            },
        ), mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "research-daily"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["command"], "research-daily")
        self.assertFalse(payload["safety"]["trading_mutations"])
        self.assertFalse(payload["safety"]["policy_write"])
        self.assertEqual(payload["artifact"]["kind"], "daily")

    def test_research_pretrade_rejects_invalid_symbol_before_provider_call(self):
        with mock.patch.object(
            hermes_operator.h4_monitor,
            "load_policy",
            return_value=self.sample_policy(),
        ), mock.patch.object(hermes_operator, "pretrade_check") as pretrade_check, mock.patch.object(
            hermes_operator, "write_event"
        ), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "research-pretrade", "BAD"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "INVALID")
        self.assertEqual(payload["command"], "research-pretrade")
        self.assertEqual(payload["artifact"], None)
        pretrade_check.assert_not_called()

    def test_research_budget_report_summarizes_local_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            h4_dir = root / "h4_gate"
            pretrade_dir = root / "pretrade"
            budget_dir = root / "openrouter_budget"
            h4_dir.mkdir()
            pretrade_dir.mkdir()
            budget_dir.mkdir()
            today = datetime.now(timezone.utc).date().isoformat()
            (budget_dir / f"{today}.json").write_text(
                json.dumps(
                    {
                        "period": today,
                        "calls": [
                            {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "mode": "h4_news_gate",
                                "model": "perplexity/sonar",
                                "symbols": ["SBER@MISX"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (h4_dir / "sonar.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "provider": "openrouter",
                        "mode": "h4_news_gate",
                        "model": "perplexity/sonar",
                        "symbols": ["SBER@MISX"],
                        "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
                    }
                ),
                encoding="utf-8",
            )
            (pretrade_dir / "sonar-pro.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "provider": "openrouter",
                        "mode": "pretrade_check",
                        "model": "perplexity/sonar-pro",
                        "symbols": ["LKOH@MISX"],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                    }
                ),
                encoding="utf-8",
            )
            (h4_dir / "rss.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "provider": "finam_rss",
                        "mode": "h4_news_gate",
                        "model": "finam_rss",
                        "symbols": ["MOEX@MISX"],
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
                sys,
                "argv",
                ["hermes_operator.py", "research-budget-report", "--root", str(root)],
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["command"], "research-budget-report")
        self.assertEqual(payload["artifact_records"], 3)
        self.assertEqual(payload["provider_call_records"], 2)
        self.assertEqual(payload["budget_attempts"]["records"], 1)
        self.assertEqual(payload["budget_attempts"]["groups"][0]["attempts"], 1)
        self.assertEqual(payload["totals"]["prompt_tokens"], 210)
        self.assertEqual(payload["totals"]["completion_tokens"], 120)
        self.assertEqual(payload["totals"]["total_tokens"], 330)
        self.assertEqual(payload["totals"]["cache_misses"], 0)
        self.assertEqual(payload["totals"]["estimated_cost_usd"], "0.00063")
        self.assertFalse(payload["safety"]["trading_mutations"])
        self.assertFalse(payload["safety"]["policy_write"])

    def test_research_budget_report_rejects_invalid_date(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys,
            "argv",
            ["hermes_operator.py", "research-budget-report", "--root", tmp, "--date", "20260608"],
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "INVALID")
        self.assertEqual(payload["reason"], "invalid_date")

    def test_autonomous_run_cli_blocks_before_report_without_env_gate(self):
        policy = self.sample_policy()
        policy["mode"] = "autonomous_demo"
        with mock.patch.object(hermes_operator.h4_monitor, "load_policy", return_value=policy), mock.patch.object(
            hermes_operator.h4_monitor, "build_report"
        ) as build_report, mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys, "argv", ["hermes_operator.py", "autonomous-run", "--live"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(payload["reason"], "FINAM_H4_AUTONOMOUS_DEMO_ENABLED_not_true")
        build_report.assert_not_called()

    def test_validate_policy_accepts_current_shape(self):
        validation = hermes_operator.validate_policy(self.sample_policy())

        self.assertEqual(validation["errors"], [])

    def test_validate_policy_accepts_report_only_growth_mode(self):
        policy = self.sample_policy()
        policy["growth_mode"] = {
            "enabled": False,
            "report_only": True,
            "target_annual_return": "0.5",
            "stretch_annual_return": "1.0",
            "min_growth_score_for_buy": 75,
            "min_growth_score_for_watch": 60,
            "max_new_trades_per_run": 1,
            "max_open_positions": 5,
            "risk_per_trade_pct": "1.0",
            "allow_second_tier": False,
            "allow_short_analysis": True,
            "allow_short_orders": False,
        }

        validation = hermes_operator.validate_policy(policy)

        self.assertEqual(validation["errors"], [])

    def test_validate_policy_rejects_active_growth_or_short_orders(self):
        policy = self.sample_policy()
        policy["growth_mode"] = {
            "enabled": True,
            "report_only": False,
            "target_annual_return": "-0.5",
            "stretch_annual_return": "1.0",
            "min_growth_score_for_buy": 101,
            "min_growth_score_for_watch": 60,
            "max_new_trades_per_run": 1,
            "max_open_positions": 5,
            "risk_per_trade_pct": "1.0",
            "allow_second_tier": False,
            "allow_short_analysis": True,
            "allow_short_orders": True,
        }

        validation = hermes_operator.validate_policy(policy)

        self.assertIn("growth_mode.enabled must remain false until growth execution has a separate approved gate", validation["errors"])
        self.assertIn("growth_mode.report_only must remain true until growth execution has a separate approved gate", validation["errors"])
        self.assertIn("growth_mode.allow_short_orders must remain false until short availability is separately approved", validation["errors"])
        self.assertIn("growth_mode.target_annual_return must be a positive number", validation["errors"])
        self.assertIn("growth_mode.min_growth_score_for_buy must be <= 100", validation["errors"])

    def test_policy_propose_returns_diff_without_writing(self):
        output = hermes_operator._policy_propose_output(
            self.sample_policy(),
            policy_path=Path("policy.json"),
            mode=None,
            set_values=["risk.risk_per_trade_pct=0.5", "research.max_candidates=4"],
            add_universe=["TATN@MISX"],
            remove_universe=["GAZP@MISX"],
        )

        self.assertEqual(output["status"], "OK")
        self.assertFalse(output["write_applied"])
        self.assertEqual(output["proposed_policy"]["risk"]["risk_per_trade_pct"], 0.5)
        self.assertEqual(output["proposed_policy"]["research"]["max_candidates"], 4)
        self.assertIn("TATN@MISX", output["proposed_policy"]["universe"])
        self.assertNotIn("GAZP@MISX", output["proposed_policy"]["universe"])
        self.assertEqual(output["validation"]["errors"], [])

    def test_policy_propose_rejects_unknown_set_path(self):
        output = hermes_operator._policy_propose_output(
            self.sample_policy(),
            policy_path=Path("policy.json"),
            mode=None,
            set_values=["permissions.new_buys_require_telegram_confirmation=false"],
            add_universe=[],
            remove_universe=[],
        )

        self.assertEqual(output["status"], "INVALID")
        self.assertIn("policy path is not editable", output["validation"]["errors"][0])
        self.assertTrue(output["safety"]["read_only_operator_cli"])

    def test_policy_apply_requires_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(json.dumps(self.sample_policy()), encoding="utf-8")

            output = hermes_operator._policy_apply_output(
                self.sample_policy(),
                policy_path=policy_path,
                mode=None,
                set_values=["risk.risk_per_trade_pct=0.5"],
                add_universe=[],
                remove_universe=[],
                confirm="",
                backup_dir=Path(tmp) / "backups",
            )

            stored = json.loads(policy_path.read_text(encoding="utf-8"))
            self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
            self.assertFalse(output["write_applied"])
            self.assertEqual(stored["risk"]["risk_per_trade_pct"], "1.0")
            self.assertFalse((Path(tmp) / "backups").exists())

    def test_policy_apply_writes_valid_policy_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            backup_dir = Path(tmp) / "backups"
            policy_path.write_text(json.dumps(self.sample_policy()), encoding="utf-8")

            output = hermes_operator._policy_apply_output(
                self.sample_policy(),
                policy_path=policy_path,
                mode=None,
                set_values=["risk.risk_per_trade_pct=0.5"],
                add_universe=["TATN@MISX"],
                remove_universe=[],
                confirm="APPLY_POLICY",
                backup_dir=backup_dir,
            )

            stored = json.loads(policy_path.read_text(encoding="utf-8"))
            backup = json.loads(Path(output["backup_path"]).read_text(encoding="utf-8"))
            self.assertEqual(output["status"], "OK")
            self.assertTrue(output["write_applied"])
            self.assertFalse(output["safety"]["trading_mutations"])
            self.assertFalse(output["safety"]["production_send"])
            self.assertTrue(output["safety"]["policy_write"])
            self.assertEqual(stored["risk"]["risk_per_trade_pct"], 0.5)
            self.assertIn("TATN@MISX", stored["universe"])
            self.assertEqual(backup["risk"]["risk_per_trade_pct"], "1.0")

    def test_policy_apply_rejects_invalid_policy_without_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            backup_dir = Path(tmp) / "backups"
            policy_path.write_text(json.dumps(self.sample_policy()), encoding="utf-8")

            output = hermes_operator._policy_apply_output(
                self.sample_policy(),
                policy_path=policy_path,
                mode=None,
                set_values=["risk.risk_per_trade_pct=-1"],
                add_universe=[],
                remove_universe=[],
                confirm="APPLY_POLICY",
                backup_dir=backup_dir,
            )

            stored = json.loads(policy_path.read_text(encoding="utf-8"))
            self.assertEqual(output["status"], "INVALID")
            self.assertFalse(output["write_applied"])
            self.assertEqual(stored["risk"]["risk_per_trade_pct"], "1.0")
            self.assertFalse(backup_dir.exists())

    def test_policy_validate_cli_reports_invalid_policy(self):
        invalid = self.sample_policy()
        invalid["risk"]["risk_per_trade_pct"] = "-1"

        with mock.patch.object(hermes_operator.h4_monitor, "load_policy", return_value=invalid), mock.patch.object(
            hermes_operator, "write_event"
        ), mock.patch.object(sys, "argv", ["hermes_operator.py", "--policy", "policy.json", "policy-validate"]):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "INVALID")
        self.assertIn("risk.risk_per_trade_pct must be a positive number", payload["validation"]["errors"])


    def test_arena_confirm_blocks_us_pending_approval_before_regular_session_without_broker_submit(self):
        class ArenaClient:
            def create_session(self, secret):
                raise AssertionError("market-session gate must block arena-confirm before broker session/order submit")

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("market-session gate must not submit /orders while US market is closed")

        class MarketClient:
            def create_session(self, secret):
                raise AssertionError("market-session gate must block before market price revalidation")

        policy = self.sample_arena_policy()
        policy["accounts"][1]["universe"] = ["AAPL@XNGS"]
        scan = self._arena_scan_with_candidate(account_id="DEMO-US", symbol="AAPL@XNGS")
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending),
            }
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                run = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-US",
                    live=True,
                    confirmation="",
                    env=env,
                )
            hermes_operator.persist_arena_pending_approvals_from_run_all(
                {"runs": [run]},
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                now=datetime(2026, 6, 4, 12, 55, tzinfo=timezone.utc),
                env=env,
            )
            output = hermes_operator._arena_confirm_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                confirmation="CONFIRM_ARENA_BUY AAPL@XNGS DEMO-US",
                live=True,
                env=env,
                client=ArenaClient(),
                market_client=MarketClient(),
                now=datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(output["status"], "WAIT_MARKET_CLOSED")
        self.assertEqual(output["reason"], "us_regular_session_closed")
        self.assertEqual(output["market_session"]["mic"], "XNGS")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_confirm_live_executes_triggered_stops_before_order(self):
        class ArenaClient:
            def create_session(self, secret):
                raise AssertionError("arena-confirm must not create broker session after triggered stop exit")

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("arena-confirm must not submit buy after triggered stop exit")

        policy = self.sample_arena_policy()
        scan = self._arena_scan_with_candidate(account_id="DEMO-RU")
        stop_check = {
            "status": "ARENA_SOFT_STOP_TRIGGERED",
            "triggered": [{"account_id": "DEMO-RU", "symbol": "SBER@MISX", "check_status": "EXIT_SUBMITTED"}],
            "safety": {"trading_mutations": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending),
            }
            with mock.patch.object(hermes_operator, "build_arena_scan", return_value=scan):
                run = hermes_operator._arena_run_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    live=True,
                    confirmation="",
                    env=env,
                )
            hermes_operator.persist_arena_pending_approvals_from_run_all(
                {"runs": [run]},
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                now=datetime(2026, 6, 4, 12, 55, tzinfo=timezone.utc),
                env=env,
            )
            with mock.patch.object(
                hermes_operator, "_arena_revalidated_market_price", return_value=(hermes_operator.Decimal("300.00"), None)
            ), mock.patch.object(
                hermes_operator, "_arena_check_stops_output", return_value=stop_check
            ) as check_stops:
                output = hermes_operator._arena_confirm_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                    live=True,
                    client=ArenaClient(),
                    env=env,
                    now=datetime(2026, 6, 4, 12, 56, tzinfo=timezone.utc),
                )

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["reason"], "arena_stop_check_not_clear")
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertTrue(check_stops.call_args.kwargs["execute_triggered_stops"])

    def test_arena_confirm_portfolio_live_executes_triggered_stops_before_review(self):
        policy = self.sample_arena_policy()
        stop_check = {
            "status": "ARENA_SOFT_STOP_TRIGGERED",
            "triggered": [{"account_id": "DEMO-US", "symbol": "KO@XNYS", "check_status": "EXIT_SUBMITTED"}],
            "safety": {"trading_mutations": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending.json"
            env = {
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_PENDING_APPROVALS_PATH": str(pending),
                "HERMES_TRADE_SAFETY_STATE": str(Path(tmp) / "safety.json"),
            }
            hermes_operator.persist_arena_pending_approvals_from_run_all(
                {
                    "runs": [
                        {
                            "status": "CONFIRMATION_REQUIRED",
                            "command": "arena-portfolio-run",
                            "account_id": "DEMO-RU",
                            "action": "REPLACE",
                            "required_confirmation": "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                            "replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                        }
                    ]
                },
                policy_path=Path("config/finam_arena_policy.json"),
                pending_path=pending,
                now=datetime(2026, 6, 4, 12, 55, tzinfo=timezone.utc),
                env=env,
            )
            with mock.patch.object(
                hermes_operator,
                "_arena_check_stops_output",
                return_value=stop_check,
            ) as check_stops, mock.patch.object(
                hermes_operator,
                "build_arena_scan",
                side_effect=AssertionError("portfolio confirm review must wait for triggered stop exit"),
            ), mock.patch.object(
                hermes_operator,
                "build_arena_portfolio_review",
                side_effect=AssertionError("portfolio confirm action must wait for triggered stop exit"),
            ):
                output = hermes_operator._arena_confirm_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    confirmation="CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                    live=True,
                    env=env,
                    now=datetime(2026, 6, 4, 12, 56, tzinfo=timezone.utc),
                )

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["reason"], "arena_stop_check_not_clear")
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertTrue(check_stops.call_args.kwargs["execute_triggered_stops"])

    def test_arena_execute_live_requires_internal_mutation_context_before_broker_submit(self):
        class ArenaClient:
            def create_session(self, secret):
                raise AssertionError("direct _arena_execute_live_output must block before broker session/order submit")

        proposal = {
            "proposal": {
                "account_id": "DEMO-RU",
                "symbol": "SBER@MISX",
                "side": "BUY",
                "quantity": "10",
                "entry": {"limit_price": "300.00"},
                "protective_stop": {"side": "SELL", "stop_price": "290.00"},
            },
            "order_payloads": {
                "entry_order": {"symbol": "SBER@MISX", "side": "SIDE_BUY", "quantity": {"value": "10.0"}},
                "protective_stop": {"symbol": "SBER@MISX", "side": "SELL", "quantity": {"value": "10.0"}},
            },
        }

        output = hermes_operator._arena_execute_live_output(
            self.sample_arena_policy(),
            proposal,
            policy_path=Path("config/finam_arena_policy.json"),
            account_id="DEMO-RU",
            confirmation="CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
            client=ArenaClient(),
            env={"FINAM_ARENA_API": "secret"},
        )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["reason"], "missing_arena_mutation_context")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_replace_portfolio_action_requires_supervised_confirmation_before_sell_or_buy(self):
        class ArenaClient:
            def place_order(self, jwt, account_id, payload):
                raise AssertionError("REPLACE must not sell/buy without supervised confirmation snapshot")

        output = hermes_operator._execute_arena_portfolio_action(
            self.sample_arena_policy(),
            policy_path=Path("config/finam_arena_policy.json"),
            action={"account_id": "DEMO-RU", "action": "REPLACE", "sell_symbol": "SBER@MISX"},
            account_review={"replacement": {"sell_symbol": "SBER@MISX", "buy_candidate": {"symbol": "GAZP@MISX"}}},
            scan_account={"account_id": "DEMO-RU", "equity": "1000000"},
            client=ArenaClient(),
            jwt="arena-jwt",
            env={"FINAM_ARENA_API": "secret"},
        )

        self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(output["reason"], "arena_replace_requires_supervised_confirmation")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_portfolio_run_live_executes_triggered_stops_before_review(self):
        policy = self.sample_arena_policy()
        stop_check = {
            "status": "ARENA_SOFT_STOP_TRIGGERED",
            "triggered": [{"account_id": "DEMO-AI", "symbol": "KO@XNYS", "check_status": "EXIT_SUBMITTED"}],
            "safety": {"trading_mutations": True},
        }
        scan = {"status": "OK", "accounts": [{"account_id": "DEMO-AI"}], "candidates": [], "research": {"status": "skipped"}}

        with mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value=stop_check) as check_stops, mock.patch.object(
            hermes_operator, "build_arena_portfolio_review", side_effect=AssertionError("portfolio review must wait for triggered stop exit")
        ):
            output = hermes_operator._arena_portfolio_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
                scan=scan,
            )

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["reason"], "arena_stop_check_not_clear")
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertTrue(check_stops.call_args.kwargs["execute_triggered_stops"])

    def test_arena_portfolio_run_auto_executes_exit_weak_for_auto_account(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "positions": [
                        {"symbol": "LKOH@MISX", "quantity": {"value": "52.0"}, "current_price": "4638.50"},
                    ]
                }

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                return {
                    "order_id": "exit-lkoh",
                    "status": "ORDER_STATUS_FILLED",
                    "executed_quantity": payload["quantity"]["value"],
                    "execution_price": "4638.50",
                }

        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["accounts"][2]["trade_mode"] = "auto"
        policy["portfolio"] = {"autonomy_mode": "full_rotation"}
        scan = {"status": "OK", "accounts": [{"account_id": "DEMO-AI"}], "candidates": [], "research": {"status": "skipped"}}
        review = {
            "status": "OK",
            "accounts": [{"account_id": "DEMO-AI", "positions": []}],
            "planned_actions": [
                {"account_id": "DEMO-AI", "action": "EXIT_WEAK", "symbol": "LKOH@MISX", "quantity": "52.0", "reason": "negative_r_progress"}
            ],
            "exit_proposals": [],
            "replacement_proposals": [],
        }
        client = ArenaClient()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}
        ), mock.patch.object(hermes_operator, "build_arena_portfolio_review", return_value=review):
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(Path(tmp) / "ledger.jsonl"),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            output = hermes_operator._arena_portfolio_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
                env=env,
                client=client,
                scan=scan,
            )
            ledger = (Path(tmp) / "ledger.jsonl").read_text(encoding="utf-8")

        self.assertEqual(output["status"], "EXECUTED_ARENA_PORTFOLIO")
        self.assertEqual(client.orders, [{"symbol": "LKOH@MISX", "side": "SIDE_SELL", "quantity": {"value": "52.0"}}])
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["results"][0]["auto_execution"]["allowed"])
        self.assertNotIn("required_confirmation", output["results"][0])
        self.assertIn("LKOH@MISX", ledger)

    def test_arena_portfolio_run_waits_for_us_session_before_auto_exit(self):
        class ArenaClient:
            def create_session(self, secret):
                raise AssertionError("market-session guard must block before broker session")

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("market-session guard must not submit broker orders")

        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["accounts"][1]["trade_mode"] = "auto"
        policy["portfolio"] = {"autonomy_mode": "full_rotation"}
        scan = {
            "status": "OK",
            "accounts": [{"account_id": "DEMO-US"}],
            "candidates": [],
            "research": {"status": "skipped"},
        }
        review = {
            "status": "OK",
            "accounts": [{"account_id": "DEMO-US", "positions": []}],
            "planned_actions": [
                {
                    "account_id": "DEMO-US",
                    "action": "EXIT_WEAK",
                    "symbol": "XOM@XNYS",
                    "quantity": "1716.0",
                    "reason": "negative_r_progress",
                }
            ],
            "exit_proposals": [],
            "replacement_proposals": [],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}
        ), mock.patch.object(hermes_operator, "build_arena_portfolio_review", return_value=review):
            safety = Path(tmp) / "safety.json"
            output = hermes_operator._arena_portfolio_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
                env={
                    "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                    "FINAM_ARENA_API": "secret",
                    "HERMES_TRADE_SAFETY_STATE": str(safety),
                },
                client=ArenaClient(),
                scan=scan,
                now=datetime(2026, 6, 18, 8, 7, tzinfo=timezone.utc),
            )

            self.assertFalse(safety.exists())

        self.assertEqual(output["status"], "WAIT_MARKET_CLOSED")
        self.assertEqual(output["results"][0]["reason"], "us_regular_session_closed")
        self.assertEqual(output["results"][0]["market_session"]["mic"], "XNYS")
        self.assertEqual(output["results"][0]["market_session"]["opens_at_msk"], "2026-06-18T16:30:00+03:00")
        self.assertTrue(output["results"][0]["halt_new_entries"])
        self.assertFalse(output["broker_mutation"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_portfolio_run_exits_auto_keeps_replace_confirmation_required(self):
        class ArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("exits_auto must not execute REPLACE")

        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["accounts"][0]["trade_mode"] = "auto"
        policy["portfolio"] = {"autonomy_mode": "exits_auto"}
        scan = {"status": "OK", "accounts": [{"account_id": "DEMO-RU"}], "candidates": [], "research": {"status": "skipped"}}
        review = {
            "status": "OK",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                }
            ],
            "planned_actions": [
                {"account_id": "DEMO-RU", "action": "REPLACE", "sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"}
            ],
            "exit_proposals": [],
            "replacement_proposals": [],
        }

        with mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}
        ), mock.patch.object(hermes_operator, "build_arena_portfolio_review", return_value=review):
            output = hermes_operator._arena_portfolio_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
                client=ArenaClient(),
                scan=scan,
            )

        self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(output["results"][0]["reason"], "arena_replace_requires_supervised_confirmation")
        self.assertFalse(output["results"][0]["auto_execution"]["allowed"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_portfolio_run_full_rotation_replaces_and_halts_when_buy_revalidation_fails(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "300.00"}]}

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                if len(self.orders) > 1:
                    raise AssertionError("buy leg must not submit when post-sell gates fail")
                return {
                    "order_id": "sell-order",
                    "status": "ORDER_STATUS_FILLED",
                    "executed_quantity": payload["quantity"]["value"],
                    "execution_price": "300.00",
                }

        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["accounts"][0]["trade_mode"] = "auto"
        policy["portfolio"] = {"autonomy_mode": "full_rotation"}
        initial_scan = {"status": "OK", "accounts": [{"account_id": "DEMO-RU"}], "candidates": [], "research": {"status": "skipped"}}
        review = {
            "status": "OK",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                }
            ],
            "planned_actions": [
                {"account_id": "DEMO-RU", "action": "REPLACE", "sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"}
            ],
            "exit_proposals": [],
            "replacement_proposals": [],
        }
        post_sell_scan = self._arena_scan_with_candidate(account_id="DEMO-RU", symbol="PLZL@MISX", quantity="2", stop="14500.00")
        post_sell_scan["candidates"][0]["execution_allowed"] = False
        post_sell_scan["candidates"][0]["gate_reasons"] = ["symbol_exposure_limit_exceeded"]
        client = ArenaClient()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}
        ), mock.patch.object(hermes_operator, "build_arena_portfolio_review", return_value=review), mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=post_sell_scan
        ):
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(Path(tmp) / "ledger.jsonl"),
                "HERMES_TRADE_SAFETY_STATE": str(Path(tmp) / "safety.json"),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            output = hermes_operator._arena_portfolio_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
                env=env,
                client=client,
                scan=initial_scan,
            )
            safety_state = json.loads((Path(tmp) / "safety.json").read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["results"][0]["status"], "REPLACE_SELL_DONE_BUY_BLOCKED")
        self.assertEqual(output["results"][0]["reason"], "arena_replace_buy_revalidation_failed_after_sell")
        self.assertEqual(client.orders, [{"symbol": "SBER@MISX", "side": "SIDE_SELL", "quantity": {"value": "10.0"}}])
        self.assertTrue(output["results"][0]["auto_execution"]["allowed"])
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["results"][0]["halt_new_entries"])
        self.assertEqual(safety_state["status"], "REPLACE_SELL_DONE_BUY_BLOCKED")

    def test_arena_portfolio_run_auto_route_blockers_do_not_submit_orders(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []
                self.sessions = 0

            def create_session(self, secret):
                self.sessions += 1
                return "arena-jwt"

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                raise AssertionError("blocked auto route must not submit broker orders")

        policy = self.sample_arena_policy()
        policy["mode"] = "autonomous"
        policy["approval_until"] = "2099-06-03T23:59:59+03:00"
        policy["accounts"][2]["trade_mode"] = "auto"
        policy["portfolio"] = {"autonomy_mode": "full_rotation"}
        scan = {"status": "OK", "accounts": [{"account_id": "DEMO-AI"}], "candidates": [], "research": {"status": "skipped"}}
        review = {
            "status": "OK",
            "accounts": [{"account_id": "DEMO-AI", "positions": []}],
            "planned_actions": [{"account_id": "DEMO-AI", "action": "EXIT_WEAK", "symbol": "LKOH@MISX"}],
            "exit_proposals": [],
            "replacement_proposals": [],
        }
        client = ArenaClient()
        with mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}
        ), mock.patch.object(hermes_operator, "build_arena_portfolio_review", return_value=review):
            output = hermes_operator._arena_portfolio_run_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                live=True,
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
                client=client,
                scan=scan,
            )

        self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(output["results"][0]["auto_execution"]["reason"], "account_requires_confirmation")
        self.assertEqual(client.orders, [])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_position_sell_dry_run_plans_partial_sell_without_order(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "side": "LONG", "quantity": "320", "current_price": "318.50"}]}

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                raise AssertionError("dry-run must not submit broker orders")

        client = ArenaClient()
        output = hermes_operator._arena_position_sell_output(
            self.sample_arena_policy(),
            policy_path=Path("config/finam_arena_policy.json"),
            account_id="DEMO-RU",
            symbol="SBER@MISX",
            quantity="158",
            live=False,
            client=client,
            env={"FINAM_ARENA_API": "secret"},
        )

        self.assertEqual(output["status"], "DRY_RUN")
        self.assertEqual(output["planned_action"]["quantity"], "158.0")
        self.assertEqual(output["position_quantity"], "320.0")
        self.assertEqual(output["required_confirmation"], "CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158 DEMO-RU")
        self.assertFalse(client.orders)
        self.assertFalse(output["broker_mutation"])

    def test_arena_position_sell_live_executes_exact_manual_partial_sell(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "side": "LONG", "quantity": "320", "current_price": "318.50"}]}

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                return {
                    "order_id": "manual-sell-1",
                    "status": "ORDER_STATUS_FILLED",
                    "executed_quantity": payload["quantity"]["value"],
                    "execution_price": "318.50",
                }

        client = ArenaClient()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}
        ):
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(Path(tmp) / "ledger.jsonl"),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            output = hermes_operator._arena_position_sell_output(
                self.sample_arena_policy(),
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                quantity="158",
                live=True,
                confirmation="CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158 DEMO-RU",
                client=client,
                env=env,
            )
            ledger_lines = (Path(tmp) / "ledger.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(output["status"], "EXECUTED_ARENA_POSITION_SELL")
        self.assertEqual(client.orders, [{"symbol": "SBER@MISX", "side": "SIDE_SELL", "quantity": {"value": "158.0"}}])
        self.assertEqual(output["exit_order_payload"]["quantity"]["value"], "158.0")
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["halt_new_entries"])
        self.assertEqual(len(ledger_lines), 1)

    def test_arena_position_sell_halts_when_residual_soft_stop_is_already_triggered(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "side": "LONG", "quantity": "1553", "current_price": "320.04"}]}

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                return {
                    "order_id": "manual-sell-1",
                    "status": "ORDER_STATUS_FILLED",
                    "executed_quantity": payload["quantity"]["value"],
                    "execution_price": "320.04",
                }

        client = ArenaClient()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}
        ):
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "1553.0",
                                "stop_price": "322.22",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(Path(tmp) / "ledger.jsonl"),
                "HERMES_TRADE_SAFETY_STATE": str(safety),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            output = hermes_operator._arena_position_sell_output(
                self.sample_arena_policy(),
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                quantity="158",
                live=True,
                confirmation="CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158 DEMO-RU",
                client=client,
                env=env,
            )
            safety_state = json.loads(safety.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "EXECUTED_ARENA_POSITION_SELL_RESIDUAL_STOP_TRIGGERED")
        self.assertEqual(output["reason"], "arena_residual_soft_stop_triggered_after_exit")
        self.assertEqual(output["soft_stop_update"]["remaining_quantity"], "1395.0")
        self.assertEqual(output["residual_stop"]["stop_price"], "322.22")
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["halt_new_entries"])
        self.assertTrue(safety_state["halt_new_buys"])
        self.assertEqual(safety_state["status"], "EXECUTED_ARENA_EXIT_RESIDUAL_STOP_TRIGGERED")
        self.assertEqual(safety_state["arena_soft_stops"][0]["quantity"], "1395.0")

    def test_arena_position_sell_rejects_old_sell_confirmation_without_mutation(self):
        class ArenaClient:
            def create_session(self, secret):
                raise AssertionError("confirmation gate must block before broker session")

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("confirmation gate must block before broker order")

        output = hermes_operator._arena_position_sell_output(
            self.sample_arena_policy(),
            policy_path=Path("config/finam_arena_policy.json"),
            account_id="DEMO-RU",
            symbol="SBER@MISX",
            quantity="158",
            live=True,
            confirmation="CONFIRM_ARENA_SELL SBER@MISX DEMO-RU",
            client=ArenaClient(),
            env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
        )

        self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(output["required_confirmation"], "CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158 DEMO-RU")
        self.assertFalse(output["broker_mutation"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_position_sell_live_executes_triggered_stops_before_manual_sell(self):
        class ArenaClient:
            def create_session(self, secret):
                raise AssertionError("manual sell must not create broker session after triggered stop exit")

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("manual sell must not submit after triggered stop exit")

        stop_check = {
            "status": "ARENA_SOFT_STOP_TRIGGERED",
            "triggered": [{"account_id": "DEMO-US", "symbol": "KO@XNYS", "check_status": "EXIT_SUBMITTED"}],
            "safety": {"trading_mutations": True},
        }

        with mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value=stop_check) as check_stops:
            output = hermes_operator._arena_position_sell_output(
                self.sample_arena_policy(),
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                quantity="158",
                live=True,
                confirmation="CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158 DEMO-RU",
                client=ArenaClient(),
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "HALT")
        self.assertEqual(output["reason"], "arena_stop_check_not_clear")
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertTrue(check_stops.call_args.kwargs["execute_triggered_stops"])

    def test_arena_position_sell_rejects_quantity_above_live_position_without_order(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "side": "LONG", "quantity": "120", "current_price": "318.50"}]}

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                raise AssertionError("oversized partial sell must not submit broker orders")

        client = ArenaClient()
        with mock.patch.object(
            hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}
        ):
            output = hermes_operator._arena_position_sell_output(
                self.sample_arena_policy(),
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                quantity="158",
                live=True,
                confirmation="CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158 DEMO-RU",
                client=client,
                env={"FINAM_ARENA_AUTO_TRADE_ENABLED": "true", "FINAM_ARENA_API": "secret"},
            )

        self.assertEqual(output["status"], "NO_TRADE")
        self.assertEqual(output["reason"], "requested_quantity_exceeds_position")
        self.assertEqual(output["position_quantity"], "120.0")
        self.assertFalse(client.orders)
        self.assertFalse(output["broker_mutation"])

    def test_arena_position_sell_cli_parser_routes_arguments(self):
        with mock.patch.object(hermes_operator, "load_arena_policy", return_value=self.sample_arena_policy()), mock.patch.object(
            hermes_operator,
            "_arena_position_sell_output",
            return_value={"status": "DRY_RUN", "command": "arena-position-sell", "safety": {"trading_mutations": False}},
        ) as route, mock.patch.object(hermes_operator, "write_event"), mock.patch.object(
            sys,
            "argv",
            [
                "hermes_operator.py",
                "--arena-policy",
                "config/finam_arena_policy.json",
                "arena-position-sell",
                "--account",
                "DEMO-RU",
                "--symbol",
                "SBER@MISX",
                "--quantity",
                "158",
                "--live",
                "--confirmation",
                "CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158 DEMO-RU",
            ],
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = hermes_operator.main()

        self.assertEqual(result, 0)
        route.assert_called_once()
        kwargs = route.call_args.kwargs
        self.assertEqual(kwargs["account_id"], "DEMO-RU")
        self.assertEqual(kwargs["symbol"], "SBER@MISX")
        self.assertEqual(kwargs["quantity"], "158")
        self.assertTrue(kwargs["live"])
        self.assertEqual(kwargs["confirmation"], "CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158 DEMO-RU")
        self.assertEqual(json.loads(stdout.getvalue())["command"], "arena-position-sell")

    def test_confirmed_arena_replace_sells_first_revalidates_then_buys(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                if not self.orders:
                    return {
                        "positions": [
                            {"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "300.00"},
                        ]
                    }
                if len(self.orders) == 1:
                    return {"positions": []}
                return {
                    "positions": [
                        {"symbol": "PLZL@MISX", "quantity": {"value": "2.0"}, "current_price": "15000.00"},
                    ]
                }

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                return {
                    "order_id": f"order-{len(self.orders)}",
                    "status": "ORDER_STATUS_FILLED",
                    "executed_quantity": (payload.get("quantity") or {}).get("value"),
                    "execution_price": "300.00" if payload.get("side") == "SIDE_SELL" else "15000.00",
                }

        policy = self.sample_arena_policy()
        client = ArenaClient()
        post_sell_scan = self._arena_scan_with_candidate(account_id="DEMO-RU", symbol="PLZL@MISX", quantity="2", stop="14500.00")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hermes_operator, "build_arena_scan", return_value=post_sell_scan):
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(Path(tmp) / "ledger.jsonl"),
                "HERMES_TRADE_SAFETY_STATE": str(Path(tmp) / "safety.json"),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            output = hermes_operator._execute_confirmed_arena_replacement(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                action={"account_id": "DEMO-RU", "action": "REPLACE", "sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                account_review={"replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"}},
                confirmation="CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                client=client,
                jwt="arena-jwt",
                env=env,
                market_client=None,
            )

        self.assertEqual(output["status"], "EXECUTED_ARENA_REPLACEMENT")
        self.assertEqual([order["symbol"] for order in client.orders], ["SBER@MISX", "PLZL@MISX"])
        self.assertEqual(client.orders[0]["side"], "SIDE_SELL")
        self.assertEqual(client.orders[1]["side"], "SIDE_BUY")
        self.assertTrue(output["safety"]["trading_mutations"])

    def test_confirmed_arena_replace_stops_after_sell_when_buy_revalidation_fails(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "300.00"}]}

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                if len(self.orders) > 1:
                    raise AssertionError("buy leg must not submit when fresh gates fail")
                return {
                    "order_id": "sell-order",
                    "status": "ORDER_STATUS_FILLED",
                    "executed_quantity": (payload.get("quantity") or {}).get("value"),
                    "execution_price": "300.00",
                }

        policy = self.sample_arena_policy()
        post_sell_scan = self._arena_scan_with_candidate(account_id="DEMO-RU", symbol="PLZL@MISX", quantity="2", stop="14500.00")
        post_sell_scan["candidates"][0]["execution_allowed"] = False
        post_sell_scan["candidates"][0]["gate_reasons"] = ["symbol_exposure_limit_exceeded"]
        client = ArenaClient()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(hermes_operator, "build_arena_scan", return_value=post_sell_scan):
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(Path(tmp) / "ledger.jsonl"),
                "HERMES_TRADE_SAFETY_STATE": str(Path(tmp) / "safety.json"),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            output = hermes_operator._execute_confirmed_arena_replacement(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                action={"account_id": "DEMO-RU", "action": "REPLACE", "sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                account_review={"replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"}},
                confirmation="CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                client=client,
                jwt="arena-jwt",
                env=env,
                market_client=None,
            )
            safety_state = json.loads((Path(tmp) / "safety.json").read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "REPLACE_SELL_DONE_BUY_BLOCKED")
        self.assertEqual(output["reason"], "arena_replace_buy_revalidation_failed_after_sell")
        self.assertEqual(output["buy_gate_reasons"], ["symbol_exposure_limit_exceeded"])
        self.assertEqual([order["symbol"] for order in client.orders], ["SBER@MISX"])
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["halt_new_entries"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertEqual(safety_state["status"], "REPLACE_SELL_DONE_BUY_BLOCKED")

    def test_confirmed_arena_replace_executes_triggered_stops_before_buy_after_sell(self):
        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "300.00"}]}

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                if len(self.orders) > 1:
                    raise AssertionError("buy leg must not submit after triggered stop exit")
                return {
                    "order_id": "sell-order",
                    "status": "ORDER_STATUS_FILLED",
                    "executed_quantity": (payload.get("quantity") or {}).get("value"),
                    "execution_price": "300.00",
                }

        policy = self.sample_arena_policy()
        post_sell_scan = self._arena_scan_with_candidate(account_id="DEMO-RU", symbol="PLZL@MISX", quantity="2", stop="14500.00")
        stop_check = {
            "status": "ARENA_SOFT_STOP_TRIGGERED",
            "triggered": [{"account_id": "DEMO-US", "symbol": "KO@XNYS", "check_status": "EXIT_SUBMITTED"}],
            "safety": {"trading_mutations": True},
        }
        client = ArenaClient()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=post_sell_scan
        ), mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value=stop_check) as check_stops:
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(Path(tmp) / "ledger.jsonl"),
                "HERMES_TRADE_SAFETY_STATE": str(Path(tmp) / "safety.json"),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            output = hermes_operator._execute_confirmed_arena_replacement(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                action={"account_id": "DEMO-RU", "action": "REPLACE", "sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                account_review={"replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"}},
                confirmation="CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                client=client,
                jwt="arena-jwt",
                env=env,
                market_client=None,
            )

        self.assertEqual(output["status"], "REPLACE_SELL_DONE_BUY_BLOCKED")
        self.assertEqual(output["reason"], "arena_replace_buy_stop_check_not_clear_after_sell")
        self.assertEqual([order["symbol"] for order in client.orders], ["SBER@MISX"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertTrue(check_stops.call_args.kwargs["execute_triggered_stops"])

    def test_confirmed_arena_replace_does_not_buy_when_price_drift_fails_after_sell(self):
        class MarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def last_quote(self, jwt, symbol):
                return {"last": "330.00"}

        class ArenaClient:
            def __init__(self):
                self.orders = []

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                if not self.orders:
                    return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "300.00"}]}
                return {"positions": []}

            def place_order(self, jwt, account_id, payload):
                self.orders.append(payload)
                if len(self.orders) > 1:
                    raise AssertionError("buy leg must not submit when price drift fails")
                return {
                    "order_id": "sell-order",
                    "status": "ORDER_STATUS_FILLED",
                    "executed_quantity": (payload.get("quantity") or {}).get("value"),
                    "execution_price": "300.00",
                }

        policy = self.sample_arena_policy()
        post_sell_scan = self._arena_scan_with_candidate(account_id="DEMO-RU", symbol="PLZL@MISX", quantity="2", stop="14500.00")
        client = ArenaClient()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            hermes_operator, "build_arena_scan", return_value=post_sell_scan
        ), mock.patch.object(hermes_operator, "_arena_check_stops_output", return_value={"status": "OK", "safety": {"trading_mutations": False}}):
            env = {
                "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                "FINAM_ARENA_API": "secret",
                "FINAM_TOKEN": "market-secret",
                "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(Path(tmp) / "ledger.jsonl"),
                "HERMES_TRADE_SAFETY_STATE": str(Path(tmp) / "safety.json"),
                "FINAM_BUY_FILL_CHECKS": "1",
                "FINAM_BUY_FILL_SLEEP_SECONDS": "0",
            }
            output = hermes_operator._execute_confirmed_arena_replacement(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                action={"account_id": "DEMO-RU", "action": "REPLACE", "sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                account_review={"replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"}},
                confirmation="CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                client=client,
                jwt="arena-jwt",
                env=env,
                market_client=MarketClient(),
            )
            safety_state = json.loads((Path(tmp) / "safety.json").read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "REPLACE_SELL_DONE_BUY_BLOCKED")
        self.assertEqual(output["reason"], "approval_price_drift_exceeded")
        self.assertEqual(output["buy_revalidation"]["reason"], "approval_price_drift_exceeded")
        self.assertEqual([order["symbol"] for order in client.orders], ["SBER@MISX"])
        self.assertTrue(output["broker_mutation"])
        self.assertTrue(output["halt_new_entries"])
        self.assertTrue(output["safety"]["trading_mutations"])
        self.assertEqual(safety_state["status"], "REPLACE_SELL_DONE_BUY_BLOCKED")

    def test_arena_check_stops_live_is_read_only_unless_execute_triggered_stops_is_explicit(self):
        class CheckClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "280.00"}]}

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("arena-check-stops --live must not execute triggered stops")

        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "10.0",
                                "stop_price": "290.00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = hermes_operator._arena_check_stops_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                live=True,
                client=CheckClient(),
                env={
                    "FINAM_ARENA_API": "secret",
                    "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(safety),
                },
            )

        self.assertEqual(output["status"], "STOP_TRIGGERED_DRY_RUN")
        self.assertEqual(output["command"], "arena-check-stops")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_check_stops_enriches_missing_current_price_from_trade_api_quote(self):
        class CheckClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "1395.0"}, "current_price": None}]}

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("dry-run stop check must not place an order")

        class MarketClient:
            def create_session(self, token):
                self.token = token
                return "trade-jwt"

            def last_quote(self, jwt, symbol):
                self.jwt = jwt
                self.symbol = symbol
                return {"last": {"value": "315.87"}}

        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "1395.0",
                                "stop_price": "322.22",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(hermes_operator, "FinamClient", MarketClient):
                output = hermes_operator._arena_check_stops_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    symbol="SBER@MISX",
                    live=False,
                    client=CheckClient(),
                    env={
                        "FINAM_ARENA_API": "secret",
                        "FINAM_TOKEN": "trade-token",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                    },
                )

        self.assertEqual(output["status"], "STOP_TRIGGERED_DRY_RUN")
        self.assertEqual(output["checks"][0]["check_status"], "TRIGGERED")
        self.assertEqual(output["checks"][0]["current_price"], "315.87")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_check_stops_keeps_safe_read_only_result_when_quote_enrichment_fails(self):
        class CheckClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "1395.0"}, "current_price": None}]}

        class MarketClient:
            def create_session(self, token):
                return "trade-jwt"

            def last_quote(self, jwt, symbol):
                raise RuntimeError("quote unavailable")

        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "1395.0",
                                "stop_price": "322.22",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(hermes_operator, "FinamClient", MarketClient):
                output = hermes_operator._arena_check_stops_output(
                    policy,
                    policy_path=Path("config/finam_arena_policy.json"),
                    account_id="DEMO-RU",
                    symbol="SBER@MISX",
                    live=False,
                    client=CheckClient(),
                    env={
                        "FINAM_ARENA_API": "secret",
                        "FINAM_TOKEN": "trade-token",
                        "HERMES_TRADE_SAFETY_STATE": str(safety),
                    },
                )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["checks"][0]["check_status"], "ACTIVE")
        self.assertIsNone(output["checks"][0]["current_price"])
        self.assertIn("warnings", output)
        self.assertIn("quote unavailable", output["warnings"][0])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_arena_check_stops_read_only_does_not_remove_soft_stop_state_when_position_is_gone(self):
        class CheckClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": []}

        policy = self.sample_arena_policy()
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "10.0",
                                "stop_price": "290.00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = hermes_operator._arena_check_stops_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                live=False,
                client=CheckClient(),
                env={"FINAM_ARENA_API": "secret", "HERMES_TRADE_SAFETY_STATE": str(safety)},
            )
            state_after = json.loads(safety.read_text(encoding="utf-8"))

        self.assertEqual(output["checks"][0]["check_status"], "NO_POSITION")
        self.assertTrue(output["checks"][0]["would_remove_soft_stop"])
        self.assertNotIn("soft_stop_removed", output["checks"][0])
        self.assertEqual(state_after["arena_soft_stops"][0]["symbol"], "SBER@MISX")

    def test_arena_execute_triggered_stops_writes_execution_ledger(self):
        class CheckClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "280.00"}]}

            def place_order(self, jwt, account_id, payload):
                return {
                    "order_id": "stop-exit-1",
                    "order": {
                        "symbol": "SBER@MISX",
                        "side": "SIDE_SELL",
                        "quantity": {"value": "10.0"},
                        "execution_price": {"value": "279.50"},
                        "commission": {"value": "0.98"},
                    },
                }

        policy = self.sample_arena_policy()
        policy["auto_trade_env"] = "FINAM_ARENA_AUTO_TRADE_ENABLED"
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            ledger = Path(tmp) / "ledger.jsonl"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "10.0",
                                "stop_price": "290.00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = hermes_operator._arena_check_stops_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                live=True,
                execute_triggered_stops=True,
                client=CheckClient(),
                env={
                    "FINAM_ARENA_API": "secret",
                    "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(safety),
                    "FINAM_ARENA_EXECUTION_LEDGER_PATH": str(ledger),
                },
            )
            ledger_record = json.loads(ledger.read_text(encoding="utf-8").strip())

        self.assertEqual(output["status"], "ARENA_SOFT_STOP_TRIGGERED")
        self.assertEqual(output["checks"][0]["check_status"], "EXIT_SUBMITTED")
        self.assertEqual(output["checks"][0]["execution_ledger_delivery"], "ok")
        self.assertEqual(ledger_record["account_id"], "DEMO-RU")
        self.assertEqual(ledger_record["symbol"], "SBER@MISX")
        self.assertEqual(ledger_record["side"], "SELL")
        self.assertEqual(ledger_record["action"], "SOFT_STOP_TRIGGERED")
        self.assertEqual(ledger_record["order_id"], "stop-exit-1")
        self.assertEqual(ledger_record["quantity"], "10.0")
        self.assertEqual(ledger_record["price"], "279.50")

    def test_arena_execute_triggered_stops_skips_order_when_soft_stop_already_removed_under_lock(self):
        class CheckClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                safety.write_text("{}", encoding="utf-8")
                return {"positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10.0"}, "current_price": "280.00"}]}

            def place_order(self, jwt, account_id, payload):
                raise AssertionError("second process must not submit duplicate soft-stop exit")

        policy = self.sample_arena_policy()
        policy["auto_trade_env"] = "FINAM_ARENA_AUTO_TRADE_ENABLED"
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(
                json.dumps(
                    {
                        "halt_new_buys": False,
                        "status": "ARENA_SOFT_STOPS_ACTIVE",
                        "arena_soft_stops": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-RU",
                                "symbol": "SBER@MISX",
                                "side": "SELL",
                                "quantity": "10.0",
                                "stop_price": "290.00",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = hermes_operator._arena_check_stops_output(
                policy,
                policy_path=Path("config/finam_arena_policy.json"),
                account_id="DEMO-RU",
                symbol="SBER@MISX",
                live=True,
                execute_triggered_stops=True,
                client=CheckClient(),
                env={
                    "FINAM_ARENA_API": "secret",
                    "FINAM_ARENA_AUTO_TRADE_ENABLED": "true",
                    "HERMES_TRADE_SAFETY_STATE": str(safety),
                },
            )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["checks"][0]["check_status"], "SOFT_STOP_ALREADY_HANDLED")
        self.assertTrue(output["checks"][0]["soft_stop_already_removed"])
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_trade_buy_live_requires_exact_confirmation_not_only_intent(self):
        class Client:
            def create_session(self, token):
                raise AssertionError("trade-buy live must not reach broker without exact confirmation")

        output = hermes_operator._trade_buy_output(
            self.sample_proposal_report(),
            symbol="SBER@MISX",
            intent="купи SBER@MISX",
            live=True,
            client=Client(),
            env={"FINAM_DEMO_LIVE_TRADING_ENABLED": "true", "FINAM_TOKEN": "secret"},
        )

        self.assertEqual(output["status"], "CONFIRMATION_REQUIRED")
        self.assertEqual(output["required_confirmation"], "CONFIRM_BUY SBER@MISX")
        self.assertFalse(output["safety"]["trading_mutations"])


if __name__ == "__main__":
    unittest.main()
