"""Finam Arena runtime helpers.

Arena uses a separate API host and account set from the Finam demo runtime.
This module keeps the contest profile explicit so demo safety gates do not
silently leak into Arena execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from finam_trading_bot.client import FinamClient
from finam_trading_bot.contract import decimal_payload
from finam_trading_bot.mcp_shadow import build_mcp_shadow_review
from finam_trading_bot.research import pretrade_check, research_candidates
from finam_trading_bot.arena_learning import DEFAULT_LEARNING_POLICY, learning_settings, validate_learning_policy


ARENA_BASE_URL = "https://arena.finam.ru"
ARENA_BASE_URL_ENV = "FINAM_ARENA_BASE_URL"
ARENA_APPROVAL_UNTIL_ENV = "FINAM_ARENA_APPROVAL_UNTIL"
ARENA_STARTING_EQUITY = Decimal("1000000")
ARENA_ACCOUNT_IDS = ("DEMO-RU", "DEMO-US", "DEMO-AI")
ARENA_MARGIN_TRADING_NOT_SUPPORTED = "arena_margin_trading_not_supported"
ARENA_LIMITS_TIMEZONE = ZoneInfo("Europe/Moscow")
DEFAULT_MARKET_DATA_CACHE_ROOT = Path(__file__).resolve().parents[2] / "data" / "runtime" / "finam_market_data_cache"
ARENA_AUCTION_SCORE_MODEL_VERSION = "arena_opportunity_auction_v1_provisional"
REPEAT_PATTERN_LOSS_COOLDOWN_GATE = "repeat_pattern_loss_cooldown"
REPEAT_PATTERN_LOSS_SCORE_GATE = "repeat_pattern_loss_score_below_min"
PRETRADE_BUDGET_EXHAUSTED_GATE = "pretrade_budget_exhausted_blocks_autonomy"
PRETRADE_UNAVAILABLE_GATE = "pretrade_event_check_unavailable"
PRETRADE_RISK_GATE = "pretrade_event_check_risk"
CROSS_MARKET_ROLE_MISX_CONCENTRATION_GATE = "cross_market_role_misx_concentration"
SINGLE_SYMBOL_ANCHOR_ROTATION_GATE = "single_symbol_anchor_rotation_candidate"


DEFAULT_ARENA_POLICY: dict[str, Any] = {
    "mode": "autonomous",
    "emergency_stop": False,
    "base_url": ARENA_BASE_URL,
    "base_url_env": ARENA_BASE_URL_ENV,
    "approval_until": "2026-06-03T23:59:59+03:00",
    "approval_until_env": ARENA_APPROVAL_UNTIL_ENV,
    "auto_trade_env": "FINAM_ARENA_AUTO_TRADE_ENABLED",
    "session_secret_env": "FINAM_ARENA_API",
    "accounts": [
        {
            "account_id": "DEMO-RU",
            "label": "РФ",
            "strategy": "russian_equities_h1_h4",
            "markets": ["MISX"],
            "universe": ["SBER@MISX", "GAZP@MISX", "LKOH@MISX", "TATN@MISX", "MOEX@MISX", "PLZL@MISX"],
            "allow_long": True,
            "allow_short": False,
            "paused": False,
            "trade_mode": "auto",
            "risk_multiplier": "1.0",
            "experimental": False,
        },
        {
            "account_id": "DEMO-US",
            "label": "США",
            "strategy": "us_equities_h1_h4",
            "markets": ["XNGS", "XNYS", "XNMS", "XNCM", "XASE", "PINX"],
            "universe": ["AAPL@XNGS", "MSFT@XNGS", "NVDA@XNGS", "TSLA@XNGS", "AMZN@XNGS", "META@XNGS"],
            "allow_long": True,
            "allow_short": False,
            "paused": False,
            "trade_mode": "auto",
            "risk_multiplier": "1.0",
            "primary_entries_per_day": 2,
            "replacement_entries_per_day": 1,
            "experimental": False,
        },
        {
            "account_id": "DEMO-AI",
            "label": "AI",
            "strategy": "experimental_ai_cross_market",
            "markets": ["MISX", "XNGS", "XNYS", "XNMS", "XNCM", "XASE", "PINX"],
            "universe": ["SBER@MISX", "LKOH@MISX", "MOEX@MISX", "AAPL@XNGS", "NVDA@XNGS", "TSLA@XNGS"],
            "allow_long": True,
            "allow_short": False,
            "paused": False,
            "trade_mode": "auto",
            "risk_multiplier": "1.0",
            "experimental": True,
        },
    ],
    "risk": {
        "profile": "aggressive_controlled",
        "starting_equity": "1000000",
        "risk_per_trade_pct": "1.0",
        "max_position_notional_pct": "25.0",
        "stop_atr_multiplier": "2.0",
        "take_profit_r": "2.0",
        "max_daily_loss_pct": "3.0",
        "max_account_drawdown_pct": "8.0",
        "max_open_risk_pct": "5.0",
        "max_open_positions": 8,
        "max_new_trades_per_account_per_run": 1,
        "max_account_gross_exposure_pct": "50.0",
        "max_symbol_exposure_pct": "25.0",
        "max_new_notional_pct_per_day": "25.0",
        "max_new_trades_per_account_per_day": 1,
        "exceptional_entry_limit_overrides": {
            "DEMO-US": {
                "enabled": True,
                "min_score": 86,
                "max_primary_entries_per_day": 2,
                "max_new_notional_pct_per_day": "50.0",
                "allowed_limit_gates": ["daily_new_notional_limit_exceeded", "primary_daily_limit_reached"],
                "forbidden_gates": [
                    "account_gross_exposure_limit_reached",
                    "symbol_exposure_limit_exceeded",
                    "same_symbol_position_open",
                    "same_symbol_trade_today",
                    "research_unavailable_below_exceptional_score",
                    "cross_market_role_misx_concentration",
                ],
            },
            "DEMO-AI": {
                "enabled": True,
                "min_score": 89,
                "research_ok_min_score": 85,
                "max_primary_entries_per_day": 2,
                "max_new_notional_pct_per_day": "50.0",
                "allowed_limit_gates": ["daily_new_notional_limit_exceeded", "primary_daily_limit_reached"],
                "forbidden_gates": [
                    "account_gross_exposure_limit_reached",
                    "symbol_exposure_limit_exceeded",
                    "same_symbol_position_open",
                    "same_symbol_trade_today",
                    "research_unavailable_below_exceptional_score",
                    "cross_market_role_misx_concentration",
                ],
            },
        },
        "max_daily_operations_per_account": 200,
        "allow_same_symbol_scale_in": False,
        "mcp_confirmed_entry": {
            "enabled": True,
            "min_score": 79,
            "max_score": 83,
            "requires_mcp": True,
            "requires_research_ok": True,
            "risk_per_trade_pct": "0.35",
            "max_position_notional_pct": "12.5",
            "max_entries_per_account_per_day": 2,
            "max_new_notional_pct_per_day": "37.5",
            "allowed_removed_gates": [
                "candidate_score_below_min",
                "daily_new_notional_limit_exceeded",
                "primary_daily_limit_reached",
            ],
            "forbidden_gates": [
                "account_gross_exposure_limit_reached",
                "symbol_exposure_limit_exceeded",
                "same_symbol_position_open",
                "same_symbol_trade_today",
                "research_unavailable_below_exceptional_score",
                "entry_strength_confirmation_missing",
                "market_data_degraded_429",
                "cross_market_role_misx_concentration",
                "single_symbol_anchor_rotation_candidate",
            ],
        },
        "hard_halts": [
            "daily_loss",
            "account_drawdown",
            "unresolved_order_state",
            "missing_protective_stop",
            "open_risk_limit",
            "api_degradation",
            "research_provider_failure",
        ],
    },
    "fees": {
        "commission_pct_per_side": "0.035",
        "commission_pct_by_mic": {
            "MISX": "0.035",
            "RUSX": "0.035",
            "RTSX": "0.001",
            "PINX": "0.1",
            "XASE": "0.1",
            "XCME": "0.1",
            "XNCM": "0.1",
            "XNGS": "0.1",
            "XNMS": "0.1",
            "XNYM": "0.1",
            "XNYS": "0.1",
        },
        "slippage_pct_per_side": "0.0",
        "source": "finam_arena_commission_grid",
    },
    "portfolio": {
        "min_cash_buffer_pct": "10.0",
        "replacement_min_score_delta": 20,
        "replacement_min_edge_after_cost_pct": "0.10",
        "breakeven_at_r": "1.0",
        "trail_at_r": "1.5",
        "take_partial_at_r": "2.0",
        "take_partial_fraction_pct": "50.0",
        "autonomy_mode": "full_rotation",
        "target_gross_exposure_pct": "65.0",
        "max_gross_exposure_pct": "75.0",
        "deleveraging_step_pct": "25.0",
        "max_rotation_actions_per_account_per_run": 1,
        "max_rotation_actions_per_account_per_day": 4,
        "min_candidate_score_for_buy": 75,
        "min_replacement_score_delta": 15,
        "min_replacement_edge_after_cost_pct": "0.30",
        "min_hold_minutes_before_rotation": 30,
        "weak_exit_min_hold_minutes": 120,
        "weak_exit_negative_r_threshold": "-0.25",
        "weak_exit_noise_buffer_pct": "0.10",
        "weak_exit_immediate_score_threshold": 35,
        "weak_exit_min_mae_samples": 30,
        "weak_exit_threshold_source": "provisional_hysteresis_v1",
        "block_new_entries_when_over_target_gross": True,
        "opportunity_min_score": 75,
        "opportunity_hold_cash_score": 60,
        "opportunity_exceptional_base_score": 84,
        "opportunity_reserve_until_msk": "19:00",
        "opportunity_max_quote_age_seconds": 60,
        "opportunity_max_replacement_turnover_cost_pct": "0.50",
        "event_overheated_gap_pct": "5.0",
        "event_min_rr_after_gap": "1.5",
    },
    "strategy": {
        "timeframes": ["H4", "H1", "M30"],
        "h4_role": "regime_filter",
        "entry_timeframes": ["H1", "M30"],
        "instruments": "equities_long_only_arena",
        "max_symbols_per_account_per_scan": 12,
        "max_market_data_429_per_scan": 3,
        "quote_cache_ttl_seconds": 10,
    },
    "relative_value": {
        "enabled": True,
        "mode": "long_only_rank_modifier",
        "max_score_delta": 10,
    },
    "learning": DEFAULT_LEARNING_POLICY,
    "telegram": {
        "pulse_max_chars": 1800,
        "menu": ["overview", "account_DEMO-RU", "account_DEMO-US", "account_DEMO-AI", "risks", "strategy"],
    },
    "research": {
        "enabled": False,
        "provider": "codex_review",
        "model": "openai-codex",
        "h4_model": "perplexity/sonar",
        "pretrade_model": "perplexity/sonar-pro",
        "daily_model": "perplexity/sonar-pro",
        "weekly_model": "perplexity/sonar-pro",
        "h4_max_candidates": 2,
        "h4_max_tokens": 350,
        "h4_cache_ttl_seconds": 1800,
        "h4_daily_request_budget": 6,
        "pretrade_daily_request_budget": 4,
        "daily_digest_request_budget": 1,
        "weekly_review_request_budget": 1,
        "arena_h4_max_symbols_per_scan": 3,
        "arena_h4_max_symbols_per_account": 1,
        "finam_rss_enabled": True,
    },
}


@dataclass(frozen=True)
class ArenaAccountProfile:
    account_id: str
    label: str
    strategy: str
    markets: tuple[str, ...]
    universe: tuple[str, ...]
    allow_long: bool
    allow_short: bool
    paused: bool
    trade_mode: str
    risk_multiplier: Decimal
    primary_entries_per_day: int | None = None
    replacement_entries_per_day: int | None = None
    experimental: bool = False


def load_arena_policy(path: Path) -> dict[str, Any]:
    policy = deepcopy(DEFAULT_ARENA_POLICY)
    if not path.exists():
        return policy
    with path.open("r", encoding="utf-8") as file:
        override = json.load(file)
    if not isinstance(override, dict):
        raise RuntimeError(f"Arena policy file is not a JSON object: {path}")
    return _deep_merge(policy, override)


def arena_base_url(policy: dict[str, Any], *, env: Any = os.environ) -> str:
    env_name = str(policy.get("base_url_env") or ARENA_BASE_URL_ENV)
    from_env = str(env.get(env_name) or "").strip()
    return from_env or str(policy.get("base_url") or ARENA_BASE_URL)


def arena_approval_until(policy: dict[str, Any], *, env: Any = os.environ) -> str | None:
    env_name = str(policy.get("approval_until_env") or ARENA_APPROVAL_UNTIL_ENV)
    from_env = str(env.get(env_name) or "").strip()
    if from_env:
        return from_env
    value = policy.get("approval_until")
    return str(value) if value is not None else None


def arena_account_profiles(policy: dict[str, Any]) -> list[ArenaAccountProfile]:
    accounts = policy.get("accounts") if isinstance(policy.get("accounts"), list) else []
    profiles: list[ArenaAccountProfile] = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("account_id") or "").strip()
        if not account_id:
            continue
        markets = tuple(str(market) for market in item.get("markets") or [])
        universe = tuple(str(symbol) for symbol in item.get("universe") or [])
        profiles.append(
            ArenaAccountProfile(
                account_id=account_id,
                label=str(item.get("label") or account_id),
                strategy=str(item.get("strategy") or "arena_strategy"),
                markets=markets,
                universe=universe,
                allow_long=bool(item.get("allow_long", True)),
                allow_short=bool(item.get("allow_short", False)),
                paused=bool(item.get("paused", False)),
                trade_mode=str(item.get("trade_mode") or "manual"),
                risk_multiplier=_decimal(item.get("risk_multiplier")) or Decimal("1"),
                primary_entries_per_day=(
                    item.get("primary_entries_per_day")
                    if isinstance(item.get("primary_entries_per_day"), int)
                    else None
                ),
                replacement_entries_per_day=(
                    item.get("replacement_entries_per_day")
                    if isinstance(item.get("replacement_entries_per_day"), int)
                    else None
                ),
                experimental=bool(item.get("experimental", False)),
            )
        )
    return profiles


def validate_arena_policy(policy: dict[str, Any]) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    profiles = arena_account_profiles(policy)
    account_ids = [profile.account_id for profile in profiles]

    if policy.get("mode") not in {"approval", "autonomous"}:
        errors.append("mode must be approval or autonomous")
    if not isinstance(policy.get("emergency_stop", False), bool):
        errors.append("emergency_stop must be boolean")
    if len(account_ids) != 3:
        errors.append("accounts must contain exactly three configured accounts")
    if len(set(account_ids)) != len(account_ids):
        errors.append("accounts must not contain duplicates")

    risk = _risk_settings(policy)
    for path in (
        "risk_per_trade_pct",
        "max_position_notional_pct",
        "max_daily_loss_pct",
        "max_account_drawdown_pct",
        "max_open_risk_pct",
        "max_account_gross_exposure_pct",
        "max_symbol_exposure_pct",
        "max_new_notional_pct_per_day",
    ):
        if _decimal(risk.get(path)) is None or _decimal(risk.get(path)) <= 0:
            errors.append(f"risk.{path} must be a positive number")
    for path in ("max_open_positions", "max_new_trades_per_account_per_run", "max_new_trades_per_account_per_day", "max_daily_operations_per_account"):
        value = risk.get(path)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"risk.{path} must be a positive integer")
    if not isinstance(risk.get("allow_same_symbol_scale_in", False), bool):
        errors.append("risk.allow_same_symbol_scale_in must be boolean")
    _validate_exceptional_entry_limit_overrides(risk, account_ids=account_ids, errors=errors)
    _validate_mcp_confirmed_entry(risk, errors=errors)

    fees = _fees_settings(policy)
    for path in ("commission_pct_per_side", "slippage_pct_per_side"):
        if _decimal(fees.get(path)) is None or _decimal(fees.get(path)) < 0:
            errors.append(f"fees.{path} must be a non-negative number")
    commission_by_mic = fees.get("commission_pct_by_mic") if isinstance(fees.get("commission_pct_by_mic"), dict) else {}
    for mic, value in commission_by_mic.items():
        if _decimal(value) is None or _decimal(value) < 0:
            errors.append(f"fees.commission_pct_by_mic.{mic} must be a non-negative number")

    portfolio = _portfolio_settings(policy)
    for path in (
        "min_cash_buffer_pct",
        "replacement_min_edge_after_cost_pct",
        "breakeven_at_r",
        "trail_at_r",
        "take_partial_at_r",
        "take_partial_fraction_pct",
        "target_gross_exposure_pct",
        "max_gross_exposure_pct",
        "deleveraging_step_pct",
        "min_replacement_edge_after_cost_pct",
        "weak_exit_noise_buffer_pct",
    ):
        if _decimal(portfolio.get(path)) is None or _decimal(portfolio.get(path)) < 0:
            errors.append(f"portfolio.{path} must be a non-negative number")
    for path in (
        "replacement_min_score_delta",
        "max_rotation_actions_per_account_per_run",
        "max_rotation_actions_per_account_per_day",
        "min_candidate_score_for_buy",
        "min_replacement_score_delta",
        "min_hold_minutes_before_rotation",
        "weak_exit_min_hold_minutes",
        "weak_exit_immediate_score_threshold",
        "weak_exit_min_mae_samples",
    ):
        if not isinstance(portfolio.get(path), int) or portfolio.get(path, 0) < 0:
            errors.append(f"portfolio.{path} must be a non-negative integer")
    score_by_account = portfolio.get("min_candidate_score_for_buy_by_account")
    if score_by_account is not None:
        if not isinstance(score_by_account, dict):
            errors.append("portfolio.min_candidate_score_for_buy_by_account must be an object")
        else:
            for account_id, value in score_by_account.items():
                if str(account_id) not in set(account_ids):
                    errors.append(f"portfolio.min_candidate_score_for_buy_by_account.{account_id} account is not allowed")
                if not isinstance(value, int) or value < 0 or value > 100:
                    errors.append(f"portfolio.min_candidate_score_for_buy_by_account.{account_id} must be an integer from 0 to 100")
    role_gates = portfolio.get("account_role_gates")
    if role_gates is not None:
        if not isinstance(role_gates, dict):
            errors.append("portfolio.account_role_gates must be an object")
        else:
            _validate_account_role_gates(role_gates, account_ids=account_ids, errors=errors)
    weak_exit_threshold = _decimal(portfolio.get("weak_exit_negative_r_threshold"))
    if weak_exit_threshold is None or weak_exit_threshold >= 0:
        errors.append("portfolio.weak_exit_negative_r_threshold must be a negative number")
    if portfolio.get("autonomy_mode") not in {"review_only", "exits_auto", "full_rotation"}:
        errors.append("portfolio.autonomy_mode must be review_only, exits_auto, or full_rotation")
    if not isinstance(portfolio.get("block_new_entries_when_over_target_gross", True), bool):
        errors.append("portfolio.block_new_entries_when_over_target_gross must be boolean")
    target_gross = _decimal(portfolio.get("target_gross_exposure_pct"))
    max_gross = _decimal(portfolio.get("max_gross_exposure_pct"))
    if target_gross is not None and max_gross is not None and max_gross < target_gross:
        errors.append("portfolio.max_gross_exposure_pct must be >= portfolio.target_gross_exposure_pct")
    strategy = _strategy_settings(policy)
    max_symbols = strategy.get("max_symbols_per_account_per_scan")
    if not isinstance(max_symbols, int) or max_symbols <= 0:
        errors.append("strategy.max_symbols_per_account_per_scan must be a positive integer")
    for path in ("max_market_data_429_per_scan", "quote_cache_ttl_seconds"):
        if not isinstance(strategy.get(path), int) or strategy.get(path, 0) < 0:
            errors.append(f"strategy.{path} must be a non-negative integer")
    if strategy.get("instruments") != "equities_long_only_arena":
        errors.append("strategy.instruments must be equities_long_only_arena")
    relative_value = policy.get("relative_value") if isinstance(policy.get("relative_value"), dict) else DEFAULT_ARENA_POLICY["relative_value"]
    if not isinstance(relative_value.get("enabled"), bool):
        errors.append("relative_value.enabled must be boolean")
    if relative_value.get("mode") != "long_only_rank_modifier":
        errors.append("relative_value.mode must be long_only_rank_modifier")
    if not isinstance(relative_value.get("max_score_delta"), int) or not (0 <= int(relative_value.get("max_score_delta")) <= 10):
        errors.append("relative_value.max_score_delta must be an integer from 0 to 10")
    learning_validation = validate_learning_policy(policy)
    errors.extend(learning_validation["errors"])
    warnings.extend(learning_validation["warnings"])

    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    for path in ("h4_max_candidates", "h4_max_tokens", "h4_cache_ttl_seconds"):
        if path in research and (not isinstance(research.get(path), int) or research.get(path, 0) <= 0):
            errors.append(f"research.{path} must be a positive integer")
    if _decimal(risk.get("risk_per_trade_pct")) and _decimal(risk.get("risk_per_trade_pct")) > Decimal("2"):
        warnings.append("risk.risk_per_trade_pct above 2% is very aggressive")
    if _decimal(risk.get("max_position_notional_pct")) and _decimal(risk.get("max_position_notional_pct")) > Decimal("40"):
        warnings.append("risk.max_position_notional_pct above 40% is very concentrated")

    for profile in profiles:
        if not profile.markets:
            errors.append(f"{profile.account_id}: markets must not be empty")
        if not profile.universe:
            errors.append(f"{profile.account_id}: universe must not be empty")
        if profile.trade_mode not in {"manual", "auto"}:
            errors.append(f"{profile.account_id}: trade_mode must be manual or auto")
        if profile.risk_multiplier <= 0:
            errors.append(f"{profile.account_id}: risk_multiplier must be positive")
        if profile.primary_entries_per_day is not None and profile.primary_entries_per_day <= 0:
            errors.append(f"{profile.account_id}: primary_entries_per_day must be a positive integer")
        if profile.replacement_entries_per_day is not None and profile.replacement_entries_per_day < 0:
            errors.append(f"{profile.account_id}: replacement_entries_per_day must be a non-negative integer")
        if profile.risk_multiplier > Decimal("2"):
            warnings.append(f"{profile.account_id}: risk_multiplier above 2 is very aggressive")
        if profile.allow_short:
            warnings.append(f"{profile.account_id}: allow_short is ignored for long-only Arena; short entries remain WATCH-only")
        if profile.allow_short and not profile.allow_long:
            warnings.append(f"{profile.account_id}: short-only profile requires extra review")

    return {"errors": errors, "warnings": warnings}


def _validate_account_role_gates(role_gates: dict[str, Any], *, account_ids: list[str], errors: list[str]) -> None:
    allowed = set(account_ids)
    for account_id, gates in role_gates.items():
        normalized_account = str(account_id)
        if normalized_account not in allowed:
            errors.append(f"portfolio.account_role_gates.{normalized_account} account is not allowed")
        if not isinstance(gates, dict):
            errors.append(f"portfolio.account_role_gates.{normalized_account} must be an object")
            continue
        anchor = gates.get("single_symbol_anchor_rotation")
        if anchor is not None:
            if not isinstance(anchor, dict):
                errors.append(f"portfolio.account_role_gates.{normalized_account}.single_symbol_anchor_rotation must be an object")
            else:
                if not isinstance(anchor.get("enabled", True), bool):
                    errors.append(f"portfolio.account_role_gates.{normalized_account}.single_symbol_anchor_rotation.enabled must be boolean")
                for key in ("anchor_notional_pct", "required_progress_r"):
                    if _decimal(anchor.get(key)) is None:
                        errors.append(f"portfolio.account_role_gates.{normalized_account}.single_symbol_anchor_rotation.{key} must be a number")
                for key in ("candidate_min_score", "min_score_delta"):
                    if key in anchor and (not isinstance(anchor.get(key), int) or anchor.get(key, 0) < 0):
                        errors.append(f"portfolio.account_role_gates.{normalized_account}.single_symbol_anchor_rotation.{key} must be a non-negative integer")
        concentration = gates.get("cross_market_concentration")
        if concentration is not None:
            if not isinstance(concentration, dict):
                errors.append(f"portfolio.account_role_gates.{normalized_account}.cross_market_concentration must be an object")
            else:
                if not isinstance(concentration.get("enabled", True), bool):
                    errors.append(f"portfolio.account_role_gates.{normalized_account}.cross_market_concentration.enabled must be boolean")
                if not isinstance(concentration.get("manual_override", False), bool):
                    errors.append(f"portfolio.account_role_gates.{normalized_account}.cross_market_concentration.manual_override must be boolean")
                if not isinstance(concentration.get("max_positions", 0), int) or int(concentration.get("max_positions") or 0) < 1:
                    errors.append(f"portfolio.account_role_gates.{normalized_account}.cross_market_concentration.max_positions must be a positive integer")


def _validate_exceptional_entry_limit_overrides(
    risk: dict[str, Any],
    *,
    account_ids: list[str],
    errors: list[str],
) -> None:
    overrides = risk.get("exceptional_entry_limit_overrides")
    if overrides is None:
        return
    if not isinstance(overrides, dict):
        errors.append("risk.exceptional_entry_limit_overrides must be an object")
        return
    allowed_accounts = set(account_ids)
    for account_id, config in overrides.items():
        normalized_account = str(account_id)
        path = f"risk.exceptional_entry_limit_overrides.{normalized_account}"
        if normalized_account not in allowed_accounts:
            errors.append(f"{path} account is not allowed")
        if not isinstance(config, dict):
            errors.append(f"{path} must be an object")
            continue
        if not isinstance(config.get("enabled", True), bool):
            errors.append(f"{path}.enabled must be boolean")
        for key in ("min_score", "research_ok_min_score"):
            if key in config and (not isinstance(config.get(key), int) or not (0 <= int(config.get(key)) <= 100)):
                errors.append(f"{path}.{key} must be an integer from 0 to 100")
        if not isinstance(config.get("max_primary_entries_per_day", 0), int) or int(config.get("max_primary_entries_per_day") or 0) < 1:
            errors.append(f"{path}.max_primary_entries_per_day must be a positive integer")
        if _decimal(config.get("max_new_notional_pct_per_day")) is None or _decimal(config.get("max_new_notional_pct_per_day")) <= 0:
            errors.append(f"{path}.max_new_notional_pct_per_day must be a positive number")
        for key in ("allowed_limit_gates", "forbidden_gates"):
            value = config.get(key)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(item, str) and item for item in value)
            ):
                errors.append(f"{path}.{key} must be a list of non-empty strings")


def _validate_mcp_confirmed_entry(risk: dict[str, Any], *, errors: list[str]) -> None:
    config = risk.get("mcp_confirmed_entry")
    if config is None:
        return
    path = "risk.mcp_confirmed_entry"
    if not isinstance(config, dict):
        errors.append(f"{path} must be an object")
        return
    for key in ("enabled", "requires_mcp", "requires_research_ok"):
        if not isinstance(config.get(key, True), bool):
            errors.append(f"{path}.{key} must be boolean")
    for key in ("min_score", "max_score"):
        if not isinstance(config.get(key), int) or not (0 <= int(config.get(key) or 0) <= 100):
            errors.append(f"{path}.{key} must be an integer from 0 to 100")
    if isinstance(config.get("min_score"), int) and isinstance(config.get("max_score"), int) and config["min_score"] > config["max_score"]:
        errors.append(f"{path}.min_score must be <= max_score")
    for key in ("risk_per_trade_pct", "max_position_notional_pct", "max_new_notional_pct_per_day"):
        if _decimal(config.get(key)) is None or _decimal(config.get(key)) <= 0:
            errors.append(f"{path}.{key} must be a positive number")
    if not isinstance(config.get("max_entries_per_account_per_day"), int) or int(config.get("max_entries_per_account_per_day") or 0) < 1:
        errors.append(f"{path}.max_entries_per_account_per_day must be a positive integer")
    for key in ("allowed_removed_gates", "forbidden_gates"):
        value = config.get(key)
        if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) and item for item in value)):
            errors.append(f"{path}.{key} must be a list of non-empty strings")


def build_arena_status(
    policy: dict[str, Any],
    *,
    client: FinamClient | None = None,
    env: Any = os.environ,
    now: datetime | None = None,
    soft_stops: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validation = validate_arena_policy(policy)
    base_url = arena_base_url(policy, env=env)
    approval_until = arena_approval_until(policy, env=env)
    report: dict[str, Any] = {
        "status": "OK" if not validation["errors"] else "INVALID",
        "command": "arena-status",
        "mode": policy.get("mode"),
        "emergency_stop": bool(policy.get("emergency_stop", False)),
        "base_url": base_url,
        "approval_until": approval_until,
        "accounts": [],
        "validation": validation,
        "errors": list(validation["errors"]),
        "warnings": list(validation["warnings"]),
    }
    if validation["errors"]:
        return report
    if policy.get("emergency_stop") is True:
        report["status"] = "HALT"
        report["warnings"].append("Arena emergency_stop is enabled; scans and broker mutations are blocked")

    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    secret = str(env.get(secret_env) or "").strip()
    if not secret:
        report["status"] = "NO_TRADE"
        report["errors"].append(f"{secret_env} is not set")
        return report

    client = client or FinamClient(base_url=base_url)
    try:
        jwt = client.create_session(secret)
    except Exception as exc:  # noqa: BLE001
        report["status"] = "NO_TRADE"
        report["errors"].append(f"Arena session failed: {exc}")
        return report

    account_errors = 0
    account_halts = 0
    risk = _risk_settings(policy)
    current_time = now or datetime.now(timezone.utc)
    for profile in arena_account_profiles(policy):
        try:
            raw = client.get_account(jwt, profile.account_id)
            summary = arena_account_summary(raw, profile, risk=risk)
            _apply_recent_trades(summary, client=client, jwt=jwt, account_id=profile.account_id, now=current_time, warnings=report["warnings"])
            if not summary.get("halt"):
                _apply_protective_stop_halt(
                    summary,
                    raw,
                    account_id=profile.account_id,
                    risk=risk,
                    soft_stops=soft_stops or [],
                )
            if summary.get("halt"):
                account_halts += 1
                report["warnings"].append(f"{profile.account_id}: hard halt {summary.get('halt')}")
        except Exception as exc:  # noqa: BLE001
            account_errors += 1
            summary = {
                "account_id": profile.account_id,
                "label": profile.label,
                "strategy": profile.strategy,
                "status": "ERROR",
                "error": str(exc),
            }
        report["accounts"].append(summary)

    if account_errors:
        report["status"] = "DEGRADED" if account_errors < len(report["accounts"]) else "NO_TRADE"
    if account_halts:
        report["status"] = "HALT"
    return report


def arena_account_summary(raw: dict[str, Any], profile: ArenaAccountProfile, *, risk: dict[str, Any] | None = None) -> dict[str, Any]:
    risk = risk or {}
    equity = _decimal(_first_value(raw, "equity"))
    pnl = equity - ARENA_STARTING_EQUITY if equity is not None else None
    pnl_pct = (pnl / ARENA_STARTING_EQUITY * Decimal("100")) if pnl is not None else None
    daily_pnl = _decimal(_first_existing(raw, ("daily_pnl", "daily_profit", "pnl_today", "day_pnl")))
    daily_pnl_pct = (daily_pnl / equity * Decimal("100")) if daily_pnl is not None and equity is not None and equity > 0 else None
    positions = raw.get("positions") if isinstance(raw.get("positions"), list) else []
    halt_reasons = _arena_account_halt_reasons(pnl_pct, daily_pnl_pct, risk=risk)
    status = "HALT" if profile.paused or halt_reasons else raw.get("status") or "ACCOUNT_ACTIVE"
    return {
        "account_id": str(raw.get("account_id") or profile.account_id),
        "label": profile.label,
        "strategy": profile.strategy,
        "markets": list(profile.markets),
        "universe_size": len(profile.universe),
        "allow_long": profile.allow_long,
        "allow_short": profile.allow_short,
        "paused": profile.paused,
        "trade_mode": profile.trade_mode,
        "risk_multiplier": _decimal_payload(profile.risk_multiplier),
        "experimental": profile.experimental,
        "status": status,
        "equity": _decimal_payload(equity),
        "cash": _decimal_payload(_first_value(raw, "cash")),
        "available_cash": _decimal_payload(_first_value(raw, "available_cash")),
        "unrealized_pnl": _decimal_payload(_first_value(raw, "unrealized_profit")),
        "daily_pnl_rub": _decimal_payload(daily_pnl),
        "daily_pnl_pct": _decimal_payload(daily_pnl_pct),
        "pnl_rub": _decimal_payload(pnl),
        "pnl_pct": _decimal_payload(pnl_pct),
        "positions_count": len(_arena_open_positions(positions)),
        "positions": _arena_open_positions(positions),
        "stop_orders": [],
        "recent_trades": [],
        "open_risk_rub": None,
        "open_risk_pct": None,
        "halt": "account_paused" if profile.paused else (halt_reasons[0] if halt_reasons else None),
        "halt_reasons": halt_reasons,
        "top_signal": None,
    }


def _arena_account_halt_reasons(pnl_pct: Decimal | None, daily_pnl_pct: Decimal | None, *, risk: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    max_daily_loss = _decimal(risk.get("max_daily_loss_pct"))
    max_drawdown = _decimal(risk.get("max_account_drawdown_pct"))
    if daily_pnl_pct is not None and max_daily_loss is not None and daily_pnl_pct <= -max_daily_loss:
        reasons.append("daily_loss")
    if pnl_pct is not None and max_drawdown is not None and pnl_pct <= -max_drawdown:
        reasons.append("account_drawdown")
    return reasons


def _apply_recent_trades(
    summary: dict[str, Any],
    *,
    client: FinamClient,
    jwt: str,
    account_id: str,
    now: datetime,
    warnings: list[str],
) -> None:
    trades_fn = getattr(client, "trades", None)
    if not callable(trades_fn):
        return
    try:
        response = trades_fn(jwt, account_id, limit=1000, start_time=_iso_z(now - timedelta(days=7)))
    except Exception as exc:  # noqa: BLE001
        summary["recent_trades_error"] = str(exc)
        warnings.append(f"{account_id}: recent trades unavailable: {exc}")
        return
    all_trades = _arena_recent_trades(response, limit=1000)
    summary["recent_trades"] = all_trades[:5]
    day_trades = _trades_on_day(all_trades, now=now, day_tz=ARENA_LIMITS_TIMEZONE)
    summary["daily_trades"] = day_trades
    summary["daily_operations_count"] = len(day_trades)
    summary["daily_buy_count"] = len([trade for trade in day_trades if "BUY" in str(trade.get("side") or "").upper()])


def _arena_recent_trades(response: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for trade in _items_list(response, ("trades", "items", "data"))[:limit]:
        symbol = str(_first_existing(trade, ("symbol", "security_code", "ticker")) or "").strip()
        side = str(_first_existing(trade, ("side", "operation", "buy_sell")) or "").upper()
        quantity = _decimal(_first_existing(trade, ("quantity", "qty", "balance")))
        price = _decimal(_first_existing(trade, ("price", "execution_price", "trade_price")))
        if not symbol:
            continue
        result.append(
            {
                "symbol": symbol,
                "side": side or None,
                "quantity": _decimal_payload(quantity),
                "price": _decimal_payload(price),
                "time": _first_existing(trade, ("time", "created_at", "trade_time", "timestamp")),
                "trade_id": _first_existing(trade, ("trade_id", "id")),
            }
        )
    return result


def _apply_protective_stop_halt(
    summary: dict[str, Any],
    raw_account: dict[str, Any],
    *,
    account_id: str,
    risk: dict[str, Any],
    soft_stops: list[dict[str, Any]],
) -> None:
    positions = _arena_open_positions(raw_account.get("positions") if isinstance(raw_account.get("positions"), list) else [])
    summary["positions"] = positions
    if not positions:
        return
    stop_orders = _arena_soft_stop_orders(soft_stops, account_id=account_id)
    summary["stop_orders"] = stop_orders
    summary["stop_protection_mode"] = "arena_soft_stop"
    open_risk_rub, open_risk_pct = _arena_open_risk(positions, stop_orders, _decimal(summary.get("equity")))
    summary["open_risk_rub"] = _decimal_payload(open_risk_rub)
    summary["open_risk_pct"] = _decimal_payload(open_risk_pct)
    missing = [position for position in positions if not _position_has_protective_stop(position, stop_orders)]
    if missing:
        _append_halt(summary, "missing_protective_stop")
        summary["missing_protective_stop_symbols"] = [str(item.get("symbol")) for item in missing]
    max_open_risk_pct = _decimal(risk.get("max_open_risk_pct"))
    if open_risk_pct is not None and max_open_risk_pct is not None and open_risk_pct > max_open_risk_pct:
        _append_halt(summary, "open_risk_limit")


def _arena_open_positions(positions: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        symbol = str(_first_existing(item, ("symbol", "security_code", "ticker")) or "").strip()
        quantity = _decimal(_first_existing(item, ("quantity", "balance", "qty")))
        if not symbol or quantity in {None, Decimal("0")}:
            continue
        side = _position_side(item, quantity)
        result.append(
            {
                "symbol": symbol,
                "quantity": _decimal_payload(abs(quantity)),
                "side": side,
                "average_price": _decimal_payload(_first_existing(item, ("average_price", "avg_price", "price"))),
                "current_price": _decimal_payload(_first_existing(item, ("current_price", "last_price", "market_price", "price"))),
                "protective_stop_side": "SELL" if side == "LONG" else "BUY",
            }
        )
    return result


def _arena_active_stop_orders(orders_response: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for order in _orders_list(orders_response):
        symbol = str(_first_existing(order, ("symbol", "security_code", "ticker")) or "").strip()
        status = str(_first_value(order, "status") or "").upper()
        side = str(_first_value(order, "side") or "").upper()
        quantity = _decimal(_first_existing(order, ("quantity_sl", "quantity", "balance", "qty")))
        stop_price = _decimal(_first_existing(order, ("sl_price", "stop_price", "stop")))
        if not symbol or quantity is None or quantity <= 0 or stop_price is None:
            continue
        if not _is_active_stop_status(status):
            continue
        if side not in {"SIDE_SELL", "SELL", "SIDE_BUY", "BUY"}:
            continue
        result.append(
            {
                "symbol": symbol,
                "side": "SELL" if side.endswith("SELL") else "BUY",
                "quantity": _decimal_payload(quantity),
                "stop_price": _decimal_payload(stop_price),
                "status": status,
                "order_id": _first_existing(order, ("order_id", "id")),
            }
        )
    return result


def _arena_soft_stop_orders(soft_stops: list[dict[str, Any]], *, account_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in soft_stops:
        if not isinstance(item, dict):
            continue
        if str(item.get("account_id") or "") != account_id:
            continue
        symbol = str(item.get("symbol") or "").strip()
        side = str(item.get("side") or "").upper()
        quantity = _decimal(item.get("quantity"))
        stop_price = _decimal(item.get("stop_price"))
        status = str(item.get("status") or "ACTIVE").upper()
        if not symbol or side not in {"SELL", "BUY", "SIDE_SELL", "SIDE_BUY"}:
            continue
        if quantity is None or quantity <= 0 or stop_price is None:
            continue
        if status not in {"ACTIVE", "ARENA_SOFT_STOP_ACTIVE"}:
            continue
        result.append(
            {
                "symbol": symbol,
                "side": "SELL" if side.endswith("SELL") else "BUY",
                "quantity": _decimal_payload(quantity),
                "stop_price": _decimal_payload(stop_price),
                "status": "ARENA_SOFT_STOP_ACTIVE",
                "order_id": item.get("entry_order_id"),
                "mode": "arena_soft_stop",
            }
        )
    return result


def _position_has_protective_stop(position: dict[str, Any], stop_orders: list[dict[str, Any]]) -> bool:
    return _covering_protective_stop(position, stop_orders) is not None


def _covering_protective_stop(position: dict[str, Any], stop_orders: list[dict[str, Any]]) -> dict[str, Any] | None:
    required_side = str(position.get("protective_stop_side") or "")
    required_qty = _decimal(position.get("quantity"))
    if required_qty is None:
        return None
    for order in stop_orders:
        if order.get("symbol") != position.get("symbol"):
            continue
        if order.get("side") != required_side:
            continue
        order_qty = _decimal(order.get("quantity"))
        if order_qty is not None and order_qty >= required_qty:
            return order
    return None


def _arena_open_risk(
    positions: list[dict[str, Any]], stop_orders: list[dict[str, Any]], equity: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    total: Decimal | None = None
    for position in positions:
        stop_order = _covering_protective_stop(position, stop_orders)
        if stop_order is None:
            continue
        qty = _decimal(position.get("quantity"))
        price = _decimal(position.get("current_price")) or _decimal(position.get("average_price"))
        stop_price = _decimal(stop_order.get("stop_price"))
        if qty is None or price is None or stop_price is None or qty <= 0:
            continue
        if position.get("side") == "SHORT":
            risk_per_share = stop_price - price
        else:
            risk_per_share = price - stop_price
        if risk_per_share < 0:
            risk_per_share = Decimal("0")
        total = (total or Decimal("0")) + qty * risk_per_share
    if total is None:
        return None, None
    pct = (total / equity * Decimal("100")) if equity is not None and equity > 0 else None
    return total, pct


def _position_side(position: dict[str, Any], quantity: Decimal) -> str:
    side = str(_first_value(position, "side") or _first_value(position, "position_side") or "").upper()
    if "SHORT" in side or side.endswith("SELL"):
        return "SHORT"
    if "LONG" in side or side.endswith("BUY"):
        return "LONG"
    return "LONG" if quantity > 0 else "SHORT"


def _is_active_stop_status(status: str) -> bool:
    return any(token in status for token in ("WATCHING", "ACTIVE", "ACCEPTED", "NEW"))


def _orders_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    return _items_list(response, ("orders", "items", "data"))


def _items_list(response: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        items = response.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _append_halt(summary: dict[str, Any], reason: str) -> None:
    reasons = summary.get("halt_reasons") if isinstance(summary.get("halt_reasons"), list) else []
    if reason not in reasons:
        reasons.append(reason)
    summary["halt_reasons"] = reasons
    summary["halt"] = reasons[0]
    summary["status"] = "HALT"


class _ArenaMarketDataCache:
    def __init__(self, *, now: datetime, root: Path | None = None, quote_ttl_seconds: int = 10) -> None:
        self.now = now
        self.root = root or DEFAULT_MARKET_DATA_CACHE_ROOT
        self.quote_ttl_seconds = max(0, quote_ttl_seconds)
        self._bars: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        self._quotes: dict[str, tuple[datetime, dict[str, Any]]] = {}

    def bars(
        self,
        client: FinamClient,
        jwt: str,
        symbol: str,
        *,
        interval: str,
        days: int,
    ) -> list[dict[str, Any]]:
        bucket = _market_data_bucket(self.now, interval)
        key = (symbol, interval, bucket)
        if key in self._bars:
            return self._bars[key]
        use_persisted_cache = isinstance(client, FinamClient)
        if use_persisted_cache:
            cached = self._read_bars(symbol, interval, bucket)
            if cached is not None:
                self._bars[key] = cached
                return cached
        bars = _fetch_bars(client, jwt, symbol, interval=interval, now=self.now, days=days)
        self._bars[key] = bars
        if use_persisted_cache:
            self._write_bars(symbol, interval, bucket, bars)
        return bars

    def quote(self, client: FinamClient, jwt: str, symbol: str) -> dict[str, Any]:
        cached = self._quotes.get(symbol)
        if cached is not None:
            created_at, quote = cached
            if (self.now - created_at).total_seconds() <= self.quote_ttl_seconds:
                return quote
        quote = client.last_quote(jwt, symbol)
        self._quotes[symbol] = (self.now, quote)
        return quote

    def _read_bars(self, symbol: str, interval: str, bucket: str) -> list[dict[str, Any]] | None:
        path = self._bars_path(symbol, interval, bucket)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if payload.get("expires_at") is not None:
            expires_at = _parse_datetime(str(payload.get("expires_at")))
            if expires_at is None or expires_at <= self.now:
                return None
        bars = payload.get("bars")
        if not isinstance(bars, list):
            return None
        return [item for item in bars if isinstance(item, dict)]

    def _write_bars(self, symbol: str, interval: str, bucket: str, bars: list[dict[str, Any]]) -> None:
        path = self._bars_path(symbol, interval, bucket)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "symbol": symbol,
            "interval": interval,
            "bucket": bucket,
            "expires_at": _iso_z(_market_data_bucket_expires_at(self.now, interval)),
            "bars": bars,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _bars_path(self, symbol: str, interval: str, bucket: str) -> Path:
        digest = hashlib.sha256(f"{symbol}|{interval}|{bucket}".encode("utf-8")).hexdigest()[:24]
        return self.root / "bars" / f"{digest}.json"


def build_arena_scan(
    policy: dict[str, Any],
    *,
    arena_client: FinamClient | None = None,
    market_client: FinamClient | None = None,
    research_opener: Any | None = None,
    research_mode: str = "normal",
    env: Any = os.environ,
    now: datetime | None = None,
    soft_stops: list[dict[str, Any]] | None = None,
    execution_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    status = build_arena_status(policy, client=arena_client, env=env, now=current, soft_stops=soft_stops)
    if execution_ledger:
        _enrich_recent_trades_from_ledger(
            status.get("accounts") if isinstance(status.get("accounts"), list) else [],
            execution_ledger,
            now=current,
            fees=_fees_settings(policy),
        )
    if status.get("status") not in {"OK", "DEGRADED"}:
        return arena_scan_from_status(status)
    if policy.get("emergency_stop") is True:
        scan = arena_scan_from_status(status)
        scan["status"] = "HALT"
        scan["warnings"] = list(scan.get("warnings") or []) + ["Arena emergency_stop is enabled; candidate scan skipped"]
        return scan

    market_secret = str(env.get("FINAM_TOKEN") or "").strip()
    if not market_secret:
        scan = arena_scan_from_status(status)
        scan["status"] = "DEGRADED"
        scan["warnings"] = list(scan.get("warnings") or []) + ["FINAM_TOKEN is not set; Arena market scan skipped"]
        return scan

    market_client = market_client or FinamClient()
    try:
        market_jwt = market_client.create_session(market_secret)
    except Exception as exc:  # noqa: BLE001
        scan = arena_scan_from_status(status)
        scan["status"] = "DEGRADED"
        scan["warnings"] = list(scan.get("warnings") or []) + [f"Finam market session failed: {exc}"]
        return scan

    accounts_by_id = {
        str(account.get("account_id")): account
        for account in status.get("accounts") or []
        if isinstance(account, dict)
    }
    candidates: list[dict[str, Any]] = []
    warnings = list(status.get("warnings") or [])
    strategy = _strategy_settings(policy)
    market_data_cache = _ArenaMarketDataCache(
        now=current,
        root=Path(str(env.get("FINAM_MARKET_DATA_CACHE_ROOT") or DEFAULT_MARKET_DATA_CACHE_ROOT)),
        quote_ttl_seconds=int(strategy.get("quote_cache_ttl_seconds") or 10),
    )
    _enrich_arena_position_prices(
        accounts_by_id,
        market_client=market_client,
        market_jwt=market_jwt,
        market_data_cache=market_data_cache,
        warnings=warnings,
    )
    for profile in arena_account_profiles(policy):
        account = accounts_by_id.get(profile.account_id)
        if not account:
            continue
        if profile.paused:
            account["top_signal"] = "счет на паузе"
            continue
        account["entry_limits"] = _arena_account_entry_limits(
            profile,
            account,
            policy=policy,
            now=current,
            execution_ledger=execution_ledger,
        )
        warnings_before = len(warnings)
        account_candidates = _scan_account_candidates(
            profile,
            account,
            policy=policy,
            market_client=market_client,
            market_jwt=market_jwt,
            market_data_cache=market_data_cache,
            now=current,
            warnings=warnings,
        )
        if not account_candidates and len(warnings) > warnings_before and account.get("status") == "ACCOUNT_ACTIVE":
            account["status"] = "WATCH"
        candidates.extend(account_candidates)

    repeat_pattern = _apply_repeat_pattern_loss_guard(
        candidates,
        policy,
        execution_ledger=execution_ledger or [],
        now=current,
    )
    research = _arena_research_gate(candidates, policy, urlopen=research_opener, warnings=warnings, research_mode=research_mode)
    _apply_arena_candidate_scores(candidates, research=research, policy=policy)
    _apply_arena_account_role_gates(candidates, accounts_by_id=accounts_by_id, policy=policy)
    _apply_exceptional_entry_limit_overrides(candidates, accounts_by_id=accounts_by_id, policy=policy, now=current)
    market_data_429_count = _market_data_429_count(warnings)
    max_market_data_429 = int(strategy.get("max_market_data_429_per_scan") or 0)
    market_data_degraded = max_market_data_429 > 0 and market_data_429_count > max_market_data_429
    if market_data_degraded:
        for candidate in candidates:
            _block_arena_candidate(candidate, "market_data_degraded_429")
        warnings.append(
            f"market_data_degraded_429: {market_data_429_count} Finam 429 responses exceeded threshold {max_market_data_429}; new entries blocked"
        )
    pretrade = _arena_pretrade_event_gate(
        candidates,
        policy,
        urlopen=research_opener,
        warnings=warnings,
        research_mode=research_mode,
        now=current,
    )
    mcp_shadow = None
    if _has_mcp_confirmed_entry_potential(candidates, policy):
        mcp_shadow_scan = {
            "status": status.get("status") or "OK",
            "command": "arena-scan",
            "mode": status.get("mode"),
            "accounts": list(accounts_by_id.values()),
            "candidates": candidates,
            "errors": status.get("errors") or [],
            "warnings": warnings,
        }
        mcp_shadow = build_mcp_shadow_review(policy, arena_scan=mcp_shadow_scan, env=env, max_bytes=8000, now=current)
        _apply_mcp_confirmed_entry_overrides(
            candidates,
            accounts_by_id=accounts_by_id,
            policy=policy,
            mcp_shadow=mcp_shadow,
            now=current,
        )
        if any((item.get("mcp_confirmed_entry") or {}).get("applied") for item in candidates if isinstance(item, dict)):
            mcp_shadow["trading_gate_effect"] = "reduced_risk_entry_override"
    _apply_arena_account_candidate_signals(accounts_by_id, candidates)
    scan_status = status.get("status")
    errors = status.get("errors") or []
    if research.get("classification") == "provider_client_failure":
        scan_status = "HALT"
        errors = list(errors) + ["Arena research provider failure; autonomous trading halted"]
    elif research.get("status") not in {None, "disabled", "skipped", "ok"}:
        scan_status = "DEGRADED" if scan_status == "OK" else scan_status
    elif market_data_degraded:
        scan_status = "DEGRADED" if scan_status == "OK" else scan_status

    scan = {
        "status": scan_status,
        "command": "arena-scan",
        "mode": status.get("mode"),
        "accounts": list(accounts_by_id.values()),
        "candidates": candidates,
        "research": research,
        "pretrade": pretrade,
        "repeat_pattern": repeat_pattern,
        "errors": errors,
        "warnings": warnings,
    }
    if mcp_shadow is not None:
        scan["mcp_shadow"] = mcp_shadow
    return scan


def build_arena_portfolio_review(policy: dict[str, Any], scan: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Build read-only portfolio exit and replacement proposals from an Arena scan."""
    portfolio = _portfolio_settings(policy)
    fees = _fees_settings(policy)
    accounts = [item for item in scan.get("accounts") or [] if isinstance(item, dict)]
    candidates = [item for item in scan.get("candidates") or [] if isinstance(item, dict)]
    account_reviews: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    planned_actions: list[dict[str, Any]] = []
    research = scan.get("research") if isinstance(scan.get("research"), dict) else {}
    current = now or datetime.now(timezone.utc)

    for account in accounts:
        account_id = str(account.get("account_id") or "")
        account_candidates = [item for item in candidates if str(item.get("account_id") or "") == account_id]
        gross_exposure_pct = _account_gross_exposure_pct(account)
        position_reviews = [
            _arena_position_review(position, account=account, portfolio=portfolio, fees=fees, research=research, now=current)
            for position in account.get("positions") or []
            if isinstance(position, dict)
        ]
        _apply_overexposure_actions(account, position_reviews, portfolio=portfolio)
        for review in position_reviews:
            if review.get("action") in {"BREAKEVEN_STOP", "TRAIL_STOP", "TAKE_PARTIAL_PROFIT", "TRIM_OVEREXPOSURE", "EXIT_WEAK"}:
                exits.append({"account_id": account_id, **review})
        replacement = _arena_replacement_proposal(account, position_reviews, account_candidates, portfolio=portfolio, fees=fees)
        if replacement is not None:
            replacements.append(replacement)
        action = _arena_planned_action(account, position_reviews, replacement, portfolio=portfolio)
        if action is not None:
            planned_actions.append(action)
        account_reviews.append(
            {
                "account_id": account_id,
                "label": account.get("label"),
                "equity": account.get("equity"),
                "cash": account.get("cash") or account.get("available_cash"),
                "gross_exposure_pct": _decimal_payload(gross_exposure_pct),
                "entry_limits": account.get("entry_limits"),
                "positions": position_reviews,
                "replacement": replacement,
                "replacement_block_reason": account.get("replacement_block_reason"),
                "planned_action": action,
            }
        )

    return {
        "status": "OK" if scan.get("status") in {"OK", "DEGRADED"} else scan.get("status"),
        "command": "arena-portfolio-review",
        "mode": scan.get("mode"),
        "accounts": account_reviews,
        "replacement_proposals": replacements,
        "exit_proposals": exits,
        "planned_actions": planned_actions,
        "errors": scan.get("errors") or [],
        "warnings": scan.get("warnings") or [],
        "cost_model": {
            "commission_pct_per_side": _decimal_payload(_decimal(fees.get("commission_pct_per_side"))),
            "commission_pct_by_mic": dict(fees.get("commission_pct_by_mic") or {}),
            "slippage_pct_per_side": _decimal_payload(_decimal(fees.get("slippage_pct_per_side"))),
            "source": fees.get("source"),
        },
    }


def build_repeat_pattern_loss_report(
    policy: dict[str, Any],
    execution_ledger: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    settings = _repeat_pattern_loss_settings(policy)
    events = _repeat_pattern_loss_events(execution_ledger, policy, now=current)
    active_cooldowns = []
    active_penalties = []
    for event in events:
        payload = _repeat_pattern_candidate_payload(event, now=current)
        if payload.get("action") == "cooldown":
            active_cooldowns.append(event)
        else:
            active_penalties.append(event)
    total_loss = sum((_decimal(item.get("net_pnl_rub")) or Decimal("0")) for item in events)
    return {
        "status": "OK" if settings.get("enabled", True) else "DISABLED",
        "command": "arena-pattern-report",
        "generated_at": current.isoformat(timespec="seconds"),
        "settings": settings,
        "events_count": len(events),
        "active_cooldowns_count": len(active_cooldowns),
        "active_penalties_count": len(active_penalties),
        "repeat_loss_net_pnl_rub": _decimal_payload(total_loss),
        "events": sorted(events, key=lambda item: str(item.get("exit_timestamp") or ""), reverse=True)[:10],
        "broker_mutation": False,
        "provider_call": False,
    }


def build_arena_opportunity_auction(
    policy: dict[str, Any],
    scan: dict[str, Any],
    portfolio_review: dict[str, Any],
    *,
    event_candidates: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rank read-only Arena action opportunities without changing execution state."""
    current = now or datetime.now(timezone.utc)
    portfolio = _portfolio_settings(policy)
    accounts = {str(item.get("account_id") or ""): item for item in scan.get("accounts") or [] if isinstance(item, dict)}
    review_accounts = {
        str(item.get("account_id") or ""): item
        for item in portfolio_review.get("accounts") or []
        if isinstance(item, dict)
    }
    opportunities: list[dict[str, Any]] = []
    opportunities.extend(_arena_auction_risk_exit_opportunities(portfolio_review))
    opportunities.extend(_arena_auction_replacement_opportunities(portfolio_review, accounts=accounts, portfolio=portfolio))
    opportunities.extend(_arena_auction_base_entry_opportunities(scan, accounts=accounts, portfolio=portfolio, now=current))
    opportunities.extend(
        _arena_auction_event_opportunities(event_candidates or [], accounts=accounts, scan=scan, portfolio=portfolio, now=current)
    )
    opportunities.extend(_arena_auction_hold_cash_opportunities(accounts, review_accounts=review_accounts, portfolio=portfolio))

    winner = _arena_auction_winner(opportunities, portfolio=portfolio)
    rejected = [
        {
            "opportunity_type": item.get("opportunity_type"),
            "account_id": item.get("account_id"),
            "symbol": item.get("symbol"),
            "decision": item.get("decision"),
            "reason": item.get("reason"),
            "risk_adjusted_score": item.get("risk_adjusted_score"),
        }
        for item in opportunities
        if not _arena_same_opportunity(item, winner)
    ]
    research = scan.get("research") if isinstance(scan.get("research"), dict) else {}
    return {
        "status": "OK",
        "command": "arena-opportunity-auction",
        "mode": scan.get("mode"),
        "score_model": {
            "version": ARENA_AUCTION_SCORE_MODEL_VERSION,
            "kind": "provisional_heuristic_not_ev_or_probability",
            "shadow_min_days": 7,
            "live_use_allowed": False,
        },
        "winner": winner,
        "opportunities": opportunities,
        "rejected": rejected,
        "shadow_decision": {
            "decision_timestamp": current.isoformat(timespec="seconds"),
            "current_contour_action": _arena_current_contour_action(scan, portfolio_review),
            "auction_winner": _arena_compact_auction_winner(winner),
            "lookahead_guard": "frozen_inputs_only; outcome checkpoints must be appended separately",
        },
        "research": {
            "status": research.get("status"),
            "provider_call": bool(research.get("provider_call")),
            "cache_hit": research.get("cache_hit"),
            "model": research.get("model"),
        },
        "errors": list(dict.fromkeys(list(scan.get("errors") or []) + list(portfolio_review.get("errors") or []))),
        "warnings": list(dict.fromkeys(list(scan.get("warnings") or []) + list(portfolio_review.get("warnings") or []))),
    }


def build_arena_attribution(
    policy: dict[str, Any],
    status: dict[str, Any],
    *,
    now: datetime | None = None,
    execution_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build read-only daily PnL attribution from Arena account state and trades."""
    fees = _fees_settings(policy)
    current = now or datetime.now(timezone.utc)
    accounts: list[dict[str, Any]] = []
    total_turnover = Decimal("0")
    total_commission = Decimal("0")
    total_realized = Decimal("0")
    total_unrealized = Decimal("0")
    total_price = Decimal("0")
    ledger_by_account = _daily_ledger_by_account(execution_ledger or [], now=current)
    report_accounts = [item for item in status.get("accounts") or [] if isinstance(item, dict)]
    seen_account_ids = {str(item.get("account_id") or "") for item in report_accounts}
    policy_accounts = {profile.account_id: profile for profile in arena_account_profiles(policy)}
    for account_id in ledger_by_account:
        if account_id in seen_account_ids:
            continue
        profile = policy_accounts.get(account_id)
        report_accounts.append(
            {
                "account_id": account_id,
                "label": profile.label if profile is not None else account_id,
                "positions": [],
                "daily_trades": [],
            }
        )
    for account in report_accounts:
        account_id = str(account.get("account_id") or "")
        buy_turnover = Decimal("0")
        sell_turnover = Decimal("0")
        commission = Decimal("0")
        realized = Decimal("0")
        incomplete_trades = 0
        ledger_items = ledger_by_account.get(account_id, [])
        ledger_ids = {str(item.get("order_id") or "") for item in ledger_items if item.get("order_id")}
        trades = account.get("daily_trades") if isinstance(account.get("daily_trades"), list) else _trades_on_day(account.get("recent_trades") or [], now=current)
        attribution_items = list(ledger_items)
        used_ledger_indexes: set[int] = set()
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            trade_id = str(trade.get("trade_id") or "")
            if trade_id and trade_id in ledger_ids:
                continue
            match_index, match = _match_recent_trade_to_ledger(trade, ledger_items, used_ledger_indexes)
            if match is not None:
                used_ledger_indexes.add(match_index)
                _apply_ledger_trade_fields(trade, match, fees=fees)
                if "SELL" in str(match.get("side") or "").upper():
                    realized_estimate = _estimated_realized_pnl_from_ledger_sell(match, ledger_items)
                    if realized_estimate is not None:
                        trade["realized_pnl_estimate"] = _decimal_payload(realized_estimate)
                        trade["realized_pnl_source"] = "arena_execution_ledger_estimate"
                continue
            attribution_items.append(trade | {"source": "arena_recent_trades"})
        for trade in attribution_items:
            if not isinstance(trade, dict):
                continue
            if trade.get("source") in {"arena_replacement_allowance", "arena_execution_ledger_error"}:
                continue
            quantity = _decimal(trade.get("quantity")) or Decimal("0")
            price = _decimal(trade.get("price")) or Decimal("0")
            symbol = str(trade.get("symbol") or "")
            if quantity <= 0 or price <= 0:
                incomplete_trades += 1
                continue
            notional = abs(quantity) * price
            side = str(trade.get("side") or "").upper()
            if "SELL" in side:
                sell_turnover += notional
                realized_estimate = _decimal(trade.get("realized_pnl_estimate"))
                if realized_estimate is None:
                    realized_estimate = _estimated_realized_pnl_from_ledger_sell(trade, ledger_items)
                if realized_estimate is not None:
                    realized += realized_estimate
            else:
                buy_turnover += notional
            trade_commission = _decimal(trade.get("estimated_commission"))
            if trade_commission is None:
                trade_commission = notional * _arena_commission_pct(symbol, fees=fees) / Decimal("100")
            commission += trade_commission
        unrealized = sum(
            (
                _decimal(position.get("unrealized_pnl")) or Decimal("0")
                for position in account.get("positions") or []
                if isinstance(position, dict)
            ),
            Decimal("0"),
        )
        price_contribution = unrealized
        total = buy_turnover + sell_turnover
        total_turnover += total
        total_commission += commission
        total_realized += realized
        total_unrealized += unrealized
        total_price += price_contribution
        accounts.append(
            {
                "account_id": account.get("account_id"),
                "label": account.get("label"),
                "cash": account.get("cash") or account.get("available_cash"),
                "equity": account.get("equity"),
                "buy_turnover": _decimal_payload(buy_turnover),
                "sell_turnover": _decimal_payload(sell_turnover),
                "turnover": _decimal_payload(total),
                "estimated_commission": _decimal_payload(commission),
                "realized_pnl_estimate": _decimal_payload(realized),
                "unrealized_pnl": _decimal_payload(unrealized),
                "price_contribution": _decimal_payload(price_contribution),
                "commission_drag": _decimal_payload(-commission),
                "operations_count": len(trades),
                "attributed_operations_count": len(attribution_items) - incomplete_trades,
                "incomplete_trades_count": incomplete_trades,
                "attribution_source": "ledger+arena_recent_trades" if ledger_items else "arena_recent_trades",
            }
        )
    report_status = status.get("status")
    if ledger_by_account and accounts and report_status == "NO_TRADE":
        report_status = "DEGRADED"
    return {
        "status": report_status,
        "command": "arena-attribution",
        "mode": status.get("mode"),
        "accounts": accounts,
        "totals": {
            "turnover": _decimal_payload(total_turnover),
            "estimated_commission": _decimal_payload(total_commission),
            "realized_pnl_estimate": _decimal_payload(total_realized),
            "unrealized_pnl": _decimal_payload(total_unrealized),
            "price_contribution": _decimal_payload(total_price),
            "commission_drag": _decimal_payload(-total_commission),
        },
        "cost_model": {
            "commission_pct_per_side": _decimal_payload(_decimal(fees.get("commission_pct_per_side"))),
            "commission_pct_by_mic": dict(fees.get("commission_pct_by_mic") or {}),
            "source": fees.get("source"),
            "currency_model": "arena_virtual_rub_no_fx",
        },
        "errors": status.get("errors") or [],
        "warnings": status.get("warnings") or [],
    }


def _daily_ledger_by_account(ledger: list[dict[str, Any]], *, now: datetime) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    current_date = now.astimezone(timezone.utc).date()
    for item in ledger:
        if not isinstance(item, dict):
            continue
        timestamp = _parse_datetime(item.get("timestamp") or item.get("time") or item.get("created_at"))
        if timestamp is None or timestamp.astimezone(timezone.utc).date() != current_date:
            continue
        account_id = str(item.get("account_id") or "")
        if not account_id:
            continue
        result.setdefault(account_id, []).append(item)
    return result




def _enrich_recent_trades_from_ledger(
    accounts: list[Any],
    ledger: list[dict[str, Any]],
    *,
    now: datetime,
    fees: dict[str, Any],
) -> None:
    ledger_by_account = _daily_ledger_by_account(ledger, now=now)
    for account in accounts:
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("account_id") or "")
        ledger_items = [item for item in ledger_by_account.get(account_id, []) if _ledger_trade_is_attributable(item)]
        if not ledger_items:
            continue
        used_ledger_indexes: set[int] = set()
        for trade in account.get("recent_trades") or []:
            if not isinstance(trade, dict):
                continue
            match_index, match = _match_recent_trade_to_ledger(trade, ledger_items, used_ledger_indexes)
            if match is None:
                continue
            used_ledger_indexes.add(match_index)
            _apply_ledger_trade_fields(trade, match, fees=fees)
            if "SELL" in str(match.get("side") or "").upper():
                realized = _estimated_realized_pnl_from_ledger_sell(match, ledger_items)
                if realized is not None:
                    trade["realized_pnl_estimate"] = _decimal_payload(realized)
                    trade["realized_pnl_source"] = "arena_execution_ledger_estimate"


def _ledger_trade_is_attributable(item: dict[str, Any]) -> bool:
    if item.get("source") in {"arena_replacement_allowance", "arena_execution_ledger_error"}:
        return False
    return bool(item.get("symbol") and item.get("side") and item.get("quantity") and item.get("price"))


def _match_recent_trade_to_ledger(
    trade: dict[str, Any],
    ledger_items: list[dict[str, Any]],
    used_indexes: set[int],
) -> tuple[int, dict[str, Any] | None]:
    trade_symbol = str(trade.get("symbol") or "").upper()
    trade_side = str(trade.get("side") or "").upper()
    trade_price = _decimal(trade.get("price"))
    trade_time = _parse_datetime(trade.get("time"))
    best: tuple[Decimal, int, dict[str, Any]] | None = None
    for index, item in enumerate(ledger_items):
        if index in used_indexes:
            continue
        if str(item.get("symbol") or "").upper() != trade_symbol:
            continue
        item_side = str(item.get("side") or "").upper()
        if ("BUY" in trade_side) != ("BUY" in item_side):
            continue
        if ("SELL" in trade_side) != ("SELL" in item_side):
            continue
        item_price = _decimal(item.get("price"))
        if trade_price is not None and item_price is not None and abs(trade_price - item_price) > Decimal("0.05"):
            continue
        item_time = _parse_datetime(item.get("timestamp") or item.get("time"))
        if trade_time is not None and item_time is not None:
            seconds = abs(Decimal(str((trade_time - item_time).total_seconds())))
            if seconds > Decimal("7200"):
                continue
        elif trade_time is not None or item_time is not None:
            continue
        score = abs((trade_price or Decimal("0")) - (item_price or Decimal("0")))
        if trade_time is not None and item_time is not None:
            score += abs(Decimal(str((trade_time - item_time).total_seconds()))) / Decimal("100000")
        if best is None or score < best[0]:
            best = (score, index, item)
    if best is None:
        return -1, None
    return best[1], best[2]


def _apply_ledger_trade_fields(trade: dict[str, Any], ledger_item: dict[str, Any], *, fees: dict[str, Any]) -> None:
    if _decimal(trade.get("quantity")) is None:
        trade["quantity"] = ledger_item.get("quantity")
    if _decimal(trade.get("price")) is None:
        trade["price"] = ledger_item.get("price")
    quantity = _decimal(trade.get("quantity"))
    price = _decimal(trade.get("price"))
    if _decimal(trade.get("notional")) is None and quantity is not None and price is not None:
        trade["notional"] = _decimal_payload(abs(quantity) * price)
    commission = _decimal(ledger_item.get("estimated_commission"))
    if commission is None:
        symbol = str(ledger_item.get("symbol") or trade.get("symbol") or "")
        notional = _decimal(trade.get("notional"))
        if notional is not None:
            commission = notional * _arena_commission_pct(symbol, fees=fees) / Decimal("100")
    if commission is not None and _decimal(trade.get("estimated_commission")) is None:
        trade["estimated_commission"] = _decimal_payload(commission)
    trade["source"] = "broker_recent_trades+arena_execution_ledger"
    if ledger_item.get("order_id") and not trade.get("ledger_order_id"):
        trade["ledger_order_id"] = ledger_item.get("order_id")


def _estimated_realized_pnl_from_ledger_sell(sell: dict[str, Any], ledger_items: list[dict[str, Any]]) -> Decimal | None:
    symbol = str(sell.get("symbol") or "").upper()
    sell_time = _parse_datetime(sell.get("timestamp") or sell.get("time"))
    sell_qty = _decimal(sell.get("quantity"))
    sell_price = _decimal(sell.get("price"))
    if not symbol or sell_time is None or sell_qty is None or sell_qty <= 0 or sell_price is None:
        return None
    remaining = sell_qty
    cost = Decimal("0")
    buy_commission = Decimal("0")
    buys = []
    for item in ledger_items:
        if str(item.get("symbol") or "").upper() != symbol:
            continue
        if "BUY" not in str(item.get("side") or "").upper():
            continue
        buy_time = _parse_datetime(item.get("timestamp") or item.get("time"))
        if buy_time is None or buy_time > sell_time:
            continue
        quantity = _decimal(item.get("quantity"))
        price = _decimal(item.get("price"))
        if quantity is None or quantity <= 0 or price is None:
            continue
        buys.append((buy_time, quantity, price, _decimal(item.get("estimated_commission")) or Decimal("0")))
    for _buy_time, quantity, price, commission in sorted(buys, key=lambda item: item[0], reverse=True):
        if remaining <= 0:
            break
        matched = min(remaining, quantity)
        cost += matched * price
        if quantity > 0:
            buy_commission += commission * matched / quantity
        remaining -= matched
    if remaining > 0:
        return None
    sell_notional = sell_qty * sell_price
    sell_commission = _decimal(sell.get("estimated_commission")) or Decimal("0")
    return sell_notional - cost - buy_commission - sell_commission


def arena_scan_from_status(status: dict[str, Any]) -> dict[str, Any]:
    accounts = status.get("accounts") if isinstance(status.get("accounts"), list) else []
    return {
        "status": status.get("status"),
        "command": "arena-scan",
        "mode": status.get("mode"),
        "accounts": accounts,
        "candidates": [],
        "next_step": "Arena H4-regime plus H1/M30 activity candidate engine is not enabled yet; this scan is account/preflight only.",
        "errors": status.get("errors") or [],
        "warnings": status.get("warnings") or [],
    }


def arena_trade_proposal(candidate: dict[str, Any], *, account: dict[str, Any]) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "")
    side = str(candidate.get("side") or "BUY").upper()
    protective_side = "BUY" if side == "SELL" else "SELL"
    proposal = {
        "account_id": str(account.get("account_id") or candidate.get("account_id")),
        "symbol": symbol,
        "side": side,
        "quantity": int(_decimal(candidate.get("quantity")) or Decimal("0")),
        "entry": {
            "type": "LIMIT",
            "limit_price": candidate.get("entry_price"),
            "timeframe": candidate.get("entry_timeframe"),
        },
        "protective_stop": {
            "side": protective_side,
            "stop_price": candidate.get("stop_price"),
            "must_place_immediately_after_fill": True,
        },
        "take_profit": {
            "type": "TP_2R",
            "price": candidate.get("take_profit_price"),
        },
        "risk": {
            "risk_rub": candidate.get("risk_rub"),
            "risk_per_share": candidate.get("risk_per_share"),
            "notional": candidate.get("notional"),
            "atr_h4": candidate.get("atr_h4"),
            "short_availability": candidate.get("short_availability"),
        },
        "gates": {
            "status": candidate.get("status"),
            "gate_reasons": candidate.get("gate_reasons") or [],
            "execution_allowed": bool(candidate.get("execution_allowed")),
        },
        "execution": {
            "broker_mutation": False,
            "confirmation_required": True,
            "confirmation_phrase": f"CONFIRM_ARENA_{side} {symbol} {candidate.get('account_id')}",
        },
    }
    if isinstance(candidate.get("mcp_confirmed_entry"), dict):
        proposal["mcp_confirmed_entry"] = candidate["mcp_confirmed_entry"]
    return proposal


def arena_order_payloads(proposal: dict[str, Any]) -> dict[str, Any]:
    side = str(proposal.get("side") or "BUY").upper()
    broker_side = "SIDE_SELL" if side == "SELL" else "SIDE_BUY"
    stop_side = str(proposal.get("protective_stop", {}).get("side") or "SELL").upper()
    broker_stop_side = "SIDE_BUY" if stop_side == "BUY" else "SIDE_SELL"
    quantity = proposal.get("quantity")
    return {
        "entry_order": {
            "symbol": proposal["symbol"],
            "side": broker_side,
            "quantity": {"value": decimal_payload(quantity, min_scale=1)},
        },
        "protective_stop": {
            "mode": "arena_soft_stop",
            "symbol": proposal["symbol"],
            "side": "BUY" if broker_stop_side == "SIDE_BUY" else "SELL",
            "quantity": {"value": decimal_payload(quantity, min_scale=1)},
            "stop_price": {"value": str(proposal["protective_stop"]["stop_price"])},
        },
    }


def _arena_research_gate(
    candidates: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    urlopen: Any | None,
    warnings: list[str],
    research_mode: str,
) -> dict[str, Any]:
    research_policy = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    if not research_policy.get("enabled", False):
        return {"status": "disabled", "verdict": "UNAVAILABLE"}
    if not candidates:
        return {"status": "skipped", "reason": "no_trade_candidates", "verdict": "UNAVAILABLE"}
    if research_mode == "skip":
        return {"status": "skipped", "reason": "research_skipped", "verdict": "UNAVAILABLE"}
    if research_mode == "budgeted":
        selected = _arena_executable_research_candidates(candidates, policy)
        if not selected:
            selected = _arena_near_executable_research_candidates(candidates, policy)
        if not selected:
            return {
                "status": "skipped",
                "reason": "research_skipped_budgeted_no_executable_candidates",
                "verdict": "UNAVAILABLE",
                "mode": research_mode,
            }
    else:
        selected = _arena_research_candidates(candidates, policy)
    if not selected:
        return {"status": "skipped", "reason": "no_researchable_candidates", "verdict": "UNAVAILABLE"}
    result = research_candidates(selected, policy, urlopen=urlopen, cache_only=research_mode == "cache_only")
    if research_mode == "cache_only" and result.get("reason") == "h4_research_cache_miss":
        return result
    if result.get("classification") == "provider_client_failure":
        warnings.append("Arena research provider failure; trading halted")
        for candidate in candidates:
            _block_arena_candidate(candidate, "research_provider_failure")
        return result
    if result.get("status") != "ok":
        warnings.append("Arena research unavailable; continuing without research score bonus")
        return result
    avoid_symbols = _arena_research_avoid_symbols(result)
    risk_symbols = _arena_research_risk_symbols(result)
    for candidate in candidates:
        if str(candidate.get("symbol")) in avoid_symbols:
            _block_arena_candidate(candidate, "research_avoid")
        elif str(candidate.get("symbol")) in risk_symbols:
            _block_arena_candidate(candidate, "research_risk_requires_manual_review")
    return result


def _apply_repeat_pattern_loss_guard(
    candidates: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    execution_ledger: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    settings = _repeat_pattern_loss_settings(policy)
    if not settings.get("enabled", True):
        return {"status": "disabled", "events": 0, "blocked": 0, "penalized": 0}
    events = _repeat_pattern_loss_events(execution_ledger, policy, now=now)
    if not events:
        return {"status": "ok", "events": 0, "blocked": 0, "penalized": 0}
    blocked = 0
    penalized = 0
    skipped_strong_cluster = 0
    matched_events: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not _candidate_in_repeat_pattern_scope(candidate, settings):
            continue
        match, match_mode = _latest_repeat_pattern_match(events, candidate, settings=settings, now=now)
        if match is None:
            continue
        payload = _repeat_pattern_candidate_payload(match, now=now, candidate=candidate, match_mode=match_mode)
        if payload.get("action") == "penalty" and match_mode == "cluster":
            scope = _candidate_repeat_pattern_scope(candidate, settings) or {}
            pre_score = _arena_candidate_score(candidate)
            raw_threshold = scope.get("cluster_penalty_max_pre_score")
            threshold = int(raw_threshold) if raw_threshold is not None else 90
            if pre_score >= threshold:
                skipped_strong_cluster += 1
                continue
        candidate["repeat_pattern_loss"] = payload
        matched_events.append(payload)
        if payload.get("action") == "cooldown":
            _block_arena_candidate(candidate, REPEAT_PATTERN_LOSS_COOLDOWN_GATE)
            blocked += 1
            continue
        if payload.get("action") == "penalty":
            raw_penalty = settings.get("score_penalty")
            penalty = int(raw_penalty) if raw_penalty is not None else 25
            _apply_repeat_pattern_score_penalty(candidate, penalty=penalty)
            penalized += 1
            min_score = _min_candidate_score_for_buy(policy, account_id=str(candidate.get("account_id") or ""))
            if int(_arena_candidate_score(candidate)) < min_score:
                _block_arena_candidate(candidate, REPEAT_PATTERN_LOSS_SCORE_GATE)
                blocked += 1
    return {
        "status": "ok",
        "events": len(events),
        "blocked": blocked,
        "penalized": penalized,
        "skipped_strong_cluster": skipped_strong_cluster,
        "matched": matched_events[:5],
        "broker_mutation": False,
        "provider_call": False,
    }


def _arena_pretrade_event_gate(
    candidates: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    urlopen: Any | None,
    warnings: list[str],
    research_mode: str,
    now: datetime,
) -> dict[str, Any]:
    research_policy = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    if not research_policy.get("enabled", False):
        return {"status": "disabled", "mode": "pretrade_check", "provider_call": False}
    if research_mode in {"skip", "cache_only"}:
        return {
            "status": "skipped",
            "mode": "pretrade_check",
            "reason": f"pretrade_not_run_in_{research_mode}_mode",
            "provider_call": False,
        }
    selected = _arena_pretrade_candidates(candidates, policy)
    if not selected:
        return {"status": "skipped", "mode": "pretrade_check", "reason": "no_unblocked_us_buy_candidate", "provider_call": False}
    items: list[dict[str, Any]] = []
    results_by_symbol: dict[str, dict[str, Any]] = {}
    provider_call = False
    blocked = 0
    for candidate in selected:
        symbol = str(candidate.get("symbol") or "").upper()
        result = results_by_symbol.get(symbol)
        if result is None:
            result = pretrade_check(symbol, policy, urlopen=urlopen, now=now)
            results_by_symbol[symbol] = result
        provider_call = provider_call or bool(result.get("provider_call"))
        verdict = str(result.get("verdict") or "UNAVAILABLE").upper()
        item = {
            "symbol": symbol,
            "account_id": str(candidate.get("account_id") or ""),
            "status": result.get("status"),
            "reason": result.get("reason"),
            "verdict": verdict,
            "provider_call": bool(result.get("provider_call")),
            "budget": {
                "period": result.get("budget_period"),
                "limit": result.get("budget_limit"),
                "used": result.get("budget_used"),
                "remaining": result.get("budget_remaining"),
                "spent_symbols": result.get("budget_spent_symbols"),
            },
        }
        candidate["pretrade_check"] = item
        if result.get("reason") == "openrouter_daily_budget_exhausted":
            _block_arena_candidate(candidate, PRETRADE_BUDGET_EXHAUSTED_GATE)
            warnings.append(f"{candidate.get('account_id')}:{symbol}: pretrade research budget exhausted; autonomous US entry blocked")
            blocked += 1
        elif result.get("status") != "ok":
            _block_arena_candidate(candidate, PRETRADE_UNAVAILABLE_GATE)
            warnings.append(f"{candidate.get('account_id')}:{symbol}: pretrade event-check unavailable; autonomous US entry blocked")
            blocked += 1
        elif verdict in {"RISK", "AVOID", "UNAVAILABLE"}:
            _block_arena_candidate(candidate, PRETRADE_RISK_GATE)
            warnings.append(f"{candidate.get('account_id')}:{symbol}: pretrade verdict {verdict}; autonomous US entry blocked")
            blocked += 1
        items.append(item)
    return {
        "status": "blocked" if blocked else "ok",
        "mode": "pretrade_check",
        "items": items,
        "blocked": blocked,
        "provider_call": provider_call,
        "broker_mutation": False,
    }


def _repeat_pattern_loss_settings(policy: dict[str, Any]) -> dict[str, Any]:
    defaults = DEFAULT_LEARNING_POLICY.get("repeat_pattern_loss")
    default_settings = defaults if isinstance(defaults, dict) else {}
    configured = learning_settings(policy).get("repeat_pattern_loss")
    settings = dict(default_settings)
    if isinstance(configured, dict):
        settings.update(configured)
    settings["account_ids"] = [str(item) for item in settings.get("account_ids") or []]
    settings["markets"] = [str(item).upper() for item in settings.get("markets") or []]
    settings["side"] = str(settings.get("side") or "BUY").upper()
    settings["exit_reasons"] = [str(item).upper() for item in settings.get("exit_reasons") or ["EXIT_WEAK"]]
    settings["scopes"] = _repeat_pattern_loss_scopes(settings)
    return settings


def _repeat_pattern_loss_scopes(settings: dict[str, Any]) -> list[dict[str, Any]]:
    raw_scopes = settings.get("scopes")
    scopes: list[dict[str, Any]] = []
    if isinstance(raw_scopes, list):
        for index, item in enumerate(raw_scopes, start=1):
            if not isinstance(item, dict):
                continue
            account_ids = [str(account_id) for account_id in item.get("account_ids") or [] if str(account_id)]
            markets = [str(market).upper() for market in item.get("markets") or [] if str(market)]
            if not account_ids or not markets:
                continue
            scopes.append(
                {
                    "name": str(item.get("name") or f"scope_{index}"),
                    "account_ids": account_ids,
                    "markets": markets,
                    "side": str(item.get("side") or settings.get("side") or "BUY").upper(),
                    "match_mode": str(item.get("match_mode") or "setup_signature"),
                    "cluster_penalty_max_pre_score": int(item["cluster_penalty_max_pre_score"])
                    if item.get("cluster_penalty_max_pre_score") is not None
                    else 90,
                }
            )
    if scopes:
        return scopes
    account_ids = [str(item) for item in settings.get("account_ids") or [] if str(item)]
    markets = [str(item).upper() for item in settings.get("markets") or [] if str(item)]
    if not account_ids or not markets:
        return []
    return [
        {
            "name": "legacy",
            "account_ids": account_ids,
            "markets": markets,
            "side": str(settings.get("side") or "BUY").upper(),
            "match_mode": "setup_signature",
            "cluster_penalty_max_pre_score": 90,
        }
    ]


def _repeat_pattern_loss_events(ledger: list[dict[str, Any]], policy: dict[str, Any], *, now: datetime) -> list[dict[str, Any]]:
    settings = _repeat_pattern_loss_settings(policy)
    buys: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    events: list[dict[str, Any]] = []
    for record in sorted([item for item in ledger if isinstance(item, dict)], key=lambda item: str(item.get("timestamp") or "")):
        account_id = str(record.get("account_id") or "")
        symbol = str(record.get("symbol") or "").upper()
        side = str(record.get("side") or "").upper()
        if not account_id or not symbol:
            continue
        buy_scope = _ledger_repeat_pattern_scope(record, settings, side=side)
        if buy_scope is not None:
            buys.setdefault((account_id, symbol, str(buy_scope.get("name") or "")), []).append(record)
            continue
        if side != "SELL" or str(record.get("action") or "").upper() not in set(settings.get("exit_reasons") or []):
            continue
        sell_scope = _ledger_repeat_pattern_scope(record, settings, side=str(settings.get("side") or "BUY").upper())
        if sell_scope is None:
            continue
        key = (account_id, symbol, str(sell_scope.get("name") or ""))
        if not buys.get(key):
            continue
        buy = buys[key].pop(0)
        event = _repeat_pattern_event_from_pair(buy, record, settings=settings, scope=sell_scope, now=now)
        if event is not None:
            events.append(event)
    return events


def _repeat_pattern_event_from_pair(
    buy: dict[str, Any],
    sell: dict[str, Any],
    *,
    settings: dict[str, Any],
    scope: dict[str, Any],
    now: datetime,
) -> dict[str, Any] | None:
    entry_time = _parse_datetime(buy.get("timestamp"))
    exit_time = _parse_datetime(sell.get("timestamp"))
    if entry_time is None or exit_time is None:
        return None
    hold_minutes = int((exit_time - entry_time).total_seconds() // 60)
    if hold_minutes < 0 or hold_minutes > int(settings.get("quick_exit_max_hold_minutes") or 240):
        return None
    entry_price = _decimal(buy.get("price"))
    exit_price = _decimal(sell.get("price"))
    quantity = min(_decimal(buy.get("quantity")) or Decimal("0"), _decimal(sell.get("quantity")) or Decimal("0"))
    if entry_price is None or exit_price is None or quantity <= 0:
        return None
    commission = (_decimal(buy.get("estimated_commission")) or Decimal("0")) + (_decimal(sell.get("estimated_commission")) or Decimal("0"))
    pnl = (exit_price - entry_price) * quantity - commission
    if pnl >= 0:
        return None
    cooldown_until = exit_time + timedelta(hours=int(settings.get("cooldown_hours") or 24))
    penalty_until = cooldown_until + timedelta(days=int(settings.get("penalty_trading_days") or 5))
    if now > penalty_until:
        return None
    account_id = str(buy.get("account_id") or sell.get("account_id") or "")
    symbol = str(buy.get("symbol") or sell.get("symbol") or "").upper()
    mic = _symbol_mic(symbol)
    scope_side = str(scope.get("side") or settings.get("side") or "BUY").upper()
    signature = str(buy.get("setup_signature") or _repeat_pattern_signature(account_id=account_id, mic=mic, side=scope_side))
    return {
        "account_id": account_id,
        "symbol": symbol,
        "market": mic,
        "scope": scope.get("name"),
        "setup_signature": signature,
        "entry_timestamp": entry_time.isoformat(timespec="seconds"),
        "exit_timestamp": exit_time.isoformat(timespec="seconds"),
        "hold_minutes": hold_minutes,
        "net_pnl_rub": _decimal_payload(pnl),
        "exit_reason": sell.get("action"),
        "cooldown_until": cooldown_until.isoformat(timespec="seconds"),
        "penalty_until": penalty_until.isoformat(timespec="seconds"),
    }


def _latest_repeat_pattern_event(events: list[dict[str, Any]], *, signature: str, now: datetime) -> dict[str, Any] | None:
    matches = [item for item in events if str(item.get("setup_signature") or "") == signature]
    active = [
        item
        for item in matches
        if (_parse_datetime(item.get("penalty_until")) is not None and now <= (_parse_datetime(item.get("penalty_until")) or now))
    ]
    if not active:
        return None
    return sorted(active, key=lambda item: str(item.get("exit_timestamp") or ""), reverse=True)[0]


def _latest_repeat_pattern_match(
    events: list[dict[str, Any]],
    candidate: dict[str, Any],
    *,
    settings: dict[str, Any],
    now: datetime,
) -> tuple[dict[str, Any] | None, str]:
    scope = _candidate_repeat_pattern_scope(candidate, settings)
    if not scope:
        return (None, "")
    match_mode = str(scope.get("match_mode") or "setup_signature")
    if match_mode != "symbol_then_cluster":
        signature = _candidate_repeat_pattern_signature(candidate)
        return (_latest_repeat_pattern_event(events, signature=signature, now=now), "setup_signature")

    active = [
        item
        for item in events
        if str(item.get("scope") or "") == str(scope.get("name") or "")
        and (_parse_datetime(item.get("penalty_until")) is not None and now <= (_parse_datetime(item.get("penalty_until")) or now))
    ]
    if not active:
        return (None, "")
    candidate_symbol = str(candidate.get("symbol") or "").upper()
    exact = [item for item in active if str(item.get("symbol") or "").upper() == candidate_symbol]
    if exact:
        return (sorted(exact, key=lambda item: str(item.get("exit_timestamp") or ""), reverse=True)[0], "symbol")
    candidate_market = _symbol_mic(candidate_symbol)
    cluster = [item for item in active if str(item.get("market") or "").upper() == candidate_market]
    if cluster:
        return (sorted(cluster, key=lambda item: str(item.get("exit_timestamp") or ""), reverse=True)[0], "cluster")
    return (None, "")


def _repeat_pattern_candidate_payload(
    event: dict[str, Any],
    *,
    now: datetime,
    candidate: dict[str, Any] | None = None,
    match_mode: str = "setup_signature",
) -> dict[str, Any]:
    cooldown_until = _parse_datetime(event.get("cooldown_until"))
    action = "cooldown" if match_mode != "cluster" and cooldown_until is not None and now <= cooldown_until else "penalty"
    return {
        "action": action,
        "source_symbol": event.get("symbol"),
        "candidate_symbol": candidate.get("symbol") if isinstance(candidate, dict) else None,
        "match_mode": match_mode,
        "cluster": event.get("market"),
        "setup_signature": event.get("setup_signature"),
        "source_exit_timestamp": event.get("exit_timestamp"),
        "source_hold_minutes": event.get("hold_minutes"),
        "source_net_pnl_rub": event.get("net_pnl_rub"),
        "cooldown_until": event.get("cooldown_until"),
        "penalty_until": event.get("penalty_until"),
    }


def _candidate_in_repeat_pattern_scope(candidate: dict[str, Any], settings: dict[str, Any]) -> bool:
    return _candidate_repeat_pattern_scope(candidate, settings) is not None


def _candidate_repeat_pattern_scope(candidate: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any] | None:
    return _repeat_pattern_scope_for(
        settings,
        account_id=str(candidate.get("account_id") or ""),
        symbol=str(candidate.get("symbol") or ""),
        side=str(candidate.get("side") or "").upper(),
    )


def _ledger_repeat_pattern_scope(record: dict[str, Any], settings: dict[str, Any], *, side: str) -> dict[str, Any] | None:
    return _repeat_pattern_scope_for(
        settings,
        account_id=str(record.get("account_id") or ""),
        symbol=str(record.get("symbol") or ""),
        side=side,
    )


def _repeat_pattern_scope_for(settings: dict[str, Any], *, account_id: str, symbol: str, side: str) -> dict[str, Any] | None:
    mic = _symbol_mic(symbol)
    normalized_side = str(side or "").upper()
    for scope in settings.get("scopes") or []:
        if not isinstance(scope, dict):
            continue
        if str(account_id) not in {str(item) for item in scope.get("account_ids") or []}:
            continue
        if mic not in {str(item).upper() for item in scope.get("markets") or []}:
            continue
        if normalized_side != str(scope.get("side") or settings.get("side") or "BUY").upper():
            continue
        return scope
    return None


def _candidate_repeat_pattern_signature(candidate: dict[str, Any]) -> str:
    existing = str(candidate.get("setup_signature") or "")
    if existing:
        return existing
    return _repeat_pattern_signature(
        account_id=str(candidate.get("account_id") or ""),
        mic=_symbol_mic(str(candidate.get("symbol") or "")),
        side=str(candidate.get("side") or "BUY").upper(),
    )


def _repeat_pattern_signature(*, account_id: str, mic: str, side: str) -> str:
    return f"{account_id}|{mic}|{side}|H4|H1_M30_TECHNICAL"


def _symbol_mic(symbol: str) -> str:
    parts = str(symbol or "").upper().split("@", 1)
    return parts[1] if len(parts) == 2 else ""


def _apply_repeat_pattern_score_penalty(candidate: dict[str, Any], *, penalty: int) -> None:
    _apply_learning_candidate_penalty(candidate, component="repeat_pattern_loss", flag="repeat_pattern_loss", penalty=penalty)


def _arena_pretrade_candidates(candidates: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    research_policy = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    account_ids = {
        str(item)
        for item in research_policy.get("pretrade_account_ids", ["DEMO-US"])
    }
    markets = {str(item).upper() for item in research_policy.get("pretrade_markets", ["XNGS"])}
    by_account: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("execution_allowed") is not True:
            continue
        account_id = str(candidate.get("account_id") or "")
        if account_id not in account_ids or str(candidate.get("side") or "").upper() != "BUY":
            continue
        if _symbol_mic(str(candidate.get("symbol") or "")) not in markets:
            continue
        previous = by_account.get(account_id)
        if previous is None or _arena_candidate_score(candidate) > _arena_candidate_score(previous):
            by_account[account_id] = candidate
    return list(by_account.values())


ARENA_NEAR_EXECUTABLE_RESEARCH_GATES = {
    "research_unavailable_below_exceptional_score",
    "candidate_score_below_min",
    "entry_strength_confirmation_missing",
}


def _arena_margin_disabled_short_candidate(candidate: dict[str, Any]) -> bool:
    reasons = {str(item) for item in candidate.get("gate_reasons") or []}
    return str(candidate.get("side") or "").upper() == "SELL" and ARENA_MARGIN_TRADING_NOT_SUPPORTED in reasons


def _arena_research_candidates(candidates: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    executable = _arena_executable_research_candidates(candidates, policy)
    if executable:
        return executable
    research_policy = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    limit = _positive_int(research_policy.get("h4_max_candidates"), default=2)
    pool = [
        item
        for item in candidates
        if isinstance(item, dict) and not _arena_margin_disabled_short_candidate(item)
    ]
    return pool[:limit]


def _arena_executable_research_candidates(candidates: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    research_policy = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    scan_limit = _positive_int(
        research_policy.get("arena_h4_max_symbols_per_scan") or research_policy.get("h4_max_candidates"),
        default=2,
    )
    per_account_limit = _positive_int(research_policy.get("arena_h4_max_symbols_per_account"), default=1)
    selected: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    account_counts: dict[str, int] = {}
    for item in candidates:
        if not isinstance(item, dict) or item.get("execution_allowed") is not True:
            continue
        account_id = str(item.get("account_id") or "")
        if account_id and account_counts.get(account_id, 0) >= per_account_limit:
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol or symbol in seen_symbols:
            continue
        selected.append(item)
        seen_symbols.add(symbol)
        if account_id:
            account_counts[account_id] = account_counts.get(account_id, 0) + 1
        if len(selected) >= scan_limit:
            break
    return selected


def _arena_near_executable_research_candidates(candidates: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    research_policy = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    scan_limit = _positive_int(
        research_policy.get("arena_h4_max_symbols_per_scan") or research_policy.get("h4_max_candidates"),
        default=2,
    )
    per_account_limit = _positive_int(research_policy.get("arena_h4_max_symbols_per_account"), default=1)
    selected: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    account_counts: dict[str, int] = {}
    pool = [item for item in candidates if isinstance(item, dict)]
    for item in sorted(pool, key=_arena_candidate_sort_score, reverse=True):
        if item.get("execution_allowed") is True or str(item.get("side") or "").upper() != "BUY":
            continue
        gates = {str(gate) for gate in item.get("gate_reasons") or [] if str(gate)}
        if not gates or not gates.issubset(ARENA_NEAR_EXECUTABLE_RESEARCH_GATES):
            continue
        account_id = str(item.get("account_id") or "")
        if account_id and account_counts.get(account_id, 0) >= per_account_limit:
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol or symbol in seen_symbols:
            continue
        min_score = _min_candidate_score_for_buy(policy, account_id=account_id)
        if _arena_candidate_sort_score(item) < max(60, min_score - 15):
            continue
        selected.append(item)
        seen_symbols.add(symbol)
        if account_id:
            account_counts[account_id] = account_counts.get(account_id, 0) + 1
        if len(selected) >= scan_limit:
            break
    return selected


def _arena_candidate_sort_score(candidate: dict[str, Any]) -> int:
    score_payload = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    raw = score_payload.get("score", candidate.get("score"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _arena_research_item_verdict_symbols(
    research: dict[str, Any],
    verdict: str,
) -> tuple[set[str], set[str]]:
    matched: set[str] = set()
    item_symbols: set[str] = set()
    for item in research.get("items") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        item_symbols.add(symbol)
        if str(item.get("verdict") or "").upper() == verdict:
            matched.add(symbol)
    return matched, item_symbols


def _arena_research_avoid_symbols(research: dict[str, Any]) -> set[str]:
    avoid, item_symbols = _arena_research_item_verdict_symbols(research, "AVOID")
    if str(research.get("verdict") or "").upper() == "AVOID":
        for symbol in research.get("symbols") or []:
            normalized = str(symbol).strip()
            if normalized and normalized not in item_symbols:
                avoid.add(normalized)
    return avoid


def _arena_research_risk_symbols(research: dict[str, Any]) -> set[str]:
    risky, item_symbols = _arena_research_item_verdict_symbols(research, "RISK")
    if str(research.get("verdict") or "").upper() == "RISK":
        for symbol in research.get("symbols") or []:
            normalized = str(symbol).strip()
            if normalized and normalized not in item_symbols:
                risky.add(normalized)
    return risky


def _arena_auction_risk_exit_opportunities(portfolio_review: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in portfolio_review.get("exit_proposals") or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "")
        score = {
            "EXIT_WEAK": 95,
            "TRIM_OVEREXPOSURE": 90,
            "TAKE_PARTIAL_PROFIT": 85,
            "BREAKEVEN_STOP": 82,
            "TRAIL_STOP": 80,
        }.get(action, 80)
        result.append(
            _arena_opportunity(
                opportunity_type="risk_exit",
                account_id=str(item.get("account_id") or ""),
                symbol=str(item.get("symbol") or ""),
                score=score,
                action_cost=0,
                slot_cost=0,
                capital_cost_pct=None,
                gate_reasons=[],
                decision="shadow_winner_candidate",
                reason=str(item.get("reason") or action or "risk_exit"),
                source=item,
                priority=400,
            )
        )
    return result


def _arena_auction_replacement_opportunities(
    portfolio_review: dict[str, Any],
    *,
    accounts: dict[str, dict[str, Any]],
    portfolio: dict[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in portfolio_review.get("replacement_proposals") or []:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("account_id") or "")
        buy_candidate = item.get("buy_candidate") if isinstance(item.get("buy_candidate"), dict) else {}
        score = int(item.get("candidate_score") or _arena_candidate_score(buy_candidate))
        gate_reasons: list[str] = []
        quote_age = _positive_int_or_none(buy_candidate.get("quote_age_seconds"))
        max_quote_age = _positive_int(portfolio.get("opportunity_max_quote_age_seconds"), default=60)
        if quote_age is None:
            gate_reasons.append("quote_age_unknown_manual_review")
        elif quote_age > max_quote_age:
            gate_reasons.append("stale_quote_manual_review")
        equity = _decimal((accounts.get(account_id) or {}).get("equity")) or ARENA_STARTING_EQUITY
        estimated_cost = _decimal(item.get("estimated_cost_rub"))
        cost_pct = (estimated_cost / equity * Decimal("100")) if estimated_cost is not None and equity > 0 else None
        max_cost_pct = _decimal(portfolio.get("opportunity_max_replacement_turnover_cost_pct")) or Decimal("0.50")
        if estimated_cost is None:
            gate_reasons.append("estimated_turnover_cost_unknown_manual_review")
        elif cost_pct is not None and cost_pct > max_cost_pct:
            gate_reasons.append("estimated_turnover_cost_too_high")
        min_edge = _decimal(portfolio.get("min_replacement_edge_after_cost_pct")) or Decimal("0")
        edge_after_cost = _decimal(item.get("edge_after_cost_pct"))
        if edge_after_cost is None or edge_after_cost < min_edge:
            gate_reasons.append("min_delta_after_cost_not_met")
        decision = "manual_review" if gate_reasons else "shadow_winner_candidate"
        result.append(
            _arena_opportunity(
                opportunity_type="replacement",
                account_id=account_id,
                symbol=str(item.get("buy_symbol") or ""),
                score=score,
                action_cost=-5 if gate_reasons else 0,
                slot_cost=1,
                capital_cost_pct=_decimal_payload(cost_pct),
                gate_reasons=gate_reasons,
                decision=decision,
                reason=";".join(gate_reasons) if gate_reasons else "replacement_edge_after_cost",
                source=item | {"slot_kind": "replacement"},
                priority=300,
            )
        )
    return result


def _arena_auction_base_entry_opportunities(
    scan: dict[str, Any],
    *,
    accounts: dict[str, dict[str, Any]],
    portfolio: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    research_unavailable = _arena_scan_research_unavailable_for_live(scan)
    for candidate in scan.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        account_id = str(candidate.get("account_id") or "")
        score = _arena_candidate_score(candidate)
        gate_reasons = [str(item) for item in candidate.get("gate_reasons") or []]
        if candidate.get("execution_allowed") is True and research_unavailable:
            gate_reasons.insert(0, "research_unavailable_shadow_only")
        account = accounts.get(account_id) or {}
        reserve_reason = _arena_optionality_reserve_reason(candidate, account=account, portfolio=portfolio, now=now)
        action_cost = 0
        decision = "shadow_winner_candidate" if candidate.get("execution_allowed") is True and not gate_reasons else "manual_review"
        reason = "base_entry_candidate"
        if gate_reasons:
            reason = ";".join(gate_reasons)
        if reserve_reason:
            gate_reasons.append(reserve_reason)
            action_cost -= 20
            decision = "reserve_hold"
            reason = reserve_reason
        result.append(
            _arena_opportunity(
                opportunity_type="base_entry",
                account_id=account_id,
                symbol=str(candidate.get("symbol") or ""),
                score=score,
                action_cost=action_cost,
                slot_cost=1,
                capital_cost_pct=_arena_candidate_capital_cost_pct(candidate, account),
                gate_reasons=gate_reasons,
                decision=decision,
                reason=reason,
                source=candidate | {"slot_kind": "primary"},
                priority=200,
            )
        )
    return result


def _arena_auction_event_opportunities(
    event_candidates: list[dict[str, Any]],
    *,
    accounts: dict[str, dict[str, Any]],
    scan: dict[str, Any],
    portfolio: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in event_candidates:
        if not isinstance(event, dict):
            continue
        account_ids = [str(item) for item in event.get("account_ids") or [] if str(item)]
        if not account_ids and event.get("account_id"):
            account_ids = [str(event.get("account_id"))]
        if not account_ids:
            account_ids = _arena_event_account_ids(event, scan=scan)
        for account_id in account_ids:
            account = accounts.get(account_id) or {}
            score = int(_decimal(event.get("score")) or Decimal("0"))
            gate_reasons = _arena_event_gate_reasons(event, portfolio=portfolio)
            if _arena_scan_research_unavailable_for_live(scan):
                gate_reasons.insert(0, "research_unavailable_shadow_only")
            reserve_reason = _arena_optionality_reserve_reason(event, account=account, portfolio=portfolio, now=now)
            if reserve_reason:
                gate_reasons.append(reserve_reason)
            decision = "event_watch_only" if gate_reasons else "shadow_winner_candidate"
            result.append(
                _arena_opportunity(
                    opportunity_type="event_entry",
                    account_id=account_id,
                    symbol=str(event.get("symbol") or ""),
                    score=score,
                    action_cost=-25 if gate_reasons else 0,
                    slot_cost=1,
                    capital_cost_pct=event.get("capital_cost_pct"),
                    gate_reasons=gate_reasons,
                    decision=decision,
                    reason=";".join(gate_reasons) if gate_reasons else "event_candidate",
                    source=event | {"slot_kind": "primary"},
                    priority=200,
                )
            )
    return result


def _arena_auction_hold_cash_opportunities(
    accounts: dict[str, dict[str, Any]],
    *,
    review_accounts: dict[str, dict[str, Any]],
    portfolio: dict[str, Any],
) -> list[dict[str, Any]]:
    hold_score = int(portfolio.get("opportunity_hold_cash_score") or 60)
    account_ids = sorted(set(accounts) | set(review_accounts))
    if not account_ids:
        account_ids = [""]
    return [
        _arena_opportunity(
            opportunity_type="hold_cash",
            account_id=account_id,
            symbol="",
            score=hold_score,
            action_cost=0,
            slot_cost=0,
            capital_cost_pct="0",
            gate_reasons=[],
            decision="shadow_winner_candidate",
            reason="no_action_beats_hold_cash",
            source={},
            priority=100,
        )
        for account_id in account_ids
    ]


def _arena_opportunity(
    *,
    opportunity_type: str,
    account_id: str,
    symbol: str,
    score: int,
    action_cost: int,
    slot_cost: int,
    capital_cost_pct: Any,
    gate_reasons: list[str],
    decision: str,
    reason: str,
    source: dict[str, Any],
    priority: int,
) -> dict[str, Any]:
    adjusted = max(0, min(100, int(score) + int(action_cost)))
    return {
        "opportunity_type": opportunity_type,
        "account_id": account_id,
        "symbol": symbol,
        "score": int(score),
        "risk_adjusted_score": adjusted,
        "action_cost": int(action_cost),
        "slot_cost": int(slot_cost),
        "capital_cost_pct": capital_cost_pct,
        "gate_reasons": gate_reasons,
        "decision": decision,
        "reason": reason,
        "broker_mutation": False,
        "priority": priority,
        "source": source,
    }


def _arena_auction_winner(opportunities: list[dict[str, Any]], *, portfolio: dict[str, Any]) -> dict[str, Any]:
    if not opportunities:
        return _arena_opportunity(
            opportunity_type="hold_cash",
            account_id="",
            symbol="",
            score=int(portfolio.get("opportunity_hold_cash_score") or 60),
            action_cost=0,
            slot_cost=0,
            capital_cost_pct="0",
            gate_reasons=[],
            decision="shadow_winner_candidate",
            reason="no_opportunities",
            source={},
            priority=100,
        )
    risk_exits = [item for item in opportunities if item.get("opportunity_type") == "risk_exit"]
    if risk_exits:
        return max(risk_exits, key=lambda item: int(item.get("risk_adjusted_score") or 0))
    min_score = int(portfolio.get("opportunity_min_score") or portfolio.get("min_candidate_score_for_buy") or 75)
    executable = [
        item
        for item in opportunities
        if item.get("opportunity_type") != "hold_cash"
        and item.get("decision") == "shadow_winner_candidate"
        and int(item.get("risk_adjusted_score") or 0) >= min_score
    ]
    if executable:
        return max(executable, key=lambda item: (int(item.get("priority") or 0), int(item.get("risk_adjusted_score") or 0)))
    holds = [item for item in opportunities if item.get("opportunity_type") == "hold_cash"]
    if holds:
        hold = max(holds, key=lambda item: int(item.get("risk_adjusted_score") or 0))
        return hold | {"reason": "best_opportunity_below_threshold_or_manual_review"}
    return max(opportunities, key=lambda item: (int(item.get("priority") or 0), int(item.get("risk_adjusted_score") or 0)))


def _arena_same_opportunity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("opportunity_type", "account_id", "symbol", "decision")
    return all(left.get(key) == right.get(key) for key in keys)


def _arena_compact_auction_winner(winner: dict[str, Any]) -> dict[str, Any]:
    keys = ("opportunity_type", "account_id", "symbol", "risk_adjusted_score", "decision", "reason")
    return {key: winner.get(key) for key in keys}


def _arena_current_contour_action(scan: dict[str, Any], portfolio_review: dict[str, Any]) -> dict[str, Any]:
    actions = [item for item in portfolio_review.get("planned_actions") or [] if isinstance(item, dict)]
    if actions:
        return {"source": "portfolio_review", **actions[0]}
    for candidate in scan.get("candidates") or []:
        if isinstance(candidate, dict) and candidate.get("execution_allowed") is True:
            return {
                "source": "arena_scan",
                "action": "BUY" if str(candidate.get("side") or "BUY").upper() == "BUY" else str(candidate.get("side")),
                "account_id": candidate.get("account_id"),
                "symbol": candidate.get("symbol"),
                "arena_growth_score": candidate.get("arena_growth_score"),
            }
    return {"source": "arena_scan", "action": "HOLD", "reason": "no_executable_candidate"}


def _arena_optionality_reserve_reason(
    candidate: dict[str, Any],
    *,
    account: dict[str, Any],
    portfolio: dict[str, Any],
    now: datetime,
) -> str | None:
    if str(candidate.get("side") or "BUY").upper() != "BUY":
        return None
    if not _arena_symbol_session_active(str(candidate.get("symbol") or ""), now=now):
        return None
    entry_limits = account.get("entry_limits") if isinstance(account.get("entry_limits"), dict) else {}
    primary_limit = int(entry_limits.get("primary_limit") or 0)
    primary_used = int(entry_limits.get("primary_used") or 0)
    if primary_limit <= 0 or primary_used < primary_limit - 1:
        return None
    exceptional_score = int(portfolio.get("opportunity_exceptional_base_score") or 84)
    if _arena_candidate_score(candidate) >= exceptional_score:
        return None
    if now.astimezone(ARENA_LIMITS_TIMEZONE).time() < _arena_reserve_cutoff_time(portfolio):
        return "optionality_reserve_last_primary_slot"
    return None


def _arena_reserve_cutoff_time(portfolio: dict[str, Any]) -> Any:
    text = str(portfolio.get("opportunity_reserve_until_msk") or "19:00")
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        parsed = datetime.strptime("19:00", "%H:%M")
    return parsed.time()


def _arena_symbol_session_active(symbol: str, *, now: datetime) -> bool:
    mic = _symbol_mic(symbol)
    local = now.astimezone(ARENA_LIMITS_TIMEZONE)
    if mic in {"MISX", "RUSX", "RTSX"}:
        return local.weekday() < 5 and 10 <= local.hour < 19
    if mic in {"XNGS", "XNYS", "XNAS", "XNMS", "XNCM", "XASE", "PINX", "XCME", "XNYM"}:
        return local.weekday() < 5 and 16 <= local.hour < 23
    return False


def _arena_candidate_capital_cost_pct(candidate: dict[str, Any], account: dict[str, Any]) -> str | None:
    equity = _decimal(account.get("equity"))
    notional = _decimal(candidate.get("notional"))
    if equity is None or equity <= 0 or notional is None:
        return None
    return _decimal_payload(notional / equity * Decimal("100"))


def _arena_event_account_ids(event: dict[str, Any], *, scan: dict[str, Any]) -> list[str]:
    symbol = str(event.get("symbol") or "").upper()
    ids: list[str] = []
    for candidate in scan.get("candidates") or []:
        if isinstance(candidate, dict) and str(candidate.get("symbol") or "").upper() == symbol:
            account_id = str(candidate.get("account_id") or "")
            if account_id and account_id not in ids:
                ids.append(account_id)
    return ids


def _arena_event_gate_reasons(event: dict[str, Any], *, portfolio: dict[str, Any]) -> list[str]:
    reasons: list[str] = [str(item) for item in event.get("gate_reasons") or []]
    research_verdict = str(event.get("research_verdict") or event.get("verdict") or "").upper()
    if research_verdict and research_verdict != "OK":
        reasons.append("event_research_not_ok")
    elif not research_verdict:
        reasons.append("event_unclassified_manual_review")
    gap_pct = _decimal(event.get("gap_pct"))
    overheated = _decimal(portfolio.get("event_overheated_gap_pct")) or Decimal("5.0")
    rr_after_gap = _decimal(event.get("rr_after_gap"))
    min_rr = _decimal(portfolio.get("event_min_rr_after_gap")) or Decimal("1.5")
    if gap_pct is not None and gap_pct >= overheated and (rr_after_gap is None or rr_after_gap < min_rr):
        reasons.append("event_gap_overheated_without_rr")
    if rr_after_gap is not None and rr_after_gap < min_rr:
        reasons.append("event_rr_after_gap_too_low")
    if event.get("volume_confirmed") is False:
        reasons.append("event_volume_unconfirmed")
    if event.get("level_hold_confirmed") is False and event.get("retest_confirmed") is False:
        reasons.append("event_level_hold_or_retest_unconfirmed")
    return list(dict.fromkeys(reasons))


def _arena_scan_research_unavailable_for_live(scan: dict[str, Any]) -> bool:
    research = scan.get("research") if isinstance(scan.get("research"), dict) else {}
    status = str(research.get("status") or "").lower()
    if status == "ok":
        return False
    if status in {"disabled"}:
        return False
    return bool(scan.get("candidates"))


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _apply_arena_account_candidate_signals(accounts_by_id: dict[str, dict[str, Any]], candidates: list[dict[str, Any]]) -> None:
    by_account: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        account_id = str(candidate.get("account_id") or "")
        if account_id:
            by_account.setdefault(account_id, []).append(candidate)
    for account_id, account_candidates in by_account.items():
        account = accounts_by_id.get(account_id)
        if not account:
            continue
        executable = next((item for item in account_candidates if item.get("execution_allowed") is True), None)
        if executable is not None:
            account["top_signal"] = _candidate_signal_line(executable)
            continue
        account["top_signal"] = _candidate_block_reason_line(account_candidates[0])
        if account.get("status") == "ACCOUNT_ACTIVE":
            account["status"] = "WATCH"


def _block_arena_candidate(candidate: dict[str, Any], reason: str) -> None:
    candidate["execution_allowed"] = False
    reasons = candidate.get("gate_reasons") if isinstance(candidate.get("gate_reasons"), list) else []
    if reason not in reasons:
        if reason.startswith("research_"):
            reasons.insert(0, reason)
        else:
            reasons.append(reason)
    candidate["gate_reasons"] = reasons


def _enrich_arena_position_prices(
    accounts_by_id: dict[str, dict[str, Any]],
    *,
    market_client: FinamClient,
    market_jwt: str,
    market_data_cache: _ArenaMarketDataCache,
    warnings: list[str],
) -> None:
    for account_id, account in accounts_by_id.items():
        positions = account.get("positions") if isinstance(account.get("positions"), list) else []
        for position in positions:
            if not isinstance(position, dict) or _decimal(position.get("current_price")) is not None:
                continue
            symbol = str(position.get("symbol") or "").strip()
            if not symbol:
                continue
            try:
                quote = market_data_cache.quote(market_client, market_jwt, symbol)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{account_id}:{symbol}: position price unavailable: {exc}")
                continue
            price = _quote_price(quote)
            if price is not None:
                position["current_price"] = _decimal_payload(price)


def _arena_long_entry_gate_reasons(
    account: dict[str, Any],
    *,
    symbol: str,
    notional: Decimal,
    equity: Decimal,
    risk: dict[str, Any],
    now: datetime,
) -> list[str]:
    reasons: list[str] = []
    current_gross = _account_gross_notional(account)
    current_symbol = _account_symbol_notional(account, symbol)
    account_id = str(account.get("account_id") or "")
    max_account_pct = _arena_risk_limit_pct(risk, "max_account_gross_exposure_pct", account_id=account_id, symbol=symbol, default="100")
    max_symbol_pct = _arena_risk_limit_pct(risk, "max_symbol_exposure_pct", account_id=account_id, symbol=symbol, default="100")
    max_daily_notional_pct = _arena_risk_limit_pct(risk, "max_new_notional_pct_per_day", account_id=account_id, symbol=symbol, default="100")
    same_symbol_scale_in = bool(risk.get("allow_same_symbol_scale_in", False))

    if equity > 0:
        if current_gross / equity * Decimal("100") >= max_account_pct:
            reasons.append("account_gross_exposure_limit_reached")
        if (current_symbol + notional) / equity * Decimal("100") > max_symbol_pct:
            reasons.append("symbol_exposure_limit_exceeded")
        new_notional_today = _daily_trade_notional(account, now=now, side="BUY")
        if (new_notional_today + notional) / equity * Decimal("100") > max_daily_notional_pct:
            reasons.append("daily_new_notional_limit_exceeded")
    if current_symbol > 0 and not same_symbol_scale_in:
        reasons.append("same_symbol_position_open")
    if _daily_trade_count(account, now=now, side="BUY", symbol=symbol) > 0:
        reasons.append("same_symbol_trade_today")
    return reasons


def _account_gross_notional(account: dict[str, Any]) -> Decimal:
    total = Decimal("0")
    for position in account.get("positions") or []:
        if isinstance(position, dict):
            total += _position_notional(position)
    return total


def _account_symbol_notional(account: dict[str, Any], symbol: str) -> Decimal:
    total = Decimal("0")
    normalized = symbol.upper()
    for position in account.get("positions") or []:
        if not isinstance(position, dict):
            continue
        if str(position.get("symbol") or "").upper() == normalized:
            total += _position_notional(position)
    return total


def _position_notional(position: dict[str, Any]) -> Decimal:
    quantity = _decimal(position.get("quantity")) or Decimal("0")
    price = _decimal(position.get("current_price")) or _decimal(position.get("average_price")) or Decimal("0")
    return abs(quantity) * price


def _daily_trade_count(
    account: dict[str, Any],
    *,
    now: datetime,
    side: str,
    symbol: str | None = None,
    day_tz: timezone | ZoneInfo = timezone.utc,
) -> int:
    return len(_daily_trades(account, now=now, side=side, symbol=symbol, day_tz=day_tz))


def _daily_trade_notional(account: dict[str, Any], *, now: datetime, side: str, symbol: str | None = None) -> Decimal:
    total = Decimal("0")
    for trade in _daily_trades(account, now=now, side=side, symbol=symbol):
        quantity = _decimal(trade.get("quantity"))
        price = _decimal(trade.get("price"))
        if quantity is not None and price is not None:
            total += abs(quantity) * price
    return total


def _daily_trades(
    account: dict[str, Any],
    *,
    now: datetime,
    side: str,
    symbol: str | None = None,
    day_tz: timezone | ZoneInfo = timezone.utc,
) -> list[dict[str, Any]]:
    normalized_side = side.upper()
    normalized_symbol = symbol.upper() if symbol else None
    result: list[dict[str, Any]] = []
    trades_source = account.get("daily_trades") if isinstance(account.get("daily_trades"), list) else account.get("recent_trades")
    for trade in trades_source or []:
        if not isinstance(trade, dict):
            continue
        trade_side = str(trade.get("side") or "").upper()
        if normalized_side != "ANY":
            if normalized_side == "BUY" and "BUY" not in trade_side:
                continue
            if normalized_side == "SELL" and "SELL" not in trade_side:
                continue
        trade_symbol = str(trade.get("symbol") or "").upper()
        if normalized_symbol and trade_symbol != normalized_symbol:
            continue
        trade_time = _parse_datetime(trade.get("time"))
        if trade_time is None or trade_time.astimezone(day_tz).date() != now.astimezone(day_tz).date():
            continue
        result.append(trade)
    return result


def _trades_on_day(
    trades: list[dict[str, Any]],
    *,
    now: datetime,
    day_tz: timezone | ZoneInfo = timezone.utc,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for trade in trades:
        trade_time = _parse_datetime(trade.get("time"))
        if trade_time is not None and trade_time.astimezone(day_tz).date() == now.astimezone(day_tz).date():
            result.append(trade)
    return result


def _daily_replacement_entry_count(ledger: list[dict[str, Any]], *, account_id: str, now: datetime) -> int:
    count = 0
    for item in ledger:
        if not isinstance(item, dict):
            continue
        if str(item.get("account_id") or "") != account_id:
            continue
        if str(item.get("status") or "") != "EXECUTED_ARENA_REPLACEMENT":
            continue
        timestamp = _parse_datetime(item.get("timestamp"))
        if timestamp is None or timestamp.astimezone(ARENA_LIMITS_TIMEZONE).date() != now.astimezone(
            ARENA_LIMITS_TIMEZONE
        ).date():
            continue
        count += 1
    return count


def _arena_ledger_has_errors(ledger: list[dict[str, Any]]) -> bool:
    return any(isinstance(item, dict) and item.get("source") == "arena_execution_ledger_error" for item in ledger)


def _arena_trade_costs(notional: Decimal, *, symbol: str, fees: dict[str, Any]) -> dict[str, Any]:
    commission_pct = _arena_commission_pct(symbol, fees=fees)
    slippage_pct = _decimal(fees.get("slippage_pct_per_side")) or Decimal("0")
    one_side_pct = commission_pct + slippage_pct
    round_trip_pct = one_side_pct * Decimal("2")
    return {
        "commission_pct_per_side": _decimal_payload(commission_pct),
        "slippage_pct_per_side": _decimal_payload(slippage_pct),
        "entry_cost_rub": _decimal_payload(notional * one_side_pct / Decimal("100")),
        "round_trip_cost_rub": _decimal_payload(notional * round_trip_pct / Decimal("100")),
        "break_even_move_pct": _decimal_payload(round_trip_pct),
    }


def _arena_commission_pct(symbol: str, *, fees: dict[str, Any]) -> Decimal:
    mic = _symbol_mic(symbol)
    by_mic = fees.get("commission_pct_by_mic") if isinstance(fees.get("commission_pct_by_mic"), dict) else {}
    return _decimal(by_mic.get(mic)) or _decimal(fees.get("commission_pct_per_side")) or Decimal("0")


def _symbol_mic(symbol: str) -> str:
    if "@" not in symbol:
        return ""
    return symbol.rsplit("@", maxsplit=1)[-1].upper()


def _account_gross_exposure_pct(account: dict[str, Any]) -> Decimal | None:
    equity = _decimal(account.get("equity"))
    if equity is None or equity <= 0:
        return None
    return _account_gross_notional(account) / equity * Decimal("100")


def _arena_position_review(
    position: dict[str, Any],
    *,
    account: dict[str, Any],
    portfolio: dict[str, Any],
    fees: dict[str, Any],
    research: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    symbol = str(position.get("symbol") or "")
    side = str(position.get("side") or "LONG").upper()
    quantity = _decimal(position.get("quantity")) or Decimal("0")
    average = _decimal(position.get("average_price"))
    current = _decimal(position.get("current_price"))
    stop = _position_stop_price(position, account)
    progress_r = _position_progress_r(side=side, average=average, current=current, stop=stop)
    verdict = _arena_research_verdict_for_symbol(research, symbol)
    holding_score_payload = _arena_holding_score(position, progress_r=progress_r, current=current, stop=stop, fees=fees, research_verdict=verdict)
    holding_score = int(holding_score_payload["score"])
    position_age_minutes = _position_age_minutes(position, account, now=now or datetime.now(timezone.utc))
    adverse_move_pct = _position_adverse_move_pct(side=side, average=average, current=current)
    round_trip_cost_pct = _position_round_trip_cost_pct(symbol, fees=fees)
    action, reason = _arena_position_action(
        progress_r,
        current=current,
        portfolio=portfolio,
        holding_score=holding_score,
        research_verdict=verdict,
        position_age_minutes=position_age_minutes,
        adverse_move_pct=adverse_move_pct,
        round_trip_cost_pct=round_trip_cost_pct,
    )
    learning_deprioritized = symbol.upper() in {str(item).upper() for item in portfolio.get("learning_deprioritize_symbols") or []}
    learning_attribution_uncertain = symbol.upper() in {str(item).upper() for item in portfolio.get("learning_attribution_uncertain_symbols") or []}
    if action == "HOLD" and learning_deprioritized and progress_r is not None and progress_r < 0:
        threshold = _decimal(portfolio.get("learning_deprioritized_exit_negative_r_threshold")) or Decimal("-0.10")
        noise_buffer = _decimal(portfolio.get("weak_exit_noise_buffer_pct")) or Decimal("0")
        cost_noise_threshold = round_trip_cost_pct + noise_buffer
        if progress_r <= threshold or (adverse_move_pct is not None and adverse_move_pct >= cost_noise_threshold):
            action = "EXIT_WEAK"
            reason = "learning_deprioritized_negative_r_progress"
    return {
        "symbol": symbol,
        "side": side,
        "quantity": _decimal_payload(quantity),
        "average_price": _decimal_payload(average),
        "current_price": _decimal_payload(current),
        "stop_price": _decimal_payload(stop),
        "progress_r": _decimal_payload(progress_r),
        "holding_score": holding_score,
        "holding_score_detail": holding_score_payload,
        "learning_deprioritized": learning_deprioritized,
        "learning_attribution_uncertain": learning_attribution_uncertain,
        "research_verdict": verdict,
        "position_age_minutes": position_age_minutes,
        "adverse_move_pct": _decimal_payload(adverse_move_pct),
        "weak_exit_policy": _weak_exit_policy_payload(portfolio, round_trip_cost_pct=round_trip_cost_pct),
        "action": action,
        "reason": reason,
        "broker_mutation": False,
        "confirmation_required": action in {"TAKE_PARTIAL_PROFIT", "TRIM_OVEREXPOSURE", "EXIT_WEAK"},
        "confirmation_phrase": f"CONFIRM_ARENA_EXIT {symbol} {account.get('account_id')}" if action in {"TAKE_PARTIAL_PROFIT", "TRIM_OVEREXPOSURE", "EXIT_WEAK"} else None,
    }


def _position_stop_price(position: dict[str, Any], account: dict[str, Any]) -> Decimal | None:
    symbol = str(position.get("symbol") or "")
    for stop in account.get("stop_orders") or []:
        if isinstance(stop, dict) and str(stop.get("symbol") or "") == symbol:
            return _decimal(stop.get("stop_price"))
    return None


def _position_progress_r(*, side: str, average: Decimal | None, current: Decimal | None, stop: Decimal | None) -> Decimal | None:
    if average is None or current is None or stop is None:
        return None
    if side == "SHORT":
        risk = stop - average
        pnl = average - current
    else:
        risk = average - stop
        pnl = current - average
    if risk <= 0:
        return None
    return pnl / risk


def _position_age_minutes(position: dict[str, Any], account: dict[str, Any], *, now: datetime) -> int | None:
    opened_at = _position_opened_at(position, account)
    if opened_at is None:
        return None
    age_seconds = (now.astimezone(timezone.utc) - opened_at.astimezone(timezone.utc)).total_seconds()
    if age_seconds < 0:
        return 0
    return int(age_seconds // 60)


def _position_opened_at(position: dict[str, Any], account: dict[str, Any]) -> datetime | None:
    for key in ("opened_at", "open_time", "created_at", "time", "timestamp"):
        parsed = _parse_datetime(position.get(key))
        if parsed is not None:
            return parsed
    symbol = str(position.get("symbol") or "").upper()
    side = str(position.get("side") or "LONG").upper()
    entry_token = "SELL" if side == "SHORT" else "BUY"
    candidates: list[datetime] = []
    for trade in (account.get("daily_trades") or []) + (account.get("recent_trades") or []):
        if not isinstance(trade, dict):
            continue
        if str(trade.get("symbol") or "").upper() != symbol:
            continue
        if entry_token not in str(trade.get("side") or "").upper():
            continue
        parsed = _parse_datetime(trade.get("time") or trade.get("created_at") or trade.get("timestamp"))
        if parsed is not None:
            candidates.append(parsed)
    return max(candidates) if candidates else None


def _position_adverse_move_pct(*, side: str, average: Decimal | None, current: Decimal | None) -> Decimal | None:
    if average is None or current is None or average <= 0:
        return None
    adverse = current - average if side == "SHORT" else average - current
    if adverse <= 0:
        return Decimal("0")
    return adverse / average * Decimal("100")


def _position_round_trip_cost_pct(symbol: str, *, fees: dict[str, Any]) -> Decimal:
    slippage_pct = _decimal(fees.get("slippage_pct_per_side")) or Decimal("0")
    return (_arena_commission_pct(symbol, fees=fees) + slippage_pct) * Decimal("2")


def _weak_exit_policy_payload(portfolio: dict[str, Any], *, round_trip_cost_pct: Decimal) -> dict[str, Any]:
    noise_buffer = _decimal(portfolio.get("weak_exit_noise_buffer_pct")) or Decimal("0")
    return {
        "threshold_source": portfolio.get("weak_exit_threshold_source"),
        "min_hold_minutes": portfolio.get("weak_exit_min_hold_minutes"),
        "negative_r_threshold": portfolio.get("weak_exit_negative_r_threshold"),
        "noise_buffer_pct": _decimal_payload(noise_buffer),
        "round_trip_cost_pct": _decimal_payload(round_trip_cost_pct),
        "cost_noise_threshold_pct": _decimal_payload(round_trip_cost_pct + noise_buffer),
        "immediate_score_threshold": portfolio.get("weak_exit_immediate_score_threshold"),
    }

def _arena_position_action(
    progress_r: Decimal | None,
    *,
    current: Decimal | None,
    portfolio: dict[str, Any],
    holding_score: int,
    research_verdict: str,
    position_age_minutes: int | None = None,
    adverse_move_pct: Decimal | None = None,
    round_trip_cost_pct: Decimal | None = None,
) -> tuple[str, str]:
    if current is None:
        return ("HOLD", "live_price_unavailable")
    if progress_r is None:
        return ("HOLD", "r_progress_unavailable")
    if research_verdict == "AVOID":
        return ("EXIT_WEAK", "research_avoid")
    take_partial = _decimal(portfolio.get("take_partial_at_r")) or Decimal("2")
    trail = _decimal(portfolio.get("trail_at_r")) or Decimal("1.5")
    breakeven = _decimal(portfolio.get("breakeven_at_r")) or Decimal("1")
    if progress_r >= take_partial:
        return ("TAKE_PARTIAL_PROFIT", "take_partial_profit_threshold_reached")
    if progress_r >= trail:
        return ("TRAIL_STOP", "trail_stop_threshold_reached")
    if progress_r >= breakeven:
        return ("BREAKEVEN_STOP", "breakeven_threshold_reached")
    immediate_score = int(portfolio.get("weak_exit_immediate_score_threshold") or 35)
    if holding_score < immediate_score:
        return ("EXIT_WEAK", "holding_score_below_weak_exit_threshold")
    if progress_r < 0:
        min_hold = int(portfolio.get("weak_exit_min_hold_minutes") or 0)
        if position_age_minutes is not None and min_hold > 0 and position_age_minutes < min_hold:
            return ("HOLD", "negative_r_inside_weak_exit_grace")
        negative_r_threshold = _decimal(portfolio.get("weak_exit_negative_r_threshold")) or Decimal("-0.25")
        if progress_r > negative_r_threshold:
            return ("HOLD", "negative_r_inside_weak_exit_grace")
        noise_buffer = _decimal(portfolio.get("weak_exit_noise_buffer_pct")) or Decimal("0")
        cost_noise_threshold = (round_trip_cost_pct or Decimal("0")) + noise_buffer
        if adverse_move_pct is not None and adverse_move_pct < cost_noise_threshold:
            return ("HOLD", "negative_move_inside_cost_noise")
        return ("EXIT_WEAK", "negative_r_progress")
    return ("HOLD", "position_inside_hold_band")


def _arena_holding_score(
    position: dict[str, Any],
    *,
    progress_r: Decimal | None,
    current: Decimal | None,
    stop: Decimal | None,
    fees: dict[str, Any],
    research_verdict: str,
) -> dict[str, Any]:
    components: dict[str, int] = {}
    if current is None or progress_r is None:
        components["r_progress"] = 20
    else:
        components["r_progress"] = max(0, min(45, int((Decimal("25") + progress_r * Decimal("15")).to_integral_value(rounding=ROUND_FLOOR))))
    components["soft_stop"] = 15 if stop is not None else -20
    pnl = _decimal(position.get("unrealized_pnl")) or Decimal("0")
    components["pnl"] = 15 if pnl > 0 else (-10 if pnl < 0 else 0)
    notional = _position_notional(position)
    commission_pct = _arena_commission_pct(str(position.get("symbol") or ""), fees=fees)
    exit_cost_pct = commission_pct if notional > 0 else Decimal("0")
    components["exit_cost"] = 5 if exit_cost_pct <= Decimal("0.05") else -5
    if research_verdict == "OK":
        components["research"] = 10
    elif research_verdict == "RISK":
        components["research"] = -10
    elif research_verdict == "AVOID":
        components["research"] = -25
    else:
        components["research"] = 0
    score = max(0, min(100, 50 + sum(components.values())))
    return {"score": score, "components": components}


def _arena_candidate_score(candidate: dict[str, Any]) -> int:
    score_payload = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else None
    if score_payload is not None:
        return int(score_payload.get("score") or 0)
    signal = candidate.get("signal") if isinstance(candidate.get("signal"), dict) else {}
    score = 50
    score += 10 if signal.get("h4") in {"up", "down"} else 0
    score += 10 if signal.get("h1") in {"up", "down"} else 0
    score += 5 if signal.get("m30") in {"up", "down"} else 0
    risk_rub = _decimal(candidate.get("risk_rub"))
    notional = _decimal(candidate.get("notional"))
    if risk_rub is not None and notional is not None and notional > 0:
        risk_pct = risk_rub / notional * Decimal("100")
        if risk_pct <= Decimal("1"):
            score += 10
        elif risk_pct >= Decimal("4"):
            score -= 10
    return max(0, min(score, 100))


def _apply_arena_candidate_scores(candidates: list[dict[str, Any]], *, research: dict[str, Any], policy: dict[str, Any]) -> None:
    portfolio = _portfolio_settings(policy)
    learning = learning_settings(policy)
    deprioritized = _learning_deprioritized_symbols(learning)
    attribution_uncertain = _learning_attribution_uncertain_symbols(learning)
    deprioritized_action = str(learning.get("deprioritized_entry_action") or "manual_review").lower()
    attribution_uncertain_action = str(learning.get("attribution_uncertain_entry_action") or "manual_review").lower()
    raw_deprioritized_penalty = learning.get("deprioritized_score_penalty")
    raw_attribution_uncertain_penalty = learning.get("attribution_uncertain_score_penalty")
    deprioritized_penalty = int(raw_deprioritized_penalty) if raw_deprioritized_penalty is not None else 30
    attribution_uncertain_penalty = int(raw_attribution_uncertain_penalty) if raw_attribution_uncertain_penalty is not None else 15
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate["arena_growth_score"] = _arena_growth_score(candidate, research=research)
        symbol = str(candidate.get("symbol") or "").upper()
        if symbol in deprioritized:
            _apply_learning_deprioritized_candidate(candidate, penalty=deprioritized_penalty)
            if deprioritized_action in {"manual_review", "block"}:
                _block_arena_candidate(candidate, "learning_deprioritized_requires_manual_review")
        elif symbol in attribution_uncertain:
            _apply_learning_attribution_uncertain_candidate(candidate, penalty=attribution_uncertain_penalty)
            if attribution_uncertain_action in {"manual_review", "block"}:
                _block_arena_candidate(candidate, "learning_attribution_uncertain_requires_manual_review")
        repeat_pattern = candidate.get("repeat_pattern_loss") if isinstance(candidate.get("repeat_pattern_loss"), dict) else {}
        if repeat_pattern.get("action") == "penalty":
            settings = _repeat_pattern_loss_settings(policy)
            _apply_repeat_pattern_score_penalty(candidate, penalty=int(settings.get("score_penalty") or 25))
        _apply_arena_entry_quality_gates(candidate, portfolio=portfolio)
        min_score = _min_candidate_score_for_buy(policy, account_id=str(candidate.get("account_id") or ""))
        if str(candidate.get("side") or "BUY").upper() == "BUY" and int(candidate["arena_growth_score"]["score"]) < min_score:
            _block_arena_candidate(candidate, "candidate_score_below_min")


def _apply_arena_entry_quality_gates(candidate: dict[str, Any], *, portfolio: dict[str, Any]) -> None:
    if str(candidate.get("side") or "BUY").upper() != "BUY":
        return
    score_payload = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    score = int(score_payload.get("score") or 0)
    verdict = str(score_payload.get("research_verdict") or candidate.get("research_verdict") or "UNAVAILABLE").upper()
    exceptional_score = _research_unavailable_exceptional_score(portfolio, candidate)
    if verdict == "UNAVAILABLE" and score < exceptional_score:
        _block_arena_candidate(candidate, "research_unavailable_below_exceptional_score")

    relative_strength = _decimal(candidate.get("relative_strength_score")) or Decimal("0")
    signal = candidate.get("signal") if isinstance(candidate.get("signal"), dict) else {}
    if relative_strength <= 0 and str(signal.get("m30") or "").lower() != "up":
        _block_arena_candidate(candidate, "entry_strength_confirmation_missing")


def _min_candidate_score_for_buy(policy: dict[str, Any], *, account_id: str) -> int:
    return _min_candidate_score_for_buy_from_portfolio(_portfolio_settings(policy), account_id=account_id)


def _min_candidate_score_for_buy_from_portfolio(portfolio: dict[str, Any], *, account_id: str) -> int:
    default = int(portfolio.get("min_candidate_score_for_buy") or 0)
    by_account = portfolio.get("min_candidate_score_for_buy_by_account")
    if not isinstance(by_account, dict):
        return default
    value = by_account.get(str(account_id))
    return int(value) if isinstance(value, int) else default


def _research_unavailable_exceptional_score(portfolio: dict[str, Any], candidate: dict[str, Any]) -> int:
    """Minimum score that can pass when the research provider is unavailable.

    Default remains conservative. Per-account / per-MIC overrides allow
    controlled medium-high contours when research is down.
    """
    default = int(portfolio.get("opportunity_exceptional_base_score") or 84)
    account_id = str(candidate.get("account_id") or "")
    by_account = portfolio.get("opportunity_exceptional_base_score_by_account")
    if isinstance(by_account, dict):
        value = by_account.get(account_id)
        if isinstance(value, int):
            return value
    mic = _symbol_mic(str(candidate.get("symbol") or ""))
    by_mic = portfolio.get("opportunity_exceptional_base_score_by_mic")
    if isinstance(by_mic, dict):
        value = by_mic.get(mic)
        if isinstance(value, int):
            return value
    return default


def _arena_max_position_notional_pct(risk: dict[str, Any], *, account_id: str, symbol: str) -> Decimal:
    """Max position notional percent with optional account/MIC overrides."""
    return _arena_risk_limit_pct(risk, "max_position_notional_pct", account_id=account_id, symbol=symbol, default="25")


def _arena_risk_limit_pct(risk: dict[str, Any], key: str, *, account_id: str, symbol: str, default: str) -> Decimal:
    base = _decimal(risk.get(key)) or Decimal(default)
    by_account = risk.get(f"{key}_by_account")
    if isinstance(by_account, dict):
        value = _decimal(by_account.get(str(account_id)))
        if value is not None and value > 0:
            return value
    by_mic = risk.get(f"{key}_by_mic")
    if isinstance(by_mic, dict):
        value = _decimal(by_mic.get(_symbol_mic(symbol)))
        if value is not None and value > 0:
            return value
    return base


ENTRY_DAILY_LIMIT_GATES = {"daily_new_notional_limit_exceeded", "primary_daily_limit_reached"}


def _apply_exceptional_entry_limit_overrides(
    candidates: list[dict[str, Any]],
    *,
    accounts_by_id: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    now: datetime,
) -> None:
    risk = _risk_settings(policy)
    overrides = risk.get("exceptional_entry_limit_overrides")
    if not isinstance(overrides, dict):
        return
    for candidate in candidates:
        if not isinstance(candidate, dict) or str(candidate.get("side") or "BUY").upper() != "BUY":
            continue
        account_id = str(candidate.get("account_id") or "")
        config = overrides.get(account_id)
        if not isinstance(config, dict) or not bool(config.get("enabled", True)):
            continue
        reasons = [str(item) for item in candidate.get("gate_reasons") or [] if str(item)]
        reason_set = set(reasons)
        limit_gates = reason_set & ENTRY_DAILY_LIMIT_GATES
        if not limit_gates:
            continue
        allowed_limit_gates = {
            str(item)
            for item in config.get("allowed_limit_gates", ENTRY_DAILY_LIMIT_GATES)
            if str(item)
        }
        if not limit_gates <= allowed_limit_gates:
            continue
        forbidden_gates = {str(item) for item in config.get("forbidden_gates", []) if str(item)}
        if reason_set & forbidden_gates:
            continue
        if reason_set - allowed_limit_gates:
            continue
        if not _exceptional_entry_limit_score_ok(candidate, config):
            continue
        account = accounts_by_id.get(account_id) or {}
        if "primary_daily_limit_reached" in limit_gates and not _exceptional_entry_primary_limit_ok(
            account,
            config=config,
        ):
            continue
        if "daily_new_notional_limit_exceeded" in limit_gates and not _exceptional_entry_notional_limit_ok(
            candidate,
            account,
            config=config,
            now=now,
        ):
            continue
        remaining = [reason for reason in reasons if reason not in limit_gates]
        candidate["gate_reasons"] = remaining
        candidate["execution_allowed"] = not remaining
        candidate["exceptional_entry_limit_override"] = {
            "account_id": account_id,
            "removed_gates": sorted(limit_gates),
            "score": _arena_candidate_score(candidate),
            "research_verdict": _arena_candidate_research_verdict(candidate),
            "max_primary_entries_per_day": config.get("max_primary_entries_per_day"),
            "max_new_notional_pct_per_day": config.get("max_new_notional_pct_per_day"),
        }


def _exceptional_entry_limit_score_ok(candidate: dict[str, Any], config: dict[str, Any]) -> bool:
    score = _arena_candidate_score(candidate)
    if score >= int(config.get("min_score") or 101):
        return True
    research_ok_min = config.get("research_ok_min_score")
    return (
        isinstance(research_ok_min, int)
        and score >= research_ok_min
        and _arena_candidate_research_verdict(candidate) == "OK"
    )


def _arena_candidate_research_verdict(candidate: dict[str, Any]) -> str:
    score_payload = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    return str(score_payload.get("research_verdict") or candidate.get("research_verdict") or "UNAVAILABLE").upper()


def _exceptional_entry_primary_limit_ok(account: dict[str, Any], *, config: dict[str, Any]) -> bool:
    max_primary = int(config.get("max_primary_entries_per_day") or 0)
    entry_limits = account.get("entry_limits") if isinstance(account.get("entry_limits"), dict) else {}
    primary_used = int(entry_limits.get("primary_used") or 0)
    return max_primary > 0 and primary_used < max_primary


def _exceptional_entry_notional_limit_ok(
    candidate: dict[str, Any],
    account: dict[str, Any],
    *,
    config: dict[str, Any],
    now: datetime,
) -> bool:
    max_pct = _decimal(config.get("max_new_notional_pct_per_day"))
    equity = _decimal(account.get("equity"))
    notional = _decimal(candidate.get("notional"))
    if max_pct is None or equity is None or equity <= 0 or notional is None:
        return False
    new_notional_today = _daily_trade_notional(account, now=now, side="BUY")
    return (new_notional_today + notional) / equity * Decimal("100") <= max_pct


def _mcp_confirmed_entry_settings(policy: dict[str, Any]) -> dict[str, Any]:
    risk = _risk_settings(policy)
    config = risk.get("mcp_confirmed_entry")
    return config if isinstance(config, dict) else {}


def _has_mcp_confirmed_entry_potential(candidates: list[dict[str, Any]], policy: dict[str, Any]) -> bool:
    config = _mcp_confirmed_entry_settings(policy)
    if not bool(config.get("enabled", False)):
        return False
    allowed = {str(item) for item in config.get("allowed_removed_gates", []) if str(item)}
    forbidden = {str(item) for item in config.get("forbidden_gates", []) if str(item)}
    for candidate in candidates:
        if not _mcp_confirmed_entry_score_band_ok(candidate, config):
            continue
        reasons = {str(item) for item in candidate.get("gate_reasons") or [] if str(item)}
        if reasons and not reasons.intersection(forbidden) and reasons <= allowed:
            return True
    return False


def _apply_mcp_confirmed_entry_overrides(
    candidates: list[dict[str, Any]],
    *,
    accounts_by_id: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    mcp_shadow: dict[str, Any] | None,
    now: datetime,
) -> None:
    config = _mcp_confirmed_entry_settings(policy)
    if not bool(config.get("enabled", False)):
        return
    notes = _mcp_candidate_notes_by_key(mcp_shadow)
    matched_ids = {
        str(item.get("account_id") or "")
        for item in (mcp_shadow or {}).get("matched_arena_accounts") or []
        if isinstance(item, dict)
    }
    account_warning_types = _mcp_portfolio_warning_types_by_account(mcp_shadow)
    for candidate in candidates:
        if not _mcp_confirmed_entry_candidate_base_ok(candidate, config=config):
            continue
        account_id = str(candidate.get("account_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        account = accounts_by_id.get(account_id) or {}
        reasons = [str(item) for item in candidate.get("gate_reasons") or [] if str(item)]
        removable = set(reasons)
        note = notes.get((account_id, symbol))
        failed_reason = _mcp_confirmed_entry_failure_reason(
            candidate,
            account=account,
            config=config,
            mcp_shadow=mcp_shadow,
            note=note,
            matched_ids=matched_ids,
            account_warning_types=account_warning_types,
            now=now,
        )
        if failed_reason:
            candidate["mcp_confirmed_entry"] = {
                "applied": False,
                "reason": failed_reason,
                "score": _arena_candidate_score(candidate),
            }
            continue
        sized = _apply_mcp_confirmed_entry_sizing(candidate, account=account, policy=policy, config=config)
        if not sized:
            candidate["mcp_confirmed_entry"] = {
                "applied": False,
                "reason": "reduced_risk_quantity_unavailable",
                "score": _arena_candidate_score(candidate),
            }
            continue
        candidate["gate_reasons"] = [reason for reason in reasons if reason not in removable]
        candidate["execution_allowed"] = not candidate["gate_reasons"]
        candidate["mcp_confirmed_entry"] = {
            "applied": bool(candidate["execution_allowed"]),
            "mode": "mcp_confirmed_reduced_risk_entry",
            "score": _arena_candidate_score(candidate),
            "research_verdict": _arena_candidate_research_verdict(candidate),
            "removed_gates": sorted(removable),
            "risk_per_trade_pct": config.get("risk_per_trade_pct"),
            "max_position_notional_pct": config.get("max_position_notional_pct"),
            "max_entries_per_account_per_day": config.get("max_entries_per_account_per_day"),
            "max_new_notional_pct_per_day": config.get("max_new_notional_pct_per_day"),
            "portfolio_context": {
                "matched_account": account_id in matched_ids,
                "already_exposed": bool((note or {}).get("already_exposed")),
                "new_exposure": bool((note or {}).get("new_exposure")),
                "watchlist_context": (note or {}).get("watchlist_context"),
                "quote_context": (note or {}).get("quote_context"),
            },
        }


def _mcp_confirmed_entry_candidate_base_ok(candidate: dict[str, Any], *, config: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict) or str(candidate.get("side") or "BUY").upper() != "BUY":
        return False
    if candidate.get("execution_allowed") is True:
        return False
    if not _mcp_confirmed_entry_score_band_ok(candidate, config):
        return False
    reasons = {str(item) for item in candidate.get("gate_reasons") or [] if str(item)}
    allowed = {str(item) for item in config.get("allowed_removed_gates", []) if str(item)}
    forbidden = {str(item) for item in config.get("forbidden_gates", []) if str(item)}
    return bool(reasons) and not reasons.intersection(forbidden) and reasons <= allowed


def _mcp_confirmed_entry_score_band_ok(candidate: dict[str, Any], config: dict[str, Any]) -> bool:
    if not isinstance(candidate, dict) or str(candidate.get("side") or "BUY").upper() != "BUY":
        return False
    score = _arena_candidate_score(candidate)
    return int(config.get("min_score") or 101) <= score <= int(config.get("max_score") or -1)


def _mcp_confirmed_entry_failure_reason(
    candidate: dict[str, Any],
    *,
    account: dict[str, Any],
    config: dict[str, Any],
    mcp_shadow: dict[str, Any] | None,
    note: dict[str, Any] | None,
    matched_ids: set[str],
    account_warning_types: dict[str, set[str]],
    now: datetime,
) -> str | None:
    account_id = str(candidate.get("account_id") or "")
    if bool(config.get("requires_research_ok", True)) and _arena_candidate_research_verdict(candidate) != "OK":
        return "research_not_ok"
    if bool(config.get("requires_mcp", True)) and (not isinstance(mcp_shadow, dict) or mcp_shadow.get("status") != "OK"):
        return "mcp_unavailable"
    if account_id not in matched_ids:
        return "mcp_account_not_matched"
    warning_types = account_warning_types.get(account_id, set())
    if "arena_account_not_visible_in_mcp" in warning_types:
        return "mcp_account_not_visible"
    if "position_set_diff" in warning_types:
        return "mcp_position_set_diff"
    if not isinstance(note, dict) or note.get("mcp_unavailable"):
        return "mcp_candidate_context_unavailable"
    if note.get("already_exposed") is True or note.get("new_exposure") is not True:
        return "already_exposed"
    if note.get("quote_context") != "available":
        return "mcp_quote_unavailable"
    if not _mcp_confirmed_entry_primary_limit_ok(account, config=config):
        return "mcp_daily_entry_limit_reached"
    if not _mcp_confirmed_entry_notional_limit_ok(candidate, account, config=config, now=now):
        return "mcp_daily_notional_limit_reached"
    return None


def _mcp_candidate_notes_by_key(mcp_shadow: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    notes: dict[tuple[str, str], dict[str, Any]] = {}
    for item in (mcp_shadow or {}).get("candidate_context_notes") or []:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("account_id") or "")
        symbol = str(item.get("symbol") or "").upper()
        if account_id and symbol:
            notes[(account_id, symbol)] = item
    return notes


def _mcp_portfolio_warning_types_by_account(mcp_shadow: dict[str, Any] | None) -> dict[str, set[str]]:
    by_account: dict[str, set[str]] = {}
    for item in (mcp_shadow or {}).get("portfolio_warnings") or []:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("account_id") or "")
        warning_type = str(item.get("type") or "")
        if account_id and warning_type:
            by_account.setdefault(account_id, set()).add(warning_type)
    return by_account


def _mcp_confirmed_entry_primary_limit_ok(account: dict[str, Any], *, config: dict[str, Any]) -> bool:
    max_entries = int(config.get("max_entries_per_account_per_day") or 0)
    entry_limits = account.get("entry_limits") if isinstance(account.get("entry_limits"), dict) else {}
    primary_used = int(entry_limits.get("primary_used") or 0)
    return max_entries > 0 and primary_used < max_entries


def _mcp_confirmed_entry_notional_limit_ok(
    candidate: dict[str, Any],
    account: dict[str, Any],
    *,
    config: dict[str, Any],
    now: datetime,
) -> bool:
    max_pct = _decimal(config.get("max_new_notional_pct_per_day"))
    equity = _decimal(account.get("equity"))
    sizing = _mcp_confirmed_entry_reduced_sizing(candidate, account=account, config=config)
    notional = sizing[1] if sizing is not None else None
    if max_pct is None or equity is None or equity <= 0 or notional is None:
        return False
    new_notional_today = _daily_trade_notional(account, now=now, side="BUY")
    return (new_notional_today + notional) / equity * Decimal("100") <= max_pct


def _apply_mcp_confirmed_entry_sizing(
    candidate: dict[str, Any],
    *,
    account: dict[str, Any],
    policy: dict[str, Any],
    config: dict[str, Any],
) -> bool:
    sizing = _mcp_confirmed_entry_reduced_sizing(candidate, account=account, config=config)
    if sizing is None:
        return False
    quantity, notional, risk_per_share = sizing
    candidate["quantity"] = str(int(quantity))
    candidate["notional"] = _decimal_payload(notional)
    candidate["risk_rub"] = _decimal_payload(quantity * risk_per_share)
    candidate["costs"] = _arena_trade_costs(notional, symbol=str(candidate.get("symbol") or ""), fees=_fees_settings(policy))
    return True


def _mcp_confirmed_entry_reduced_sizing(
    candidate: dict[str, Any],
    *,
    account: dict[str, Any],
    config: dict[str, Any],
) -> tuple[Decimal, Decimal, Decimal] | None:
    equity = _decimal(account.get("equity"))
    cash = _decimal(account.get("available_cash")) or _decimal(account.get("cash")) or Decimal("0")
    entry_price = _decimal(candidate.get("entry_price"))
    risk_per_share = _decimal(candidate.get("risk_per_share"))
    risk_pct = _decimal(config.get("risk_per_trade_pct"))
    max_notional_pct = _decimal(config.get("max_position_notional_pct"))
    if (
        equity is None
        or equity <= 0
        or entry_price is None
        or entry_price <= 0
        or risk_per_share is None
        or risk_per_share <= 0
        or risk_pct is None
        or risk_pct <= 0
        or max_notional_pct is None
        or max_notional_pct <= 0
    ):
        return None
    risk_budget = equity * risk_pct / Decimal("100")
    max_notional = equity * max_notional_pct / Decimal("100")
    quantity = min(
        (risk_budget / risk_per_share).to_integral_value(rounding=ROUND_FLOOR),
        (cash / entry_price).to_integral_value(rounding=ROUND_FLOOR),
        (max_notional / entry_price).to_integral_value(rounding=ROUND_FLOOR),
    )
    if quantity <= 0:
        return None
    notional = quantity * entry_price
    return quantity, notional, risk_per_share


def _apply_arena_account_role_gates(
    candidates: list[dict[str, Any]],
    *,
    accounts_by_id: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> None:
    portfolio = _portfolio_settings(policy)
    gates = portfolio.get("account_role_gates") if isinstance(portfolio.get("account_role_gates"), dict) else {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        account_id = str(candidate.get("account_id") or "")
        account = accounts_by_id.get(account_id) or {}
        account_gates = gates.get(account_id) if isinstance(gates.get(account_id), dict) else {}
        _apply_cross_market_concentration_gate(candidate, account=account, config=account_gates.get("cross_market_concentration"))
        _apply_single_symbol_anchor_rotation_gate(
            candidate,
            account=account,
            policy=policy,
            config=account_gates.get("single_symbol_anchor_rotation"),
        )


def _apply_cross_market_concentration_gate(candidate: dict[str, Any], *, account: dict[str, Any], config: Any) -> None:
    if not isinstance(config, dict) or not bool(config.get("enabled", True)) or bool(config.get("manual_override", False)):
        return
    market = str(config.get("market") or "MISX").upper()
    max_positions = int(config.get("max_positions") or 2)
    if str(candidate.get("side") or "BUY").upper() != "BUY" or _symbol_mic(str(candidate.get("symbol") or "")) != market:
        return
    if _account_positions_count_by_mic(account, market) >= max_positions:
        _block_arena_candidate(candidate, CROSS_MARKET_ROLE_MISX_CONCENTRATION_GATE)


def _apply_single_symbol_anchor_rotation_gate(
    candidate: dict[str, Any],
    *,
    account: dict[str, Any],
    policy: dict[str, Any],
    config: Any,
) -> None:
    if not isinstance(config, dict) or not bool(config.get("enabled", True)):
        return
    if str(candidate.get("side") or "BUY").upper() != "BUY":
        return
    anchor = _single_symbol_anchor_position(account, config=config)
    if anchor is None:
        return
    anchor_symbol = str(anchor.get("symbol") or "").upper()
    if not anchor_symbol or anchor_symbol == str(candidate.get("symbol") or "").upper():
        return
    progress_r = _position_progress_r(
        side=str(anchor.get("side") or "LONG").upper(),
        average=_decimal(anchor.get("average_price")),
        current=_decimal(anchor.get("current_price")) or _decimal(anchor.get("average_price")),
        stop=_position_stop_price(anchor, account),
    )
    required_progress = _decimal(config.get("required_progress_r")) or Decimal("1")
    if progress_r is not None and progress_r >= required_progress:
        return
    fees = _fees_settings(policy)
    holding_score = int(
        _arena_holding_score(
            anchor,
            progress_r=progress_r,
            current=_decimal(anchor.get("current_price")) or _decimal(anchor.get("average_price")),
            stop=_position_stop_price(anchor, account),
            fees=fees,
            research_verdict="UNAVAILABLE",
        ).get("score")
        or 0
    )
    candidate_score = _arena_candidate_score(candidate)
    min_score = int(config.get("candidate_min_score") or 80)
    min_delta = int(config.get("min_score_delta") or 15)
    if candidate_score < min_score and candidate_score - holding_score < min_delta:
        return
    candidate["anchor_rotation"] = {
        "source_symbol": anchor_symbol,
        "source_notional_pct": anchor.get("anchor_notional_pct"),
        "source_progress_r": _decimal_payload(progress_r),
        "source_holding_score": holding_score,
        "candidate_score": candidate_score,
        "suggested_partial_sell_quantity": decimal_payload(_anchor_partial_sell_quantity(anchor, account=account, config=config), min_scale=1),
        "confirmation_required": True,
    }
    _block_arena_candidate(candidate, SINGLE_SYMBOL_ANCHOR_ROTATION_GATE)


def _anchor_partial_sell_quantity(anchor: dict[str, Any], *, account: dict[str, Any], config: dict[str, Any]) -> Decimal:
    equity = _decimal(account.get("equity")) or ARENA_STARTING_EQUITY
    threshold_pct = _decimal(config.get("anchor_notional_pct")) or Decimal("45")
    current_price = _decimal(anchor.get("current_price")) or _decimal(anchor.get("average_price")) or Decimal("0")
    current_quantity = abs(_decimal(anchor.get("quantity")) or Decimal("0"))
    if equity <= 0 or threshold_pct <= 0 or current_price <= 0 or current_quantity <= 0:
        return Decimal("0")
    target_notional = equity * threshold_pct / Decimal("100")
    excess_notional = _position_notional(anchor) - target_notional
    if excess_notional <= 0:
        return Decimal("0")
    quantity = (excess_notional / current_price).to_integral_value(rounding=ROUND_CEILING)
    return min(current_quantity, max(Decimal("1"), quantity))


def _single_symbol_anchor_position(account: dict[str, Any], *, config: dict[str, Any]) -> dict[str, Any] | None:
    positions = [item for item in account.get("positions") or [] if isinstance(item, dict)]
    if not positions:
        return None
    market = str(config.get("market") or "").upper()
    if market:
        positions = [item for item in positions if _symbol_mic(str(item.get("symbol") or "")) == market]
    if not positions:
        return None
    equity = _decimal(account.get("equity")) or ARENA_STARTING_EQUITY
    if equity <= 0:
        return None
    anchor = max(positions, key=_position_notional)
    notional_pct = _position_notional(anchor) / equity * Decimal("100")
    threshold = _decimal(config.get("anchor_notional_pct")) or Decimal("45")
    if notional_pct < threshold:
        return None
    anchor["anchor_notional_pct"] = _decimal_payload(notional_pct)
    return anchor


def _account_positions_count_by_mic(account: dict[str, Any], mic: str) -> int:
    market = str(mic or "").upper()
    return len(
        [
            item
            for item in account.get("positions") or []
            if isinstance(item, dict) and _symbol_mic(str(item.get("symbol") or "")) == market
        ]
    )


def _learning_deprioritized_symbols(learning: dict[str, Any]) -> set[str]:
    return {str(item).upper() for item in learning.get("deprioritize_symbols") or [] if str(item).strip()}


def _learning_attribution_uncertain_symbols(learning: dict[str, Any]) -> set[str]:
    return {str(item).upper() for item in learning.get("attribution_uncertain_symbols") or [] if str(item).strip()}


def _apply_learning_deprioritized_candidate(candidate: dict[str, Any], *, penalty: int) -> None:
    _apply_learning_candidate_penalty(candidate, component="learning_deprioritized", flag="learning_deprioritized", penalty=penalty)


def _apply_learning_attribution_uncertain_candidate(candidate: dict[str, Any], *, penalty: int) -> None:
    _apply_learning_candidate_penalty(candidate, component="learning_attribution_uncertain", flag="learning_attribution_uncertain", penalty=penalty)


def _apply_learning_candidate_penalty(candidate: dict[str, Any], *, component: str, flag: str, penalty: int) -> None:
    existing_score = candidate.get("arena_growth_score")
    score_payload: dict[str, Any] = existing_score if isinstance(existing_score, dict) else {}
    existing_components = score_payload.get("components")
    components: dict[str, Any] = existing_components if isinstance(existing_components, dict) else {}
    components[component] = -abs(int(penalty))
    raw_score = int(score_payload.get("score") or 0) - abs(int(penalty))
    score = max(0, min(100, raw_score))
    score_payload["score"] = score
    score_payload["label"] = "HIGH" if score >= 75 else ("MEDIUM" if score >= 60 else "LOW")
    score_payload["components"] = components
    score_payload[flag] = True
    candidate["arena_growth_score"] = score_payload


def _arena_growth_score(candidate: dict[str, Any], *, research: dict[str, Any]) -> dict[str, Any]:
    components: dict[str, int] = {}
    signal = candidate.get("signal") if isinstance(candidate.get("signal"), dict) else {}
    side = str(candidate.get("side") or "BUY").upper()
    favorable = "down" if side == "SELL" else "up"
    entry_timeframe = str(candidate.get("entry_timeframe") or "")
    trend_momentum = 10 if signal.get("h4") == favorable else 0
    if entry_timeframe == "H1" and signal.get("h1") == favorable:
        trend_momentum += 15
    elif entry_timeframe == "M30" and signal.get("m30") == favorable:
        trend_momentum += 15
    else:
        trend_momentum += (8 if signal.get("h1") == favorable else 0) + (7 if signal.get("m30") == favorable else 0)
    components["trend_momentum"] = trend_momentum
    costs = candidate.get("costs") if isinstance(candidate.get("costs"), dict) else {}
    target_r = _decimal(candidate.get("nearest_target_r")) or _decimal(candidate.get("take_profit_r")) or Decimal("2")
    break_even_pct = _decimal(costs.get("break_even_move_pct")) or Decimal("0")
    risk_per_share = _decimal(candidate.get("risk_per_share")) or Decimal("0")
    entry = _decimal(candidate.get("entry_price")) or Decimal("0")
    gross_edge_pct = (risk_per_share * target_r / entry * Decimal("100")) if entry > 0 and risk_per_share > 0 else Decimal("0")
    net_edge_pct = gross_edge_pct - break_even_pct
    if break_even_pct > 0 and gross_edge_pct < break_even_pct * Decimal("3"):
        edge_points = 0
    else:
        edge_points = max(0, min(25, int((net_edge_pct * Decimal("7")).to_integral_value(rounding=ROUND_FLOOR))))
    components["expected_edge_after_cost"] = edge_points
    components["relative_strength"] = max(0, min(15, int(_decimal(candidate.get("relative_strength_score")) or Decimal("0"))))
    risk_rub = _decimal(candidate.get("risk_rub"))
    notional = _decimal(candidate.get("notional"))
    if risk_rub is not None and notional is not None and notional > 0:
        risk_pct = risk_rub / notional * Decimal("100")
        components["volatility_risk"] = 10 if risk_pct <= Decimal("2.5") else (4 if risk_pct <= Decimal("4") else 0)
    else:
        components["volatility_risk"] = 0
    components["liquidity_execution"] = max(0, min(10, int(_decimal(candidate.get("liquidity_score")) or Decimal("0"))))
    verdict = _arena_research_verdict_for_symbol(research, str(candidate.get("symbol") or ""))
    if verdict == "OK":
        components["research_regime"] = 15
    elif verdict == "RISK":
        components["research_regime"] = -15
    elif verdict == "AVOID":
        components["research_regime"] = -30
    else:
        components["research_regime"] = 0
    context_components = _arena_report_only_score_components(candidate, verdict=verdict)
    components.update(context_components)
    components["technical_momentum_context"] = max(
        -10,
        min(
            10,
            int(context_components.get("technical_confirmation") or 0)
            + int(context_components.get("risk_adjusted_momentum") or 0),
        ),
    )
    active_components = [
        "trend_momentum",
        "expected_edge_after_cost",
        "relative_strength",
        "volatility_risk",
        "liquidity_execution",
        "research_regime",
        "technical_momentum_context",
    ]
    report_only_components = ["news_event_context", "pair_relative_value"]
    score = max(0, min(100, sum(components.get(key, 0) for key in active_components)))
    label = "HIGH" if score >= 75 else ("MEDIUM" if score >= 60 else "LOW")
    return {
        "score": score,
        "label": label,
        "components": components,
        "active_components": active_components,
        "report_only_components": sorted(report_only_components),
        "gross_edge_pct": _decimal_payload(gross_edge_pct),
        "net_edge_pct": _decimal_payload(net_edge_pct),
        "break_even_move_pct": _decimal_payload(break_even_pct),
        "research_verdict": verdict,
    }


def _arena_report_only_score_components(candidate: dict[str, Any], *, verdict: str) -> dict[str, int]:
    technical = candidate.get("technical_context") if isinstance(candidate.get("technical_context"), dict) else {}
    momentum = candidate.get("risk_adjusted_momentum") if isinstance(candidate.get("risk_adjusted_momentum"), dict) else {}
    news_context = candidate.get("news_event_context") if isinstance(candidate.get("news_event_context"), dict) else {}
    relative_value = candidate.get("relative_value_context") if isinstance(candidate.get("relative_value_context"), dict) else {}
    return {
        "technical_confirmation": _bounded_int(technical.get("score"), minimum=-10, maximum=10, default=0),
        "risk_adjusted_momentum": _bounded_int(momentum.get("score"), minimum=-10, maximum=10, default=0),
        "news_event_context": _bounded_int(news_context.get("score"), minimum=-10, maximum=10, default=_news_event_score_from_verdict(verdict)),
        "pair_relative_value": _bounded_int(relative_value.get("score"), minimum=-10, maximum=10, default=0),
    }


def _news_event_score_from_verdict(verdict: str) -> int:
    if verdict == "OK":
        return 5
    if verdict == "RISK":
        return -5
    if verdict == "AVOID":
        return -10
    return 0


def _arena_research_verdict_for_symbol(research: dict[str, Any], symbol: str) -> str:
    normalized = str(symbol or "").upper()
    for item in research.get("items") or []:
        if isinstance(item, dict) and str(item.get("symbol") or "").upper() == normalized:
            return str(item.get("verdict") or "UNAVAILABLE").upper()
    if normalized and normalized in {str(item).upper() for item in research.get("symbols") or []}:
        return str(research.get("verdict") or "UNAVAILABLE").upper()
    if str(research.get("verdict") or "").upper() in {"OK", "RISK", "AVOID"} and len(research.get("symbols") or []) == 1:
        return str(research.get("verdict") or "").upper()
    return "UNAVAILABLE"


def _relative_strength_score(bars: list[dict[str, Any]], *, side: str) -> int:
    if len(bars) < 6:
        return 0
    start = _decimal(bars[-6].get("close"))
    end = _decimal(bars[-1].get("close"))
    if start is None or start <= 0 or end is None:
        return 0
    move_pct = (end - start) / start * Decimal("100")
    if side == "SELL":
        move_pct = -move_pct
    return max(0, min(15, int((move_pct * Decimal("3")).to_integral_value(rounding=ROUND_FLOOR))))


def _arena_technical_context(bars: list[dict[str, Any]], *, side: str) -> dict[str, Any]:
    closes = [_decimal(item.get("close")) for item in bars]
    closes = [item for item in closes if item is not None and item > 0]
    if len(closes) < 20:
        return {"score": 0, "status": "insufficient_bars", "report_only": True}
    latest = closes[-1]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    rsi14 = _rsi(closes, 14)
    bollinger = _bollinger_position(closes, 20)
    atr_regime = _atr_regime_score(bars)
    score = 0
    reasons: list[str] = []
    if sma20 is not None and latest > sma20:
        score += 3
        reasons.append("price_above_sma20")
    elif sma20 is not None:
        score -= 3
        reasons.append("price_below_sma20")
    if sma20 is not None and sma50 is not None:
        if sma20 > sma50:
            score += 3
            reasons.append("sma20_above_sma50")
        else:
            score -= 3
            reasons.append("sma20_below_sma50")
    if rsi14 is not None:
        if Decimal("45") <= rsi14 <= Decimal("70"):
            score += 2
            reasons.append("rsi_constructive")
        elif rsi14 > Decimal("78") or rsi14 < Decimal("35"):
            score -= 2
            reasons.append("rsi_extreme")
    if bollinger is not None:
        if Decimal("0.20") <= bollinger <= Decimal("0.85"):
            score += 1
            reasons.append("bollinger_position_constructive")
        elif bollinger > Decimal("1.05"):
            score -= 2
            reasons.append("bollinger_overheated")
    score += atr_regime
    if side == "SELL":
        score = -score
    return {
        "score": max(-10, min(10, score)),
        "report_only": True,
        "sma20": _decimal_payload(sma20),
        "sma50": _decimal_payload(sma50),
        "rsi14": _decimal_payload(rsi14),
        "bollinger_position": _decimal_payload(bollinger),
        "atr_regime_score": atr_regime,
        "reasons": reasons[:6],
    }


def _arena_risk_adjusted_momentum_context(bars: list[dict[str, Any]], *, side: str) -> dict[str, Any]:
    closes = [_decimal(item.get("close")) for item in bars]
    closes = [item for item in closes if item is not None and item > 0]
    if len(closes) < 21:
        return {"score": 0, "status": "insufficient_bars", "report_only": True}
    return_20 = _period_return(closes, 20)
    return_60 = _period_return(closes, 60) if len(closes) >= 61 else None
    volatility = _realized_volatility(closes[-21:])
    direction = Decimal("-1") if side == "SELL" else Decimal("1")
    directional_return = (return_20 or Decimal("0")) * direction
    sharpe_like = (directional_return / volatility) if volatility is not None and volatility > 0 else None
    score = 0
    if directional_return > Decimal("0"):
        score += min(5, int((directional_return * Decimal("2")).to_integral_value(rounding=ROUND_FLOOR)))
    elif directional_return < Decimal("0"):
        score -= min(5, abs(int((directional_return * Decimal("2")).to_integral_value(rounding=ROUND_FLOOR))))
    if sharpe_like is not None:
        if sharpe_like >= Decimal("1"):
            score += 5
        elif sharpe_like <= Decimal("-1"):
            score -= 5
    return {
        "score": max(-10, min(10, score)),
        "report_only": True,
        "return_20_pct": _decimal_payload(return_20),
        "return_60_pct": _decimal_payload(return_60),
        "realized_volatility_pct": _decimal_payload(volatility),
        "sharpe_like": _decimal_payload(sharpe_like),
    }


def _arena_news_event_context(symbol: str) -> dict[str, Any]:
    return {
        "report_only": True,
        "symbol": str(symbol or "").upper(),
        "source": "research_verdict_or_finam_rss_cache",
        "status": "awaiting_symbol_news_cache",
    }


def _arena_relative_value_context(symbol: str, policy: dict[str, Any]) -> dict[str, Any]:
    config = policy.get("relative_value") if isinstance(policy.get("relative_value"), dict) else {}
    enabled = bool(config.get("enabled", True))
    max_delta = _bounded_int(config.get("max_score_delta"), minimum=0, maximum=10, default=10)
    return {
        "score": 0,
        "report_only": True,
        "enabled": enabled,
        "mode": str(config.get("mode") or "long_only_rank_modifier"),
        "max_score_delta": max_delta,
        "symbol": str(symbol or "").upper(),
        "status": "neutral_no_pair_signal",
        "short_orders_allowed": False,
        "broker_payload": None,
    }


def _sma(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    return sum(values[-period:], Decimal("0")) / Decimal(period)


def _rsi(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period + 1:
        return None
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for index in range(len(values) - period, len(values)):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains.append(change)
            losses.append(Decimal("0"))
        else:
            gains.append(Decimal("0"))
            losses.append(abs(change))
    avg_gain = sum(gains, Decimal("0")) / Decimal(period)
    avg_loss = sum(losses, Decimal("0")) / Decimal(period)
    if avg_loss == 0:
        return Decimal("100")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def _bollinger_position(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window, Decimal("0")) / Decimal(period)
    variance = sum((item - mean) * (item - mean) for item in window) / Decimal(period)
    if variance <= 0:
        return None
    std = Decimal(str(math.sqrt(float(variance))))
    lower = mean - std * Decimal("2")
    upper = mean + std * Decimal("2")
    width = upper - lower
    if width <= 0:
        return None
    return (values[-1] - lower) / width


def _atr_regime_score(bars: list[dict[str, Any]]) -> int:
    atr14 = _atr(bars, period=14)
    atr28 = _atr(bars, period=28)
    if atr14 is None or atr28 is None or atr28 <= 0:
        return 0
    ratio = atr14 / atr28
    if Decimal("0.75") <= ratio <= Decimal("1.25"):
        return 1
    if ratio > Decimal("1.75"):
        return -2
    return 0


def _period_return(values: list[Decimal], period: int) -> Decimal | None:
    if len(values) < period + 1:
        return None
    start = values[-period - 1]
    end = values[-1]
    if start <= 0:
        return None
    return (end - start) / start * Decimal("100")


def _realized_volatility(values: list[Decimal]) -> Decimal | None:
    if len(values) < 3:
        return None
    returns: list[float] = []
    for index in range(1, len(values)):
        previous = values[index - 1]
        current = values[index]
        if previous > 0:
            returns.append(float((current - previous) / previous * Decimal("100")))
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    return Decimal(str(math.sqrt(variance)))


def _apply_overexposure_actions(account: dict[str, Any], position_reviews: list[dict[str, Any]], *, portfolio: dict[str, Any]) -> None:
    gross = _account_gross_exposure_pct(account)
    target = _decimal(portfolio.get("target_gross_exposure_pct")) or Decimal("50")
    if gross is None or gross <= target or not position_reviews:
        return
    actionable = [item for item in position_reviews if item.get("action") == "HOLD"]
    if not actionable:
        return
    weakest = min(actionable, key=lambda item: int(item.get("holding_score") or 0))
    if int(weakest.get("holding_score") or 0) < 60:
        weakest["action"] = "TRIM_OVEREXPOSURE"
        weakest["reason"] = "account_gross_exposure_above_target"
        weakest["confirmation_required"] = True
        weakest["confirmation_phrase"] = f"CONFIRM_ARENA_EXIT {weakest.get('symbol')} {account.get('account_id')}"


def _arena_planned_action(
    account: dict[str, Any],
    position_reviews: list[dict[str, Any]],
    replacement: dict[str, Any] | None,
    *,
    portfolio: dict[str, Any],
) -> dict[str, Any] | None:
    account_id = str(account.get("account_id") or "")
    priority = {
        "TRAIL_STOP": 10,
        "BREAKEVEN_STOP": 20,
        "TAKE_PARTIAL_PROFIT": 30,
        "TRIM_OVEREXPOSURE": 40,
        "EXIT_WEAK": 50,
    }
    actionable = [item for item in position_reviews if item.get("action") in priority]
    if actionable:
        chosen = max(actionable, key=lambda item: priority[str(item.get("action"))])
        return {
            "account_id": account_id,
            "action": chosen.get("action"),
            "symbol": chosen.get("symbol"),
            "quantity": chosen.get("quantity"),
            "reason": chosen.get("reason"),
            "holding_score": chosen.get("holding_score"),
            "broker_mutation": False,
        }
    if replacement and portfolio.get("autonomy_mode") == "full_rotation":
        return {
            "account_id": account_id,
            "action": "REPLACE",
            "sell_symbol": replacement.get("sell_symbol"),
            "buy_symbol": replacement.get("buy_symbol"),
            "reason": "replacement_edge_after_cost",
            "score_delta": replacement.get("score_delta"),
            "broker_mutation": False,
        }
    return None


def _arena_replacement_proposal(
    account: dict[str, Any],
    position_reviews: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    portfolio: dict[str, Any],
    fees: dict[str, Any],
) -> dict[str, Any] | None:
    if not position_reviews or not candidates:
        return None
    entry_limits = account.get("entry_limits") if isinstance(account.get("entry_limits"), dict) else {}
    replacement_limit = int(entry_limits.get("replacement_limit") or 0)
    replacement_used = int(entry_limits.get("replacement_used") or 0)
    if replacement_limit <= 0 or replacement_used >= replacement_limit:
        account["replacement_block_reason"] = "replacement_daily_limit_reached"
        return None
    if not bool(entry_limits.get("replacement_ledger_available")):
        account["replacement_block_reason"] = "replacement_ledger_unavailable"
        return None
    held_symbols = {str(item.get("symbol") or "") for item in position_reviews}
    min_candidate_score = _min_candidate_score_for_buy_from_portfolio(
        portfolio,
        account_id=str(account.get("account_id") or ""),
    )
    fresh_candidates = [
        item
        for item in candidates
        if str(item.get("symbol") or "") not in held_symbols
        and str(item.get("side") or "BUY").upper() == "BUY"
        and _candidate_allowed_for_replacement(item)
        and _arena_candidate_score(item) >= min_candidate_score
    ]
    if not fresh_candidates:
        return None
    weakest = min(position_reviews, key=lambda item: int(item.get("holding_score") or 0))
    candidate = max(fresh_candidates, key=_arena_candidate_score)
    holding_score = int(weakest.get("holding_score") or 0)
    candidate_score = _arena_candidate_score(candidate)
    delta = candidate_score - holding_score
    min_delta = int(portfolio.get("min_replacement_score_delta") or portfolio.get("replacement_min_score_delta") or 0)
    sell_notional = _decimal(weakest.get("quantity")) or Decimal("0")
    sell_price = _decimal(weakest.get("current_price")) or _decimal(weakest.get("average_price")) or Decimal("0")
    sell_notional *= sell_price
    buy_notional = _decimal(candidate.get("notional")) or Decimal("0")
    total_turnover = sell_notional + buy_notional
    sell_symbol = str(weakest.get("symbol") or "")
    buy_symbol = str(candidate.get("symbol") or "")
    slippage_pct = _decimal(fees.get("slippage_pct_per_side")) or Decimal("0")
    sell_one_side_pct = _arena_commission_pct(sell_symbol, fees=fees) + slippage_pct
    buy_one_side_pct = _arena_commission_pct(buy_symbol, fees=fees) + slippage_pct
    estimated_cost = (sell_notional * sell_one_side_pct + buy_notional * buy_one_side_pct) / Decimal("100")
    edge_after_cost_pct = Decimal(delta) - (estimated_cost / (_decimal(account.get("equity")) or ARENA_STARTING_EQUITY) * Decimal("100"))
    min_edge_after_cost = _decimal(portfolio.get("min_replacement_edge_after_cost_pct")) or _decimal(portfolio.get("replacement_min_edge_after_cost_pct")) or Decimal("0")
    if delta < min_delta or edge_after_cost_pct < min_edge_after_cost:
        return None
    return {
        "account_id": str(account.get("account_id") or ""),
        "sell_symbol": weakest.get("symbol"),
        "buy_symbol": candidate.get("symbol"),
        "buy_candidate": candidate,
        "holding_score": holding_score,
        "candidate_score": candidate_score,
        "score_delta": delta,
        "estimated_cost_rub": _decimal_payload(estimated_cost),
        "edge_after_cost_pct": _decimal_payload(edge_after_cost_pct),
        "replacement_entries_used": replacement_used,
        "replacement_entries_limit": replacement_limit,
        "broker_mutation": False,
        "confirmation_required": True,
        "confirmation_phrase": f"CONFIRM_ARENA_REPLACE {weakest.get('symbol')} -> {candidate.get('symbol')} {account.get('account_id')}",
    }


def _candidate_allowed_for_replacement(candidate: dict[str, Any]) -> bool:
    reasons = {str(item) for item in candidate.get("gate_reasons") or []}
    hard_blocks = {
        "research_avoid",
        "research_risk_requires_manual_review",
        "research_provider_failure",
        "research_unavailable_requires_manual_confirmation",
        "research_unavailable_below_exceptional_score",
        "entry_strength_confirmation_missing",
        REPEAT_PATTERN_LOSS_COOLDOWN_GATE,
        REPEAT_PATTERN_LOSS_SCORE_GATE,
        PRETRADE_BUDGET_EXHAUSTED_GATE,
        PRETRADE_UNAVAILABLE_GATE,
        PRETRADE_RISK_GATE,
        "candidate_score_below_min",
        "short_availability_not_confirmed",
        "arena_shortability_api_unavailable",
        ARENA_MARGIN_TRADING_NOT_SUPPORTED,
    }
    return not bool(reasons & hard_blocks)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _arena_account_entry_limits(
    profile: ArenaAccountProfile,
    account: dict[str, Any],
    *,
    policy: dict[str, Any],
    now: datetime,
    execution_ledger: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    risk = _risk_settings(policy)
    primary_limit = profile.primary_entries_per_day
    if primary_limit is None:
        primary_limit = int(risk.get("max_new_trades_per_account_per_day") or 1)
    replacement_limit = profile.replacement_entries_per_day
    if replacement_limit is None:
        replacement_limit = int(risk.get("replacement_entries_per_account_per_day") or 1)
    replacement_ledger_available = execution_ledger is not None and not _arena_ledger_has_errors(
        execution_ledger
    )
    replacement_used = (
        _daily_replacement_entry_count(execution_ledger or [], account_id=profile.account_id, now=now)
        if replacement_ledger_available
        else 0
    )
    daily_buy_count = _daily_trade_count(account, now=now, side="BUY", day_tz=ARENA_LIMITS_TIMEZONE)
    primary_used = max(0, daily_buy_count - replacement_used)
    return {
        "primary_used": primary_used,
        "primary_limit": primary_limit,
        "replacement_used": replacement_used,
        "replacement_limit": replacement_limit,
        "replacement_ledger_available": replacement_ledger_available,
        "timezone": str(ARENA_LIMITS_TIMEZONE),
    }


def _scan_account_candidates(
    profile: ArenaAccountProfile,
    account: dict[str, Any],
    *,
    policy: dict[str, Any],
    market_client: FinamClient,
    market_jwt: str,
    market_data_cache: _ArenaMarketDataCache,
    now: datetime,
    warnings: list[str],
) -> list[dict[str, Any]]:
    risk = _risk_settings(policy)
    fees = _fees_settings(policy)
    strategy = _strategy_settings(policy)
    equity = _decimal(account.get("equity")) or ARENA_STARTING_EQUITY
    cash = _decimal(account.get("available_cash")) or _decimal(account.get("cash")) or Decimal("0")
    max_new = int(risk.get("max_new_trades_per_account_per_run") or 1)
    max_daily_operations = int(risk.get("max_daily_operations_per_account") or 200)
    daily_operations = int(account.get("daily_operations_count") or _daily_trade_count(account, now=now, side="ANY"))
    if max_daily_operations > 0 and daily_operations >= max_daily_operations:
        account["top_signal"] = f"дневной лимит операций Arena: {daily_operations}/{max_daily_operations}"
        if account.get("status") == "ACCOUNT_ACTIVE":
            account["status"] = "WATCH"
        warnings.append(f"{profile.account_id}: max_daily_operations_per_account reached; new candidates skipped")
        return []
    entry_limits = account.get("entry_limits") if isinstance(account.get("entry_limits"), dict) else {}
    max_daily_new = int(entry_limits.get("primary_limit") or risk.get("max_new_trades_per_account_per_day") or 1)
    daily_new_count = int(entry_limits.get("primary_used") or 0)
    primary_limit_reached = max_daily_new > 0 and daily_new_count >= max_daily_new
    if primary_limit_reached:
        account["top_signal"] = f"дневной лимит primary BUY: {daily_new_count}/{max_daily_new}"
        if account.get("status") == "ACCOUNT_ACTIVE":
            account["status"] = "WATCH"
        warnings.append(f"{profile.account_id}: primary_daily_limit_reached; ordinary BUY candidates blocked")
        if int(entry_limits.get("replacement_limit") or 0) > 0 and not bool(
            entry_limits.get("replacement_ledger_available")
        ):
            warnings.append(f"{profile.account_id}: replacement_ledger_unavailable; extra replacement allowance blocked")
    max_positions = int(risk.get("max_open_positions") or 0)
    positions_count = int(account.get("positions_count") or 0)
    if max_positions > 0 and positions_count >= max_positions:
        account["top_signal"] = f"лимит позиций: {positions_count}/{max_positions}"
        if account.get("status") == "ACCOUNT_ACTIVE":
            account["status"] = "WATCH"
        warnings.append(f"{profile.account_id}: max_open_positions reached; new candidates skipped")
        return []
    max_symbols = int(strategy.get("max_symbols_per_account_per_scan") or len(profile.universe))
    candidates: list[dict[str, Any]] = []
    for symbol in list(profile.universe)[:max_symbols]:
        executable_count = len([item for item in candidates if item.get("execution_allowed") is True])
        if executable_count >= max_new:
            break
        candidate = _candidate_for_symbol(
            profile,
            account,
            symbol,
            policy=policy,
            risk=risk,
            fees=fees,
            risk_multiplier=profile.risk_multiplier,
            equity=equity,
            cash=cash,
            market_client=market_client,
            market_jwt=market_jwt,
            market_data_cache=market_data_cache,
            now=now,
            warnings=warnings,
        )
        if candidate is not None:
            if primary_limit_reached and str(candidate.get("side") or "BUY").upper() == "BUY":
                _block_arena_candidate(candidate, "primary_daily_limit_reached")
            candidates.append(candidate)
    return candidates


def _candidate_for_symbol(
    profile: ArenaAccountProfile,
    account: dict[str, Any],
    symbol: str,
    *,
    policy: dict[str, Any],
    risk: dict[str, Any],
    fees: dict[str, Any],
    risk_multiplier: Decimal,
    equity: Decimal,
    cash: Decimal,
    market_client: FinamClient,
    market_jwt: str,
    market_data_cache: _ArenaMarketDataCache,
    now: datetime,
    warnings: list[str],
) -> dict[str, Any] | None:
    try:
        h4 = _bars(market_client, market_jwt, symbol, interval="TIME_FRAME_H4", now=now, days=28, cache=market_data_cache)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{profile.account_id}:{symbol}: market data unavailable: {exc}")
        return None

    h4_closed = [bar for bar in h4 if bar.get("close") is not None]
    if len(h4_closed) < 15:
        return None
    h4_direction = _bar_direction(h4_closed[-2], h4_closed[-1])
    if h4_direction == "flat" or (h4_direction == "up" and not profile.allow_long) or (h4_direction == "down" and not profile.allow_short):
        return None

    h1_closed: list[dict[str, Any]] = []
    m30_closed: list[dict[str, Any]] = []
    side: str | None = None
    entry_timeframe: str | None = None
    try:
        h1 = _bars(market_client, market_jwt, symbol, interval="TIME_FRAME_H1", now=now, days=7, cache=market_data_cache)
        h1_closed = [bar for bar in h1 if bar.get("close") is not None]
        if len(h1_closed) >= 2:
            h1_direction = _bar_direction(h1_closed[-2], h1_closed[-1])
            if h4_direction == "up" and h1_direction == "up":
                side, entry_timeframe = "BUY", "H1"
            elif h4_direction == "down" and h1_direction == "down":
                side, entry_timeframe = "SELL", "H1"
        if side is None:
            m30 = _bars(market_client, market_jwt, symbol, interval="TIME_FRAME_M30", now=now, days=7, cache=market_data_cache)
            m30_closed = [bar for bar in m30 if bar.get("close") is not None]
            if len(m30_closed) >= 2:
                m30_direction = _bar_direction(m30_closed[-2], m30_closed[-1])
                if h4_direction == "up" and m30_direction == "up":
                    side, entry_timeframe = "BUY", "M30"
                elif h4_direction == "down" and m30_direction == "down":
                    side, entry_timeframe = "SELL", "M30"
        if side is None or entry_timeframe is None:
            return None
        quote = market_data_cache.quote(market_client, market_jwt, symbol)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"{profile.account_id}:{symbol}: market data unavailable: {exc}")
        return None
    if side == "BUY" and not profile.allow_long:
        return None

    fallback_bars = h1_closed if h1_closed else m30_closed
    current = _quote_price(quote) or _decimal(fallback_bars[-1].get("close"))
    atr = _atr(h4_closed, period=14)
    if current is None or current <= 0 or atr is None or atr <= 0:
        return None
    stop_multiplier = _decimal(risk.get("stop_atr_multiplier")) or Decimal("2")
    take_profit_r = _decimal(risk.get("take_profit_r")) or Decimal("2")
    risk_per_share = atr * stop_multiplier
    risk_rub = equity * (_decimal(risk.get("risk_per_trade_pct")) or Decimal("1")) * risk_multiplier / Decimal("100")
    max_position_notional_pct = _arena_max_position_notional_pct(
        risk,
        account_id=profile.account_id,
        symbol=symbol,
    )
    max_notional = equity * max_position_notional_pct / Decimal("100")
    raw_quantity = min(
        (risk_rub / risk_per_share).to_integral_value(rounding=ROUND_FLOOR),
        (cash / current).to_integral_value(rounding=ROUND_FLOOR),
        (max_notional / current).to_integral_value(rounding=ROUND_FLOOR),
    )
    if raw_quantity <= 0:
        return None
    costs = _arena_trade_costs(raw_quantity * current, symbol=symbol, fees=fees)
    if side == "BUY":
        stop = current - risk_per_share
        take_profit = current + risk_per_share * take_profit_r
        short_availability = None
        gate_reasons = _arena_long_entry_gate_reasons(
            account,
            symbol=symbol,
            notional=raw_quantity * current,
            equity=equity,
            risk=risk,
            now=now,
        )
        execution_allowed = not gate_reasons
    else:
        stop = current + risk_per_share
        take_profit = current - risk_per_share * take_profit_r
        short_availability = _arena_margin_disabled_short_availability()
        execution_allowed = False
        gate_reasons = [ARENA_MARGIN_TRADING_NOT_SUPPORTED]

    nearest_target_r = abs(take_profit - current) / risk_per_share if risk_per_share > 0 else None
    candidate = {
        "account_id": profile.account_id,
        "account_label": profile.label,
        "strategy": profile.strategy,
        "symbol": symbol,
        "side": side,
        "status": "WATCH" if side == "SELL" else "PROPOSE_ONLY",
        "execution_allowed": execution_allowed,
        "gate_reasons": gate_reasons,
        "short_entry_forbidden": side == "SELL",
        "entry_timeframe": entry_timeframe,
        "regime_timeframe": "H4",
        "setup_signature": _repeat_pattern_signature(account_id=profile.account_id, mic=_symbol_mic(symbol), side=side),
        "entry_price": _decimal_payload(current),
        "quantity": str(int(raw_quantity)),
        "notional": _decimal_payload(raw_quantity * current),
        "stop_price": _decimal_payload(stop),
        "take_profit_price": _decimal_payload(take_profit),
        "risk_per_share": _decimal_payload(risk_per_share),
        "risk_rub": _decimal_payload(raw_quantity * risk_per_share),
        "atr_h4": _decimal_payload(atr),
        "nearest_target_r": _decimal_payload(nearest_target_r),
        "relative_strength_score": _relative_strength_score(h4_closed, side=side),
        "liquidity_score": 10,
        "short_availability": short_availability,
        "technical_context": _arena_technical_context(h4_closed, side=side),
        "risk_adjusted_momentum": _arena_risk_adjusted_momentum_context(h4_closed, side=side),
        "news_event_context": _arena_news_event_context(symbol),
        "relative_value_context": _arena_relative_value_context(symbol, policy),
        "costs": costs,
        "signal": {
            "h4": _bar_direction(h4_closed[-2], h4_closed[-1]),
            "h1": _bar_direction(h1_closed[-2], h1_closed[-1]) if len(h1_closed) >= 2 else "unavailable",
            "m30": _bar_direction(m30_closed[-2], m30_closed[-1]) if len(m30_closed) >= 2 else "unavailable",
        },
    }
    candidate["arena_growth_score"] = _arena_growth_score(candidate, research={})
    return candidate


def _candidate_signal_line(candidate: dict[str, Any]) -> str:
    side = "LONG" if candidate.get("side") == "BUY" else "SHORT"
    return f"{side} {candidate.get('symbol')} @ {candidate.get('entry_price')}"


def _candidate_block_reason_line(candidate: dict[str, Any]) -> str:
    reasons = candidate.get("gate_reasons") if isinstance(candidate.get("gate_reasons"), list) else []
    reason = str(reasons[0]) if reasons else "execution_gate_blocked"
    return f"нет исполнимого сигнала: {reason}"


def _h4_entry_signal(
    h4: list[dict[str, Any]],
    h1: list[dict[str, Any]],
    m30: list[dict[str, Any]],
    *,
    allow_short: bool,
) -> tuple[str, str] | None:
    h4_direction = _bar_direction(h4[-2], h4[-1])
    h1_direction = _bar_direction(h1[-2], h1[-1]) if len(h1) >= 2 else "flat"
    m30_direction = _bar_direction(m30[-2], m30[-1]) if len(m30) >= 2 else "flat"
    if h4_direction == "up" and h1_direction == "up":
        return ("BUY", "H1")
    if h4_direction == "up" and m30_direction == "up":
        return ("BUY", "M30")
    if allow_short and h4_direction == "down" and h1_direction == "down":
        return ("SELL", "H1")
    if allow_short and h4_direction == "down" and m30_direction == "down":
        return ("SELL", "M30")
    return None


def _bar_direction(previous: dict[str, Any], current: dict[str, Any]) -> str:
    prev_close = _decimal(previous.get("close"))
    close = _decimal(current.get("close"))
    if prev_close is None or close is None:
        return "flat"
    if close > prev_close:
        return "up"
    if close < prev_close:
        return "down"
    return "flat"


def _bars(
    client: FinamClient,
    jwt: str,
    symbol: str,
    *,
    interval: str,
    now: datetime,
    days: int,
    cache: _ArenaMarketDataCache | None = None,
) -> list[dict[str, Any]]:
    if cache is not None:
        return cache.bars(client, jwt, symbol, interval=interval, days=days)
    return _fetch_bars(client, jwt, symbol, interval=interval, now=now, days=days)


def _fetch_bars(client: FinamClient, jwt: str, symbol: str, *, interval: str, now: datetime, days: int) -> list[dict[str, Any]]:
    response = client.bars(
        jwt,
        symbol,
        interval=interval,
        start_time=_iso_z(now - timedelta(days=days)),
        end_time=_iso_z(now),
    )
    raw = response.get("bars") if isinstance(response.get("bars"), list) else []
    parsed = [_parse_bar(item) for item in raw]
    return sorted([item for item in parsed if item is not None], key=lambda item: str(item.get("time")))


def _market_data_bucket(now: datetime, interval: str) -> str:
    return _iso_z(_market_data_bucket_start(now, interval))


def _market_data_bucket_expires_at(now: datetime, interval: str) -> datetime:
    return _market_data_bucket_start(now, interval) + _timeframe_delta(interval)


def _market_data_bucket_start(now: datetime, interval: str) -> datetime:
    current = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    delta = _timeframe_delta(interval)
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 0:
        return current
    day_start = current.replace(hour=0, minute=0)
    elapsed_minutes = int((current - day_start).total_seconds() // 60)
    bucket_minutes = elapsed_minutes - (elapsed_minutes % minutes)
    return day_start + timedelta(minutes=bucket_minutes)


def _timeframe_delta(interval: str) -> timedelta:
    normalized = str(interval)
    if normalized.endswith("_M30"):
        return timedelta(minutes=30)
    if normalized.endswith("_H1"):
        return timedelta(hours=1)
    if normalized.endswith("_H4"):
        return timedelta(hours=4)
    return timedelta(minutes=1)


def _market_data_429_count(warnings: list[str]) -> int:
    return sum(1 for item in warnings if "Finam HTTP 429" in item)


def _parse_bar(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    close = _first_value(item, "close")
    high = _first_value(item, "high")
    low = _first_value(item, "low")
    open_ = _first_value(item, "open")
    if close is None or high is None or low is None:
        return None
    return {
        "time": item.get("time") or item.get("timestamp") or item.get("date"),
        "open": _decimal_payload(open_),
        "high": _decimal_payload(high),
        "low": _decimal_payload(low),
        "close": _decimal_payload(close),
    }


def _atr(bars: list[dict[str, Any]], *, period: int) -> Decimal | None:
    if len(bars) < period + 1:
        return None
    ranges: list[Decimal] = []
    for index in range(1, len(bars)):
        high = _decimal(bars[index].get("high"))
        low = _decimal(bars[index].get("low"))
        previous_close = _decimal(bars[index - 1].get("close"))
        if high is None or low is None or previous_close is None:
            continue
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(ranges) < period:
        return None
    return sum(ranges[-period:], Decimal("0")) / Decimal(period)


def _quote_price(quote: dict[str, Any]) -> Decimal | None:
    for key in ("last", "price", "last_price", "close"):
        value = _first_value(quote, key)
        parsed = _decimal(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _arena_margin_disabled_short_availability() -> dict[str, Any]:
    return {
        "available": False,
        "verified": True,
        "reason": ARENA_MARGIN_TRADING_NOT_SUPPORTED,
        "gate_reason": ARENA_MARGIN_TRADING_NOT_SUPPORTED,
        "source": "arena_policy_margin_disabled",
    }


def _short_availability(client: FinamClient, jwt: str, symbol: str, *, account_id: str) -> dict[str, Any]:
    try:
        params = client.asset_params(jwt, symbol, account_id=account_id)
    except Exception as exc:  # noqa: BLE001
        reason = _short_availability_error_reason(exc, account_id=account_id)
        return {
            "available": False,
            "verified": False,
            "reason": reason,
            "gate_reason": reason,
            "source": "finam_trade_api_asset_params",
        }
    raw = _first_value(_first_value(params, "shortable"), "value")
    if raw is None:
        raw = _first_value(_first_value(params, "short_available"), "value")
    if raw is None:
        raw = _first_value(params, "shortable")
    available = raw == "AVAILABLE" or raw is True or str(raw).upper() == "AVAILABLE"
    result: dict[str, Any] = {
        "available": available,
        "verified": True,
        "raw": raw,
        "source": "finam_trade_api_asset_params",
    }
    if not available:
        result["gate_reason"] = "short_availability_not_confirmed"
    return result


def _short_availability_error_reason(exc: Exception, *, account_id: str) -> str:
    text = str(exc)
    if account_id in ARENA_ACCOUNT_IDS and "Account with id" in text and "not found" in text:
        return "arena_shortability_api_unavailable"
    return "short_availability_not_confirmed"


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def arena_keyboard_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "📊 Обзор", "callback_data": "arena:overview"}],
            [
                {"text": "🇷🇺 DEMO-RU РФ", "callback_data": "arena:account:DEMO-RU"},
                {"text": "🇺🇸 DEMO-US США", "callback_data": "arena:account:DEMO-US"},
            ],
            [{"text": "🧪 DEMO-AI AI", "callback_data": "arena:account:DEMO-AI"}],
            [
                {"text": "🚨 Риски", "callback_data": "arena:risks"},
                {"text": "⚙️ Стратегия", "callback_data": "arena:strategy"},
            ],
            [{"text": "🔁 Ротация", "callback_data": "arena:rotation"}],
            [{"text": "📊 Attribution", "callback_data": "arena:attribution"}],
        ]
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _risk_settings(policy: dict[str, Any]) -> dict[str, Any]:
    risk = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    return _deep_merge(DEFAULT_ARENA_POLICY["risk"], risk)


def _fees_settings(policy: dict[str, Any]) -> dict[str, Any]:
    fees = policy.get("fees") if isinstance(policy.get("fees"), dict) else {}
    return _deep_merge(DEFAULT_ARENA_POLICY["fees"], fees)


def _portfolio_settings(policy: dict[str, Any]) -> dict[str, Any]:
    portfolio = policy.get("portfolio") if isinstance(policy.get("portfolio"), dict) else {}
    merged = _deep_merge(DEFAULT_ARENA_POLICY["portfolio"], portfolio)
    learning = learning_settings(policy)
    merged["learning_deprioritize_symbols"] = list(_learning_deprioritized_symbols(learning))
    merged["learning_attribution_uncertain_symbols"] = list(_learning_attribution_uncertain_symbols(learning))
    merged["learning_deprioritized_exit_negative_r_threshold"] = learning.get("deprioritized_exit_negative_r_threshold", "-0.10")
    return merged


def _strategy_settings(policy: dict[str, Any]) -> dict[str, Any]:
    strategy = policy.get("strategy") if isinstance(policy.get("strategy"), dict) else {}
    return _deep_merge(DEFAULT_ARENA_POLICY["strategy"], strategy)


def _first_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _first_value(item, key)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _first_value(item, key)
            if found is not None:
                return found
    return None


def _first_existing(value: Any, keys: tuple[str, ...]) -> Any:
    for key in keys:
        found = _first_value(value, key)
        if found is not None:
            return found
    return None


def _decimal_payload(value: Any) -> str | None:
    parsed = _decimal(value)
    return format(parsed, "f") if parsed is not None else None


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _bounded_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, dict):
        if "units" in value or "nanos" in value:
            units = Decimal(str(value.get("units") or "0"))
            nanos = Decimal(str(value.get("nanos") or "0")) / Decimal("1000000000")
            return units + nanos
        value = value.get("value") or value.get("num") or value.get("amount")
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
