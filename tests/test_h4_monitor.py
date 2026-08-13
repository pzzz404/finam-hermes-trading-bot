import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location("h4_monitor_script", ROOT / "scripts" / "h4_monitor.py")
h4_monitor = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(h4_monitor)


class H4MonitorTests(unittest.TestCase):
    def test_decision_id_uses_sha256_prefix(self):
        candidate = {"symbol": "SBER@MISX", "candidate_source": "static", "status": "BLOCKED", "gate_reasons": ["risk"]}

        self.assertEqual(h4_monitor._decision_id(candidate), "588e0fc9b644")

    def test_closed_bars_excludes_current_forming_h4(self):
        bars = [
            {"time": "2026-05-21T01:00:00Z"},
            {"time": "2026-05-21T05:00:00Z"},
            {"time": "2026-05-21T09:00:00Z"},
        ]

        closed = h4_monitor._closed_bars(
            bars,
            now=datetime(2026, 5, 21, 9, 30, tzinfo=timezone.utc),
            timeframe=timedelta(hours=4),
        )

        self.assertEqual([item["time"] for item in closed], ["2026-05-21T01:00:00Z", "2026-05-21T05:00:00Z"])

    def test_target_gate_blocks_autonomous_buy_when_nearest_target_below_min_r(self):
        candidate = {"symbol": "LKOH@MISX", "status": "PROPOSE_ONLY", "requires_confirmation": True}

        gated = h4_monitor._apply_candidate_gates(
            candidate,
            min_target_r=Decimal("1.5"),
            nearest_target_r=Decimal("0.6"),
            second_tier=False,
            complete=True,
        )

        self.assertEqual(gated["status"], "BLOCKED")
        self.assertIn("target_r_below_min_1.5", gated["gate_reasons"])
        self.assertTrue(gated["requires_confirmation"])
        self.assertEqual(gated["decision_record"]["action"], "do_not_buy")

    def test_unknown_target_requires_confirmation_and_propose_only(self):
        candidate = {"symbol": "MOEX@MISX", "status": "PROPOSE_ONLY", "requires_confirmation": False}

        gated = h4_monitor._apply_candidate_gates(
            candidate,
            min_target_r=Decimal("1.5"),
            nearest_target_r=None,
            second_tier=False,
            complete=True,
        )

        self.assertEqual(gated["status"], "PROPOSE_ONLY")
        self.assertTrue(gated["requires_confirmation"])
        self.assertIn("unknown_target_resistance", gated["gate_reasons"])
        self.assertEqual(gated["decision_record"]["action"], "manual_confirmation_required")

    def test_incomplete_sl_tp_quantity_or_risk_blocks_candidate(self):
        candidate = {"symbol": "ROSN@MISX", "status": "PROPOSE_ONLY", "requires_confirmation": False}

        gated = h4_monitor._apply_candidate_gates(
            candidate,
            min_target_r=Decimal("1.5"),
            nearest_target_r=Decimal("2"),
            second_tier=False,
            complete=False,
        )

        self.assertEqual(gated["status"], "BLOCKED")
        self.assertTrue(gated["requires_confirmation"])
        self.assertIn("incomplete_sl_tp_quantity_or_risk", gated["gate_reasons"])

    def test_second_tier_requires_separate_confirmation(self):
        candidate = {"symbol": "GAZP@MISX", "status": "PROPOSE_ONLY", "requires_confirmation": False}

        gated = h4_monitor._apply_candidate_gates(
            candidate,
            min_target_r=Decimal("1.5"),
            nearest_target_r=Decimal("2"),
            second_tier=True,
            complete=True,
        )

        self.assertEqual(gated["status"], "PROPOSE_ONLY")
        self.assertTrue(gated["requires_confirmation"])
        self.assertIn("second_tier_requires_confirmation", gated["gate_reasons"])

    def test_codex_review_avoid_blocks_trade(self):
        candidates = [{"symbol": "PLZL@MISX", "status": "PROPOSE_ONLY", "requires_confirmation": True}]
        research = {"status": "ok", "summary": "PLZL@MISX: AVOID — дивидендный гэп-риск"}

        h4_monitor._apply_research_gates(
            candidates,
            research,
            explicit_avoid_blocks_trade=True,
        )

        self.assertEqual(candidates[0]["status"], "BLOCKED")
        self.assertIn("codex_review_avoid", candidates[0]["gate_reasons"])
        self.assertEqual(candidates[0]["decision_record"]["action"], "do_not_buy")

    def test_research_timeout_keeps_candidate_propose_only_with_confirmation(self):
        candidates = [{"symbol": "TATN@MISX", "status": "PROPOSE_ONLY", "requires_confirmation": False}]
        research = {"status": "failed", "error": "timeout"}

        h4_monitor._apply_research_gates(
            candidates,
            research,
            explicit_avoid_blocks_trade=True,
        )

        self.assertEqual(candidates[0]["status"], "PROPOSE_ONLY")
        self.assertTrue(candidates[0]["requires_confirmation"])
        self.assertIn("codex_review_required", candidates[0]["gate_reasons"])

    def test_researchable_candidates_skip_blocked_by_default(self):
        candidates = [
            {"symbol": "SBER@MISX", "status": "BLOCKED"},
            {"symbol": "GAZP@MISX", "status": "PROPOSE_ONLY"},
        ]

        result = h4_monitor._researchable_candidates(candidates, {"research": {}})

        self.assertEqual([item["symbol"] for item in result], ["GAZP@MISX"])

    def test_quantity_rounds_down_to_lot_size(self):
        self.assertEqual(
            h4_monitor._round_quantity_to_lot(Decimal("831"), Decimal("10")),
            Decimal("830"),
        )
        self.assertEqual(
            h4_monitor._round_quantity_to_lot(Decimal("601"), Decimal("1")),
            Decimal("601"),
        )

    def test_lot_size_parses_nested_finam_asset_response(self):
        asset = {"asset": {"security": {"lot_size": {"value": "10"}}}}

        self.assertEqual(h4_monitor._lot_size_from_asset(asset), Decimal("10"))

    def test_moex_lot_size_uses_tqbr_shares_endpoint_without_warning(self):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        response.read.return_value = json.dumps(
            {
                "securities": {
                    "columns": ["SECID", "LOTSIZE", "SHORTNAME", "BOARDID"],
                    "data": [["MTSS", 10, "МТС-ао", "TQBR"]],
                }
            }
        ).encode("utf-8")
        errors: list[str] = []

        with mock.patch.object(h4_monitor.urllib.request, "urlopen", return_value=response) as urlopen:
            lot_size = h4_monitor._moex_lot_size("MTSS@MISX", errors=errors)

        self.assertEqual(lot_size, Decimal("10"))
        self.assertEqual(errors, [])
        self.assertIn("/boards/TQBR/securities/MTSS.json", urlopen.call_args.args[0])

    def test_moex_lot_size_falls_back_to_market_endpoint_when_tqbr_empty(self):
        empty_response = mock.Mock()
        empty_response.__enter__ = mock.Mock(return_value=empty_response)
        empty_response.__exit__ = mock.Mock(return_value=None)
        empty_response.read.return_value = json.dumps(
            {"securities": {"columns": ["SECID", "LOTSIZE", "SHORTNAME", "BOARDID"], "data": []}}
        ).encode("utf-8")
        market_response = mock.Mock()
        market_response.__enter__ = mock.Mock(return_value=market_response)
        market_response.__exit__ = mock.Mock(return_value=None)
        market_response.read.return_value = json.dumps(
            {
                "securities": {
                    "columns": ["SECID", "LOTSIZE", "SHORTNAME", "BOARDID"],
                    "data": [["MTSS", 10, "МТС-ао", "TQBR"]],
                }
            }
        ).encode("utf-8")
        errors: list[str] = []

        with mock.patch.object(h4_monitor.urllib.request, "urlopen", side_effect=[empty_response, market_response]) as urlopen:
            lot_size = h4_monitor._moex_lot_size("MTSS@MISX", errors=errors)

        self.assertEqual(lot_size, Decimal("10"))
        self.assertEqual(errors, [])
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("/markets/shares/securities/MTSS.json", urlopen.call_args_list[1].args[0])

    def test_instrument_rules_block_without_report_warning_when_moex_lot_exists(self):
        client = mock.Mock()
        client.asset.side_effect = RuntimeError("Finam HTTP 404")
        errors: list[str] = []

        with mock.patch.object(h4_monitor, "_moex_lot_size", return_value=Decimal("1")) as moex_lot_size:
            rules, verified = h4_monitor._instrument_rules(
                client,
                "jwt",
                "DOMRF@MISX",
                account_id="account",
                errors=errors,
            )

        self.assertFalse(verified)
        self.assertEqual(rules.lot_size, Decimal("1"))
        self.assertEqual(errors, [])
        moex_lot_size.assert_called_once()

    def test_instrument_rules_warn_when_finam_and_moex_contracts_are_unavailable(self):
        client = mock.Mock()
        client.asset.side_effect = RuntimeError("Finam HTTP 404")
        errors: list[str] = []

        def moex_lot_size(_symbol, *, errors):
            errors.append("BROKEN@MISX: MOEX lot size не найден")
            return Decimal("1")

        with mock.patch.object(h4_monitor, "_moex_lot_size", side_effect=moex_lot_size):
            _rules, verified = h4_monitor._instrument_rules(
                client,
                "jwt",
                "BROKEN@MISX",
                account_id="account",
                errors=errors,
            )

        self.assertFalse(verified)
        self.assertEqual(len(errors), 2)
        self.assertIn("Finam instrument contract недоступен", errors[0])
        self.assertIn("MOEX lot size не найден", errors[1])

    def test_moex_lot_size_unexpected_format_warns_and_defaults_to_one(self):
        response = mock.Mock()
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=None)
        response.read.return_value = json.dumps({"description": {"data": []}}).encode("utf-8")
        errors: list[str] = []

        with mock.patch.object(h4_monitor.urllib.request, "urlopen", return_value=response):
            lot_size = h4_monitor._moex_lot_size("MTSS@MISX", errors=errors)

        self.assertEqual(lot_size, Decimal("1"))
        self.assertEqual(errors, ["MTSS@MISX: MOEX lot size вернул неожиданный формат"])

    def test_total_open_risk_pct_blocks_new_candidates_above_limit(self):
        self.assertTrue(
            h4_monitor._total_open_risk_exceeds_limit(
                open_risk_rub=Decimal("12000.01"),
                equity=Decimal("400000"),
                max_total_open_risk_pct=Decimal("3.0"),
            )
        )

    def test_growth_kpi_is_report_only_and_calculates_required_pace(self):
        policy = {
            "growth_mode": {
                "enabled": False,
                "report_only": True,
                "target_annual_return": "0.5",
                "stretch_annual_return": "1.0",
            }
        }
        account = {"equity": "392210.37"}

        kpi = h4_monitor._growth_kpi(policy, account)

        self.assertFalse(kpi["enabled"])
        self.assertTrue(kpi["report_only"])
        self.assertEqual(kpi["current_equity"], "392210.37")
        self.assertEqual(kpi["target_required_monthly_pct"], "3.44")
        self.assertEqual(kpi["target_required_weekly_pct"], "0.78")
        self.assertEqual(kpi["target_required_trading_day_pct"], "0.16")
        self.assertEqual(kpi["stretch_required_monthly_pct"], "5.95")
        self.assertEqual(kpi["stretch_required_weekly_pct"], "1.34")
        self.assertEqual(kpi["stretch_required_trading_day_pct"], "0.28")
        self.assertEqual(kpi["tracking_status"], "report_only")

    def test_growth_score_rewards_rr_signal_contract_and_penalizes_gates(self):
        strong = {
            "symbol": "SBER@MISX",
            "status": "PROPOSE_ONLY",
            "nearest_target_r": "2.0",
            "atr14": "3.2",
            "finam_contract": {"verified": True},
            "signal": {"status": "WATCH"},
            "gate_reasons": [],
        }
        blocked = {
            "symbol": "GAZP@MISX",
            "status": "BLOCKED",
            "nearest_target_r": "0.4",
            "atr14": "1.1",
            "finam_contract": {"verified": False},
            "signal": {"status": "WATCH"},
            "gate_reasons": ["target_r_below_min_1.5", "finam_contract_unverified"],
        }

        candidates = [blocked, strong]
        h4_monitor._apply_growth_scores(candidates, {"verdict": "OK"})

        self.assertEqual(strong["growth_score"]["score"], 70)
        self.assertEqual(strong["growth_score"]["label"], "MEDIUM")
        self.assertEqual(blocked["growth_score"]["label"], "LOW")
        self.assertLess(blocked["growth_score"]["score"], strong["growth_score"]["score"])
        self.assertEqual([item["symbol"] for item in candidates], ["GAZP@MISX", "SBER@MISX"])

    def test_growth_kpi_v2_tracks_mtd_ytd_r_and_target_gap(self):
        policy = {
            "growth_mode": {
                "enabled": False,
                "report_only": True,
                "target_annual_return": "0.5",
                "stretch_annual_return": "1.0",
                "mtd_start_equity": "390000",
                "ytd_start_equity": "360000",
                "accumulated_r": "1.25",
            }
        }
        account = {"equity": "396000"}

        kpi = h4_monitor._growth_kpi(policy, account, now=datetime(2026, 3, 31, tzinfo=timezone.utc))

        self.assertEqual(kpi["mtd_return_pct"], "1.54")
        self.assertEqual(kpi["ytd_return_pct"], "10.00")
        self.assertEqual(kpi["accumulated_r"], "1.25")
        self.assertEqual(kpi["target_required_ytd_pct"], "10.51")
        self.assertEqual(kpi["target_gap_pct"], "0.51")
        self.assertEqual(kpi["target_gap_rub"], "1852.66")
        self.assertEqual(kpi["goal_status"], "BEHIND")

    def test_growth_score_v2_exposes_required_breakdown_and_entry_blockers(self):
        candidate = {
            "symbol": "SBER@MISX",
            "status": "BLOCKED",
            "nearest_target_r": "1.2",
            "atr14": "3.2",
            "notional": "250000",
            "relative_strength_score": 12,
            "signal": {"status": "WATCH"},
            "gate_reasons": ["target_r_below_min_1.5", "perplexity_news_avoid"],
        }

        h4_monitor._apply_growth_scores([candidate], {"verdict": "RISK"})

        components = candidate["growth_score"]["components"]
        self.assertEqual(set(components), {"h4_momentum", "risk_reward", "relative_strength", "liquidity", "atr", "research", "research_context", "gate_penalty"})
        self.assertEqual(components["h4_momentum"], 25)
        self.assertEqual(components["risk_reward"], 15)
        self.assertEqual(components["relative_strength"], 12)
        self.assertEqual(components["liquidity"], 10)
        self.assertEqual(components["atr"], 10)
        self.assertEqual(components["research"], -10)
        self.assertEqual(components["research_context"], 0)
        self.assertEqual(components["gate_penalty"], -45)
        self.assertEqual(candidate["entry_blockers"], [
            "R/R ниже минимума 1.5R",
            "research дал AVOID/RISK по новости",
        ])

    def test_research_context_is_advisory_score_modifier_not_trade_gate(self):
        candidate = {
            "symbol": "SBER@MISX",
            "status": "PROPOSE_ONLY",
            "requires_confirmation": True,
            "nearest_target_r": "2.0",
            "atr14": "3.2",
            "notional": "250000",
            "signal": {"status": "WATCH"},
            "gate_reasons": [],
        }

        h4_monitor._apply_research_context(
            [candidate],
            {"weekly": {"priority_watchlist": ["SBER@MISX"], "avoid_symbols": []}},
        )
        h4_monitor._apply_growth_scores([candidate], {"items": [{"symbol": "SBER@MISX", "verdict": "OK"}]})

        self.assertEqual(candidate["status"], "PROPOSE_ONLY")
        self.assertEqual(candidate["growth_score"]["components"]["research_context"], 5)
        self.assertEqual(candidate["entry_blockers"], [])
        self.assertEqual(candidate["research_context"]["notes"], ["weekly: priority_watchlist"])
        self.assertEqual(candidate["research_context"]["blocking_notes"], [])

    def test_research_context_avoid_and_events_are_entry_blockers(self):
        candidate = {
            "symbol": "SBER@MISX",
            "status": "PROPOSE_ONLY",
            "requires_confirmation": True,
            "nearest_target_r": "2.0",
            "atr14": "3.2",
            "notional": "250000",
            "signal": {"status": "WATCH"},
            "gate_reasons": [],
        }

        h4_monitor._apply_research_context(
            [candidate],
            {
                "daily": {
                    "avoid_symbols": ["SBER@MISX"],
                    "event_watchlist": [{"symbol": "SBER@MISX", "event": "дивидендная отсечка"}],
                }
            },
        )
        h4_monitor._apply_growth_scores([candidate], {"items": [{"symbol": "SBER@MISX", "verdict": "OK"}]})

        self.assertEqual(candidate["growth_score"]["components"]["research_context"], -15)
        self.assertEqual(candidate["entry_blockers"], [
            "research context: daily: avoid_symbols",
            "research context: daily: дивидендная отсечка",
        ])

    def test_position_advisory_action_is_report_only_and_does_not_mutate_stops(self):
        position = {"symbol": "MOEX@MISX", "quantity": "710", "average_price": "100", "current_price": "104"}
        market = {"atr14": "2", "signal": {"status": "WATCH"}}
        risk = {"stop_atr_multiplier": "2", "take_profit_r": "2"}

        enriched = h4_monitor._enrich_position(position, market, risk)

        self.assertEqual(enriched["progress_r"], "1")
        self.assertEqual(enriched["advisory_action"], {
            "action": "BREAKEVEN_CANDIDATE",
            "report_only": True,
            "reason": "+1R достигнут; можно предложить breakeven/trailing, но стоп не двигать без подтверждения",
        })

    def test_open_position_r_does_not_override_missing_accumulated_r(self):
        growth = {"accumulated_r": None}
        positions = [{"progress_r": "0.8"}, {"progress_r": "-0.2"}]

        h4_monitor._apply_growth_position_r(growth, positions)

        self.assertEqual(growth["open_position_r"], "0.6")
        self.assertIsNone(growth["accumulated_r"])

    def test_max_new_trades_does_not_stop_static_market_discovery(self):
        policy = {
            "risk": {
                "max_open_positions": 5,
                "max_new_trades_per_run": 1,
                "max_total_open_risk_pct": "3.0",
                "risk_per_trade_pct": "1.0",
                "min_target_r": "1.5",
                "stop_atr_multiplier": "2",
                "take_profit_r": "2",
            },
            "permissions": {},
            "growth_mode": {"allow_short_analysis": True, "max_short_candidates": "bad"},
            "universe": ["SBER@MISX", "GAZP@MISX", "LKOH@MISX"],
        }
        market = {
            "atr14": "1",
            "signal": {"status": "WATCH"},
            "short_signal": {"status": "WATCH", "orders_allowed": False},
            "closed_h4": [{"high": "110"}],
        }
        short_candidates: list[dict[str, object]] = []

        with (
            mock.patch.object(h4_monitor, "_market_context", return_value=market) as market_context,
            mock.patch.object(h4_monitor, "_safe_quote", return_value={"last": "100"}),
            mock.patch.object(
                h4_monitor,
                "_instrument_rules",
                return_value=(
                    h4_monitor.FinamInstrumentRules(
                        symbol="SBER@MISX",
                        lot_size=Decimal("1"),
                        decimals=2,
                        min_step=Decimal("0.01"),
                        price_step=Decimal("0.01"),
                        is_tradable=True,
                        longable="AVAILABLE",
                    ),
                    True,
                ),
            ),
        ):
            candidates = h4_monitor._scan_candidates(
                mock.Mock(),
                "jwt",
                account_id="account",
                policy=policy,
                held_symbols=set(),
                cash=Decimal("100000"),
                equity=Decimal("100000"),
                open_positions=0,
                open_risk_rub=Decimal("0"),
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                errors=[],
                short_candidates=short_candidates,
            )

        self.assertEqual(market_context.call_count, 3)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0]["actionable_this_run"], True)
        self.assertEqual([item["symbol"] for item in short_candidates], ["SBER@MISX", "GAZP@MISX", "LKOH@MISX"])
        self.assertTrue(any("max_new_trades_per_run_selection_limit" in item.get("gate_reasons", []) for item in candidates[1:]))

    def test_dynamic_watchlist_discovers_aflt_outside_static_universe(self):
        policy = {
            "universe": ["SBER@MISX"],
            "dynamic_universe": {
                "enabled": True,
                "boards": ["TQBR"],
                "max_scan_symbols": 80,
                "min_daily_turnover_rub": 100000000,
                "min_trades_today": 500,
                "min_price_rub": 5,
            },
        }
        rows = [
            {
                "symbol": "AFLT@MISX",
                "price": "65",
                "turnover_rub": "250000000",
                "trades_today": "4000",
                "day_momentum_pct": "2.5",
                "time": "12:00:00",
                "board": "TQBR",
            },
            {
                "symbol": "SBER@MISX",
                "price": "320",
                "turnover_rub": "900000000",
                "trades_today": "9000",
                "day_momentum_pct": "1.0",
                "time": "12:00:00",
                "board": "TQBR",
            },
        ]

        with mock.patch.object(h4_monitor, "_moex_tqbr_market_snapshot", return_value=rows):
            watchlist = h4_monitor._discover_dynamic_watchlist(
                policy,
                now=datetime(2026, 5, 25, 9, 0, tzinfo=timezone.utc),
                errors=[],
            )

        self.assertEqual(watchlist[0]["symbol"], "AFLT@MISX")
        self.assertEqual(watchlist[0]["status"], "DISCOVERED")
        self.assertIn("liquid_turnover", watchlist[0]["discovery_reasons"])
        self.assertNotIn("SBER@MISX", [item["symbol"] for item in watchlist])

    def test_low_liquidity_dynamic_symbol_is_rejected(self):
        item = h4_monitor._dynamic_watchlist_item(
            {
                "symbol": "LOWL@MISX",
                "price": "20",
                "turnover_rub": "1000000",
                "trades_today": "12",
                "day_momentum_pct": "3",
                "time": "12:00:00",
            },
            config=h4_monitor.DEFAULT_POLICY["dynamic_universe"],
            now=datetime(2026, 5, 25, 9, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(item["status"], "REJECTED")
        self.assertIn("low_turnover", item["reject_reasons"])
        self.assertIn("low_trades", item["reject_reasons"])

    def test_dynamic_candidate_keeps_source_score_and_requires_confirmation(self):
        policy = {
            "risk": {
                "max_open_positions": 5,
                "max_new_trades_per_run": 1,
                "max_total_open_risk_pct": "3.0",
                "risk_per_trade_pct": "1.0",
                "min_target_r": "1.5",
                "stop_atr_multiplier": "2",
                "take_profit_r": "2",
            },
            "permissions": {},
            "dynamic_universe": h4_monitor.DEFAULT_POLICY["dynamic_universe"],
            "universe": [],
        }
        market = {
            "atr14": "1",
            "signal": {"status": "WATCH"},
            "short_signal": {"status": "WAIT", "orders_allowed": False},
            "closed_h4": [{"high": "120"}],
        }
        watchlist = [
            {
                "symbol": "AFLT@MISX",
                "status": "DISCOVERED",
                "discovery_score": 80,
                "discovery_reasons": ["liquid_turnover", "positive_day_momentum"],
            }
        ]

        with (
            mock.patch.object(h4_monitor, "_market_context", return_value=market),
            mock.patch.object(h4_monitor, "_safe_quote", return_value={"last": "100"}),
            mock.patch.object(
                h4_monitor,
                "_instrument_rules",
                return_value=(
                    h4_monitor.FinamInstrumentRules(
                        symbol="AFLT@MISX",
                        lot_size=Decimal("1"),
                        decimals=2,
                        min_step=Decimal("0.01"),
                        price_step=Decimal("0.01"),
                        is_tradable=True,
                        longable="AVAILABLE",
                    ),
                    True,
                ),
            ),
        ):
            candidates = h4_monitor._scan_candidates(
                mock.Mock(),
                "jwt",
                account_id="account",
                policy=policy,
                held_symbols=set(),
                cash=Decimal("100000"),
                equity=Decimal("100000"),
                open_positions=0,
                open_risk_rub=Decimal("0"),
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                errors=[],
                dynamic_watchlist=watchlist,
            )

        self.assertEqual(candidates[0]["symbol"], "AFLT@MISX")
        self.assertEqual(candidates[0]["candidate_source"], "dynamic")
        self.assertEqual(candidates[0]["discovery_score"], 80)
        self.assertTrue(candidates[0]["requires_confirmation"])

    def test_dynamic_watchlist_gets_report_only_rr_for_every_item_and_sorts_by_rr(self):
        risk = {"stop_atr_multiplier": "2"}
        watchlist = [
            {
                "symbol": "LOWRR@MISX",
                "status": "DISCOVERED",
                "price": "100",
                "discovery_score": 100,
            },
            {
                "symbol": "HIGHRR@MISX",
                "status": "DISCOVERED",
                "price": "100",
                "discovery_score": 60,
            },
            {
                "symbol": "NODATA@MISX",
                "status": "DISCOVERED",
                "price": "100",
                "discovery_score": 90,
            },
        ]

        def market_context(_client, _jwt, symbol, *, now, risk, errors):
            if symbol == "LOWRR@MISX":
                return {"atr14": "1", "closed_h4": [{"high": "102"}], "signal": {"status": "WAIT"}}
            if symbol == "HIGHRR@MISX":
                return {"atr14": "1", "closed_h4": [{"high": "106"}], "signal": {"status": "WATCH"}}
            return {"atr14": None, "closed_h4": [], "signal": {"status": "WAIT"}}

        with mock.patch.object(h4_monitor, "_market_context", side_effect=market_context):
            h4_monitor._enrich_dynamic_watchlist_risk_reward(
                mock.Mock(),
                "jwt",
                watchlist,
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                risk=risk,
            )

        self.assertEqual([item["symbol"] for item in watchlist], ["HIGHRR@MISX", "LOWRR@MISX", "NODATA@MISX"])
        self.assertEqual(watchlist[0]["nearest_target_r"], "3")
        self.assertEqual(watchlist[0]["nearest_target"], "106")
        self.assertEqual(watchlist[0]["atr14"], "1")
        self.assertTrue(watchlist[0]["rr_report_only"])
        self.assertEqual(watchlist[1]["nearest_target_r"], "1")
        self.assertIsNone(watchlist[2]["nearest_target_r"])
        self.assertEqual(watchlist[2]["rr_unavailable_reason"], "missing_atr_or_price")

    def test_dynamic_watchlist_rr_skips_items_rejected_before_rr_gate(self):
        risk = {"stop_atr_multiplier": "2", "min_target_r": "1.5"}
        watchlist = [
            {
                "symbol": "LOWLIQ@MISX",
                "status": "REJECTED",
                "reject_reasons": ["low_turnover"],
                "price": "100",
                "discovery_score": 90,
            },
            {
                "symbol": "LOWRR@MISX",
                "status": "REJECTED",
                "reject_reasons": ["target_r_below_min_1.5"],
                "price": "100",
                "discovery_score": 80,
            },
        ]

        def market_context(_client, _jwt, symbol, *, now, risk, errors):
            return {"atr14": "1", "closed_h4": [{"high": "102"}], "signal": {"status": "WATCH"}}

        with mock.patch.object(h4_monitor, "_market_context", side_effect=market_context) as market_context_mock:
            h4_monitor._enrich_dynamic_watchlist_risk_reward(
                mock.Mock(),
                "jwt",
                watchlist,
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                risk=risk,
            )

        self.assertEqual(market_context_mock.call_count, 1)
        self.assertEqual(market_context_mock.call_args.args[2], "LOWRR@MISX")
        by_symbol = {item["symbol"]: item for item in watchlist}
        self.assertEqual(by_symbol["LOWLIQ@MISX"]["rr_unavailable_reason"], "skipped_before_rr_gate")
        self.assertEqual(by_symbol["LOWRR@MISX"]["nearest_target_r"], "1")

    def test_dynamic_watchlist_adds_report_only_rr_tiers_and_entry_price_for_min_r(self):
        risk = {"stop_atr_multiplier": "2", "min_target_r": "1.5"}
        watchlist = [
            {"symbol": "A@MISX", "status": "DISCOVERED", "price": "100", "discovery_score": 10},
            {"symbol": "B@MISX", "status": "DISCOVERED", "price": "100", "discovery_score": 10},
            {"symbol": "C@MISX", "status": "DISCOVERED", "price": "100", "discovery_score": 10},
            {"symbol": "D@MISX", "status": "DISCOVERED", "price": "100", "discovery_score": 10},
        ]

        highs_by_symbol = {
            "A@MISX": "104",  # 2.0R, current entry already qualifies.
            "B@MISX": "102.4",  # 1.2R, half-risk advisory.
            "C@MISX": "101.8",  # 0.9R, quarter-risk/manual advisory.
            "D@MISX": "101",  # 0.5R, wait for pullback.
        }

        def market_context(_client, _jwt, symbol, *, now, risk, errors):
            return {"atr14": "1", "closed_h4": [{"high": highs_by_symbol[symbol]}], "signal": {"status": "WATCH"}}

        with mock.patch.object(h4_monitor, "_market_context", side_effect=market_context):
            h4_monitor._enrich_dynamic_watchlist_risk_reward(
                mock.Mock(),
                "jwt",
                watchlist,
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                risk=risk,
            )

        by_symbol = {item["symbol"]: item for item in watchlist}
        self.assertEqual(by_symbol["A@MISX"]["rr_strategy"]["tier"], "A")
        self.assertEqual(by_symbol["A@MISX"]["rr_strategy"]["risk_multiplier"], "1")
        self.assertEqual(by_symbol["B@MISX"]["rr_strategy"]["tier"], "B")
        self.assertEqual(by_symbol["B@MISX"]["rr_strategy"]["risk_multiplier"], "0.5")
        self.assertEqual(by_symbol["C@MISX"]["rr_strategy"]["tier"], "C")
        self.assertEqual(by_symbol["C@MISX"]["rr_strategy"]["risk_multiplier"], "0.25")
        self.assertEqual(by_symbol["D@MISX"]["rr_strategy"]["tier"], "WAIT")
        self.assertEqual(by_symbol["B@MISX"]["entry_price_for_min_r"], "99.4")
        self.assertEqual(by_symbol["B@MISX"]["required_pullback_pct"], "0.600")
        self.assertTrue(by_symbol["C@MISX"]["rr_strategy"]["manual_confirmation_required"])
        self.assertTrue(by_symbol["C@MISX"]["rr_strategy"]["report_only"])

    def test_held_position_scale_in_is_blocked_before_one_r(self):
        policy = {
            "risk": {
                "max_open_positions": 5,
                "max_new_trades_per_run": 1,
                "max_total_open_risk_pct": "3.0",
                "risk_per_trade_pct": "1.0",
                "min_target_r": "1.5",
                "stop_atr_multiplier": "2",
                "take_profit_r": "2",
            },
            "permissions": {"second_tier_requires_confirmation": True},
            "universe": ["MOEX@MISX"],
        }
        market = {
            "atr14": "1",
            "signal": {"status": "WATCH", "reason": "h4_bullish_breakout_candidate"},
            "short_signal": {"status": "WAIT", "orders_allowed": False},
            "closed_h4": [{"high": "110"}],
        }
        held_position = {
            "symbol": "MOEX@MISX",
            "quantity": "710",
            "average_price": "172.70",
            "progress_r": "0.75",
            "has_watching_sell_sltp": True,
        }

        with (
            mock.patch.object(h4_monitor, "_market_context", return_value=market),
            mock.patch.object(h4_monitor, "_safe_quote", return_value={"last": "100"}),
            mock.patch.object(
                h4_monitor,
                "_instrument_rules",
                return_value=(
                    h4_monitor.FinamInstrumentRules(
                        symbol="MOEX@MISX",
                        lot_size=Decimal("10"),
                        decimals=2,
                        min_step=Decimal("0.01"),
                        price_step=Decimal("0.01"),
                        is_tradable=True,
                        longable="AVAILABLE",
                    ),
                    True,
                ),
            ),
        ):
            candidates = h4_monitor._scan_candidates(
                mock.Mock(),
                "jwt",
                account_id="account",
                policy=policy,
                held_symbols={"MOEX@MISX"},
                held_positions_by_symbol={"MOEX@MISX": held_position},
                cash=Decimal("200000"),
                equity=Decimal("400000"),
                open_positions=1,
                open_risk_rub=Decimal("4000"),
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                errors=[],
            )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["symbol"], "MOEX@MISX")
        self.assertEqual(candidate["candidate_source"], "held_scale_in")
        self.assertEqual(candidate["status"], "BLOCKED")
        self.assertIn("scale_in_progress_below_1r", candidate["gate_reasons"])
        self.assertEqual(candidate["current_position"]["quantity"], "710")
        self.assertEqual(candidate["projected_position"]["total_quantity"], "2710")

    def test_held_scale_in_does_not_consume_new_trade_selection(self):
        policy = {
            "risk": {
                "max_open_positions": 5,
                "max_new_trades_per_run": 1,
                "max_total_open_risk_pct": "3.0",
                "risk_per_trade_pct": "1.0",
                "min_target_r": "1.5",
                "stop_atr_multiplier": "2",
                "take_profit_r": "2",
            },
            "permissions": {"second_tier_requires_confirmation": True},
            "universe": ["SBER@MISX", "MOEX@MISX"],
        }
        market = {
            "atr14": "1",
            "signal": {"status": "WATCH", "reason": "h4_bullish_breakout_candidate"},
            "short_signal": {"status": "WAIT", "orders_allowed": False},
            "closed_h4": [{"high": "110"}],
        }
        held_position = {
            "symbol": "MOEX@MISX",
            "quantity": "710",
            "average_price": "90",
            "progress_r": "1.2",
            "has_watching_sell_sltp": True,
            "watching_sell_sltp_details": [{"quantity": "710", "stop": "88"}],
        }

        with (
            mock.patch.object(h4_monitor, "_market_context", return_value=market),
            mock.patch.object(h4_monitor, "_safe_quote", return_value={"last": "100"}),
            mock.patch.object(
                h4_monitor,
                "_instrument_rules",
                return_value=(
                    h4_monitor.FinamInstrumentRules(
                        symbol="SBER@MISX",
                        lot_size=Decimal("10"),
                        decimals=2,
                        min_step=Decimal("0.01"),
                        price_step=Decimal("0.01"),
                        is_tradable=True,
                        longable="AVAILABLE",
                    ),
                    True,
                ),
            ),
        ):
            candidates = h4_monitor._scan_candidates(
                mock.Mock(),
                "jwt",
                account_id="account",
                policy=policy,
                held_symbols={"MOEX@MISX"},
                held_positions_by_symbol={"MOEX@MISX": held_position},
                cash=Decimal("200000"),
                equity=Decimal("400000"),
                open_positions=1,
                open_risk_rub=Decimal("4000"),
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                errors=[],
            )

        by_source = {item["candidate_source"]: item for item in candidates}
        self.assertTrue(by_source["static"]["actionable_this_run"])
        self.assertFalse(by_source["held_scale_in"]["actionable_this_run"])
        self.assertEqual(by_source["held_scale_in"]["status"], "PROPOSE_ONLY")
        self.assertIn("scale_in_live_execution_disabled", by_source["held_scale_in"]["gate_reasons"])
        self.assertNotIn("second_tier_requires_confirmation", by_source["held_scale_in"]["gate_reasons"])

    def test_held_scale_in_still_reports_when_position_count_at_limit(self):
        policy = {
            "risk": {
                "max_open_positions": 1,
                "max_new_trades_per_run": 1,
                "max_total_open_risk_pct": "3.0",
                "risk_per_trade_pct": "1.0",
                "min_target_r": "1.5",
                "stop_atr_multiplier": "2",
                "take_profit_r": "2",
            },
            "universe": ["MOEX@MISX"],
        }
        market = {
            "atr14": "1",
            "signal": {"status": "WATCH", "reason": "h4_bullish_breakout_candidate"},
            "closed_h4": [{"high": "110"}],
        }
        held_position = {
            "symbol": "MOEX@MISX",
            "quantity": "710",
            "average_price": "90",
            "progress_r": "1.2",
            "has_watching_sell_sltp": True,
            "watching_sell_sltp_details": [{"quantity": "710", "stop": "88"}],
        }

        with (
            mock.patch.object(h4_monitor, "_market_context", return_value=market),
            mock.patch.object(h4_monitor, "_safe_quote", return_value={"last": "100"}),
            mock.patch.object(
                h4_monitor,
                "_instrument_rules",
                return_value=(
                    h4_monitor.FinamInstrumentRules(
                        symbol="MOEX@MISX",
                        lot_size=Decimal("10"),
                        decimals=2,
                        min_step=Decimal("0.01"),
                        price_step=Decimal("0.01"),
                        is_tradable=True,
                        longable="AVAILABLE",
                    ),
                    True,
                ),
            ),
        ):
            candidates = h4_monitor._scan_candidates(
                mock.Mock(),
                "jwt",
                account_id="account",
                policy=policy,
                held_symbols={"MOEX@MISX"},
                held_positions_by_symbol={"MOEX@MISX": held_position},
                cash=Decimal("200000"),
                equity=Decimal("400000"),
                open_positions=1,
                open_risk_rub=Decimal("4000"),
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                errors=[],
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["candidate_source"], "held_scale_in")
        self.assertFalse(candidates[0]["actionable_this_run"])

    def test_held_scale_in_blocks_when_current_stop_does_not_cover_position(self):
        policy = {
            "risk": {
                "max_open_positions": 5,
                "max_new_trades_per_run": 1,
                "max_total_open_risk_pct": "3.0",
                "risk_per_trade_pct": "1.0",
                "min_target_r": "1.5",
                "stop_atr_multiplier": "2",
                "take_profit_r": "2",
            },
            "universe": ["MOEX@MISX"],
        }
        market = {
            "atr14": "1",
            "signal": {"status": "WATCH", "reason": "h4_bullish_breakout_candidate"},
            "closed_h4": [{"high": "110"}],
        }
        held_position = {
            "symbol": "MOEX@MISX",
            "quantity": "710",
            "average_price": "90",
            "progress_r": "1.2",
            "has_watching_sell_sltp": True,
            "watching_sell_sltp_details": [{"quantity": "700", "stop": "88"}],
        }

        with (
            mock.patch.object(h4_monitor, "_market_context", return_value=market),
            mock.patch.object(h4_monitor, "_safe_quote", return_value={"last": "100"}),
            mock.patch.object(
                h4_monitor,
                "_instrument_rules",
                return_value=(
                    h4_monitor.FinamInstrumentRules(
                        symbol="MOEX@MISX",
                        lot_size=Decimal("10"),
                        decimals=2,
                        min_step=Decimal("0.01"),
                        price_step=Decimal("0.01"),
                        is_tradable=True,
                        longable="AVAILABLE",
                    ),
                    True,
                ),
            ),
        ):
            candidates = h4_monitor._scan_candidates(
                mock.Mock(),
                "jwt",
                account_id="account",
                policy=policy,
                held_symbols={"MOEX@MISX"},
                held_positions_by_symbol={"MOEX@MISX": held_position},
                cash=Decimal("200000"),
                equity=Decimal("400000"),
                open_positions=1,
                open_risk_rub=Decimal("4000"),
                now=datetime(2026, 5, 25, tzinfo=timezone.utc),
                errors=[],
            )

        self.assertEqual(candidates[0]["status"], "BLOCKED")
        self.assertIn("current_protective_stop_incomplete", candidates[0]["gate_reasons"])

    def test_h4_research_excludes_held_scale_in_candidates(self):
        candidates = [
            {"symbol": "MOEX@MISX", "status": "PROPOSE_ONLY", "candidate_source": "held_scale_in"},
            {"symbol": "SBER@MISX", "status": "PROPOSE_ONLY", "candidate_source": "static"},
        ]
        policy = {
            "dynamic_universe": {"max_research_symbols": 2},
            "research": {"h4_max_candidates": 2},
        }

        with mock.patch.object(h4_monitor, "research_candidates", return_value={"status": "ok"}) as research:
            h4_monitor._research_candidates(candidates, policy)

        self.assertEqual([item["symbol"] for item in research.call_args.args[0]], ["SBER@MISX"])

    def test_short_signal_marks_bearish_breakdown_without_allowing_orders(self):
        bars = [
            {"open": "105", "high": "108", "low": "100", "close": "106"},
            {"open": "104", "high": "105", "low": "99", "close": "98"},
        ]

        signal = h4_monitor._short_signal(bars[-1], bars)

        self.assertEqual(signal["status"], "WATCH")
        self.assertEqual(signal["reason"], "h4_bearish_breakdown_candidate")
        self.assertFalse(signal["orders_allowed"])


if __name__ == "__main__":
    unittest.main()
