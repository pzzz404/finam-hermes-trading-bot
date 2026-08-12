import os
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot import arena


class _ArenaDailyLimitClient:
    def __init__(self, *, daily_us_buys: int):
        self.daily_us_buys = daily_us_buys

    def create_session(self, secret):
        return "arena-jwt"

    def get_account(self, jwt, account_id):
        positions = []
        if account_id == "DEMO-US":
            positions = [
                {
                    "symbol": "MSFT@XNGS",
                    "quantity": {"value": "10"},
                    "average_price": {"value": "300"},
                    "position_side": "LONG",
                }
            ]
        return {
            "account_id": account_id,
            "cash": {"value": "997000.00"},
            "equity": {"value": "1000000.00"},
            "available_cash": {"value": "997000.00"},
            "positions": positions,
        }

    def trades(self, jwt, account_id, *, limit=None, start_time=None):
        if account_id != "DEMO-US":
            return {"trades": []}
        return {
            "trades": [
                {
                    "symbol": f"BUY{i}@XNGS",
                    "side": "SIDE_BUY",
                    "quantity": {"value": "1"},
                    "price": {"value": "100"},
                    "time": f"2026-06-01T0{i}:00:00Z",
                    "trade_id": f"buy-{i}",
                }
                for i in range(self.daily_us_buys)
            ]
        }


class _ArenaReplacementMarketClient:
    def create_session(self, secret):
        return "market-jwt"

    def bars(self, jwt, symbol, *, interval, start_time, end_time):
        if symbol != "NVDA@XNGS":
            return {"bars": []}
        count = 16 if interval == "TIME_FRAME_H4" else 3
        return {"bars": [_arena_replacement_bar(index, 200 + index) for index in range(count)]}

    def last_quote(self, jwt, symbol):
        if symbol == "MSFT@XNGS":
            return {"last": {"value": "260"}}
        return {"last": {"value": "220"}}

    def asset_params(self, jwt, symbol, *, account_id):
        return {"shortable": {"value": "AVAILABLE"}}


def _arena_replacement_bar(index, value):
    return {
        "time": f"2026-05-30T{index:02d}:00:00Z",
        "open": {"value": str(value)},
        "high": {"value": str(value + 1)},
        "low": {"value": str(value - 1)},
        "close": {"value": str(value)},
    }


class ArenaTests(unittest.TestCase):
    def _auction_scan(self, candidate: dict | None = None) -> dict:
        account = {
            "account_id": "DEMO-US",
            "label": "США",
            "equity": "1000000",
            "cash": "900000",
            "entry_limits": {
                "primary_used": 0,
                "primary_limit": 2,
                "replacement_used": 0,
                "replacement_limit": 1,
                "replacement_ledger_available": True,
            },
        }
        return {
            "status": "OK",
            "mode": "approval",
            "accounts": [account],
            "candidates": [candidate] if candidate is not None else [],
            "research": {"status": "ok", "provider_call": False, "cache_hit": True},
            "errors": [],
            "warnings": [],
        }

    def _auction_candidate(self, *, score: int = 82, account_id: str = "DEMO-US", symbol: str = "AAPL@XNGS") -> dict:
        return {
            "account_id": account_id,
            "symbol": symbol,
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "notional": "100000",
            "arena_growth_score": {"score": score, "label": "HIGH", "components": {}},
        }

    def test_opportunity_auction_risk_exit_beats_replacement_and_base_entry(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        candidate = self._auction_candidate(score=90)
        scan = self._auction_scan(candidate)
        review = {
            "status": "OK",
            "accounts": [],
            "planned_actions": [{"account_id": "DEMO-US", "action": "EXIT_WEAK", "symbol": "MSFT@XNGS"}],
            "exit_proposals": [{"account_id": "DEMO-US", "action": "EXIT_WEAK", "symbol": "MSFT@XNGS", "reason": "weak_exit"}],
            "replacement_proposals": [
                {
                    "account_id": "DEMO-US",
                    "buy_symbol": "AAPL@XNGS",
                    "candidate_score": 95,
                    "estimated_cost_rub": "100",
                    "edge_after_cost_pct": "10",
                    "buy_candidate": candidate | {"quote_age_seconds": 10},
                }
            ],
            "errors": [],
            "warnings": [],
        }

        result = arena.build_arena_opportunity_auction(
            policy,
            scan,
            review,
            now=datetime(2026, 6, 8, 16, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["winner"]["opportunity_type"], "risk_exit")
        self.assertFalse(result["winner"]["broker_mutation"])
        self.assertEqual(result["score_model"]["kind"], "provisional_heuristic_not_ev_or_probability")

    def test_opportunity_auction_keeps_winner_account_scoped(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        scan = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "equity": "1000000", "entry_limits": {"primary_used": 0, "primary_limit": 1}},
                {"account_id": "DEMO-US", "equity": "1000000", "entry_limits": {"primary_used": 0, "primary_limit": 2}},
            ],
            "candidates": [
                self._auction_candidate(score=80, account_id="DEMO-RU", symbol="SBER@MISX"),
                self._auction_candidate(score=92, account_id="DEMO-US", symbol="AAPL@XNGS"),
            ],
            "research": {"status": "ok", "provider_call": False, "cache_hit": True},
            "errors": [],
            "warnings": [],
        }

        result = arena.build_arena_opportunity_auction(
            policy,
            scan,
            {"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            now=datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["winner"]["account_id"], "DEMO-US")
        self.assertEqual(result["winner"]["symbol"], "AAPL@XNGS")

    def test_opportunity_auction_reserves_last_primary_slot_before_cutoff_unless_exceptional(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        ordinary = self._auction_candidate(score=82)
        scan = self._auction_scan(ordinary)
        scan["accounts"][0]["entry_limits"]["primary_used"] = 1
        result = arena.build_arena_opportunity_auction(
            policy,
            scan,
            {"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            now=datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc),
        )
        base = next(item for item in result["opportunities"] if item["opportunity_type"] == "base_entry")
        self.assertEqual(base["decision"], "reserve_hold")
        self.assertEqual(result["winner"]["opportunity_type"], "hold_cash")

        exceptional = self._auction_candidate(score=90)
        scan["candidates"] = [exceptional]
        result = arena.build_arena_opportunity_auction(
            policy,
            scan,
            {"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            now=datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(result["winner"]["opportunity_type"], "base_entry")

    def test_opportunity_auction_does_not_reserve_outside_symbol_session(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        scan = self._auction_scan(self._auction_candidate(score=82, symbol="AAPL@XNGS"))
        scan["accounts"][0]["entry_limits"]["primary_used"] = 1

        result = arena.build_arena_opportunity_auction(
            policy,
            scan,
            {"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            now=datetime(2026, 6, 8, 8, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result["winner"]["opportunity_type"], "base_entry")

    def test_opportunity_auction_event_gap_without_rr_is_watch_only(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        scan = self._auction_scan()
        event = {
            "symbol": "AAPL@XNGS",
            "account_ids": ["DEMO-US"],
            "score": 92,
            "gap_pct": "8",
            "rr_after_gap": "1.0",
            "volume_confirmed": False,
            "level_hold_confirmed": False,
            "retest_confirmed": False,
        }

        result = arena.build_arena_opportunity_auction(
            policy,
            scan,
            {"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            event_candidates=[event],
            now=datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc),
        )

        event_opp = next(item for item in result["opportunities"] if item["opportunity_type"] == "event_entry")
        self.assertEqual(event_opp["decision"], "event_watch_only")
        self.assertIn("event_unclassified_manual_review", event_opp["gate_reasons"])
        self.assertIn("event_gap_overheated_without_rr", event_opp["gate_reasons"])
        self.assertEqual(result["winner"]["opportunity_type"], "hold_cash")

    def test_opportunity_auction_cache_miss_keeps_base_entry_shadow_only(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        scan = self._auction_scan(self._auction_candidate(score=92))
        scan["research"] = {"status": "skipped", "reason": "h4_research_cache_miss", "provider_call": False, "cache_hit": False}

        result = arena.build_arena_opportunity_auction(
            policy,
            scan,
            {"status": "OK", "accounts": [], "planned_actions": [], "exit_proposals": [], "replacement_proposals": []},
            now=datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc),
        )

        base = next(item for item in result["opportunities"] if item["opportunity_type"] == "base_entry")
        self.assertEqual(base["decision"], "manual_review")
        self.assertIn("research_unavailable_shadow_only", base["gate_reasons"])
        self.assertEqual(result["winner"]["opportunity_type"], "hold_cash")

    def test_opportunity_auction_stale_replacement_is_manual_review(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        candidate = self._auction_candidate(score=95)
        scan = self._auction_scan(candidate)
        review = {
            "status": "OK",
            "accounts": [],
            "planned_actions": [],
            "exit_proposals": [],
            "replacement_proposals": [
                {
                    "account_id": "DEMO-US",
                    "buy_symbol": "AAPL@XNGS",
                    "candidate_score": 95,
                    "estimated_cost_rub": "100",
                    "edge_after_cost_pct": "10",
                    "buy_candidate": candidate | {"quote_age_seconds": 120},
                }
            ],
        }

        result = arena.build_arena_opportunity_auction(
            policy,
            scan,
            review,
            now=datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc),
        )

        replacement = next(item for item in result["opportunities"] if item["opportunity_type"] == "replacement")
        self.assertEqual(replacement["decision"], "manual_review")
        self.assertIn("stale_quote_manual_review", replacement["gate_reasons"])

    def test_arena_research_risk_uses_item_verdict_before_overall_verdict(self):
        research = {
            "verdict": "RISK",
            "symbols": ["META@XNGS", "LKOH@MISX"],
            "items": [
                {"symbol": "META@XNGS", "verdict": "OK"},
                {"symbol": "LKOH@MISX", "verdict": "RISK"},
            ],
        }

        self.assertEqual(arena._arena_research_risk_symbols(research), {"LKOH@MISX"})

    def test_arena_research_risk_falls_back_to_overall_verdict_for_missing_items(self):
        research = {
            "verdict": "RISK",
            "symbols": ["META@XNGS", "LKOH@MISX"],
            "items": [{"symbol": "META@XNGS", "verdict": "OK"}],
        }

        self.assertEqual(arena._arena_research_risk_symbols(research), {"LKOH@MISX"})

    def test_learning_deprioritized_symbol_requires_manual_review_for_entry(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["learning"] = dict(policy["learning"])
        policy["learning"]["deprioritize_symbols"] = ["AAPL@XNGS"]
        policy["learning"]["deprioritized_score_penalty"] = 30
        policy["learning"]["deprioritized_entry_action"] = "manual_review"
        candidate = {
            "symbol": "AAPL@XNGS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "signal": {"h4": "up", "h1": "up", "m30": "up"},
            "entry_timeframe": "H1",
            "entry_price": "100",
            "risk_per_share": "2",
            "nearest_target_r": "2",
            "notional": "100000",
            "risk_rub": "2000",
            "liquidity_score": 10,
            "relative_strength_score": 15,
        }

        arena._apply_arena_candidate_scores([candidate], research={"verdict": "OK", "symbols": ["AAPL@XNGS"]}, policy=policy)

        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("learning_deprioritized_requires_manual_review", candidate["gate_reasons"])
        self.assertTrue(candidate["arena_growth_score"]["learning_deprioritized"])
        self.assertLess(candidate["arena_growth_score"]["score"], 75)

    def test_learning_deprioritized_position_exits_on_smaller_negative_r(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["learning"] = dict(policy["learning"])
        policy["learning"]["deprioritize_symbols"] = ["AAPL@XNGS"]
        portfolio = arena._portfolio_settings(policy)
        account = {
            "account_id": "DEMO-US",
            "stop_orders": [{"symbol": "AAPL@XNGS", "stop_price": "95"}],
            "recent_trades": [{"symbol": "AAPL@XNGS", "side": "SIDE_BUY", "time": "2026-06-08T10:00:00Z"}],
        }
        position = {"symbol": "AAPL@XNGS", "side": "LONG", "quantity": "10", "average_price": "100", "current_price": "99.4"}

        review = arena._arena_position_review(
            position,
            account=account,
            portfolio=portfolio,
            fees=arena.DEFAULT_ARENA_POLICY["fees"],
            research={"verdict": "OK", "symbols": ["AAPL@XNGS"]},
            now=datetime(2026, 6, 8, 13, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(review["learning_deprioritized"])
        self.assertEqual(review["action"], "EXIT_WEAK")
        self.assertEqual(review["reason"], "learning_deprioritized_negative_r_progress")

    def test_learning_attribution_uncertain_symbol_requires_manual_review_without_deprioritized_flag(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["learning"] = dict(policy["learning"])
        policy["learning"]["attribution_uncertain_symbols"] = ["LKOH@MISX"]
        policy["learning"]["attribution_uncertain_score_penalty"] = 15
        policy["learning"]["attribution_uncertain_entry_action"] = "manual_review"
        candidate = {
            "symbol": "LKOH@MISX",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "signal": {"h4": "up", "h1": "up", "m30": "up"},
            "entry_timeframe": "H1",
            "entry_price": "100",
            "risk_per_share": "2",
            "nearest_target_r": "2",
            "notional": "100000",
            "risk_rub": "2000",
            "liquidity_score": 10,
            "relative_strength_score": 15,
        }

        arena._apply_arena_candidate_scores([candidate], research={"verdict": "OK", "symbols": ["LKOH@MISX"]}, policy=policy)

        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("learning_attribution_uncertain_requires_manual_review", candidate["gate_reasons"])
        self.assertTrue(candidate["arena_growth_score"]["learning_attribution_uncertain"])
        self.assertNotIn("learning_deprioritized", candidate["arena_growth_score"])

    def test_learning_attribution_uncertain_position_uses_regular_weak_exit_rules(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["learning"] = dict(policy["learning"])
        policy["learning"]["attribution_uncertain_symbols"] = ["LKOH@MISX"]
        portfolio = arena._portfolio_settings(policy)
        account = {
            "account_id": "DEMO-AI",
            "stop_orders": [{"symbol": "LKOH@MISX", "stop_price": "95"}],
            "recent_trades": [{"symbol": "LKOH@MISX", "side": "SIDE_BUY", "time": "2026-06-08T10:00:00Z"}],
        }
        position = {"symbol": "LKOH@MISX", "side": "LONG", "quantity": "10", "average_price": "100", "current_price": "99.4"}

        review = arena._arena_position_review(
            position,
            account=account,
            portfolio=portfolio,
            fees=arena.DEFAULT_ARENA_POLICY["fees"],
            research={"verdict": "OK", "symbols": ["LKOH@MISX"]},
            now=datetime(2026, 6, 8, 13, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(review["learning_deprioritized"])
        self.assertTrue(review["learning_attribution_uncertain"])
        self.assertEqual(review["action"], "HOLD")
        self.assertEqual(review["reason"], "negative_r_inside_weak_exit_grace")

    def test_arena_research_avoid_uses_item_verdict_before_overall_verdict(self):
        research = {
            "verdict": "AVOID",
            "symbols": ["META@XNGS", "LKOH@MISX"],
            "items": [
                {"symbol": "META@XNGS", "verdict": "OK"},
                {"symbol": "LKOH@MISX", "verdict": "AVOID"},
            ],
        }

        self.assertEqual(arena._arena_research_avoid_symbols(research), {"LKOH@MISX"})

    def test_arena_growth_score_caps_active_technical_momentum_components(self):
        candidate = {
            "symbol": "SBER@MISX",
            "side": "BUY",
            "entry_timeframe": "H1",
            "entry_price": "100",
            "risk_per_share": "1",
            "nearest_target_r": "2",
            "risk_rub": "1",
            "notional": "100",
            "relative_strength_score": "5",
            "liquidity_score": "5",
            "costs": {"break_even_move_pct": "0"},
            "signal": {"h4": "up", "h1": "up", "m30": "up"},
        }
        base_score = arena._arena_growth_score(candidate, research={})
        enriched = arena._arena_growth_score(
            candidate
            | {
                "technical_context": {"score": 10},
                "risk_adjusted_momentum": {"score": 10},
                "news_event_context": {"score": -10},
                "relative_value_context": {"score": 10},
            },
            research={},
        )

        self.assertEqual(enriched["score"], min(100, base_score["score"] + 10))
        self.assertEqual(enriched["components"]["technical_confirmation"], 10)
        self.assertEqual(enriched["components"]["risk_adjusted_momentum"], 10)
        self.assertEqual(enriched["components"]["technical_momentum_context"], 10)
        self.assertEqual(enriched["components"]["news_event_context"], -10)
        self.assertEqual(enriched["components"]["pair_relative_value"], 10)
        self.assertIn("technical_momentum_context", enriched["active_components"])
        self.assertNotIn("technical_confirmation", enriched["report_only_components"])
        self.assertIn("news_event_context", enriched["report_only_components"])

    def test_default_policy_validates_expected_accounts(self):
        validation = arena.validate_arena_policy(arena.DEFAULT_ARENA_POLICY)

        self.assertEqual(validation["errors"], [])
        self.assertEqual(arena.DEFAULT_ARENA_POLICY["mode"], "autonomous")
        self.assertEqual(arena.DEFAULT_ARENA_POLICY["strategy"]["instruments"], "equities_long_only_arena")
        self.assertEqual(arena.DEFAULT_ARENA_POLICY["relative_value"]["mode"], "long_only_rank_modifier")
        self.assertEqual(
            [profile.account_id for profile in arena.arena_account_profiles(arena.DEFAULT_ARENA_POLICY)],
            ["DEMO-RU", "DEMO-US", "DEMO-AI"],
        )
        self.assertTrue(all(profile.trade_mode == "auto" for profile in arena.arena_account_profiles(arena.DEFAULT_ARENA_POLICY)))
        self.assertTrue(all(not profile.allow_short for profile in arena.arena_account_profiles(arena.DEFAULT_ARENA_POLICY)))

    def test_arena_policy_warns_on_short_enabled_accounts_and_rejects_non_long_only_wording(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"][1]["allow_short"] = True
        policy["strategy"]["instruments"] = "equities_plus_verified_shorts"
        policy["relative_value"]["mode"] = "long_short_pair_trade"
        policy["relative_value"]["max_score_delta"] = 25

        validation = arena.validate_arena_policy(policy)

        self.assertIn("DEMO-US: allow_short is ignored for long-only Arena; short entries remain WATCH-only", validation["warnings"])
        self.assertIn("strategy.instruments must be equities_long_only_arena", validation["errors"])
        self.assertIn("relative_value.mode must be long_only_rank_modifier", validation["errors"])
        self.assertIn("relative_value.max_score_delta must be an integer from 0 to 10", validation["errors"])

    def test_policy_validates_account_specific_score_floor_and_role_gates(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)

        validation = arena.validate_arena_policy(policy)

        self.assertEqual(validation["errors"], [])

        policy["portfolio"]["min_candidate_score_for_buy_by_account"] = {"999999": 80, "DEMO-US": 101}
        invalid = arena.validate_arena_policy(policy)

        self.assertIn("portfolio.min_candidate_score_for_buy_by_account.999999 account is not allowed", invalid["errors"])
        self.assertIn("portfolio.min_candidate_score_for_buy_by_account.DEMO-US must be an integer from 0 to 100", invalid["errors"])

    def test_demo_account_is_not_valid_arena_policy(self):
        policy = dict(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"] = [{"account_id": "DEMO-ACCOUNT", "markets": ["MISX"]}]

        validation = arena.validate_arena_policy(policy)

        self.assertIn("accounts must contain exactly three configured accounts", validation["errors"])

    def test_build_arena_status_summarizes_three_accounts_without_printing_token(self):
        class FakeArenaClient:
            def __init__(self):
                self.secret = None
                self.account_ids = []

            def create_session(self, secret):
                self.secret = secret
                return "jwt"

            def get_account(self, jwt, account_id):
                self.account_ids.append(account_id)
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "unrealized_profit": {"value": "0"},
                    "positions": [],
                }

        client = FakeArenaClient()
        report = arena.build_arena_status(arena.DEFAULT_ARENA_POLICY, client=client, env={"FINAM_ARENA_API": "secret"})

        self.assertEqual(report["status"], "OK")
        self.assertEqual(client.secret, "secret")
        self.assertEqual(client.account_ids, ["DEMO-RU", "DEMO-US", "DEMO-AI"])
        self.assertEqual([item["pnl_pct"] for item in report["accounts"]], ["0.00", "0.00", "0.00"])
        self.assertNotIn("secret", str(report))

    def test_build_arena_status_uses_arena_env_overrides(self):
        created_clients = []

        class FakeArenaClient:
            def __init__(self, *, base_url):
                self.base_url = base_url
                created_clients.append(self)

            def create_session(self, secret):
                return "jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        with mock.patch.object(arena, "FinamClient", FakeArenaClient):
            report = arena.build_arena_status(
                arena.DEFAULT_ARENA_POLICY,
                env={
                    "FINAM_ARENA_API": "secret",
                    "FINAM_ARENA_BASE_URL": "https://arena.example.test",
                    "FINAM_ARENA_APPROVAL_UNTIL": "2026-06-02T23:59:59+03:00",
                },
            )

        self.assertEqual(report["status"], "OK")
        self.assertEqual(report["base_url"], "https://arena.example.test")
        self.assertEqual(report["approval_until"], "2026-06-02T23:59:59+03:00")
        self.assertEqual(created_clients[0].base_url, "https://arena.example.test")

    def test_build_arena_status_includes_recent_trades_without_mutations(self):
        class FakeArenaClient:
            def __init__(self):
                self.trade_calls = []

            def create_session(self, secret):
                return "jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

            def trades(self, jwt, account_id, *, limit=None, start_time=None):
                self.trade_calls.append((account_id, limit, start_time))
                return {
                    "trades": [
                        {
                            "symbol": "SBER@MISX",
                            "side": "SIDE_BUY",
                            "quantity": {"value": "10"},
                            "price": {"value": "300.50"},
                            "time": "2026-05-31T10:00:00Z",
                            "trade_id": "t1",
                        }
                    ]
                }

        client = FakeArenaClient()
        report = arena.build_arena_status(
            arena.DEFAULT_ARENA_POLICY,
            client=client,
            env={"FINAM_ARENA_API": "secret"},
            now=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(report["status"], "OK")
        self.assertEqual(len(client.trade_calls), 3)
        self.assertEqual(client.trade_calls[0], ("DEMO-RU", 1000, "2026-05-24T12:00:00Z"))
        self.assertEqual(report["accounts"][0]["recent_trades"][0]["symbol"], "SBER@MISX")
        self.assertEqual(report["accounts"][0]["recent_trades"][0]["quantity"], "10")
        self.assertEqual(report["accounts"][0]["recent_trades"][0]["price"], "300.50")


    def test_arena_scan_enriches_recent_trades_from_execution_ledger(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

            def trades(self, jwt, account_id, *, limit=None, start_time=None):
                if account_id != "DEMO-US":
                    return {"trades": []}
                return {
                    "trades": [
                        {
                            "symbol": "NFLX@XNGS",
                            "side": "SIDE_SELL",
                            "price": {"value": "82.27"},
                            "time": "2026-06-04T15:37:30Z",
                            "trade_id": "broker-exit-nflx",
                        }
                    ]
                }

        ledger = [
            {
                "account_id": "DEMO-US",
                "symbol": "NFLX@XNGS",
                "side": "BUY",
                "quantity": "2984.0",
                "price": "82.43",
                "notional": "245971.120",
                "estimated_commission": "245.97112",
                "timestamp": "2026-06-04T15:07:25+00:00",
                "source": "arena_execution",
            },
            {
                "account_id": "DEMO-US",
                "symbol": "NFLX@XNGS",
                "side": "SELL",
                "quantity": "2984.0",
                "price": "82.27",
                "notional": "245493.680",
                "estimated_commission": "245.49368",
                "timestamp": "2026-06-04T15:37:30+00:00",
                "source": "arena_execution",
            },
        ]

        scan = arena.build_arena_scan(
            arena.DEFAULT_ARENA_POLICY,
            arena_client=FakeArenaClient(),
            env={"FINAM_ARENA_API": "secret"},
            now=datetime(2026, 6, 4, 18, 0, tzinfo=timezone.utc),
            execution_ledger=ledger,
        )

        account = next(item for item in scan["accounts"] if item["account_id"] == "DEMO-US")
        trade = account["recent_trades"][0]
        self.assertEqual(trade["quantity"], "2984.0")
        self.assertEqual(trade["notional"], "245493.680")
        self.assertEqual(trade["estimated_commission"], "245.49368")
        self.assertEqual(trade["realized_pnl_estimate"], "-968.90480")
        self.assertEqual(trade["source"], "broker_recent_trades+arena_execution_ledger")

    def test_arena_attribution_uses_execution_ledger_for_turnover_and_commission(self):
        status = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-US",
                    "label": "США",
                    "daily_trades": [
                        {
                            "symbol": "MSFT@XNGS",
                            "side": "SIDE_SELL",
                            "quantity": None,
                            "price": "461.00",
                            "time": "2026-06-01T19:00:23Z",
                            "trade_id": "exit-msft",
                        }
                    ],
                    "positions": [],
                }
            ],
            "errors": [],
            "warnings": [],
        }
        ledger = [
            {
                "timestamp": "2026-06-01T19:00:23+00:00",
                "account_id": "DEMO-US",
                "symbol": "MSFT@XNGS",
                "side": "SELL",
                "quantity": "540.0",
                "price": "461.00",
                "order_id": "exit-msft",
            }
        ]

        report = arena.build_arena_attribution(
            arena.DEFAULT_ARENA_POLICY,
            status,
            now=datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
            execution_ledger=ledger,
        )

        account = report["accounts"][0]
        self.assertEqual(account["sell_turnover"], "248940.000")
        self.assertEqual(account["estimated_commission"], "248.9400")
        self.assertEqual(account["incomplete_trades_count"], 0)
        self.assertEqual(account["attribution_source"], "ledger+arena_recent_trades")

    def test_arena_attribution_matches_null_quantity_trade_to_ledger_without_same_id(self):
        status = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "daily_trades": [
                        {
                            "symbol": "MTSS@MISX",
                            "side": "SIDE_SELL",
                            "quantity": None,
                            "price": "222.75",
                            "time": "2026-06-22T11:30:00Z",
                            "trade_id": "broker-only-id",
                        }
                    ],
                    "positions": [],
                }
            ],
            "errors": [],
            "warnings": [],
        }
        ledger = [
            {
                "timestamp": "2026-06-22T10:00:00+00:00",
                "account_id": "DEMO-RU",
                "symbol": "MTSS@MISX",
                "side": "BUY",
                "quantity": "1099.0",
                "price": "223.80",
                "estimated_commission": "86.08743",
                "order_id": "ledger-buy",
            },
            {
                "timestamp": "2026-06-22T11:30:00+00:00",
                "account_id": "DEMO-RU",
                "symbol": "MTSS@MISX",
                "side": "SELL",
                "quantity": "1099.0",
                "price": "222.75",
                "estimated_commission": "85.683675",
                "order_id": "ledger-sell",
                "action": "EXIT_WEAK",
            },
        ]

        report = arena.build_arena_attribution(
            arena.DEFAULT_ARENA_POLICY,
            status,
            now=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
            execution_ledger=ledger,
        )

        account = report["accounts"][0]
        self.assertEqual(account["sell_turnover"], "244802.250")
        self.assertEqual(account["estimated_commission"], "171.771105")
        self.assertEqual(account["realized_pnl_estimate"], "-1325.721105")
        self.assertEqual(account["incomplete_trades_count"], 0)
        self.assertEqual(status["accounts"][0]["daily_trades"][0]["quantity"], "1099.0")

    def test_arena_attribution_marks_recent_trades_without_quantity_incomplete(self):
        status = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-AI",
                    "label": "AI",
                    "daily_trades": [
                        {
                            "symbol": "LKOH@MISX",
                            "side": "SIDE_SELL",
                            "quantity": None,
                            "price": "4912.00",
                            "time": "2026-06-01T19:00:23Z",
                            "trade_id": "exit-lkoh",
                        }
                    ],
                    "positions": [],
                }
            ],
            "errors": [],
            "warnings": [],
        }

        report = arena.build_arena_attribution(
            arena.DEFAULT_ARENA_POLICY,
            status,
            now=datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
        )

        account = report["accounts"][0]
        self.assertEqual(account["sell_turnover"], "0")
        self.assertEqual(account["estimated_commission"], "0")
        self.assertEqual(account["incomplete_trades_count"], 1)

    def test_arena_attribution_reports_ledger_when_status_accounts_unavailable(self):
        status = {
            "status": "NO_TRADE",
            "mode": "autonomous",
            "accounts": [],
            "errors": ["Arena session failed"],
            "warnings": [],
        }
        ledger = [
            {
                "timestamp": "2026-06-01T19:00:24+00:00",
                "account_id": "DEMO-US",
                "symbol": "MSFT@XNGS",
                "side": "SELL",
                "quantity": "540.0",
                "price": "460.80",
                "order_id": "exit-msft",
            }
        ]

        report = arena.build_arena_attribution(
            arena.DEFAULT_ARENA_POLICY,
            status,
            now=datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc),
            execution_ledger=ledger,
        )

        self.assertEqual(report["status"], "DEGRADED")
        self.assertEqual(len(report["accounts"]), 1)
        self.assertEqual(report["accounts"][0]["label"], "США")
        self.assertEqual(report["accounts"][0]["sell_turnover"], "248832.000")
        self.assertEqual(report["totals"]["estimated_commission"], "248.8320")

    def test_arena_scan_is_preflight_only_until_candidate_engine_enabled(self):
        status = {
            "status": "OK",
            "mode": "approval",
            "accounts": [{"account_id": "DEMO-RU"}],
            "errors": [],
            "warnings": [],
        }

        scan = arena.arena_scan_from_status(status)

        self.assertEqual(scan["command"], "arena-scan")
        self.assertEqual(scan["candidates"], [])
        self.assertIn("candidate engine", scan["next_step"])

    def test_build_arena_scan_creates_h1_h4_candidate_without_broker_mutation(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, Decimalish=100 + index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "130"}}

            def asset_params(self, jwt, symbol, *, account_id):
                raise AssertionError("Arena short-entry must not call Trade API shortability")

        def _bar(index, *, Decimalish):
            value = str(Decimalish)
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": value},
                "high": {"value": str(Decimalish + 1)},
                "low": {"value": str(Decimalish - 1)},
                "close": {"value": value},
            }

        policy = arena.DEFAULT_ARENA_POLICY | {
            "risk": arena.DEFAULT_ARENA_POLICY["risk"] | {
                "max_position_notional_pct": "100",
                "max_symbol_exposure_pct": "100",
                "max_new_notional_pct_per_day": "100",
            },
            "accounts": [
                dict(account, universe=[account["universe"][0]])
                for account in arena.DEFAULT_ARENA_POLICY["accounts"]
            ]
        }

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        self.assertEqual(scan["status"], "OK")
        self.assertEqual(scan["command"], "arena-scan")
        self.assertEqual(len(scan["candidates"]), 3)
        candidate = scan["candidates"][0]
        self.assertEqual(candidate["account_id"], "DEMO-RU")
        self.assertEqual(candidate["side"], "BUY")
        self.assertEqual(candidate["status"], "PROPOSE_ONLY")
        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("research_unavailable_below_exceptional_score", candidate["gate_reasons"])
        self.assertEqual(candidate["costs"]["commission_pct_per_side"], "0.035")
        self.assertEqual(candidate["entry_timeframe"], "H1")
        self.assertIn("research_unavailable_below_exceptional_score", scan["accounts"][0]["top_signal"])

    def test_arena_scan_blocks_new_buy_when_account_gross_exposure_limit_reached(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "500000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "500000.00"},
                    "positions": [
                        {
                            "symbol": "SBER@MISX",
                            "quantity": {"value": "1600"},
                            "average_price": {"value": "320"},
                            "current_price": {"value": "320"},
                            "position_side": "LONG",
                        }
                    ],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, 100 + index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "130"}}

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"] = [dict(account, universe=["GAZP@MISX"]) for account in policy["accounts"]]

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            soft_stops=[
                {
                    "account_id": account["account_id"],
                    "symbol": "SBER@MISX",
                    "side": "SELL",
                    "quantity": "1600",
                    "stop_price": "300",
                    "status": "ARENA_SOFT_STOP_ACTIVE",
                }
                for account in policy["accounts"]
            ],
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        self.assertFalse(scan["candidates"][0]["execution_allowed"])
        self.assertIn("account_gross_exposure_limit_reached", scan["candidates"][0]["gate_reasons"])


    def test_portfolio_review_holds_nflx_like_micro_loss_inside_weak_exit_grace(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        now = datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)
        scan = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-US",
                    "label": "США",
                    "equity": "1000000",
                    "cash": "750000",
                    "positions": [
                        {"symbol": "NFLX@XNGS", "side": "LONG", "quantity": "2984", "average_price": "82.43", "current_price": "82.27"}
                    ],
                    "stop_orders": [{"symbol": "NFLX@XNGS", "side": "SELL", "quantity": "2984", "stop_price": "80.6129"}],
                    "daily_trades": [
                        {"symbol": "NFLX@XNGS", "side": "SIDE_BUY", "time": (now - timedelta(minutes=35)).isoformat()}
                    ],
                }
            ],
            "candidates": [],
            "research": {"items": [{"symbol": "NFLX@XNGS", "verdict": "OK"}]},
            "errors": [],
            "warnings": [],
        }

        review = arena.build_arena_portfolio_review(policy, scan, now=now)
        position_review = review["accounts"][0]["positions"][0]

        self.assertEqual(position_review["action"], "HOLD")
        self.assertEqual(position_review["reason"], "negative_r_inside_weak_exit_grace")
        self.assertEqual(position_review["position_age_minutes"], 35)
        self.assertEqual(review["exit_proposals"], [])

    def test_portfolio_review_exits_after_hold_when_negative_r_exceeds_threshold(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        now = datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)
        scan = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "equity": "1000000",
                    "cash": "750000",
                    "positions": [
                        {"symbol": "SBER@MISX", "side": "LONG", "quantity": "10", "average_price": "100", "current_price": "98"}
                    ],
                    "stop_orders": [{"symbol": "SBER@MISX", "side": "SELL", "quantity": "10", "stop_price": "92"}],
                    "daily_trades": [
                        {"symbol": "SBER@MISX", "side": "SIDE_BUY", "time": (now - timedelta(minutes=130)).isoformat()}
                    ],
                }
            ],
            "candidates": [],
            "research": {"items": [{"symbol": "SBER@MISX", "verdict": "OK"}]},
            "errors": [],
            "warnings": [],
        }

        review = arena.build_arena_portfolio_review(policy, scan, now=now)

        self.assertEqual(review["exit_proposals"][0]["action"], "EXIT_WEAK")
        self.assertEqual(review["exit_proposals"][0]["reason"], "negative_r_progress")

    def test_portfolio_review_exits_immediately_on_research_avoid(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        now = datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)
        scan = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-US",
                    "label": "США",
                    "equity": "1000000",
                    "positions": [
                        {"symbol": "NFLX@XNGS", "side": "LONG", "quantity": "2984", "average_price": "82.43", "current_price": "82.27"}
                    ],
                    "stop_orders": [{"symbol": "NFLX@XNGS", "side": "SELL", "quantity": "2984", "stop_price": "80.6129"}],
                    "daily_trades": [
                        {"symbol": "NFLX@XNGS", "side": "SIDE_BUY", "time": (now - timedelta(minutes=35)).isoformat()}
                    ],
                }
            ],
            "candidates": [],
            "research": {"items": [{"symbol": "NFLX@XNGS", "verdict": "AVOID"}]},
            "errors": [],
            "warnings": [],
        }

        review = arena.build_arena_portfolio_review(policy, scan, now=now)

        self.assertEqual(review["exit_proposals"][0]["action"], "EXIT_WEAK")
        self.assertEqual(review["exit_proposals"][0]["reason"], "research_avoid")

    def test_position_action_exits_immediately_on_low_holding_score(self):
        action, reason = arena._arena_position_action(
            Decimal("-0.01"),
            current=Decimal("99"),
            portfolio=arena.DEFAULT_ARENA_POLICY["portfolio"],
            holding_score=34,
            research_verdict="OK",
            position_age_minutes=5,
            adverse_move_pct=Decimal("0.01"),
            round_trip_cost_pct=Decimal("0.2"),
        )

        self.assertEqual(action, "EXIT_WEAK")
        self.assertEqual(reason, "holding_score_below_weak_exit_threshold")

    def test_portfolio_review_holds_negative_r_inside_cost_noise_threshold(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        now = datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)
        scan = {
            "status": "OK",
            "mode": "autonomous",
            "accounts": [
                {
                    "account_id": "DEMO-US",
                    "label": "США",
                    "equity": "1000000",
                    "positions": [
                        {"symbol": "MSFT@XNGS", "side": "LONG", "quantity": "10", "average_price": "100", "current_price": "99.95"}
                    ],
                    "stop_orders": [{"symbol": "MSFT@XNGS", "side": "SELL", "quantity": "10", "stop_price": "99.85"}],
                    "daily_trades": [
                        {"symbol": "MSFT@XNGS", "side": "SIDE_BUY", "time": (now - timedelta(minutes=130)).isoformat()}
                    ],
                }
            ],
            "candidates": [],
            "research": {"items": [{"symbol": "MSFT@XNGS", "verdict": "OK"}]},
            "errors": [],
            "warnings": [],
        }

        review = arena.build_arena_portfolio_review(policy, scan, now=now)
        position_review = review["accounts"][0]["positions"][0]

        self.assertEqual(position_review["action"], "HOLD")
        self.assertEqual(position_review["reason"], "negative_move_inside_cost_noise")
        self.assertEqual(position_review["weak_exit_policy"]["round_trip_cost_pct"], "0.2")
        self.assertEqual(review["exit_proposals"], [])

    def test_portfolio_review_proposes_exit_for_negative_r_and_uses_cost_model(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        scan = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "equity": "1000000",
                    "cash": "100000",
                    "positions": [
                        {"symbol": "SBER@MISX", "side": "LONG", "quantity": "10", "average_price": "300", "current_price": "295"}
                    ],
                    "stop_orders": [{"symbol": "SBER@MISX", "side": "SELL", "quantity": "10", "stop_price": "290"}],
                }
            ],
            "candidates": [],
            "errors": [],
            "warnings": [],
        }

        review = arena.build_arena_portfolio_review(policy, scan)

        self.assertEqual(review["status"], "OK")
        self.assertEqual(review["cost_model"]["commission_pct_per_side"], "0.035")
        self.assertEqual(review["cost_model"]["commission_pct_by_mic"]["RUSX"], "0.035")
        self.assertEqual(review["cost_model"]["commission_pct_by_mic"]["XNGS"], "0.1")
        self.assertEqual(review["exit_proposals"][0]["action"], "EXIT_WEAK")
        self.assertEqual(review["exit_proposals"][0]["confirmation_phrase"], "CONFIRM_ARENA_EXIT SBER@MISX DEMO-RU")

    def test_us_candidate_uses_us_arena_commission_grid(self):
        costs = arena._arena_trade_costs(Decimal("3000"), symbol="MSFT@XNGS", fees=arena.DEFAULT_ARENA_POLICY["fees"])

        self.assertEqual(costs["commission_pct_per_side"], "0.1")
        self.assertEqual(costs["round_trip_cost_rub"], "6.0")
        self.assertEqual(costs["break_even_move_pct"], "0.2")

    def test_us_primary_limit_blocks_ordinary_buy_but_allows_replacement_scan(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        for account in policy["accounts"]:
            account["universe"] = ["NVDA@XNGS"] if account["account_id"] == "DEMO-US" else ["SBER@MISX"]
        policy["risk"]["max_position_notional_pct"] = "100"
        policy["risk"]["max_symbol_exposure_pct"] = "100"
        policy["risk"]["max_new_notional_pct_per_day"] = "100"

        scan = arena.build_arena_scan(
            policy,
            arena_client=_ArenaDailyLimitClient(daily_us_buys=2),
            market_client=_ArenaReplacementMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            soft_stops=[
                {
                    "account_id": "DEMO-US",
                    "symbol": "MSFT@XNGS",
                    "side": "SELL",
                    "quantity": "10",
                    "stop_price": "280",
                }
            ],
            execution_ledger=[],
        )
        us_account = next(item for item in scan["accounts"] if item["account_id"] == "DEMO-US")
        us_candidates = [item for item in scan["candidates"] if item["account_id"] == "DEMO-US"]
        us_candidates[0]["gate_reasons"] = ["primary_daily_limit_reached"]
        us_candidates[0]["arena_growth_score"] = {"score": 90, "label": "HIGH", "components": {}}
        review = arena.build_arena_portfolio_review(policy, scan)
        us_review = next(item for item in review["accounts"] if item["account_id"] == "DEMO-US")

        self.assertEqual(us_account["entry_limits"]["primary_used"], 2)
        self.assertEqual(us_account["entry_limits"]["primary_limit"], 2)
        self.assertEqual(us_account["entry_limits"]["replacement_used"], 0)
        self.assertEqual(us_account["entry_limits"]["replacement_limit"], 1)
        self.assertIn("primary_daily_limit_reached", us_candidates[0]["gate_reasons"])
        self.assertFalse(us_candidates[0]["execution_allowed"])
        self.assertEqual(us_review["replacement"]["sell_symbol"], "MSFT@XNGS")
        self.assertEqual(us_review["replacement"]["buy_symbol"], "NVDA@XNGS")

    def test_us_replacement_daily_limit_blocks_replacement_proposal(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        for account in policy["accounts"]:
            account["universe"] = ["NVDA@XNGS"] if account["account_id"] == "DEMO-US" else ["SBER@MISX"]
        policy["risk"]["max_position_notional_pct"] = "100"
        policy["risk"]["max_symbol_exposure_pct"] = "100"
        policy["risk"]["max_new_notional_pct_per_day"] = "100"
        ledger = [
            {
                "timestamp": "2026-06-01T12:00:00+03:00",
                "account_id": "DEMO-US",
                "status": "EXECUTED_ARENA_REPLACEMENT",
                "action": "REPLACE",
                "source": "arena_replacement_allowance",
            }
        ]

        scan = arena.build_arena_scan(
            policy,
            arena_client=_ArenaDailyLimitClient(daily_us_buys=3),
            market_client=_ArenaReplacementMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            soft_stops=[
                {
                    "account_id": "DEMO-US",
                    "symbol": "MSFT@XNGS",
                    "side": "SELL",
                    "quantity": "10",
                    "stop_price": "280",
                }
            ],
            execution_ledger=ledger,
        )
        review = arena.build_arena_portfolio_review(policy, scan)

        us_review = next(item for item in review["accounts"] if item["account_id"] == "DEMO-US")

        self.assertIsNone(us_review["replacement"])
        self.assertEqual(us_review["replacement_block_reason"], "replacement_daily_limit_reached")

    def test_us_replacement_allowance_blocks_when_ledger_has_errors(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        for account in policy["accounts"]:
            account["universe"] = ["NVDA@XNGS"] if account["account_id"] == "DEMO-US" else ["SBER@MISX"]
        policy["risk"]["max_position_notional_pct"] = "100"
        policy["risk"]["max_symbol_exposure_pct"] = "100"
        policy["risk"]["max_new_notional_pct_per_day"] = "100"

        scan = arena.build_arena_scan(
            policy,
            arena_client=_ArenaDailyLimitClient(daily_us_buys=2),
            market_client=_ArenaReplacementMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            soft_stops=[
                {
                    "account_id": "DEMO-US",
                    "symbol": "MSFT@XNGS",
                    "side": "SELL",
                    "quantity": "10",
                    "stop_price": "280",
                }
            ],
            execution_ledger=[{"source": "arena_execution_ledger_error", "error": "json_decode"}],
        )
        review = arena.build_arena_portfolio_review(policy, scan)
        us_review = next(item for item in review["accounts"] if item["account_id"] == "DEMO-US")

        self.assertIsNone(us_review["replacement"])
        self.assertEqual(us_review["replacement_block_reason"], "replacement_ledger_unavailable")
        self.assertTrue(any("replacement_ledger_unavailable" in item for item in scan["warnings"]))

    def test_daily_operation_cap_blocks_new_candidates(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

            def trades(self, jwt, account_id, *, limit=None, start_time=None):
                return {
                    "trades": [
                        {
                            "symbol": "SBER@MISX",
                            "side": "SIDE_BUY",
                            "quantity": {"value": "1"},
                            "price": {"value": "300"},
                            "time": "2026-05-31T10:00:00Z",
                        }
                    ]
                    * 200
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, 100 + index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "130"}}

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["risk"]["max_new_trades_per_account_per_day"] = 250
        policy["risk"]["max_daily_operations_per_account"] = 199
        policy["accounts"] = [dict(account, universe=[account["universe"][0]]) for account in policy["accounts"]]

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        self.assertEqual(scan["candidates"], [])
        self.assertTrue(any("max_daily_operations_per_account reached" in item for item in scan["warnings"]))

    def test_arena_scan_enriches_open_position_current_price_from_market_quote(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [
                        {
                            "symbol": "SBER@MISX",
                            "quantity": {"value": "10"},
                            "average_price": {"value": "300"},
                            "position_side": "LONG",
                        }
                    ],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, 100 + index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "305"}}

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"] = [dict(account, universe=["GAZP@MISX"]) for account in policy["accounts"]]

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            soft_stops=[
                {
                    "account_id": account["account_id"],
                    "symbol": "SBER@MISX",
                    "side": "SELL",
                    "quantity": "10",
                    "stop_price": "290",
                    "status": "ARENA_SOFT_STOP_ACTIVE",
                }
                for account in policy["accounts"]
            ],
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        self.assertEqual(scan["accounts"][0]["positions"][0]["current_price"], "305")

    def test_arena_scan_does_not_label_blocked_short_as_actionable_top_signal(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, 130 - index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "110"}}

            def asset_params(self, jwt, symbol, *, account_id):
                raise RuntimeError("short availability unavailable")

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"] = [
            dict(account, universe=[account["universe"][0]], allow_short=True)
            for account in policy["accounts"]
        ]

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        account = next(item for item in scan["accounts"] if item["account_id"] == "DEMO-US")
        candidate = next(item for item in scan["candidates"] if item["account_id"] == "DEMO-US")
        self.assertEqual(candidate["side"], "SELL")
        self.assertEqual(candidate["status"], "WATCH")
        self.assertTrue(candidate["short_entry_forbidden"])
        self.assertFalse(candidate["execution_allowed"])
        self.assertEqual(account["status"], "WATCH")
        self.assertIn("нет исполнимого сигнала", account["top_signal"])
        self.assertIn("arena_margin_trading_not_supported", account["top_signal"])

    def test_arena_scan_blocks_short_when_arena_margin_trading_is_not_supported(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, 130 - index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "110"}}

            def asset_params(self, jwt, symbol, *, account_id):
                raise AssertionError("Arena short-entry must not call Trade API shortability")

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"] = [
            dict(account, universe=[account["universe"][0]], allow_short=True)
            for account in policy["accounts"]
        ]

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        account = next(item for item in scan["accounts"] if item["account_id"] == "DEMO-US")
        candidate = next(item for item in scan["candidates"] if item["account_id"] == "DEMO-US")
        self.assertEqual(candidate["side"], "SELL")
        self.assertEqual(candidate["status"], "WATCH")
        self.assertTrue(candidate["short_entry_forbidden"])
        self.assertFalse(candidate["execution_allowed"])
        self.assertEqual(candidate["gate_reasons"], ["arena_margin_trading_not_supported"])
        self.assertEqual(candidate["short_availability"]["source"], "arena_policy_margin_disabled")
        self.assertIsNone(candidate["relative_value_context"]["broker_payload"])
        self.assertFalse(candidate["relative_value_context"]["short_orders_allowed"])
        self.assertEqual(account["status"], "WATCH")
        self.assertIn("arena_margin_trading_not_supported", account["top_signal"])

    def test_blocked_short_does_not_consume_executable_trade_slot(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                if symbol not in {"AAPL@XNGS", "NVDA@XNGS"}:
                    return {"bars": []}
                count = 16 if interval == "TIME_FRAME_H4" else 3
                if symbol == "AAPL@XNGS":
                    return {"bars": [_bar(index, 130 - index) for index in range(count)]}
                return {"bars": [_bar(index, 200 + index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                if symbol == "AAPL@XNGS":
                    return {"last": {"value": "110"}}
                return {"last": {"value": "220"}}

            def asset_params(self, jwt, symbol, *, account_id):
                raise AssertionError("Arena short-entry must not call Trade API shortability")

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"][0]["universe"] = ["SBER@MISX"]
        policy["accounts"][1]["universe"] = ["AAPL@XNGS", "NVDA@XNGS"]
        policy["accounts"][2]["universe"] = ["SBER@MISX"]
        policy["risk"]["max_new_trades_per_account_per_run"] = 1

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        account = next(item for item in scan["accounts"] if item["account_id"] == "DEMO-US")
        account_candidates = [item for item in scan["candidates"] if item["account_id"] == "DEMO-US"]

        self.assertEqual([item["symbol"] for item in account_candidates], ["NVDA@XNGS"])
        self.assertFalse(account_candidates[0]["execution_allowed"])
        self.assertIn("research_unavailable_below_exceptional_score", account_candidates[0]["gate_reasons"])
        self.assertEqual(account_candidates[0]["side"], "BUY")
        self.assertEqual(account["status"], "WATCH")
        self.assertIn("research_unavailable_below_exceptional_score", account["top_signal"])

    def test_arena_scan_keeps_short_watch_even_when_trade_api_would_confirm_shortability(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, 130 - index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "110"}}

            def asset_params(self, jwt, symbol, *, account_id):
                return {"shortable": {"value": "AVAILABLE"}}

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"] = [
            dict(account, universe=[account["universe"][0]], allow_short=True)
            for account in policy["accounts"]
        ]

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        candidate = next(item for item in scan["candidates"] if item["account_id"] == "DEMO-US")
        self.assertEqual(candidate["side"], "SELL")
        self.assertEqual(candidate["status"], "WATCH")
        self.assertTrue(candidate["short_entry_forbidden"])
        self.assertFalse(candidate["execution_allowed"])
        self.assertEqual(candidate["gate_reasons"], ["arena_margin_trading_not_supported"])
        self.assertEqual(candidate["short_availability"]["source"], "arena_policy_margin_disabled")
        self.assertIn("arena_margin_trading_not_supported", scan["accounts"][1]["top_signal"])

    def test_arena_scan_retires_openrouter_before_provider_client_call(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, 100 + index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "130"}}

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"] = {
            "enabled": True,
            "provider": "openrouter",
            "h4_max_candidates": 1,
            "h4_max_tokens": 350,
            "finam_rss_enabled": False,
        }
        policy["risk"]["max_position_notional_pct"] = "100"
        policy["risk"]["max_symbol_exposure_pct"] = "100"
        policy["risk"]["max_new_notional_pct_per_day"] = "100"
        policy["accounts"] = [
            dict(account, universe=[account["universe"][0]])
            for account in policy["accounts"]
        ]

        original_research_candidates = arena.research_candidates
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "research-secret"}
        ), mock.patch.object(
            arena,
            "research_candidates",
            side_effect=lambda candidates, research_policy, **kwargs: original_research_candidates(
                candidates,
                research_policy,
                **kwargs,
                cache_root=Path(tmp),
            ),
        ):
            scan = arena.build_arena_scan(
                policy,
                arena_client=FakeArenaClient(),
                market_client=FakeMarketClient(),
                research_opener=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
                now=datetime(2026, 5, 31, tzinfo=timezone.utc),
            )

        self.assertEqual(scan["status"], "DEGRADED")
        self.assertEqual(scan["research"]["status"], "codex_review_required")
        self.assertEqual(scan["research"]["reason"], "expert_verdict_requires_recorded_codex_review")
        self.assertNotIn("research_unavailable_requires_manual_confirmation", scan["candidates"][0]["gate_reasons"])
        self.assertFalse(scan["candidates"][0]["execution_allowed"])
        self.assertIn("research_unavailable_below_exceptional_score", scan["candidates"][0]["gate_reasons"])

    def test_arena_scan_cache_only_research_does_not_call_provider(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_bar(index, 100 + index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "130"}}

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 1)},
                "low": {"value": str(value - 1)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"] = {"enabled": True, "h4_max_candidates": 2, "h4_model": "perplexity/sonar"}
        policy["risk"]["max_position_notional_pct"] = "100"
        policy["risk"]["max_symbol_exposure_pct"] = "100"
        policy["risk"]["max_new_notional_pct_per_day"] = "100"
        policy["accounts"] = [dict(account, universe=[account["universe"][0]]) for account in policy["accounts"]]

        original_research_candidates = arena.research_candidates
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "research-secret"}
        ), mock.patch.object(
            arena,
            "research_candidates",
            side_effect=lambda candidates, research_policy, **kwargs: original_research_candidates(
                candidates,
                research_policy,
                **kwargs,
                cache_root=Path(tmp),
            ),
        ):
            scan = arena.build_arena_scan(
                policy,
                arena_client=FakeArenaClient(),
                market_client=FakeMarketClient(),
                research_opener=mock.Mock(side_effect=AssertionError("provider should not be called")),
                research_mode="cache_only",
                env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
                now=datetime(2026, 5, 31, tzinfo=timezone.utc),
            )

        self.assertEqual(scan["status"], "OK")
        self.assertEqual(scan["research"]["reason"], "h4_research_cache_miss")
        self.assertFalse(scan["candidates"][0]["execution_allowed"])
        self.assertIn("research_unavailable_below_exceptional_score", scan["candidates"][0]["gate_reasons"])

    def test_arena_research_candidates_prefer_executable_candidates(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"]["h4_max_candidates"] = 2
        candidates = [
            {"symbol": "SHORT@MISX", "side": "SELL", "execution_allowed": False, "gate_reasons": ["arena_margin_trading_not_supported"]},
            {"symbol": "LONG1@MISX", "execution_allowed": True},
            {"symbol": "LONG2@MISX", "execution_allowed": True},
        ]

        selected = arena._arena_research_candidates(candidates, policy)

        self.assertEqual([item["symbol"] for item in selected], ["LONG1@MISX", "LONG2@MISX"])

    def test_arena_executable_research_candidates_caps_per_account_and_deduplicates_symbols(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"]["arena_h4_max_symbols_per_scan"] = 3
        policy["research"]["arena_h4_max_symbols_per_account"] = 1
        candidates = [
            {"account_id": "DEMO-RU", "symbol": "AAPL@XNGS", "execution_allowed": True},
            {"account_id": "DEMO-RU", "symbol": "NVDA@XNGS", "execution_allowed": True},
            {"account_id": "DEMO-US", "symbol": "AAPL@XNGS", "execution_allowed": True},
            {"account_id": "DEMO-US", "symbol": "MSFT@XNGS", "execution_allowed": True},
            {"account_id": "DEMO-AI", "symbol": "LKOH@MISX", "execution_allowed": True},
        ]

        selected = arena._arena_executable_research_candidates(candidates, policy)

        self.assertEqual(
            [(item["account_id"], item["symbol"]) for item in selected],
            [("DEMO-RU", "AAPL@XNGS"), ("DEMO-US", "MSFT@XNGS"), ("DEMO-AI", "LKOH@MISX")],
        )

    def test_arena_research_candidates_skip_margin_disabled_shorts_when_no_executable(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"]["h4_max_candidates"] = 2
        candidates = [
            {
                "symbol": "AAPL@XNGS",
                "side": "SELL",
                "execution_allowed": False,
                "gate_reasons": ["arena_margin_trading_not_supported"],
            },
            {
                "symbol": "SBER@MISX",
                "side": "SELL",
                "execution_allowed": False,
                "gate_reasons": ["arena_margin_trading_not_supported"],
            },
        ]

        selected = arena._arena_research_candidates(candidates, policy)

        self.assertEqual(selected, [])

    def test_arena_budgeted_research_skips_provider_without_executable_candidates(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy['research']['enabled'] = True
        candidates = [
            {
                'symbol': 'AAPL@XNGS',
                'side': 'BUY',
                'execution_allowed': False,
                'gate_reasons': ['primary_daily_limit_reached'],
            }
        ]
        warnings = []

        with mock.patch.object(arena, 'research_candidates', side_effect=AssertionError('provider should not be called')):
            result = arena._arena_research_gate(
                candidates,
                policy,
                urlopen=None,
                warnings=warnings,
                research_mode='budgeted',
            )

        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['reason'], 'research_skipped_budgeted_no_executable_candidates')
        self.assertEqual(warnings, [])

    def test_arena_budgeted_research_calls_provider_for_executable_candidates(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy['research']['enabled'] = True
        candidates = [
            {'symbol': 'AAPL@XNGS', 'side': 'BUY', 'execution_allowed': False},
            {'symbol': 'MSFT@XNGS', 'side': 'BUY', 'execution_allowed': True},
        ]
        warnings = []

        with mock.patch.object(
            arena,
            'research_candidates',
            return_value={'status': 'ok', 'verdict': 'OK', 'items': [], 'symbols': ['MSFT@XNGS']},
        ) as research:
            result = arena._arena_research_gate(
                candidates,
                policy,
                urlopen=None,
                warnings=warnings,
                research_mode='budgeted',
            )

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(research.call_args.args[0], [{'symbol': 'MSFT@XNGS', 'side': 'BUY', 'execution_allowed': True}])

    def test_arena_budgeted_research_calls_provider_for_near_executable_candidates(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy['research']['enabled'] = True
        policy['research']['arena_h4_max_symbols_per_scan'] = 3
        policy['portfolio']['min_candidate_score_for_buy'] = 75
        candidates = [
            {
                'account_id': 'DEMO-RU',
                'symbol': 'SBER@MISX',
                'side': 'BUY',
                'execution_allowed': False,
                'gate_reasons': ['research_unavailable_below_exceptional_score', 'candidate_score_below_min'],
                'arena_growth_score': {'score': 70},
            },
            {
                'account_id': 'DEMO-US',
                'symbol': 'MSFT@XNGS',
                'side': 'BUY',
                'execution_allowed': False,
                'gate_reasons': ['primary_daily_limit_reached'],
                'arena_growth_score': {'score': 90},
            },
        ]
        warnings = []

        with mock.patch.object(
            arena,
            'research_candidates',
            return_value={'status': 'codex_review_required', 'verdict': 'UNAVAILABLE', 'symbols': ['SBER@MISX']},
        ) as research:
            result = arena._arena_research_gate(
                candidates,
                policy,
                urlopen=None,
                warnings=warnings,
                research_mode='budgeted',
            )

        self.assertEqual(result['status'], 'codex_review_required')
        self.assertEqual(research.call_args.args[0], [candidates[0]])

    def test_repeat_pattern_loss_cooldown_blocks_before_research_provider_call(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"]["enabled"] = True
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "META@XNGS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 90, "label": "HIGH", "components": {}},
        }
        ledger = [
            {
                "timestamp": "2026-06-09T13:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "AAPL@XNGS",
                "side": "BUY",
                "quantity": "10",
                "price": "100",
                "estimated_commission": "1",
            },
            {
                "timestamp": "2026-06-09T15:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "AAPL@XNGS",
                "side": "SELL",
                "quantity": "10",
                "price": "98",
                "estimated_commission": "1",
                "action": "EXIT_WEAK",
            },
        ]

        repeat = arena._apply_repeat_pattern_loss_guard(
            [candidate],
            policy,
            execution_ledger=ledger,
            now=datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc),
        )
        with mock.patch.object(arena, "research_candidates", side_effect=AssertionError("provider should not be called")):
            research = arena._arena_research_gate([candidate], policy, urlopen=None, warnings=[], research_mode="budgeted")

        self.assertEqual(repeat["blocked"], 1)
        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("repeat_pattern_loss_cooldown", candidate["gate_reasons"])
        self.assertEqual(research["reason"], "research_skipped_budgeted_no_executable_candidates")

    def test_repeat_pattern_loss_penalty_can_block_after_cooldown(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["min_candidate_score_for_buy"] = 75
        policy["learning"]["repeat_pattern_loss"]["penalty_trading_days"] = 5
        policy["learning"]["repeat_pattern_loss"]["score_penalty"] = 25
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "META@XNGS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 90, "label": "HIGH", "components": {}},
        }
        ledger = [
            {
                "timestamp": "2026-06-09T13:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "AAPL@XNGS",
                "side": "BUY",
                "quantity": "10",
                "price": "100",
                "estimated_commission": "1",
            },
            {
                "timestamp": "2026-06-09T15:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "AAPL@XNGS",
                "side": "SELL",
                "quantity": "10",
                "price": "98",
                "estimated_commission": "1",
                "action": "EXIT_WEAK",
            },
        ]

        repeat = arena._apply_repeat_pattern_loss_guard(
            [candidate],
            policy,
            execution_ledger=ledger,
            now=datetime(2026, 6, 11, 16, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(repeat["penalized"], 1)
        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("repeat_pattern_loss_score_below_min", candidate["gate_reasons"])
        self.assertEqual(candidate["arena_growth_score"]["components"]["repeat_pattern_loss"], -25)

    def test_repeat_pattern_loss_scope_catches_us_xnys_symbol(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["learning"]["repeat_pattern_loss"]["scopes"] = [
            {"name": "DEMO-US_us", "account_ids": ["DEMO-US"], "markets": ["XNGS", "XNYS"], "side": "BUY"}
        ]
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "V@XNYS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 90, "label": "HIGH", "components": {}},
        }
        ledger = [
            {
                "timestamp": "2026-06-10T16:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "UNH@XNYS",
                "side": "BUY",
                "quantity": "10",
                "price": "100",
                "estimated_commission": "1",
            },
            {
                "timestamp": "2026-06-10T18:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "UNH@XNYS",
                "side": "SELL",
                "quantity": "10",
                "price": "98",
                "estimated_commission": "1",
                "action": "EXIT_WEAK",
            },
        ]

        repeat = arena._apply_repeat_pattern_loss_guard(
            [candidate],
            policy,
            execution_ledger=ledger,
            now=datetime(2026, 6, 10, 19, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(repeat["blocked"], 1)
        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("repeat_pattern_loss_cooldown", candidate["gate_reasons"])
        self.assertEqual(candidate["repeat_pattern_loss"]["source_symbol"], "UNH@XNYS")

    def test_repeat_pattern_symbol_then_cluster_cooldown_only_blocks_same_symbol(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["learning"]["repeat_pattern_loss"]["scopes"] = [
            {
                "name": "DEMO-US_us",
                "account_ids": ["DEMO-US"],
                "markets": ["XNGS"],
                "side": "BUY",
                "match_mode": "symbol_then_cluster",
                "cluster_penalty_max_pre_score": 90,
            }
        ]
        same_symbol = {
            "account_id": "DEMO-US",
            "symbol": "META@XNGS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 95, "label": "HIGH", "components": {}},
        }
        strong_cluster = {
            "account_id": "DEMO-US",
            "symbol": "AAPL@XNGS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 95, "label": "HIGH", "components": {}},
        }
        ledger = [
            {
                "timestamp": "2026-06-09T13:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "META@XNGS",
                "side": "BUY",
                "quantity": "10",
                "price": "100",
                "estimated_commission": "1",
            },
            {
                "timestamp": "2026-06-09T15:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "META@XNGS",
                "side": "SELL",
                "quantity": "10",
                "price": "98",
                "estimated_commission": "1",
                "action": "EXIT_WEAK",
            },
        ]

        repeat = arena._apply_repeat_pattern_loss_guard(
            [same_symbol, strong_cluster],
            policy,
            execution_ledger=ledger,
            now=datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(repeat["blocked"], 1)
        self.assertEqual(repeat["skipped_strong_cluster"], 1)
        self.assertFalse(same_symbol["execution_allowed"])
        self.assertIn("repeat_pattern_loss_cooldown", same_symbol["gate_reasons"])
        self.assertTrue(strong_cluster["execution_allowed"])
        self.assertNotIn("repeat_pattern_loss", strong_cluster)

    def test_repeat_pattern_cluster_threshold_zero_disables_cross_symbol_penalty(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["min_candidate_score_for_buy"] = 75
        policy["learning"]["repeat_pattern_loss"]["scopes"] = [
            {
                "name": "DEMO-RU_misx",
                "account_ids": ["DEMO-RU"],
                "markets": ["MISX"],
                "side": "BUY",
                "match_mode": "symbol_then_cluster",
                "cluster_penalty_max_pre_score": 0,
            }
        ]
        candidate = {
            "account_id": "DEMO-RU",
            "symbol": "SBER@MISX",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 65, "label": "MEDIUM", "components": {}},
        }
        ledger = [
            {
                "timestamp": "2026-06-17T12:00:00+00:00",
                "account_id": "DEMO-RU",
                "symbol": "GAZP@MISX",
                "side": "BUY",
                "quantity": "10",
                "price": "100",
                "estimated_commission": "1",
            },
            {
                "timestamp": "2026-06-17T15:00:00+00:00",
                "account_id": "DEMO-RU",
                "symbol": "GAZP@MISX",
                "side": "SELL",
                "quantity": "10",
                "price": "98",
                "estimated_commission": "1",
                "action": "EXIT_WEAK",
            },
        ]

        repeat = arena._apply_repeat_pattern_loss_guard(
            [candidate],
            policy,
            execution_ledger=ledger,
            now=datetime(2026, 6, 19, 18, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(repeat["penalized"], 0)
        self.assertEqual(repeat["skipped_strong_cluster"], 1)
        self.assertTrue(candidate["execution_allowed"])
        self.assertNotIn("repeat_pattern_loss", candidate)

    def test_repeat_pattern_symbol_then_cluster_can_penalize_weak_cluster_candidate(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["min_candidate_score_for_buy"] = 75
        policy["learning"]["repeat_pattern_loss"]["score_penalty"] = 25
        policy["learning"]["repeat_pattern_loss"]["scopes"] = [
            {
                "name": "DEMO-US_us",
                "account_ids": ["DEMO-US"],
                "markets": ["XNGS"],
                "side": "BUY",
                "match_mode": "symbol_then_cluster",
                "cluster_penalty_max_pre_score": 90,
            }
        ]
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "AAPL@XNGS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 80, "label": "HIGH", "components": {}},
        }
        ledger = [
            {
                "timestamp": "2026-06-09T13:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "META@XNGS",
                "side": "BUY",
                "quantity": "10",
                "price": "100",
                "estimated_commission": "1",
            },
            {
                "timestamp": "2026-06-09T15:00:00+00:00",
                "account_id": "DEMO-US",
                "symbol": "META@XNGS",
                "side": "SELL",
                "quantity": "10",
                "price": "98",
                "estimated_commission": "1",
                "action": "EXIT_WEAK",
            },
        ]

        repeat = arena._apply_repeat_pattern_loss_guard(
            [candidate],
            policy,
            execution_ledger=ledger,
            now=datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(repeat["penalized"], 1)
        self.assertFalse(candidate["execution_allowed"])
        self.assertEqual(candidate["repeat_pattern_loss"]["action"], "penalty")
        self.assertEqual(candidate["repeat_pattern_loss"]["match_mode"], "cluster")
        self.assertIn("repeat_pattern_loss_score_below_min", candidate["gate_reasons"])

    def test_account_specific_score_floor_blocks_us_candidate_below_80(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["min_candidate_score_for_buy_by_account"] = {"DEMO-US": 80}
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "V@XNYS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "entry_timeframe": "H1",
            "nearest_target_r": "2",
            "risk_per_share": "10",
            "entry_price": "100",
            "relative_strength_score": 0,
            "liquidity_score": 10,
            "costs": {"break_even_move_pct": "0.20"},
            "signal": {"h4": "up", "h1": "up", "m30": "up"},
        }

        arena._apply_arena_candidate_scores([candidate], research={"verdict": "OK", "symbols": ["V@XNYS"]}, policy=policy)

        self.assertLess(candidate["arena_growth_score"]["score"], 80)
        self.assertIn("candidate_score_below_min", candidate["gate_reasons"])

    def test_exceptional_entry_override_allows_clean_178_daily_notional_limit(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        account = {
            "account_id": "DEMO-US",
            "equity": "1000000",
            "entry_limits": {"primary_used": 1, "primary_limit": 2},
            "daily_trades": [
                {
                    "symbol": "AAPL@XNGS",
                    "side": "SIDE_BUY",
                    "quantity": "2500",
                    "price": "100",
                    "time": "2026-07-06T12:00:00Z",
                }
            ],
        }
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "BA@XNYS",
            "side": "BUY",
            "execution_allowed": False,
            "gate_reasons": ["daily_new_notional_limit_exceeded"],
            "notional": "250000",
            "arena_growth_score": {"score": 86, "label": "HIGH", "components": {}, "research_verdict": "UNAVAILABLE"},
        }

        arena._apply_exceptional_entry_limit_overrides(
            [candidate],
            accounts_by_id={"DEMO-US": account},
            policy=policy,
            now=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(candidate["execution_allowed"])
        self.assertEqual(candidate["gate_reasons"], [])
        self.assertEqual(candidate["exceptional_entry_limit_override"]["removed_gates"], ["daily_new_notional_limit_exceeded"])

    def test_exceptional_entry_override_allows_179_only_with_research_ok_at_85(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        account = {
            "account_id": "DEMO-AI",
            "equity": "1000000",
            "entry_limits": {"primary_used": 1, "primary_limit": 1},
            "daily_trades": [],
        }
        unavailable = {
            "account_id": "DEMO-AI",
            "symbol": "ISRG@XNGS",
            "side": "BUY",
            "execution_allowed": False,
            "gate_reasons": ["primary_daily_limit_reached"],
            "notional": "250000",
            "arena_growth_score": {"score": 85, "label": "HIGH", "components": {}, "research_verdict": "UNAVAILABLE"},
        }
        research_ok = deepcopy(unavailable)
        research_ok["arena_growth_score"]["research_verdict"] = "OK"

        arena._apply_exceptional_entry_limit_overrides(
            [unavailable, research_ok],
            accounts_by_id={"DEMO-AI": account},
            policy=policy,
            now=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(unavailable["execution_allowed"])
        self.assertIn("primary_daily_limit_reached", unavailable["gate_reasons"])
        self.assertTrue(research_ok["execution_allowed"])
        self.assertEqual(research_ok["gate_reasons"], [])

    def test_exceptional_entry_override_does_not_bypass_same_symbol_or_concentration(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        account = {
            "account_id": "DEMO-AI",
            "equity": "1000000",
            "entry_limits": {"primary_used": 1, "primary_limit": 1},
            "daily_trades": [],
        }
        candidate = {
            "account_id": "DEMO-AI",
            "symbol": "ISRG@XNGS",
            "side": "BUY",
            "execution_allowed": False,
            "gate_reasons": ["primary_daily_limit_reached", "same_symbol_position_open"],
            "notional": "250000",
            "arena_growth_score": {"score": 89, "label": "HIGH", "components": {}, "research_verdict": "UNAVAILABLE"},
        }

        arena._apply_exceptional_entry_limit_overrides(
            [candidate],
            accounts_by_id={"DEMO-AI": account},
            policy=policy,
            now=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("primary_daily_limit_reached", candidate["gate_reasons"])
        self.assertIn("same_symbol_position_open", candidate["gate_reasons"])

    def test_mcp_confirmed_entry_allows_reduced_risk_score_79_to_83(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        account = {
            "account_id": "DEMO-US",
            "equity": "1000000",
            "cash": "900000",
            "entry_limits": {"primary_used": 1, "primary_limit": 1},
            "daily_trades": [
                {
                    "symbol": "AAPL@XNGS",
                    "side": "SIDE_BUY",
                    "quantity": "2500",
                    "price": "100",
                    "time": "2026-07-06T12:00:00Z",
                }
            ],
        }
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "BA@XNYS",
            "side": "BUY",
            "execution_allowed": False,
            "gate_reasons": ["candidate_score_below_min", "primary_daily_limit_reached", "daily_new_notional_limit_exceeded"],
            "entry_price": "100",
            "quantity": "2500",
            "notional": "250000",
            "risk_per_share": "10",
            "risk_rub": "25000",
            "arena_growth_score": {"score": 81, "label": "MEDIUM", "components": {}, "research_verdict": "OK"},
        }
        mcp_shadow = {
            "status": "OK",
            "matched_arena_accounts": [{"account_id": "DEMO-US"}],
            "portfolio_warnings": [],
            "candidate_context_notes": [
                {
                    "account_id": "DEMO-US",
                    "symbol": "BA@XNYS",
                    "already_exposed": False,
                    "new_exposure": True,
                    "watchlist_context": "in_watchlist",
                    "quote_context": "available",
                }
            ],
        }

        arena._apply_mcp_confirmed_entry_overrides(
            [candidate],
            accounts_by_id={"DEMO-US": account},
            policy=policy,
            mcp_shadow=mcp_shadow,
            now=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(candidate["execution_allowed"])
        self.assertEqual(candidate["gate_reasons"], [])
        self.assertEqual(candidate["quantity"], "350")
        self.assertEqual(candidate["notional"], "35000")
        self.assertEqual(candidate["risk_rub"], "3500")
        self.assertEqual(
            candidate["mcp_confirmed_entry"]["removed_gates"],
            ["candidate_score_below_min", "daily_new_notional_limit_exceeded", "primary_daily_limit_reached"],
        )
        self.assertEqual(candidate["mcp_confirmed_entry"]["portfolio_context"]["watchlist_context"], "in_watchlist")

    def test_mcp_confirmed_entry_fails_closed_on_forbidden_gate_or_position_diff(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        account = {
            "account_id": "DEMO-US",
            "equity": "1000000",
            "cash": "900000",
            "entry_limits": {"primary_used": 1, "primary_limit": 1},
            "daily_trades": [],
        }
        forbidden = {
            "account_id": "DEMO-US",
            "symbol": "BA@XNYS",
            "side": "BUY",
            "execution_allowed": False,
            "gate_reasons": ["candidate_score_below_min", "same_symbol_position_open"],
            "entry_price": "100",
            "quantity": "2500",
            "notional": "250000",
            "risk_per_share": "10",
            "arena_growth_score": {"score": 81, "label": "MEDIUM", "components": {}, "research_verdict": "OK"},
        }
        diff = deepcopy(forbidden)
        diff["gate_reasons"] = ["candidate_score_below_min"]
        mcp_shadow = {
            "status": "OK",
            "matched_arena_accounts": [{"account_id": "DEMO-US"}],
            "portfolio_warnings": [{"type": "position_set_diff", "account_id": "DEMO-US"}],
            "candidate_context_notes": [
                {"account_id": "DEMO-US", "symbol": "BA@XNYS", "already_exposed": False, "new_exposure": True, "quote_context": "available"}
            ],
        }

        arena._apply_mcp_confirmed_entry_overrides(
            [forbidden, diff],
            accounts_by_id={"DEMO-US": account},
            policy=policy,
            mcp_shadow=mcp_shadow,
            now=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(forbidden["execution_allowed"])
        self.assertNotIn("mcp_confirmed_entry", forbidden)
        self.assertFalse(diff["execution_allowed"])
        self.assertEqual(diff["mcp_confirmed_entry"]["reason"], "mcp_position_set_diff")

    def test_mcp_confirmed_entry_keeps_candidate_blocked_when_mcp_unavailable(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        account = {
            "account_id": "DEMO-US",
            "equity": "1000000",
            "cash": "900000",
            "entry_limits": {"primary_used": 1, "primary_limit": 1},
            "daily_trades": [],
        }
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "BA@XNYS",
            "side": "BUY",
            "execution_allowed": False,
            "gate_reasons": ["candidate_score_below_min"],
            "entry_price": "100",
            "quantity": "2500",
            "notional": "250000",
            "risk_per_share": "10",
            "arena_growth_score": {"score": 81, "label": "MEDIUM", "components": {}, "research_verdict": "OK"},
        }

        arena._apply_mcp_confirmed_entry_overrides(
            [candidate],
            accounts_by_id={"DEMO-US": account},
            policy=policy,
            mcp_shadow={"status": "MCP_UNAVAILABLE", "candidate_context_notes": []},
            now=datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(candidate["execution_allowed"])
        self.assertEqual(candidate["gate_reasons"], ["candidate_score_below_min"])
        self.assertEqual(candidate["mcp_confirmed_entry"]["reason"], "mcp_unavailable")

    def test_arena_trade_proposal_includes_mcp_confirmed_entry_context(self):
        proposal = arena.arena_trade_proposal(
            {
                "account_id": "DEMO-US",
                "symbol": "BA@XNYS",
                "side": "BUY",
                "quantity": "350",
                "entry_price": "100.00",
                "stop_price": "90.00",
                "take_profit_price": "120.00",
                "entry_timeframe": "H1",
                "execution_allowed": True,
                "mcp_confirmed_entry": {"applied": True, "mode": "mcp_confirmed_reduced_risk_entry"},
            },
            account={"account_id": "DEMO-US"},
        )

        self.assertEqual(proposal["mcp_confirmed_entry"]["mode"], "mcp_confirmed_reduced_risk_entry")
        self.assertEqual(proposal["quantity"], 350)

    def test_arena_buy_blocks_research_unavailable_below_exceptional_score(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["min_candidate_score_for_buy"] = 75
        candidate = {
            "account_id": "DEMO-RU",
            "symbol": "SBER@MISX",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "entry_timeframe": "H1",
            "nearest_target_r": "2",
            "risk_per_share": "10",
            "entry_price": "100",
            "relative_strength_score": 4,
            "liquidity_score": 10,
            "costs": {"break_even_move_pct": "0.070"},
            "signal": {"h4": "up", "h1": "up", "m30": "up"},
        }

        arena._apply_arena_candidate_scores([candidate], research={}, policy=policy)

        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("research_unavailable_below_exceptional_score", candidate["gate_reasons"])

    def test_arena_buy_requires_relative_strength_or_m30_confirmation(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["min_candidate_score_for_buy"] = 75
        candidate = {
            "account_id": "DEMO-AI",
            "symbol": "LKOH@MISX",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "entry_timeframe": "H1",
            "nearest_target_r": "2",
            "risk_per_share": "10",
            "entry_price": "100",
            "relative_strength_score": 0,
            "liquidity_score": 10,
            "costs": {"break_even_move_pct": "0.070"},
            "signal": {"h4": "up", "h1": "up", "m30": "unavailable"},
        }

        arena._apply_arena_candidate_scores([candidate], research={"verdict": "OK", "symbols": ["LKOH@MISX"]}, policy=policy)

        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("entry_strength_confirmation_missing", candidate["gate_reasons"])

    def test_arena_medium_buy_is_blocked_by_high_score_floor(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["min_candidate_score_for_buy"] = 75
        candidate = {
            "account_id": "DEMO-RU",
            "symbol": "SBER@MISX",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 65, "label": "MEDIUM", "components": {}},
        }

        arena._apply_arena_candidate_scores([candidate], research={"verdict": "OK", "symbols": ["SBER@MISX"]}, policy=policy)

        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("candidate_score_below_min", candidate["gate_reasons"])

    def test_cross_market_role_gate_blocks_third_misx_buy_on_ai_account(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["account_role_gates"] = {
            "DEMO-AI": {
                "cross_market_concentration": {
                    "enabled": True,
                    "market": "MISX",
                    "max_positions": 2,
                    "manual_override": False,
                }
            }
        }
        account = {
            "account_id": "DEMO-AI",
            "equity": "1000000",
            "positions": [
                {"symbol": "LKOH@MISX", "quantity": "50", "average_price": "4700", "current_price": "4700"},
                {"symbol": "PLZL@MISX", "quantity": "120", "average_price": "1950", "current_price": "1950"},
            ],
        }
        candidate = {
            "account_id": "DEMO-AI",
            "symbol": "SBER@MISX",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 82, "label": "HIGH", "components": {}},
        }

        arena._apply_arena_account_role_gates([candidate], accounts_by_id={"DEMO-AI": account}, policy=policy)

        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("cross_market_role_misx_concentration", candidate["gate_reasons"])

    def test_single_symbol_anchor_gate_allows_confirmed_rotation_proposal(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["portfolio"]["account_role_gates"] = {
            "DEMO-RU": {
                "single_symbol_anchor_rotation": {
                    "enabled": True,
                    "market": "MISX",
                    "anchor_notional_pct": "45.0",
                    "required_progress_r": "1.0",
                    "candidate_min_score": 80,
                    "min_score_delta": 15,
                }
            }
        }
        account = {
            "account_id": "DEMO-RU",
            "label": "РФ",
            "equity": "1000000",
            "cash": "500000",
            "entry_limits": {"replacement_used": 0, "replacement_limit": 1, "replacement_ledger_available": True},
            "positions": [
                {
                    "symbol": "SBER@MISX",
                    "side": "LONG",
                    "quantity": "1553",
                    "average_price": "322.22",
                    "current_price": "322.30",
                }
            ],
            "stop_orders": [],
        }
        candidate = {
            "account_id": "DEMO-RU",
            "symbol": "PLZL@MISX",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "notional": "240000",
            "arena_growth_score": {"score": 82, "label": "HIGH", "components": {}},
        }

        arena._apply_arena_account_role_gates([candidate], accounts_by_id={"DEMO-RU": account}, policy=policy)
        review = arena.build_arena_portfolio_review(
            policy,
            {
                "status": "OK",
                "mode": "autonomous",
                "accounts": [account],
                "candidates": [candidate],
                "research": {"status": "skipped"},
                "errors": [],
                "warnings": [],
            },
            now=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        )

        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("single_symbol_anchor_rotation_candidate", candidate["gate_reasons"])
        self.assertEqual(candidate["anchor_rotation"]["suggested_partial_sell_quantity"], "157.0")
        self.assertEqual(review["replacement_proposals"][0]["sell_symbol"], "SBER@MISX")
        self.assertEqual(review["replacement_proposals"][0]["buy_symbol"], "PLZL@MISX")
        self.assertFalse(review["replacement_proposals"][0]["broker_mutation"])

    def test_pretrade_budget_exhaustion_blocks_autonomous_us_entry(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"]["enabled"] = True
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "META@XNGS",
            "side": "BUY",
            "execution_allowed": True,
            "gate_reasons": [],
            "arena_growth_score": {"score": 90, "label": "HIGH", "components": {}},
        }

        with mock.patch.object(
            arena,
            "pretrade_check",
            return_value={
                "status": "failed",
                "reason": "openrouter_daily_budget_exhausted",
                "verdict": "UNAVAILABLE",
                "provider_call": False,
                "budget_period": "2026-06-09",
                "budget_limit": 4,
                "budget_used": 4,
                "budget_remaining": 0,
                "budget_spent_symbols": ["NVDA@XNGS", "META@XNGS"],
            },
        ):
            result = arena._arena_pretrade_event_gate(
                [candidate],
                policy,
                urlopen=None,
                warnings=[],
                research_mode="budgeted",
                now=datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["provider_call"])
        self.assertEqual(result["items"][0]["budget"]["limit"], 4)
        self.assertEqual(result["items"][0]["budget"]["spent_symbols"], ["NVDA@XNGS", "META@XNGS"])
        self.assertFalse(candidate["execution_allowed"])
        self.assertIn("pretrade_budget_exhausted_blocks_autonomy", candidate["gate_reasons"])

    def test_pretrade_event_gate_reuses_same_symbol_result_within_scan(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"]["enabled"] = True
        policy["research"]["pretrade_account_ids"] = ["DEMO-US", "DEMO-EXTRA"]
        candidates = [
            {
                "account_id": "DEMO-US",
                "symbol": "NVDA@XNGS",
                "side": "BUY",
                "execution_allowed": True,
                "gate_reasons": [],
                "arena_growth_score": {"score": 91, "label": "HIGH", "components": {}},
            },
            {
                "account_id": "DEMO-EXTRA",
                "symbol": "NVDA@XNGS",
                "side": "BUY",
                "execution_allowed": True,
                "gate_reasons": [],
                "arena_growth_score": {"score": 90, "label": "HIGH", "components": {}},
            },
        ]

        with mock.patch.object(
            arena,
            "pretrade_check",
            return_value={
                "status": "ok",
                "verdict": "OK",
                "provider_call": True,
            },
        ) as pretrade:
            result = arena._arena_pretrade_event_gate(
                candidates,
                policy,
                urlopen=None,
                warnings=[],
                research_mode="budgeted",
                now=datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["items"]), 2)
        pretrade.assert_called_once_with("NVDA@XNGS", policy, urlopen=None, now=datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc))
        self.assertTrue(all(item["execution_allowed"] for item in candidates))

    def test_default_pretrade_daily_budget_is_four(self):
        self.assertEqual(arena.DEFAULT_ARENA_POLICY["research"]["pretrade_daily_request_budget"], 4)

    def test_pretrade_skips_already_blocked_candidate_without_provider_call(self):
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["research"]["enabled"] = True
        candidate = {
            "account_id": "DEMO-US",
            "symbol": "META@XNGS",
            "side": "BUY",
            "execution_allowed": False,
            "gate_reasons": ["repeat_pattern_loss_cooldown"],
            "arena_growth_score": {"score": 90, "label": "HIGH", "components": {}},
        }

        with mock.patch.object(arena, "pretrade_check", side_effect=AssertionError("pretrade should not be called")):
            result = arena._arena_pretrade_event_gate(
                [candidate],
                policy,
                urlopen=None,
                warnings=[],
                research_mode="budgeted",
                now=datetime(2026, 6, 9, 18, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_unblocked_us_buy_candidate")

    def test_arena_scan_uses_m30_entry_when_h1_is_not_aligned(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                if interval == "TIME_FRAME_H4":
                    values = list(range(100, 116))
                elif interval == "TIME_FRAME_H1":
                    values = [120, 120]
                else:
                    values = [119, 121]
                return {"bars": [_bar(index, value) for index, value in enumerate(values)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "121"}}

        def _bar(index, value):
            return {
                "time": f"2026-05-30T{index:02d}:00:00Z",
                "open": {"value": str(value)},
                "high": {"value": str(value + 2)},
                "low": {"value": str(value - 2)},
                "close": {"value": str(value)},
            }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"] = [
            dict(account, universe=[account["universe"][0]])
            for account in policy["accounts"]
        ]

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        self.assertEqual(scan["status"], "OK")
        self.assertEqual(scan["candidates"][0]["entry_timeframe"], "M30")
        self.assertEqual(scan["candidates"][0]["signal"]["h1"], "flat")
        self.assertEqual(scan["candidates"][0]["signal"]["m30"], "up")

    def test_arena_scan_limits_symbols_per_account_for_rate_guard(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def __init__(self):
                self.symbols = []

            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                self.symbols.append((symbol, interval))
                return {"bars": []}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "100"}}

        market = FakeMarketClient()
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["strategy"]["max_symbols_per_account_per_scan"] = 2
        policy["accounts"] = [
            dict(account, universe=["AAA@MISX", "BBB@MISX", "CCC@MISX"])
            for account in policy["accounts"]
        ]

        arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=market,
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        scanned_symbols = {symbol for symbol, _interval in market.symbols}
        self.assertEqual(scanned_symbols, {"AAA@MISX", "BBB@MISX"})
        self.assertNotIn("CCC@MISX", scanned_symbols)

    def test_arena_scan_short_circuits_flat_h4_before_entry_timeframes(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def __init__(self):
                self.calls = []

            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                self.calls.append((symbol, interval))
                return {
                    "bars": [
                        {
                            "time": f"2026-05-30T{index:02d}:00:00Z",
                            "open": {"value": "100"},
                            "high": {"value": "101"},
                            "low": {"value": "99"},
                            "close": {"value": "100"},
                        }
                        for index in range(16)
                    ]
                }

            def last_quote(self, jwt, symbol):
                raise AssertionError("quote should not be requested for flat H4")

        market = FakeMarketClient()
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"] = [dict(account, universe=["AAA@MISX"]) for account in policy["accounts"]]

        with tempfile.TemporaryDirectory() as tmp:
            scan = arena.build_arena_scan(
                policy,
                arena_client=FakeArenaClient(),
                market_client=market,
                env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret", "FINAM_MARKET_DATA_CACHE_ROOT": tmp},
                now=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(scan["candidates"], [])
        self.assertEqual({interval for _symbol, interval in market.calls}, {"TIME_FRAME_H4"})

    def test_arena_scan_coalesces_bars_across_accounts_and_blocks_entries_after_429_threshold(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def __init__(self):
                self.calls = []

            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                self.calls.append((symbol, interval))
                if symbol.startswith("ERR"):
                    raise RuntimeError("Finam HTTP 429 while calling /v1/instruments/ERR@MISX/bars")
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [_arena_replacement_bar(index, 100 + index) for index in range(count)]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "120"}}

        market = FakeMarketClient()
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["risk"]["max_new_trades_per_account_per_run"] = 99
        policy["strategy"]["max_market_data_429_per_scan"] = 3
        policy["accounts"][0]["universe"] = ["AAA@MISX", "ERR1@MISX", "ERR2@MISX", "ERR3@MISX", "ERR4@MISX"]
        policy["accounts"][1]["universe"] = ["AAA@MISX"]
        policy["accounts"][2]["universe"] = ["AAA@MISX"]

        with tempfile.TemporaryDirectory() as tmp:
            scan = arena.build_arena_scan(
                policy,
                arena_client=FakeArenaClient(),
                market_client=market,
                env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret", "FINAM_MARKET_DATA_CACHE_ROOT": tmp},
                now=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(scan["status"], "DEGRADED")
        self.assertEqual(market.calls.count(("AAA@MISX", "TIME_FRAME_H4")), 1)
        self.assertTrue(any("market_data_degraded_429" in item for item in scan["warnings"]))
        executable = [item for item in scan["candidates"] if item.get("execution_allowed") is True]
        self.assertEqual(executable, [])
        self.assertTrue(all("market_data_degraded_429" in item.get("gate_reasons", []) for item in scan["candidates"]))

    def test_arena_scan_skips_new_candidates_at_open_position_limit(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [
                        {
                            "symbol": "SBER@MISX",
                            "quantity": {"value": "10"},
                            "current_price": {"value": "300.00"},
                        }
                    ]
                    if account_id == "DEMO-RU"
                    else [],
                }

            def orders(self, jwt, account_id):
                return {
                    "orders": [
                        {
                            "symbol": "SBER@MISX",
                            "status": "ORDER_STATUS_WATCHING",
                            "side": "SIDE_SELL",
                            "quantity_sl": {"value": "10"},
                            "sl_price": {"value": "290.00"},
                        }
                    ]
                }

        class FakeMarketClient:
            def __init__(self):
                self.bars_calls = 0

            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                self.bars_calls += 1
                return {"bars": []}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "300"}}

        market = FakeMarketClient()
        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["risk"]["max_open_positions"] = 1

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=market,
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
            soft_stops=[
                {
                    "account_id": "DEMO-RU",
                    "symbol": "SBER@MISX",
                    "side": "SELL",
                    "quantity": "10",
                    "stop_price": "280.00",
                    "status": "ACTIVE",
                }
            ],
        )

        self.assertEqual(scan["status"], "OK")
        self.assertEqual(scan["candidates"], [])
        self.assertGreater(market.bars_calls, 0)
        self.assertEqual(scan["accounts"][0]["status"], "WATCH")
        self.assertIn("лимит позиций: 1/1", scan["accounts"][0]["top_signal"])
        self.assertIn("DEMO-RU: max_open_positions reached; new candidates skipped", scan["warnings"])

    def test_arena_order_payloads_make_short_stop_a_buy(self):
        proposal = arena.arena_trade_proposal(
            {
                "account_id": "DEMO-US",
                "symbol": "AAPL@XNGS",
                "side": "SELL",
                "quantity": "3",
                "entry_price": "190.00",
                "stop_price": "196.00",
                "take_profit_price": "178.00",
                "entry_timeframe": "H1",
                "execution_allowed": True,
            },
            account={"account_id": "DEMO-US"},
        )

        payloads = arena.arena_order_payloads(proposal)

        self.assertEqual(proposal["protective_stop"]["side"], "BUY")
        self.assertEqual(payloads["entry_order"]["side"], "SIDE_SELL")
        self.assertNotIn("type", payloads["entry_order"])
        self.assertNotIn("limit_price", payloads["entry_order"])
        self.assertEqual(payloads["protective_stop"]["mode"], "arena_soft_stop")
        self.assertEqual(payloads["protective_stop"]["side"], "BUY")
        self.assertFalse(proposal["execution"]["broker_mutation"])

    def test_arena_scan_caps_candidate_notional_per_position(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        class FakeMarketClient:
            def create_session(self, secret):
                return "market-jwt"

            def bars(self, jwt, symbol, *, interval, start_time, end_time):
                count = 16 if interval == "TIME_FRAME_H4" else 3
                return {"bars": [
                    {
                        "time": f"2026-05-30T{index:02d}:00:00Z",
                        "open": {"value": str(100 + index)},
                        "high": {"value": str(101 + index)},
                        "low": {"value": str(99 + index)},
                        "close": {"value": str(100 + index)},
                    }
                    for index in range(count)
                ]}

            def last_quote(self, jwt, symbol):
                return {"last": {"value": "100"}}

        policy = arena.DEFAULT_ARENA_POLICY | {
            "risk": arena.DEFAULT_ARENA_POLICY["risk"] | {"risk_per_trade_pct": "10", "max_position_notional_pct": "25"},
            "accounts": [
                dict(arena.DEFAULT_ARENA_POLICY["accounts"][0], universe=["SBER@MISX"]),
                dict(arena.DEFAULT_ARENA_POLICY["accounts"][1], universe=[]),
                dict(arena.DEFAULT_ARENA_POLICY["accounts"][2], universe=[]),
            ],
        }
        policy["accounts"][1]["universe"] = ["AAPL@XNGS"]
        policy["accounts"][2]["universe"] = ["SBER@MISX"]

        scan = arena.build_arena_scan(
            policy,
            arena_client=FakeArenaClient(),
            market_client=FakeMarketClient(),
            env={"FINAM_ARENA_API": "arena-secret", "FINAM_TOKEN": "market-secret"},
            now=datetime(2026, 5, 31, tzinfo=timezone.utc),
        )

        self.assertLessEqual(float(scan["candidates"][0]["notional"]), 250000.0)

    def test_emergency_stop_and_paused_accounts_block_scan_candidates(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["emergency_stop"] = True

        scan = arena.build_arena_scan(policy, arena_client=FakeArenaClient(), env={"FINAM_ARENA_API": "secret"})

        self.assertEqual(scan["status"], "HALT")
        self.assertEqual(scan["candidates"], [])
        self.assertIn("emergency_stop", " ".join(scan["warnings"]))

    def test_paused_account_is_marked_halt_in_status(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [],
                }

        policy = deepcopy(arena.DEFAULT_ARENA_POLICY)
        policy["accounts"][0]["paused"] = True

        status = arena.build_arena_status(policy, client=FakeArenaClient(), env={"FINAM_ARENA_API": "secret"})

        self.assertEqual(status["accounts"][0]["status"], "HALT")
        self.assertTrue(status["accounts"][0]["paused"])

    def test_daily_loss_hard_halt_blocks_arena_scan(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                daily_pnl = "-40000.00" if account_id == "DEMO-RU" else "0"
                return {
                    "account_id": account_id,
                    "cash": {"value": "990000.00"},
                    "equity": {"value": "990000.00"},
                    "available_cash": {"value": "990000.00"},
                    "daily_pnl": {"value": daily_pnl},
                    "positions": [],
                }

        scan = arena.build_arena_scan(
            deepcopy(arena.DEFAULT_ARENA_POLICY),
            arena_client=FakeArenaClient(),
            env={"FINAM_ARENA_API": "secret", "FINAM_TOKEN": "market-secret"},
        )

        self.assertEqual(scan["status"], "HALT")
        self.assertEqual(scan["candidates"], [])
        self.assertEqual(scan["accounts"][0]["halt"], "daily_loss")
        self.assertIn("DEMO-RU: hard halt daily_loss", scan["warnings"])

    def test_account_drawdown_hard_halt_is_reported(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                equity = "915000.00" if account_id == "DEMO-US" else "1000000.00"
                return {
                    "account_id": account_id,
                    "cash": {"value": equity},
                    "equity": {"value": equity},
                    "available_cash": {"value": equity},
                    "positions": [],
                }

        status = arena.build_arena_status(
            deepcopy(arena.DEFAULT_ARENA_POLICY),
            client=FakeArenaClient(),
            env={"FINAM_ARENA_API": "secret"},
        )

        self.assertEqual(status["status"], "HALT")
        self.assertEqual(status["accounts"][1]["halt"], "account_drawdown")
        self.assertEqual(status["accounts"][1]["pnl_pct"], "-8.500")
        self.assertIn("DEMO-US: hard halt account_drawdown", status["warnings"])

    def test_missing_protective_stop_hard_halts_open_position(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [
                        {
                            "symbol": "SBER@MISX",
                            "quantity": {"value": "10"},
                            "current_price": {"value": "300.00"},
                        }
                    ]
                    if account_id == "DEMO-RU"
                    else [],
                }

            def orders(self, jwt, account_id):
                return {"orders": []}

        status = arena.build_arena_status(
            deepcopy(arena.DEFAULT_ARENA_POLICY),
            client=FakeArenaClient(),
            env={"FINAM_ARENA_API": "secret"},
        )

        self.assertEqual(status["status"], "HALT")
        self.assertEqual(status["accounts"][0]["halt"], "missing_protective_stop")
        self.assertEqual(status["accounts"][0]["missing_protective_stop_symbols"], ["SBER@MISX"])
        self.assertIn("DEMO-RU: hard halt missing_protective_stop", status["warnings"])

    def test_covering_protective_stop_allows_open_position_status(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [
                        {
                            "symbol": "SBER@MISX",
                            "quantity": {"value": "10"},
                            "current_price": {"value": "300.00"},
                        }
                    ]
                    if account_id == "DEMO-RU"
                    else [],
                }

            def orders(self, jwt, account_id):
                return {
                    "orders": [
                        {
                            "symbol": "SBER@MISX",
                            "status": "ORDER_STATUS_WATCHING",
                            "side": "SIDE_SELL",
                            "quantity_sl": {"value": "10"},
                            "sl_price": {"value": "280.00"},
                        }
                    ]
                }

        status = arena.build_arena_status(
            deepcopy(arena.DEFAULT_ARENA_POLICY),
            client=FakeArenaClient(),
            env={"FINAM_ARENA_API": "secret"},
            soft_stops=[
                {
                    "account_id": "DEMO-RU",
                    "symbol": "SBER@MISX",
                    "side": "SELL",
                    "quantity": "10",
                    "stop_price": "280.00",
                    "status": "ACTIVE",
                }
            ],
        )

        self.assertEqual(status["status"], "OK")
        self.assertIsNone(status["accounts"][0]["halt"])
        self.assertEqual(status["accounts"][0]["positions"][0]["protective_stop_side"], "SELL")
        self.assertEqual(status["accounts"][0]["stop_orders"][0]["side"], "SELL")
        self.assertEqual(status["accounts"][0]["open_risk_rub"], "200.00")
        self.assertEqual(status["accounts"][0]["open_risk_pct"], "0.0200")

    def test_open_risk_limit_hard_halts_account(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [
                        {
                            "symbol": "SBER@MISX",
                            "quantity": {"value": "1000"},
                            "current_price": {"value": "360.00"},
                        }
                    ]
                    if account_id == "DEMO-RU"
                    else [],
                }

            def orders(self, jwt, account_id):
                return {
                    "orders": [
                        {
                            "symbol": "SBER@MISX",
                            "status": "ORDER_STATUS_WATCHING",
                            "side": "SIDE_SELL",
                            "quantity_sl": {"value": "1000"},
                            "sl_price": {"value": "290.00"},
                        }
                    ]
                }

        status = arena.build_arena_status(
            deepcopy(arena.DEFAULT_ARENA_POLICY),
            client=FakeArenaClient(),
            env={"FINAM_ARENA_API": "secret"},
            soft_stops=[
                {
                    "account_id": "DEMO-RU",
                    "symbol": "SBER@MISX",
                    "side": "SELL",
                    "quantity": "1000",
                    "stop_price": "290.00",
                    "status": "ACTIVE",
                }
            ],
        )

        self.assertEqual(status["status"], "HALT")
        self.assertEqual(status["accounts"][0]["halt"], "open_risk_limit")
        self.assertEqual(status["accounts"][0]["open_risk_rub"], "70000.00")
        self.assertEqual(status["accounts"][0]["open_risk_pct"], "7.00")
        self.assertIn("DEMO-RU: hard halt open_risk_limit", status["warnings"])

    def test_active_plain_order_does_not_count_as_protective_stop(self):
        class FakeArenaClient:
            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {
                    "account_id": account_id,
                    "cash": {"value": "1000000.00"},
                    "equity": {"value": "1000000.00"},
                    "available_cash": {"value": "1000000.00"},
                    "positions": [{"symbol": "SBER@MISX", "quantity": {"value": "10"}}] if account_id == "DEMO-RU" else [],
                }

            def orders(self, jwt, account_id):
                return {
                    "orders": [
                        {
                            "symbol": "SBER@MISX",
                            "status": "ORDER_STATUS_ACTIVE",
                            "side": "SIDE_SELL",
                            "quantity": {"value": "10"},
                        }
                    ]
                }

        status = arena.build_arena_status(
            deepcopy(arena.DEFAULT_ARENA_POLICY),
            client=FakeArenaClient(),
            env={"FINAM_ARENA_API": "secret"},
        )

        self.assertEqual(status["status"], "HALT")
        self.assertEqual(status["accounts"][0]["halt"], "missing_protective_stop")
        self.assertEqual(status["accounts"][0]["stop_orders"], [])


if __name__ == "__main__":
    unittest.main()
