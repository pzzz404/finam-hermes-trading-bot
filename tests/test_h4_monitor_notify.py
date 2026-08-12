import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("h4_monitor_notify_script", ROOT / "scripts" / "h4_monitor_notify.py")
h4_monitor_notify = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(h4_monitor_notify)


class H4MonitorNotifyTests(unittest.TestCase):
    def sample_report(self):
        return {
            "status": "OK",
            "mode": "read_only",
            "time_utc": "2026-05-21T11:00:27+00:00",
            "time_msk": "2026-05-21 14:00",
            "account": {
                "account_id": "DEMO-ACCOUNT",
                "status": "ACCOUNT_ACTIVE",
                "readonly": False,
                "equity": "399268.73",
                "cash": "194514.93",
                "available_cash": "262926.35",
                "unrealized_pnl": "482.8",
            },
            "positions": [
                {
                    "symbol": "MOEX@MISX",
                    "quantity": "710.0",
                    "average_price": "172.7",
                    "current_price": "173.38",
                    "unrealized_pnl": "482.8",
                    "has_watching_sell_sltp": True,
                    "atr14": "1.51",
                    "calculated_stop": "169.68",
                    "tp_2r": "178.74",
                    "progress_r": "0.18",
                }
            ],
            "zero_positions": [
                {
                    "symbol": "GAZP@MISX",
                    "quantity": "0.0",
                    "current_price": "119.58",
                    "closed_note": "защитный SL ранее исполнен",
                }
            ],
            "orders": {
                "orders_count": 6,
                "watching_sell_sltp_details_by_symbol": {
                    "MOEX@MISX": [{"order_id": "14181", "side": "SIDE_SELL", "quantity": "710.0", "stop": "170.57"}]
                },
            },
            "watch": {
                "symbol": "PLZL@MISX",
                "quote": {"last": "2070.8"},
                "latest_h4": {
                    "time": "2026-05-21T05:00:00Z",
                    "open": "2073.8",
                    "high": "2077.8",
                    "low": "2046.2",
                    "close": "2052.4",
                },
                "forming_h4": {
                    "time": "2026-05-21T09:00:00Z",
                    "open": "2052.2",
                    "high": "2069.0",
                    "low": "2048.0",
                    "close": "2068.4",
                },
                "atr14": "17.9",
                "signal": {"status": "WAIT", "reason": "latest_h4_not_bullish"},
            },
            "errors": [],
        }

    def test_format_includes_delivery_and_schedule_context(self):
        text = h4_monitor_notify.format_telegram_report(self.sample_report())

        self.assertIn("Сводка счёта", text)
        self.assertIn("Finam H4 демо-монитор — 2026-05-21 14:00 МСК", text)
        self.assertIn("💼 Счёт: DEMO-ACCOUNT", text)
        self.assertIn("🛡️ Защитные стопы", text)
        self.assertIn("MOEX@MISX", text)
        self.assertIn("📦 Кол-во: 710 шт.", text)
        self.assertIn("🧾 Заявка: 14181", text)
        self.assertIn("🛑 <b>GAZP@MISX</b>: позиция 0 шт.; защитный SL ранее исполнен", text)
        self.assertIn("<b>MOEX@MISX</b>", text)
        self.assertIn("🛡️ Защита: 🟢 <b>ОК</b> — защитный стоп уже был, новый не дублировал", text)
        self.assertIn("📌 Статус: 🟡 <b>НАБЛЮДАТЬ</b>", text)
        self.assertIn("📌 Статус: 🔴 <b>СТОП</b>", text)
        self.assertIn("📏 ATR(14) H4: 1.51; прогресс: 0.18R", text)
        self.assertNotIn("host worker", text)
        self.assertNotIn("Runtime", text)
        self.assertNotIn("read-only", text)

    def test_format_arena_pulse_is_compact_and_lists_three_accounts(self):
        report = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "equity": "1000000",
                    "pnl_rub": "0",
                    "pnl_pct": "0",
                    "positions_count": 0,
                    "open_risk_pct": None,
                },
                {
                    "account_id": "DEMO-US",
                    "label": "США",
                    "equity": "1000000",
                    "pnl_rub": "0",
                    "pnl_pct": "0",
                    "positions_count": 0,
                    "open_risk_pct": None,
                },
                {
                    "account_id": "DEMO-AI",
                    "label": "AI",
                    "equity": "1000000",
                    "pnl_rub": "0",
                    "pnl_pct": "0",
                    "positions_count": 0,
                    "open_risk_pct": None,
                },
            ],
            "errors": [],
            "warnings": [],
        }

        text = h4_monitor_notify.format_arena_pulse(report)

        self.assertLess(len(text), 1800)
        self.assertIn("🏟️ Finam Arena Pulse", text)
        self.assertIn("💼 Стоимость портфеля", text)
        self.assertIn("📈 P&L", text)
        self.assertIn("⚠️ риск", text)
        self.assertIn("Безопасность", text)
        self.assertIn("DEMO-RU", text)
        self.assertIn("DEMO-US", text)
        self.assertIn("DEMO-AI", text)
        self.assertIn("Детали и управление: кнопки", text)
        self.assertNotIn("Итого equity", text)
        self.assertNotIn("Safety", text)
        h4_monitor_notify.validate_telegram_html(text)

    def test_format_arena_pulse_hides_expired_approvals(self):
        report = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "equity": "1000000",
                    "pnl_rub": "0",
                    "pnl_pct": "0",
                    "positions_count": 0,
                    "open_risk_pct": None,
                }
            ],
            "pending_approvals": [
                {
                    "status": "expired",
                    "account_id": "DEMO-RU",
                    "confirmation": "CONFIRM_ARENA_BUY LKOH@MISX DEMO-RU",
                    "expires_at": "2026-06-01T12:41:07+00:00",
                }
            ],
            "errors": [],
            "warnings": [],
        }

        text = h4_monitor_notify.format_arena_pulse(report)

        self.assertNotIn("approval expired", text)
        self.assertNotIn("CONFIRM_ARENA_BUY", text)
        h4_monitor_notify.validate_telegram_html(text)

    def test_arena_pulse_summarizes_market_data_warnings(self):
        report = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {
                    "account_id": "DEMO-US",
                    "label": "США",
                    "equity": "1000000",
                    "pnl_rub": "0",
                    "pnl_pct": "0",
                    "positions_count": 0,
                    "open_risk_pct": None,
                    "status": "WATCH",
                },
            ],
            "errors": [],
            "warnings": [
                "DEMO-US:AAPL@XNGS: market data unavailable: Finam HTTP 404 long diagnostic",
                "DEMO-US:MSFT@XNGS: market data unavailable: Finam HTTP 404 long diagnostic",
                "DEMO-US:NVDA@XNGS: market data unavailable: Finam HTTP 404 long diagnostic",
            ],
        }

        text = h4_monitor_notify.format_arena_pulse(report)

        self.assertIn("market data unavailable for 3 symbols", text)
        self.assertIn("НАБЛЮДАТЬ", text)
        self.assertNotIn("long diagnostic", text)
        h4_monitor_notify.validate_telegram_html(text)

    def test_arena_reply_markup_contains_navigation_callbacks(self):
        markup = h4_monitor_notify.arena_reply_markup()

        callbacks = [
            button["callback_data"]
            for row in markup["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            callbacks,
            [
                "arena:overview",
                "arena:account:DEMO-RU",
                "arena:account:DEMO-US",
                "arena:account:DEMO-AI",
                "arena:risks",
                "arena:strategy",
                "arena:rotation",
                "arena:attribution",
            ],
        )

    def test_arena_account_detail_and_risk_views_are_telegram_html(self):
        report = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "strategy": "russian_equities_h1_h4",
                    "equity": "1001000",
                    "cash": "800000",
                    "pnl_rub": "1000",
                    "pnl_pct": "0.10",
                    "positions": [{"symbol": "SBER@MISX", "quantity": "10", "price": "300"}],
                    "active_stop_orders": [{"symbol": "SBER@MISX", "side": "SELL", "stop_price": "290"}],
                    "recent_trades": [{"symbol": "SBER@MISX", "side": "BUY", "quantity": "10"}],
                    "open_risk_pct": "0.5",
                }
            ],
            "candidates": [{"account_id": "DEMO-RU", "symbol": "SBER@MISX", "side": "BUY", "entry_price": "300"}],
            "pending_approvals": [
                {
                    "status": "pending",
                    "account_id": "DEMO-RU",
                    "confirmation": "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
                    "expires_at": "2026-06-16T12:00:00+00:00",
                }
            ],
            "errors": [],
            "warnings": [],
        }

        detail = h4_monitor_notify.format_arena_account_detail(report, "DEMO-RU")
        risks = h4_monitor_notify.format_arena_risks_view(report)

        self.assertIn("DEMO-RU", detail)
        self.assertIn("💼 Портфель", detail)
        self.assertIn("⚠️ Открытый риск", detail)
        self.assertIn("🧠 Стратегия", detail)
        self.assertIn("Ожидает подтверждения", detail)
        self.assertIn("CONFIRM_ARENA_REPLACE SBER@MISX -&gt; PLZL@MISX DEMO-RU", detail)
        self.assertIn("python scripts/hermes_operator.py arena-confirm --live --confirmation", detail)
        self.assertIn("Стоп-защита", detail)
        self.assertIn("SBER: LONG 10 шт.; вход 300.00", detail)
        self.assertIn("SBER: стоп — продать н/д шт. по 290.00", detail)
        self.assertIn("Куплено SBER: 10 шт.", detail)
        self.assertIn("P&L: н/д", detail)
        self.assertIn("SBER: кандидат — купить н/д шт. по 300.00", detail)
        self.assertNotIn("SBER@MISX BUY x10", detail)
        self.assertNotIn("@ 300", detail)
        self.assertNotIn("SIDE_SELL", detail)
        self.assertIn("Arena Risks", risks)
        h4_monitor_notify.validate_telegram_html(detail)
        h4_monitor_notify.validate_telegram_html(risks)


    def test_arena_account_detail_shows_enriched_trade_amount_and_estimated_pnl(self):
        report = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-US",
                    "label": "США",
                    "strategy": "us_equities_h1_h4",
                    "equity": "982713.88",
                    "cash": "982713.88",
                    "pnl_rub": "-17286.12",
                    "pnl_pct": "-1.73",
                    "positions": [],
                    "recent_trades": [
                        {
                            "symbol": "AVGO@XNGS",
                            "side": "SELL",
                            "quantity": "453.0",
                            "price": "409.24",
                            "notional": "185385.720",
                            "realized_pnl_estimate": "-488.66572",
                        }
                    ],
                }
            ],
            "candidates": [],
            "errors": [],
            "warnings": [],
        }

        detail = h4_monitor_notify.format_arena_account_detail(report, "DEMO-US")

        self.assertIn("Продано AVGO: 453 шт. по 409.24", detail)
        self.assertIn("сумма ≈ 185 385.72 RUB", detail)
        self.assertIn("P&L ≈ -488.67 RUB", detail)
        self.assertNotIn("н/д шт.", detail)
        self.assertNotIn("AVGO@XNGS SELL", detail)
        h4_monitor_notify.validate_telegram_html(detail)

    def test_arena_strategy_view_lists_three_account_universes(self):
        policy = {
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "label": "РФ", "strategy": "russian_equities_h1_h4", "universe": ["SBER@MISX"], "trade_mode": "manual"},
                {"account_id": "DEMO-US", "label": "США", "strategy": "us_equities_h1_h4", "universe": ["AAPL@XNGS"], "trade_mode": "auto"},
                {"account_id": "DEMO-AI", "label": "AI", "strategy": "experimental_ai_cross_market", "universe": ["NVDA@XNGS"], "paused": True},
            ],
            "risk": {
                "risk_per_trade_pct": "1",
                "max_daily_loss_pct": "3",
                "max_account_drawdown_pct": "8",
            },
        }

        text = h4_monitor_notify.format_arena_strategy_view(policy)

        self.assertIn("Arena Strategy", text)
        self.assertIn("DEMO-RU", text)
        self.assertIn("DEMO-US", text)
        self.assertIn("DEMO-AI", text)
        self.assertIn("proposal + confirm", text)
        h4_monitor_notify.validate_telegram_html(text)

        markup = h4_monitor_notify.arena_strategy_reply_markup(policy)
        callbacks = [
            button["callback_data"]
            for row in markup["inline_keyboard"]
            for button in row
        ]
        self.assertIn("arena:st:DEMO-RU:pause", callbacks)
        self.assertIn("arena:st:DEMO-RU:auto", callbacks)
        self.assertIn("arena:st:DEMO-US:manual", callbacks)
        self.assertIn("arena:st:DEMO-AI:resume", callbacks)
        self.assertIn("arena:st:DEMO-RU:risk_up", callbacks)
        self.assertIn("arena:st:DEMO-RU:risk_down", callbacks)

    def test_format_arena_strategy_proposal_is_telegram_html(self):
        proposal = {
            "status": "OK",
            "account_id": "DEMO-RU",
            "changes": [
                {"path": "accounts.DEMO-RU.trade_mode", "old": "manual", "new": "auto"},
                {"path": "accounts.DEMO-RU.universe", "action": "add", "symbol": "GAZP@MISX"},
            ],
            "validation": {"errors": [], "warnings": ["risk warning"]},
        }

        text = h4_monitor_notify.format_arena_strategy_proposal(proposal)

        self.assertIn("Arena Strategy Proposal", text)
        self.assertIn("manual", text)
        self.assertIn("auto", text)
        self.assertIn("+GAZP@MISX", text)
        self.assertIn("отдельный confirm", text)
        h4_monitor_notify.validate_telegram_html(text)

    def test_format_distinguishes_blocked_candidates_from_confirmation_candidates(self):
        report = self.sample_report()
        report["candidates"] = [
            {
                "symbol": "PLZL@MISX",
                "status": "BLOCKED",
                "requires_confirmation": True,
                "quantity": "1",
                "current_price": "2080.8",
                "notional": "2080.8",
                "stop": "2045.0",
                "tp_2r": "2152.4",
                "nearest_target": "2090.0",
                "nearest_target_r": "0.26",
                "risk_rub": "35.8",
                "gate_reasons": [
                    "target_r_below_min_1.5",
                    "codex_review_avoid",
                    "single_symbol_anchor_rotation_candidate",
                    "cross_market_role_misx_concentration",
                ],
                "growth_score": {"score": 42, "label": "LOW"},
            }
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("📌 Статус: 🔴 <b>БЛОК</b> — заблокировано правилом; не покупать", text)
        self.assertIn("🚀 Оценка роста: 42/100 НИЗКАЯ", text)
        self.assertIn(
            "⚠️ Ограничения: R/R ниже минимума 1.5R; новостная проверка дала AVOID/RISK; "
            "кандидат на ротацию якорной позиции; требуется отдельное решение; "
            "счёт уже перегружен российскими позициями по своей роли",
            text,
        )
        self.assertNotIn("single_symbol_anchor_rotation_candidate", text)
        self.assertNotIn("cross_market_role_misx_concentration", text)
        self.assertIn("⏳ Ничего не делать: кандидаты на покупку заблокированы правилами", text)
        self.assertIn("✅ Рекомендация: держать; заблокированные кандидаты не покупать", text)
        self.assertNotIn("🟡 <b>WAIT</b> — требуется подтверждение перед покупкой", text)

    def test_format_arena_portfolio_review_lists_exit_confirmations(self):
        text = h4_monitor_notify.format_arena_portfolio_review(
            {
                "status": "OK",
                "accounts": [
                    {
                        "account_id": "DEMO-US",
                        "cash": "900000",
                        "gross_exposure_pct": "0",
                        "entry_limits": {"primary_used": 0, "primary_limit": 2, "replacement_used": 0, "replacement_limit": 1},
                    }
                ],
                "replacement_proposals": [
                    {
                        "account_id": "DEMO-US",
                        "sell_symbol": "MSFT@XNGS",
                        "buy_symbol": "NVDA@XNGS",
                        "confirmation_phrase": "CONFIRM_ARENA_REPLACE MSFT@XNGS -> NVDA@XNGS DEMO-US",
                    }
                ],
                "exit_proposals": [
                    {
                        "account_id": "DEMO-US",
                        "symbol": "MSFT@XNGS",
                        "action": "EXIT_WEAK",
                        "progress_r": "-0.2",
                        "reason": "holding_score_below_weak_exit_threshold",
                        "confirmation_phrase": "CONFIRM_ARENA_EXIT MSFT@XNGS DEMO-US",
                    }
                ],
                "cost_model": {},
            }
        )

        self.assertIn("CONFIRM_ARENA_REPLACE MSFT@XNGS -&gt; NVDA@XNGS DEMO-US", text)
        self.assertIn("CONFIRM_ARENA_EXIT MSFT@XNGS DEMO-US", text)

    def test_format_escapes_recommendation_comparator_for_telegram_html(self):
        report = self.sample_report()
        report["positions"][0]["status"] = "WATCH"

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("прогрессом &lt; 0.25R", text)
        self.assertNotIn("прогрессом < 0.25R", text)

    def test_format_includes_growth_goal_report_only_block(self):
        report = self.sample_report()
        report["growth"] = {
            "enabled": False,
            "report_only": True,
            "target_annual_return": "0.5",
            "stretch_annual_return": "1.0",
            "target_required_monthly_pct": "3.44",
            "target_required_weekly_pct": "0.78",
            "target_required_trading_day_pct": "0.16",
            "stretch_required_monthly_pct": "5.95",
            "stretch_required_weekly_pct": "1.34",
            "stretch_required_trading_day_pct": "0.28",
            "current_equity": "392210.37",
            "tracking_status": "report_only",
        }

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("🎯 Ориентир роста портфеля", text)
        self.assertIn("📈 50% годовых: +3.44%/мес, +0.78%/нед, +0.16%/торг.день", text)
        self.assertIn("🚀 100% годовых: +5.95%/мес, +1.34%/нед, +0.28%/торг.день", text)
        self.assertIn("💼 Стоимость портфеля для трекинга: 392 210.37 RUB", text)
        self.assertIn("🧪 Режим: только отчёт, без автоторговли", text)

    def test_format_includes_growth_dashboard_v2_metrics(self):
        report = self.sample_report()
        report["growth"] = {
            "enabled": False,
            "report_only": True,
            "target_required_monthly_pct": "3.44",
            "target_required_weekly_pct": "0.78",
            "target_required_trading_day_pct": "0.16",
            "stretch_required_monthly_pct": "5.95",
            "stretch_required_weekly_pct": "1.34",
            "stretch_required_trading_day_pct": "0.28",
            "current_equity": "396000",
            "mtd_return_pct": "1.54",
            "ytd_return_pct": "10.00",
            "accumulated_r": "1.25",
            "target_gap_pct": "0.63",
            "target_gap_rub": "2279.14",
            "goal_status": "BEHIND",
        }

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("📊 Месяц/год: +1.54% / +10.00%", text)
        self.assertIn("📐 Накоплено R: +1.25R", text)
        self.assertIn("🧭 Статус к цели: 🟠 <b>ОТСТАЁМ</b>", text)
        self.assertIn("📉 Разрыв до 50% цели: 0.63% / 2 279.14 RUB", text)

    def test_format_labels_open_r_when_accumulated_r_is_absent(self):
        report = self.sample_report()
        report["growth"] = {
            "enabled": False,
            "report_only": True,
            "target_required_monthly_pct": "3.44",
            "target_required_weekly_pct": "0.78",
            "target_required_trading_day_pct": "0.16",
            "stretch_required_monthly_pct": "5.95",
            "stretch_required_weekly_pct": "1.34",
            "stretch_required_trading_day_pct": "0.28",
            "current_equity": "396000",
            "open_position_r": "0.60",
            "goal_status": "REPORT_ONLY",
        }

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("📐 Открытый R: +0.60R", text)
        self.assertNotIn("📐 Накоплено R: +0.60R", text)

    def test_format_includes_report_only_growth_diagnostics(self):
        report = self.sample_report()
        report["account"]["equity"] = "392581.62"
        report["account"]["cash"] = "268118.62"
        report["account"]["open_risk_pct"] = "1.0"
        report["account"]["max_total_open_risk_pct"] = "3.0"
        report["policy"] = {"risk": {"max_open_positions": 5}}
        report["growth"] = {
            "enabled": False,
            "report_only": True,
            "target_required_monthly_pct": "3.44",
            "target_required_weekly_pct": "0.78",
            "target_required_trading_day_pct": "0.16",
            "stretch_required_monthly_pct": "5.95",
            "stretch_required_weekly_pct": "1.34",
            "stretch_required_trading_day_pct": "0.28",
            "current_equity": "392581.62",
            "open_position_r": "0.74",
            "goal_status": "REPORT_ONLY",
        }
        report["candidates"] = [
            {
                "symbol": "AFKS@MISX",
                "status": "BLOCKED",
                "quantity": "1",
                "current_price": "11.51",
                "notional": "11.51",
                "stop": "10.78",
                "tp_2r": "12.95",
                "nearest_target": "12.06",
                "nearest_target_r": "0.77",
                "risk_rub": "0.73",
                "gate_reasons": ["target_r_below_min_1.5"],
            },
            {
                "symbol": "CBOM@MISX",
                "status": "BLOCKED",
                "quantity": "1",
                "current_price": "7.42",
                "notional": "7.42",
                "stop": "6.79",
                "tp_2r": "8.67",
                "nearest_target": "7.78",
                "nearest_target_r": "0.58",
                "risk_rub": "0.63",
                "gate_reasons": ["target_r_below_min_1.5"],
            },
        ]
        report["dynamic_watchlist"] = [
            {
                "symbol": "SFIN@MISX",
                "status": "DISCOVERED",
                "discovery_score": 92,
                "turnover_rub": "600812801.00",
                "trades_today": 26976,
                "reject_reasons": [],
            }
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("📉 Почему портфель не растёт", text)
        self.assertIn("📉 Основной тормоз: мало чистых входов", text)
        self.assertIn("⛔ 2/2 кандидатов заблокированы; R/R ниже 1.5R: 2", text)
        self.assertIn("💤 Свободные деньги: 268 118.62 RUB — капитал в основном не работает", text)
        self.assertIn("📦 Позиций: 1 из 5 лимита", text)
        self.assertIn("🧭 Динамические кандидаты для ручного разбора: SFIN@MISX", text)
        self.assertIn("🧪 Статус: только отчёт; торговые решения не меняет", text)

    def test_format_includes_growth_score_breakdown_and_entry_blockers(self):
        report = self.sample_report()
        report["candidates"] = [
            {
                "symbol": "SBER@MISX",
                "status": "BLOCKED",
                "quantity": "10",
                "current_price": "320",
                "notional": "3200",
                "stop": "314",
                "tp_2r": "332",
                "nearest_target": "327",
                "nearest_target_r": "1.2",
                "risk_rub": "60",
                "growth_score": {
                    "score": 12,
                    "label": "LOW",
                    "components": {
                        "h4_momentum": 25,
                        "risk_reward": 15,
                        "relative_strength": 12,
                        "liquidity": 10,
                        "atr": 10,
                        "research": -10,
                        "research_context": 0,
                        "gate_penalty": -45,
                    },
                },
                "entry_blockers": ["R/R ниже минимума 1.5R", "research дал AVOID/RISK по новости"],
            }
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("🚀 Оценка роста: 12/100 НИЗКАЯ", text)
        self.assertIn("📊 Разбор оценки: H4-импульс 25/25; риск/цель 15/25; сила к рынку 12/15; ликвидность 10/10; ATR 10/10; новости -10; контекст 0; штрафы -45", text)
        self.assertIn("⛔ Что мешает входу:", text)
        self.assertIn("- R/R ниже минимума 1.5R", text)
        self.assertIn("- research дал AVOID/RISK по новости", text)

    def test_format_uses_russian_labels_and_compact_numbers_for_candidates_and_dynamic_watchlist(self):
        report = self.sample_report()
        report["candidates"] = [
            {
                "symbol": "MOEX@MISX",
                "status": "BLOCKED",
                "candidate_source": "held_scale_in",
                "discovery_score": None,
                "quantity": "1030.0",
                "current_price": "175.44",
                "notional": "180703.2",
                "stop": "171.64",
                "tp_2r": "183.05",
                "nearest_target": "175.30",
                "nearest_target_r": "0.01537870049980776624375240292",
                "risk_rub": "3923.196428571428571428571429",
                "gate_reasons": [
                    "target_r_below_min_1.5",
                    "scale_in_live_execution_disabled",
                    "scale_in_progress_below_1r",
                ],
                "growth_score": {
                    "score": 0,
                    "label": "LOW",
                    "components": {
                        "h4_momentum": 0,
                        "risk_reward": 0,
                        "relative_strength": 0,
                        "liquidity": 10,
                        "atr": 10,
                        "research": -10,
                        "research_context": 0,
                        "gate_penalty": -45,
                    },
                },
                "entry_blockers": ["R/R ниже минимума 1.5R"],
            }
        ]
        report["dynamic_watchlist"] = [
            {
                "symbol": "LSRG@MISX",
                "status": "REJECTED",
                "discovery_score": 100,
                "turnover_rub": "263033040.00",
                "trades_today": 21809,
                "reject_reasons": ["target_r_below_min_1.5"],
            },
            {
                "symbol": "VKCO@MISX",
                "status": "DISCOVERED",
                "discovery_score": 90,
                "turnover_rub": "862201666.00",
                "trades_today": 26876,
                "reject_reasons": [],
            },
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("🚀 Ближайшая цель: 175.30; запас до цели: 0.02R", text)
        self.assertIn("⚠️ Ограничения: R/R ниже минимума 1.5R; live-докупка отключена; докупка заблокирована: позиция ещё не дошла до +1R", text)
        self.assertIn("🧭 Источник: докупка текущей позиции; рейтинг поиска: н/д", text)
        self.assertIn("🚀 Оценка роста: 0/100 НИЗКАЯ", text)
        self.assertIn("📊 Разбор оценки: H4-импульс 0/25; риск/цель 0/25; сила к рынку 0/15; ликвидность 10/10; ATR 10/10; новости -10; контекст 0; штрафы -45", text)
        self.assertIn("Динамический список наблюдения", text)
        self.assertIn("📊 Рейтинг: 100/100", text)
        self.assertIn("💰 Оборот: 263 033 040.00 RUB", text)
        self.assertIn("🔁 Сделок: 21 809", text)
        self.assertIn("⛔ Причина: R/R ниже минимума 1.5R", text)
        self.assertNotIn("Gate:", text)
        self.assertNotIn("Source:", text)
        self.assertNotIn("Breakdown:", text)
        self.assertNotIn("Dynamic watchlist", text)
        self.assertNotIn("target_r_below_min_1.5", text)
        self.assertNotIn("held_scale_in", text)
        self.assertNotIn("0.015378700499", text)

    def test_format_bolds_candidate_and_dynamic_ticker_headings(self):
        report = self.sample_report()
        report["candidates"] = [
            {
                "symbol": "SBER@MISX",
                "status": "BLOCKED",
                "quantity": "1",
                "current_price": "323.49",
                "notional": "323.49",
                "stop": "320.60",
                "tp_2r": "329.27",
                "nearest_target": "323.70",
                "nearest_target_r": "0.07",
                "risk_rub": "2.89",
                "gate_reasons": ["target_r_below_min_1.5"],
            }
        ]
        report["dynamic_watchlist"] = [
            {
                "symbol": "RUAL@MISX",
                "status": "DISCOVERED",
                "discovery_score": 66,
                "turnover_rub": "80122.35",
                "trades_today": 2310,
                "reject_reasons": [],
            }
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("<b>SBER@MISX</b>", text)
        self.assertIn("<b>RUAL@MISX</b>", text)
        self.assertNotIn("\nSBER@MISX\n", text)
        self.assertNotIn("\nRUAL@MISX\n", text)

    def test_format_dynamic_watchlist_shows_rr_and_sorts_by_rr(self):
        report = self.sample_report()
        report["dynamic_watchlist"] = [
            {
                "symbol": "LOWRR@MISX",
                "status": "DISCOVERED",
                "discovery_score": 100,
                "turnover_rub": "100000000",
                "trades_today": 1000,
                "nearest_target": "102",
                "nearest_target_r": "1",
            },
            {
                "symbol": "HIGHRR@MISX",
                "status": "DISCOVERED",
                "discovery_score": 50,
                "turnover_rub": "90000000",
                "trades_today": 900,
                "nearest_target": "106",
                "nearest_target_r": "3",
                "reject_reasons": ["low_turnover"],
            },
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertLess(text.index("<b>HIGHRR@MISX</b>"), text.index("<b>LOWRR@MISX</b>"))
        self.assertIn("🚀 Ближайшая цель: 106.00; запас до цели: 3.00R", text)
        self.assertIn("🚀 Ближайшая цель: 102.00; запас до цели: 1.00R", text)
        self.assertIn("⛔ Причина: оборот ниже минимума", text)

    def test_format_shows_report_only_rr_tier_and_entry_price_for_min_r(self):
        report = self.sample_report()
        report["dynamic_watchlist"] = [
            {
                "symbol": "BMODE@MISX",
                "status": "DISCOVERED",
                "discovery_score": 75,
                "turnover_rub": "100000000",
                "trades_today": 1000,
                "nearest_target": "102.4",
                "nearest_target_r": "1.2",
                "entry_price_for_min_r": "99.4",
                "required_pullback_pct": "0.6",
                "rr_strategy": {
                    "tier": "B",
                    "risk_multiplier": "0.5",
                    "manual_confirmation_required": True,
                    "report_only": True,
                },
            }
        ]
        report["candidates"] = [
            {
                "symbol": "CMODE@MISX",
                "status": "BLOCKED",
                "quantity": "1",
                "current_price": "100",
                "notional": "100",
                "stop": "98",
                "tp_2r": "104",
                "nearest_target": "101.8",
                "nearest_target_r": "0.9",
                "entry_price_for_min_r": "98.8",
                "required_pullback_pct": "1.2",
                "risk_rub": "2",
                "gate_reasons": ["target_r_below_min_1.5"],
                "rr_strategy": {
                    "tier": "C",
                    "risk_multiplier": "0.25",
                    "manual_confirmation_required": True,
                    "report_only": True,
                },
            }
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("🧭 RR-режим: C — 0.25 базового риска; только вручную/отчёт", text)
        self.assertIn("🎯 Цена для 1.5R: 98.80; нужен откат: 1.20%", text)
        self.assertIn("🧭 RR-режим: B — 0.50 базового риска; только вручную/отчёт", text)
        self.assertIn("🎯 Цена для 1.5R: 99.40; нужен откат: 0.60%", text)

    def test_format_includes_report_only_advisory_actions_for_positions(self):
        report = self.sample_report()
        report["positions"][0]["advisory_action"] = {
            "action": "BREAKEVEN_CANDIDATE",
            "report_only": True,
            "reason": "+1R достигнут; можно предложить breakeven/trailing, но стоп не двигать без подтверждения",
        }

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("🛡️ Действие: 🟡 <b>Б/У КАНДИДАТ</b>", text)
        self.assertIn("⚠️ Комментарий: +1R достигнут; можно предложить breakeven/trailing, но стоп не двигать без подтверждения", text)

    def test_format_includes_short_side_marking_without_orders(self):
        report = self.sample_report()
        report["short_candidates"] = [
            {
                "symbol": "GAZP@MISX",
                "short_signal": {"status": "WATCH", "reason": "h4_bearish_breakdown_candidate", "orders_allowed": False},
            }
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("📉 Анализ шорт-сценариев", text)
        self.assertIn("<b>GAZP@MISX</b>", text)
        self.assertIn("📉 Кандидат в шорт: да", text)
        self.assertIn("⚠️ Шорт-заявка: запрещена — доступность шорта не подтверждена", text)

    def test_format_sorts_candidate_display_by_growth_score_only(self):
        report = self.sample_report()
        report["candidates"] = [
            {
                "symbol": "LOW@MISX",
                "status": "PROPOSE_ONLY",
                "quantity": "1",
                "current_price": "100",
                "notional": "100",
                "stop": "95",
                "tp_2r": "110",
                "nearest_target": "104",
                "nearest_target_r": "0.8",
                "risk_rub": "5",
                "growth_score": {"score": 20, "label": "LOW"},
            },
            {
                "symbol": "HIGH@MISX",
                "status": "PROPOSE_ONLY",
                "quantity": "1",
                "current_price": "100",
                "notional": "100",
                "stop": "95",
                "tp_2r": "110",
                "nearest_target": "112",
                "nearest_target_r": "2.4",
                "risk_rub": "5",
                "growth_score": {"score": 82, "label": "HIGH"},
            },
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertLess(text.index("<b>HIGH@MISX</b>"), text.index("<b>LOW@MISX</b>"))
        self.assertEqual([item["symbol"] for item in report["candidates"]], ["LOW@MISX", "HIGH@MISX"])

    def test_format_uses_policy_stop_atr_multiplier_label(self):
        report = self.sample_report()
        report["policy"] = {"risk": {"stop_atr_multiplier": 2.7}}

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("🛡️ Стоп 2.7ATR: 169.68", text)
        self.assertIn("Расчётный стоп = вход − 2.7ATR", text)
        self.assertNotIn("Стоп 2ATR", text)

    def test_format_collapses_finam_contract_timeout_into_clean_warning(self):
        report = self.sample_report()
        report["warnings"] = [
            "MTSS@MISX: Finam instrument contract недоступен: The read operation timed out",
            "MTSS@MISX: MOEX lot size вернул неожиданный формат",
        ]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("⚠️ MTSS@MISX: контракт Finam не подтверждён, покупка заблокирована до следующей проверки", text)
        self.assertNotIn("The read operation timed out", text)
        self.assertNotIn("MOEX lot size вернул неожиданный формат", text)
        self.assertIn("⏳ Ничего не делать: есть пропуски данных, новые сделки запрещены", text)

    def test_format_sanitizes_provider_nonetype_errors(self):
        report = self.sample_report()
        report["errors"] = ["openai-codex TypeError: 'NoneType' object is not iterable"]

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("Сбой provider/client до получения ответа; торговый сигнал не сформирован.", text)
        self.assertNotIn("NoneType", text)
        self.assertNotIn("not iterable", text)

    def test_format_keeps_full_research_and_splitter_chunks_for_telegram(self):
        report = self.sample_report()
        report["candidates"] = [
            {
                "symbol": "SBER@MISX",
                "status": "BLOCKED",
                "quantity": "1",
                "current_price": "323.49",
                "notional": "323.49",
                "stop": "320.60",
                "tp_2r": "329.27",
                "nearest_target": "323.70",
                "nearest_target_r": "0.07",
                "risk_rub": "2.89",
                "gate_reasons": ["target_r_below_min_1.5"],
            }
        ]
        report["research"] = {
            "status": "ok",
            "verdict": "RISK",
            "summary": "SBER@MISX — RISK\n" + ("важная research строка " * 300),
        }

        text = h4_monitor_notify.format_telegram_report(report)
        chunks = h4_monitor_notify.split_telegram_messages(text, limit=1000)

        self.assertIn("🧠 Вердикт: 🟠 <b>РИСК</b>", text)
        self.assertIn("важная research строка", text)
        self.assertNotIn("...обрезано", text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_format_renders_structured_research_without_raw_json(self):
        report = self.sample_report()
        report["candidates"] = [
            {
                "symbol": "CHMF@MISX",
                "status": "BLOCKED",
                "quantity": "1",
                "current_price": "704.40",
                "notional": "704.40",
                "stop": "679.20",
                "tp_2r": "755.00",
                "nearest_target": "706.00",
                "nearest_target_r": "0.06",
                "risk_rub": "25.20",
                "gate_reasons": ["target_r_below_min_1.5"],
            }
        ]
        report["research"] = {
            "status": "ok",
            "verdict": "RISK",
            "summary": json.dumps(
                {
                    "items": [
                        {
                            "symbol": "CHMF@MISX",
                            "verdict": "RISK",
                            "reason": "Высокий риск санкций/ограничений для металлургов",
                            "source_hint": "MOEX профиль эмитента CHMF",
                        },
                        {
                            "symbol": "SBER@MISX",
                            "verdict": "OK",
                            "reason": "Новых негативных событий не выявлено",
                            "source_hint": "MOEX профиль SBER",
                        },
                    ],
                    "overall_verdict": "RISK",
                },
                ensure_ascii=False,
            ),
        }

        text = h4_monitor_notify.format_telegram_report(report)

        self.assertIn("🧠 Вердикт: 🟠 <b>РИСК</b>", text)
        self.assertIn("• <b>CHMF@MISX</b>: 🟠 <b>РИСК</b> — Высокий риск санкций/ограничений для металлургов", text)
        self.assertIn("• <b>SBER@MISX</b>: 🟢 <b>ОК</b> — Новых негативных событий не выявлено", text)
        self.assertNotIn("• CHMF@MISX: 🟠", text)
        self.assertNotIn("• SBER@MISX: 🟢", text)
        self.assertNotIn('{"items"', text)
        self.assertNotIn("overall_verdict", text)

    def test_send_telegram_uses_home_channel_without_printing_token(self):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        response.read.return_value = b'{"ok": true}'

        with mock.patch.dict(
            h4_monitor_notify.os.environ,
            {"TELEGRAM_BOT_TOKEN": "secret-token", "TELEGRAM_HOME_CHANNEL": "-100123"},
            clear=True,
        ), mock.patch.object(h4_monitor_notify.urllib.request, "urlopen", return_value=response) as urlopen:
            h4_monitor_notify.send_telegram_message("hello")

        request = urlopen.call_args.args[0]
        self.assertIn("botsecret-token/sendMessage", request.full_url)
        self.assertIn(b"chat_id=-100123", request.data)
        self.assertIn(b"text=hello", request.data)
        self.assertIn(b"parse_mode=HTML", request.data)

    def test_send_telegram_splits_long_messages(self):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        response.read.return_value = b'{"ok": true}'
        long_text = "\n".join(f"line {index} " + ("x" * 80) for index in range(120))

        with mock.patch.dict(
            h4_monitor_notify.os.environ,
            {"TELEGRAM_BOT_TOKEN": "secret-token", "TELEGRAM_HOME_CHANNEL": "-100123"},
            clear=True,
        ), mock.patch.object(h4_monitor_notify.urllib.request, "urlopen", return_value=response) as urlopen:
            h4_monitor_notify.send_telegram_message(long_text)

        self.assertGreater(urlopen.call_count, 1)
        for call in urlopen.call_args_list:
            request = call.args[0]
            self.assertIn(b"parse_mode=HTML", request.data)
            parsed = h4_monitor_notify.urllib.parse.parse_qs(request.data.decode("utf-8"))
            self.assertLessEqual(len(parsed["text"][0]), h4_monitor_notify.TELEGRAM_SAFE_MESSAGE)

    def test_send_telegram_accepts_inline_reply_markup(self):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        response.read.return_value = b'{"ok": true}'

        with mock.patch.dict(
            h4_monitor_notify.os.environ,
            {"TELEGRAM_BOT_TOKEN": "secret-token", "TELEGRAM_HOME_CHANNEL": "-100123"},
            clear=True,
        ), mock.patch.object(h4_monitor_notify.urllib.request, "urlopen", return_value=response) as urlopen:
            h4_monitor_notify.send_telegram_message("hello", reply_markup=h4_monitor_notify.arena_reply_markup())

        request = urlopen.call_args.args[0]
        parsed = h4_monitor_notify.urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertIn("reply_markup", parsed)
        self.assertIn("arena:overview", parsed["reply_markup"][0])

    def test_outbox_send_uses_saved_payload_without_rebuilding_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(h4_monitor_notify.h4_monitor, "build_report", return_value=self.sample_report()):
                item = h4_monitor_notify.build_outbox_report(outbox_root=Path(tmp), include_research=False)

            with mock.patch.object(h4_monitor_notify.h4_monitor, "build_report", side_effect=AssertionError("must not rebuild")), mock.patch.object(
                h4_monitor_notify, "send_telegram_message"
            ) as send:
                with redirect_stdout(io.StringIO()):
                    result = h4_monitor_notify.send_outbox_report(item["run_id"], outbox_root=Path(tmp))

            self.assertEqual(result, 0)
            self.assertIn("Finam H4 демо-монитор", send.call_args.args[0])
            delivery = json.loads((Path(tmp) / item["run_id"] / "delivery.json").read_text(encoding="utf-8"))
            self.assertEqual(delivery["status"], "ok")

    def test_latest_sendable_outbox_run_ignores_dry_run_and_sent_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for run_id, status in (("sent", "ok"), ("dry", "dry_run"), ("pending", "pending")):
                run = root / run_id
                run.mkdir()
                (run / "delivery.json").write_text(
                    json.dumps({"status": status, "sendable": run_id == "pending", "created_at": "2026-05-22T15:00:00+00:00"}),
                    encoding="utf-8",
                )

            latest = h4_monitor_notify.latest_sendable_outbox_run(
                root,
                now=h4_monitor_notify.datetime(2026, 5, 22, 15, 10, tzinfo=h4_monitor_notify.timezone.utc),
            )

            self.assertEqual(latest, "pending")

    def test_validate_telegram_html_rejects_raw_less_than(self):
        with self.assertRaisesRegex(RuntimeError, "raw '<'"):
            h4_monitor_notify.validate_telegram_html("<b>ok</b>\nprogress < 0.25R")

    def test_format_trade_execution_result_is_russian_emoji_block(self):
        output = {
            "status": "EXECUTED_DEMO",
            "proposal": {
                "symbol": "MTSS@MISX",
                "quantity": 830,
                "entry": {"limit_price": "233.5"},
                "protective_stop": {"stop_price": "230.36"},
            },
            "buy_order_response": {
                "order_id": "6122935",
                "status": "ORDER_STATUS_NEW",
                "order": {
                    "symbol": "MTSS@MISX",
                    "quantity": {"value": "830.0"},
                    "limit_price": {"value": "233.5"},
                },
            },
            "protective_stop_response": {"order_id": "6122936", "status": "ORDER_STATUS_WATCHING"},
            "stop_verification": {"verified": True},
            "halt_new_buys": False,
        }

        text = h4_monitor_notify.format_trade_execution_result(output)

        self.assertIn("<b>MTSS@MISX</b>", text)
        self.assertIn("✅ BUY выполнен / DEMO", text)
        self.assertIn("📦 Кол-во: 830 шт.", text)
        self.assertIn("💰 Цена LIMIT: 233.50", text)
        self.assertIn("🧾 BUY Order: 6122935", text)
        self.assertIn("📌 Статус BUY: NEW", text)
        self.assertIn("🛡️ SL: 230.36", text)
        self.assertIn("🧾 SL Order: 6122936", text)
        self.assertIn("✅ SL проверен: да", text)
        self.assertIn("🚦 Новые покупки: разрешены", text)
        self.assertNotIn("BUY response", text)


    def test_build_outbox_report_creates_preview_not_auto_resumable_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(h4_monitor_notify.h4_monitor, "build_report", return_value=self.sample_report()):
                item = h4_monitor_notify.build_outbox_report(outbox_root=Path(tmp), include_research=False)
            delivery = json.loads((Path(tmp) / item["run_id"] / "delivery.json").read_text(encoding="utf-8"))
            latest = h4_monitor_notify.latest_sendable_outbox_run(
                Path(tmp),
                now=h4_monitor_notify.datetime(2026, 5, 22, 15, 10, tzinfo=h4_monitor_notify.timezone.utc),
            )

        self.assertEqual(delivery["status"], "preview")
        self.assertFalse(delivery["sendable"])
        self.assertIsNone(latest)


if __name__ == "__main__":
    unittest.main()
