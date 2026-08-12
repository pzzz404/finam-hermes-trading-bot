import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location("arena_executor_notify_script", ROOT / "scripts" / "arena_executor_notify.py")
arena_executor_notify = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(arena_executor_notify)


class ArenaExecutorNotifyTests(unittest.TestCase):
    def setUp(self):
        self._executor_gate_patch = mock.patch.dict(
            os.environ,
            {"FINAM_ARENA_EXECUTOR_MUTATIONS_ENABLED": "true"},
            clear=False,
        )
        self._executor_gate_patch.start()
        self.addCleanup(self._executor_gate_patch.stop)

    def sample_policy(self):
        return {"accounts": [{"account_id": "DEMO-RU"}]}

    def confirmation_run_all(self):
        return {
            "status": "CONFIRMATION_REQUIRED",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "CONFIRMATION_REQUIRED",
                    "account_id": "DEMO-RU",
                    "required_confirmation": "CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
                    "proposal": {
                        "proposal": {
                            "account_id": "DEMO-RU",
                            "symbol": "SBER@MISX",
                            "side": "BUY",
                            "quantity": 10,
                            "entry": {"limit_price": "300.123456"},
                            "protective_stop": {"side": "SELL", "stop_price": "290.987654321"},
                            "take_profit": {"price": "320.555555"},
                            "risk": {"risk_rub": "100.123456", "risk_per_share": "10.987654321", "notional": "3000.123456"},
                            "gates": {"execution_allowed": True, "gate_reasons": []},
                            "execution": {"confirmation_phrase": "CONFIRM_ARENA_BUY SBER@MISX DEMO-RU"},
                        },
                        "candidate": {
                            "signal": {"h4": "up", "h1": "up", "m30": "flat"},
                            "costs": {
                                "entry_cost_rub": "0.531",
                                "round_trip_cost_rub": "2.1",
                                "break_even_move_pct": "0.07",
                            },
                        },
                        "order_payloads": {
                            "entry_order": {"symbol": "SBER@MISX", "side": "SIDE_BUY", "quantity": {"value": "10.0"}},
                            "protective_stop": {"symbol": "SBER@MISX", "side": "SELL", "quantity": {"value": "10.0"}, "stop_price": {"value": "290.00"}},
                        },
                    },
                    "safety": {"trading_mutations": False},
                }
            ],
            "safety": {"trading_mutations": False},
        }

    def executed_run_all(self):
        return {
            "status": "EXECUTED_ARENA",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
                    "account_id": "DEMO-US",
                    "proposal": {"proposal": {"symbol": "AAPL@XNGS"}},
                    "entry_fill_state": {"executed_quantity": "3"},
                    "soft_stop": {"side": "SELL", "stop_price": "190.987654321"},
                    "safety": {"trading_mutations": True},
                }
            ],
            "safety": {"trading_mutations": True},
        }

    def auto_buy_preview_run_all(self):
        return {
            "status": "DRY_RUN",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "DRY_RUN",
                    "command": "arena-run",
                    "account_id": "DEMO-US",
                    "proposal": {
                        "proposal": {
                            "account_id": "DEMO-US",
                            "symbol": "AAPL@XNGS",
                            "side": "BUY",
                            "quantity": 3,
                            "entry": {"limit_price": "190.00"},
                            "protective_stop": {"side": "SELL", "stop_price": "185.00"},
                            "take_profit": {"price": "200.00"},
                            "risk": {"risk_rub": "15.00", "risk_per_share": "5.00", "notional": "570.00"},
                            "gates": {"execution_allowed": True, "gate_reasons": []},
                            "execution": {
                                "route": "auto_direct",
                                "confirmation_kind": "live_authorization",
                                "confirmation_phrase": "CONFIRM_ARENA_BUY AAPL@XNGS DEMO-US",
                            },
                        },
                        "candidate": {
                            "symbol": "AAPL@XNGS",
                            "arena_growth_score": {
                                "score": 86,
                                "label": "HIGH",
                                "research_verdict": "OK",
                            },
                            "signal": {"h4": "up", "h1": "up", "m30": "up"},
                            "pretrade_check": {
                                "status": "ok",
                                "verdict": "OK",
                                "provider_call": True,
                                "budget": {"used": 1, "limit": 4, "remaining": 3},
                            },
                        },
                    },
                    "safety": {"trading_mutations": False},
                }
            ],
            "safety": {"trading_mutations": False},
        }

    def portfolio_executed_run_all(self):
        return {
            "status": "EXECUTED_ARENA_PORTFOLIO",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "EXECUTED_ARENA_PORTFOLIO",
                    "command": "arena-portfolio-run",
                    "planned_actions": [
                        {"account_id": "DEMO-US", "action": "EXIT_WEAK", "symbol": "MSFT@XNGS"},
                    ],
                    "results": [
                        {
                            "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
                            "command": "arena-portfolio-run",
                            "account_id": "DEMO-US",
                            "symbol": "MSFT@XNGS",
                            "action": "EXIT_WEAK",
                            "exit_order_payload": {"symbol": "MSFT@XNGS", "side": "SIDE_SELL"},
                            "exit_fill_state": {"status": "filled", "executed_quantity": "540"},
                            "broker_mutation": True,
                            "safety": {"trading_mutations": True},
                        }
                    ],
                    "safety": {"trading_mutations": True},
                }
            ],
            "safety": {"trading_mutations": True},
        }

    def planned_portfolio_run_all(self):
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "LIVE_GATE_REQUIRED",
                    "command": "arena-portfolio-run",
                    "reason": "FINAM_ARENA_AUTO_TRADE_ENABLED_not_true",
                    "planned_actions": [
                        {
                            "account_id": "DEMO-RU",
                            "action": "TAKE_PARTIAL_PROFIT",
                            "symbol": "SBER@MISX",
                            "quantity": "1553.0",
                            "reason": "take_partial_profit_threshold_reached",
                        },
                        {
                            "account_id": "DEMO-AI",
                            "action": "TRAIL_STOP",
                            "symbol": "SBER@MISX",
                            "quantity": "1562.0",
                            "reason": "trail_stop_threshold_reached",
                        },
                    ],
                    "safety": {"trading_mutations": False},
                }
            ],
            "safety": {"trading_mutations": False},
        }

    def portfolio_confirmation_run_all(self):
        return {
            "status": "CONFIRMATION_REQUIRED",
            "command": "arena-run-all",
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
                            "reason": "arena_replace_requires_supervised_confirmation",
                            "required_confirmation": "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                            "replacement": {
                                "sell_symbol": "SBER@MISX",
                                "buy_symbol": "PLZL@MISX",
                            },
                            "safety": {"trading_mutations": False},
                        }
                    ],
                    "safety": {"trading_mutations": False},
                }
            ],
            "safety": {"trading_mutations": False},
        }

    def blocked_anchor_rotation_run_all(self):
        return {
            "status": "BLOCKED",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "NO_ACTION",
                    "command": "arena-portfolio-run",
                    "review": {
                        "replacement_proposals": [
                            {
                                "account_id": "DEMO-RU",
                                "sell_symbol": "SBER@MISX",
                                "buy_symbol": "PLZL@MISX",
                                "score_delta": 18,
                                "estimated_cost_rub": "420.50",
                                "replacement_entries_used": 0,
                                "replacement_entries_limit": 1,
                                "confirmation_phrase": "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                            }
                        ]
                    },
                    "safety": {"trading_mutations": False},
                },
                {
                    "status": "BLOCKED",
                    "command": "arena-run",
                    "account_id": "DEMO-RU",
                    "reason": "arena_execution_gate_blocked",
                    "gate_reasons": ["single_symbol_anchor_rotation_candidate"],
                    "proposal": {
                        "account": {
                            "account_id": "DEMO-RU",
                            "label": "РФ",
                            "equity": "999655.10",
                            "pnl_pct": "-0.03",
                            "positions_count": 1,
                            "open_risk_pct": "0.00",
                            "top_signal": "нет исполнимого сигнала: single_symbol_anchor_rotation_candidate",
                        },
                        "candidate": {
                            "account_id": "DEMO-RU",
                            "symbol": "PLZL@MISX",
                            "arena_growth_score": {"score": 90, "label": "HIGH"},
                            "anchor_rotation": {
                                "source_symbol": "SBER@MISX",
                                "source_notional_pct": "52.5",
                                "source_progress_r": "0.25",
                                "source_holding_score": 72,
                                "candidate_score": 90,
                                "suggested_partial_sell_quantity": "158.0",
                                "confirmation_required": True,
                            },
                            "signal": {"h4": "up", "h1": "up", "m30": None},
                        },
                    },
                    "safety": {"trading_mutations": False},
                },
            ],
            "safety": {"trading_mutations": False},
        }

    def blocked_anchor_rotation_without_replacement_run_all(self):
        run_all = json.loads(json.dumps(self.blocked_anchor_rotation_run_all()))
        run_all["runs"][0]["review"]["replacement_proposals"] = []
        candidate = run_all["runs"][1]["proposal"]["candidate"]
        candidate["anchor_rotation"]["source_holding_score"] = 90
        candidate["anchor_rotation"]["candidate_score"] = 90
        return run_all

    def live_gate_before_preview_run_all(self):
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "LIVE_GATE_REQUIRED",
                    "command": "arena-check-stops",
                    "reason": "FINAM_ARENA_AUTO_TRADE_ENABLED_not_true",
                    "checks": [{"account_id": "DEMO-RU", "symbol": "SBER@MISX"}],
                    "safety": {"trading_mutations": False},
                }
            ],
            "safety": {"trading_mutations": False},
        }

    def blocked_run_all(self):
        return {
            "status": "BLOCKED",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "NO_ORDER",
                    "command": "arena-run",
                    "account_id": "DEMO-RU",
                    "reason": "no_active_arena_proposal",
                    "proposal": {
                        "account": {
                            "account_id": "DEMO-RU",
                            "label": "РФ",
                            "equity": "1006335.06",
                            "pnl_pct": "0.6335",
                            "positions_count": 2,
                            "open_risk_pct": "0.7822",
                            "top_signal": None,
                        }
                    },
                    "safety": {"trading_mutations": False},
                },
                {
                    "status": "NO_ORDER",
                    "command": "arena-run",
                    "account_id": "DEMO-US",
                    "reason": "no_active_arena_proposal",
                    "proposal": {
                        "account": {
                            "account_id": "DEMO-US",
                            "label": "США",
                            "equity": "984171.45",
                            "pnl_pct": "-1.5828",
                            "positions_count": 0,
                            "open_risk_pct": None,
                            "top_signal": None,
                        }
                    },
                    "safety": {"trading_mutations": False},
                },
                {
                    "status": "BLOCKED",
                    "command": "arena-run",
                    "account_id": "DEMO-AI",
                    "reason": "arena_execution_gate_blocked",
                    "gate_reasons": ["account_gross_exposure_limit_reached"],
                    "proposal": {
                        "account": {
                            "account_id": "DEMO-AI",
                            "label": "AI",
                            "equity": "994076.30",
                            "pnl_pct": "-0.5923",
                            "positions_count": 1,
                            "open_risk_pct": "0.3795",
                            "top_signal": "нет исполнимого сигнала: account_gross_exposure_limit_reached",
                        },
                        "candidate": {
                            "symbol": "LKOH@MISX",
                            "arena_growth_score": {"score": 79, "label": "HIGH"},
                            "signal": {"h4": "up", "h1": "up", "m30": "down"},
                        },
                    },
                    "safety": {"trading_mutations": False},
                },
            ],
            "safety": {"trading_mutations": False},
        }

    def test_confirmation_required_sends_compact_approval_alert(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.confirmation_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send, mock.patch.dict(
            os.environ, {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(Path(tmp) / "pending.json")}
        ):
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )
            pending = json.loads((Path(tmp) / "pending.json").read_text(encoding="utf-8"))

        self.assertEqual(output["telegram_delivery"], "ok")
        text = send.call_args.args[0]
        self.assertIn("<b>🏟️ Arena: торговый контур</b>", text)
        self.assertIn("<b>DEMO-RU</b> · SBER@MISX", text)
        self.assertIn("Цена/объём: 300.12 × 10; сумма 3 000.12", text)
        self.assertIn("Stop: 290.99; риск 100.12 (10.99 на акцию)", text)
        self.assertIn("TP: 320.56; R/R 2.24", text)
        self.assertIn("круг 2.1", text)
        self.assertIn("CONFIRM_ARENA_BUY SBER@MISX DEMO-RU", text)
        self.assertIn("Подтверждение действует 10 минут", text)
        self.assertEqual(pending["approvals"][0]["confirmation"], "CONFIRM_ARENA_BUY SBER@MISX DEMO-RU")
        self.assertEqual(pending["approvals"][0]["status"], "pending")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_portfolio_replacement_confirmation_sends_actionable_approval_alert(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.portfolio_confirmation_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send, mock.patch.dict(
            os.environ, {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(Path(tmp) / "pending.json")}
        ):
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )
            pending = json.loads((Path(tmp) / "pending.json").read_text(encoding="utf-8"))

        self.assertEqual(output["telegram_delivery"], "ok")
        self.assertNotEqual(output.get("suppressed_reason"), "stable_blocked_gate")
        text = send.call_args.args[0]
        self.assertIn("<b>DEMO-RU</b> · SBER@MISX→PLZL@MISX", text)
        self.assertIn("✋ Ожидает подтверждения: CONFIRM_ARENA_REPLACE SBER@MISX -&gt; PLZL@MISX DEMO-RU", text)
        self.assertIn("python scripts/hermes_operator.py arena-confirm --live --confirmation \"CONFIRM_ARENA_REPLACE SBER@MISX -&gt; PLZL@MISX DEMO-RU\"", text)
        self.assertIn("Ротация: продать SBER@MISX → купить PLZL@MISX", text)
        self.assertIn("ротация позиции требует отдельного подтверждения", text)
        self.assertNotIn("single_symbol_anchor_rotation_candidate", text)
        self.assertEqual(pending["approvals"][0]["confirmation"], "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU")
        self.assertEqual(pending["approvals"][0]["side"], "REPLACE")
        self.assertEqual(pending["approvals"][0]["status"], "pending")

    def test_duplicate_portfolio_replacement_approval_is_deduped(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.portfolio_confirmation_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send, mock.patch.dict(
            os.environ, {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(Path(tmp) / "pending.json")}
        ):
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            pending = json.loads((Path(tmp) / "pending.json").read_text(encoding="utf-8"))

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "deduped")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len([item for item in pending["approvals"] if item["status"] == "pending"]), 1)
        self.assertEqual(pending["approvals"][0]["side"], "REPLACE")

    def test_blocked_anchor_rotation_alert_is_actionable_without_fake_approval(self):
        text = arena_executor_notify.format_arena_executor_alert(self.blocked_anchor_rotation_run_all())

        self.assertIn("<b>DEMO-RU</b> · PLZL@MISX", text)
        self.assertIn("Кандидат на ротацию: SBER@MISX -> PLZL@MISX; требуется отдельное решение", text)
        self.assertIn("текущая доля 52.50%; прогресс 0.25R; score удержания 72; score идеи 90; Δ +18", text)
        self.assertIn("replacement: cost 420.50 RUB; лимит 0/1", text)
        self.assertIn("arena-position-sell --account DEMO-RU --symbol SBER@MISX --quantity 158.0", text)
        self.assertIn("CONFIRM_ARENA_SELL_PARTIAL SBER@MISX 158.0 DEMO-RU", text)
        self.assertIn("CONFIRM_ARENA_REPLACE SBER@MISX -&gt; PLZL@MISX DEMO-RU", text)
        self.assertIn("варианты: оставить якорную позицию; создать pending REPLACE snapshot через arena-portfolio-run; заблокировать ротацию на сегодня", text)
        self.assertIn("pending confirmation не создан", text)
        self.assertNotIn("нужен ручной разбор", text)
        self.assertNotIn("python scripts/hermes_operator.py arena-confirm", text)

    def test_blocked_anchor_rotation_without_replacement_does_not_promise_snapshot(self):
        text = arena_executor_notify.format_arena_executor_alert(self.blocked_anchor_rotation_without_replacement_run_all())

        self.assertIn("текущая доля 52.50%; прогресс 0.25R; score удержания 90; score идеи 90; Δ +0", text)
        self.assertIn("replacement snapshot не создан: PLZL@MISX не лучше SBER@MISX по текущей оценке; нужен ручной разбор", text)
        self.assertIn("варианты: оставить якорную позицию; ручной разбор ротации; заблокировать ротацию на сегодня", text)
        self.assertNotIn("создать CONFIRM_ARENA_REPLACE snapshot", text)
        self.assertNotIn("сначала нужен arena-portfolio-review/arena-portfolio-run для создания replace snapshot", text)

    def test_duplicate_alert_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.confirmation_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send, mock.patch.dict(
            os.environ, {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(Path(tmp) / "pending.json")}
        ):
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            pending = json.loads((Path(tmp) / "pending.json").read_text(encoding="utf-8"))

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "deduped")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len([item for item in pending["approvals"] if item["status"] == "pending"]), 1)

    def test_expired_duplicate_approval_sends_new_alert(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.confirmation_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send, mock.patch.dict(
            os.environ, {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(Path(tmp) / "pending.json")}
        ):
            state = Path(tmp) / "alert.json"
            pending_path = Path(tmp) / "pending.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            pending["approvals"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
            pending_path.write_text(json.dumps(pending), encoding="utf-8")

            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            refreshed = json.loads(pending_path.read_text(encoding="utf-8"))

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "ok")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len([item for item in refreshed["approvals"] if item["status"] == "pending"]), 1)
        self.assertEqual(len([item for item in refreshed["approvals"] if item["status"] == "expired"]), 1)

    def test_dry_run_does_not_call_live_executor(self):
        calls = []

        def fake_run_all(policy, *, policy_path, live, research_mode):
            calls.append((live, research_mode))
            return {"status": "NO_ORDER", "runs": [], "safety": {"trading_mutations": False}}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", side_effect=fake_run_all
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=True,
            )

        self.assertEqual(calls, [(False, 'cache_only')])
        self.assertFalse(output["live_requested"])
        self.assertFalse(output["safety"]["trading_mutations"])
        send.assert_not_called()

    def test_no_event_does_not_send(self):
        run_all = {"status": "NO_ORDER", "runs": [{"status": "NO_ORDER", "account_id": "DEMO-RU"}], "safety": {"trading_mutations": False}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=run_all
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(output["telegram_delivery"], "not_needed")
        send.assert_not_called()

    def test_first_stable_blocked_alert_sends_and_repeat_is_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.blocked_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "suppressed")
        self.assertEqual(second["suppressed_reason"], "stable_blocked_gate")
        self.assertEqual(send.call_count, 1)

    def test_research_unavailable_low_score_blocked_alert_repeat_is_not_suppressed(self):
        run_all = self.blocked_run_all()
        blocked = run_all["runs"][2]
        blocked["gate_reasons"] = [
            "research_unavailable_below_exceptional_score",
            "entry_strength_confirmation_missing",
            "candidate_score_below_min",
        ]
        blocked["proposal"]["account"]["top_signal"] = "нет исполнимого сигнала: research_unavailable_below_exceptional_score"
        blocked["proposal"]["candidate"]["arena_growth_score"] = {
            "score": 60,
            "label": "MEDIUM",
            "research_verdict": "UNAVAILABLE",
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=run_all
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "ok")
        self.assertNotEqual(second.get("suppressed_reason"), "stable_blocked_gate")
        self.assertEqual(send.call_count, 2)

    def test_changed_blocked_gate_reasons_inside_cooldown_is_suppressed(self):
        first_run = self.blocked_run_all()
        changed_run = self.blocked_run_all()
        changed_run["runs"][2]["gate_reasons"] = ["candidate_score_below_min"]
        changed_run["runs"][2]["proposal"]["account"]["top_signal"] = "нет исполнимого сигнала: candidate_score_below_min"

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", side_effect=[first_run, first_run, changed_run, changed_run]
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "suppressed")
        self.assertEqual((second.get("blocked_alert") or {}).get("reason"), "blocked_cooldown")
        self.assertEqual(send.call_count, 1)

    def test_blocked_alert_daily_reset_allows_first_alert_again(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.blocked_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            payload = json.loads(state.read_text(encoding="utf-8"))
            payload["blocked_alerts"]["date_msk"] = "2026-06-07"
            state.write_text(json.dumps(payload), encoding="utf-8")
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "ok")
        self.assertEqual(send.call_count, 2)

    def test_protection_blocked_gate_is_not_suppressed(self):
        run_all = self.blocked_run_all()
        blocked = run_all["runs"][2]
        blocked["gate_reasons"] = ["research_provider_failure"]
        blocked["proposal"]["account"]["top_signal"] = "нет исполнимого сигнала: research_provider_failure"

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=run_all
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "ok")
        self.assertEqual(send.call_count, 2)

    def test_corrupt_alert_state_does_not_block_first_stable_blocked_alert(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.blocked_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            state = Path(tmp) / "alert.json"
            state.write_text("{not-json", encoding="utf-8")
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            stored = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(output["telegram_delivery"], "ok")
        self.assertIn("blocked_alerts", stored)
        self.assertEqual(send.call_count, 1)

    def test_confirmation_required_is_not_blocked_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.confirmation_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send, mock.patch.dict(
            os.environ, {"FINAM_ARENA_PENDING_APPROVALS_PATH": str(Path(tmp) / "pending.json")}
        ):
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(output["telegram_delivery"], "ok")
        self.assertNotEqual(output.get("suppressed_reason"), "stable_blocked_gate")
        self.assertEqual(send.call_count, 1)

    def test_executed_alert_is_not_deduped_or_blocked_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.executed_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "ok")
        self.assertIn("Мягкий стоп активен: продажа 190.99", send.call_args_list[0].args[0])
        self.assertEqual(send.call_count, 2)

    def test_halt_alert_is_not_suppressed(self):
        run_all = {
            "status": "HALT",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "HALT",
                    "command": "arena-run",
                    "account_id": "DEMO-RU",
                    "symbol": "SBER@MISX",
                    "reason": "arena_stop_check_not_clear",
                    "safety": {"trading_mutations": False},
                }
            ],
            "safety": {"trading_mutations": False},
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=run_all
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            state = Path(tmp) / "alert.json"
            first = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )
            second = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=state,
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(first["telegram_delivery"], "ok")
        self.assertEqual(second["telegram_delivery"], "ok")
        self.assertEqual(send.call_count, 2)

    def test_stop_check_halt_alert_explains_existing_buy_and_soft_stop(self):
        run_all = {
            "status": "HALT",
            "command": "arena-run-all",
            "_execution_ledger_tail": [
                {
                    "account_id": "DEMO-US",
                    "symbol": "MSFT@XNGS",
                    "side": "BUY",
                    "quantity": "623.0",
                    "price": "385.51",
                    "order_id": "8dba65a5-e904-4c36-aa95-824e6308c7e9",
                    "timestamp": "2026-07-02T13:31:13+00:00",
                }
            ],
            "runs": [
                {
                    "status": "HALT",
                    "command": "arena-portfolio-run",
                    "reason": "arena_stop_check_not_clear",
                    "stop_check": {
                        "status": "BROKER_ERROR",
                        "command": "arena-check-stops",
                        "reason": "arena_session_create_failed",
                        "error": "Finam connection failed while calling /v1/sessions: _ssl.c:983: The handshake operation timed out",
                        "checks": [
                            {
                                "status": "ACTIVE",
                                "mode": "arena_soft_stop",
                                "account_id": "DEMO-US",
                                "symbol": "MSFT@XNGS",
                                "side": "SELL",
                                "quantity": "623.0",
                                "stop_price": "370.6171664285714285714285714",
                                "entry_order_id": "8dba65a5-e904-4c36-aa95-824e6308c7e9",
                            }
                        ],
                    },
                    "safety": {"trading_mutations": False},
                }
            ],
            "safety": {"trading_mutations": False},
        }

        text = arena_executor_notify.format_arena_executor_alert(run_all)

        self.assertIn("<b>Защитные стопы</b> · проверка", text)
        self.assertIn("Повторный BUY не отправлять", text)
        self.assertIn("_ssl.c:983", text)
        self.assertIn("Последний BUY: DEMO-US MSFT@XNGS, 623.0 @ 385.51", text)
        self.assertIn("order_id: 8dba65a5-e904-4c36-aa95-824e6308c7e9", text)
        self.assertIn("Активный soft-stop: DEMO-US MSFT@XNGS, продажа 623.0 @ 370.62", text)
        self.assertIn("entry_order_id: 8dba65a5-e904-4c36-aa95-824e6308c7e9", text)
        self.assertIn("повторить arena-check-stops", text)

    def test_replace_sell_done_buy_blocked_is_halt_alert(self):
        run_all = {
            "status": "REPLACE_SELL_DONE_BUY_BLOCKED",
            "command": "arena-run-all",
            "runs": [
                {
                    "status": "REPLACE_SELL_DONE_BUY_BLOCKED",
                    "command": "arena-portfolio-run",
                    "account_id": "DEMO-RU",
                    "action": "REPLACE",
                    "reason": "approval_price_drift_exceeded",
                    "replacement": {"sell_symbol": "SBER@MISX", "buy_symbol": "PLZL@MISX"},
                    "sell_result": {"status": "EXECUTED_ARENA_EXIT_CASH", "symbol": "SBER@MISX"},
                    "broker_mutation": True,
                    "halt_new_entries": True,
                    "safety": {"trading_mutations": True},
                }
            ],
            "safety": {"trading_mutations": True},
        }

        text = arena_executor_notify.format_arena_executor_alert(run_all)

        self.assertIn("ротация частично выполнена, покупка заблокирована", text)
        self.assertIn("approval_price_drift_exceeded", text)

    def test_executed_alert_reports_soft_stop(self):
        text = arena_executor_notify.format_arena_executor_alert(self.executed_run_all())

        self.assertIn("<b>DEMO-US</b> · AAPL@XNGS", text)
        self.assertIn("✅ Сделка исполнена; объём: 3", text)
        self.assertIn("🛡️ Мягкий стоп активен: продажа 190.99", text)

    def test_live_auto_buy_sends_pretrade_alert_before_execution_alert(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator,
            "_arena_run_all_output",
            side_effect=[self.auto_buy_preview_run_all(), self.executed_run_all()],
        ) as run_all, mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_market_session_block", return_value=None
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(output["pretrade_alert"]["telegram_delivery"], "ok")
        self.assertEqual(output["telegram_delivery"], "ok")
        self.assertEqual(send.call_count, 2)
        self.assertFalse(run_all.call_args_list[0].kwargs["live"])
        self.assertEqual(run_all.call_args_list[0].kwargs["research_mode"], "budgeted")
        self.assertTrue(run_all.call_args_list[1].kwargs["live"])
        self.assertEqual(run_all.call_args_list[1].kwargs["research_mode"], "budgeted")
        pretrade_text = send.call_args_list[0].args[0]
        executed_text = send.call_args_list[1].args[0]
        self.assertIn("<b>🏟️ Arena: план входа</b>", pretrade_text)
        self.assertIn("Сейчас будет live BUY", pretrade_text)
        self.assertIn("Скоринг: 86 / сильная", pretrade_text)
        self.assertIn("Pretrade: OK; внешний provider вызван", pretrade_text)
        self.assertIn("✅ Сделка исполнена", executed_text)

    def test_live_auto_buy_aborts_if_pretrade_alert_delivery_fails(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator,
            "_arena_run_all_output",
            side_effect=[self.auto_buy_preview_run_all(), AssertionError("live execution must not start")],
        ) as run_all, mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_market_session_block", return_value=None
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify,
            "send_telegram_message",
            side_effect=RuntimeError("telegram down"),
        ):
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(output["status"], "FAILED")
        self.assertEqual(output["reason"], "pretrade_alert_delivery_failed")
        self.assertEqual(run_all.call_count, 1)

    def test_planned_portfolio_actions_send_alert_when_live_gate_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.planned_portfolio_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(output["telegram_delivery"], "ok")
        text = send.call_args.args[0]
        self.assertIn("<b>План по портфелю</b>", text)
        self.assertIn("SBER@MISX — частично зафиксировать прибыль", text)
        self.assertIn("SBER@MISX — подтянуть стоп", text)
        self.assertIn("Автоторги выключены: рубильник автоторгов сейчас выключен", text)
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_live_gate_stop_check_uses_readonly_preview_for_operator_alert(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator,
            "_arena_run_all_output",
            side_effect=[self.planned_portfolio_run_all(), self.live_gate_before_preview_run_all(), self.planned_portfolio_run_all()],
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["telegram_delivery"], "ok")
        self.assertIn("alert_run_all", output)
        text = send.call_args.args[0]
        self.assertIn("SBER@MISX — частично зафиксировать прибыль", text)
        self.assertIn("Автоторги выключены: рубильник автоторгов сейчас выключен", text)

    def test_live_executor_uses_budgeted_research_before_readonly_gate_preview(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, 'load_arena_policy', return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator,
            '_arena_run_all_output',
            side_effect=[self.planned_portfolio_run_all(), self.live_gate_before_preview_run_all(), self.planned_portfolio_run_all()],
        ) as run_all, mock.patch.object(
            arena_executor_notify.h4_monitor_notify, 'send_telegram_message'
        ):
            arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / 'policy.json',
                alert_state_path=Path(tmp) / 'alert.json',
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(run_all.call_args_list[0].kwargs['research_mode'], 'budgeted')
        self.assertEqual(run_all.call_args_list[1].kwargs['research_mode'], 'budgeted')
        self.assertEqual(run_all.call_args_list[2].kwargs['research_mode'], 'cache_only')

    def test_reason_labels_translate_arena_gate_codes(self):
        self.assertEqual(
            arena_executor_notify._reason_label("symbol_exposure_limit_exceeded"),
            "лимит доли этой бумаги в счёте будет превышен",
        )
        self.assertEqual(
            arena_executor_notify._reason_label("same_symbol_position_open"),
            "по этой бумаге уже открыта позиция",
        )
        self.assertEqual(
            arena_executor_notify._reason_label("same_symbol_trade_today"),
            "по этой бумаге сегодня уже была сделка",
        )
        self.assertEqual(
            arena_executor_notify._top_signal_label("нет исполнимого сигнала: same_symbol_trade_today"),
            "нет исполнимого сигнала: по этой бумаге сегодня уже была сделка",
        )
        self.assertEqual(
            arena_executor_notify._reason_label("pretrade_budget_exhausted_blocks_autonomy"),
            "исчерпан бюджет предторговой проверки, автономный вход заблокирован",
        )
        self.assertEqual(
            arena_executor_notify._top_signal_label("нет исполнимого сигнала: pretrade_event_check_risk"),
            "нет исполнимого сигнала: предторговая проверка событий показала повышенный риск",
        )
        self.assertEqual(
            arena_executor_notify._reason_label("research_risk_requires_manual_review"),
            "исследовательский фильтр видит повышенный риск, нужен ручной разбор",
        )
        self.assertEqual(
            arena_executor_notify._reason_label("single_symbol_anchor_rotation_candidate"),
            "кандидат на ротацию якорной позиции; требуется отдельное решение",
        )
        self.assertEqual(
            arena_executor_notify._top_signal_label("нет исполнимого сигнала: cross_market_role_misx_concentration"),
            "нет исполнимого сигнала: счёт уже перегружен российскими позициями по своей роли",
        )

    def test_blocked_candidate_alert_explains_gate_reasons(self):
        text = arena_executor_notify.format_arena_executor_alert(self.blocked_run_all())

        self.assertIn("<b>Счета</b>", text)
        self.assertIn("<b>DEMO-RU</b> РФ", text)
        self.assertIn("📦 2", text)
        self.assertIn("🎯 нет активного сигнала", text)
        self.assertIn("<b>DEMO-US</b> США", text)
        self.assertIn("<b>DEMO-AI</b> AI", text)
        self.assertIn("<b>DEMO-AI</b> · LKOH@MISX", text)
        self.assertIn("Заявка не отправлена: исполнение заблокировано правилами риска", text)
        self.assertIn("счёт уже набрал лимит общей загрузки", text)
        self.assertIn("Оценка идеи: 79 / сильная", text)
        self.assertIn("H4 вверх, H1 вверх, M30 вниз", text)

    def test_blocked_pretrade_budget_alert_shows_budget_details(self):
        run_all = self.blocked_run_all()
        blocked = run_all["runs"][2]
        blocked["account_id"] = "DEMO-US"
        blocked["gate_reasons"] = ["pretrade_budget_exhausted_blocks_autonomy"]
        blocked["proposal"]["account"]["account_id"] = "DEMO-US"
        blocked["proposal"]["account"]["label"] = "США"
        blocked["proposal"]["account"]["top_signal"] = "нет исполнимого сигнала: pretrade_budget_exhausted_blocks_autonomy"
        blocked["proposal"]["candidate"]["symbol"] = "NVDA@XNGS"
        blocked["proposal"]["candidate"]["pretrade_check"] = {
            "status": "skipped",
            "reason": "openrouter_daily_budget_exhausted",
            "verdict": "UNAVAILABLE",
            "provider_call": False,
            "budget": {
                "period": "2026-06-16",
                "limit": 4,
                "used": 4,
                "remaining": 0,
                "spent_symbols": ["NVDA@XNGS", "META@XNGS"],
            },
        }

        text = arena_executor_notify.format_arena_executor_alert(run_all)

        self.assertIn("<b>DEMO-US</b> · NVDA@XNGS", text)
        self.assertIn("Pretrade budget: 4/4, осталось 0", text)
        self.assertIn("уже потрачено: NVDA@XNGS, META@XNGS", text)

    def test_blocked_candidate_alert_translates_raw_arena_reason_codes(self):
        run_all = self.blocked_run_all()
        blocked = run_all["runs"][2]
        blocked["gate_reasons"] = [
            "symbol_exposure_limit_exceeded",
            "same_symbol_position_open",
            "same_symbol_trade_today",
            "primary_daily_limit_reached",
            "single_symbol_anchor_rotation_candidate",
        ]
        blocked["proposal"]["account"]["top_signal"] = "нет исполнимого сигнала: single_symbol_anchor_rotation_candidate"

        text = arena_executor_notify.format_arena_executor_alert(run_all)

        self.assertIn("лимит доли этой бумаги в счёте будет превышен", text)
        self.assertIn("по этой бумаге уже открыта позиция", text)
        self.assertIn("по этой бумаге сегодня уже была сделка", text)
        self.assertIn("дневной лимит основных входов уже выбран", text)
        self.assertIn("кандидат на ротацию якорной позиции; требуется отдельное решение", text)
        self.assertNotIn("symbol_exposure_limit_exceeded", text)
        self.assertNotIn("same_symbol_position_open", text)
        self.assertNotIn("same_symbol_trade_today", text)
        self.assertNotIn("single_symbol_anchor_rotation_candidate", text)

    def test_portfolio_execution_alert_reports_exit_results(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator, "_arena_run_all_output", return_value=self.portfolio_executed_run_all()
        ), mock.patch.object(
            arena_executor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(output["telegram_delivery"], "ok")
        text = send.call_args.args[0]
        self.assertIn("<b>DEMO-US</b> · MSFT@XNGS", text)
        self.assertIn("Действие по портфелю: выйти из слабой позиции; объём: 540", text)
        self.assertIn("Заявка на выход: продажа MSFT@XNGS", text)


    def test_live_executor_requires_extra_mutation_gate_before_run_all(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_executor_notify, "load_arena_policy", return_value=self.sample_policy()
        ), mock.patch.object(
            arena_executor_notify.hermes_operator,
            "_arena_run_all_output",
            side_effect=AssertionError("missing executor mutation gate must block before arena-run-all"),
        ), mock.patch.dict(os.environ, {}, clear=True):
            output = arena_executor_notify.build_arena_executor_alert_output(
                policy_path=Path(tmp) / "policy.json",
                alert_state_path=Path(tmp) / "alert.json",
                live=True,
                execute_live=True,
                dry_run=False,
            )

        self.assertEqual(output["status"], "LIVE_GATE_REQUIRED")
        self.assertEqual(output["reason"], "FINAM_ARENA_EXECUTOR_MUTATIONS_ENABLED_not_true")
        self.assertFalse(output["safety"]["trading_mutations"])


if __name__ == "__main__":
    unittest.main()
