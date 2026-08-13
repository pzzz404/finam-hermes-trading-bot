#!/usr/bin/env python3
"""Finam H4 portfolio monitor used by Hermes and the host scheduler.

The monitor is intentionally deterministic: it fetches account state, checks
protective stops, recalculates H4 ATR, builds trade candidates, and prints a
machine-readable report. Hermes remains the trading operator in supervised mode.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.client import FinamClient  # noqa: E402
from finam_trading_bot.config import get_finam_account_id, get_finam_token  # noqa: E402
from finam_trading_bot.contract import (  # noqa: E402
    FinamInstrumentRules,
    fallback_rules,
    normalize_price,
    normalize_quantity_to_lot,
    rules_from_finam,
)
from finam_trading_bot.operator_journal import write_event  # noqa: E402
from finam_trading_bot.research import latest_research_artifact, research_candidates  # noqa: E402


LOCK_PATH = Path("/tmp/finam-h4-monitor.lock")
DEFAULT_POLICY_PATH = ROOT / "config" / "finam_h4_policy.json"
MOSCOW_TZ = timezone(timedelta(hours=3))
FINAM_NO_PROXY_HOSTS = ("finam.ru", ".finam.ru", "api.finam.ru", "www.finam.ru")

DEFAULT_POLICY: dict[str, Any] = {
    "mode": "supervised",
    "account_id": "DEMO-ACCOUNT",
    "schedule_msk": ["10:00", "14:00", "18:00", "22:00"],
    "risk": {
        "risk_per_trade_pct": "1.0",
        "atr_period": 14,
        "stop_atr_multiplier": "2",
        "take_profit_r": "2",
        "min_target_r": "1.5",
        "max_total_open_risk_pct": "3.0",
        "max_open_positions": 5,
        "max_new_trades_per_run": 1,
    },
    "permissions": {
        "protective_stops_auto": True,
        "new_buys_require_telegram_confirmation": True,
        "manual_sells_require_confirmation": True,
        "shorts_require_separate_confirmation": True,
        "second_tier_requires_confirmation": True,
        "unknown_target_requires_confirmation": True,
        "incomplete_candidate_blocks_trade": True,
    },
    "universe": [
        "SBER@MISX",
        "GAZP@MISX",
        "LKOH@MISX",
        "ROSN@MISX",
        "TATN@MISX",
        "MOEX@MISX",
        "PLZL@MISX",
        "MTSS@MISX",
        "CHMF@MISX",
        "SNGSP@MISX",
        "NVTK@MISX",
        "GMKN@MISX",
    ],
    "dynamic_universe": {
        "enabled": True,
        "boards": ["TQBR"],
        "max_scan_symbols": 80,
        "max_dynamic_candidates": 8,
        "max_research_symbols": 2,
        "min_daily_turnover_rub": 100000000,
        "min_trades_today": 500,
        "min_price_rub": 5,
        "require_finam_longable": True,
        "dynamic_candidates_require_confirmation": True,
        "auto_add_to_static_universe": False,
        "openrouter_budget_policy": "no_increase_h4_default",
    },
    "research": {
        "enabled": True,
        "provider": "codex_review",
        "model": "openai-codex",
        "h4_model": "perplexity/sonar",
        "pretrade_model": "perplexity/sonar-pro",
        "daily_model": "perplexity/sonar-pro",
        "weekly_model": "perplexity/sonar-pro",
        "timeout_seconds": 20,
        "max_candidates": 1,
        "context_max_bytes": 4096,
        "codex_review_ttl_minutes": 240,
        "recent_outcomes_limit": 5,
        "rss_flags_limit": 5,
        "review_dir": "data/runtime/codex_reviews",
        "run_for_blocked_candidates": False,
        "h4_max_candidates": 2,
        "h4_max_tokens": 350,
        "h4_cache_ttl_seconds": 1800,
        "daily_max_tokens": 1000,
        "weekly_max_tokens": 1600,
        "pretrade_max_tokens": 800,
        "daily_max_symbols": 12,
        "weekly_max_symbols": 24,
        "finam_rss_enabled": True,
        "finam_rss_url": "https://www.finam.ru/analysis/conews/rsspoint/",
        "failure_policy": {
            "supervised": "show_candidate_and_require_manual_confirmation",
            "autonomous_demo": "retry_then_fallback_then_reduced_risk",
            "allow_reduced_risk_trade_if_research_unavailable": False,
            "reduced_risk_per_trade_pct": "0.5",
            "reduced_risk_max_trades_per_day": 1,
            "explicit_avoid_blocks_trade": True,
            "research_unavailable_requires_confirmation": True,
        },
    },
    "growth_mode": {
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
    },
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only", action="store_true", help="Required. This command never mutates broker state.")
    parser.add_argument("--once", action="store_true", help="Run one monitoring pass.")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY_PATH), help="Path to H4 policy JSON.")
    parser.add_argument("--skip-research", action="store_true", help="Skip Codex-review facts collection.")
    args = parser.parse_args()

    if not args.read_only:
        print(json.dumps({"status": "NO_TRADE", "error": "--read-only is required"}, ensure_ascii=False, indent=2))
        return 2

    with LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "NO_TRADE", "error": "another h4 monitor run is active"}, indent=2))
            return 2

        report = build_report(policy_path=Path(args.policy), include_research=not args.skip_research)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] in {"OK", "DEGRADED", "NO_TRADE"} else 2


def build_report(
    *,
    policy_path: Path | None = None,
    include_research: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = now or datetime.now(timezone.utc)
    policy = load_policy(policy_path or DEFAULT_POLICY_PATH)
    raw_risk = policy.get("risk")
    risk: dict[str, Any] = raw_risk if isinstance(raw_risk, dict) else {}
    report: dict[str, Any] = {
        "status": "OK",
        "mode": "read_only",
        "operator_mode": policy.get("mode", "supervised"),
        "time_utc": now_utc.isoformat(timespec="seconds"),
        "time_msk": now_utc.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M"),
        "policy": _public_policy(policy),
        "account": {},
        "positions": [],
        "zero_positions": [],
        "orders": {},
        "static_universe": [str(item) for item in policy.get("universe") or []],
        "dynamic_watchlist": [],
        "candidates": [],
        "candidate_pool": [],
        "short_candidates": [],
        "research": {"status": "skipped"},
        "research_context": {},
        "growth": _growth_kpi(policy, {}, now=now_utc),
        "errors": [],
        "warnings": [],
    }

    try:
        route_check = _route_env_self_check(os.environ)
        report["route_check"] = route_check
        report["warnings"].extend(route_check.get("warnings") or [])
        if route_check.get("errors"):
            report["errors"].extend(route_check["errors"])
            return report

        token = get_finam_token()
        account_id = get_finam_account_id() or str(policy.get("account_id") or "").strip()
        if not account_id:
            raise RuntimeError("FINAM_ACCOUNT_ID is not set")

        client = FinamClient()
        jwt = client.create_session(token)
        details = client.session_details(jwt)
        account = client.get_account(jwt, account_id)
        orders = client.orders(jwt, account_id)

        raw_orders = orders.get("orders") or orders.get("items") or []
        active_orders = _active_orders(raw_orders)
        stop_details = _watching_sell_order_details_by_symbol(active_orders)
        report["orders"] = {
            "orders_count": len(active_orders),
            "watching_sell_sltp_by_symbol": {symbol: len(items) for symbol, items in stop_details.items()},
            "watching_sell_sltp_details_by_symbol": stop_details,
        }

        report["account"] = {
            "account_id": account_id,
            "status": account.get("status"),
            "readonly": details.get("readonly"),
            "equity": _decimal_payload(account.get("equity")),
            "cash": _cash_payload(account.get("cash"), currency="RUB"),
            "available_cash": _decimal_payload((account.get("portfolio_mc") or {}).get("available_cash")),
            "unrealized_pnl": _decimal_payload(account.get("unrealized_profit")),
        }
        report["growth"] = _growth_kpi(policy, report["account"], now=now_utc)
        if account.get("status") != "ACCOUNT_ACTIVE":
            report["warnings"].append("account is not ACCOUNT_ACTIVE")

        positions = account.get("positions") if isinstance(account.get("positions"), list) else []
        held_symbols: set[str] = set()
        held_positions_by_symbol: dict[str, dict[str, Any]] = {}
        for raw_position in positions:
            if not isinstance(raw_position, dict):
                continue
            position = _base_position_payload(raw_position, stop_details)
            symbol = str(position["symbol"])
            qty = _decimal(position.get("quantity"))
            if qty is None or qty == 0:
                report["zero_positions"].append(position | {"closed_note": _closed_note(position)})
                continue

            held_symbols.add(symbol)
            market = _market_context(client, jwt, symbol, now=now_utc, risk=risk, errors=report["warnings"])
            enriched_position = _enrich_position(position, market, risk)
            report["positions"].append(enriched_position)
            held_positions_by_symbol[symbol] = enriched_position
        _apply_growth_position_r(report["growth"], report["positions"])

        open_risk_rub = _total_open_risk_rub(report["positions"])
        equity = _decimal(report["account"].get("equity"))
        max_total_open_risk_pct = _decimal(risk.get("max_total_open_risk_pct")) or Decimal("3.0")
        report["account"]["open_risk_rub"] = _decimal_payload(open_risk_rub)
        report["account"]["open_risk_pct"] = _decimal_payload(
            (open_risk_rub / equity * Decimal("100")) if equity and equity > 0 else None
        )
        report["account"]["max_total_open_risk_pct"] = str(max_total_open_risk_pct)

        dynamic_watchlist = _discover_dynamic_watchlist(policy, now=now_utc, errors=report["warnings"])
        report["dynamic_watchlist"] = dynamic_watchlist
        report["candidates"] = _scan_candidates(
            client,
            jwt,
            account_id=account_id,
            policy=policy,
            held_symbols=held_symbols,
            held_positions_by_symbol=held_positions_by_symbol,
            cash=_decimal(report["account"].get("cash")),
            equity=equity,
            open_positions=len(report["positions"]),
            open_risk_rub=open_risk_rub,
            now=now_utc,
            errors=report["warnings"],
            short_candidates=report["short_candidates"],
            dynamic_watchlist=dynamic_watchlist,
        )
        report["candidate_pool"] = report["candidates"]
        _apply_dynamic_watchlist_candidate_status(report["dynamic_watchlist"], report["candidates"])
        _enrich_dynamic_watchlist_risk_reward(
            client,
            jwt,
            report["dynamic_watchlist"],
            now=now_utc,
            risk=risk,
        )
        if include_research:
            research_candidates_for_provider = _researchable_candidates(report["candidates"], policy)
            report["research"] = _research_candidates(research_candidates_for_provider, policy)
            report["research"]["candidate_count"] = len(research_candidates_for_provider)
            report["research"]["skipped_blocked_candidates"] = len(report["candidates"]) - len(research_candidates_for_provider)
            failure_policy = (policy.get("research") or {}).get("failure_policy") if isinstance(policy.get("research"), dict) else {}
            _apply_research_gates(
                report["candidates"],
                report["research"],
                explicit_avoid_blocks_trade=bool((failure_policy or {}).get("explicit_avoid_blocks_trade", True)),
            )
        report["research_context"] = _load_research_context(now=now_utc)
        _apply_research_context(report["candidates"], report["research_context"])
        _apply_growth_scores(report["candidates"], report["research"])
        _write_scan_journal(report)
    except Exception as exc:  # noqa: BLE001 - report should preserve failure cause.
        report["errors"].append(str(exc))

    if report["errors"]:
        report["status"] = "NO_TRADE"
    elif report["warnings"]:
        report["status"] = "DEGRADED"
    else:
        report["status"] = "OK"
    return report


def load_policy(path: Path) -> dict[str, Any]:
    policy = deepcopy(DEFAULT_POLICY)
    if not path.exists():
        return policy
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise RuntimeError(f"policy file is not a JSON object: {path}")
    return _deep_merge(policy, data)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _public_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": policy.get("mode"),
        "schedule_msk": policy.get("schedule_msk"),
        "risk": policy.get("risk"),
        "permissions": policy.get("permissions"),
        "universe_size": len(policy.get("universe") or []),
        "dynamic_universe": policy.get("dynamic_universe"),
        "research": policy.get("research"),
        "growth_mode": policy.get("growth_mode"),
    }


def _route_env_self_check(env: Any) -> dict[str, Any]:
    http_proxy = str(env.get("HTTP_PROXY") or env.get("http_proxy") or "")
    https_proxy = str(env.get("HTTPS_PROXY") or env.get("https_proxy") or "")
    all_proxy = str(env.get("ALL_PROXY") or env.get("all_proxy") or "")
    no_proxy = str(env.get("NO_PROXY") or env.get("no_proxy") or "")
    no_proxy_entries = _no_proxy_entries(no_proxy)
    proxies = {"HTTP_PROXY": http_proxy, "HTTPS_PROXY": https_proxy, "ALL_PROXY": all_proxy}
    expected_proxy = str(env.get("FINAM_EXPECTED_PROXY") or "")
    errors: list[str] = []
    warnings: list[str] = []

    finam_direct = {host: _host_bypassed_by_no_proxy(host, no_proxy) for host in FINAM_NO_PROXY_HOSTS}
    proxy_configured = any(proxies.values())
    finam_route_ok = not proxy_configured or all(finam_direct.values())
    if not finam_route_ok:
        errors.append("Finam direct route is blocked: NO_PROXY must include finam.ru,.finam.ru,api.finam.ru,www.finam.ru")

    for key, value in proxies.items():
        if expected_proxy and value and value != expected_proxy:
            warnings.append(f"{key} does not match FINAM_EXPECTED_PROXY")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "expected_proxy": expected_proxy or None,
        "proxy_configured": proxy_configured,
        "no_proxy_hosts": no_proxy_entries,
        "routes": {
            "finam": {"mode": "direct_via_no_proxy_if_proxy_configured", "ok": finam_route_ok, "hosts": finam_direct},
            "telegram": {"mode": "environment_default", "ok": True},
        },
    }


def _growth_kpi(policy: dict[str, Any], account: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    growth_mode = policy.get("growth_mode")
    if not isinstance(growth_mode, dict):
        growth_mode = {}
    target = _decimal(growth_mode.get("target_annual_return")) or Decimal("0.5")
    stretch = _decimal(growth_mode.get("stretch_annual_return")) or Decimal("1.0")
    enabled = bool(growth_mode.get("enabled", False))
    report_only = bool(growth_mode.get("report_only", True))
    current_equity = _decimal(account.get("equity"))
    mtd_start_equity = _decimal(growth_mode.get("mtd_start_equity") or growth_mode.get("period_start_equity"))
    ytd_start_equity = _decimal(growth_mode.get("ytd_start_equity") or growth_mode.get("period_start_equity"))
    mtd_return_pct = _return_pct(current_equity, mtd_start_equity)
    ytd_return_pct = _return_pct(current_equity, ytd_start_equity)
    now_utc = now or datetime.now(timezone.utc)
    elapsed_days = max(1, now_utc.timetuple().tm_yday)
    target_required_ytd = Decimal(str((math.pow(1 + float(target), elapsed_days / 365) - 1) * 100))
    target_gap_pct = None
    target_gap_rub = None
    if ytd_return_pct is not None:
        target_gap_pct = max(Decimal("0"), target_required_ytd - ytd_return_pct)
    if ytd_start_equity is not None and current_equity is not None:
        required_equity = ytd_start_equity * (Decimal("1") + target_required_ytd / Decimal("100"))
        target_gap_rub = max(Decimal("0"), required_equity - current_equity)
    goal_status = _growth_goal_status(
        current_equity=current_equity,
        ytd_start_equity=ytd_start_equity,
        ytd_return_pct=ytd_return_pct,
        target_gap_pct=target_gap_pct,
    )
    return {
        "enabled": enabled,
        "report_only": report_only,
        "target_annual_return": _decimal_payload(target),
        "stretch_annual_return": _decimal_payload(stretch),
        "target_required_monthly_pct": _required_period_pct(target, periods=12),
        "target_required_weekly_pct": _required_period_pct(target, periods=52),
        "target_required_trading_day_pct": _required_period_pct(target, periods=252),
        "stretch_required_monthly_pct": _required_period_pct(stretch, periods=12),
        "stretch_required_weekly_pct": _required_period_pct(stretch, periods=52),
        "stretch_required_trading_day_pct": _required_period_pct(stretch, periods=252),
        "target_required_ytd_pct": _pct_payload(target_required_ytd),
        "current_equity": _decimal_payload(current_equity),
        "mtd_start_equity": _decimal_payload(mtd_start_equity),
        "ytd_start_equity": _decimal_payload(ytd_start_equity),
        "mtd_return_pct": _pct_payload(mtd_return_pct),
        "ytd_return_pct": _pct_payload(ytd_return_pct),
        "target_gap_pct": _pct_payload(target_gap_pct),
        "target_gap_rub": _pct_payload(target_gap_rub),
        "accumulated_r": _decimal_payload(growth_mode.get("accumulated_r")),
        "goal_status": goal_status,
        "tracking_status": "report_only",
    }


def _required_period_pct(annual_return: Decimal, *, periods: int) -> str:
    pct = (math.pow(1 + float(annual_return), 1 / periods) - 1) * 100
    return f"{pct:.2f}"


def _return_pct(current: Decimal | None, baseline: Decimal | None) -> Decimal | None:
    if current is None or baseline is None or baseline <= 0:
        return None
    return (current - baseline) / baseline * Decimal("100")


def _pct_payload(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}"


def _growth_goal_status(
    *,
    current_equity: Decimal | None,
    ytd_start_equity: Decimal | None,
    ytd_return_pct: Decimal | None,
    target_gap_pct: Decimal | None,
) -> str:
    if current_equity is None or ytd_start_equity is None or ytd_return_pct is None or target_gap_pct is None:
        return "REPORT_ONLY"
    if current_equity < ytd_start_equity:
        return "DRAWDOWN"
    if target_gap_pct <= 0:
        return "AHEAD"
    if target_gap_pct <= Decimal("0.25"):
        return "ON_TRACK"
    return "BEHIND"


def _apply_growth_position_r(growth: dict[str, Any], positions: list[dict[str, Any]]) -> None:
    total = Decimal("0")
    seen = False
    for position in positions:
        progress = _decimal(position.get("progress_r"))
        if progress is None:
            continue
        total += progress
        seen = True
    if not seen:
        return
    growth["open_position_r"] = _decimal_payload(total)


def _base_position_payload(position: dict[str, Any], stop_details: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    symbol = _position_symbol(position)
    symbol_stop_details = stop_details.get(symbol) or []
    return {
        "symbol": symbol,
        "quantity": _decimal_payload(position.get("quantity")),
        "average_price": _decimal_payload(position.get("average_price") or position.get("price")),
        "current_price": _decimal_payload(position.get("current_price")),
        "daily_pnl": _decimal_payload(position.get("daily_pnl")),
        "unrealized_pnl": _decimal_payload(position.get("unrealized_pnl")),
        "has_watching_sell_sltp": bool(symbol_stop_details),
        "watching_sell_sltp_details": symbol_stop_details,
    }


def _enrich_position(position: dict[str, Any], market: dict[str, Any], risk: dict[str, Any]) -> dict[str, Any]:
    entry = _decimal(position.get("average_price"))
    current = _decimal(position.get("current_price"))
    atr = _decimal(market.get("atr14"))
    stop_multiplier = _decimal(risk.get("stop_atr_multiplier")) or Decimal("2")
    take_profit_r = _decimal(risk.get("take_profit_r")) or Decimal("2")

    stop = entry - stop_multiplier * atr if entry is not None and atr is not None else None
    r_value = entry - stop if entry is not None and stop is not None else None
    target = entry + take_profit_r * r_value if entry is not None and r_value is not None else None
    progress = (current - entry) / r_value if current is not None and entry is not None and r_value and r_value > 0 else None

    status = "HOLD"
    if progress is None or progress < Decimal("0.25"):
        status = "WATCH"
    if current is not None and stop is not None and current <= stop:
        status = "SELL/STOP"

    return position | {
        "status": status,
        "market_data_source": market.get("market_data_source"),
        "h4_fresh": market.get("h4_fresh"),
        "latest_h4": market.get("latest_h4"),
        "atr14": _decimal_payload(atr),
        "calculated_stop": _decimal_payload(_round_price(stop)),
        "tp_2r": _decimal_payload(_round_price(target)),
        "progress_r": _decimal_payload(progress),
        "signal": market.get("signal"),
        "advisory_action": _position_advisory_action(progress, status, market.get("signal")),
    }


def _market_context(
    client: FinamClient,
    jwt: str,
    symbol: str,
    *,
    now: datetime,
    risk: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    end_dt = now
    # MOEX ISS candle responses are row-limited; a compact window keeps fallback
    # data fresh while still providing enough H4 bars for ATR(14).
    start_dt = end_dt - timedelta(days=21)
    start = _iso_z(start_dt)
    end = _iso_z(end_dt)
    try:
        bars_response = client.bars(jwt, symbol, interval="TIME_FRAME_H4", start_time=start, end_time=end)
        bars = bars_response.get("bars") or []
        parsed = [_parse_bar(item) for item in bars]
        parsed = [item for item in parsed if item is not None]
        source = "finam_rest"
    except Exception as exc:  # noqa: BLE001 - fallback keeps the report alive.
        errors.append(f"{symbol}: Finam H4 bars недоступны: {exc}")
        parsed = _moex_h4_bars(symbol, start_dt=start_dt, end_dt=end_dt, errors=errors)
        source = "moex_iss" if parsed else "none"

    parsed.sort(key=lambda item: item["time"])
    closed = _closed_bars(parsed, now=end_dt, timeframe=timedelta(hours=4))
    latest = closed[-1] if closed else None
    forming = parsed[-1] if parsed and (not latest or parsed[-1]["time"] != latest["time"]) else None
    atr = _atr(closed, period=int(risk.get("atr_period") or 14))
    h4_fresh = _h4_fresh(latest, now=end_dt)
    if latest is None:
        errors.append(f"{symbol}: нет закрытых H4 свечей")
    elif not h4_fresh:
        errors.append(f"{symbol}: H4 данные не выглядят свежими")

    return {
        "symbol": symbol,
        "market_data_source": source,
        "h4_bars_count": len(parsed),
        "closed_h4_bars_count": len(closed),
        "latest_h4": latest,
        "forming_h4": forming,
        "h4_fresh": h4_fresh,
        "atr14": str(atr) if atr is not None else None,
        "closed_h4": closed,
        "signal": _long_signal(latest, closed),
        "short_signal": _short_signal(latest, closed),
    }


def _position_advisory_action(progress: Decimal | None, status: str, signal: Any) -> dict[str, Any]:
    if status == "SELL/STOP":
        return {
            "action": "EXIT_CANDIDATE",
            "report_only": True,
            "reason": "цена у расчётного стопа; только проверить, заявок не менять без подтверждения",
        }
    if progress is None:
        return {"action": "HOLD", "report_only": True, "reason": "недостаточно данных для R-сопровождения"}
    if progress >= Decimal("2"):
        return {
            "action": "REDUCE_CANDIDATE",
            "report_only": True,
            "reason": "+2R достигнут; можно предложить частичную фиксацию/tighter trailing, без заявки",
        }
    if progress >= Decimal("1.5"):
        return {
            "action": "TRAIL_CANDIDATE",
            "report_only": True,
            "reason": "+1.5R достигнут; можно предложить tighter trailing, но стоп не двигать без подтверждения",
        }
    if progress >= Decimal("1"):
        return {
            "action": "BREAKEVEN_CANDIDATE",
            "report_only": True,
            "reason": "+1R достигнут; можно предложить breakeven/trailing, но стоп не двигать без подтверждения",
        }
    if isinstance(signal, dict) and signal.get("status") != "WATCH" and progress < 0:
        return {
            "action": "EXIT_CANDIDATE",
            "report_only": True,
            "reason": "momentum ухудшился при отрицательном R; только advisory, без sell-заявки",
        }
    return {"action": "HOLD", "report_only": True, "reason": "до +1R не дошли; стоп не менять"}


def _held_scale_in_candidate(
    client: FinamClient,
    jwt: str,
    *,
    symbol: str,
    account_id: str,
    held_position: dict[str, Any],
    policy: dict[str, Any],
    cash: Decimal,
    equity: Decimal,
    open_risk_rub: Decimal,
    max_total_open_risk_pct: Decimal,
    now: datetime,
    errors: list[str],
) -> dict[str, Any] | None:
    raw_risk = policy.get("risk")
    risk: dict[str, Any] = raw_risk if isinstance(raw_risk, dict) else {}
    raw_permissions = policy.get("permissions")
    permissions: dict[str, Any] = raw_permissions if isinstance(raw_permissions, dict) else {}
    market = _market_context(client, jwt, symbol, now=now, risk=risk, errors=[])
    atr = _decimal(market.get("atr14"))
    quote = _safe_quote(client, jwt, symbol)
    current = _quote_price(quote)
    held_qty = _decimal(held_position.get("quantity"))
    held_avg = _decimal(held_position.get("average_price"))
    progress = _decimal(held_position.get("progress_r"))
    if atr is None or atr <= 0 or current is None or current <= 0 or held_qty is None or held_qty <= 0:
        return None
    raw_stop_details = held_position.get("watching_sell_sltp_details")
    stop_details = raw_stop_details if isinstance(raw_stop_details, list) else []
    current_protective_stop = _protective_stop_covering_quantity(stop_details, held_qty)

    stop_multiplier = _decimal(risk.get("stop_atr_multiplier")) or Decimal("2")
    risk_per_share = stop_multiplier * atr
    if risk_per_share <= 0:
        return None
    per_trade_risk_rub = equity * (_decimal(risk.get("risk_per_trade_pct")) or Decimal("1")) / Decimal("100")
    total_risk_headroom = (equity * max_total_open_risk_pct / Decimal("100")) - open_risk_rub
    if total_risk_headroom <= 0:
        total_risk_headroom = Decimal("0")
    shares_by_trade_risk = (per_trade_risk_rub / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)
    shares_by_total_risk = (total_risk_headroom / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)
    shares_by_cash = (cash / current).to_integral_value(rounding=ROUND_FLOOR)
    raw_quantity = min(shares_by_trade_risk, shares_by_total_risk, shares_by_cash)
    rules, contract_verified = _instrument_rules(client, jwt, symbol, account_id=account_id, errors=errors)
    quantity = normalize_quantity_to_lot(raw_quantity, rules)
    if quantity <= 0:
        return None

    stop = current - risk_per_share
    target = current + risk_per_share * (_decimal(risk.get("take_profit_r")) or Decimal("2"))
    limit_price = normalize_price(current, rules, direction="floor")
    stop_price = normalize_price(stop, rules, direction="ceil")
    target_price = normalize_price(target, rules, direction="floor")
    nearest_target = _nearest_resistance_above_current(market.get("closed_h4") or [], current)
    nearest_target_price = normalize_price(nearest_target, rules, direction="floor") if nearest_target is not None else None
    nearest_target_r = ((nearest_target - current) / risk_per_share) if nearest_target is not None and risk_per_share > 0 else None
    rr_advisory = _rr_advisory_fields(
        current=current,
        nearest_target=nearest_target,
        risk_per_share=risk_per_share,
        min_target_r=_decimal(risk.get("min_target_r")) or Decimal("1.5"),
    )
    total_quantity = held_qty + quantity
    projected_average = (
        ((held_qty * held_avg) + (quantity * limit_price)) / total_quantity
        if held_avg is not None and total_quantity > 0
        else None
    )
    complete = all(item is not None for item in (current, quantity, stop_price, target_price, quantity * risk_per_share))
    lot = {"lot_size": _decimal_payload(rules.lot_size)}
    if quantity != raw_quantity:
        lot["raw_quantity"] = _decimal_payload(raw_quantity)

    candidate = _apply_candidate_gates(
        {
            "symbol": symbol,
            "status": "PROPOSE_ONLY",
            "candidate_source": "held_scale_in",
            "current_price": _decimal_payload(limit_price),
            "reference_price": _decimal_payload(current),
            "quantity": str(quantity),
            "notional": _decimal_payload(quantity * limit_price),
            "lot": lot,
            "finam_contract": _finam_contract_payload(rules, verified=contract_verified),
            "atr14": _decimal_payload(atr),
            "stop": _decimal_payload(stop_price),
            "tp_2r": _decimal_payload(target_price),
            "nearest_target": _decimal_payload(nearest_target_price),
            "nearest_target_r": _decimal_payload(nearest_target_r),
            **rr_advisory,
            "risk_rub": _decimal_payload(quantity * risk_per_share),
            "signal": market.get("signal"),
            "requires_confirmation": True,
            "current_position": {
                "quantity": _decimal_payload(held_qty),
                "average_price": _decimal_payload(held_avg),
                "progress_r": _decimal_payload(progress),
                "has_watching_sell_sltp": bool(held_position.get("has_watching_sell_sltp")),
                "protective_stop": current_protective_stop,
            },
            "projected_position": {
                "add_quantity": _decimal_payload(quantity),
                "total_quantity": _decimal_payload(total_quantity),
                "average_price": _decimal_payload(projected_average),
                "protective_stop_required_quantity": _decimal_payload(total_quantity),
            },
        },
        min_target_r=_decimal(risk.get("min_target_r")) or Decimal("1.5"),
        nearest_target_r=nearest_target_r,
        second_tier=bool((market.get("signal") or {}).get("second_tier"))
        and bool(permissions.get("second_tier_requires_confirmation", True)),
        complete=complete,
    )
    _add_gate_reason(candidate, "scale_in_live_execution_disabled")
    if progress is None or progress < Decimal("1"):
        _add_gate_reason(candidate, "scale_in_progress_below_1r")
        candidate["status"] = "BLOCKED"
        candidate["requires_confirmation"] = True
    if nearest_target_r is None:
        _add_gate_reason(candidate, "scale_in_unknown_target_resistance")
        candidate["status"] = "BLOCKED"
        candidate["requires_confirmation"] = True
    if not bool(held_position.get("has_watching_sell_sltp")):
        _add_gate_reason(candidate, "current_protective_stop_missing")
        candidate["status"] = "BLOCKED"
        candidate["requires_confirmation"] = True
    elif current_protective_stop is None:
        _add_gate_reason(candidate, "current_protective_stop_incomplete")
        candidate["status"] = "BLOCKED"
        candidate["requires_confirmation"] = True
    if not contract_verified:
        _add_gate_reason(candidate, "finam_contract_unverified")
        candidate["status"] = "BLOCKED"
        candidate["requires_confirmation"] = True
    elif not rules.broker_buy_allowed:
        _add_gate_reason(candidate, "finam_contract_not_longable")
        candidate["status"] = "BLOCKED"
        candidate["requires_confirmation"] = True
    _set_decision_record(candidate)
    return candidate


def _protective_stop_covering_quantity(stop_details: list[Any], required_quantity: Decimal) -> dict[str, Any] | None:
    for raw_stop in stop_details:
        if not isinstance(raw_stop, dict):
            continue
        quantity = _decimal(raw_stop.get("quantity"))
        stop_price = _decimal(raw_stop.get("stop"))
        if quantity is None or stop_price is None:
            continue
        if quantity >= required_quantity:
            return raw_stop
    return None


def _scan_candidates(
    client: FinamClient,
    jwt: str,
    *,
    account_id: str,
    policy: dict[str, Any],
    held_symbols: set[str],
    cash: Decimal | None,
    equity: Decimal | None,
    open_positions: int,
    open_risk_rub: Decimal,
    now: datetime,
    errors: list[str],
    short_candidates: list[dict[str, Any]] | None = None,
    dynamic_watchlist: list[dict[str, Any]] | None = None,
    held_positions_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    raw_risk = policy.get("risk")
    risk: dict[str, Any] = raw_risk if isinstance(raw_risk, dict) else {}
    raw_permissions = policy.get("permissions")
    permissions: dict[str, Any] = raw_permissions if isinstance(raw_permissions, dict) else {}
    raw_growth_mode = policy.get("growth_mode")
    growth_mode = raw_growth_mode if isinstance(raw_growth_mode, dict) else {}
    allow_short_analysis = bool(growth_mode.get("allow_short_analysis", False))
    max_short_marks = _positive_int(growth_mode.get("max_short_candidates"), default=3)
    max_positions = int(risk.get("max_open_positions") or 5)
    max_new = int(risk.get("max_new_trades_per_run") or 1)
    max_total_open_risk_pct = _decimal(risk.get("max_total_open_risk_pct")) or Decimal("3.0")
    if cash is None or cash <= 0 or equity is None:
        return []
    if _total_open_risk_exceeds_limit(
        open_risk_rub=open_risk_rub,
        equity=equity,
        max_total_open_risk_pct=max_total_open_risk_pct,
    ):
        errors.append("total open risk exceeds policy limit; no new autonomous buy candidates")
        return []

    risk_rub = equity * (_decimal(risk.get("risk_per_trade_pct")) or Decimal("1")) / Decimal("100")
    min_target_r = _decimal(risk.get("min_target_r")) or Decimal("1.5")
    static_universe = [str(item) for item in policy.get("universe") or []]
    scan_items = _candidate_scan_items(static_universe, dynamic_watchlist or [], policy)
    candidates: list[dict[str, Any]] = []
    if open_positions < max_positions and max_new > 0:
        for scan_item in scan_items:
            symbol = scan_item["symbol"]
            if symbol in held_symbols:
                continue
            market = _market_context(client, jwt, symbol, now=now, risk=risk, errors=[])
            raw_short_signal = market.get("short_signal")
            short_signal = raw_short_signal if isinstance(raw_short_signal, dict) else {}
            if (
                allow_short_analysis
                and short_candidates is not None
                and len(short_candidates) < max_short_marks
                and short_signal.get("status") == "WATCH"
            ):
                short_candidates.append(
                    {
                        "symbol": symbol,
                        "short_signal": short_signal,
                        "short_order": "forbidden_until_availability_confirmed",
                    }
                )
            atr = _decimal(market.get("atr14"))
            if market.get("signal", {}).get("status") != "WATCH" or atr is None or atr <= 0:
                continue
            quote = _safe_quote(client, jwt, symbol)
            current = _quote_price(quote)
            if current is None or current <= 0:
                continue
            stop_multiplier = _decimal(risk.get("stop_atr_multiplier")) or Decimal("2")
            risk_per_share = stop_multiplier * atr
            shares_by_risk = (risk_rub / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)
            shares_by_cash = (cash / current).to_integral_value(rounding=ROUND_FLOOR)
            raw_quantity = min(shares_by_risk, shares_by_cash)
            rules, contract_verified = _instrument_rules(client, jwt, symbol, account_id=account_id, errors=errors)
            quantity = normalize_quantity_to_lot(raw_quantity, rules)
            if quantity <= 0:
                continue
            stop = current - risk_per_share
            target = current + risk_per_share * (_decimal(risk.get("take_profit_r")) or Decimal("2"))
            limit_price = normalize_price(current, rules, direction="floor")
            stop_price = normalize_price(stop, rules, direction="ceil")
            target_price = normalize_price(target, rules, direction="floor")
            nearest_target = _nearest_resistance_above_current(market.get("closed_h4") or [], current)
            nearest_target_price = normalize_price(nearest_target, rules, direction="floor") if nearest_target is not None else None
            nearest_target_r = ((nearest_target - current) / risk_per_share) if nearest_target is not None and risk_per_share > 0 else None
            rr_advisory = _rr_advisory_fields(
                current=current,
                nearest_target=nearest_target,
                risk_per_share=risk_per_share,
                min_target_r=min_target_r,
            )
            complete = all(item is not None for item in (current, quantity, stop_price, target_price, quantity * risk_per_share))
            lot = {"lot_size": _decimal_payload(rules.lot_size)}
            if quantity != raw_quantity:
                lot["raw_quantity"] = _decimal_payload(raw_quantity)
            candidate = _apply_candidate_gates(
                {
                    "symbol": symbol,
                    "status": "PROPOSE_ONLY",
                    "candidate_source": scan_item["candidate_source"],
                    "discovery_score": scan_item.get("discovery_score"),
                    "discovery_reasons": scan_item.get("discovery_reasons") or [],
                    "current_price": _decimal_payload(limit_price),
                    "reference_price": _decimal_payload(current),
                    "quantity": str(quantity),
                    "notional": _decimal_payload(quantity * limit_price),
                    "lot": lot,
                    "finam_contract": _finam_contract_payload(rules, verified=contract_verified),
                    "atr14": _decimal_payload(atr),
                    "stop": _decimal_payload(stop_price),
                    "tp_2r": _decimal_payload(target_price),
                    "nearest_target": _decimal_payload(nearest_target_price),
                    "nearest_target_r": _decimal_payload(nearest_target_r),
                    **rr_advisory,
                    "risk_rub": _decimal_payload(quantity * risk_per_share),
                    "signal": market.get("signal"),
                    "requires_confirmation": _candidate_requires_confirmation(scan_item, policy),
                },
                min_target_r=min_target_r,
                nearest_target_r=nearest_target_r,
                second_tier=bool(market.get("signal", {}).get("second_tier"))
                and bool(permissions.get("second_tier_requires_confirmation", True)),
                complete=complete,
            )
            if not contract_verified:
                _add_gate_reason(candidate, "finam_contract_unverified")
                candidate["status"] = "BLOCKED"
                candidate["requires_confirmation"] = True
                _set_decision_record(candidate)
            elif not rules.broker_buy_allowed:
                _add_gate_reason(candidate, "finam_contract_not_longable")
                candidate["status"] = "BLOCKED"
                candidate["requires_confirmation"] = True
                _set_decision_record(candidate)
            candidates.append(candidate)

    if held_positions_by_symbol:
        for scan_item in scan_items:
            symbol = scan_item["symbol"]
            if symbol not in held_symbols:
                continue
            held_position = held_positions_by_symbol.get(symbol)
            if not held_position:
                continue
            scale_candidate = _held_scale_in_candidate(
                client,
                jwt,
                symbol=symbol,
                account_id=account_id,
                held_position=held_position,
                policy=policy,
                cash=cash,
                equity=equity,
                open_risk_rub=open_risk_rub,
                max_total_open_risk_pct=max_total_open_risk_pct,
                now=now,
                errors=errors,
            )
            if scale_candidate is not None:
                candidates.append(scale_candidate)

    candidates.sort(key=lambda item: _decimal(item.get("risk_rub")) or Decimal("0"), reverse=True)
    _apply_actionable_selection_limit(candidates, max_new=max_new)
    return candidates


def _candidate_scan_items(
    static_universe: list[str],
    dynamic_watchlist: list[dict[str, Any]],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for symbol in static_universe:
        if symbol in seen:
            continue
        seen.add(symbol)
        result.append({"symbol": symbol, "candidate_source": "static", "discovery_score": None, "discovery_reasons": []})

    config = _dynamic_universe_config(policy)
    max_dynamic = _positive_int(config.get("max_dynamic_candidates"), default=8)
    for item in dynamic_watchlist:
        if item.get("status") not in {"DISCOVERED", "DYNAMIC_CANDIDATE"}:
            continue
        symbol = str(item.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(
            {
                "symbol": symbol,
                "candidate_source": "dynamic",
                "discovery_score": item.get("discovery_score"),
                "discovery_reasons": item.get("discovery_reasons") or [],
            }
        )
        if sum(1 for entry in result if entry.get("candidate_source") == "dynamic") >= max_dynamic:
            break
    return result


def _candidate_requires_confirmation(scan_item: dict[str, Any], policy: dict[str, Any]) -> bool:
    if scan_item.get("candidate_source") == "dynamic":
        config = _dynamic_universe_config(policy)
        return bool(config.get("dynamic_candidates_require_confirmation", True))
    permissions = policy.get("permissions") if isinstance(policy.get("permissions"), dict) else {}
    return bool(permissions.get("new_buys_require_telegram_confirmation", True))


def _apply_actionable_selection_limit(candidates: list[dict[str, Any]], *, max_new: int) -> None:
    selected = 0
    for candidate in candidates:
        if candidate.get("candidate_source") == "held_scale_in":
            _add_gate_reason(candidate, "scale_in_live_execution_disabled")
            candidate["actionable_this_run"] = False
            candidate["requires_confirmation"] = True
            _set_decision_record(candidate)
            continue
        if candidate.get("status") == "BLOCKED":
            candidate["actionable_this_run"] = False
            _set_decision_record(candidate)
            continue
        if selected < max_new:
            candidate["actionable_this_run"] = True
            selected += 1
        else:
            candidate["actionable_this_run"] = False
            _add_gate_reason(candidate, "max_new_trades_per_run_selection_limit")
            _set_decision_record(candidate)


def _discover_dynamic_watchlist(policy: dict[str, Any], *, now: datetime, errors: list[str]) -> list[dict[str, Any]]:
    config = _dynamic_universe_config(policy)
    if not config.get("enabled", True):
        return []
    static_universe = {str(item) for item in policy.get("universe") or []}
    boards = [str(item) for item in config.get("boards") or ["TQBR"]]
    max_scan = _positive_int(config.get("max_scan_symbols"), default=80)
    rows: list[dict[str, Any]] = []
    for board in boards:
        rows.extend(_moex_tqbr_market_snapshot(board=board, errors=errors))

    rows.sort(key=lambda item: _decimal(item.get("turnover_rub")) or Decimal("0"), reverse=True)
    watchlist: list[dict[str, Any]] = []
    for row in rows[:max_scan]:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        if symbol in static_universe:
            continue
        item = _dynamic_watchlist_item(row, config=config, now=now)
        watchlist.append(item)
    watchlist.sort(key=lambda item: _decimal(item.get("discovery_score")) or Decimal("-1"), reverse=True)
    return watchlist


def _dynamic_universe_config(policy: dict[str, Any]) -> dict[str, Any]:
    raw = policy.get("dynamic_universe")
    merged = deepcopy(DEFAULT_POLICY["dynamic_universe"])
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


def _dynamic_watchlist_item(row: dict[str, Any], *, config: dict[str, Any], now: datetime) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "")
    price = _decimal(row.get("price"))
    turnover = _decimal(row.get("turnover_rub")) or Decimal("0")
    trades = _decimal(row.get("trades_today")) or Decimal("0")
    momentum_pct = _decimal(row.get("day_momentum_pct")) or Decimal("0")
    min_turnover = _decimal(config.get("min_daily_turnover_rub")) or Decimal("100000000")
    min_trades = _decimal(config.get("min_trades_today")) or Decimal("500")
    min_price = _decimal(config.get("min_price_rub")) or Decimal("5")

    reject_reasons: list[str] = []
    if price is None or price < min_price:
        reject_reasons.append("low_price")
    if turnover < min_turnover:
        reject_reasons.append("low_turnover")
    if trades < min_trades:
        reject_reasons.append("low_trades")
    if _marketdata_stale(row.get("time"), now=now):
        reject_reasons.append("stale_marketdata")

    score, reasons = _discovery_score(
        turnover=turnover,
        trades=trades,
        momentum_pct=momentum_pct,
        min_turnover=min_turnover,
        min_trades=min_trades,
    )
    status = "REJECTED" if reject_reasons else "DISCOVERED"
    return {
        "symbol": symbol,
        "status": status,
        "candidate_source": "dynamic",
        "discovery_score": score,
        "discovery_reasons": reasons,
        "reject_reasons": reject_reasons,
        "price": _decimal_payload(price),
        "turnover_rub": _decimal_payload(turnover),
        "trades_today": _decimal_payload(trades),
        "day_momentum_pct": _decimal_payload(momentum_pct),
        "board": row.get("board") or "TQBR",
        "requires_confirmation": bool(config.get("dynamic_candidates_require_confirmation", True)),
    }


def _discovery_score(
    *,
    turnover: Decimal,
    trades: Decimal,
    momentum_pct: Decimal,
    min_turnover: Decimal,
    min_trades: Decimal,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    turnover_ratio = turnover / min_turnover if min_turnover > 0 else Decimal("0")
    trades_ratio = trades / min_trades if min_trades > 0 else Decimal("0")
    liquidity = min(40, int((turnover_ratio * Decimal("20")).to_integral_value(rounding=ROUND_FLOOR)))
    activity = min(20, int((trades_ratio * Decimal("10")).to_integral_value(rounding=ROUND_FLOOR)))
    momentum = max(0, min(25, int((momentum_pct * Decimal("5")).to_integral_value(rounding=ROUND_FLOOR))))
    if turnover >= min_turnover:
        reasons.append("liquid_turnover")
    if trades >= min_trades:
        reasons.append("active_trading")
    if momentum_pct > 0:
        reasons.append("positive_day_momentum")
    score = max(0, min(100, liquidity + activity + momentum + 15))
    return score, reasons


def _marketdata_stale(value: Any, *, now: datetime) -> bool:
    if value in {None, ""}:
        return False
    if ":" in str(value) and len(str(value)) <= 8:
        return False
    parsed = _parse_time(str(value))
    if parsed is None:
        today = now.astimezone(MOSCOW_TZ).date().isoformat()
        return today not in str(value)
    return (now - parsed) > timedelta(hours=18)


def _moex_tqbr_market_snapshot(*, board: str, errors: list[str]) -> list[dict[str, Any]]:
    url = (
        "https://iss.moex.com/iss/engines/stock/markets/shares/"
        f"boards/{board}/securities.json"
        "?iss.only=securities,marketdata"
        "&securities.columns=SECID,BOARDID,SHORTNAME,LOTSIZE"
        "&marketdata.columns=SECID,BOARDID,LAST,OPEN,LCLOSEPRICE,PREVPRICE,LASTTOPREVPRICE,VALTODAY,NUMTRADES,TIME"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - dynamic discovery is optional.
        errors.append(f"MOEX dynamic universe {board} недоступен: {exc}")
        return []
    return _moex_market_snapshot_from_response(data, board=board)


def _moex_market_snapshot_from_response(data: Any, *, board: str) -> list[dict[str, Any]]:
    marketdata = data.get("marketdata") if isinstance(data, dict) else None
    columns = marketdata.get("columns") if isinstance(marketdata, dict) else None
    rows = marketdata.get("data") if isinstance(marketdata, dict) else None
    if not isinstance(columns, list) or not isinstance(rows, list):
        return []
    index = {str(name).upper(): pos for pos, name in enumerate(columns)}
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        secid = _row_value(row, index, "SECID")
        if not secid:
            continue
        price = _decimal(_row_value(row, index, "LAST"))
        prev = _decimal(_row_value(row, index, "PREVPRICE")) or _decimal(_row_value(row, index, "LCLOSEPRICE"))
        momentum = _decimal(_row_value(row, index, "LASTTOPREVPRICE"))
        if momentum is None and price is not None and prev is not None and prev > 0:
            momentum = (price - prev) / prev * Decimal("100")
        result.append(
            {
                "symbol": f"{str(secid).upper()}@MISX",
                "board": _row_value(row, index, "BOARDID") or board,
                "price": _decimal_payload(price),
                "turnover_rub": _decimal_payload(_row_value(row, index, "VALTODAY")),
                "trades_today": _decimal_payload(_row_value(row, index, "NUMTRADES")),
                "day_momentum_pct": _decimal_payload(momentum),
                "time": _row_value(row, index, "TIME"),
            }
        )
    return result


def _row_value(row: list[Any], index: dict[str, int], key: str) -> Any:
    pos = index.get(key)
    if pos is None or pos >= len(row):
        return None
    return row[pos]


def _apply_dynamic_watchlist_candidate_status(watchlist: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> None:
    candidate_by_symbol = {str(item.get("symbol")): item for item in candidates if isinstance(item, dict)}
    for item in watchlist:
        symbol = str(item.get("symbol") or "")
        candidate = candidate_by_symbol.get(symbol)
        if candidate is None:
            continue
        if candidate.get("status") == "BLOCKED":
            item["status"] = "REJECTED"
            item["reject_reasons"] = list(item.get("reject_reasons") or []) + list(candidate.get("gate_reasons") or [])
        else:
            item["status"] = "DYNAMIC_CANDIDATE"


def _rr_advisory_fields(
    *,
    current: Decimal | None,
    nearest_target: Decimal | None,
    risk_per_share: Decimal | None,
    min_target_r: Decimal,
) -> dict[str, Any]:
    nearest_target_r = (
        (nearest_target - current) / risk_per_share
        if current is not None and nearest_target is not None and risk_per_share is not None and risk_per_share > 0
        else None
    )
    fields: dict[str, Any] = {
        "rr_strategy": _rr_strategy_payload(nearest_target_r, min_target_r=min_target_r),
        "entry_price_for_min_r": None,
        "required_pullback_pct": None,
    }
    if current is None or current <= 0 or nearest_target is None or risk_per_share is None or risk_per_share <= 0:
        return fields

    entry_price = nearest_target - (min_target_r * risk_per_share)
    pullback_pct = (current - entry_price) / current * Decimal("100")
    if pullback_pct < 0:
        pullback_pct = Decimal("0")
    fields["entry_price_for_min_r"] = _decimal_payload(entry_price)
    fields["required_pullback_pct"] = _decimal_payload(pullback_pct)
    return fields


def _rr_strategy_payload(nearest_target_r: Decimal | None, *, min_target_r: Decimal) -> dict[str, Any]:
    if nearest_target_r is None:
        return {
            "tier": "UNKNOWN",
            "risk_multiplier": "0",
            "manual_confirmation_required": True,
            "report_only": True,
            "reason": "RR не рассчитан",
        }
    if nearest_target_r >= min_target_r:
        return {
            "tier": "A",
            "risk_multiplier": "1",
            "manual_confirmation_required": False,
            "report_only": True,
            "reason": "RR проходит основной минимум",
        }
    if nearest_target_r >= Decimal("1"):
        return {
            "tier": "B",
            "risk_multiplier": "0.5",
            "manual_confirmation_required": True,
            "report_only": True,
            "reason": "RR ниже 1.5R, но выше 1R; только половинный риск и ручное подтверждение",
        }
    if nearest_target_r >= Decimal("0.8"):
        return {
            "tier": "C",
            "risk_multiplier": "0.25",
            "manual_confirmation_required": True,
            "report_only": True,
            "reason": "RR 0.8–1.0R; только четверть риска и ручное подтверждение",
        }
    return {
        "tier": "WAIT",
        "risk_multiplier": "0",
        "manual_confirmation_required": True,
        "report_only": True,
        "reason": "RR ниже 0.8R; ждать откат или новую цель",
    }


def _enrich_dynamic_watchlist_risk_reward(
    client: FinamClient,
    jwt: str,
    watchlist: list[dict[str, Any]],
    *,
    now: datetime,
    risk: dict[str, Any],
) -> None:
    """Add report-only nearest-target R/R to every dynamic watchlist item.

    This runs after trade candidate selection, so it must not affect order
    selection, gates, policy, or broker-mutating paths.
    """
    stop_multiplier = _decimal(risk.get("stop_atr_multiplier")) or Decimal("2")
    for item in watchlist:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        current = _decimal(item.get("price"))
        item["rr_report_only"] = True
        item["nearest_target"] = None
        item["nearest_target_r"] = None
        if not _dynamic_watchlist_rr_eligible(item):
            item["rr_unavailable_reason"] = "skipped_before_rr_gate"
            continue
        if not symbol or current is None or current <= 0:
            item["rr_unavailable_reason"] = "missing_atr_or_price"
            continue

        local_errors: list[str] = []
        market = _market_context(client, jwt, symbol, now=now, risk=risk, errors=local_errors)
        atr = _decimal(market.get("atr14"))
        item["atr14"] = _decimal_payload(atr)
        item["signal"] = market.get("signal")
        if local_errors:
            item["rr_warnings"] = local_errors[:2]
        if atr is None or atr <= 0 or stop_multiplier <= 0:
            item["rr_unavailable_reason"] = "missing_atr_or_price"
            continue

        risk_per_share = stop_multiplier * atr
        nearest_target = _nearest_resistance_above_current(market.get("closed_h4") or [], current)
        if nearest_target is None:
            item["rr_unavailable_reason"] = "missing_target_resistance"
            continue
        nearest_target_r = (nearest_target - current) / risk_per_share
        item["nearest_target"] = _decimal_payload(nearest_target)
        item["nearest_target_r"] = _decimal_payload(nearest_target_r)
        item.update(
            _rr_advisory_fields(
                current=current,
                nearest_target=nearest_target,
                risk_per_share=risk_per_share,
                min_target_r=_decimal(risk.get("min_target_r")) or Decimal("1.5"),
            )
        )
        item.pop("rr_unavailable_reason", None)

    watchlist.sort(key=_dynamic_watchlist_rr_sort_key, reverse=True)


def _dynamic_watchlist_rr_eligible(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").upper()
    reject_reasons = {str(reason) for reason in item.get("reject_reasons") or []}
    pre_rr_rejects = {"low_price", "low_turnover", "low_trades", "stale_marketdata"}
    if reject_reasons & pre_rr_rejects:
        return False
    return status in {"DISCOVERED", "DYNAMIC_CANDIDATE", "REJECTED"}


def _dynamic_watchlist_rr_sort_key(item: dict[str, Any]) -> tuple[int, Decimal, int]:
    rr = _decimal(item.get("nearest_target_r")) if isinstance(item, dict) else None
    score = _positive_int(item.get("discovery_score"), default=0) if isinstance(item, dict) else 0
    if rr is None:
        return (0, Decimal("-999999"), score)
    return (1, rr, score)


def _no_proxy_entries(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _host_bypassed_by_no_proxy(host: str, no_proxy: str) -> bool:
    normalized_host = host.lower().strip(".")
    for entry in _no_proxy_entries(no_proxy):
        normalized_entry = entry.lstrip(".")
        if entry == "*":
            return True
        if normalized_host == normalized_entry:
            return True
        if entry.startswith(".") and normalized_host.endswith("." + normalized_entry):
            return True
    return False


def _apply_candidate_gates(
    candidate: dict[str, Any],
    *,
    min_target_r: Decimal,
    nearest_target_r: Decimal | None,
    second_tier: bool,
    complete: bool,
) -> dict[str, Any]:
    gated = dict(candidate)
    reasons = list(gated.get("gate_reasons") or [])

    if not complete:
        reasons.append("incomplete_sl_tp_quantity_or_risk")
        gated["status"] = "BLOCKED"
        gated["requires_confirmation"] = True
    elif nearest_target_r is None:
        reasons.append("unknown_target_resistance")
        gated["status"] = "PROPOSE_ONLY"
        gated["requires_confirmation"] = True
    elif nearest_target_r < min_target_r:
        reasons.append(f"target_r_below_min_{min_target_r}")
        gated["status"] = "BLOCKED"
        gated["requires_confirmation"] = True

    if second_tier:
        reasons.append("second_tier_requires_confirmation")
        if gated.get("status") != "BLOCKED":
            gated["status"] = "PROPOSE_ONLY"
        gated["requires_confirmation"] = True

    gated["gate_reasons"] = reasons
    _set_decision_record(gated)
    return gated


def _apply_research_gates(
    candidates: list[dict[str, Any]],
    research: dict[str, Any],
    *,
    explicit_avoid_blocks_trade: bool,
) -> None:
    status = str(research.get("status") or "")
    if status == "ok":
        summary = str(research.get("summary") or "")
        avoid_symbols = _avoid_symbols_from_items(research) or _avoid_symbols_from_research(
            summary,
            [str(item.get("symbol")) for item in candidates],
        )
        if explicit_avoid_blocks_trade:
            for candidate in candidates:
                if str(candidate.get("symbol")) in avoid_symbols:
                    _add_gate_reason(candidate, "codex_review_avoid")
                    candidate["status"] = "BLOCKED"
                    candidate["requires_confirmation"] = True
                    _set_decision_record(candidate)
        for candidate in candidates:
            _set_decision_record(candidate)
        return

    if status in {"failed", "unavailable", "codex_review_required"}:
        for candidate in candidates:
            if candidate.get("status") != "BLOCKED":
                candidate["status"] = "PROPOSE_ONLY"
            candidate["requires_confirmation"] = True
            if research.get("classification") == "provider_client_failure":
                _add_gate_reason(candidate, "provider_client_failure_no_signal")
            _add_gate_reason(candidate, "codex_review_required")
            _set_decision_record(candidate)


def _apply_growth_scores(candidates: list[dict[str, Any]], research: dict[str, Any]) -> None:
    for candidate in candidates:
        candidate["growth_score"] = _growth_score(candidate, research)
        candidate["entry_blockers"] = _entry_blockers(candidate)


def _write_scan_journal(report: dict[str, Any]) -> None:
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    research = report.get("research") if isinstance(report.get("research"), dict) else {}
    run_id = str(report.get("time_utc") or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    events: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        decision = candidate.get("decision_record") if isinstance(candidate.get("decision_record"), dict) else {}
        events.append(
            {
                "run_id": run_id,
                "decision_id": decision.get("decision_id"),
                "symbol": candidate.get("symbol"),
                "candidate_source": candidate.get("candidate_source"),
                "discovery_score": candidate.get("discovery_score"),
                "gates": candidate.get("gate_reasons") or [],
                "reject_reasons": candidate.get("entry_blockers") or [],
                "status": candidate.get("status"),
            }
        )
    if not events:
        return
    try:
        write_event(
            {
                "command": "h4-scan",
                "run_id": run_id,
                "status": report.get("status"),
                "decisions": events,
                "research": {
                    "symbols": research.get("symbols") or [],
                    "max_tokens": (report.get("policy") or {}).get("research", {}).get("h4_max_tokens"),
                    "provider": research.get("provider"),
                    "model": research.get("model"),
                    "usage": research.get("usage"),
                    "cache": research.get("cache"),
                    "status": research.get("status"),
                },
            }
        )
    except Exception:
        return


def _load_research_context(*, now: datetime) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for kind in ("daily", "weekly"):
        artifact = latest_research_artifact(kind, now=now)
        if artifact is not None:
            context[kind] = artifact
    return context


def _apply_research_context(candidates: list[dict[str, Any]], context: dict[str, Any]) -> None:
    if not context:
        return
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "")
        notes: list[str] = []
        blocking_notes: list[str] = []
        boost = 0
        penalty = 0
        for kind in ("daily", "weekly"):
            artifact = context.get(kind) if isinstance(context.get(kind), dict) else {}
            if not artifact:
                continue
            if symbol in {str(item) for item in artifact.get("priority_watchlist") or []}:
                notes.append(f"{kind}: priority_watchlist")
                boost = max(boost, 5)
            if symbol in {str(item) for item in artifact.get("avoid_symbols") or []}:
                notes.append(f"{kind}: avoid_symbols")
                blocking_notes.append(f"{kind}: avoid_symbols")
                penalty = max(penalty, 15)
            for event in artifact.get("event_watchlist") or []:
                if isinstance(event, dict) and str(event.get("symbol")) == symbol:
                    event_text = str(event.get("event") or "event_watchlist")
                    notes.append(f"{kind}: {event_text}")
                    blocking_notes.append(f"{kind}: {event_text}")
                    penalty = max(penalty, 10)
        if notes:
            candidate["research_context"] = {
                "notes": notes,
                "blocking_notes": blocking_notes,
                "score_modifier": boost - penalty,
            }


def _growth_score(candidate: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    components: dict[str, int] = {}

    nearest_target_r = _decimal(candidate.get("nearest_target_r"))
    rr_points = 0
    if nearest_target_r is not None and nearest_target_r > 0:
        rr_points = min(25, int((nearest_target_r / Decimal("2.0") * Decimal("25")).to_integral_value(rounding=ROUND_FLOOR)))
    components["risk_reward"] = rr_points

    signal = candidate.get("signal")
    if not isinstance(signal, dict):
        signal = {}
    components["h4_momentum"] = 25 if signal.get("status") == "WATCH" else 0
    components["relative_strength"] = _bounded_component(candidate.get("relative_strength_score"), maximum=15)
    components["liquidity"] = _liquidity_component(candidate)
    components["atr"] = 10 if (_decimal(candidate.get("atr14")) or Decimal("0")) > 0 else 0

    verdict = _research_verdict_for_symbol(research, str(candidate.get("symbol") or ""))
    if verdict == "OK":
        components["research"] = 10
    elif verdict == "RISK":
        components["research"] = -10
    elif verdict == "AVOID":
        components["research"] = -20
    else:
        components["research"] = 0
    context = candidate.get("research_context") if isinstance(candidate.get("research_context"), dict) else {}
    context_modifier = _bounded_context_modifier(context.get("score_modifier"))
    components["research_context"] = context_modifier

    gate_reasons = list(candidate.get("gate_reasons") or [])
    penalty = min(30, 15 * len(gate_reasons))
    if candidate.get("status") == "BLOCKED":
        penalty += 15
    components["gate_penalty"] = -penalty

    score = max(0, min(100, sum(components.values())))
    if score >= 75:
        label = "HIGH"
    elif score >= 60:
        label = "MEDIUM"
    else:
        label = "LOW"
    return {"score": score, "label": label, "components": components}


def _bounded_component(value: Any, *, maximum: int) -> int:
    parsed = _decimal(value)
    if parsed is None:
        return 0
    return max(0, min(maximum, int(parsed.to_integral_value(rounding=ROUND_FLOOR))))


def _bounded_context_modifier(value: Any) -> int:
    parsed = _decimal(value)
    if parsed is None:
        return 0
    return max(-20, min(5, int(parsed.to_integral_value(rounding=ROUND_FLOOR))))


def _liquidity_component(candidate: dict[str, Any]) -> int:
    explicit = candidate.get("liquidity_score")
    if explicit is not None:
        return _bounded_component(explicit, maximum=10)
    notional = _decimal(candidate.get("notional"))
    if notional is None or notional <= 0:
        return 0
    if notional >= Decimal("100000"):
        return 10
    return 5


def _entry_blockers(candidate: dict[str, Any]) -> list[str]:
    mapping = {
        "target_r_below_min_1.5": "R/R ниже минимума 1.5R",
        "unknown_target_resistance": "не найдена ближайшая цель/сопротивление",
        "incomplete_sl_tp_quantity_or_risk": "неполный расчёт SL/TP/количества/риска",
        "second_tier_requires_confirmation": "второй эшелон требует отдельного подтверждения",
        "scale_in_progress_below_1r": "докупка заблокирована: позиция ещё не дошла до +1R",
        "scale_in_unknown_target_resistance": "докупка заблокирована: не найдена ближайшая цель/сопротивление",
        "current_protective_stop_missing": "докупка заблокирована: текущий защитный стоп на продажу не найден",
        "current_protective_stop_incomplete": "докупка заблокирована: текущий защитный стоп на продажу не покрывает всю позицию",
        "scale_in_live_execution_disabled": "докупка только в отчёте: нужен отдельный безопасный перенос защитного стопа на весь итоговый объём",
        "finam_contract_unverified": "контракт Finam не подтверждён",
        "finam_contract_not_longable": "инструмент не подтверждён как доступный для LONG",
        "perplexity_news_avoid": "research дал AVOID/RISK по новости",
        "provider_client_failure_no_signal": "provider/client failure; это не торговый сигнал",
        "research_unavailable_requires_manual_confirmation": "research недоступен, нужно ручное подтверждение",
        "max_new_trades_per_run_selection_limit": "не выбран как actionable-кандидат в этом H4 запуске",
    }
    blockers: list[str] = []
    for reason in candidate.get("gate_reasons") or []:
        text = mapping.get(str(reason))
        if text and text not in blockers:
            blockers.append(text)
    context = candidate.get("research_context") if isinstance(candidate.get("research_context"), dict) else {}
    for note in context.get("blocking_notes") or []:
        text = f"research context: {note}"
        if text not in blockers:
            blockers.append(text)
    if candidate.get("status") == "BLOCKED" and not blockers:
        blockers.append("кандидат заблокирован правилами")
    return blockers


def _avoid_symbols_from_items(research: dict[str, Any]) -> set[str]:
    avoid: set[str] = set()
    for item in research.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("verdict") or "").upper() == "AVOID":
            avoid.add(str(item.get("symbol") or ""))
    return {item for item in avoid if item}


def _research_verdict_for_symbol(research: dict[str, Any], symbol: str) -> str:
    for item in research.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("symbol")) == symbol:
            verdict = str(item.get("verdict") or "UNAVAILABLE").upper()
            return verdict if verdict in {"OK", "RISK", "AVOID", "UNAVAILABLE"} else "UNAVAILABLE"
    verdict = str(research.get("verdict") or "UNAVAILABLE").upper()
    return verdict if verdict in {"OK", "RISK", "AVOID", "UNAVAILABLE"} else "UNAVAILABLE"


def _avoid_symbols_from_research(summary: str, symbols: list[str]) -> set[str]:
    avoid: set[str] = set()
    upper = summary.upper()
    for symbol in symbols:
        ticker = symbol.split("@", 1)[0].upper()
        marker_positions = [upper.find(symbol.upper()), upper.find(ticker)]
        positions = [pos for pos in marker_positions if pos >= 0]
        if not positions:
            continue
        pos = min(positions)
        window = upper[pos : pos + 180]
        if "AVOID" in window:
            avoid.add(symbol)
    return avoid


def _add_gate_reason(candidate: dict[str, Any], reason: str) -> None:
    reasons = list(candidate.get("gate_reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    candidate["gate_reasons"] = reasons


def _set_decision_record(candidate: dict[str, Any]) -> None:
    status = str(candidate.get("status") or "UNKNOWN")
    requires_confirmation = bool(candidate.get("requires_confirmation"))
    gate_reasons = [str(item) for item in candidate.get("gate_reasons") or []]
    if status == "BLOCKED":
        action = "do_not_buy"
    elif candidate.get("actionable_this_run") is False:
        action = "not_selected_this_run"
    elif requires_confirmation:
        action = "manual_confirmation_required"
    else:
        action = "eligible"
    decision_id = _decision_id(candidate)
    candidate["decision_record"] = {
        "decision_id": decision_id,
        "status": status,
        "action": action,
        "requires_confirmation": requires_confirmation,
        "gate_reasons": gate_reasons,
    }


def _decision_id(candidate: dict[str, Any]) -> str:
    payload = "|".join(
        [
            str(candidate.get("symbol") or "UNKNOWN"),
            str(candidate.get("candidate_source") or "static"),
            str(candidate.get("status") or "UNKNOWN"),
            ",".join(str(item) for item in candidate.get("gate_reasons") or []),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _nearest_resistance_above_current(bars: list[dict[str, Any]], current: Decimal) -> Decimal | None:
    highs: list[Decimal] = []
    for bar in bars[-20:]:
        high = _decimal(bar.get("high"))
        if high is not None and high > current:
            highs.append(high)
    return min(highs) if highs else None


def _total_open_risk_rub(positions: list[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for position in positions:
        qty = _decimal(position.get("quantity"))
        current = _decimal(position.get("current_price")) or _decimal(position.get("average_price"))
        stop = _decimal(position.get("calculated_stop"))
        if qty is None or current is None or stop is None or qty <= 0 or current <= stop:
            continue
        total += qty * (current - stop)
    return total


def _total_open_risk_exceeds_limit(
    *,
    open_risk_rub: Decimal,
    equity: Decimal,
    max_total_open_risk_pct: Decimal,
) -> bool:
    if equity <= 0:
        return True
    return (open_risk_rub / equity * Decimal("100")) > max_total_open_risk_pct


def _research_candidates(candidates: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    dynamic = _dynamic_universe_config(policy)
    max_symbols = _positive_int(dynamic.get("max_research_symbols"), default=2)
    h4_max = _positive_int(research.get("h4_max_candidates"), default=2)
    cap = min(max_symbols, h4_max)
    researchable = [
        item
        for item in candidates
        if isinstance(item, dict) and item.get("candidate_source") != "held_scale_in"
    ]
    selected = [item for item in researchable if item.get("status") != "BLOCKED"][:cap]
    if not selected:
        selected = researchable[:cap]
    return research_candidates(selected, policy)


def _researchable_candidates(candidates: list[dict[str, Any]], policy: dict[str, Any]) -> list[dict[str, Any]]:
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    if bool(research.get("run_for_blocked_candidates", False)):
        return list(candidates)
    return [candidate for candidate in candidates if candidate.get("status") != "BLOCKED"]


def _safe_quote(client: FinamClient, jwt: str, symbol: str) -> dict[str, Any]:
    try:
        return client.last_quote(jwt, symbol)
    except Exception:
        return {}


def _instrument_rules(
    client: FinamClient,
    jwt: str,
    symbol: str,
    *,
    account_id: str,
    errors: list[str],
) -> tuple[FinamInstrumentRules, bool]:
    try:
        asset = client.asset(jwt, symbol, account_id=account_id)
        params = client.asset_params(jwt, symbol, account_id=account_id)
        return rules_from_finam(symbol, asset, params), True
    except Exception as exc:  # noqa: BLE001 - lot fallback should not abort candidate scanning.
        moex_errors: list[str] = []
        lot_size = _moex_lot_size(symbol, errors=moex_errors)
        if moex_errors:
            errors.append(f"{symbol}: Finam instrument contract недоступен: {exc}")
            errors.extend(moex_errors)
        return fallback_rules(symbol, lot_size=lot_size), False


def _finam_contract_payload(rules: FinamInstrumentRules, *, verified: bool) -> dict[str, Any]:
    return rules.to_payload() | {"verified": verified}


def _lot_size_from_asset(asset: dict[str, Any]) -> Decimal | None:
    for key in ("lot_size", "lotSize", "lotsize", "lot", "min_quantity", "minQuantity"):
        parsed = _decimal(_first_value_for_key(asset, key))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _round_quantity_to_lot(quantity: Decimal, lot_size: Decimal) -> Decimal:
    return normalize_quantity_to_lot(quantity, fallback_rules("UNKNOWN@MISX", lot_size=lot_size))


def _moex_lot_size(symbol: str, *, errors: list[str]) -> Decimal:
    ticker = symbol.split("@", 1)[0]
    urls = [
        (
            "https://iss.moex.com/iss/engines/stock/markets/shares/"
            f"boards/TQBR/securities/{ticker}.json"
            "?iss.only=securities&securities.columns=SECID,LOTSIZE,SHORTNAME,BOARDID"
        ),
        (
            "https://iss.moex.com/iss/engines/stock/markets/shares/"
            f"securities/{ticker}.json"
            "?iss.only=securities&securities.columns=SECID,LOTSIZE,SHORTNAME,BOARDID"
        ),
    ]
    failures: list[str] = []
    unexpected_format = False
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - lot fallback should not abort candidate scanning.
            failures.append(str(exc))
            continue
        lot_size, parsed_shape = _moex_lot_size_from_response(data, ticker)
        if lot_size is not None:
            return lot_size
        unexpected_format = unexpected_format or not parsed_shape
    if failures:
        errors.append(f"{symbol}: MOEX lot size недоступен: {'; '.join(failures)}")
    elif unexpected_format:
        errors.append(f"{symbol}: MOEX lot size вернул неожиданный формат")
    else:
        errors.append(f"{symbol}: MOEX lot size не найден")
    return Decimal("1")


def _moex_lot_size_from_response(data: Any, ticker: str) -> tuple[Decimal | None, bool]:
    securities = data.get("securities") if isinstance(data, dict) else None
    columns = securities.get("columns") if isinstance(securities, dict) else None
    rows = securities.get("data") if isinstance(securities, dict) else None
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None, False
    index = {str(name).upper(): pos for pos, name in enumerate(columns)}
    lot_pos = index.get("LOTSIZE")
    secid_pos = index.get("SECID")
    board_pos = index.get("BOARDID")
    if lot_pos is None:
        return None, True
    for row in rows:
        if not isinstance(row, list) or len(row) <= lot_pos:
            continue
        if secid_pos is not None and (len(row) <= secid_pos or str(row[secid_pos]).upper() != ticker.upper()):
            continue
        if board_pos is not None and len(row) > board_pos and row[board_pos] not in {None, "", "TQBR"}:
            continue
        parsed = _decimal(row[lot_pos])
        if parsed is not None and parsed > 0:
            return parsed, True
    return None, True


def _quote_price(quote: dict[str, Any]) -> Decimal | None:
    data = quote.get("quote") if isinstance(quote.get("quote"), dict) else quote
    for key in ("last", "last_price", "price", "close"):
        parsed = _decimal(data.get(key)) if isinstance(data, dict) else None
        if parsed is not None:
            return parsed
    return None


def _moex_h4_bars(
    symbol: str,
    *,
    start_dt: datetime,
    end_dt: datetime,
    errors: list[str],
) -> list[dict[str, Any]]:
    ticker = symbol.split("@", 1)[0]
    url = (
        "https://iss.moex.com/iss/engines/stock/markets/shares/securities/"
        f"{ticker}/candles.json?interval=60&from={start_dt.date().isoformat()}&till={end_dt.date().isoformat()}"
    )
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - market-data fallback should not abort the whole report.
        errors.append(f"{symbol}: MOEX ISS недоступен: {exc}")
        return []

    candles = data.get("candles") if isinstance(data, dict) else None
    columns = candles.get("columns") if isinstance(candles, dict) else None
    rows = candles.get("data") if isinstance(candles, dict) else None
    if not isinstance(columns, list) or not isinstance(rows, list):
        errors.append(f"{symbol}: MOEX ISS вернул неожиданный формат candles")
        return []
    index = {name: pos for pos, name in enumerate(columns)}
    hourly: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        begin = row[index.get("begin", -1)] if "begin" in index and len(row) > index["begin"] else None
        parsed = _parse_time(str(begin)) if begin else None
        if parsed is None:
            continue
        hourly.append(
            {
                "time": _iso_z(parsed),
                "open": str(row[index["open"]]),
                "high": str(row[index["high"]]),
                "low": str(row[index["low"]]),
                "close": str(row[index["close"]]),
            }
        )
    return _aggregate_hourly_to_h4(hourly)


def _aggregate_hourly_to_h4(hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for item in hourly:
        start = _parse_time(item["time"])
        if start is None:
            continue
        session_hour = ((start.hour - 7) // 4) * 4 + 7 if start.hour >= 7 else 3
        bucket = start.replace(hour=session_hour, minute=0, second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(item)

    result: list[dict[str, Any]] = []
    for bucket, items in sorted(buckets.items()):
        items.sort(key=lambda item: item["time"])
        if len(items) < 2:
            continue
        result.append(
            {
                "time": _iso_z(bucket),
                "open": items[0]["open"],
                "high": str(max(Decimal(item["high"]) for item in items)),
                "low": str(min(Decimal(item["low"]) for item in items)),
                "close": items[-1]["close"],
            }
        )
    return result


def _parse_bar(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    time_value = item.get("timestamp") or item.get("time") or item.get("begin")
    open_price = _decimal(item.get("open"))
    high = _decimal(item.get("high"))
    low = _decimal(item.get("low"))
    close = _decimal(item.get("close"))
    if time_value is None or None in {open_price, high, low, close}:
        return None
    return {
        "time": str(time_value),
        "open": str(open_price),
        "high": str(high),
        "low": str(low),
        "close": str(close),
    }


def _atr14(bars: list[dict[str, Any]]) -> Decimal | None:
    return _atr(bars, period=14)


def _atr(bars: list[dict[str, Any]], *, period: int) -> Decimal | None:
    if len(bars) < period + 1:
        return None
    true_ranges: list[Decimal] = []
    for index in range(1, len(bars)):
        high = Decimal(bars[index]["high"])
        low = Decimal(bars[index]["low"])
        prev_close = Decimal(bars[index - 1]["close"])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < period:
        return None
    return sum(true_ranges[-period:], Decimal("0")) / Decimal(str(period))


def _closed_bars(
    bars: list[dict[str, Any]],
    *,
    now: datetime,
    timeframe: timedelta,
) -> list[dict[str, Any]]:
    closed: list[dict[str, Any]] = []
    for item in bars:
        start = _parse_time(item["time"])
        if start is None:
            continue
        if start + timeframe <= now:
            closed.append(item)
    return closed


def _h4_fresh(latest: dict[str, Any] | None, *, now: datetime) -> bool:
    if latest is None:
        return False
    start = _parse_time(str(latest.get("time")))
    if start is None:
        return False
    return now - start <= timedelta(hours=12)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _long_signal(latest: dict[str, Any] | None, bars: list[dict[str, Any]]) -> dict[str, Any]:
    if latest is None or len(bars) < 2:
        return {"status": "WAIT", "reason": "not_enough_h4_bars"}
    latest_close = Decimal(latest["close"])
    latest_open = Decimal(latest["open"])
    previous_high = Decimal(bars[-2]["high"])
    if latest_close <= latest_open:
        return {"status": "WAIT", "reason": "latest_h4_not_bullish"}
    if latest_close <= previous_high:
        return {"status": "WAIT", "reason": "no_breakout_above_previous_h4_high"}
    return {"status": "WATCH", "reason": "h4_bullish_breakout_candidate"}


def _short_signal(latest: dict[str, Any] | None, bars: list[dict[str, Any]]) -> dict[str, Any]:
    if latest is None or len(bars) < 2:
        return {"status": "WAIT", "reason": "not_enough_h4_bars", "orders_allowed": False}
    latest_close = Decimal(latest["close"])
    latest_open = Decimal(latest["open"])
    previous_low = Decimal(bars[-2]["low"])
    if latest_close >= latest_open:
        return {"status": "WAIT", "reason": "latest_h4_not_bearish", "orders_allowed": False}
    if latest_close >= previous_low:
        return {"status": "WAIT", "reason": "no_breakdown_below_previous_h4_low", "orders_allowed": False}
    return {"status": "WATCH", "reason": "h4_bearish_breakdown_candidate", "orders_allowed": False}


def _watching_sell_order_details_by_symbol(orders: list[Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        text = json.dumps(order, ensure_ascii=False)
        if "ORDER_STATUS_WATCHING" not in text or "SIDE_SELL" not in text:
            continue
        symbol = _first_symbol(order)
        if symbol:
            result.setdefault(symbol, []).append(
                {
                    "order_id": order.get("order_id"),
                    "side": _first_value_for_key(order, "side") or "SIDE_SELL",
                    "quantity": _decimal_payload(
                        _first_value_for_key(order, "quantity_sl")
                        or _first_value_for_key(order, "initial_quantity")
                        or _first_value_for_key(order, "quantity")
                    ),
                    "stop": _decimal_payload(
                        _first_value_for_key(order, "sl_price")
                        or _first_value_for_key(order, "stop_price")
                        or _first_value_for_key(order, "price")
                    ),
                }
            )
    return result


def _active_orders(orders: list[Any]) -> list[Any]:
    active_statuses = {
        "ORDER_STATUS_NEW",
        "ORDER_STATUS_ACTIVE",
        "ORDER_STATUS_WATCHING",
        "ORDER_STATUS_PARTIALLY_EXECUTED",
    }
    result: list[Any] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        status = order.get("status")
        if status in active_statuses:
            result.append(order)
    return result


def _position_symbol(position: dict[str, Any]) -> str:
    return str(position.get("symbol") or position.get("asset_id") or position.get("ticker") or "UNKNOWN")


def _first_symbol(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("symbol", "asset_id", "ticker"):
            item = value.get(key)
            if isinstance(item, str) and "@" in item:
                return item
        for item in value.values():
            found = _first_symbol(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_symbol(item)
            if found:
                return found
    return None


def _first_value_for_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _first_value_for_key(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_value_for_key(item, key)
            if found is not None:
                return found
    return None


def _closed_note(position: dict[str, Any]) -> str:
    pnl = _decimal(position.get("daily_pnl")) or _decimal(position.get("unrealized_pnl"))
    if pnl is not None and pnl != 0:
        return "защитный SL ранее исполнен"
    return "позиция закрыта"


def _round_price(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(Decimal("0.01"))


def _decimal_payload(value: Any) -> str | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return str(parsed)


def _cash_payload(value: Any, *, currency: str) -> str | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("currency_code") == currency:
                return _decimal_payload(item)
        return None
    return _decimal_payload(value)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


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


if __name__ == "__main__":
    raise SystemExit(main())
