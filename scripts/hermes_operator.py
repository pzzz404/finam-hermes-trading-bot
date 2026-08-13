#!/usr/bin/env python3
"""Operator command surface for Hermes.

This CLI gives Hermes stable commands for portfolio status, H4 scans, research,
compact Codex reviews, policy checks, trade proposals, guarded Arena execution,
and report previews. It never sends Telegram messages; broker mutations are
limited to explicit live commands that pass their policy gates.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_OPERATOR_ENV_FILES = (
    Path.home() / ".config" / "hermes" / ".env",
    Path.home() / ".config" / "finam-hermes-trading-bot" / "runtime.env",
)

import h4_monitor  # noqa: E402
import h4_monitor_notify  # noqa: E402
import runtime_doctor  # noqa: E402
from finam_trading_bot.client import FinamClient  # noqa: E402
from finam_trading_bot.arena import (  # noqa: E402
    ARENA_MARGIN_TRADING_NOT_SUPPORTED,
    arena_approval_until,
    arena_base_url,
    arena_order_payloads,
    arena_trade_proposal,
    build_arena_attribution,
    build_arena_opportunity_auction,
    build_arena_scan,
    build_arena_portfolio_review,
    build_repeat_pattern_loss_report,
    build_arena_status,
    load_arena_policy,
    validate_arena_policy,
)
from finam_trading_bot.arena_learning import (  # noqa: E402
    DEFAULT_DECISION_EVENTS_PATH,
    DEFAULT_OUTCOMES_PATH,
    DEFAULT_SUGGESTIONS_PATH,
    append_jsonl,
    apply_learning_patch,
    build_learning_proposal,
    build_learning_report,
    decision_snapshots_from_scan,
    identify_positive_learning_candidates,
    outcome_records_from_execution_ledger,
    read_jsonl,
)
from finam_trading_bot.contract import (  # noqa: E402
    FinamContractError,
    assert_broker_buy_allowed,
    decimal_payload,
    normalize_price,
    normalize_quantity_to_lot,
    rules_from_finam,
)
from finam_trading_bot.execution import GuardedOrderExecutor, OrderGuardConfig, OrderNotAllowed  # noqa: E402
from finam_trading_bot.mcp_shadow import build_mcp_shadow_review  # noqa: E402
from finam_trading_bot.operator_journal import write_event  # noqa: E402
from finam_trading_bot.research import (  # noqa: E402
    DEFAULT_RESEARCH_ROOT,
    daily_digest,
    pretrade_check,
    research_candidates,
    weekly_review,
    write_research_artifact,
)
from finam_trading_bot.redaction import redact_environment_values  # noqa: E402
from finam_trading_bot.risk import OrderProposal, RiskConfig  # noqa: E402

DEMO_ACCOUNT_ID = "DEMO-ACCOUNT"
LIVE_DEMO_GATE_ENV = "FINAM_H4_ALLOW_LIVE_DEMO_ORDERS"
AUTONOMOUS_DEMO_GATE_ENV = "FINAM_H4_AUTONOMOUS_DEMO_ENABLED"
DEFAULT_SAFETY_STATE_PATH = ROOT / "data" / "runtime" / "finam_h4_safety_state.json"
DEFAULT_ARENA_POLICY_PATH = ROOT / "config" / "finam_arena_policy.json"
ARENA_POLICY_ENV = "FINAM_ARENA_POLICY_PATH"
DEFAULT_ARENA_PENDING_APPROVALS_PATH = ROOT / "data" / "runtime" / "arena_pending_approvals.json"
DEFAULT_ARENA_EXECUTION_LEDGER_PATH = ROOT / "data" / "runtime" / "arena_execution_ledger.jsonl"
DEFAULT_ARENA_LEARNING_EVENTS_PATH = DEFAULT_DECISION_EVENTS_PATH
DEFAULT_ARENA_LEARNING_OUTCOMES_PATH = DEFAULT_OUTCOMES_PATH
DEFAULT_ARENA_LEARNING_SUGGESTIONS_PATH = DEFAULT_SUGGESTIONS_PATH
DEFAULT_ARENA_EVENT_CANDIDATES_PATH = ROOT / "data" / "runtime" / "arena_event_candidates.jsonl"
DEFAULT_ARENA_OPPORTUNITY_LEDGER_PATH = ROOT / "data" / "runtime" / "arena_opportunity_auction.jsonl"
DEFAULT_ARENA_TELEGRAM_SNAPSHOT_PATH = ROOT / "data" / "runtime" / "arena_telegram_status_snapshot.json"
DEFAULT_ARENA_GROWTH_LOOP_STATE_PATH = ROOT / "data" / "runtime" / "arena_growth_loop_state.json"
DEFAULT_ARENA_GROWTH_LOOP_AUDIT_PATH = ROOT / "data" / "runtime" / "arena_growth_loop_audit.jsonl"
ARENA_PENDING_APPROVALS_ENV = "FINAM_ARENA_PENDING_APPROVALS_PATH"
ARENA_EXECUTION_LEDGER_ENV = "FINAM_ARENA_EXECUTION_LEDGER_PATH"
ARENA_EVENT_CANDIDATES_ENV = "FINAM_ARENA_EVENT_CANDIDATES_PATH"
ARENA_OPPORTUNITY_LEDGER_ENV = "FINAM_ARENA_OPPORTUNITY_LEDGER_PATH"
ARENA_TELEGRAM_SNAPSHOT_ENV = "FINAM_ARENA_TELEGRAM_SNAPSHOT_PATH"
ARENA_GROWTH_LOOP_STATE_ENV = "FINAM_ARENA_GROWTH_LOOP_STATE_PATH"
ARENA_GROWTH_LOOP_AUDIT_ENV = "FINAM_ARENA_GROWTH_LOOP_AUDIT_PATH"
ARENA_CODEX_REVIEW_COMMAND_ENV = "FINAM_ARENA_CODEX_REVIEW_COMMAND"
ARENA_TELEGRAM_SNAPSHOT_TTL_ENV = "FINAM_ARENA_TELEGRAM_SNAPSHOT_TTL_SECONDS"
ARENA_APPROVAL_TTL_ENV = "FINAM_ARENA_APPROVAL_TTL_SECONDS"
ARENA_APPROVAL_MAX_PRICE_DRIFT_ENV = "FINAM_ARENA_APPROVAL_MAX_PRICE_DRIFT_PCT"
ARENA_PRICE_REVALIDATION_RETRIES_ENV = "FINAM_ARENA_PRICE_REVALIDATION_RETRIES"
ARENA_PRICE_REVALIDATION_SLEEP_ENV = "FINAM_ARENA_PRICE_REVALIDATION_SLEEP_SECONDS"
DEFAULT_ARENA_CODEX_REVIEW_COMMAND = "codex exec --json"
DEFAULT_ARENA_APPROVAL_TTL_SECONDS = 600
DEFAULT_ARENA_TELEGRAM_SNAPSHOT_TTL_SECONDS = 900
DEFAULT_ARENA_APPROVAL_MAX_PRICE_DRIFT_PCT = Decimal("0.5")
ARENA_ASSET_PROBE_MICS = ("XNGS", "XNYS", "XNMS", "XNCM", "XASE", "PINX", "MISX")
ARENA_US_REGULAR_SESSION_MICS = {"XNGS", "XNYS", "XNAS", "XNMS", "XNCM", "XASE", "PINX", "XCME", "XNYM"}
ARENA_US_MARKET_TZ = ZoneInfo("America/New_York")
ARENA_MSK_TZ = ZoneInfo("Europe/Moscow")
ARENA_UNRESOLVED_EXECUTION_STATUSES = {
    "BROKER_ERROR",
    "ENTRY_PENDING_NO_STOP",
    "MANUAL_PROTECTION_REQUIRED",
    "STOP_NOT_VERIFIED",
    "MISSING_SOFT_STOP",
    "SOFT_STOP_UNDER_COVERED",
    "SOFT_STOP_SIDE_MISMATCH",
    "REPLACE_SELL_DONE_BUY_BLOCKED",
    "EXECUTED_ARENA_EXIT_RESIDUAL_STOP_TRIGGERED",
    "EXECUTED_ARENA_POSITION_SELL_RESIDUAL_STOP_TRIGGERED",
}
ARENA_EXECUTED_STATUSES = {
    "EXECUTED_ARENA",
    "EXECUTED_ARENA_PARTIAL",
    "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
    "EXECUTED_ARENA_PARTIAL_SOFT_STOP_ACTIVE",
    "EXECUTED_ARENA_PORTFOLIO",
    "EXECUTED_ARENA_REPLACEMENT",
    "EXECUTED_ARENA_EXIT_CASH",
    "ARENA_SOFT_STOP_TRIGGERED",
}
ARENA_RETRYABLE_APPROVAL_REASONS = {
    "arena_stop_check_not_clear",
    "unresolved_trade_safety_state",
    "approval_price_revalidation_failed",
    "approval_price_revalidation_unavailable",
}


def _configured_h4_account_id(*, policy: dict[str, Any] | None, env: Any) -> str:
    """Resolve the account without baking a private identifier into public code."""
    configured = str(env.get("FINAM_ACCOUNT_ID") or "").strip()
    from_policy = str((policy or {}).get("account_id") or "").strip()
    return configured or from_policy or DEMO_ACCOUNT_ID
ARENA_PROTECTION_HALT_STATUSES = {
    "MISSING_SOFT_STOP",
    "SOFT_STOP_UNDER_COVERED",
    "SOFT_STOP_SIDE_MISMATCH",
}
ARENA_US_UNIVERSE_REFRESH_SEEDS = (
    "MS@XNYS",
    "GS@XNYS",
    "ABBV@XNYS",
    "PEP@XNGS",
    "ADBE@XNGS",
    "INTU@XNGS",
    "TXN@XNGS",
    "AMAT@XNGS",
    "WMT@XNYS",
    "HD@XNYS",
)
ARENA_LEARNING_CLEANUP_TARGET = {
    "deprioritize_symbols": ["AAPL@XNGS", "META@XNGS", "GMKN@MISX", "ALRS@MISX", "UNH@XNYS"],
    "attribution_uncertain_symbols": ["MSFT@XNGS", "SBER@MISX", "TATN@MISX"],
}
ARENA_UNIVERSE_LEARNING_GATES = {
    "candidate_score_below_min",
    "learning_deprioritized_requires_manual_review",
    "learning_attribution_uncertain_requires_manual_review",
    "repeat_pattern_loss_cooldown",
    "repeat_pattern_loss_score_below_min",
}
ARENA_GROWTH_REVIEW_GATES = {
    "research_unavailable_below_exceptional_score",
    "entry_strength_confirmation_missing",
    "candidate_score_below_min",
}
ARENA_GROWTH_LEARNING_ALLOWED_KEYS = {
    "last_objective",
    "weak_exit_threshold_status",
    "deprioritize_symbols",
    "repeat_pattern_loss_status",
    "attribution_uncertain_symbols",
    "score_weight_delta_cap",
    "suggested_score_adjustments",
    "observation",
}
CODEX_REVIEW_SCHEMA_VERSION = 1
CODEX_REVIEW_SOURCE = "hermes_codex"
CODEX_REVIEW_CONFIRM = "RECORD_CODEX_REVIEW"
CODEX_REVIEW_VERDICTS = {"OK", "RISK", "AVOID", "UNAVAILABLE"}
DEFAULT_CODEX_REVIEW_DIR = ROOT / "data" / "runtime" / "codex_reviews"
DEFAULT_CODEX_CONTEXT_MAX_BYTES = 4096
DEFAULT_CODEX_REVIEW_TTL_MINUTES = 240


def _add_arena_strategy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True, metavar="ACCOUNT_ID")
    parser.add_argument("--mode", choices=["approval", "autonomous"], default=None)
    parser.add_argument("--risk-multiplier", default=None, metavar="DECIMAL")
    parser.add_argument("--pause", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--trade-mode", choices=["manual", "auto"], default=None)
    parser.add_argument("--add-universe", action="append", default=[], metavar="SYMBOL@MIC")
    parser.add_argument("--remove-universe", action="append", default=[], metavar="SYMBOL@MIC")


def _add_arena_research_args(parser: argparse.ArgumentParser, *, default: str) -> None:
    parser.add_argument("--research-mode", choices=["cache_only", "budgeted", "normal", "skip"], default=default)
    parser.add_argument("--fresh-research", action="store_true", help="Allow a non-cache research pass. OpenRouter is retired for Hermes live runtime.")


def _arena_research_mode_arg(args: argparse.Namespace) -> str:
    return "normal" if bool(getattr(args, "fresh_research", False)) else str(getattr(args, "research_mode", "normal"))


def _default_arena_policy_path() -> Path:
    raw = str(os.environ.get(ARENA_POLICY_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_ARENA_POLICY_PATH


def _load_default_operator_env() -> list[str]:
    if str(os.environ.get("HERMES_OPERATOR_AUTOLOAD_ENV") or "").strip().lower() in {"0", "false", "no"}:
        return []

    env_files = list(DEFAULT_OPERATOR_ENV_FILES)
    hermes_home = str(os.environ.get("HERMES_HOME") or "").strip()
    if hermes_home:
        profile_env = Path(hermes_home).expanduser() / ".env"
        runtime_env = Path("/etc/finam-hermes-trading-bot/runtime.env")
        env_files = [env_files[0], profile_env, runtime_env]

    loaded: list[str] = []
    process_env_keys = set(os.environ)
    for path in env_files:
        if not path.exists():
            continue
        loaded.append(str(path))
        for line in path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            if not key or key in process_env_keys:
                continue
            os.environ[key] = value.strip().strip('"').strip("'")
    return loaded


def main() -> int:
    _load_default_operator_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", default=str(h4_monitor.DEFAULT_POLICY_PATH))
    parser.add_argument("--arena-policy", default=str(_default_arena_policy_path()))
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status")
    scan = subparsers.add_parser("scan")
    scan.add_argument("--with-research", action="store_true")
    subparsers.add_parser("morning-plan")
    research = subparsers.add_parser("research")
    research.add_argument("symbol")
    subparsers.add_parser("research-daily")
    subparsers.add_parser("research-weekly")
    research_budget = subparsers.add_parser("research-budget-report")
    research_budget.add_argument("--date", default=None, metavar="YYYY-MM-DD")
    research_budget.add_argument("--root", default=str(DEFAULT_RESEARCH_ROOT))
    research_pretrade = subparsers.add_parser("research-pretrade")
    research_pretrade.add_argument("symbol", metavar="SYMBOL@MISX")
    codex_context = subparsers.add_parser("codex-review-context")
    codex_context.add_argument("--symbol", required=True, metavar="SYMBOL@MISX")
    codex_context.add_argument("--format", choices=["compact-json"], default="compact-json")
    codex_record = subparsers.add_parser("codex-review-record")
    codex_record.add_argument("--symbol", required=True, metavar="SYMBOL@MISX")
    codex_record.add_argument("--review-json", required=True)
    codex_record.add_argument("--confirm", required=True, metavar=CODEX_REVIEW_CONFIRM)
    trade_proposal = subparsers.add_parser("trade-proposal")
    trade_proposal.add_argument("--symbol", metavar="SYMBOL@MISX")
    trade_confirm = subparsers.add_parser("trade-confirm")
    trade_confirm.add_argument("--symbol", required=True, metavar="SYMBOL@MISX")
    trade_confirm.add_argument("--confirmation", required=True, metavar="CONFIRM_BUY SYMBOL@MISX")
    trade_execute = subparsers.add_parser("trade-execute-demo")
    trade_execute.add_argument("--symbol", required=True, metavar="SYMBOL@MISX")
    trade_execute.add_argument("--confirmation", required=True, metavar="CONFIRM_BUY SYMBOL@MISX")
    trade_execute.add_argument("--live", action="store_true")
    trade_buy = subparsers.add_parser("trade-buy")
    trade_buy.add_argument("--symbol", required=True, metavar="SYMBOL@MISX")
    trade_buy.add_argument("--intent", required=True, help='User text, e.g. "купи MTSS" or "MTSS подтверждаю"')
    trade_buy.add_argument("--confirmation", default="", metavar="CONFIRM_BUY SYMBOL@MISX")
    trade_buy.add_argument("--live", action="store_true")
    autonomous_run = subparsers.add_parser("autonomous-run")
    autonomous_run.add_argument("--live", action="store_true")
    subparsers.add_parser("report-preview")
    arena_status = subparsers.add_parser("arena-status")
    arena_status.add_argument("--send-telegram", action="store_true")
    arena_status.add_argument("--dry-run", action="store_true", help="Build Telegram payload but do not send.")
    arena_status.add_argument("--full-json", action="store_true", help="Print full diagnostic JSON instead of compact LLM status.")
    arena_scan = subparsers.add_parser("arena-scan")
    _add_arena_research_args(arena_scan, default="cache_only")
    arena_portfolio_review = subparsers.add_parser("arena-portfolio-review")
    _add_arena_research_args(arena_portfolio_review, default="cache_only")
    arena_news_scan = subparsers.add_parser("arena-news-scan")
    arena_news_scan.add_argument("--event-path", default=str(DEFAULT_ARENA_EVENT_CANDIDATES_PATH))
    arena_news_scan.add_argument("--limit", type=int, default=20)
    arena_news_propose = subparsers.add_parser("arena-news-propose")
    arena_news_propose.add_argument("--event-path", default=str(DEFAULT_ARENA_EVENT_CANDIDATES_PATH))
    arena_news_propose.add_argument("--limit", type=int, default=20)
    arena_opportunity = subparsers.add_parser("arena-opportunity-auction")
    arena_opportunity.add_argument("--event-path", default=str(DEFAULT_ARENA_EVENT_CANDIDATES_PATH))
    arena_opportunity.add_argument("--ledger-path", default=str(DEFAULT_ARENA_OPPORTUNITY_LEDGER_PATH))
    arena_opportunity.add_argument("--no-ledger", action="store_true")
    _add_arena_research_args(arena_opportunity, default="cache_only")
    arena_llm_context = subparsers.add_parser("arena-llm-context")
    arena_llm_context.add_argument("--max-bytes", type=int, default=8000)
    arena_llm_context.add_argument("--max-list-items", type=int, default=5)
    arena_llm_context.add_argument("--include-mcp-shadow", action="store_true")
    arena_mcp_shadow = subparsers.add_parser("arena-mcp-shadow-review")
    arena_mcp_shadow.add_argument("--max-bytes", type=int, default=8000)
    _add_arena_research_args(arena_mcp_shadow, default="cache_only")
    arena_portfolio_run = subparsers.add_parser("arena-portfolio-run")
    arena_portfolio_run.add_argument("--live", action="store_true")
    _add_arena_research_args(arena_portfolio_run, default="cache_only")
    subparsers.add_parser("arena-attribution")
    arena_pattern_report = subparsers.add_parser("arena-pattern-report")
    arena_pattern_report.add_argument("--account", default="", metavar="ACCOUNT_ID")
    arena_universe_refresh = subparsers.add_parser("arena-universe-refresh-propose")
    arena_universe_refresh.add_argument("--account", required=True, metavar="ACCOUNT_ID")
    arena_growth_loop = subparsers.add_parser("arena-growth-loop")
    arena_growth_mode = arena_growth_loop.add_mutually_exclusive_group()
    arena_growth_mode.add_argument("--apply", action="store_true")
    arena_growth_mode.add_argument("--dry-run", action="store_true")
    arena_growth_loop.add_argument("--daily", action="store_true")
    arena_growth_loop.add_argument("--max-reviews", type=int, default=3)
    arena_growth_loop.add_argument("--review-timeout-seconds", type=int, default=90)
    arena_growth_propose = subparsers.add_parser("arena-growth-propose")
    arena_growth_propose.add_argument("--top-n", type=int, default=5)
    arena_growth_propose.add_argument("--run-research", action="store_true")
    arena_growth_research = subparsers.add_parser("arena-growth-research")
    arena_growth_research.add_argument("--top-n", type=int, default=3)
    arena_growth_research.add_argument("--review-timeout-seconds", type=int, default=90)
    arena_learning_update = subparsers.add_parser("arena-learning-update")
    _add_arena_research_args(arena_learning_update, default="cache_only")
    subparsers.add_parser("arena-learning-report")
    subparsers.add_parser("arena-learning-propose")
    arena_learning_apply = subparsers.add_parser("arena-learning-apply")
    arena_learning_apply.add_argument("--confirm", default="", metavar="APPLY_ARENA_LEARNING")
    arena_learning_apply.add_argument("--backup-dir", default=None)
    arena_attribution_backfill = subparsers.add_parser("arena-attribution-backfill")
    arena_attribution_backfill.add_argument("--since", required=True, metavar="YYYY-MM-DD")
    arena_attribution_backfill.add_argument("--dry-run", action="store_true")
    arena_assets = subparsers.add_parser("arena-assets")
    arena_assets.add_argument("--query", required=True, metavar="TEXT")
    arena_assets.add_argument("--mic", action="append", default=[], metavar="MIC")
    arena_assets.add_argument("--limit", type=int, default=20)
    arena_view = subparsers.add_parser("arena-view")
    arena_view.add_argument("--callback", required=True, metavar="CALLBACK_ID")
    arena_proposal = subparsers.add_parser("arena-proposal")
    arena_proposal.add_argument("--account", required=True, metavar="ACCOUNT_ID")
    _add_arena_research_args(arena_proposal, default="cache_only")
    arena_run = subparsers.add_parser("arena-run")
    arena_run.add_argument("--account", required=True, metavar="ACCOUNT_ID")
    arena_run.add_argument("--live", action="store_true")
    arena_run.add_argument("--confirmation", default="", metavar="CONFIRM_ARENA_BUY SYMBOL@MIC ACCOUNT")
    _add_arena_research_args(arena_run, default="cache_only")
    arena_position_sell = subparsers.add_parser("arena-position-sell")
    arena_position_sell.add_argument("--account", required=True, metavar="ACCOUNT_ID")
    arena_position_sell.add_argument("--symbol", required=True, metavar="SYMBOL@MIC")
    arena_position_sell.add_argument("--quantity", required=True, metavar="QTY")
    arena_position_sell.add_argument("--live", action="store_true")
    arena_position_sell.add_argument(
        "--confirmation",
        default="",
        metavar="CONFIRM_ARENA_SELL_PARTIAL SYMBOL@MIC QTY ACCOUNT",
    )
    arena_confirm = subparsers.add_parser("arena-confirm")
    arena_confirm.add_argument("--confirmation", required=True, nargs="+", metavar="CONFIRM_ARENA_BUY SYMBOL@MIC ACCOUNT")
    arena_confirm.add_argument("--live", action="store_true")
    arena_run_all = subparsers.add_parser("arena-run-all")
    arena_run_all.add_argument("--live", action="store_true")
    _add_arena_research_args(arena_run_all, default="cache_only")
    arena_check_stops = subparsers.add_parser("arena-check-stops")
    arena_check_stops.add_argument("--account", default="", metavar="ACCOUNT_ID")
    arena_check_stops.add_argument("--symbol", default="", metavar="SYMBOL@MIC")
    arena_check_stops.add_argument("--live", action="store_true")
    arena_execute_stops = subparsers.add_parser("arena-execute-triggered-stops")
    arena_execute_stops.add_argument("--account", default="", metavar="ACCOUNT_ID")
    arena_execute_stops.add_argument("--symbol", default="", metavar="SYMBOL@MIC")
    arena_execute_stops.add_argument("--live", action="store_true")
    arena_recover = subparsers.add_parser("arena-recover-protection")
    arena_recover.add_argument("--account", required=True, metavar="ACCOUNT_ID")
    arena_recover.add_argument("--symbol", required=True, metavar="SYMBOL@MIC")
    arena_recover.add_argument("--live", action="store_true")
    arena_recover.add_argument("--confirmation", default="", metavar="CONFIRM_ARENA_RECOVER SYMBOL@MIC ACCOUNT")
    arena_strategy_propose = subparsers.add_parser("arena-strategy-propose")
    _add_arena_strategy_args(arena_strategy_propose)
    arena_strategy_apply = subparsers.add_parser("arena-strategy-apply")
    _add_arena_strategy_args(arena_strategy_apply)
    arena_strategy_apply.add_argument("--confirm", default="", metavar="APPLY_ARENA_STRATEGY")
    arena_strategy_apply.add_argument("--backup-dir", default=None)
    arena_emergency_stop = subparsers.add_parser("arena-emergency-stop")
    arena_emergency_stop.add_argument("--confirm", default="", metavar="ARENA_EMERGENCY_STOP")
    arena_emergency_stop.add_argument("--backup-dir", default=None)
    policy_check = subparsers.add_parser("policy-check")
    policy_check.add_argument("--verbose", action="store_true")
    subparsers.add_parser("policy-validate")
    policy_propose = subparsers.add_parser("policy-propose")
    policy_propose.add_argument("--mode", choices=["supervised", "autonomous_demo"])
    policy_propose.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    policy_propose.add_argument("--add-universe", action="append", default=[], metavar="SYMBOL@MISX")
    policy_propose.add_argument("--remove-universe", action="append", default=[], metavar="SYMBOL@MISX")
    policy_apply = subparsers.add_parser("policy-apply")
    policy_apply.add_argument("--mode", choices=["supervised", "autonomous_demo"])
    policy_apply.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    policy_apply.add_argument("--add-universe", action="append", default=[], metavar="SYMBOL@MISX")
    policy_apply.add_argument("--remove-universe", action="append", default=[], metavar="SYMBOL@MISX")
    policy_apply.add_argument("--confirm", default="", metavar="APPLY_POLICY")
    policy_apply.add_argument("--backup-dir", default=None)

    args = parser.parse_args()
    policy_path = Path(args.policy)
    arena_policy_path = Path(args.arena_policy)

    if args.command == "status":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=False)
        output = _status_output(report)
    elif args.command == "scan":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=bool(args.with_research))
        output = _scan_output(report)
    elif args.command == "morning-plan":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=False)
        output = _morning_plan_output(report)
    elif args.command == "research":
        policy = h4_monitor.load_policy(policy_path)
        output = research_candidates([{"symbol": args.symbol}], policy)
    elif args.command == "research-daily":
        policy = h4_monitor.load_policy(policy_path)
        result = daily_digest(policy, [str(item) for item in policy.get("universe") or []])
        artifact = write_research_artifact("daily", result) if result.get("status") == "ok" else None
        output = {
            "status": result.get("status"),
            "command": "research-daily",
            "artifact": artifact,
            "research": result,
            "safety": _safety_payload(),
        }
    elif args.command == "research-weekly":
        policy = h4_monitor.load_policy(policy_path)
        result = weekly_review(policy, [str(item) for item in policy.get("universe") or []])
        artifact = write_research_artifact("weekly", result) if result.get("status") == "ok" else None
        output = {
            "status": result.get("status"),
            "command": "research-weekly",
            "artifact": artifact,
            "research": result,
            "safety": _safety_payload(),
        }
    elif args.command == "research-budget-report":
        output = _research_budget_report_output(
            root=Path(args.root),
            date=args.date,
            now=datetime.now(timezone.utc),
        )
    elif args.command == "research-pretrade":
        policy = h4_monitor.load_policy(policy_path)
        symbol = _normalize_symbol(args.symbol)
        if not symbol:
            output = {
                "status": "INVALID",
                "command": "research-pretrade",
                "error": f"invalid symbol: {args.symbol}",
                "artifact": None,
                "safety": _safety_payload(),
            }
        else:
            result = pretrade_check(symbol, policy)
            artifact = write_research_artifact("pretrade", result) if result.get("status") == "ok" else None
            output = {
                "status": result.get("status"),
                "command": "research-pretrade",
                "artifact": artifact,
                "research": result,
                "safety": _safety_payload(),
            }
    elif args.command == "codex-review-context":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=False)
        policy = h4_monitor.load_policy(policy_path)
        output = _codex_review_context_output(report, policy=policy, symbol=args.symbol)
    elif args.command == "codex-review-record":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=False)
        policy = h4_monitor.load_policy(policy_path)
        output = _codex_review_record_output(
            report,
            policy=policy,
            symbol=args.symbol,
            review_json=args.review_json,
            confirm=args.confirm,
        )
    elif args.command == "trade-proposal":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=False)
        policy = h4_monitor.load_policy(policy_path)
        output = _trade_proposal_output(report, policy=policy, symbol=args.symbol)
    elif args.command == "trade-confirm":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=False)
        policy = h4_monitor.load_policy(policy_path)
        output = _trade_confirm_output(report, policy=policy, symbol=args.symbol, confirmation=args.confirmation)
    elif args.command == "trade-execute-demo":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=False)
        policy = h4_monitor.load_policy(policy_path)
        output = _trade_execute_demo_output(
            report,
            policy=policy,
            symbol=args.symbol,
            confirmation=args.confirmation,
            live=bool(args.live),
        )
    elif args.command == "trade-buy":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=True)
        output = _trade_buy_output(report, symbol=args.symbol, intent=args.intent, confirmation=args.confirmation, live=bool(args.live))
    elif args.command == "autonomous-run":
        policy = h4_monitor.load_policy(policy_path)
        output = _autonomous_pre_report_gate(policy, live=bool(args.live), env=os.environ)
        if output is None:
            report = h4_monitor.build_report(policy_path=policy_path, include_research=True)
            output = _autonomous_run_output(report, policy=policy, live=bool(args.live))
    elif args.command == "report-preview":
        report = h4_monitor.build_report(policy_path=policy_path, include_research=False)
        output = {
            "status": report.get("status"),
            "command": "report-preview",
            "telegram_delivery": "dry_run",
            "telegram_report": h4_monitor_notify.format_telegram_report(report),
        }
    elif args.command == "arena-status":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_status_output(
            policy,
            policy_path=arena_policy_path,
            send_telegram=bool(args.send_telegram),
            dry_run=bool(args.dry_run),
            full_json=bool(args.full_json),
        )
    elif args.command == "arena-scan":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_scan_output(policy, policy_path=arena_policy_path, research_mode=_arena_research_mode_arg(args))
    elif args.command == "arena-portfolio-review":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_portfolio_review_output(policy, policy_path=arena_policy_path, research_mode=_arena_research_mode_arg(args))
    elif args.command == "arena-news-scan":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_news_scan_output(
            policy,
            policy_path=arena_policy_path,
            event_path=Path(args.event_path),
            limit=int(args.limit),
        )
    elif args.command == "arena-news-propose":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_news_propose_output(
            policy,
            policy_path=arena_policy_path,
            event_path=Path(args.event_path),
            limit=int(args.limit),
        )
    elif args.command == "arena-opportunity-auction":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_opportunity_auction_output(
            policy,
            policy_path=arena_policy_path,
            research_mode=_arena_research_mode_arg(args),
            event_path=Path(args.event_path),
            ledger_path=None if bool(args.no_ledger) else Path(args.ledger_path),
        )
    elif args.command == "arena-llm-context":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_llm_context_output(
            policy,
            policy_path=arena_policy_path,
            max_bytes=int(args.max_bytes),
            max_list_items=int(args.max_list_items),
            include_mcp_shadow=bool(args.include_mcp_shadow),
        )
    elif args.command == "arena-mcp-shadow-review":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_mcp_shadow_review_output(
            policy,
            policy_path=arena_policy_path,
            max_bytes=int(args.max_bytes),
            research_mode=_arena_research_mode_arg(args),
        )
    elif args.command == "arena-portfolio-run":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_portfolio_run_output(
            policy,
            policy_path=arena_policy_path,
            live=bool(args.live),
            research_mode=_arena_research_mode_arg(args),
        )
    elif args.command == "arena-attribution":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_attribution_output(policy, policy_path=arena_policy_path)
    elif args.command == "arena-pattern-report":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_pattern_report_output(policy, policy_path=arena_policy_path, account_id=str(args.account or ""))
    elif args.command == "arena-universe-refresh-propose":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_universe_refresh_propose_output(policy, policy_path=arena_policy_path, account_id=str(args.account or ""))
    elif args.command == "arena-growth-loop":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_growth_loop_output(
            policy,
            policy_path=arena_policy_path,
            apply=bool(args.apply),
            daily=bool(args.daily),
            max_reviews=int(args.max_reviews),
            review_timeout_seconds=int(args.review_timeout_seconds),
            env=os.environ,
        )
    elif args.command == "arena-growth-propose":
        policy = load_arena_policy(arena_policy_path)
        if bool(args.run_research):
            output = _arena_growth_research_output(
                policy,
                policy_path=arena_policy_path,
                top_n=int(args.top_n),
                review_timeout_seconds=90,
                env=os.environ,
            )
        else:
            output = _arena_growth_propose_output(policy, policy_path=arena_policy_path, top_n=int(args.top_n), env=os.environ)
    elif args.command == "arena-growth-research":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_growth_research_output(
            policy,
            policy_path=arena_policy_path,
            top_n=int(args.top_n),
            review_timeout_seconds=int(args.review_timeout_seconds),
            env=os.environ,
        )
    elif args.command == "arena-learning-update":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_learning_update_output(policy, policy_path=arena_policy_path, research_mode=_arena_research_mode_arg(args))
    elif args.command == "arena-learning-report":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_learning_report_output(policy, policy_path=arena_policy_path)
    elif args.command == "arena-learning-propose":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_learning_propose_output(policy, policy_path=arena_policy_path)
    elif args.command == "arena-learning-apply":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_learning_apply_output(
            policy,
            policy_path=arena_policy_path,
            confirm=args.confirm,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        )
    elif args.command == "arena-attribution-backfill":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_attribution_backfill_output(
            policy,
            policy_path=arena_policy_path,
            since=args.since,
            dry_run=bool(args.dry_run),
        )
    elif args.command == "arena-assets":
        output = _arena_assets_output(query=args.query, mics=args.mic, limit=args.limit)
    elif args.command == "arena-view":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_view_output(policy, policy_path=arena_policy_path, callback=str(args.callback))
    elif args.command == "arena-proposal":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_proposal_output(
            policy,
            policy_path=arena_policy_path,
            account_id=args.account,
            research_mode=_arena_research_mode_arg(args),
        )
    elif args.command == "arena-run":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_run_output(
            policy,
            policy_path=arena_policy_path,
            account_id=args.account,
            live=bool(args.live),
            confirmation=args.confirmation,
            research_mode=_arena_research_mode_arg(args),
        )
    elif args.command == "arena-position-sell":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_position_sell_output(
            policy,
            policy_path=arena_policy_path,
            account_id=args.account,
            symbol=args.symbol,
            quantity=args.quantity,
            live=bool(args.live),
            confirmation=args.confirmation,
        )
    elif args.command == "arena-confirm":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_confirm_many_output(
            policy,
            policy_path=arena_policy_path,
            confirmation_text=" ".join(args.confirmation),
            live=bool(args.live),
        )
    elif args.command == "arena-run-all":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_run_all_output(policy, policy_path=arena_policy_path, live=bool(args.live), research_mode=_arena_research_mode_arg(args))
    elif args.command == "arena-check-stops":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_check_stops_output(
            policy,
            policy_path=arena_policy_path,
            account_id=args.account,
            symbol=args.symbol,
            live=bool(args.live),
        )
    elif args.command == "arena-execute-triggered-stops":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_check_stops_output(
            policy,
            policy_path=arena_policy_path,
            account_id=args.account,
            symbol=args.symbol,
            live=bool(args.live),
            execute_triggered_stops=True,
        )
    elif args.command == "arena-recover-protection":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_recover_protection_output(
            policy,
            policy_path=arena_policy_path,
            account_id=args.account,
            symbol=args.symbol,
            live=bool(args.live),
            confirmation=args.confirmation,
        )
    elif args.command == "arena-strategy-propose":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_strategy_propose_output(
            policy,
            policy_path=arena_policy_path,
            account_id=args.account,
            mode=args.mode,
            risk_multiplier=args.risk_multiplier,
            pause=bool(args.pause),
            resume=bool(args.resume),
            trade_mode=args.trade_mode,
            add_universe=args.add_universe,
            remove_universe=args.remove_universe,
        )
    elif args.command == "arena-strategy-apply":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_strategy_apply_output(
            policy,
            policy_path=arena_policy_path,
            account_id=args.account,
            mode=args.mode,
            risk_multiplier=args.risk_multiplier,
            pause=bool(args.pause),
            resume=bool(args.resume),
            trade_mode=args.trade_mode,
            add_universe=args.add_universe,
            remove_universe=args.remove_universe,
            confirm=args.confirm,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        )
    elif args.command == "arena-emergency-stop":
        policy = load_arena_policy(arena_policy_path)
        output = _arena_emergency_stop_output(
            policy,
            policy_path=arena_policy_path,
            confirm=args.confirm,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        )
    elif args.command == "policy-check":
        policy = h4_monitor.load_policy(policy_path)
        output = _policy_check_output(policy, policy_path=policy_path, verbose=bool(args.verbose))
    elif args.command == "policy-validate":
        policy = h4_monitor.load_policy(policy_path)
        output = _policy_validate_output(policy, policy_path=policy_path)
    elif args.command == "policy-propose":
        policy = h4_monitor.load_policy(policy_path)
        output = _policy_propose_output(
            policy,
            policy_path=policy_path,
            mode=args.mode,
            set_values=args.set,
            add_universe=args.add_universe,
            remove_universe=args.remove_universe,
        )
    elif args.command == "policy-apply":
        policy = h4_monitor.load_policy(policy_path)
        output = _policy_apply_output(
            policy,
            policy_path=policy_path,
            mode=args.mode,
            set_values=args.set,
            add_universe=args.add_universe,
            remove_universe=args.remove_universe,
            confirm=args.confirm,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        )
    else:
        raise AssertionError(args.command)

    _journal_path = arena_policy_path if args.command.startswith("arena-") else policy_path
    _journal(args.command, output, policy_path=_journal_path)
    if args.command in {"arena-llm-context", "arena-status"} and not output.get("full_json"):
        # Diagnostic output is recursively redacted before this sink; covered by sentinel-secret tests.
        # codeql[py/clear-text-logging-sensitive-data]
        print(json.dumps(redact_environment_values(output), ensure_ascii=False, default=str, separators=(",", ":")))
    else:
        # Diagnostic output is recursively redacted before this sink; covered by sentinel-secret tests.
        # codeql[py/clear-text-logging-sensitive-data]
        print(json.dumps(redact_environment_values(output), ensure_ascii=False, default=str, indent=2))
    return 0


def _status_output(report: dict[str, Any]) -> dict[str, Any]:
    account = report.get("account") if isinstance(report.get("account"), dict) else {}
    return {
        "status": report.get("status"),
        "command": "status",
        "time_msk": report.get("time_msk"),
        "account": account,
        "positions_count": len(report.get("positions") or []),
        "zero_positions_count": len(report.get("zero_positions") or []),
        "active_orders_count": (report.get("orders") or {}).get("orders_count", 0),
        "errors": report.get("errors") or [],
        "warnings": report.get("warnings") or [],
    }


def _scan_output(report: dict[str, Any]) -> dict[str, Any]:
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    return {
        "status": report.get("status"),
        "command": "scan",
        "time_msk": report.get("time_msk"),
        "operator_mode": report.get("operator_mode"),
        "candidates": candidates,
        "research": report.get("research"),
        "errors": report.get("errors") or [],
        "warnings": report.get("warnings") or [],
    }


def _morning_plan_output(report: dict[str, Any]) -> dict[str, Any]:
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    active = [item for item in candidates if isinstance(item, dict) and item.get("status") != "BLOCKED"]
    watchlist = [item for item in candidates if isinstance(item, dict) and item.get("status") == "BLOCKED"]
    return {
        "status": report.get("status"),
        "command": "morning-plan",
        "time_msk": report.get("time_msk"),
        "plan": [
            "Не гнаться за свободным cash; покупать только при чистом H4-кандидате.",
            "Проверить текущие LONG-позиции и наличие protective SELL SL.",
            "На H4-прогонах искать confirmed LONG с целью >= 1.5R.",
            "При кандидате запускать compact Codex review; AVOID блокирует покупку.",
            "В supervised режиме новая покупка требует Telegram confirmation.",
        ],
        "buy_candidates_now": active,
        "watchlist_now": watchlist,
        "errors": report.get("errors") or [],
        "warnings": report.get("warnings") or [],
    }


def _arena_status_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    send_telegram: bool = False,
    dry_run: bool = False,
    full_json: bool = False,
) -> dict[str, Any]:
    report = build_arena_status(policy, soft_stops=_arena_soft_stop_records())
    if send_telegram or dry_run or full_json:
        _enrich_arena_status_position_prices(report)
    pending_approvals = _arena_pending_approval_summaries()
    if pending_approvals:
        report = report | {"pending_approvals": pending_approvals}
    text = h4_monitor_notify.format_arena_pulse(report)
    markup = h4_monitor_notify.arena_reply_markup()
    h4_monitor_notify.validate_telegram_html(text)
    full_output = report | {
        "command": "arena-status",
        "policy_path": str(policy_path),
        "telegram_pulse": text,
        "telegram_reply_markup": markup,
        "telegram_delivery": "not_requested",
        "safety": _safety_payload(),
        "full_json": True,
    }
    if dry_run:
        full_output["telegram_delivery"] = "dry_run"
        _attach_arena_telegram_snapshot_write(full_output, policy_path=policy_path)
        return full_output
    if send_telegram:
        try:
            h4_monitor_notify.send_telegram_message(text, reply_markup=markup)
        except Exception as exc:  # noqa: BLE001
            full_output["telegram_delivery"] = "failed"
            full_output["telegram_error"] = str(exc)
            return full_output
        full_output["telegram_delivery"] = "ok"
        _attach_arena_telegram_snapshot_write(full_output, policy_path=policy_path)
        return full_output
    if full_json:
        _attach_arena_telegram_snapshot_write(full_output, policy_path=policy_path)
        return full_output
    return _arena_compact_status_output(report, policy=policy, policy_path=policy_path)


def _enrich_arena_status_position_prices(report: dict[str, Any], *, env: Any = os.environ) -> None:
    """Add last quote to Arena status positions before Telegram/snapshot formatting.

    Broker account payloads usually contain average entry price, but not the live
    mark. Button views are rendered from the saved arena-status snapshot, so the
    snapshot itself must carry current_price.
    """
    raw_accounts = report.get("accounts")
    accounts: list[Any] = raw_accounts if isinstance(raw_accounts, list) else []
    warnings = report.setdefault("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
        report["warnings"] = warnings
    _enrich_arena_account_position_prices(accounts, warnings=warnings, env=env)


def _enrich_arena_account_position_prices(accounts: list[Any], *, warnings: list[str], env: Any = os.environ) -> None:
    token = str(env.get("FINAM_TOKEN") or "").strip()
    symbols: list[str] = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        raw_positions = account.get("positions")
        positions = raw_positions if isinstance(raw_positions, list) else []
        for position in positions:
            if not isinstance(position, dict) or _decimal(position.get("current_price")) is not None:
                continue
            symbol = str(position.get("symbol") or "").strip()
            if symbol:
                symbols.append(symbol)
    if not token or not symbols:
        return
    try:
        client = FinamClient()
        jwt = client.create_session(token)
    except Exception as exc:  # noqa: BLE001 - status report should survive quote enrichment failures.
        warnings.append(f"Arena: текущие цены позиций недоступны: {exc}")
        return
    quote_cache: dict[str, str] = {}
    for symbol in dict.fromkeys(symbols):
        try:
            quote = client.last_quote(jwt, symbol)
            price = _arena_quote_price_payload(quote)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Arena:{symbol}: текущая цена позиции недоступна: {exc}")
            continue
        if price is not None:
            quote_cache[symbol] = price
    for account in accounts:
        if not isinstance(account, dict):
            continue
        raw_positions = account.get("positions")
        positions = raw_positions if isinstance(raw_positions, list) else []
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = str(position.get("symbol") or "").strip()
            if symbol in quote_cache and _decimal(position.get("current_price")) is None:
                position["current_price"] = quote_cache[symbol]


def _arena_quote_price_payload(quote: dict[str, Any]) -> str | None:
    if not isinstance(quote, dict):
        return None
    nested_quote = quote.get("quote")
    payload: dict[str, Any] = nested_quote if isinstance(nested_quote, dict) else quote
    for key in ("last", "price", "last_price", "close"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("num") or raw.get("amount")
        price = _decimal(raw)
        if price is not None and price > 0:
            return str(price)
    return None


def _attach_arena_telegram_snapshot_write(output: dict[str, Any], *, policy_path: Path, env: Any = os.environ) -> None:
    snapshot_path = _arena_telegram_snapshot_path(env=env)
    output["snapshot_path"] = str(snapshot_path)
    if not _arena_telegram_snapshot_payload_is_valid(output):
        output["snapshot_status"] = "skipped_invalid_payload"
        return
    try:
        snapshot = _arena_telegram_snapshot_payload(output, policy_path=policy_path)
        _write_arena_telegram_snapshot(snapshot_path, snapshot)
    except Exception as exc:  # noqa: BLE001 - Telegram report must survive cache failures.
        output["snapshot_status"] = "write_failed"
        output["snapshot_error"] = str(exc)
        return
    output["snapshot_status"] = "written"
    output["snapshot_created_at"] = snapshot.get("created_at")


def _arena_telegram_snapshot_payload(output: dict[str, Any], *, policy_path: Path) -> dict[str, Any]:
    safe_keys = (
        "status",
        "mode",
        "accounts",
        "errors",
        "warnings",
        "pending_approvals",
        "telegram_pulse",
        "telegram_reply_markup",
        "safety",
    )
    payload = {key: output.get(key) for key in safe_keys if key in output}
    payload.update(
        {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_command": "arena-status",
            "policy_path": str(policy_path),
        }
    )
    return payload


def _arena_telegram_snapshot_payload_is_valid(output: dict[str, Any]) -> bool:
    accounts = output.get("accounts")
    markup = output.get("telegram_reply_markup")
    return isinstance(accounts, list) and len(accounts) > 0 and isinstance(markup, dict)


def _write_arena_telegram_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _arena_telegram_snapshot_path(*, env: Any = os.environ) -> Path:
    configured = str(env.get(ARENA_TELEGRAM_SNAPSHOT_ENV) or "").strip()
    return Path(configured) if configured else DEFAULT_ARENA_TELEGRAM_SNAPSHOT_PATH


def _arena_telegram_snapshot_ttl_seconds(*, env: Any = os.environ) -> int:
    return _positive_int(env.get(ARENA_TELEGRAM_SNAPSHOT_TTL_ENV), default=DEFAULT_ARENA_TELEGRAM_SNAPSHOT_TTL_SECONDS)


def _read_arena_telegram_snapshot(*, policy_path: Path, env: Any = os.environ, now: datetime | None = None) -> dict[str, Any]:
    path = _arena_telegram_snapshot_path(env=env)
    current = now or datetime.now(timezone.utc)
    base = {
        "snapshot_path": str(path),
        "snapshot_source": "arena-status",
    }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return base | {"ok": False, "snapshot_status": "missing", "reason": "snapshot_missing"}
    except Exception as exc:  # noqa: BLE001
        return base | {"ok": False, "snapshot_status": "invalid", "reason": "snapshot_invalid", "error": str(exc)}
    if not isinstance(raw, dict):
        return base | {"ok": False, "snapshot_status": "invalid", "reason": "snapshot_not_object"}
    if str(raw.get("policy_path") or "") and str(raw.get("policy_path")) != str(policy_path):
        return base | {"ok": False, "snapshot_status": "invalid", "reason": "snapshot_policy_mismatch"}
    created_at = _parse_time(raw.get("created_at"))
    if created_at is None:
        return base | {"ok": False, "snapshot_status": "invalid", "reason": "snapshot_created_at_invalid"}
    age_seconds = max(0, int((current - created_at).total_seconds()))
    ttl_seconds = _arena_telegram_snapshot_ttl_seconds(env=env)
    if age_seconds > ttl_seconds:
        return base | {
            "ok": False,
            "snapshot_status": "stale",
            "reason": "snapshot_stale",
            "snapshot_age_seconds": age_seconds,
            "snapshot_ttl_seconds": ttl_seconds,
            "created_at": raw.get("created_at"),
        }
    return base | {
        "ok": True,
        "snapshot_status": "fresh",
        "snapshot_age_seconds": age_seconds,
        "snapshot_ttl_seconds": ttl_seconds,
        "created_at": raw.get("created_at"),
        "snapshot": raw,
    }


def _arena_stale_snapshot_view(*, policy_path: Path, callback: str, snapshot_state: dict[str, Any]) -> dict[str, Any]:
    reason = str(snapshot_state.get("reason") or snapshot_state.get("snapshot_status") or "snapshot_unavailable")
    text = "\n".join(
        [
            "<b>🏟️ Finam Arena</b>",
            "⚪ Данные обзора устарели или ещё не сохранены.",
            "Запроси новый обзор с кнопками.",
            f"Причина: {reason}",
        ]
    )
    return {
        "status": "STALE_SNAPSHOT",
        "command": "arena-view",
        "policy_path": str(policy_path),
        "callback": callback,
        "telegram_text": text,
        "telegram_reply_markup": h4_monitor_notify.arena_reply_markup(),
        "snapshot_status": snapshot_state.get("snapshot_status"),
        "snapshot_source": snapshot_state.get("snapshot_source"),
        "snapshot_age_seconds": snapshot_state.get("snapshot_age_seconds"),
        "snapshot_path": snapshot_state.get("snapshot_path"),
        "reason": reason,
        "safety": _safety_payload(),
    }


def _arena_snapshot_view_response(
    snapshot: dict[str, Any],
    *,
    policy_path: Path,
    callback: str,
    text: str,
    snapshot_state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "OK",
        "command": "arena-view",
        "policy_path": str(policy_path),
        "callback": callback,
        "scan_status": snapshot.get("status"),
        "telegram_text": text,
        "telegram_reply_markup": snapshot.get("telegram_reply_markup") or h4_monitor_notify.arena_reply_markup(),
        "errors": snapshot.get("errors") or [],
        "warnings": snapshot.get("warnings") or [],
        "snapshot_status": snapshot_state.get("snapshot_status"),
        "snapshot_source": snapshot.get("source_command") or snapshot_state.get("snapshot_source"),
        "snapshot_age_seconds": snapshot_state.get("snapshot_age_seconds"),
        "snapshot_path": snapshot_state.get("snapshot_path"),
        "safety": snapshot.get("safety") or _safety_payload(),
    }


def _arena_snapshot_with_fresh_pending_approvals(snapshot: dict[str, Any]) -> dict[str, Any]:
    pending_approvals = _arena_pending_approval_summaries()
    if not pending_approvals:
        return snapshot
    return dict(snapshot) | {"pending_approvals": pending_approvals}


def _arena_scan_output(policy: dict[str, Any], *, policy_path: Path, research_mode: str = "cache_only") -> dict[str, Any]:
    ledger = _read_arena_execution_ledger(env=os.environ)
    scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(), research_mode=research_mode, execution_ledger=ledger)
    pending_approvals = _arena_pending_approval_summaries()
    if pending_approvals:
        scan = scan | {"pending_approvals": pending_approvals}
    return scan | {
        "policy_path": str(policy_path),
        "telegram_pulse": h4_monitor_notify.format_arena_pulse(scan),
        "telegram_reply_markup": h4_monitor_notify.arena_reply_markup(),
        "safety": _safety_payload(),
    }


def _arena_portfolio_review_output(policy: dict[str, Any], *, policy_path: Path, research_mode: str = "cache_only") -> dict[str, Any]:
    ledger = _read_arena_execution_ledger(env=os.environ)
    scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(), research_mode=research_mode, execution_ledger=ledger)
    review = build_arena_portfolio_review(policy, scan)
    return review | {
        "policy_path": str(policy_path),
        "telegram_text": h4_monitor_notify.format_arena_portfolio_review(review),
        "telegram_reply_markup": h4_monitor_notify.arena_reply_markup(),
        "safety": _safety_payload(),
    }


def _arena_mcp_shadow_review_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    max_bytes: int = 8000,
    research_mode: str = "cache_only",
) -> dict[str, Any]:
    ledger = _read_arena_execution_ledger(env=os.environ)
    scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(), research_mode=research_mode, execution_ledger=ledger)
    shadow = build_mcp_shadow_review(policy, arena_scan=scan, env=os.environ, max_bytes=max_bytes)
    return shadow | {
        "command": "arena-mcp-shadow-review",
        "policy_path": str(policy_path),
        "scan_status": scan.get("status"),
        "safety": _safety_payload(),
    }


def _arena_news_scan_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    event_path: Path,
    limit: int,
    opener: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    url = str(research.get("finam_rss_url") or "").strip()
    if not url:
        return {
            "status": "SKIPPED",
            "command": "arena-news-scan",
            "reason": "finam_rss_url_missing",
            "event_path": str(event_path),
            "records_written": 0,
            "safety": _safety_payload(),
        }
    try:
        response = (opener or urllib.request.urlopen)(url, timeout=int(research.get("timeout_seconds") or 20))
        raw = response.read()
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "DEGRADED",
            "command": "arena-news-scan",
            "reason": "rss_fetch_failed",
            "error": str(exc),
            "event_path": str(event_path),
            "records_written": 0,
            "safety": _safety_payload(),
        }
    candidates = _arena_event_candidates_from_rss(raw, policy=policy, now=current, limit=max(1, limit))
    written = append_jsonl(event_path, candidates)
    return {
        "status": "OK",
        "command": "arena-news-scan",
        "policy_path": str(policy_path),
        "event_path": str(event_path),
        "source": "finam_rss",
        "sonar_provider_call": False,
        "records_written": written,
        "candidates": candidates,
        "safety": _safety_payload(),
    }


def _arena_news_propose_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    event_path: Path,
    limit: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    events = _read_recent_arena_event_candidates(event_path, now=current)
    events = sorted(events, key=lambda item: str(item.get("timestamp") or item.get("published_at") or ""), reverse=True)[: max(1, limit)]
    account_universes = _arena_account_universes(policy)
    proposal = {
        "add_universe": [],
        "remove_universe": [],
        "deprioritize": [],
        "manual_review": [],
    }
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        symbol = str(event.get("symbol") or "").upper()
        if not symbol:
            continue
        account_ids = [str(item) for item in event.get("account_ids") or [] if str(item)]
        decision = str(event.get("decision") or "event_candidate_unclassified").lower()
        for account_id in account_ids:
            action = _arena_news_proposal_action(symbol, account_id=account_id, decision=decision, account_universes=account_universes)
            key = (action, account_id, symbol)
            if key in seen:
                continue
            seen.add(key)
            item = {
                "account_id": account_id,
                "symbol": symbol,
                "decision": event.get("decision") or "event_candidate_unclassified",
                "title": event.get("title"),
                "link": event.get("link"),
                "reason": _arena_news_proposal_reason(action, decision),
                "apply_command": _arena_news_apply_command(action, account_id=account_id, symbol=symbol),
            }
            proposal[action].append({key: value for key, value in item.items() if value is not None})
    return {
        "status": "OK",
        "command": "arena-news-propose",
        "policy_path": str(policy_path),
        "event_path": str(event_path),
        "events_considered": len(events),
        "policy_proposal": proposal,
        "write_applied": False,
        "next_step": "Review policy_proposal, then apply through arena-strategy-apply with --confirm APPLY_ARENA_STRATEGY.",
        "safety": _safety_payload(),
    }


def _arena_account_universes(policy: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for account in policy.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("account_id") or "")
        result[account_id] = {str(symbol).upper() for symbol in account.get("universe") or [] if str(symbol)}
    return result


def _arena_news_proposal_action(
    symbol: str,
    *,
    account_id: str,
    decision: str,
    account_universes: dict[str, set[str]],
) -> str:
    current_universe = account_universes.get(account_id, set())
    if decision in {"positive_catalyst", "event_positive", "add_universe"} and symbol not in current_universe:
        return "add_universe"
    if decision in {"negative_catalyst", "event_negative", "research_avoid", "deprioritize"}:
        return "deprioritize"
    if decision in {"remove_universe", "event_remove"}:
        return "remove_universe"
    return "manual_review"


def _arena_news_proposal_reason(action: str, decision: str) -> str:
    if action == "add_universe":
        return "classified_positive_event_for_symbol_outside_account_universe"
    if action == "deprioritize":
        return "classified_negative_event_requires_learning_deprioritize_review"
    if action == "remove_universe":
        return "classified_remove_event_requires_universe_review"
    return f"{decision or 'event_candidate_unclassified'} requires manual review before policy mutation"


def _arena_news_apply_command(action: str, *, account_id: str, symbol: str) -> str | None:
    if action == "add_universe":
        return f"python scripts/hermes_operator.py arena-strategy-apply --account {account_id} --add-universe {symbol} --confirm APPLY_ARENA_STRATEGY"
    if action == "remove_universe":
        return f"python scripts/hermes_operator.py arena-strategy-apply --account {account_id} --remove-universe {symbol} --confirm APPLY_ARENA_STRATEGY"
    return None


def _arena_opportunity_auction_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    research_mode: str = "cache_only",
    event_path: Path | None = None,
    ledger_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    ledger = _read_arena_execution_ledger(env=os.environ)
    scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(), research_mode=research_mode, execution_ledger=ledger)
    review = build_arena_portfolio_review(policy, scan, now=current)
    events = _read_recent_arena_event_candidates(event_path or _arena_event_candidates_path(), now=current)
    auction = build_arena_opportunity_auction(policy, scan, review, event_candidates=events, now=current)
    record = _arena_opportunity_ledger_record(auction, scan=scan, review=review, event_path=event_path, now=current)
    records_written = 0
    if ledger_path is not None:
        records_written = append_jsonl(ledger_path, [record])
    return auction | {
        "policy_path": str(policy_path),
        "research_mode": research_mode,
        "event_path": str(event_path or _arena_event_candidates_path()),
        "ledger_path": str(ledger_path) if ledger_path is not None else None,
        "ledger_records_written": records_written,
        "calibration": _arena_opportunity_calibration_summary(ledger_path or _arena_opportunity_ledger_path(), now=current),
        "safety": _safety_payload(),
    }


def _arena_growth_propose_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    top_n: int = 5,
    env: Any = os.environ,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    limit = max(1, int(top_n or 5))
    ledger = _read_arena_execution_ledger(env=env)
    scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(env=env), research_mode="cache_only", execution_ledger=ledger)
    scan = _arena_scan_with_cached_codex_reviews(policy, scan)
    review = build_arena_portfolio_review(policy, scan, now=current)
    events = _read_recent_arena_event_candidates(_arena_event_candidates_path(env=env), now=current)
    auction = build_arena_opportunity_auction(policy, scan, review, event_candidates=events, now=current)
    top_candidates = _arena_growth_top_candidates(scan, auction=auction, limit=limit)
    research_queue = _arena_growth_research_queue(top_candidates)
    paths = _arena_learning_paths()
    decisions = read_jsonl(paths["decisions"])
    outcomes = read_jsonl(paths["outcomes"])
    learning_report = build_learning_report(policy, decisions, outcomes)
    learning_report = _learning_report_with_research_budget(learning_report)
    positive_learning = identify_positive_learning_candidates(policy, outcomes)
    capital_allocation = _arena_growth_capital_allocation(policy, outcomes, positive_learning=positive_learning)
    ai_account = _arena_growth_ai_account_review(policy, top_candidates)
    recommended_actions = _arena_growth_recommended_actions(
        research_queue=research_queue,
        positive_learning=positive_learning,
        capital_allocation=capital_allocation,
        ai_account=ai_account,
    )
    blocked_actions = _arena_growth_blocked_actions(
        top_candidates,
        positive_learning=positive_learning,
        capital_allocation=capital_allocation,
        ai_account=ai_account,
    )
    degraded = str(scan.get("status") or "").upper() not in {"OK", "WARN"} or str(review.get("status") or "").upper() not in {"OK", "WARN"}
    status = "DEGRADED" if degraded else ("PROPOSED" if (top_candidates or research_queue or recommended_actions) else "NO_ACTION")
    return {
        "status": status,
        "command": "arena-growth-propose",
        "policy_path": str(policy_path),
        "growth_mode": "shadow_propose",
        "generated_at": current.isoformat(timespec="seconds"),
        "explanations": [
            "gas is research/selection/allocation, not weakening gates",
            "hard gates unchanged",
            "live execution still requires existing Arena live gates/confirmations",
        ],
        "portfolio_state": _arena_growth_portfolio_state(scan, review),
        "learning_state": _arena_growth_learning_state(learning_report),
        "auction": {
            "status": auction.get("status"),
            "command": auction.get("command"),
            "score_model": auction.get("score_model"),
            "winner": auction.get("winner"),
            "hold_cash_benchmark": _arena_growth_hold_cash_benchmark(auction),
            "top_candidates": top_candidates,
            "research": auction.get("research"),
            "live_use_allowed": bool(((auction.get("score_model") or {}).get("live_use_allowed"))),
        },
        "research_queue": research_queue,
        "positive_learning": positive_learning,
        "capital_allocation": capital_allocation,
        "ai_account_DEMO-AI": ai_account,
        "recommended_actions": recommended_actions,
        "blocked_actions": blocked_actions,
        "errors": list(dict.fromkeys(list(scan.get("errors") or []) + list(review.get("errors") or []) + list(auction.get("errors") or []))),
        "warnings": list(dict.fromkeys(list(scan.get("warnings") or []) + list(review.get("warnings") or []) + list(auction.get("warnings") or []))),
        "broker_mutation": False,
        "write_applied": False,
        "safety": _safety_payload(policy_write=False, trading_mutations=False)
        | {
            "policy_write": False,
            "provider_call": False,
            "auction_ledger_write": False,
            "hard_gates_unchanged": True,
            "risk_expansion": False,
        },
    }


def _arena_growth_research_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    top_n: int = 3,
    review_timeout_seconds: int = 90,
    env: Any = os.environ,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    limit = max(1, int(top_n or 3))
    ledger = _read_arena_execution_ledger(env=env)
    scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(env=env), research_mode="cache_only", execution_ledger=ledger)
    review = build_arena_portfolio_review(policy, scan, now=current)
    events = _read_recent_arena_event_candidates(_arena_event_candidates_path(env=env), now=current)
    auction = build_arena_opportunity_auction(policy, scan, review, event_candidates=events, now=current)
    top_candidates = _arena_growth_top_candidates(scan, auction=auction, limit=limit)
    queue = _arena_growth_research_queue(top_candidates)
    calls: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    candidates_by_key = {
        (str(item.get("account_id") or ""), str(item.get("symbol") or "").upper()): item
        for item in scan.get("candidates") or []
        if isinstance(item, dict)
    }
    for item in queue[:limit]:
        candidate = candidates_by_key.get((str(item.get("account_id") or ""), str(item.get("symbol") or "").upper()))
        if candidate is None:
            skipped_items.append(_arena_growth_research_skipped_call(item, reason="candidate_not_found_after_scan"))
            continue
        call = _arena_growth_research_candidate(
            policy,
            scan=scan,
            candidate=candidate,
            timeout_seconds=max(5, int(review_timeout_seconds or 90)),
            env=env,
            now=current,
        )
        if _arena_growth_research_call_is_skipped(call):
            skipped_items.append(call)
        else:
            calls.append(call)
    for item in queue[limit:]:
        skipped_items.append(_arena_growth_research_skipped_call(item, reason="top_n_limit"))
    if not calls:
        if not skipped_items:
            skipped_items.append(_arena_growth_research_skipped_call({"account_id": None, "symbol": None}, reason="no_research_queue_items"))
    budget = _research_budget_report_output(root=DEFAULT_RESEARCH_ROOT, date=current.date().isoformat(), now=current)
    return {
        "status": _arena_growth_research_status(calls, skipped_items=skipped_items),
        "command": "arena-growth-research",
        "policy_path": str(policy_path),
        "growth_mode": "explicit_research",
        "research_calls": calls,
        "skipped_research_items": skipped_items,
        "research_queue_count": len(queue),
        "research_budget": budget,
        "next_growth_command": "python3 scripts/hermes_operator.py arena-growth-propose --top-n 5",
        "broker_mutation": False,
        "write_applied": False,
        "safety": _safety_payload(policy_write=False, trading_mutations=False)
        | {
            "policy_write": False,
            "risk_expansion": False,
            "provider_call": any(bool(item.get("provider_call")) for item in calls),
            "hard_gates_unchanged": True,
        },
    }


def _arena_growth_research_candidate(
    policy: dict[str, Any],
    *,
    scan: dict[str, Any],
    candidate: dict[str, Any],
    timeout_seconds: int,
    env: Any,
    now: datetime,
) -> dict[str, Any]:
    account_id = str(candidate.get("account_id") or "")
    symbol = str(candidate.get("symbol") or "").upper()
    supported_symbol = _normalize_symbol(symbol)
    if supported_symbol is None:
        return _arena_growth_research_skipped_call(
            {"account_id": account_id, "symbol": symbol},
            reason="unsupported_symbol_or_mic",
            error=f"unsupported research symbol: {symbol}",
        )
    context_output = _codex_review_context_output(_arena_scan_as_review_report(scan, candidate), policy=policy, symbol=symbol)
    if context_output.get("status") != "OK":
        return _arena_growth_research_skipped_call(
            {"account_id": account_id, "symbol": symbol},
            reason="context_unavailable",
            error=context_output.get("error") or context_output.get("reason"),
        )
    context = context_output["context"]
    context_hash = str(context.get("context_hash") or "")
    existing = _load_fresh_codex_review(symbol, context_hash, policy)
    if existing is None:
        existing = _load_latest_fresh_codex_review_for_symbol(symbol, policy)
    if existing is not None:
        verdict = _codex_review_record_verdict({"review": existing.get("review") or {}}) or "UNAVAILABLE"
        return {
            "account_id": account_id,
            "symbol": symbol,
            "provider_call": False,
            "cache_hit": True,
            "verdict": verdict,
            "growth_verdict": _arena_growth_verdict(verdict),
            "artifact_path": existing.get("_path"),
            "error": None,
            "reason": "provider_returned_unavailable_cached" if verdict == "UNAVAILABLE" else "fresh_codex_review_cache_hit",
        }
    budget = _arena_growth_research_budget_guard(policy, now=now)
    if not budget["ok"]:
        call = _arena_growth_research_skipped_call(
            {"account_id": account_id, "symbol": symbol},
            reason=budget["reason"],
            error=budget.get("error"),
        )
        call["budget"] = {key: budget.get(key) for key in ("limit", "used", "remaining")}
        _write_arena_growth_research_accounting_artifact(call, now=now)
        return call
    generated = _run_codex_review_command(context, timeout_seconds=timeout_seconds, env=env)
    provider_call = True
    if generated.get("status") != "OK":
        call = {
            "account_id": account_id,
            "symbol": symbol,
            "provider_call": provider_call,
            "cache_hit": False,
            "verdict": "UNAVAILABLE",
            "growth_verdict": "UNAVAILABLE",
            "artifact_path": None,
            "error": _redact_secret_values(generated.get("error") or generated.get("reason") or "codex_review_failed", env=env),
            "reason": generated.get("reason") or "codex_review_failed",
            "budget": {key: budget.get(key) for key in ("limit", "used", "remaining")},
        }
        _write_arena_growth_research_accounting_artifact(call, now=now)
        return call
    record = _codex_review_record_output(
        _arena_scan_as_review_report(scan, candidate),
        policy=policy,
        symbol=symbol,
        review_json=str(generated["review_json"]),
        confirm=CODEX_REVIEW_CONFIRM,
    )
    if record.get("status") != "OK":
        call = {
            "account_id": account_id,
            "symbol": symbol,
            "provider_call": provider_call,
            "cache_hit": False,
            "verdict": "UNAVAILABLE",
            "growth_verdict": "UNAVAILABLE",
            "artifact_path": None,
            "error": _redact_secret_values(record.get("error") or record.get("reason") or "codex_review_record_failed", env=env),
            "reason": record.get("reason") or "codex_review_record_failed",
            "budget": {key: budget.get(key) for key in ("limit", "used", "remaining")},
        }
        _write_arena_growth_research_accounting_artifact(call, now=now)
        return call
    verdict = str(((record.get("review") or {}).get("verdict")) or "UNAVAILABLE").upper()
    call = {
        "account_id": account_id,
        "symbol": symbol,
        "provider_call": provider_call,
        "cache_hit": False,
        "verdict": verdict,
        "growth_verdict": _arena_growth_verdict(verdict),
        "artifact_path": record.get("path"),
        "error": None,
        "reason": "codex_review_recorded",
        "budget": {key: budget.get(key) for key in ("limit", "used", "remaining")},
    }
    _write_arena_growth_research_accounting_artifact(call, now=now)
    return call


def _arena_scan_with_cached_codex_reviews(policy: dict[str, Any], scan: dict[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for candidate in scan.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        symbol = str(candidate.get("symbol") or "").upper()
        if not symbol:
            continue
        context_output = _codex_review_context_output(_arena_scan_as_review_report(scan, candidate), policy=policy, symbol=symbol)
        existing = None
        if context_output.get("status") != "OK":
            existing = _load_latest_fresh_codex_review_for_symbol(symbol, policy)
        else:
            existing = _load_fresh_codex_review(symbol, str((context_output.get("context") or {}).get("context_hash") or ""), policy)
            if existing is None:
                existing = _load_latest_fresh_codex_review_for_symbol(symbol, policy)
        if existing is None:
            continue
        records.append(
            {
                "symbol": symbol,
                "status": "EXISTS",
                "context_hash": existing.get("context_hash"),
                "review": existing.get("review") or {},
                "path": existing.get("_path"),
            }
        )
    return _arena_scan_with_codex_reviews(policy, scan, {"status": "OK", "records_written": 0, "records": records}) if records else scan


def _arena_growth_research_budget_guard(policy: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    raw_limit = research.get("arena_growth_research_daily_budget")
    if raw_limit is None:
        raw_limit = research.get("arena_growth_research_max_calls_per_day")
    try:
        limit = int(raw_limit if raw_limit is not None else 3)
    except (TypeError, ValueError):
        limit = 3
    limit = max(0, limit)
    used = _arena_growth_research_provider_calls_today(now=now)
    remaining = max(0, limit - used)
    if remaining <= 0:
        return {
            "ok": False,
            "reason": "arena_growth_research_budget_exhausted",
            "limit": limit,
            "used": used,
            "remaining": remaining,
        }
    return {"ok": True, "limit": limit, "used": used, "remaining": remaining}


def _arena_growth_research_provider_calls_today(*, now: datetime) -> int:
    target_date = now.date()
    root = DEFAULT_RESEARCH_ROOT / "arena_growth_research"
    if not root.exists():
        return 0
    count = 0
    for path in root.glob("*.json"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if modified.date() == target_date and isinstance(payload, dict) and payload.get("provider_call") is True:
            count += 1
    return count


def _write_arena_growth_research_accounting_artifact(call: dict[str, Any], *, now: datetime) -> str | None:
    target_dir = DEFAULT_RESEARCH_ROOT / "arena_growth_research"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        account_id = str(call.get("account_id") or "none")
        symbol = _artifact_symbol(str(call.get("symbol") or "NONE"))
        path = target_dir / f"{stamp}_{account_id}_{symbol}.json"
        suffix = 1
        while path.exists():
            path = target_dir / f"{stamp}_{account_id}_{symbol}_{suffix}.json"
            suffix += 1
        payload = {
            "status": "ok" if call.get("error") is None else "failed",
            "provider": "codex_review",
            "mode": "arena_growth_research",
            "model": "openai-codex",
            "symbols": [call.get("symbol")] if call.get("symbol") else [],
            "provider_call": bool(call.get("provider_call")),
            "cache_hit": bool(call.get("cache_hit")),
            "verdict": call.get("verdict"),
            "growth_verdict": call.get("growth_verdict"),
            "account_id": call.get("account_id"),
            "reason": call.get("reason"),
            "error": call.get("error"),
            "artifact_path": call.get("artifact_path"),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return None
    call["accounting_artifact_path"] = str(path)
    return str(path)


def _arena_growth_research_skipped_call(item: dict[str, Any], *, reason: str, error: Any = None) -> dict[str, Any]:
    return {
        "account_id": item.get("account_id"),
        "symbol": item.get("symbol"),
        "provider_call": False,
        "cache_hit": False,
        "verdict": "UNAVAILABLE",
        "growth_verdict": "UNAVAILABLE",
        "artifact_path": None,
        "error": str(error) if error else None,
        "reason": reason,
    }


def _arena_growth_research_call_is_skipped(call: dict[str, Any]) -> bool:
    if call.get("cache_hit") is True:
        return False
    if call.get("provider_call") is True:
        return False
    return str(call.get("reason") or "") in {
        "candidate_not_found_after_scan",
        "context_unavailable",
        "arena_growth_research_budget_exhausted",
        "unsupported_symbol_or_mic",
        "top_n_limit",
        "no_research_queue_items",
    }


def _arena_growth_research_status(calls: list[dict[str, Any]], *, skipped_items: list[dict[str, Any]] | None = None) -> str:
    actionable = [item for item in calls if item.get("symbol")]
    skipped = [item for item in skipped_items or [] if item.get("symbol")]
    if not actionable:
        if skipped:
            return "DEGRADED"
        return "OK"
    ok = [item for item in actionable if item.get("error") is None and item.get("reason") not in {"arena_growth_research_budget_exhausted", "context_unavailable"}]
    failed = [item for item in actionable if item not in ok] + skipped
    if ok and failed:
        return "PARTIAL"
    if failed and not ok:
        return "DEGRADED"
    return "OK"


def _arena_growth_verdict(verdict: str) -> str:
    normalized = str(verdict or "").upper()
    if normalized == "OK":
        return "BUY"
    if normalized == "AVOID":
        return "AVOID"
    if normalized == "RISK":
        return "NEUTRAL"
    return "UNAVAILABLE"


def _redact_secret_values(value: Any, *, env: Any = os.environ) -> str:
    return str(redact_environment_values(value or "", env=env))


def _arena_growth_top_candidates(scan: dict[str, Any], *, auction: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    candidates = [item for item in scan.get("candidates") or [] if isinstance(item, dict)]
    us_setups = {
        (str(item.get("symbol") or "").upper(), _arena_growth_candidate_setup(item))
        for item in candidates
        if str(item.get("account_id") or "") == "DEMO-US"
    }
    us_symbols = {symbol for symbol, _setup in us_setups}
    result: list[dict[str, Any]] = []
    for index, candidate in enumerate(sorted(candidates, key=_arena_candidate_score_value, reverse=True)[:limit], start=1):
        gates = [str(item) for item in candidate.get("gate_reasons") or [] if str(item)]
        account_id = str(candidate.get("account_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        setup = _arena_growth_candidate_setup(candidate)
        duplicate = account_id == "DEMO-AI" and (symbol, setup) in us_setups
        symbol_overlap = account_id == "DEMO-AI" and symbol in us_symbols
        result.append(
            {
                "rank": index,
                "account_id": account_id,
                "symbol": symbol,
                "side": str(candidate.get("side") or "BUY").upper(),
                "setup": setup,
                "score": _arena_candidate_score_value(candidate),
                "risk_adjusted_score": candidate.get("risk_adjusted_score") or _arena_candidate_score_value(candidate),
                "execution_allowed": bool(candidate.get("execution_allowed")) and not gates,
                "live_executable": bool(candidate.get("execution_allowed")) and not gates,
                "gate_reasons": gates,
                "missing_confirmations": _arena_growth_missing_confirmations(candidate, gates),
                "next_step": _arena_growth_candidate_next_step(candidate, gates, duplicate=duplicate),
                "research_verdict": _arena_growth_candidate_research_verdict(candidate),
                "duplicate_with_DEMO-US": duplicate,
                "symbol_overlap_with_DEMO-US": symbol_overlap,
                "auction_context": _arena_growth_candidate_auction_context(auction, account_id=account_id, symbol=symbol),
            }
        )
    return result


def _arena_growth_candidate_setup(candidate: dict[str, Any]) -> str:
    parts = [
        str(candidate.get("side") or "BUY").upper(),
        str(candidate.get("entry_timeframe") or candidate.get("timeframe") or "").upper(),
        str(candidate.get("setup") or candidate.get("strategy") or "").upper(),
    ]
    return ":".join(item for item in parts if item) or "BUY"


def _arena_growth_missing_confirmations(candidate: dict[str, Any], gates: list[str]) -> list[str]:
    missing: list[str] = []
    if "research_unavailable_below_exceptional_score" in gates or str(_arena_growth_candidate_research_verdict(candidate)).upper() in {"", "UNAVAILABLE"}:
        missing.append("research")
    if any("pretrade" in gate for gate in gates):
        missing.append("pretrade")
    if any("market_data" in gate or "quote" in gate for gate in gates):
        missing.append("market_data")
    if any("confirmation" in gate or "manual_review" in gate for gate in gates):
        missing.append("manual_confirmation")
    return list(dict.fromkeys(missing))


def _arena_growth_candidate_next_step(candidate: dict[str, Any], gates: list[str], *, duplicate: bool) -> str:
    if duplicate:
        return "refresh_ai_universe_not_duplicate_us_account"
    if "research_unavailable_below_exceptional_score" in gates:
        other_gates = [gate for gate in gates if gate != "research_unavailable_below_exceptional_score"]
        if not other_gates or set(other_gates) <= {"candidate_score_below_min"}:
            return "run_research"
        return "eligible_after_research_and_gate_review"
    if not gates and bool(candidate.get("execution_allowed")):
        return "manual_review_existing_live_gates"
    if "candidate_score_below_min" in gates:
        return "skip_low_quality"
    return "manual_review"


def _arena_growth_candidate_research_verdict(candidate: dict[str, Any]) -> str | None:
    score = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    verdict = score.get("research_verdict") or candidate.get("research_verdict")
    return str(verdict).upper() if verdict else None


def _arena_growth_candidate_auction_context(auction: dict[str, Any], *, account_id: str, symbol: str) -> dict[str, Any]:
    for opportunity in auction.get("opportunities") or []:
        if not isinstance(opportunity, dict):
            continue
        if str(opportunity.get("account_id") or "") == account_id and str(opportunity.get("symbol") or "").upper() == symbol:
            return {
                "opportunity_type": opportunity.get("opportunity_type"),
                "decision": opportunity.get("decision"),
                "risk_adjusted_score": opportunity.get("risk_adjusted_score"),
                "reason": opportunity.get("reason"),
            }
    return {}


def _arena_growth_research_queue(top_candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for candidate in top_candidates:
        gates = [str(item) for item in candidate.get("gate_reasons") or []]
        if "research_unavailable_below_exceptional_score" not in gates:
            continue
        hard_other_gates = [gate for gate in gates if gate not in {"research_unavailable_below_exceptional_score", "candidate_score_below_min"}]
        priority = max(1, 100 - int(candidate.get("rank") or 99) * 10)
        queue.append(
            {
                "symbol": candidate.get("symbol"),
                "account_id": candidate.get("account_id"),
                "reason": "top_candidate_research_missing",
                "blocking_gates": gates,
                "other_hard_gates": hard_other_gates,
                "research_command": "python scripts/hermes_operator.py arena-opportunity-auction --fresh-research --no-ledger",
                "safe_default_command": "python scripts/hermes_operator.py arena-growth-propose",
                "priority": priority,
                "provider_call_default": False,
            }
        )
    return queue


def _arena_growth_capital_allocation(
    policy: dict[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    positive_learning: dict[str, Any],
) -> dict[str, Any]:
    min_samples = int(positive_learning.get("min_samples_for_promotion") or 20)
    account_scores = []
    for account_id in _arena_account_ids(policy):
        records = [item for item in outcomes if str(item.get("account_id") or "") == account_id and not bool(item.get("exclude_from_learning"))]
        pnl_values = [_decimal(item.get("net_pnl_rub")) for item in records]
        pnl_values = [item for item in pnl_values if item is not None]
        wins = len([item for item in pnl_values if item > 0])
        total = sum(pnl_values, Decimal("0"))
        slope = _arena_growth_equity_slope(pnl_values)
        enough_samples = len(pnl_values) >= min_samples
        score = int(total / Decimal("1000")) if pnl_values else 0
        if pnl_values:
            score += int((Decimal(wins) / Decimal(len(pnl_values))) * Decimal("100"))
        if slope is not None:
            score += int(slope / Decimal("1000"))
        if not enough_samples:
            score -= 25
        account_scores.append(
            {
                "account_id": account_id,
                "sample_count": len(pnl_values),
                "net_pnl_rub": decimal_payload(total),
                "win_rate_pct": decimal_payload(Decimal(wins) / Decimal(len(pnl_values)) * Decimal("100")) if pnl_values else None,
                "equity_slope_rub_per_trade": decimal_payload(slope) if slope is not None else None,
                "allocation_score": score,
                "enough_samples": enough_samples,
            }
        )
    promotable = positive_learning.get("promotion_candidates") or []
    if not promotable:
        recommendation = "no risk expansion; research-only growth"
        next_slot_priority: list[dict[str, Any]] = []
    else:
        recommendation = "shift next slot priority toward promoted setup/account; no notional increase without separate apply/confirm"
        next_slot_priority = [
            {
                "account_id": item.get("account_id"),
                "symbol": item.get("symbol"),
                "setup": item.get("setup"),
                "reason": item.get("reason"),
            }
            for item in promotable[:5]
        ]
    return {
        "status": "OK",
        "account_scores": account_scores,
        "recommendation": recommendation,
        "next_slot_priority": next_slot_priority,
        "policy_write": False,
        "risk_expansion": False,
    }


def _arena_growth_equity_slope(pnls: list[Decimal]) -> Decimal | None:
    if len(pnls) < 2:
        return None
    cumulative: list[Decimal] = []
    running = Decimal("0")
    for pnl in pnls:
        running += pnl
        cumulative.append(running)
    return (cumulative[-1] - cumulative[0]) / Decimal(len(cumulative) - 1)


def _arena_growth_ai_account_review(policy: dict[str, Any], top_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    duplicates = [
        {
            "account_id": item.get("account_id"),
            "symbol": item.get("symbol"),
            "setup": item.get("setup"),
            "duplicate_of_account_id": "DEMO-US",
            "recommendation": "refresh alternative cross-market universe for DEMO-AI",
        }
        for item in top_candidates
        if item.get("duplicate_with_DEMO-US")
    ]
    overlaps = [
        {
            "account_id": item.get("account_id"),
            "symbol": item.get("symbol"),
            "setup": item.get("setup"),
            "overlaps_with_account_id": "DEMO-US",
            "recommendation": "review whether DEMO-AI should search a different symbol even when setup differs",
        }
        for item in top_candidates
        if item.get("symbol_overlap_with_DEMO-US") and not item.get("duplicate_with_DEMO-US")
    ]
    account = _arena_policy_account(policy, "DEMO-AI") or {}
    return {
        "status": "DUPLICATE_REVIEW" if duplicates else ("SYMBOL_OVERLAP_REVIEW" if overlaps else "OK"),
        "role": "experimental_alpha_cross_market_not_us_duplicate",
        "universe": [str(item).upper() for item in account.get("universe") or []],
        "duplicate_candidates": duplicates,
        "symbol_overlaps_with_DEMO-US": overlaps,
        "recommendation": "alternative search/universe refresh for DEMO-AI"
        if duplicates
        else ("review DEMO-AI symbol overlap with DEMO-US before treating it as distinct alpha" if overlaps else "keep DEMO-AI distinct from DEMO-US"),
    }


def _arena_growth_recommended_actions(
    *,
    research_queue: list[dict[str, Any]],
    positive_learning: dict[str, Any],
    capital_allocation: dict[str, Any],
    ai_account: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for item in research_queue[:5]:
        actions.append(
            {
                "action": "run_research",
                "symbol": item.get("symbol"),
                "account_id": item.get("account_id"),
                "reason": item.get("reason"),
                "command": item.get("research_command"),
                "provider_call_default": item.get("provider_call_default"),
            }
        )
    for item in (positive_learning.get("promotion_candidates") or [])[:5]:
        actions.append(
            {
                "action": "promote_setup_priority_shadow",
                "account_id": item.get("account_id"),
                "symbol": item.get("symbol"),
                "setup": item.get("setup"),
                "reason": item.get("reason"),
                "policy_write": False,
            }
        )
    if capital_allocation.get("next_slot_priority"):
        actions.append(
            {
                "action": "shift_next_slot_priority_shadow",
                "reason": capital_allocation.get("recommendation"),
                "targets": capital_allocation.get("next_slot_priority"),
                "policy_write": False,
            }
        )
    if ai_account.get("duplicate_candidates"):
        actions.append(
            {
                "action": "refresh_ai_account_universe",
                "account_id": "DEMO-AI",
                "reason": "DEMO-AI duplicates DEMO-US candidate/setup",
                "policy_write": False,
            }
        )
    elif ai_account.get("symbol_overlaps_with_DEMO-US"):
        actions.append(
            {
                "action": "review_ai_account_symbol_overlap",
                "account_id": "DEMO-AI",
                "reason": "DEMO-AI shares symbol with DEMO-US even though setup differs",
                "symbols": [item.get("symbol") for item in ai_account.get("symbol_overlaps_with_DEMO-US") or []],
                "policy_write": False,
            }
        )
    return actions


def _arena_growth_blocked_actions(
    top_candidates: list[dict[str, Any]],
    *,
    positive_learning: dict[str, Any],
    capital_allocation: dict[str, Any],
    ai_account: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for item in top_candidates:
        gates = item.get("gate_reasons") or []
        if not gates:
            continue
        actions.append(
            {
                "action": "live_execution",
                "account_id": item.get("account_id"),
                "symbol": item.get("symbol"),
                "blocked_by": gates,
                "hard_gates_unchanged": True,
                "next_step": item.get("next_step"),
            }
        )
    if not positive_learning.get("promotion_candidates"):
        actions.append(
            {
                "action": "positive_learning_promotion",
                "blocked_by": "no positive edge yet",
                "min_samples_for_promotion": positive_learning.get("min_samples_for_promotion"),
            }
        )
    if capital_allocation.get("recommendation") == "no risk expansion; research-only growth":
        actions.append({"action": "risk_expansion", "blocked_by": "negative_or_insufficient_realised_outcomes"})
    for item in ai_account.get("duplicate_candidates") or []:
        actions.append(
            {
                "action": "DEMO-AI_duplicate_candidate",
                "account_id": "DEMO-AI",
                "symbol": item.get("symbol"),
                "blocked_by": "duplicates_DEMO-US_same_symbol_setup",
            }
        )
    return actions


def _arena_growth_portfolio_state(scan: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    return {
        "scan_status": scan.get("status"),
        "review_status": review.get("status"),
        "accounts": [
            {
                "account_id": item.get("account_id"),
                "label": item.get("label"),
                "equity": item.get("equity"),
                "positions_count": item.get("positions_count") or len(item.get("positions") or []),
                "top_signal": item.get("top_signal"),
            }
            for item in scan.get("accounts") or []
            if isinstance(item, dict)
        ],
        "planned_actions_count": len(review.get("planned_actions") or []),
        "exit_proposals_count": len(review.get("exit_proposals") or []),
        "replacement_proposals_count": len(review.get("replacement_proposals") or []),
    }


def _arena_growth_learning_state(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": report.get("mode"),
        "objective": report.get("objective"),
        "decisions_count": report.get("decisions_count"),
        "outcomes_count": report.get("outcomes_count"),
        "closed_trades_count": report.get("closed_trades_count"),
        "win_rate_pct": report.get("win_rate_pct"),
        "net_pnl_rub": report.get("net_pnl_rub"),
        "equity_slope_rub_per_trade": report.get("equity_slope_rub_per_trade"),
        "top_gate_reasons": report.get("top_gate_reasons"),
        "growth_interpretation": "brake controls stay active; gas actions are research_queue, universe refresh, and allocation proposals",
    }


def _arena_growth_hold_cash_benchmark(auction: dict[str, Any]) -> dict[str, Any] | None:
    for item in auction.get("opportunities") or []:
        if isinstance(item, dict) and item.get("opportunity_type") == "hold_cash":
            return item
    winner = auction.get("winner")
    if isinstance(winner, dict) and winner.get("opportunity_type") == "hold_cash":
        return winner
    return None


def _arena_event_candidates_from_rss(raw: bytes, *, policy: dict[str, Any], now: datetime, limit: int) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    universe_accounts = _arena_universe_accounts(policy)
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in root.findall(".//item"):
        title = _xml_text(item, "title")
        description = _xml_text(item, "description")
        link = _xml_text(item, "link")
        published = _xml_text(item, "pubDate")
        haystack = f"{title} {description}".upper()
        for symbol, account_ids in universe_accounts.items():
            ticker = symbol.split("@", maxsplit=1)[0].upper()
            if ticker not in haystack:
                continue
            key = (symbol, link or title)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                {
                    "timestamp": now.isoformat(timespec="seconds"),
                    "source": "finam_rss",
                    "symbol": symbol,
                    "account_ids": account_ids,
                    "title": title,
                    "link": link,
                    "published_at": published,
                    "score": 65,
                    "decision": "event_candidate_unclassified",
                    "gap_pct": None,
                    "rr_after_gap": None,
                    "volume_confirmed": None,
                    "level_hold_confirmed": None,
                    "retest_confirmed": None,
                    "sonar_provider_call": False,
                }
            )
            if len(records) >= limit:
                return records
    return records


def _xml_text(item: Any, tag: str) -> str:
    found = item.find(tag)
    return str(found.text or "").strip() if found is not None else ""


def _arena_universe_accounts(policy: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for account in policy.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("account_id") or "")
        for symbol in account.get("universe") or []:
            normalized = str(symbol or "").upper()
            if normalized and account_id:
                result.setdefault(normalized, []).append(account_id)
    return result


def _read_recent_arena_event_candidates(path: Path, *, now: datetime) -> list[dict[str, Any]]:
    records = read_jsonl(path)
    cutoff = now - timedelta(days=1)
    result: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        timestamp = _research_parse_datetime(str(record.get("timestamp") or record.get("published_at") or ""))
        if timestamp is not None and timestamp < cutoff:
            continue
        result.append(record)
    return result


def _arena_event_candidates_path(*, env: Any = os.environ) -> Path:
    configured = str(env.get(ARENA_EVENT_CANDIDATES_ENV) or "").strip()
    return Path(configured) if configured else DEFAULT_ARENA_EVENT_CANDIDATES_PATH


def _arena_opportunity_ledger_path(*, env: Any = os.environ) -> Path:
    configured = str(env.get(ARENA_OPPORTUNITY_LEDGER_ENV) or "").strip()
    return Path(configured) if configured else DEFAULT_ARENA_OPPORTUNITY_LEDGER_PATH


def _arena_opportunity_ledger_record(
    auction: dict[str, Any],
    *,
    scan: dict[str, Any],
    review: dict[str, Any],
    event_path: Path | None,
    now: datetime,
) -> dict[str, Any]:
    digest_payload = {
        "timestamp": now.isoformat(timespec="seconds"),
        "scan_status": scan.get("status"),
        "review_status": review.get("status"),
        "winner": auction.get("winner"),
    }
    decision_id = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return {
        "record_type": "arena_opportunity_shadow_decision",
        "decision_id": decision_id,
        "decision_timestamp": now.isoformat(timespec="seconds"),
        "score_model": auction.get("score_model"),
        "scan_ref": {"status": scan.get("status"), "candidate_count": len(scan.get("candidates") or [])},
        "review_ref": {"status": review.get("status"), "planned_actions_count": len(review.get("planned_actions") or [])},
        "event_ref": {"path": str(event_path) if event_path is not None else str(_arena_event_candidates_path())},
        "current_contour_action": (auction.get("shadow_decision") or {}).get("current_contour_action"),
        "auction_winner": (auction.get("shadow_decision") or {}).get("auction_winner"),
        "rejected": auction.get("rejected") or [],
        "outcome_checkpoints": [],
        "lookahead_guard": "inputs frozen at decision_timestamp; checkpoints append future observations only",
    }


def _arena_opportunity_calibration_summary(path: Path, *, now: datetime) -> dict[str, Any]:
    records = [
        item
        for item in read_jsonl(path)
        if isinstance(item, dict) and item.get("record_type") == "arena_opportunity_shadow_decision"
    ]
    cutoff = now - timedelta(days=7)
    recent = []
    for item in records:
        timestamp = _research_parse_datetime(str(item.get("decision_timestamp") or ""))
        if timestamp is not None and timestamp >= cutoff:
            recent.append(item)
    counts: dict[str, int] = {}
    hold_cash_count = 0
    for item in recent:
        winner = item.get("auction_winner") if isinstance(item.get("auction_winner"), dict) else {}
        winner_type = str(winner.get("opportunity_type") or "unknown")
        counts[winner_type] = counts.get(winner_type, 0) + 1
        if winner_type == "hold_cash":
            hold_cash_count += 1
    return {
        "period_days": 7,
        "records": len(recent),
        "winner_counts": counts,
        "hold_cash_count": hold_cash_count,
        "live_use_allowed": False,
        "next_review": "manual calibration after 1-2 weeks of shadow records",
    }


def _arena_list_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _arena_compact_position(position: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "side",
        "quantity",
        "average_price",
        "current_price",
        "protective_stop_side",
    )
    return {key: position.get(key) for key in keys if position.get(key) is not None}


def _arena_compact_stop(stop: dict[str, Any]) -> dict[str, Any]:
    keys = ("symbol", "side", "quantity", "stop_price", "status", "mode")
    return {key: stop.get(key) for key in keys if stop.get(key) is not None}


def _arena_compact_trade(trade: dict[str, Any]) -> dict[str, Any]:
    keys = ("symbol", "side", "quantity", "price", "time", "trade_id")
    return {key: trade.get(key) for key in keys if trade.get(key) is not None}


def _arena_compact_account(account: dict[str, Any]) -> dict[str, Any]:
    positions = [item for item in _arena_list_value(account.get("positions")) if isinstance(item, dict)]
    stops = [
        item
        for item in _arena_list_value(account.get("stop_orders") or account.get("active_stop_orders"))
        if isinstance(item, dict)
    ]
    trades = [item for item in _arena_list_value(account.get("recent_trades")) if isinstance(item, dict)]
    compact: dict[str, Any] = {
        "account_id": account.get("account_id"),
        "label": account.get("label"),
        "strategy": account.get("strategy"),
        "trade_mode": account.get("trade_mode"),
        "paused": account.get("paused"),
        "status": account.get("status"),
        "equity": account.get("equity"),
        "cash": account.get("cash") or account.get("available_cash"),
        "pnl_rub": account.get("pnl_rub"),
        "pnl_pct": account.get("pnl_pct"),
        "positions_count": account.get("positions_count", len(positions)),
        "positions": [_arena_compact_position(item) for item in positions],
        "stop_orders": [_arena_compact_stop(item) for item in stops],
        "recent_trades": [_arena_compact_trade(item) for item in trades[:5]],
        "open_risk_pct": account.get("open_risk_pct"),
        "halt": account.get("halt"),
        "halt_reasons": account.get("halt_reasons") or [],
        "top_signal": account.get("top_signal"),
        "daily_operations_count": account.get("daily_operations_count"),
        "daily_buy_count": account.get("daily_buy_count"),
    }
    raw_entry_limits = account.get("entry_limits")
    entry_limits = raw_entry_limits if isinstance(raw_entry_limits, dict) else {}
    if entry_limits:
        compact["entry_limits"] = {
            "primary_used": entry_limits.get("primary_used", 0),
            "primary_limit": entry_limits.get("primary_limit", 0),
            "replacement_used": entry_limits.get("replacement_used", 0),
            "replacement_limit": entry_limits.get("replacement_limit", 0),
        }
    return {key: value for key, value in compact.items() if value not in (None, [], {})}


def _arena_compact_status_output(
    report: dict[str, Any],
    *,
    policy: dict[str, Any],
    policy_path: Path,
    max_bytes: int = 8000,
) -> dict[str, Any]:
    accounts = [item for item in _arena_list_value(report.get("accounts")) if isinstance(item, dict)]
    pending = [item for item in _arena_list_value(report.get("pending_approvals")) if isinstance(item, dict)]
    compact_pending = _compact_for_llm({"items": pending}, max_list_items=5).get("items", [])
    output = {
        "command": "arena-status",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy_path": str(policy_path),
        "status": report.get("status"),
        "mode": report.get("mode"),
        "emergency_stop": report.get("emergency_stop"),
        "base_url": report.get("base_url"),
        "approval_until": report.get("approval_until"),
        "accounts": [_arena_compact_account(item) for item in accounts],
        "configured_accounts": _arena_account_ids(policy),
        "pending_approvals": compact_pending,
        "pending_approvals_count": len(pending),
        "execution_route": _arena_execution_route_guidance(policy, compact_pending),
        "live_gate": _arena_live_gate_status(policy),
        "errors": report.get("errors") or [],
        "warnings": report.get("warnings") or [],
        "safety": _safety_payload(),
        "omitted_fields": ["telegram_pulse", "telegram_reply_markup", "recent_trades", "raw_broker_payloads"],
        "full_detail_commands": [
            "python scripts/hermes_operator.py arena-status --full-json",
            "python scripts/hermes_operator.py arena-view --callback arena:account:ACCOUNT_ID",
        ],
        "max_bytes": max_bytes,
        "full_json": False,
    }
    encoded = json.dumps(output, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    output["json_bytes"] = len(encoded)
    if len(encoded) <= max_bytes:
        return output
    for account in output["accounts"]:
        if isinstance(account, dict):
            account.pop("entry_limits", None)
            account.pop("top_signal", None)
    encoded = json.dumps(output, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    output["json_bytes"] = len(encoded)
    return output


def _compact_for_llm(value: Any, *, max_list_items: int, max_string_chars: int = 500) -> Any:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered.startswith("telegram_") or lowered in {"telegram", "reply_markup"}:
                continue
            if any(marker in lowered for marker in ("html", "raw", "blob")):
                continue
            compact[key_text] = _compact_for_llm(
                item,
                max_list_items=max_list_items,
                max_string_chars=max_string_chars,
            )
        return compact
    if isinstance(value, list):
        items = [
            _compact_for_llm(item, max_list_items=max_list_items, max_string_chars=max_string_chars)
            for item in value[:max_list_items]
        ]
        if len(value) > max_list_items:
            items.append({"truncated_items": len(value) - max_list_items})
        return items
    if isinstance(value, str) and len(value) > max_string_chars:
        return value[:max_string_chars] + f"... [truncated {len(value) - max_string_chars} chars]"
    return value


def _arena_llm_context_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    max_bytes: int = 8000,
    max_list_items: int = 5,
    include_mcp_shadow: bool = False,
) -> dict[str, Any]:
    max_bytes = max(1000, int(max_bytes))
    status = build_arena_status(policy, soft_stops=_arena_soft_stop_records())
    compact = _arena_compact_status_output(status, policy=policy, policy_path=policy_path, max_bytes=max_bytes)
    compact["command"] = "arena-llm-context"
    compact["purpose"] = "compact_context_for_llm"
    compact["usage"] = "Use full detail commands only when this summary is insufficient."
    compact["learning_summary"] = _arena_learning_summary(policy)
    if include_mcp_shadow:
        ledger = _read_arena_execution_ledger(env=os.environ)
        scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(), research_mode="cache_only", execution_ledger=ledger)
        compact["mcp_shadow"] = build_mcp_shadow_review(
            policy,
            arena_scan=scan,
            env=os.environ,
            max_bytes=max(1000, max_bytes // 2),
        )
    compact["max_list_items"] = max(1, int(max_list_items))
    encoded = json.dumps(compact, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    compact["json_bytes"] = len(encoded)
    if include_mcp_shadow and len(encoded) > max_bytes and isinstance(compact.get("mcp_shadow"), dict):
        compact["mcp_shadow"] = _compact_for_llm(compact["mcp_shadow"], max_list_items=max(1, int(max_list_items)))
        encoded = json.dumps(compact, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
        compact["json_bytes"] = len(encoded)
    return compact


def _arena_live_gate_status(policy: dict[str, Any], *, env: Any = os.environ) -> dict[str, Any]:
    required_env = str(policy.get("auto_trade_env") or "FINAM_ARENA_AUTO_TRADE_ENABLED")
    raw_value = str(env.get(required_env, "")).strip()
    enabled = raw_value.lower() == "true"
    status = {
        "required_env": required_env,
        "visible_to_hermes": bool(raw_value),
        "enabled": enabled,
    }
    if not enabled:
        status["next_action"] = f"Set {required_env}=true in the Hermes runtime environment before live arena-run."
    return status



def _arena_parse_confirmation_phrase(confirmation: str) -> dict[str, str]:
    value = str(confirmation or "").strip()
    match = re.fullmatch(r"CONFIRM_ARENA_SELL_PARTIAL\s+(\S+)\s+([0-9]+(?:\.[0-9]+)?)\s+(\S+)", value)
    if match:
        symbol, quantity, account_id = match.groups()
        return {
            "kind": "SELL_PARTIAL",
            "side": "SELL",
            "symbol": symbol.upper(),
            "quantity": quantity,
            "account_id": account_id,
        }
    match = re.fullmatch(r"CONFIRM_ARENA_(BUY|SELL)\s+(\S+)\s+(\S+)", value)
    if match:
        side, symbol, account_id = match.groups()
        return {"kind": side, "side": side, "symbol": symbol.upper(), "account_id": account_id}
    match = re.fullmatch(r"CONFIRM_ARENA_EXIT\s+(\S+)\s+(\S+)", value)
    if match:
        symbol, account_id = match.groups()
        return {"kind": "EXIT", "side": "EXIT", "symbol": symbol.upper(), "account_id": account_id}
    match = re.fullmatch(r"CONFIRM_ARENA_REPLACE\s+(\S+)\s*->\s*(\S+)\s+(\S+)", value)
    if match:
        sell_symbol, buy_symbol, account_id = match.groups()
        return {
            "kind": "REPLACE",
            "side": "REPLACE",
            "symbol": buy_symbol.upper(),
            "sell_symbol": sell_symbol.upper(),
            "buy_symbol": buy_symbol.upper(),
            "account_id": account_id,
        }
    match = re.fullmatch(r"CONFIRM_ARENA_BUY_OVERRIDE\s+(\S+)\s+(\S+)", value)
    if match:
        symbol, account_id = match.groups()
        return {"kind": "BUY_OVERRIDE", "side": "BUY", "symbol": symbol.upper(), "account_id": account_id}
    match = re.fullmatch(r"CONFIRM_ARENA_RECOVER\s+(\S+)\s+(\S+)", value)
    if match:
        symbol, account_id = match.groups()
        return {"kind": "RECOVER", "side": "RECOVER", "symbol": symbol.upper(), "account_id": account_id}
    return {}


ARENA_RUN_OVERRIDE_ALLOWED_GATE_REASONS = {
    "research_risk_requires_manual_review",
    "candidate_score_below_min",
}


def _arena_parse_run_override_confirmation_phrase(confirmation: str) -> dict[str, str]:
    match = re.fullmatch(r"CONFIRM_ARENA_BUY_OVERRIDE\s+(\S+)\s+(\S+)", confirmation.strip())
    if not match:
        return {}
    symbol, account_id = match.groups()
    return {"side": "BUY", "symbol": symbol.upper(), "account_id": account_id}


def _arena_run_override_status(trade_proposal: dict[str, Any], confirmation: str = "") -> dict[str, Any]:
    gates = trade_proposal.get("gates") if isinstance(trade_proposal.get("gates"), dict) else {}
    gate_reasons = [str(item) for item in gates.get("gate_reasons") or []]
    gate_reason_set = set(gate_reasons)
    side = str(trade_proposal.get("side") or "").upper()
    symbol = str(trade_proposal.get("symbol") or "").upper()
    account_id = str(trade_proposal.get("account_id") or "")
    override_confirmation = f"CONFIRM_ARENA_BUY_OVERRIDE {symbol} {account_id}" if symbol and account_id else ""
    override_allowed = (
        side == "BUY"
        and bool(symbol)
        and bool(account_id)
        and gates.get("execution_allowed") is not True
        and bool(gate_reason_set)
        and gate_reason_set.issubset(ARENA_RUN_OVERRIDE_ALLOWED_GATE_REASONS)
    )
    parsed = _arena_parse_run_override_confirmation_phrase(confirmation)
    confirmation_matches = (
        override_allowed
        and parsed.get("side") == "BUY"
        and parsed.get("symbol") == symbol
        and parsed.get("account_id") == account_id
    )
    return {
        "override_allowed": override_allowed,
        "override_confirmation_phrase": override_confirmation if override_allowed else "",
        "override_next_live_command": (
            f'python scripts/hermes_operator.py arena-run --account {account_id} --live --confirmation "{override_confirmation}"'
            if override_allowed
            else ""
        ),
        "override_gate_reasons": gate_reasons if override_allowed else [],
        "confirmation_matches": confirmation_matches,
        "parsed_override_confirmation": parsed,
    }


def _arena_execution_route_guidance(
    policy: dict[str, Any],
    pending_approvals: list[dict[str, Any]],
    *,
    env: Any = os.environ,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    active_confirmations = [
        str(item.get("confirmation") or "").strip()
        for item in pending_approvals
        if isinstance(item, dict) and str(item.get("confirmation") or "").strip()
    ]
    accounts = policy.get("accounts") if isinstance(policy.get("accounts"), list) else []
    confirmation_required_accounts = [
        str(account.get("account_id"))
        for account in accounts
        if isinstance(account, dict)
        and account.get("account_id")
        and _arena_confirmation_required(policy, account, now=current, env=env)
    ]
    route = "approval_snapshot_required" if confirmation_required_accounts else "auto_direct"
    return {
        "route": route,
        "arena_confirm_requires_pending_snapshot": True,
        "pending_approvals_count": len(active_confirmations),
        "active_confirmation_phrases": active_confirmations[:5],
        "confirmation_required_accounts": confirmation_required_accounts,
        "auto_direct_command_template": "python scripts/hermes_operator.py arena-run --account ACCOUNT_ID --live --confirmation \"CONFIRM_ARENA_BUY SYMBOL@MIC ACCOUNT_ID\"",
        "approval_snapshot_rule": "Use arena-confirm only when the exact confirmation phrase is present in active_confirmation_phrases.",
    }


def _arena_proposal_execution_route(
    policy: dict[str, Any],
    account: dict[str, Any] | None,
    proposal: dict[str, Any],
    *,
    env: Any = os.environ,
    now: datetime | None = None,
) -> dict[str, Any]:
    account_data = account if isinstance(account, dict) else {}
    confirmation = str(proposal.get("execution", {}).get("confirmation_phrase") or "").strip()
    account_id = str(proposal.get("account_id") or account_data.get("account_id") or "").strip()
    route = _arena_execution_route_guidance(policy, [], env=env, now=now)
    route_name = "approval_snapshot_required" if account_id in route.get("confirmation_required_accounts", []) else "auto_direct"
    confirmation_kind = "pending_snapshot_confirmation" if route_name == "approval_snapshot_required" else "live_authorization"
    next_live_command = ""
    if confirmation and account_id:
        next_live_command = f'python scripts/hermes_operator.py arena-run --account {account_id} --live --confirmation "{confirmation}"'
    return {
        "route": route_name,
        "confirmation_kind": confirmation_kind,
        "confirmation_phrase": confirmation,
        "arena_confirm_allowed": False,
        "arena_confirm_rule": "Use arena-confirm only when this exact phrase is present in active pending approvals.",
        "next_live_command": next_live_command,
        "pending_snapshot_required": route_name == "approval_snapshot_required",
    }



def _arena_confirm_missing_next_actions(
    policy: dict[str, Any],
    *,
    pending_path: Path,
    confirmation: str,
    missing_reason: str | None,
    now: datetime,
    env: Any = os.environ,
) -> dict[str, Any]:
    active_pending = _arena_pending_approval_summaries(path=pending_path, now=now)
    route = _arena_execution_route_guidance(policy, active_pending, env=env, now=now)
    parsed = _arena_parse_confirmation_phrase(confirmation)
    next_actions: list[dict[str, Any]] = [
        {
            "route": "approval_snapshot",
            "when": "Use only if the exact confirmation is listed in active_pending_approvals.",
            "command": 'python scripts/hermes_operator.py arena-confirm --live --confirmation "CONFIRM_ARENA_..."',
        },
        {
            "route": "approval_snapshot_create",
            "when": "If manual approval workflow is desired, create a fresh approval alert/snapshot before retrying arena-confirm.",
            "command": "python scripts/arena_executor_notify.py --once --live --execute-live",
        },
    ]
    parsed_next_action = _arena_confirmation_missing_primary_next_action(parsed, confirmation)
    if parsed_next_action:
        next_actions.insert(0, parsed_next_action)
    elif parsed:
        next_actions.insert(
            0,
            {
                "route": "auto_direct",
                "when": "If the account is auto/autonomous and the user explicitly authorized this exact phrase, use arena-run with live revalidation instead of arena-confirm.",
                "command": f'python scripts/hermes_operator.py arena-run --account {parsed["account_id"]} --live --confirmation "{confirmation}"',
            },
        )
    return {
        "message": "No active pending approval snapshot matches this confirmation; arena-confirm never substitutes a fresh rescan.",
        "missing_reason": missing_reason,
        "parsed_confirmation": parsed,
        "execution_route": route,
        "active_pending_approvals": active_pending,
        "next_actions": next_actions,
    }


def _arena_confirmation_missing_primary_next_action(parsed: dict[str, str], confirmation: str) -> dict[str, str] | None:
    kind = str(parsed.get("kind") or parsed.get("side") or "").upper()
    account_id = str(parsed.get("account_id") or "")
    symbol = str(parsed.get("symbol") or "")
    if kind in {"BUY", "SELL"} and account_id:
        return {
            "route": "auto_direct",
            "when": "If the account is auto/autonomous and the user explicitly authorized this exact phrase, use arena-run with live revalidation instead of arena-confirm.",
            "command": f'python scripts/hermes_operator.py arena-run --account {account_id} --live --confirmation "{confirmation}"',
        }
    if kind == "BUY_OVERRIDE" and account_id:
        return {
            "route": "auto_direct_override",
            "when": "Use only for current BUY proposals blocked exclusively by overrideable research/score gates.",
            "command": f'python scripts/hermes_operator.py arena-run --account {account_id} --live --confirmation "{confirmation}"',
        }
    if kind == "RECOVER" and account_id and symbol:
        return {
            "route": "protection_recovery",
            "when": "Use for soft-stop protection recovery; it is not a pending approval snapshot route.",
            "command": f'python scripts/hermes_operator.py arena-recover-protection --account {account_id} --symbol {symbol} --live --confirmation "{confirmation}"',
        }
    if kind in {"EXIT", "REPLACE"}:
        return {
            "route": "approval_snapshot_create",
            "when": "Create a fresh executor approval alert/snapshot, then retry arena-confirm with the same exact phrase.",
            "command": "python scripts/arena_executor_notify.py --once --live --execute-live",
        }
    return None



def _arena_attribution_output(policy: dict[str, Any], *, policy_path: Path) -> dict[str, Any]:
    status = build_arena_status(policy, soft_stops=_arena_soft_stop_records())
    ledger = _read_arena_execution_ledger(env=os.environ)
    attribution = build_arena_attribution(policy, status, execution_ledger=ledger)
    return attribution | {
        "policy_path": str(policy_path),
        "ledger_path": str(_arena_execution_ledger_path(os.environ)),
        "telegram_text": h4_monitor_notify.format_arena_attribution(attribution),
        "telegram_reply_markup": h4_monitor_notify.arena_reply_markup(),
        "safety": _safety_payload(),
    }


def _arena_pattern_report_output(policy: dict[str, Any], *, policy_path: Path, account_id: str = "") -> dict[str, Any]:
    ledger = _read_arena_execution_ledger(env=os.environ)
    if account_id:
        ledger = [item for item in ledger if isinstance(item, dict) and str(item.get("account_id") or "") == account_id]
    report = build_repeat_pattern_loss_report(policy, ledger)
    return report | {
        "policy_path": str(policy_path),
        "ledger_path": str(_arena_execution_ledger_path(os.environ)),
        "account_filter": account_id or None,
        "safety": _safety_payload(),
    }


def _arena_attribution_backfill_output(policy: dict[str, Any], *, policy_path: Path, since: str, dry_run: bool, env: Any = os.environ) -> dict[str, Any]:
    try:
        datetime.fromisoformat(f"{since}T00:00:00+00:00")
    except ValueError:
        return {
            "status": "FAILED",
            "command": "arena-attribution-backfill",
            "policy_path": str(policy_path),
            "reason": "invalid_since_date",
            "since": since,
            "safety": _safety_payload(),
        }
    journal = _arena_executor_journal_text(since=since)
    records, warnings = _arena_execution_ledger_records_from_journal(journal, policy=policy)
    ledger_path = _arena_execution_ledger_path(env)
    written = 0
    skipped = 0
    if not dry_run:
        existing = _arena_execution_ledger_keys(_read_arena_execution_ledger(env=env))
        for record in records:
            key = _arena_execution_ledger_key(record)
            if key in existing:
                skipped += 1
                continue
            _append_jsonl(ledger_path, record)
            existing.add(key)
            written += 1
    return {
        "status": "DRY_RUN" if dry_run else "OK",
        "command": "arena-attribution-backfill",
        "policy_path": str(policy_path),
        "ledger_path": str(ledger_path),
        "since": since,
        "records_found": len(records),
        "records_written": written,
        "records_skipped": skipped,
        "warnings": warnings,
        "safety": _safety_payload(),
    }


def _arena_execution_ledger_path(env: Any = os.environ) -> Path:
    configured = str(env.get(ARENA_EXECUTION_LEDGER_ENV) or "").strip()
    return Path(configured) if configured else DEFAULT_ARENA_EXECUTION_LEDGER_PATH


def _read_arena_execution_ledger(*, env: Any = os.environ) -> list[dict[str, Any]]:
    path = _arena_execution_ledger_path(env)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [{"source": "arena_execution_ledger_error", "error": str(exc), "path": str(path)}]
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            records.append(
                {
                    "source": "arena_execution_ledger_error",
                    "error": "json_decode",
                    "path": str(path),
                    "line": line_number,
                }
            )
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _arena_execution_ledger_key(record: dict[str, Any]) -> str:
    order_id = str(record.get("order_id") or "")
    if order_id:
        return f"order:{order_id}"
    return "|".join(
        str(record.get(key) or "")
        for key in ("timestamp", "account_id", "symbol", "side", "quantity", "price", "action")
    )


def _arena_execution_ledger_keys(records: list[dict[str, Any]]) -> set[str]:
    return {_arena_execution_ledger_key(record) for record in records if isinstance(record, dict)}


def _record_arena_execution_ledger(output: dict[str, Any], *, policy: dict[str, Any], env: Any) -> None:
    record, warning = _arena_execution_ledger_record_from_run(output, policy=policy)
    if warning:
        output["execution_ledger_warning"] = warning
    if record is None:
        return
    output["execution_ledger_record"] = record
    ledger_path = _arena_execution_ledger_path(env)
    existing = _arena_execution_ledger_keys(_read_arena_execution_ledger(env=env))
    if _arena_execution_ledger_key(record) in existing:
        output["execution_ledger_delivery"] = "deduped"
        return
    _append_jsonl(ledger_path, record)
    output["execution_ledger_delivery"] = "ok"


def _arena_execution_ledger_record_from_run(run: dict[str, Any], *, policy: dict[str, Any], timestamp: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    status = str(run.get("status") or "")
    if status not in ARENA_EXECUTED_STATUSES:
        return None, None
    account_id = str(run.get("account_id") or "")
    if status == "EXECUTED_ARENA_REPLACEMENT":
        buy_result = run.get("buy_result") if isinstance(run.get("buy_result"), dict) else {}
        fill_state = _run_execution_fill_state(buy_result)
        response = _run_execution_response(buy_result)
        return {
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "account_id": account_id,
            "symbol": _run_proposal_symbol(buy_result),
            "side": "REPLACE",
            "source": "arena_replacement_allowance",
            "order_id": str(fill_state.get("order_id") or response.get("order_id") or ""),
            "status": status,
            "action": run.get("action"),
        }, None
    symbol = str(run.get("symbol") or _run_proposal_symbol(run) or "").upper()
    side = _run_execution_side(run)
    candidate = _run_proposal_candidate(run)
    fill_state = _run_execution_fill_state(run)
    payload = _run_execution_payload(run)
    response = _run_execution_response(run)
    quantity = _decimal(fill_state.get("executed_quantity")) or _decimal(_first_nested(payload, "quantity")) or _decimal(_first_nested(payload, "quantity_sl"))
    price = (
        _decimal(fill_state.get("execution_price"))
        or _decimal(_first_nested(response, "execution_price"))
        or _decimal(_first_nested(response.get("order") if isinstance(response.get("order"), dict) else {}, "execution_price"))
        or _run_proposal_price(run)
    )
    if not account_id or not symbol or not side:
        return None, "missing_account_symbol_or_side"
    if quantity is None or quantity <= 0:
        return None, "missing_executed_quantity"
    if price is None or price <= 0:
        return None, "missing_execution_price"
    notional = abs(quantity) * price
    commission_pct = _arena_commission_pct_for_symbol(policy, symbol)
    return {
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "quantity": decimal_payload(abs(quantity), min_scale=1),
        "price": decimal_payload(price),
        "notional": decimal_payload(notional),
        "commission_pct": decimal_payload(commission_pct),
        "estimated_commission": decimal_payload(notional * commission_pct / Decimal("100")),
        "source": "arena_execution",
        "order_id": str(fill_state.get("order_id") or response.get("order_id") or _first_nested(response.get("order") if isinstance(response.get("order"), dict) else {}, "order_id") or ""),
        "status": status,
        "action": run.get("action"),
        "setup_signature": candidate.get("setup_signature"),
        "regime_timeframe": candidate.get("regime_timeframe"),
        "entry_timeframe": candidate.get("entry_timeframe"),
        "entry_reason": candidate.get("entry_reason"),
        "signal": candidate.get("signal"),
        "arena_growth_score": candidate.get("arena_growth_score"),
        "research_verdict": candidate.get("research_verdict")
        or ((candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}).get("research_verdict")),
        "proposal_snapshot": _arena_compact_proposal_snapshot(run),
    }, None


def _arena_compact_proposal_snapshot(run: dict[str, Any]) -> dict[str, Any]:
    proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
    candidate = proposal_output.get("candidate") if isinstance(proposal_output.get("candidate"), dict) else {}
    gates = proposal.get("gates") if isinstance(proposal.get("gates"), dict) else {}
    risk = proposal.get("risk") if isinstance(proposal.get("risk"), dict) else {}
    entry = proposal.get("entry") if isinstance(proposal.get("entry"), dict) else {}
    stop = proposal.get("protective_stop") if isinstance(proposal.get("protective_stop"), dict) else {}
    take_profit = proposal.get("take_profit") if isinstance(proposal.get("take_profit"), dict) else {}
    execution = proposal.get("execution") if isinstance(proposal.get("execution"), dict) else {}
    score = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    pretrade = candidate.get("pretrade_check") if isinstance(candidate.get("pretrade_check"), dict) else {}
    snapshot: dict[str, Any] = {
        "account_id": proposal.get("account_id") or run.get("account_id"),
        "symbol": proposal.get("symbol") or candidate.get("symbol") or run.get("symbol"),
        "side": proposal.get("side") or candidate.get("side"),
        "quantity": proposal.get("quantity") or candidate.get("quantity"),
        "entry": {key: entry.get(key) for key in ("limit_price", "basis") if entry.get(key) is not None},
        "protective_stop": {key: stop.get(key) for key in ("side", "stop_price", "basis") if stop.get(key) is not None},
        "take_profit": {key: take_profit.get(key) for key in ("price", "r_multiple") if take_profit.get(key) is not None},
        "risk": {
            key: risk.get(key)
            for key in ("notional", "risk_rub", "risk_pct_equity", "risk_per_share", "max_risk_pct")
            if risk.get(key) is not None
        },
        "gates": {
            "execution_allowed": gates.get("execution_allowed"),
            "gate_reasons": gates.get("gate_reasons") or [],
        },
        "execution": {
            key: execution.get(key)
            for key in ("route", "confirmation_kind", "confirmation_phrase")
            if execution.get(key) is not None
        },
        "arena_growth_score": score,
        "research_verdict": candidate.get("research_verdict") or score.get("research_verdict"),
        "research_source": _arena_compact_research_source(candidate),
        "pretrade_check": pretrade,
        "setup_signature": candidate.get("setup_signature"),
        "regime_timeframe": candidate.get("regime_timeframe"),
        "entry_timeframe": candidate.get("entry_timeframe"),
        "entry_reason": candidate.get("entry_reason"),
        "signal": candidate.get("signal"),
    }
    return {key: value for key, value in snapshot.items() if value not in ({}, [], None, "")}


def _arena_compact_research_source(candidate: dict[str, Any]) -> str:
    pretrade = candidate.get("pretrade_check") if isinstance(candidate.get("pretrade_check"), dict) else {}
    if pretrade:
        if pretrade.get("provider_call") is True:
            return "perplexity_sonar"
        reason = str(pretrade.get("reason") or "")
        status = str(pretrade.get("status") or "")
        if reason == "openrouter_daily_budget_exhausted":
            return "unavailable_budget_exhausted"
        if status == "ok":
            return "pretrade_cache_or_fallback"
        return "unavailable"
    score = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    if candidate.get("research_verdict") or score.get("research_verdict"):
        return "h4_research_or_cache"
    return "unavailable"


def _run_execution_fill_state(run: dict[str, Any]) -> dict[str, Any]:
    for key in ("exit_fill_state", "entry_fill_state"):
        value = run.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _run_execution_payload(run: dict[str, Any]) -> dict[str, Any]:
    for key in ("exit_order_payload", "entry_order_payload"):
        value = run.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _run_execution_response(run: dict[str, Any]) -> dict[str, Any]:
    for key in ("exit_order_response", "entry_order_response"):
        value = run.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _run_execution_side(run: dict[str, Any]) -> str:
    payload = _run_execution_payload(run)
    side = str(payload.get("side") or "").upper()
    if "SELL" in side:
        return "SELL"
    if "BUY" in side:
        return "BUY"
    proposal = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    proposal_payload = proposal.get("proposal") if isinstance(proposal.get("proposal"), dict) else {}
    side = str(proposal_payload.get("side") or "").upper()
    return "SELL" if "SELL" in side else ("BUY" if "BUY" in side else "")


def _run_proposal_symbol(run: dict[str, Any]) -> str:
    proposal = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    proposal_payload = proposal.get("proposal") if isinstance(proposal.get("proposal"), dict) else {}
    candidate = _run_proposal_candidate(run)
    return str(proposal_payload.get("symbol") or candidate.get("symbol") or "")


def _run_proposal_price(run: dict[str, Any]) -> Decimal | None:
    proposal = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    proposal_payload = proposal.get("proposal") if isinstance(proposal.get("proposal"), dict) else {}
    candidate = _run_proposal_candidate(run)
    entry = proposal_payload.get("entry") if isinstance(proposal_payload.get("entry"), dict) else {}
    return _decimal(entry.get("limit_price")) or _decimal(candidate.get("entry_price"))


def _run_proposal_candidate(run: dict[str, Any]) -> dict[str, Any]:
    proposal = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    return proposal.get("candidate") if isinstance(proposal.get("candidate"), dict) else {}


def _arena_commission_pct_for_symbol(policy: dict[str, Any], symbol: str) -> Decimal:
    fees = policy.get("fees") if isinstance(policy.get("fees"), dict) else {}
    mic = str(symbol.rsplit("@", maxsplit=1)[-1] if "@" in symbol else "").upper()
    by_mic = fees.get("commission_pct_by_mic") if isinstance(fees.get("commission_pct_by_mic"), dict) else {}
    return _decimal(by_mic.get(mic)) or _decimal(fees.get("commission_pct_per_side")) or Decimal("0")


def _arena_executor_journal_text(*, since: str) -> str:
    result = subprocess.run(
        ["journalctl", "--user", "-u", "finam-arena-executor.service", "--since", since, "--output=json", "--no-pager"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _arena_execution_ledger_records_from_journal(text: str, *, policy: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    for payload, timestamp in _arena_executor_outputs_from_journal(text):
        run_all = payload.get("run_all") if isinstance(payload.get("run_all"), dict) else payload
        for run in _iter_arena_execution_runs(run_all):
            record, warning = _arena_execution_ledger_record_from_run(run, policy=policy, timestamp=timestamp)
            if record is not None:
                records.append(record)
            elif warning:
                warnings.append(f"{run.get('account_id') or 'n/a'} {run.get('symbol') or _run_proposal_symbol(run) or 'UNKNOWN'}: {warning}")
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        deduped[_arena_execution_ledger_key(record)] = record
    return list(deduped.values()), warnings


def _iter_arena_execution_runs(run_all: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for run in run_all.get("runs") or []:
        if not isinstance(run, dict):
            continue
        if run.get("command") == "arena-portfolio-run" and isinstance(run.get("results"), list):
            result.extend([item for item in run["results"] if isinstance(item, dict)])
        else:
            result.append(run)
    return result


def _arena_executor_outputs_from_journal(text: str) -> list[tuple[dict[str, Any], str]]:
    outputs: list[tuple[dict[str, Any], str]] = []
    buffer: list[str] = []
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = False
    for raw_line in text.splitlines():
        message_timestamp, message = _journal_message(raw_line)
        stripped = message.strip()
        if not started and not stripped.startswith("{"):
            continue
        if stripped.startswith("{") and not started:
            buffer = []
            timestamp = message_timestamp
            started = True
        if not started:
            continue
        buffer.append(message)
        candidate = "\n".join(buffer)
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (value.get("command") == "arena-executor-notify" or value.get("command") == "arena-run-all" or isinstance(value.get("run_all"), dict)):
            outputs.append((value, timestamp))
        buffer = []
        started = False
    return outputs


def _journal_message(line: str) -> tuple[str, str]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if "]: " in line:
            return timestamp, line.split("]: ", maxsplit=1)[1]
        return timestamp, line
    message = str(payload.get("MESSAGE") or "")
    realtime = str(payload.get("__REALTIME_TIMESTAMP") or "")
    if realtime.isdigit():
        timestamp = datetime.fromtimestamp(int(realtime) / 1_000_000, tz=timezone.utc).isoformat(timespec="seconds")
    else:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    transport = str(payload.get("_TRANSPORT") or "")
    identifier = str(payload.get("SYSLOG_IDENTIFIER") or "")
    if (transport or identifier) and transport != "stdout" and identifier != "python":
        return timestamp, ""
    return timestamp, message


def _arena_portfolio_run_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    live: bool,
    env: Any = os.environ,
    client: Any | None = None,
    market_client: Any | None = None,
    research_mode: str = "cache_only",
    scan: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    stop_check = _arena_check_stops_output(
        policy,
        policy_path=policy_path,
        live=live,
        client=client,
        env=env,
        execute_triggered_stops=live,
    )
    if stop_check.get("status") not in {"NO_STOPS", "OK"}:
        stop_check_mutation = _arena_stop_check_trading_mutations(stop_check)
        return {
            "status": "HALT",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "reason": "arena_stop_check_not_clear",
            "stop_check": stop_check,
            "live_requested": live,
            "broker_mutation": stop_check_mutation,
            "safety": _safety_payload(trading_mutations=stop_check_mutation),
        }
    if scan is None:
        ledger = _read_arena_execution_ledger(env=env)
        scan = build_arena_scan(
            policy,
            soft_stops=_arena_soft_stop_records(env=env),
            market_client=market_client,
            env=env,
            research_mode=research_mode,
            execution_ledger=ledger,
        )
    review = build_arena_portfolio_review(policy, scan)
    actions = [item for item in review.get("planned_actions") or [] if isinstance(item, dict)]
    if not actions:
        return {
            "status": "NO_ACTION",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "review": review,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    if not live:
        return {
            "status": "DRY_RUN",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "planned_actions": actions,
            "review": review,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    auto_trade_env = str(policy.get("auto_trade_env") or "FINAM_ARENA_AUTO_TRADE_ENABLED")
    if str(env.get(auto_trade_env, "")).strip().lower() != "true":
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "reason": f"{auto_trade_env}_not_true",
            "required_env": auto_trade_env,
            "planned_actions": actions,
            "safety": _safety_payload(),
        }
    blocking_state = _blocking_trade_safety_state(env=env)
    if blocking_state is not None:
        return {
            "status": "HALT",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "reason": "unresolved_trade_safety_state",
            "safety_state": blocking_state,
            "planned_actions": actions,
            "safety": _safety_payload(),
        }
    current = now or datetime.now(timezone.utc)
    for action in actions:
        account_id = str(action.get("account_id") or "")
        account_policy = _arena_policy_account(policy, account_id) or {}
        auto_route = _arena_portfolio_auto_execution_route(policy, account_policy, action, env=env, now=current)
        if not auto_route["allowed"]:
            continue
        market_session_block = _arena_portfolio_action_market_session_block(action, now=current)
        if market_session_block is None:
            continue
        result = {
            "status": "WAIT_MARKET_CLOSED",
            "command": str(action.get("command") or "arena-portfolio-run"),
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": str(
                market_session_block.get("symbol")
                or action.get("symbol")
                or action.get("sell_symbol")
                or ""
            ).upper(),
            "action": action.get("action"),
            "reason": market_session_block["reason"],
            "market_session": market_session_block,
            "halt_new_entries": True,
            "broker_mutation": False,
            "safety": _safety_payload(),
            "auto_execution": auto_route,
        }
        return {
            "status": _arena_portfolio_run_status([result]),
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "planned_actions": actions,
            "results": [result],
            "review": review,
            "live_requested": live,
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    secret = str(env.get(secret_env) or "").strip()
    if not secret:
        return {
            "status": "NO_TRADE",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "reason": f"{secret_env}_not_set",
            "planned_actions": actions,
            "safety": _safety_payload(),
        }
    client = client or FinamClient(base_url=arena_base_url(policy, env=env))
    try:
        jwt = client.create_session(secret)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "BROKER_ERROR",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "reason": "arena_session_create_failed",
            "error": str(exc),
            "planned_actions": actions,
            "safety": _safety_payload(),
        }
    results: list[dict[str, Any]] = []
    review_by_account = {str(item.get("account_id") or ""): item for item in review.get("accounts") or [] if isinstance(item, dict)}
    scan_accounts = {str(item.get("account_id") or ""): item for item in scan.get("accounts") or [] if isinstance(item, dict)}
    for action in actions:
        account_id = str(action.get("account_id") or "")
        account_policy = _arena_policy_account(policy, account_id) or {}
        auto_route = _arena_portfolio_auto_execution_route(policy, account_policy, action, env=env)
        if auto_route["allowed"] and str(action.get("action") or "").upper() == "REPLACE":
            result = _execute_confirmed_arena_replacement(
                policy,
                policy_path=policy_path,
                action=action,
                account_review=review_by_account.get(account_id, {}),
                confirmation="",
                client=client,
                jwt=jwt,
                env=env,
                market_client=market_client,
            )
            result["auto_execution"] = auto_route
            results.append(result)
            if str(result.get("status") or "") in ARENA_UNRESOLVED_EXECUTION_STATUSES or result.get("halt_new_entries"):
                break
            continue
        result = _execute_arena_portfolio_action(
            policy,
            policy_path=policy_path,
            action=action,
            account_review=review_by_account.get(account_id, {}),
            scan_account=scan_accounts.get(account_id, {}),
            client=client,
            jwt=jwt,
            env=env,
            confirmed=bool(auto_route["allowed"]),
        )
        result["auto_execution"] = auto_route
        results.append(result)
        if str(result.get("status") or "") == "CONFIRMATION_REQUIRED":
            break
        if str(result.get("status") or "") in ARENA_UNRESOLVED_EXECUTION_STATUSES or result.get("halt_new_entries"):
            break
    return {
        "status": _arena_portfolio_run_status(results),
        "command": "arena-portfolio-run",
        "policy_path": str(policy_path),
        "planned_actions": actions,
        "results": results,
        "review": review,
        "live_requested": live,
        "broker_mutation": any(bool(item.get("broker_mutation")) for item in results),
        "safety": _safety_payload(trading_mutations=any(bool((item.get("safety") or {}).get("trading_mutations")) for item in results)),
    }


def _arena_portfolio_run_status(results: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in results}
    if not results:
        return "NO_ACTION"
    if "WAIT_MARKET_CLOSED" in statuses:
        return "WAIT_MARKET_CLOSED"
    if "CONFIRMATION_REQUIRED" in statuses:
        return "CONFIRMATION_REQUIRED"
    if statuses & ARENA_UNRESOLVED_EXECUTION_STATUSES:
        return "HALT"
    if "BROKER_ERROR" in statuses:
        return "HALT"
    if any(bool(item.get("broker_mutation")) for item in results):
        return "EXECUTED_ARENA_PORTFOLIO"
    if "SOFT_STOP_UPDATED" in statuses:
        return "SOFT_STOP_UPDATED"
    return "NO_ACTION"


def _arena_portfolio_action_market_session_block(action: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    action_type = str(action.get("action") or "").upper()
    symbol = str(action.get("symbol") or "").upper()
    if action_type == "REPLACE":
        symbol = str(action.get("sell_symbol") or symbol).upper()
    if not symbol:
        return None
    return _arena_market_session_block({"proposal": {"symbol": symbol}}, now=now)


def _arena_portfolio_auto_execution_route(
    policy: dict[str, Any],
    account: dict[str, Any],
    action: dict[str, Any],
    *,
    env: Any = os.environ,
    now: datetime | None = None,
) -> dict[str, Any]:
    action_type = str(action.get("action") or "").upper()
    account_id = str(action.get("account_id") or account.get("account_id") or "")
    portfolio = policy.get("portfolio") if isinstance(policy.get("portfolio"), dict) else {}
    autonomy_mode = str(portfolio.get("autonomy_mode") or "review_only")
    exit_actions = {"TAKE_PARTIAL_PROFIT", "TRIM_OVEREXPOSURE", "EXIT_WEAK"}
    if policy.get("mode") != "autonomous":
        reason = "policy_mode_not_autonomous"
    elif _arena_confirmation_required(policy, account, now=now, env=env):
        reason = "account_requires_confirmation"
    elif action_type in exit_actions and autonomy_mode in {"exits_auto", "full_rotation"}:
        reason = ""
    elif action_type == "REPLACE" and autonomy_mode == "full_rotation":
        reason = ""
    else:
        reason = "portfolio_autonomy_mode_requires_confirmation"
    return {
        "allowed": reason == "",
        "reason": reason or "portfolio_auto_execution_allowed",
        "account_id": account_id,
        "action": action_type,
        "autonomy_mode": autonomy_mode,
    }


def _execute_arena_portfolio_action(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    action: dict[str, Any],
    account_review: dict[str, Any],
    scan_account: dict[str, Any],
    client: Any,
    jwt: str,
    env: Any,
    confirmed: bool = False,
) -> dict[str, Any]:
    action_type = str(action.get("action") or "")
    account_id = str(action.get("account_id") or "")
    if action_type in {"BREAKEVEN_STOP", "TRAIL_STOP"}:
        return _arena_update_soft_stop_action(policy, policy_path=policy_path, action=action, account_review=account_review, env=env)
    if action_type in {"TAKE_PARTIAL_PROFIT", "TRIM_OVEREXPOSURE", "EXIT_WEAK"} and not confirmed:
        return _arena_portfolio_confirmation_required_result(
            action,
            account_review=account_review,
            reason="arena_exit_requires_supervised_confirmation",
        )
    if action_type in {"TAKE_PARTIAL_PROFIT", "TRIM_OVEREXPOSURE", "EXIT_WEAK"}:
        return _arena_execute_position_exit_action(
            policy,
            policy_path=policy_path,
            action=action,
            client=client,
            jwt=jwt,
            env=env,
        )
    if action_type == "REPLACE":
        replacement = account_review.get("replacement") if isinstance(account_review.get("replacement"), dict) else {}
        sell_symbol = str(replacement.get("sell_symbol") or action.get("sell_symbol") or "").upper()
        buy_candidate = replacement.get("buy_candidate") if isinstance(replacement.get("buy_candidate"), dict) else {}
        buy_symbol = str(buy_candidate.get("symbol") or action.get("buy_symbol") or "").upper()
        if not confirmed:
            return _arena_portfolio_confirmation_required_result(
                action | {"sell_symbol": sell_symbol, "buy_symbol": buy_symbol},
                account_review=account_review,
                reason="arena_replace_requires_supervised_confirmation",
            )
        return {
            "status": "APPROVAL_REVALIDATION_FAILED",
            "command": "arena-portfolio-run",
            "account_id": account_id,
            "action": "REPLACE",
            "reason": "arena_replace_execution_not_implemented",
            "replacement": {"sell_symbol": sell_symbol or None, "buy_symbol": buy_symbol or None},
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    return {
        "status": "NO_ACTION",
        "command": "arena-portfolio-run",
        "account_id": account_id,
        "action": action_type,
        "reason": "unsupported_portfolio_action",
        "safety": _safety_payload(),
    }


def _arena_portfolio_confirmation_required_result(
    action: dict[str, Any],
    *,
    account_review: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    action_type = str(action.get("action") or "").upper()
    account_id = str(action.get("account_id") or "")
    if action_type == "REPLACE":
        replacement = account_review.get("replacement") if isinstance(account_review.get("replacement"), dict) else {}
        sell_symbol = str(replacement.get("sell_symbol") or action.get("sell_symbol") or "").upper()
        buy_candidate = replacement.get("buy_candidate") if isinstance(replacement.get("buy_candidate"), dict) else {}
        buy_symbol = str(buy_candidate.get("symbol") or action.get("buy_symbol") or "").upper()
        confirmation = str(replacement.get("confirmation_phrase") or f"CONFIRM_ARENA_REPLACE {sell_symbol} -> {buy_symbol} {account_id}").strip()
        return {
            "status": "CONFIRMATION_REQUIRED",
            "command": "arena-portfolio-run",
            "account_id": account_id,
            "action": "REPLACE",
            "reason": reason,
            "required_confirmation": confirmation,
            "replacement": {
                "sell_symbol": sell_symbol or None,
                "buy_symbol": buy_symbol or None,
                "score_delta": replacement.get("score_delta"),
            },
            "halt_new_entries": False,
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    symbol = str(action.get("symbol") or "").upper()
    return {
        "status": "CONFIRMATION_REQUIRED",
        "command": "arena-portfolio-run",
        "account_id": account_id,
        "symbol": symbol,
        "action": action_type,
        "quantity": action.get("quantity"),
        "reason": reason,
        "required_confirmation": f"CONFIRM_ARENA_EXIT {symbol} {account_id}".strip(),
        "halt_new_entries": False,
        "broker_mutation": False,
        "safety": _safety_payload(),
    }


def _arena_proposal_from_candidate(candidate: dict[str, Any], *, account: dict[str, Any]) -> dict[str, Any]:
    proposal = arena_trade_proposal(candidate, account=account)
    return {
        "status": "PROPOSE_ONLY",
        "command": "arena-proposal",
        "account_id": proposal["account_id"],
        "account": account,
        "candidate": candidate,
        "proposal": proposal,
        "order_payloads": arena_order_payloads(proposal),
        "safety": _safety_payload(),
    }


def _arena_position_sell_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
    symbol: str,
    quantity: Any,
    live: bool,
    confirmation: str = "",
    env: Any = os.environ,
    client: Any | None = None,
) -> dict[str, Any]:
    account_id = str(account_id or "").strip()
    symbol = str(symbol or "").strip().upper()
    requested_quantity = _decimal(quantity)
    required_confirmation = (
        f"CONFIRM_ARENA_SELL_PARTIAL {symbol} {decimal_payload(requested_quantity) if requested_quantity is not None else quantity} {account_id}".strip()
    )
    base = {
        "command": "arena-position-sell",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "symbol": symbol,
        "requested_quantity": decimal_payload(requested_quantity) if requested_quantity is not None else str(quantity),
        "required_confirmation": required_confirmation,
        "live_requested": live,
    }
    if not account_id or not symbol:
        return base | {
            "status": "INVALID",
            "reason": "account_or_symbol_missing",
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    if requested_quantity is None or requested_quantity <= 0:
        return base | {
            "status": "INVALID",
            "reason": "quantity_must_be_positive",
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    account_policy = _arena_policy_account(policy, account_id)
    if account_policy is None:
        return base | {
            "status": "BLOCKED",
            "reason": "arena_account_not_configured",
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    if account_policy.get("paused") is True:
        return base | {
            "status": "BLOCKED",
            "reason": "arena_account_paused",
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    if live:
        parsed = _arena_parse_confirmation_phrase(confirmation)
        parsed_quantity = _decimal(parsed.get("quantity")) if parsed else None
        if (
            parsed.get("kind") != "SELL_PARTIAL"
            or parsed.get("account_id") != account_id
            or parsed.get("symbol") != symbol
            or parsed_quantity != requested_quantity
        ):
            return base | {
                "status": "CONFIRMATION_REQUIRED",
                "reason": "arena_position_sell_confirmation_required",
                "received_confirmation": confirmation,
                "parsed_confirmation": parsed,
                "broker_mutation": False,
                "safety": _safety_payload(),
            }
        auto_trade_env = str(policy.get("auto_trade_env") or "FINAM_ARENA_AUTO_TRADE_ENABLED")
        if str(env.get(auto_trade_env, "")).strip().lower() != "true":
            return base | {
                "status": "LIVE_GATE_REQUIRED",
                "reason": f"{auto_trade_env}_not_true",
                "required_env": auto_trade_env,
                "broker_mutation": False,
                "safety": _safety_payload(),
            }
        blocking_state = _blocking_trade_safety_state(env=env)
        if blocking_state is not None:
            return base | {
                "status": "HALT",
                "reason": "unresolved_trade_safety_state",
                "safety_state": blocking_state,
                "broker_mutation": False,
                "safety": _safety_payload(),
            }
        stop_check = _arena_check_stops_output(
            policy,
            policy_path=policy_path,
            live=live,
            client=client,
            env=env,
            execute_triggered_stops=live,
        )
        if stop_check.get("status") not in {"NO_STOPS", "OK"}:
            stop_check_mutation = _arena_stop_check_trading_mutations(stop_check)
            return base | {
                "status": "HALT",
                "reason": "arena_stop_check_not_clear",
                "stop_check": stop_check,
                "broker_mutation": stop_check_mutation,
                "safety": _safety_payload(trading_mutations=stop_check_mutation),
            }
    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    secret = str(env.get(secret_env) or "").strip()
    if not secret:
        return base | {
            "status": "NO_TRADE",
            "reason": f"{secret_env}_not_set",
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    client = client or FinamClient(base_url=arena_base_url(policy, env=env))
    try:
        jwt = client.create_session(secret)
    except Exception as exc:  # noqa: BLE001
        return base | {
            "status": "BROKER_ERROR",
            "reason": "arena_session_create_failed",
            "error": str(exc),
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    if not live:
        try:
            account = client.get_account(jwt, account_id)
        except Exception as exc:  # noqa: BLE001
            return base | {
                "status": "BROKER_ERROR",
                "reason": "arena_position_sell_account_read_failed",
                "error": str(exc),
                "broker_mutation": False,
                "safety": _safety_payload(),
            }
        position = _arena_position(account, symbol)
        position_quantity = _arena_position_quantity(account, symbol)
        blockers: list[str] = []
        if position is None:
            blockers.append("no_live_position")
        elif _arena_expected_stop_side(position) != "SELL":
            blockers.append("manual_partial_sell_only_long_positions")
        if requested_quantity > position_quantity:
            blockers.append("requested_quantity_exceeds_position")
        return base | {
            "status": "DRY_RUN" if not blockers else "BLOCKED",
            "reason": blockers[0] if blockers else "planned_manual_position_sell",
            "blockers": blockers,
            "position_quantity": decimal_payload(position_quantity, min_scale=1),
            "planned_action": {
                "action": "MANUAL_PARTIAL_SELL",
                "symbol": symbol,
                "quantity": decimal_payload(requested_quantity, min_scale=1),
                "side": "SELL",
            },
            "broker_mutation": False,
            "safety": _safety_payload(),
        }
    result = _arena_execute_position_exit_action(
        policy,
        policy_path=policy_path,
        action={
            "account_id": account_id,
            "action": "MANUAL_PARTIAL_SELL",
            "symbol": symbol,
            "quantity": decimal_payload(requested_quantity),
            "command": "arena-position-sell",
        },
        client=client,
        jwt=jwt,
        env=env,
    )
    result["command"] = "arena-position-sell"
    result["required_confirmation"] = required_confirmation
    result["received_confirmation"] = confirmation
    result["requested_quantity"] = decimal_payload(requested_quantity)
    result["live_requested"] = live
    if result.get("status") == "EXECUTED_ARENA_EXIT_RESIDUAL_STOP_TRIGGERED":
        result["status"] = "EXECUTED_ARENA_POSITION_SELL_RESIDUAL_STOP_TRIGGERED"
    elif result.get("status") in ARENA_EXECUTED_STATUSES:
        result["status"] = "EXECUTED_ARENA_POSITION_SELL"
        result["halt_new_entries"] = True
    return result


def _arena_execute_position_exit_action(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    action: dict[str, Any],
    client: Any,
    jwt: str,
    env: Any,
) -> dict[str, Any]:
    account_id = str(action.get("account_id") or "")
    symbol = str(action.get("symbol") or "").upper()
    command = str(action.get("command") or "arena-portfolio-run")
    try:
        account = client.get_account(jwt, account_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "BROKER_ERROR",
            "command": command,
            "account_id": account_id,
            "symbol": symbol,
            "action": action.get("action"),
            "reason": "arena_exit_account_read_failed",
            "error": str(exc),
            "safety": _safety_payload(),
        }
    position = _arena_position(account, symbol)
    if position is None:
        _remove_arena_soft_stop(account_id=account_id, symbol=symbol, env=env)
        return {
            "status": "NO_POSITION",
            "command": command,
            "account_id": account_id,
            "symbol": symbol,
            "action": action.get("action"),
            "soft_stop_removed": True,
            "safety": _safety_payload(),
        }
    position_quantity = _arena_position_quantity(account, symbol)
    quantity = _arena_exit_quantity_for_action(action, account=account, position=position, policy=policy)
    if str(action.get("action") or "") == "MANUAL_PARTIAL_SELL":
        if _arena_expected_stop_side(position) != "SELL":
            return {
                "status": "NO_TRADE",
                "command": command,
                "policy_path": str(policy_path),
                "account_id": account_id,
                "symbol": symbol,
                "action": action.get("action"),
                "reason": "manual_partial_sell_only_long_positions",
                "position_quantity": decimal_payload(position_quantity, min_scale=1),
                "requested_quantity": decimal_payload(quantity, min_scale=1),
                "broker_mutation": False,
                "safety": _safety_payload(),
            }
        if quantity > position_quantity:
            return {
                "status": "NO_TRADE",
                "command": command,
                "policy_path": str(policy_path),
                "account_id": account_id,
                "symbol": symbol,
                "action": action.get("action"),
                "reason": "requested_quantity_exceeds_position",
                "position_quantity": decimal_payload(position_quantity, min_scale=1),
                "requested_quantity": decimal_payload(quantity, min_scale=1),
                "broker_mutation": False,
                "safety": _safety_payload(),
            }
    if quantity <= 0:
        return {
            "status": "NO_ACTION",
            "command": command,
            "account_id": account_id,
            "symbol": symbol,
            "action": action.get("action"),
            "reason": "exit_quantity_zero",
            "safety": _safety_payload(),
        }
    stop_side = _arena_expected_stop_side(position)
    broker_side = "SIDE_BUY" if stop_side == "BUY" else "SIDE_SELL"
    payload = {"symbol": symbol, "side": broker_side, "quantity": {"value": decimal_payload(quantity, min_scale=1)}}
    try:
        response = client.place_order(jwt, account_id, payload)
    except Exception as exc:  # noqa: BLE001
        output = {
            "status": "BROKER_ERROR",
            "command": command,
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": symbol,
            "action": action.get("action"),
            "reason": "arena_portfolio_exit_submit_failed",
            "error": str(exc),
            "exit_order_payload": payload,
            "halt_new_entries": True,
            "safety": _safety_payload(trading_mutations=True),
        }
        _write_trade_safety_state(output, env=env)
        return output
    proposal = {"symbol": symbol, "side": "SELL" if broker_side == "SIDE_SELL" else "BUY", "quantity": quantity}
    fill_state = _wait_for_buy_execution(client, jwt, account_id, response, proposal, env=env)
    if fill_state.get("status") not in {"filled", "partial"}:
        output = {
            "status": "BROKER_ERROR" if fill_state.get("status") == "error" else "ENTRY_PENDING_NO_STOP",
            "command": command,
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": symbol,
            "action": action.get("action"),
            "reason": "arena_portfolio_exit_fill_unverified",
            "exit_order_payload": payload,
            "exit_order_response": response,
            "exit_fill_state": fill_state,
            "halt_new_entries": True,
            "safety": _safety_payload(trading_mutations=True),
        }
        _write_trade_safety_state(output, env=env)
        return output
    soft_stop_update = _update_soft_stop_after_exit(account_id=account_id, symbol=symbol, exited_quantity=quantity, position=position, env=env)
    output = {
        "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
        "command": command,
        "policy_path": str(policy_path),
        "account_id": account_id,
        "symbol": symbol,
        "action": action.get("action"),
        "exit_order_payload": payload,
        "exit_order_response": response,
        "exit_fill_state": fill_state,
        "soft_stop_update": soft_stop_update,
        "broker_mutation": True,
        "safety": _safety_payload(trading_mutations=True),
    }
    if soft_stop_update.get("status") == "RESIDUAL_STOP_TRIGGERED":
        output.update(
            {
                "status": "EXECUTED_ARENA_EXIT_RESIDUAL_STOP_TRIGGERED",
                "reason": "arena_residual_soft_stop_triggered_after_exit",
                "halt_new_entries": True,
                "residual_stop": soft_stop_update.get("soft_stop"),
            }
        )
        _write_trade_safety_state(output, env=env)
    _record_arena_execution_ledger(output, policy=policy, env=env)
    return output


def _arena_exit_quantity_for_action(action: dict[str, Any], *, account: dict[str, Any], position: dict[str, Any], policy: dict[str, Any]) -> Decimal:
    quantity = _arena_position_quantity(account, str(action.get("symbol") or ""))
    action_type = str(action.get("action") or "")
    if action_type == "MANUAL_PARTIAL_SELL":
        return _decimal(action.get("quantity")) or Decimal("0")
    portfolio = policy.get("portfolio") if isinstance(policy.get("portfolio"), dict) else {}
    if action_type == "TAKE_PARTIAL_PROFIT":
        fraction = _decimal(portfolio.get("take_partial_fraction_pct")) or Decimal("50")
        return max(Decimal("1"), (quantity * fraction / Decimal("100")).to_integral_value(rounding=ROUND_FLOOR))
    if action_type == "TRIM_OVEREXPOSURE":
        fraction = _decimal(portfolio.get("deleveraging_step_pct")) or Decimal("25")
        return max(Decimal("1"), (quantity * fraction / Decimal("100")).to_integral_value(rounding=ROUND_FLOOR))
    return quantity


def _update_soft_stop_after_exit(
    *,
    account_id: str,
    symbol: str,
    exited_quantity: Decimal,
    position: dict[str, Any] | None = None,
    env: Any,
) -> dict[str, Any]:
    existing = next(
        (
            item
            for item in _arena_soft_stop_records(env=env)
            if str(item.get("account_id") or "") == account_id and str(item.get("symbol") or "").upper() == symbol.upper()
        ),
        None,
    )
    if not existing:
        return {"status": "NO_SOFT_STOP_RECORD", "account_id": account_id, "symbol": symbol.upper()}
    current_qty = _decimal(existing.get("quantity")) or Decimal("0")
    remaining = current_qty - exited_quantity
    if remaining <= 0:
        _remove_arena_soft_stop(account_id=account_id, symbol=symbol, env=env)
        return {
            "status": "SOFT_STOP_REMOVED",
            "account_id": account_id,
            "symbol": symbol.upper(),
            "exited_quantity": decimal_payload(exited_quantity, min_scale=1),
            "previous_quantity": decimal_payload(current_qty, min_scale=1),
        }
    updated = dict(existing)
    updated["quantity"] = decimal_payload(remaining, min_scale=1)
    updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _upsert_arena_soft_stop(updated, env=env)
    result = {
        "status": "SOFT_STOP_QUANTITY_UPDATED",
        "account_id": account_id,
        "symbol": symbol.upper(),
        "exited_quantity": decimal_payload(exited_quantity, min_scale=1),
        "previous_quantity": decimal_payload(current_qty, min_scale=1),
        "remaining_quantity": decimal_payload(remaining, min_scale=1),
        "soft_stop": updated,
    }
    if position is not None and _arena_stop_triggered(updated, position):
        result.update(
            {
                "status": "RESIDUAL_STOP_TRIGGERED",
                "reason": "arena_residual_soft_stop_triggered_after_exit",
                "current_price": str(
                    _first_nested(position, "current_price")
                    or _first_nested(position, "last_price")
                    or _first_nested(position, "market_price")
                    or _first_nested(position, "price")
                    or ""
                )
                or None,
            }
        )
    return result


def _arena_update_soft_stop_action(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    action: dict[str, Any],
    account_review: dict[str, Any],
    env: Any,
) -> dict[str, Any]:
    account_id = str(action.get("account_id") or "")
    symbol = str(action.get("symbol") or "").upper()
    record = next(
        (
            item
            for item in _arena_soft_stop_records(env=env)
            if str(item.get("account_id") or "") == account_id and str(item.get("symbol") or "").upper() == symbol
        ),
        None,
    )
    if record is None:
        return {
            "status": "HALT",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": symbol,
            "action": action.get("action"),
            "reason": "soft_stop_record_missing",
            "safety": _safety_payload(),
        }
    position = next((item for item in account_review.get("positions") or [] if str(item.get("symbol") or "").upper() == symbol), {})
    average = _decimal(position.get("average_price"))
    current = _decimal(position.get("current_price"))
    stop = _decimal(position.get("stop_price"))
    breakeven_stop = _arena_commission_adjusted_breakeven_stop(policy, symbol=symbol, position=position, average=average)
    new_stop = breakeven_stop if action.get("action") == "BREAKEVEN_STOP" else None
    if action.get("action") == "TRAIL_STOP" and average is not None and current is not None:
        trail_stop = current - ((current - average) / Decimal("2"))
        new_stop = max(breakeven_stop or average, trail_stop)
    if new_stop is None or (stop is not None and new_stop <= stop):
        return {
            "status": "NO_ACTION",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": symbol,
            "action": action.get("action"),
            "reason": "soft_stop_update_not_improving",
            "safety": _safety_payload(),
        }
    updated = dict(record)
    updated["stop_price"] = decimal_payload(new_stop)
    updated["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _upsert_arena_soft_stop(updated, env=env)
    return {
        "status": "SOFT_STOP_UPDATED",
        "command": "arena-portfolio-run",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "symbol": symbol,
        "action": action.get("action"),
        "old_stop_price": record.get("stop_price"),
        "new_stop_price": updated["stop_price"],
        "breakeven_basis": "entry_plus_round_trip_cost",
        "broker_mutation": False,
        "safety": _safety_payload(),
    }


def _arena_commission_adjusted_breakeven_stop(
    policy: dict[str, Any],
    *,
    symbol: str,
    position: dict[str, Any],
    average: Decimal | None,
) -> Decimal | None:
    if average is None or average <= 0:
        return None
    fees = policy.get("fees") if isinstance(policy.get("fees"), dict) else {}
    slippage_pct = _decimal(fees.get("slippage_pct_per_side")) or Decimal("0")
    round_trip_pct = (_arena_commission_pct_for_symbol(policy, symbol) + slippage_pct) * Decimal("2")
    side = str(position.get("side") or "LONG").upper()
    if side == "SHORT":
        return average * (Decimal("1") - round_trip_pct / Decimal("100"))
    return average * (Decimal("1") + round_trip_pct / Decimal("100"))


def _arena_assets_output(
    *,
    query: str,
    mics: list[str],
    limit: int,
    client: Any | None = None,
    env: Any = os.environ,
) -> dict[str, Any]:
    normalized_query = query.strip().upper()
    normalized_mics = {item.strip().upper() for item in mics if item.strip()}
    if not normalized_query:
        return {
            "status": "INVALID",
            "command": "arena-assets",
            "reason": "query_required",
            "safety": _safety_payload(),
        }
    secret = str(env.get("FINAM_TOKEN") or "").strip()
    if not secret:
        return {
            "status": "NO_DATA",
            "command": "arena-assets",
            "reason": "FINAM_TOKEN_not_set",
            "query": normalized_query,
            "mics": sorted(normalized_mics),
            "safety": _safety_payload(),
        }
    client = client or FinamClient()
    try:
        jwt = client.create_session(secret)
        response = client.assets(jwt)
    except Exception as exc:  # noqa: BLE001
        matches = _asset_probe_matches(client, jwt if "jwt" in locals() else None, query=normalized_query, mics=normalized_mics, limit=limit)
        if matches:
            return {
                "status": "OK",
                "command": "arena-assets",
                "query": normalized_query,
                "mics": sorted(normalized_mics),
                "matches": matches,
                "matches_count": len(matches),
                "source": "market_data_probe",
                "warning": f"assets_lookup_failed: {exc}",
                "hint": "Use symbol exactly as returned, e.g. SYMBOL@MIC, in Arena universe.",
                "safety": _safety_payload(),
            }
        return {
            "status": "NO_DATA",
            "command": "arena-assets",
            "reason": "finam_assets_lookup_failed",
            "error": str(exc),
            "query": normalized_query,
            "mics": sorted(normalized_mics),
            "safety": _safety_payload(),
        }

    matches = _asset_matches(response, query=normalized_query, mics=normalized_mics, limit=limit)
    return {
        "status": "OK" if matches else "NO_MATCH",
        "command": "arena-assets",
        "query": normalized_query,
        "mics": sorted(normalized_mics),
        "matches": matches,
        "matches_count": len(matches),
        "hint": "Use symbol exactly as returned, e.g. SYMBOL@MIC, in Arena universe.",
        "safety": _safety_payload(),
    }


def _arena_view_output(policy: dict[str, Any], *, policy_path: Path, callback: str) -> dict[str, Any]:
    if callback == "arena:strategy":
        text = h4_monitor_notify.format_arena_strategy_view(policy)
        return {
            "status": "OK",
            "command": "arena-view",
            "policy_path": str(policy_path),
            "callback": callback,
            "telegram_text": text,
            "telegram_reply_markup": h4_monitor_notify.arena_strategy_reply_markup(policy),
            "safety": _safety_payload(),
        }
    if callback.startswith("arena:st:"):
        proposal = _arena_strategy_callback_proposal(policy, policy_path=policy_path, callback=callback)
        text = h4_monitor_notify.format_arena_strategy_proposal(proposal)
        return {
            "status": proposal.get("status"),
            "command": "arena-view",
            "policy_path": str(policy_path),
            "callback": callback,
            "telegram_text": text,
            "telegram_reply_markup": h4_monitor_notify.arena_strategy_reply_markup(policy),
            "strategy_proposal": proposal,
            "safety": _safety_payload(),
        }

    if callback in {"arena:overview", "arena:risks", "arena:attribution"} or callback.startswith("arena:account:"):
        if callback.startswith("arena:account:"):
            account_id = callback.rsplit(":", maxsplit=1)[-1]
            if account_id not in _arena_account_ids(policy):
                return {
                    "status": "INVALID",
                    "command": "arena-view",
                    "policy_path": str(policy_path),
                    "callback": callback,
                    "reason": "account_not_allowed",
                    "allowed_accounts": _arena_account_ids(policy),
                    "safety": _safety_payload(),
                }
        snapshot_state = _read_arena_telegram_snapshot(policy_path=policy_path)
        if not snapshot_state.get("ok"):
            return _arena_stale_snapshot_view(policy_path=policy_path, callback=callback, snapshot_state=snapshot_state)
        snapshot = _arena_snapshot_with_fresh_pending_approvals(snapshot_state["snapshot"])
        if callback == "arena:overview":
            text = h4_monitor_notify.format_arena_pulse(snapshot)
        elif callback == "arena:risks":
            text = h4_monitor_notify.format_arena_risks_view(snapshot)
        elif callback == "arena:attribution":
            attribution = build_arena_attribution(policy, snapshot)
            text = h4_monitor_notify.format_arena_attribution(attribution)
        else:
            text = h4_monitor_notify.format_arena_account_detail(snapshot, callback.rsplit(":", maxsplit=1)[-1])
        return _arena_snapshot_view_response(
            snapshot,
            policy_path=policy_path,
            callback=callback,
            text=text,
            snapshot_state=snapshot_state,
        )

    if callback not in {"arena:rotation"}:
        return {
            "status": "INVALID",
            "command": "arena-view",
            "policy_path": str(policy_path),
            "callback": callback,
            "reason": "unknown_callback",
            "allowed_callbacks": [
                "arena:overview",
                "arena:account:DEMO-RU",
                "arena:account:DEMO-US",
                "arena:account:DEMO-AI",
                "arena:risks",
                "arena:strategy",
                "arena:rotation",
                "arena:attribution",
            ],
            "safety": _safety_payload(),
        }

    ledger = _read_arena_execution_ledger(env=os.environ)
    scan = build_arena_scan(
        policy,
        soft_stops=_arena_soft_stop_records(),
        research_mode="cache_only",
        execution_ledger=ledger,
    )
    review = build_arena_portfolio_review(policy, scan)
    text = h4_monitor_notify.format_arena_portfolio_review(review)
    return {
        "status": "OK",
        "command": "arena-view",
        "policy_path": str(policy_path),
        "callback": callback,
        "scan_status": scan.get("status"),
        "telegram_text": text,
        "telegram_reply_markup": h4_monitor_notify.arena_reply_markup(),
        "errors": scan.get("errors") or [],
        "warnings": scan.get("warnings") or [],
        "safety": _safety_payload(),
    }


def _arena_strategy_callback_proposal(policy: dict[str, Any], *, policy_path: Path, callback: str) -> dict[str, Any]:
    parts = callback.split(":")
    if len(parts) != 4:
        return {
            "status": "INVALID",
            "command": "arena-strategy-propose",
            "policy_path": str(policy_path),
            "account_id": None,
            "changes": [],
            "validation": {"errors": ["invalid_strategy_callback"], "warnings": []},
            "write_applied": False,
            "safety": _safety_payload(),
        }
    _, _, account_id, action = parts
    account = _arena_policy_account(policy, account_id)
    if account is None:
        return {
            "status": "INVALID",
            "command": "arena-strategy-propose",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "changes": [],
            "validation": {"errors": [f"account_not_allowed: {account_id}"], "warnings": []},
            "write_applied": False,
            "safety": _safety_payload(),
        }

    risk_multiplier: str | None = None
    pause = action == "pause"
    resume = action == "resume"
    trade_mode = action if action in {"manual", "auto"} else None
    if action in {"risk_up", "risk_down"}:
        current = _decimal(account.get("risk_multiplier")) or Decimal("1")
        delta = Decimal("0.25") if action == "risk_up" else Decimal("-0.25")
        proposed = current + delta
        if proposed < Decimal("0.25"):
            proposed = Decimal("0.25")
        if proposed > Decimal("2"):
            proposed = Decimal("2")
        risk_multiplier = format(proposed, "f")
    elif action not in {"pause", "resume", "manual", "auto"}:
        return {
            "status": "INVALID",
            "command": "arena-strategy-propose",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "changes": [],
            "validation": {"errors": [f"unknown_strategy_action: {action}"], "warnings": []},
            "write_applied": False,
            "safety": _safety_payload(),
        }

    return _arena_strategy_propose_output(
        policy,
        policy_path=policy_path,
        account_id=account_id,
        mode=None,
        risk_multiplier=risk_multiplier,
        pause=pause,
        resume=resume,
        trade_mode=trade_mode,
        add_universe=[],
        remove_universe=[],
    )


def _arena_proposal_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
    research_mode: str = "cache_only",
    scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validation = validate_arena_policy(policy)
    accounts = _arena_account_ids(policy)
    if validation["errors"]:
        return {
            "status": "INVALID",
            "command": "arena-proposal",
            "policy_path": str(policy_path),
            "validation": validation,
            "safety": _safety_payload(),
        }
    if account_id not in accounts:
        return {
            "status": "INVALID",
            "command": "arena-proposal",
            "reason": "account_not_allowed",
            "account_id": account_id,
            "allowed_accounts": accounts,
            "safety": _safety_payload(),
        }
    if scan is None:
        ledger = _read_arena_execution_ledger(env=os.environ)
        scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(), research_mode=research_mode, execution_ledger=ledger)
    account = _arena_account(scan, account_id)
    candidates = [
        item for item in scan.get("candidates") or []
        if isinstance(item, dict) and str(item.get("account_id")) == account_id
    ]
    if not candidates:
        return {
            "status": "NO_PROPOSAL",
            "command": "arena-proposal",
            "policy_path": str(policy_path),
            "account": account,
            "scan_status": scan.get("status"),
            "reason": "no_arena_h4_regime_h1_m30_candidate",
            "warnings": scan.get("warnings") or [],
            "errors": scan.get("errors") or [],
            "safety": _safety_payload(),
        }
    candidate = next((item for item in candidates if item.get("execution_allowed") is True), candidates[0])
    proposal = arena_trade_proposal(candidate, account=account or {"account_id": account_id})
    execution_route = _arena_proposal_execution_route(policy, account, proposal)
    proposal["execution"] = dict(proposal.get("execution") or {}) | {
        "route": execution_route["route"],
        "confirmation_kind": execution_route["confirmation_kind"],
        "next_live_command": execution_route["next_live_command"],
        "arena_confirm_allowed": execution_route["arena_confirm_allowed"],
    }
    override_status = _arena_run_override_status(proposal)
    if override_status["override_allowed"]:
        proposal["execution"] = dict(proposal.get("execution") or {}) | {
            "override_allowed": True,
            "override_confirmation_phrase": override_status["override_confirmation_phrase"],
            "override_next_live_command": override_status["override_next_live_command"],
            "override_gate_reasons": override_status["override_gate_reasons"],
        }
        execution_route = dict(execution_route) | {
            "override_allowed": True,
            "override_confirmation_phrase": override_status["override_confirmation_phrase"],
            "override_next_live_command": override_status["override_next_live_command"],
            "override_gate_reasons": override_status["override_gate_reasons"],
        }
    return {
        "status": "PROPOSE_ONLY",
        "command": "arena-proposal",
        "policy_path": str(policy_path),
        "account": account,
        "candidate": candidate,
        "proposal": proposal,
        "execution_route": execution_route,
        "order_payloads": arena_order_payloads(proposal),
        "broker_mutation": False,
        "warnings": scan.get("warnings") or [],
        "errors": scan.get("errors") or [],
        "safety": _safety_payload(),
    }


def _arena_pending_approvals_path(*, env: Any = os.environ) -> Path:
    configured = str(env.get(ARENA_PENDING_APPROVALS_ENV) or "").strip()
    return Path(configured) if configured else DEFAULT_ARENA_PENDING_APPROVALS_PATH


def _arena_approval_ttl_seconds(*, env: Any = os.environ) -> int:
    return _positive_int(env.get(ARENA_APPROVAL_TTL_ENV), default=DEFAULT_ARENA_APPROVAL_TTL_SECONDS)


def _arena_approval_max_price_drift_pct(*, env: Any = os.environ) -> Decimal:
    configured = _decimal(env.get(ARENA_APPROVAL_MAX_PRICE_DRIFT_ENV))
    return configured if configured is not None and configured >= 0 else DEFAULT_ARENA_APPROVAL_MAX_PRICE_DRIFT_PCT


@contextlib.contextmanager
def _json_state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def _arena_stop_execution_lock(*, env: Any = os.environ):
    path = _trade_safety_state_path(env=env)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".stop-exec.lock")
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _arena_price_revalidation_retries(*, env: Any = os.environ) -> int:
    return max(1, _positive_int(env.get(ARENA_PRICE_REVALIDATION_RETRIES_ENV), default=3))


def _arena_price_revalidation_sleep_seconds(*, env: Any = os.environ) -> float:
    configured = _decimal(env.get(ARENA_PRICE_REVALIDATION_SLEEP_ENV))
    if configured is None or configured < 0:
        return 0.5
    return float(configured)


def _read_arena_pending_approvals(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "approvals": []}
    except Exception:
        return {"version": 1, "approvals": []}
    if not isinstance(data, dict):
        return {"version": 1, "approvals": []}
    approvals = data.get("approvals")
    if not isinstance(approvals, list):
        data["approvals"] = []
    return data


def _write_arena_pending_approvals(path: Path, store: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    store = dict(store)
    store["version"] = 1
    store["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _arena_proposal_hash(proposal_output: dict[str, Any]) -> str:
    proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
    payload = {
        "account_id": proposal.get("account_id"),
        "symbol": proposal.get("symbol"),
        "side": proposal.get("side"),
        "quantity": proposal.get("quantity"),
        "entry": proposal.get("entry"),
        "protective_stop": proposal.get("protective_stop"),
        "take_profit": proposal.get("take_profit"),
        "gates": proposal.get("gates"),
        "order_payloads": proposal_output.get("order_payloads"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _arena_pending_approval_from_run(
    run: dict[str, Any],
    *,
    policy_path: Path,
    now: datetime,
    env: Any = os.environ,
) -> dict[str, Any] | None:
    if run.get("status") != "CONFIRMATION_REQUIRED":
        return None
    if str(run.get("command") or "") == "arena-portfolio-run" and str(run.get("action") or ""):
        return _arena_pending_portfolio_approval_from_run(run, policy_path=policy_path, now=now, env=env)
    proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
    confirmation = str(run.get("required_confirmation") or proposal.get("execution", {}).get("confirmation_phrase") or "").strip()
    account_id = str(run.get("account_id") or proposal.get("account_id") or "").strip()
    symbol = str(proposal.get("symbol") or "").strip().upper()
    side = str(proposal.get("side") or "").strip().upper()
    if not confirmation or not account_id or not symbol or side not in {"BUY", "SELL"}:
        return None
    expires_at = now + timedelta(seconds=_arena_approval_ttl_seconds(env=env))
    return {
        "status": "pending",
        "confirmation": confirmation,
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "quantity": proposal.get("quantity"),
        "entry_price": (proposal.get("entry") or {}).get("limit_price") if isinstance(proposal.get("entry"), dict) else None,
        "proposal_hash": _arena_proposal_hash(proposal_output),
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "policy_path": str(policy_path),
        "proposal_output": copy.deepcopy(proposal_output),
    }


def _arena_pending_portfolio_approval_from_run(
    run: dict[str, Any],
    *,
    policy_path: Path,
    now: datetime,
    env: Any = os.environ,
) -> dict[str, Any] | None:
    confirmation = str(run.get("required_confirmation") or "").strip()
    account_id = str(run.get("account_id") or "").strip()
    action = str(run.get("action") or "").strip().upper()
    if not confirmation or not account_id or action not in {"REPLACE", "EXIT_WEAK", "TRIM_OVEREXPOSURE", "TAKE_PARTIAL_PROFIT"}:
        return None
    replacement = run.get("replacement") if isinstance(run.get("replacement"), dict) else {}
    symbol = str(replacement.get("buy_symbol") or run.get("symbol") or "").strip().upper()
    side = "REPLACE" if action == "REPLACE" else "EXIT"
    if not symbol:
        return None
    expires_at = now + timedelta(seconds=_arena_approval_ttl_seconds(env=env))
    portfolio_action = copy.deepcopy(run)
    return {
        "status": "pending",
        "confirmation": confirmation,
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "action": action,
        "quantity": run.get("quantity"),
        "entry_price": None,
        "proposal_hash": _arena_proposal_hash({"portfolio_action": portfolio_action}),
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "policy_path": str(policy_path),
        "portfolio_action": portfolio_action,
    }


def _iter_arena_pending_approval_runs(run_all: dict[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for run in run_all.get("runs") or []:
        if not isinstance(run, dict):
            continue
        runs.append(run)
        if run.get("command") == "arena-portfolio-run" and isinstance(run.get("results"), list):
            runs.extend(item for item in run["results"] if isinstance(item, dict))
    return runs


def persist_arena_pending_approvals_from_run_all(
    run_all: dict[str, Any],
    *,
    policy_path: Path,
    pending_path: Path | None = None,
    now: datetime | None = None,
    env: Any = os.environ,
) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    snapshots = [
        snapshot
        for run in _iter_arena_pending_approval_runs(run_all)
        for snapshot in [_arena_pending_approval_from_run(run, policy_path=policy_path, now=current, env=env)]
        if snapshot is not None
    ]
    if not snapshots:
        return []
    path = pending_path or _arena_pending_approvals_path(env=env)
    with _json_state_lock(path):
        store = _read_arena_pending_approvals(path)
        approvals = [item for item in store.get("approvals") or [] if isinstance(item, dict)]
        confirmations = {snapshot["confirmation"] for snapshot in snapshots}
        approvals = [
            item
            for item in approvals
            if not (item.get("status") == "pending" and item.get("confirmation") in confirmations)
        ]
        approvals.extend(snapshots)
        store["approvals"] = approvals[-50:]
        _write_arena_pending_approvals(path, store)
    return snapshots


def _mark_arena_pending_approval(
    path: Path,
    confirmation: str,
    status: str,
    *,
    result_status: str | None = None,
    reason: str | None = None,
) -> None:
    with _json_state_lock(path):
        store = _read_arena_pending_approvals(path)
        approvals = [item for item in store.get("approvals") or [] if isinstance(item, dict)]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for item in approvals:
            if item.get("confirmation") == confirmation and item.get("status") == "pending":
                item["status"] = status
                item["resolved_at"] = now
                if result_status:
                    item["result_status"] = result_status
                if reason:
                    item["reason"] = reason
                break
        store["approvals"] = approvals
        _write_arena_pending_approvals(path, store)


def _record_arena_pending_approval_attempt(
    path: Path,
    confirmation: str,
    *,
    status: str | None = None,
    result_status: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    retryable: bool | None = None,
    now: datetime | None = None,
) -> None:
    with _json_state_lock(path):
        store = _read_arena_pending_approvals(path)
        approvals = [item for item in store.get("approvals") or [] if isinstance(item, dict)]
        current = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        for item in approvals:
            if item.get("confirmation") != confirmation or item.get("status") != "pending":
                continue
            item["last_attempt_at"] = current
            if result_status:
                item["last_result_status"] = result_status
            if reason:
                item["last_reason"] = reason
            if error:
                item["last_error"] = error
            if retryable is not None:
                item["retryable"] = bool(retryable)
            if status and status != "pending":
                item["status"] = status
                item["resolved_at"] = current
                if reason:
                    item["reason"] = reason
            break
        store["approvals"] = approvals
        _write_arena_pending_approvals(path, store)


def _find_arena_pending_approval(path: Path, confirmation: str, *, now: datetime) -> tuple[dict[str, Any] | None, str | None]:
    with _json_state_lock(path):
        store = _read_arena_pending_approvals(path)
        approvals = [item for item in store.get("approvals") or [] if isinstance(item, dict)]
        for item in reversed(approvals):
            if item.get("confirmation") != confirmation:
                continue
            if item.get("status") != "pending":
                return None, str(item.get("status") or "not_pending")
            expires_at = _parse_time(item.get("expires_at"))
            if expires_at is not None and now.astimezone(timezone.utc) > expires_at.astimezone(timezone.utc):
                item["status"] = "expired"
                item["resolved_at"] = now.isoformat(timespec="seconds")
                item["reason"] = "approval_ttl_expired"
                store["approvals"] = approvals
                _write_arena_pending_approvals(path, store)
                return None, "expired"
            return copy.deepcopy(item), None
    return None, "not_found"


def expire_stale_arena_pending_approvals(path: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    with _json_state_lock(path):
        store = _read_arena_pending_approvals(path)
        approvals = [item for item in store.get("approvals") or [] if isinstance(item, dict)]
        current = now or datetime.now(timezone.utc)
        expired: list[dict[str, Any]] = []
        changed = False
        for item in approvals:
            if item.get("status") != "pending":
                continue
            expires_at = _parse_time(item.get("expires_at"))
            if expires_at is not None and current.astimezone(timezone.utc) > expires_at.astimezone(timezone.utc):
                item["status"] = "expired"
                item["resolved_at"] = current.isoformat(timespec="seconds")
                item["reason"] = "approval_ttl_expired"
                expired.append(copy.deepcopy(item))
                changed = True
        if changed:
            store["approvals"] = approvals
            _write_arena_pending_approvals(path, store)
    return expired


def _arena_pending_approval_summaries(
    *,
    path: Path | None = None,
    now: datetime | None = None,
    env: Any = os.environ,
    limit: int = 10,
) -> list[dict[str, Any]]:
    pending_path = path or _arena_pending_approvals_path(env=env)
    expire_stale_arena_pending_approvals(pending_path, now=now)
    store = _read_arena_pending_approvals(pending_path)
    approvals = [item for item in store.get("approvals") or [] if isinstance(item, dict)]
    visible_statuses = {"pending"}
    summaries: list[dict[str, Any]] = []
    for item in approvals:
        if item.get("status") not in visible_statuses:
            continue
        summaries.append(
            {
                "status": item.get("status"),
                "confirmation": item.get("confirmation"),
                "account_id": item.get("account_id"),
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "quantity": item.get("quantity"),
                "created_at": item.get("created_at"),
                "expires_at": item.get("expires_at"),
                "resolved_at": item.get("resolved_at"),
                "reason": item.get("reason") or item.get("last_reason"),
                "retryable": item.get("retryable"),
                "last_error": item.get("last_error"),
            }
        )
    return summaries[-limit:]


def _quote_price(value: dict[str, Any]) -> Decimal | None:
    for key in ("last", "price", "last_price", "close"):
        found = _first_nested(value, key)
        parsed = _decimal(found)
        if parsed is not None:
            return parsed
    return None


def _arena_current_market_price(symbol: str, *, env: Any, market_client: Any | None = None) -> Decimal | None:
    market_secret = str(env.get("FINAM_TOKEN") or "").strip()
    if not market_secret:
        return None
    client = market_client or FinamClient()
    jwt = client.create_session(market_secret)
    quote = client.last_quote(jwt, symbol)
    return _quote_price(quote)


def _arena_revalidated_market_price(
    symbol: str,
    *,
    env: Any,
    market_client: Any | None,
    pending_path: Path | None,
    confirmation: str,
    now: datetime,
) -> tuple[Decimal | None, dict[str, Any] | None]:
    attempts = _arena_price_revalidation_retries(env=env)
    sleep_seconds = _arena_price_revalidation_sleep_seconds(env=env)
    last_error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            price = _arena_current_market_price(symbol, env=env, market_client=market_client)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if pending_path is not None and confirmation:
                _record_arena_pending_approval_attempt(
                    pending_path,
                    confirmation,
                    result_status="price_revalidation_retry",
                    reason="approval_price_revalidation_failed",
                    error=last_error,
                    retryable=True,
                    now=now,
                )
            if attempt < attempts:
                time.sleep(sleep_seconds)
            continue
        if price is not None and price > 0:
            return price, None
        last_error = "current_price_unavailable"
        if pending_path is not None and confirmation:
            _record_arena_pending_approval_attempt(
                pending_path,
                confirmation,
                result_status="price_revalidation_retry",
                reason="approval_price_revalidation_unavailable",
                error=last_error,
                retryable=True,
                now=now,
            )
        if attempt < attempts:
            time.sleep(sleep_seconds)
    return None, {
        "status": "RETRYABLE_REVALIDATION_FAILED",
        "reason": "approval_price_revalidation_failed" if last_error and last_error != "current_price_unavailable" else "approval_price_revalidation_unavailable",
        "error": last_error,
        "retryable": True,
        "attempts": attempts,
    }


def _arena_confirm_revalidation_failure(
    *,
    reason: str,
    confirmation: str,
    approval: dict[str, Any],
    policy_path: Path,
    extra: dict[str, Any] | None = None,
    status: str = "APPROVAL_REVALIDATION_FAILED",
    pending_path: Path | None = None,
    retryable: bool | None = None,
) -> dict[str, Any]:
    is_retryable = reason in ARENA_RETRYABLE_APPROVAL_REASONS if retryable is None else retryable
    if pending_path is not None:
        _record_arena_pending_approval_attempt(
            pending_path,
            confirmation,
            status=None if is_retryable else "failed",
            result_status=status,
            reason=reason,
            error=str((extra or {}).get("error") or ""),
            retryable=is_retryable,
        )
    output = {
        "status": status,
        "command": "arena-confirm",
        "policy_path": str(policy_path),
        "reason": reason,
        "confirmation": confirmation,
        "pending_approval": {key: approval.get(key) for key in ("account_id", "symbol", "side", "quantity", "entry_price", "created_at", "expires_at", "proposal_hash")},
        "broker_mutation": False,
        "safety": _safety_payload(),
    }
    output["retryable"] = is_retryable
    if extra:
        output.update(extra)
    return output


def _arena_stop_check_trading_mutations(stop_check: dict[str, Any]) -> bool:
    return bool((stop_check.get("safety") or {}).get("trading_mutations"))


def _arena_run_revalidation_failure(
    *,
    reason: str,
    policy_path: Path,
    account_id: str,
    proposal: dict[str, Any],
    live: bool,
    extra: dict[str, Any] | None = None,
    status: str = "APPROVAL_REVALIDATION_FAILED",
    retryable: bool | None = None,
) -> dict[str, Any]:
    is_retryable = reason in ARENA_RETRYABLE_APPROVAL_REASONS if retryable is None else retryable
    output = {
        "status": status,
        "command": "arena-run",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "reason": reason,
        "proposal": proposal,
        "broker_mutation": False,
        "live_requested": live,
        "retryable": is_retryable,
        "safety": _safety_payload(),
    }
    if extra:
        output.update(extra)
    return output


def _arena_revalidate_live_price_output(
    *,
    proposal: dict[str, Any],
    policy_path: Path,
    account_id: str,
    command: str,
    live: bool,
    env: Any,
    market_client: Any | None,
    now: datetime,
    pending_path: Path | None = None,
    confirmation: str = "",
) -> dict[str, Any] | None:
    trade_proposal = proposal.get("proposal") if isinstance(proposal.get("proposal"), dict) else {}
    if command == "arena-run" and market_client is None and not str(env.get("FINAM_TOKEN") or "").strip():
        return None
    approved_price = _decimal((trade_proposal.get("entry") or {}).get("limit_price")) if isinstance(trade_proposal.get("entry"), dict) else None
    if approved_price is None or approved_price <= 0:
        if command == "arena-confirm":
            return _arena_confirm_revalidation_failure(
                reason="approval_price_revalidation_unavailable",
                confirmation=confirmation,
                approval={},
                policy_path=policy_path,
                pending_path=pending_path,
                retryable=True,
            )
        return _arena_run_revalidation_failure(
            reason="approval_price_revalidation_unavailable",
            policy_path=policy_path,
            account_id=account_id,
            proposal=proposal,
            live=live,
            status="RETRYABLE_REVALIDATION_FAILED",
            retryable=True,
        )
    current_price, retryable_failure = _arena_revalidated_market_price(
        str(trade_proposal.get("symbol") or ""),
        env=env,
        market_client=market_client,
        pending_path=pending_path,
        confirmation=confirmation,
        now=now,
    )
    if retryable_failure is not None or current_price is None:
        if command == "arena-confirm":
            return _arena_confirm_revalidation_failure(
                reason=str((retryable_failure or {}).get("reason") or "approval_price_revalidation_failed"),
                confirmation=confirmation,
                approval={},
                policy_path=policy_path,
                extra=retryable_failure,
                status="RETRYABLE_REVALIDATION_FAILED",
                pending_path=pending_path,
                retryable=True,
            )
        return _arena_run_revalidation_failure(
            reason=str((retryable_failure or {}).get("reason") or "approval_price_revalidation_failed"),
            policy_path=policy_path,
            account_id=account_id,
            proposal=proposal,
            live=live,
            extra=retryable_failure,
            status="RETRYABLE_REVALIDATION_FAILED",
            retryable=True,
        )
    drift_pct = abs(current_price - approved_price) / approved_price * Decimal("100")
    max_drift = _arena_approval_max_price_drift_pct(env=env)
    if drift_pct > max_drift:
        extra = {
            "current_price": str(current_price),
            "approved_price": str(approved_price),
            "drift_pct": str(drift_pct),
            "max_drift_pct": str(max_drift),
            "error": f"drift_pct={drift_pct}",
        }
        if command == "arena-confirm":
            return _arena_confirm_revalidation_failure(
                reason="approval_price_drift_exceeded",
                confirmation=confirmation,
                approval={},
                policy_path=policy_path,
                extra=extra,
                pending_path=pending_path,
            )
        return _arena_run_revalidation_failure(
            reason="approval_price_drift_exceeded",
            policy_path=policy_path,
            account_id=account_id,
            proposal=proposal,
            live=live,
            extra=extra,
        )
    return None


def _extract_arena_confirmations(text: str) -> list[str]:
    patterns = (
        re.compile(r"\bCONFIRM_ARENA_(BUY|SELL)\s+([A-Z0-9._-]+@[A-Z0-9._-]+)\s+([A-Z0-9._-]+)\b", re.IGNORECASE),
        re.compile(r"\bCONFIRM_ARENA_EXIT\s+([A-Z0-9._-]+@[A-Z0-9._-]+)\s+([A-Z0-9._-]+)\b", re.IGNORECASE),
        re.compile(r"\bCONFIRM_ARENA_REPLACE\s+([A-Z0-9._-]+@[A-Z0-9._-]+)\s*->\s*([A-Z0-9._-]+@[A-Z0-9._-]+)\s+([A-Z0-9._-]+)\b", re.IGNORECASE),
    )
    confirmations: list[str] = [
        f"CONFIRM_ARENA_{match.group(1).upper()} {match.group(2).upper()} {match.group(3)}"
        for match in patterns[0].finditer(text or "")
    ]
    confirmations.extend(f"CONFIRM_ARENA_EXIT {match.group(1).upper()} {match.group(2)}" for match in patterns[1].finditer(text or ""))
    confirmations.extend(
        f"CONFIRM_ARENA_REPLACE {match.group(1).upper()} -> {match.group(2).upper()} {match.group(3)}"
        for match in patterns[2].finditer(text or "")
    )
    return confirmations or [str(text or "").strip()]


def _arena_confirm_sort_key(policy: dict[str, Any], confirmation: str) -> tuple[int, str]:
    account_order = {account_id: idx for idx, account_id in enumerate(_arena_account_ids(policy))}
    account_id = confirmation.rsplit(" ", maxsplit=1)[-1] if confirmation else ""
    return (account_order.get(account_id, 999), confirmation)


def _arena_confirm_many_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    confirmation_text: str,
    live: bool,
    env: Any = os.environ,
    client: Any | None = None,
    market_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    confirmations = sorted(set(_extract_arena_confirmations(confirmation_text)), key=lambda item: _arena_confirm_sort_key(policy, item))
    if len(confirmations) == 1:
        run = _arena_confirm_output(
            policy,
            policy_path=policy_path,
            confirmation=confirmations[0],
            live=live,
            env=env,
            client=client,
            market_client=market_client,
            now=now,
        )
        if live and run.get("safety", {}).get("trading_mutations"):
            post_trade_ok = _attach_arena_post_trade_stop_check(policy, policy_path=policy_path, run=run, env=env)
            if not post_trade_ok:
                run["execution_status"] = str(run.get("status") or "")
                run["status"] = "HALT"
        return run
    runs: list[dict[str, Any]] = []
    for confirmation in confirmations:
        run = _arena_confirm_output(
            policy,
            policy_path=policy_path,
            confirmation=confirmation,
            live=live,
            env=env,
            client=client,
            market_client=market_client,
            now=now,
        )
        runs.append(run)
        if live and run.get("safety", {}).get("trading_mutations"):
            post_trade_ok = _attach_arena_post_trade_stop_check(policy, policy_path=policy_path, run=run, env=env)
            if not post_trade_ok:
                run["execution_status"] = str(run.get("status") or "")
                break
    return {
        "status": _arena_confirm_many_status(runs),
        "command": "arena-confirm",
        "policy_path": str(policy_path),
        "confirmation_count": len(confirmations),
        "confirmations": confirmations,
        "runs": runs,
        "broker_mutation": any(bool(run.get("broker_mutation")) or bool((run.get("safety") or {}).get("trading_mutations")) for run in runs),
        "live_requested": live,
        "safety": _safety_payload(trading_mutations=any((run.get("safety") or {}).get("trading_mutations") for run in runs)),
    }


def _arena_confirm_many_status(runs: list[dict[str, Any]]) -> str:
    statuses = {str(run.get("status") or "") for run in runs}
    if any(run.get("post_trade_protection_status") == "POST_TRADE_PROTECTION_UNVERIFIED" for run in runs):
        return "HALT"
    if statuses & ARENA_UNRESOLVED_EXECUTION_STATUSES:
        return "HALT"
    if "RETRYABLE_REVALIDATION_FAILED" in statuses:
        return "RETRYABLE_REVALIDATION_FAILED"
    if statuses & ARENA_EXECUTED_STATUSES:
        return "EXECUTED_ARENA"
    if "APPROVAL_REVALIDATION_FAILED" in statuses:
        return "APPROVAL_REVALIDATION_FAILED"
    if "APPROVAL_EXPIRED" in statuses:
        return "APPROVAL_EXPIRED"
    if "APPROVAL_NOT_FOUND" in statuses:
        return "APPROVAL_NOT_FOUND"
    return "NO_ORDER"


def _attach_arena_post_trade_stop_check(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    run: dict[str, Any],
    env: Any = os.environ,
) -> bool:
    account_id = str(run.get("account_id") or (run.get("pending_approval") or {}).get("account_id") or "")
    soft_stop = run.get("soft_stop") if isinstance(run.get("soft_stop"), dict) else {}
    proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
    symbol = str(soft_stop.get("symbol") or proposal.get("symbol") or "").upper()
    stop_check = _arena_check_stops_output(policy, policy_path=policy_path, account_id=account_id, symbol=symbol, live=False, env=env)
    run["post_trade_stop_check"] = stop_check
    matching = [
        item
        for item in stop_check.get("checks") or []
        if isinstance(item, dict)
        and str(item.get("account_id") or "") == account_id
        and str(item.get("symbol") or "").upper() == symbol
        and item.get("check_status") == "ACTIVE"
    ]
    if stop_check.get("status") == "OK" and matching:
        run["post_trade_protection_status"] = "OK"
        return True
    run["post_trade_protection_status"] = "POST_TRADE_PROTECTION_UNVERIFIED"
    run["post_trade_protection_reason"] = str(stop_check.get("status") or "matching_soft_stop_not_found")
    return False


def _arena_confirm_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    confirmation: str,
    live: bool,
    env: Any = os.environ,
    client: Any | None = None,
    market_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    pending_path = _arena_pending_approvals_path(env=env)
    approval, missing_reason = _find_arena_pending_approval(pending_path, confirmation, now=current)
    if approval is None:
        route_guidance = _arena_confirm_missing_next_actions(
            policy,
            pending_path=pending_path,
            confirmation=confirmation,
            missing_reason=missing_reason,
            now=current,
            env=env,
        )
        return {
            "status": "APPROVAL_EXPIRED" if missing_reason == "expired" else "APPROVAL_NOT_FOUND",
            "command": "arena-confirm",
            "policy_path": str(policy_path),
            "pending_path": str(pending_path),
            "reason": missing_reason,
            "confirmation": confirmation,
            "broker_mutation": False,
            "message": route_guidance["message"],
            "parsed_confirmation": route_guidance["parsed_confirmation"],
            "execution_route": route_guidance["execution_route"],
            "active_pending_approvals": route_guidance["active_pending_approvals"],
            "next_actions": route_guidance["next_actions"],
            "safety": _safety_payload(),
        }

    raw_proposal_output = approval.get("proposal_output")
    proposal_output: dict[str, Any] = raw_proposal_output if isinstance(raw_proposal_output, dict) else {}
    trade_proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
    account_id = str(approval.get("account_id") or trade_proposal.get("account_id") or "")
    symbol = str(approval.get("symbol") or trade_proposal.get("symbol") or "").upper()
    side = str(approval.get("side") or trade_proposal.get("side") or "").upper()
    validation = validate_arena_policy(policy)
    if validation["errors"]:
        return _arena_confirm_revalidation_failure(reason="arena_policy_invalid", confirmation=confirmation, approval=approval, policy_path=policy_path, extra={"validation": validation}, pending_path=pending_path)
    if account_id not in _arena_account_ids(policy):
        return _arena_confirm_revalidation_failure(reason="account_not_allowed", confirmation=confirmation, approval=approval, policy_path=policy_path, pending_path=pending_path)
    policy_account = _arena_policy_account(policy, account_id) or {}
    if policy.get("emergency_stop") is True:
        return _arena_confirm_revalidation_failure(reason="arena_emergency_stop_enabled", confirmation=confirmation, approval=approval, policy_path=policy_path, pending_path=pending_path)
    if policy_account.get("paused") is True:
        return _arena_confirm_revalidation_failure(reason="arena_account_paused", confirmation=confirmation, approval=approval, policy_path=policy_path, pending_path=pending_path)
    blocking_state = _blocking_trade_safety_state(env=env)
    if blocking_state is not None:
        return _arena_confirm_revalidation_failure(reason="unresolved_trade_safety_state", confirmation=confirmation, approval=approval, policy_path=policy_path, extra={"safety_state": blocking_state}, pending_path=pending_path, retryable=True)
    if side in {"REPLACE", "EXIT"} or isinstance(approval.get("portfolio_action"), dict):
        return _arena_confirm_portfolio_action_output(
            policy,
            policy_path=policy_path,
            confirmation=confirmation,
            approval=approval,
            pending_path=pending_path,
            live=live,
            env=env,
            client=client,
            market_client=market_client,
        )
    if not trade_proposal.get("gates", {}).get("execution_allowed"):
        return _arena_confirm_revalidation_failure(
            reason="arena_execution_gate_blocked",
            confirmation=confirmation,
            approval=approval,
            policy_path=policy_path,
            extra={"gate_reasons": trade_proposal.get("gates", {}).get("gate_reasons") or []},
            pending_path=pending_path,
        )
    if side == "SELL":
        short_availability = trade_proposal.get("risk", {}).get("short_availability") if isinstance(trade_proposal.get("risk"), dict) else None
        if not (isinstance(short_availability, dict) and short_availability.get("available") is True):
            reason = ARENA_MARGIN_TRADING_NOT_SUPPORTED
            if isinstance(short_availability, dict):
                reason = str(short_availability.get("gate_reason") or short_availability.get("reason") or reason)
            return _arena_confirm_revalidation_failure(reason=reason, confirmation=confirmation, approval=approval, policy_path=policy_path, pending_path=pending_path)

    market_session_block = _arena_market_session_block(proposal_output, now=current)
    if market_session_block is not None:
        return {
            "status": "WAIT_MARKET_CLOSED",
            "command": "arena-confirm",
            "policy_path": str(policy_path),
            "pending_path": str(pending_path),
            "confirmation": confirmation,
            "pending_approval": approval,
            "proposal": proposal_output,
            "account_id": account_id,
            "symbol": symbol,
            "reason": market_session_block["reason"],
            "market_session": market_session_block,
            "broker_mutation": False,
            "live_requested": live,
            "safety": _safety_payload(),
        }

    approved_price = _decimal((trade_proposal.get("entry") or {}).get("limit_price")) if isinstance(trade_proposal.get("entry"), dict) else None
    if approved_price is None or approved_price <= 0:
        return _arena_confirm_revalidation_failure(reason="approval_price_revalidation_unavailable", confirmation=confirmation, approval=approval, policy_path=policy_path, pending_path=pending_path, retryable=True)
    current_price, retryable_failure = _arena_revalidated_market_price(
        symbol,
        env=env,
        market_client=market_client,
        pending_path=pending_path,
        confirmation=confirmation,
        now=current,
    )
    if retryable_failure is not None or current_price is None:
        return _arena_confirm_revalidation_failure(
            reason=str((retryable_failure or {}).get("reason") or "approval_price_revalidation_failed"),
            confirmation=confirmation,
            approval=approval,
            policy_path=policy_path,
            extra=retryable_failure,
            status="RETRYABLE_REVALIDATION_FAILED",
            pending_path=pending_path,
            retryable=True,
        )
    drift_pct = abs(current_price - approved_price) / approved_price * Decimal("100")
    max_drift = _arena_approval_max_price_drift_pct(env=env)
    if drift_pct > max_drift:
        return _arena_confirm_revalidation_failure(
            reason="approval_price_drift_exceeded",
            confirmation=confirmation,
            approval=approval,
            policy_path=policy_path,
            extra={"current_price": str(current_price), "approved_price": str(approved_price), "drift_pct": str(drift_pct), "max_drift_pct": str(max_drift), "error": f"drift_pct={drift_pct}"},
            pending_path=pending_path,
        )
    stop_check = _arena_check_stops_output(
        policy,
        policy_path=policy_path,
        account_id=account_id,
        symbol=symbol,
        live=live,
        client=client,
        env=env,
        execute_triggered_stops=live,
    )
    if stop_check.get("status") not in {"NO_STOPS", "OK"}:
        stop_check_mutation = _arena_stop_check_trading_mutations(stop_check)
        return _arena_confirm_revalidation_failure(
            reason="arena_stop_check_not_clear",
            confirmation=confirmation,
            approval=approval,
            policy_path=policy_path,
            extra={
                "stop_check": stop_check,
                "broker_mutation": stop_check_mutation,
                "safety": _safety_payload(trading_mutations=stop_check_mutation),
            },
            status="HALT",
            pending_path=pending_path,
            retryable=True,
        )
    if not live:
        return {
            "status": "DRY_RUN",
            "command": "arena-confirm",
            "policy_path": str(policy_path),
            "confirmation": confirmation,
            "pending_approval": approval,
            "proposal": proposal_output,
            "broker_mutation": False,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    auto_trade_env = str(policy.get("auto_trade_env") or "FINAM_ARENA_AUTO_TRADE_ENABLED")
    if str(env.get(auto_trade_env, "")).strip().lower() != "true":
        return _arena_confirm_revalidation_failure(reason=f"{auto_trade_env}_not_true", confirmation=confirmation, approval=approval, policy_path=policy_path, extra={"required_env": auto_trade_env}, pending_path=pending_path)
    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    if not str(env.get(secret_env) or "").strip():
        return _arena_confirm_revalidation_failure(reason=f"{secret_env}_not_set", confirmation=confirmation, approval=approval, policy_path=policy_path, pending_path=pending_path)

    output = _arena_execute_live_output(
        policy,
        proposal_output,
        policy_path=policy_path,
        account_id=account_id,
        confirmation=confirmation,
        client=client,
        env=env,
        mutation_context="arena-confirm",
    )
    output["command"] = "arena-confirm"
    output["pending_path"] = str(pending_path)
    output["pending_approval_hash"] = approval.get("proposal_hash")
    if output.get("status") in ARENA_EXECUTED_STATUSES:
        _mark_arena_pending_approval(pending_path, confirmation, "executed", result_status=str(output.get("status")))
    elif output.get("safety", {}).get("trading_mutations"):
        _mark_arena_pending_approval(pending_path, confirmation, "failed", result_status=str(output.get("status")), reason=str(output.get("reason") or "live_attempt_failed"))
    return output


def _arena_confirm_portfolio_action_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    confirmation: str,
    approval: dict[str, Any],
    pending_path: Path,
    live: bool,
    env: Any,
    client: Any | None,
    market_client: Any | None,
) -> dict[str, Any]:
    current_action = approval.get("portfolio_action") if isinstance(approval.get("portfolio_action"), dict) else {}
    account_id = str(approval.get("account_id") or current_action.get("account_id") or "")
    action_type = str(approval.get("action") or current_action.get("action") or "").upper()
    if live:
        stop_check = _arena_check_stops_output(
            policy,
            policy_path=policy_path,
            live=live,
            client=client,
            env=env,
            execute_triggered_stops=live,
        )
        if stop_check.get("status") not in {"NO_STOPS", "OK"}:
            stop_check_mutation = _arena_stop_check_trading_mutations(stop_check)
            return _arena_confirm_revalidation_failure(
                reason="arena_stop_check_not_clear",
                confirmation=confirmation,
                approval=approval,
                policy_path=policy_path,
                extra={
                    "stop_check": stop_check,
                    "broker_mutation": stop_check_mutation,
                    "safety": _safety_payload(trading_mutations=stop_check_mutation),
                },
                status="HALT",
                pending_path=pending_path,
                retryable=True,
            )
    ledger = _read_arena_execution_ledger(env=env)
    scan = build_arena_scan(
        policy,
        soft_stops=_arena_soft_stop_records(env=env),
        market_client=market_client,
        env=env,
        research_mode="cache_only",
        execution_ledger=ledger,
    )
    review = build_arena_portfolio_review(policy, scan)
    match = _find_matching_portfolio_confirmation_action(approval, review)
    if match is None:
        return _arena_confirm_revalidation_failure(
            reason="portfolio_confirmation_action_not_current",
            confirmation=confirmation,
            approval=approval,
            policy_path=policy_path,
            extra={"portfolio_review_status": review.get("status"), "planned_actions": review.get("planned_actions") or []},
            pending_path=pending_path,
        )
    action, account_review, scan_account = match
    if not live:
        return {
            "status": "DRY_RUN",
            "command": "arena-confirm",
            "policy_path": str(policy_path),
            "pending_path": str(pending_path),
            "confirmation": confirmation,
            "pending_approval": approval,
            "portfolio_action": action,
            "broker_mutation": False,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    auto_trade_env = str(policy.get("auto_trade_env") or "FINAM_ARENA_AUTO_TRADE_ENABLED")
    if str(env.get(auto_trade_env, "")).strip().lower() != "true":
        return _arena_confirm_revalidation_failure(
            reason=f"{auto_trade_env}_not_true",
            confirmation=confirmation,
            approval=approval,
            policy_path=policy_path,
            extra={"required_env": auto_trade_env},
            pending_path=pending_path,
        )
    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    secret = str(env.get(secret_env) or "").strip()
    if not secret:
        return _arena_confirm_revalidation_failure(reason=f"{secret_env}_not_set", confirmation=confirmation, approval=approval, policy_path=policy_path, pending_path=pending_path)
    arena_client = client or FinamClient(base_url=arena_base_url(policy, env=env))
    try:
        jwt = arena_client.create_session(secret)
    except Exception as exc:  # noqa: BLE001
        return _arena_confirm_revalidation_failure(reason="arena_session_create_failed", confirmation=confirmation, approval=approval, policy_path=policy_path, extra={"error": str(exc)}, pending_path=pending_path, retryable=True)
    if action_type == "REPLACE":
        result = _execute_confirmed_arena_replacement(
            policy,
            policy_path=policy_path,
            action=action,
            account_review=account_review,
            confirmation=confirmation,
            client=arena_client,
            jwt=jwt,
            env=env,
            market_client=market_client,
        )
    else:
        result = _execute_arena_portfolio_action(
            policy,
            policy_path=policy_path,
            action=action,
            account_review=account_review,
            scan_account=scan_account,
            client=arena_client,
            jwt=jwt,
            env=env,
            confirmed=True,
        )
    result["command"] = "arena-confirm"
    result["pending_path"] = str(pending_path)
    result["pending_approval_hash"] = approval.get("proposal_hash")
    result["confirmation"] = confirmation
    if result.get("status") in ARENA_EXECUTED_STATUSES:
        _mark_arena_pending_approval(pending_path, confirmation, "executed", result_status=str(result.get("status")))
    elif result.get("status") == "APPROVAL_REVALIDATION_FAILED":
        _mark_arena_pending_approval(pending_path, confirmation, "failed", result_status=str(result.get("status")), reason=str(result.get("reason") or "portfolio_action_failed"))
    elif result.get("safety", {}).get("trading_mutations"):
        _mark_arena_pending_approval(pending_path, confirmation, "failed", result_status=str(result.get("status")), reason=str(result.get("reason") or "live_attempt_failed"))
    return result


def _execute_confirmed_arena_replacement(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    action: dict[str, Any],
    account_review: dict[str, Any],
    confirmation: str,
    client: Any,
    jwt: str,
    env: Any,
    market_client: Any | None,
) -> dict[str, Any]:
    account_id = str(action.get("account_id") or "")
    replacement = account_review.get("replacement") if isinstance(account_review.get("replacement"), dict) else {}
    sell_symbol = str(replacement.get("sell_symbol") or action.get("sell_symbol") or "").upper()
    buy_symbol = str(replacement.get("buy_symbol") or action.get("buy_symbol") or "").upper()
    if not account_id or not sell_symbol or not buy_symbol:
        return {
            "status": "APPROVAL_REVALIDATION_FAILED",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "action": "REPLACE",
            "reason": "arena_replace_symbols_missing",
            "replacement": {"sell_symbol": sell_symbol or None, "buy_symbol": buy_symbol or None},
            "broker_mutation": False,
            "safety": _safety_payload(),
        }

    sell_result = _arena_execute_position_exit_action(
        policy,
        policy_path=policy_path,
        action={"account_id": account_id, "action": "EXIT_WEAK", "symbol": sell_symbol},
        client=client,
        jwt=jwt,
        env=env,
    )
    if str(sell_result.get("status") or "") not in ARENA_EXECUTED_STATUSES:
        return {
            "status": str(sell_result.get("status") or "HALT"),
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "action": "REPLACE",
            "reason": "arena_replace_sell_leg_not_confirmed",
            "replacement": {"sell_symbol": sell_symbol, "buy_symbol": buy_symbol},
            "sell_result": sell_result,
            "halt_new_entries": True,
            "broker_mutation": bool((sell_result.get("safety") or {}).get("trading_mutations")),
            "safety": _safety_payload(trading_mutations=bool((sell_result.get("safety") or {}).get("trading_mutations"))),
        }

    ledger = _read_arena_execution_ledger(env=env)
    post_sell_scan = build_arena_scan(
        policy,
        soft_stops=_arena_soft_stop_records(env=env),
        market_client=market_client,
        env=env,
        research_mode="cache_only",
        execution_ledger=ledger,
    )
    buy_candidate = _matching_post_sell_buy_candidate(post_sell_scan, account_id=account_id, buy_symbol=buy_symbol)
    buy_account = _matching_post_sell_account(post_sell_scan, account_id=account_id)
    buy_gate_reasons = buy_candidate.get("gate_reasons") if isinstance(buy_candidate.get("gate_reasons"), list) else []
    if not buy_candidate or buy_candidate.get("execution_allowed") is not True or buy_gate_reasons:
        return _arena_replace_sell_done_buy_blocked(
            policy_path=policy_path,
            account_id=account_id,
            sell_symbol=sell_symbol,
            buy_symbol=buy_symbol,
            sell_result=sell_result,
            reason="arena_replace_buy_revalidation_failed_after_sell",
            env=env,
            extra={"post_sell_scan_status": post_sell_scan.get("status"), "buy_gate_reasons": buy_gate_reasons},
        )

    proposal_output = _arena_proposal_from_candidate(buy_candidate, account=buy_account)
    market_session_block = _arena_market_session_block(proposal_output, now=datetime.now(timezone.utc))
    if market_session_block is not None:
        return _arena_replace_sell_done_buy_blocked(
            policy_path=policy_path,
            account_id=account_id,
            sell_symbol=sell_symbol,
            buy_symbol=buy_symbol,
            sell_result=sell_result,
            reason=str(market_session_block.get("reason") or "arena_replace_buy_market_session_closed_after_sell"),
            env=env,
            extra={"market_session": market_session_block},
        )
    stop_check = _arena_check_stops_output(
        policy,
        policy_path=policy_path,
        live=True,
        client=client,
        env=env,
        execute_triggered_stops=True,
    )
    if stop_check.get("status") not in {"NO_STOPS", "OK"}:
        return _arena_replace_sell_done_buy_blocked(
            policy_path=policy_path,
            account_id=account_id,
            sell_symbol=sell_symbol,
            buy_symbol=buy_symbol,
            sell_result=sell_result,
            reason="arena_replace_buy_stop_check_not_clear_after_sell",
            env=env,
            extra={"stop_check": stop_check},
        )
    price_failure = _arena_revalidate_live_price_output(
        proposal=proposal_output,
        policy_path=policy_path,
        account_id=account_id,
        command="arena-run",
        live=True,
        env=env,
        market_client=market_client,
        now=datetime.now(timezone.utc),
        confirmation=confirmation,
    )
    if price_failure is not None:
        return _arena_replace_sell_done_buy_blocked(
            policy_path=policy_path,
            account_id=account_id,
            sell_symbol=sell_symbol,
            buy_symbol=buy_symbol,
            sell_result=sell_result,
            reason=str(price_failure.get("reason") or "arena_replace_buy_price_revalidation_failed_after_sell"),
            env=env,
            extra={"buy_revalidation": price_failure},
        )
    buy_result = _arena_execute_live_output(
        policy,
        proposal_output,
        policy_path=policy_path,
        account_id=account_id,
        confirmation=confirmation,
        client=client,
        env=env,
        mutation_context="arena-confirm",
    )
    if str(buy_result.get("status") or "") not in ARENA_EXECUTED_STATUSES:
        return {
            "status": "HALT",
            "command": "arena-portfolio-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "action": "REPLACE",
            "reason": "arena_replace_buy_leg_failed_after_sell",
            "replacement": {"sell_symbol": sell_symbol, "buy_symbol": buy_symbol},
            "sell_result": sell_result,
            "buy_result": buy_result,
            "halt_new_entries": True,
            "broker_mutation": True,
            "safety": _safety_payload(trading_mutations=True),
        }

    output = {
        "status": "EXECUTED_ARENA_REPLACEMENT",
        "command": "arena-portfolio-run",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "action": "REPLACE",
        "replacement": {"sell_symbol": sell_symbol, "buy_symbol": buy_symbol},
        "sell_result": sell_result,
        "buy_result": buy_result,
        "confirmation_used": confirmation,
        "halt_new_entries": False,
        "broker_mutation": True,
        "safety": _safety_payload(trading_mutations=True),
    }
    _record_arena_execution_ledger(output, policy=policy, env=env)
    return output


def _arena_replace_sell_done_buy_blocked(
    *,
    policy_path: Path,
    account_id: str,
    sell_symbol: str,
    buy_symbol: str,
    sell_result: dict[str, Any],
    reason: str,
    env: Any,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output = {
        "status": "REPLACE_SELL_DONE_BUY_BLOCKED",
        "command": "arena-portfolio-run",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "action": "REPLACE",
        "reason": reason,
        "replacement": {"sell_symbol": sell_symbol, "buy_symbol": buy_symbol},
        "sell_result": sell_result,
        "halt_new_entries": True,
        "broker_mutation": True,
        "safety": _safety_payload(trading_mutations=True),
    }
    if extra:
        output.update(extra)
    _write_trade_safety_state(output, env=env)
    return output


def _matching_post_sell_buy_candidate(scan: dict[str, Any], *, account_id: str, buy_symbol: str) -> dict[str, Any]:
    for candidate in scan.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("account_id") or "") == account_id and str(candidate.get("symbol") or "").upper() == buy_symbol:
            return candidate
    return {}


def _matching_post_sell_account(scan: dict[str, Any], *, account_id: str) -> dict[str, Any]:
    for account in scan.get("accounts") or []:
        if isinstance(account, dict) and str(account.get("account_id") or "") == account_id:
            return account
    return {"account_id": account_id}


def _find_matching_portfolio_confirmation_action(
    approval: dict[str, Any],
    review: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    account_id = str(approval.get("account_id") or "")
    action_type = str(approval.get("action") or "").upper()
    approved_action = approval.get("portfolio_action") if isinstance(approval.get("portfolio_action"), dict) else {}
    accounts = {str(item.get("account_id") or ""): item for item in review.get("accounts") or [] if isinstance(item, dict)}
    account_review = accounts.get(account_id) or {}
    scan_account = {"account_id": account_id}
    for action in review.get("planned_actions") or []:
        if not isinstance(action, dict):
            continue
        if str(action.get("account_id") or "") != account_id or str(action.get("action") or "").upper() != action_type:
            continue
        if action_type == "REPLACE":
            replacement = account_review.get("replacement") if isinstance(account_review.get("replacement"), dict) else {}
            expected = approved_action.get("replacement") if isinstance(approved_action.get("replacement"), dict) else {}
            sell_symbol = str(replacement.get("sell_symbol") or action.get("sell_symbol") or "").upper()
            buy_symbol = str(replacement.get("buy_symbol") or action.get("buy_symbol") or "").upper()
            if sell_symbol == str(expected.get("sell_symbol") or "").upper() and buy_symbol == str(expected.get("buy_symbol") or approval.get("symbol") or "").upper():
                return action, account_review, scan_account
            continue
        if str(action.get("symbol") or "").upper() == str(approved_action.get("symbol") or approval.get("symbol") or "").upper():
            return action, account_review, scan_account
    return None


def _arena_nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + (nth - 1) * 7)


def _arena_last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    offset = (current.weekday() - weekday) % 7
    return current - timedelta(days=offset)


def _arena_observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _arena_easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _arena_us_market_holidays(year: int) -> set[date]:
    return {
        _arena_observed_fixed_holiday(year, 1, 1),
        _arena_nth_weekday(year, 1, 0, 3),
        _arena_nth_weekday(year, 2, 0, 3),
        _arena_easter_date(year) - timedelta(days=2),
        _arena_last_weekday(year, 5, 0),
        _arena_observed_fixed_holiday(year, 6, 19),
        _arena_observed_fixed_holiday(year, 7, 4),
        _arena_nth_weekday(year, 9, 0, 1),
        _arena_nth_weekday(year, 11, 3, 4),
        _arena_observed_fixed_holiday(year, 12, 25),
    }


def _arena_is_us_market_holiday(day: date) -> bool:
    return day in _arena_us_market_holidays(day.year) or day in _arena_us_market_holidays(day.year + 1)


def _arena_next_us_market_open(current_ny: datetime) -> datetime:
    next_open = current_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    if current_ny >= next_open:
        next_open += timedelta(days=1)
    while next_open.weekday() >= 5 or _arena_is_us_market_holiday(next_open.date()):
        next_open += timedelta(days=1)
    return next_open


def _arena_market_session_block(proposal: dict[str, Any], *, now: datetime) -> dict[str, Any] | None:
    raw_trade_proposal = proposal.get("proposal")
    trade_proposal = raw_trade_proposal if isinstance(raw_trade_proposal, dict) else {}
    raw_candidate = proposal.get("candidate")
    candidate = raw_candidate if isinstance(raw_candidate, dict) else {}
    symbol = str(trade_proposal.get("symbol") or candidate.get("symbol") or "").upper()
    mic = str(symbol.rsplit("@", maxsplit=1)[-1] if "@" in symbol else "").upper()
    if mic not in ARENA_US_REGULAR_SESSION_MICS:
        return None

    current_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current_ny = current_utc.astimezone(ARENA_US_MARKET_TZ)
    open_at = current_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    close_at = current_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    holiday = _arena_is_us_market_holiday(current_ny.date())
    if current_ny.weekday() < 5 and not holiday and open_at <= current_ny < close_at:
        return None

    next_open = _arena_next_us_market_open(current_ny)
    return {
        "reason": "us_market_holiday" if holiday else "us_regular_session_closed",
        "symbol": symbol,
        "mic": mic,
        "timezone": "America/New_York",
        "holiday": holiday,
        "now_ny": current_ny.isoformat(timespec="seconds"),
        "now_msk": current_utc.astimezone(ARENA_MSK_TZ).isoformat(timespec="seconds"),
        "opens_at_ny": next_open.isoformat(timespec="seconds"),
        "opens_at_msk": next_open.astimezone(ARENA_MSK_TZ).isoformat(timespec="seconds"),
        "regular_session": "09:30-16:00 America/New_York",
    }


def _arena_run_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
    live: bool,
    confirmation: str = "",
    client: Any | None = None,
    market_client: Any | None = None,
    env: Any = os.environ,
    now: datetime | None = None,
    research_mode: str = "cache_only",
    scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    proposal = _arena_proposal_output(policy, policy_path=policy_path, account_id=account_id, research_mode=research_mode, scan=scan)
    if proposal.get("status") != "PROPOSE_ONLY":
        return {
            "status": "NO_ORDER",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": "no_active_arena_proposal",
            "proposal_status": proposal.get("status"),
            "proposal": proposal,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    trade_proposal = proposal.get("proposal") if isinstance(proposal.get("proposal"), dict) else {}
    account = proposal.get("account") if isinstance(proposal.get("account"), dict) else {}
    if policy.get("emergency_stop") is True:
        return {
            "status": "HALT",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": "arena_emergency_stop_enabled",
            "proposal": proposal,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    if account.get("paused") is True:
        return {
            "status": "HALT",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": "arena_account_paused",
            "proposal": proposal,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    override_status = _arena_run_override_status(trade_proposal, confirmation)
    if not trade_proposal.get("gates", {}).get("execution_allowed"):
        if override_status["override_allowed"] and override_status["confirmation_matches"]:
            proposal["override"] = {
                "applied": True,
                "gate_reasons": override_status["override_gate_reasons"],
                "confirmation": confirmation,
            }
            trade_proposal["gates"] = dict(trade_proposal.get("gates") or {}) | {
                "override_applied": True,
                "override_gate_reasons": override_status["override_gate_reasons"],
            }
        else:
            extra = {}
            if override_status["override_allowed"]:
                extra = {
                    "override_allowed": True,
                    "override_confirmation_phrase": override_status["override_confirmation_phrase"],
                    "override_next_live_command": override_status["override_next_live_command"],
                    "override_gate_reasons": override_status["override_gate_reasons"],
                    "override_confirmation_received": bool(str(confirmation or "").strip()),
                    "override_confirmation_matches": False,
                }
            elif override_status["parsed_override_confirmation"]:
                extra = {
                    "override_allowed": False,
                    "parsed_override_confirmation": override_status["parsed_override_confirmation"],
                    "override_rejected_reason": "gate_reasons_not_overrideable",
                }
            return {
                "status": "BLOCKED",
                "command": "arena-run",
                "policy_path": str(policy_path),
                "account_id": account_id,
                "reason": "arena_execution_gate_blocked",
                "gate_reasons": trade_proposal.get("gates", {}).get("gate_reasons") or [],
                "proposal": proposal,
                "live_requested": live,
                "safety": _safety_payload(),
            } | extra
    if not live:
        return {
            "status": "DRY_RUN",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "proposal": proposal,
            "broker_mutation": False,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    auto_trade_env = str(policy.get("auto_trade_env") or "FINAM_ARENA_AUTO_TRADE_ENABLED")
    if str(env.get(auto_trade_env, "")).strip().lower() != "true":
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": f"{auto_trade_env}_not_true",
            "required_env": auto_trade_env,
            "proposal": proposal,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    if not str(env.get(secret_env) or "").strip():
        return _arena_run_revalidation_failure(
            reason=f"{secret_env}_not_set",
            policy_path=policy_path,
            account_id=account_id,
            proposal=proposal,
            live=live,
            status="NO_TRADE",
        )
    blocking_state = _blocking_trade_safety_state(env=env)
    if blocking_state is not None:
        return {
            "status": "HALT",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": "unresolved_trade_safety_state",
            "safety_state": blocking_state,
            "proposal": proposal,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    required_confirmation = str(trade_proposal.get("execution", {}).get("confirmation_phrase") or "")
    allowed_confirmations = {required_confirmation}
    if override_status.get("confirmation_matches"):
        allowed_confirmations.add(str(override_status.get("override_confirmation_phrase") or ""))
    if _arena_confirmation_required(policy, account, now=current, env=env) and confirmation not in allowed_confirmations:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "required_confirmation": required_confirmation,
            "received_confirmation": confirmation,
            "proposal": proposal,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    market_session_block = _arena_market_session_block(proposal, now=current)
    if market_session_block is not None:
        return {
            "status": "WAIT_MARKET_CLOSED",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": market_session_block["reason"],
            "market_session": market_session_block,
            "proposal": proposal,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    stop_check = _arena_check_stops_output(
        policy,
        policy_path=policy_path,
        live=live,
        client=client,
        env=env,
        execute_triggered_stops=live,
    )
    if stop_check.get("status") not in {"NO_STOPS", "OK"}:
        stop_check_mutation = _arena_stop_check_trading_mutations(stop_check)
        return _arena_run_revalidation_failure(
            reason="arena_stop_check_not_clear",
            policy_path=policy_path,
            account_id=account_id,
            proposal=proposal,
            live=live,
            extra={
                "stop_check": stop_check,
                "broker_mutation": stop_check_mutation,
                "safety": _safety_payload(trading_mutations=stop_check_mutation),
            },
            status="HALT",
            retryable=True,
        )
    price_failure = _arena_revalidate_live_price_output(
        proposal=proposal,
        policy_path=policy_path,
        account_id=account_id,
        command="arena-run",
        live=live,
        env=env,
        market_client=market_client,
        now=current,
        confirmation=confirmation,
    )
    if price_failure is not None:
        return price_failure
    return _arena_execute_live_output(
        policy,
        proposal,
        policy_path=policy_path,
        account_id=account_id,
        confirmation=confirmation,
        client=client,
        env=env,
        mutation_context="arena-run",
    )


def _arena_confirmation_required(policy: dict[str, Any], account: dict[str, Any], *, now: datetime | None = None, env: Any = os.environ) -> bool:
    if policy.get("mode") == "approval":
        return True
    if account.get("trade_mode") != "auto":
        return True
    approval_until = _parse_time(arena_approval_until(policy, env=env))
    if approval_until is None:
        return True
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc) <= approval_until


def _arena_execute_live_output(
    policy: dict[str, Any],
    proposal_output: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
    confirmation: str,
    client: Any | None,
    env: Any,
    mutation_context: str = "",
) -> dict[str, Any]:
    if mutation_context not in {"arena-run", "arena-confirm"}:
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": "missing_arena_mutation_context",
            "required_context": ["arena-run", "arena-confirm"],
            "safety": _safety_payload(),
        }
    trade_proposal = proposal_output["proposal"]
    payloads = copy.deepcopy(proposal_output["order_payloads"])
    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    secret = str(env.get(secret_env) or "").strip()
    if not secret:
        return {
            "status": "NO_TRADE",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": f"{secret_env}_not_set",
            "safety": _safety_payload(),
        }
    client = client or FinamClient(base_url=arena_base_url(policy, env=env))
    try:
        jwt = client.create_session(secret)
    except Exception as exc:  # noqa: BLE001
        return _arena_broker_error_output("arena_session_create_failed", exc, proposal_output, policy_path=policy_path, account_id=account_id)

    try:
        entry_result = client.place_order(jwt, account_id, payloads["entry_order"])
    except Exception as exc:  # noqa: BLE001
        output = _arena_broker_error_output("arena_entry_submit_failed", exc, proposal_output, policy_path=policy_path, account_id=account_id)
        output["entry_order_payload"] = payloads["entry_order"]
        _write_trade_safety_state(output, env=env)
        return output

    _write_trade_safety_state(
        {
            "halt_new_buys": True,
            "status": "ARENA_ENTRY_SUBMITTED",
            "account_id": account_id,
            "symbol": trade_proposal["symbol"],
            "side": trade_proposal.get("side"),
            "entry_order_id": entry_result.get("order_id"),
        },
        env=env,
    )
    fill_state = _wait_for_buy_execution(client, jwt, account_id, entry_result, trade_proposal, env=env)
    if fill_state["status"] == "pending":
        output = {
            "status": "ENTRY_PENDING_NO_STOP",
            "command": "arena-run",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": "entry_order_not_filled_yet",
            "proposal": proposal_output,
            "entry_order_payload": payloads["entry_order"],
            "entry_order_response": entry_result,
            "entry_fill_state": fill_state,
            "halt_new_entries": True,
            "safety": _safety_payload(trading_mutations=True),
        }
        _write_trade_safety_state(output, env=env)
        return output
    if fill_state["status"] not in {"filled", "partial"}:
        output = _arena_broker_error_output(
            "arena_entry_fill_check_failed",
            RuntimeError(str(fill_state)),
            proposal_output,
            policy_path=policy_path,
            account_id=account_id,
        )
        output["entry_order_payload"] = payloads["entry_order"]
        output["entry_order_response"] = entry_result
        _write_trade_safety_state(output, env=env)
        return output

    filled_quantity = _decimal(fill_state["executed_quantity"]) or Decimal("0")
    protection_quantity, coverage = _arena_resolved_protection_quantity(
        client,
        jwt,
        account_id,
        trade_proposal["symbol"],
        filled_quantity=filled_quantity,
        env=env,
    )
    protected_proposal = trade_proposal | {"quantity": protection_quantity}
    payloads["protective_stop"]["quantity"] = {"value": decimal_payload(protection_quantity, min_scale=1)}
    _write_trade_safety_state(
        {
            "halt_new_buys": True,
            "status": "ARENA_ENTRY_FILLED_STOP_PENDING",
            "account_id": account_id,
            "symbol": trade_proposal["symbol"],
            "side": trade_proposal.get("side"),
            "entry_order_id": entry_result.get("order_id"),
            "executed_quantity": decimal_payload(filled_quantity, min_scale=1),
            "protection_quantity": decimal_payload(protection_quantity, min_scale=1),
        },
        env=env,
    )
    soft_stop = _arena_soft_stop_record(
        account_id=account_id,
        proposal=protected_proposal,
        entry_result=entry_result,
        fill_state=fill_state,
    )
    soft_stop["coverage"] = coverage
    _upsert_arena_soft_stop(soft_stop, env=env)

    output = {
        "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE" if fill_state["status"] == "filled" else "EXECUTED_ARENA_PARTIAL_SOFT_STOP_ACTIVE",
        "command": "arena-run",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "proposal": proposal_output,
        "entry_order_payload": payloads["entry_order"],
        "protective_stop_payload": payloads["protective_stop"],
        "entry_order_response": entry_result,
        "entry_fill_state": fill_state,
        "soft_stop": soft_stop,
        "stop_protection_mode": "arena_soft_stop",
        "confirmation_used": confirmation,
        "broker_mutation": True,
        "halt_new_entries": False,
        "safety": _safety_payload(trading_mutations=True),
    }
    _record_arena_execution_ledger(output, policy=policy, env=env)
    return output


def _arena_broker_error_output(
    reason: str,
    exc: Exception,
    proposal_output: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
) -> dict[str, Any]:
    return {
        "status": "BROKER_ERROR",
        "command": "arena-run",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "reason": reason,
        "error": str(exc),
        "proposal": proposal_output,
        "halt_new_entries": True,
        "safety": _safety_payload(trading_mutations=True),
    }


def _arena_resolved_protection_quantity(
    client: Any,
    jwt: str,
    account_id: str,
    symbol: str,
    *,
    filled_quantity: Decimal,
    env: Any = os.environ,
) -> tuple[Decimal, dict[str, Any]]:
    normalized_symbol = str(symbol or "").upper()
    try:
        account = client.get_account(jwt, account_id)
        position_quantity = _arena_position_quantity(account, normalized_symbol)
        if position_quantity > 0:
            return position_quantity, {
                "source": "position_quantity",
                "filled_quantity": decimal_payload(filled_quantity, min_scale=1),
                "position_quantity": decimal_payload(position_quantity, min_scale=1),
            }
    except Exception as exc:  # noqa: BLE001
        position_error = str(exc)
    else:
        position_error = "position_not_found"

    existing = next(
        (
            item
            for item in _arena_soft_stop_records(env=env)
            if str(item.get("account_id") or "") == account_id and str(item.get("symbol") or "").upper() == normalized_symbol
        ),
        None,
    )
    existing_quantity = _decimal((existing or {}).get("quantity")) if existing else None
    if existing_quantity is not None and existing_quantity > 0:
        combined = existing_quantity + filled_quantity
        return combined, {
            "source": "existing_soft_stop_plus_fill",
            "filled_quantity": decimal_payload(filled_quantity, min_scale=1),
            "existing_quantity": decimal_payload(existing_quantity, min_scale=1),
            "position_read_error": position_error,
        }
    return filled_quantity, {
        "source": "filled_quantity",
        "filled_quantity": decimal_payload(filled_quantity, min_scale=1),
        "position_read_error": position_error,
    }


def _arena_soft_stop_record(
    *,
    account_id: str,
    proposal: dict[str, Any],
    entry_result: dict[str, Any] | None = None,
    fill_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stop = proposal.get("protective_stop") if isinstance(proposal.get("protective_stop"), dict) else {}
    quantity = _decimal(proposal.get("quantity")) or _decimal((fill_state or {}).get("executed_quantity")) or Decimal("0")
    return {
        "status": "ACTIVE",
        "mode": "arena_soft_stop",
        "account_id": account_id,
        "symbol": str(proposal.get("symbol") or "").upper(),
        "side": str(stop.get("side") or "SELL").upper(),
        "quantity": decimal_payload(quantity, min_scale=1),
        "stop_price": str(stop.get("stop_price")),
        "entry_order_id": (entry_result or {}).get("order_id"),
        "executed_quantity": (fill_state or {}).get("executed_quantity"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _arena_soft_stop_records(*, env: Any = os.environ) -> list[dict[str, Any]]:
    state = _read_trade_safety_state(env=env)
    records = state.get("arena_soft_stops")
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]
    if state.get("mode") == "arena_soft_stop" and state.get("symbol") and state.get("account_id"):
        return [state]
    return []


def _active_arena_soft_stop_record(*, account_id: str, symbol: str, env: Any = os.environ) -> dict[str, Any] | None:
    normalized_symbol = symbol.upper()
    for item in _arena_soft_stop_records(env=env):
        if str(item.get("account_id") or "") != account_id:
            continue
        if str(item.get("symbol") or "").upper() != normalized_symbol:
            continue
        if str(item.get("status") or "ACTIVE").upper() != "ACTIVE":
            continue
        return item
    return None


def _upsert_arena_soft_stop(record: dict[str, Any], *, env: Any = os.environ) -> None:
    path = _trade_safety_state_path(env=env)
    with _json_state_lock(path):
        records = [
            item
            for item in _arena_soft_stop_records(env=env)
            if not (
                str(item.get("account_id") or "") == str(record.get("account_id") or "")
                and str(item.get("symbol") or "").upper() == str(record.get("symbol") or "").upper()
            )
        ]
        records.append(record)
        _write_trade_safety_state_unlocked(
            {
                "halt_new_buys": False,
                "status": "ARENA_SOFT_STOPS_ACTIVE",
                "arena_soft_stops": records,
            },
            env=env,
        )


def _remove_arena_soft_stop(*, account_id: str, symbol: str, env: Any = os.environ) -> None:
    path = _trade_safety_state_path(env=env)
    with _json_state_lock(path):
        remaining = [
            item
            for item in _arena_soft_stop_records(env=env)
            if not (str(item.get("account_id") or "") == account_id and str(item.get("symbol") or "").upper() == symbol.upper())
        ]
        if remaining:
            _write_trade_safety_state_unlocked(
                {
                    "halt_new_buys": False,
                    "status": "ARENA_SOFT_STOPS_ACTIVE",
                    "arena_soft_stops": remaining,
                },
                env=env,
            )
        else:
            _clear_trade_safety_state_unlocked(env=env)


def _arena_stop_triggered(record: dict[str, Any], position: dict[str, Any]) -> bool:
    current = _decimal(
        _first_nested(position, "current_price")
        or _first_nested(position, "last_price")
        or _first_nested(position, "market_price")
        or _first_nested(position, "price")
    )
    stop_price = _decimal(record.get("stop_price"))
    side = str(record.get("side") or "").upper()
    if current is None or stop_price is None:
        return False
    if side.endswith("SELL"):
        return current <= stop_price
    if side.endswith("BUY"):
        return current >= stop_price
    return False


def _arena_exit_order_payload(record: dict[str, Any], quantity: Decimal) -> dict[str, Any]:
    side = "SIDE_BUY" if str(record.get("side") or "").upper().endswith("BUY") else "SIDE_SELL"
    return {
        "symbol": str(record.get("symbol") or "").upper(),
        "side": side,
        "quantity": {"value": decimal_payload(quantity, min_scale=1)},
    }


def _arena_verify_protective_stop_watching(orders_response: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    raw_orders = _orders_list(orders_response)
    symbol = proposal["symbol"]
    required_quantity = _decimal(proposal.get("quantity"))
    required_stop = _decimal(proposal.get("protective_stop", {}).get("stop_price"))
    required_side = "SIDE_BUY" if str(proposal.get("protective_stop", {}).get("side")).upper() == "BUY" else "SIDE_SELL"
    accepted_sides = {required_side, required_side.replace("SIDE_", "")}
    matching: list[dict[str, Any]] = []
    for order in raw_orders:
        if not isinstance(order, dict):
            continue
        order_symbol = _first_nested(order, "symbol") or _first_nested(order, "security_code")
        status = str(_first_nested(order, "status") or "").upper()
        side = str(_first_nested(order, "side") or "").upper()
        quantity = _decimal(_first_nested(order, "quantity_sl") or _first_nested(order, "quantity") or _first_nested(order, "balance"))
        stop_price = _decimal(_first_nested(order, "sl_price") or _first_nested(order, "stop_price") or _first_nested(order, "stop"))
        if order_symbol != symbol:
            continue
        if side not in accepted_sides and not side.endswith(required_side):
            continue
        if "WATCH" not in status and "ACTIVE" not in status:
            continue
        if quantity is None or required_quantity is None or quantity < required_quantity:
            continue
        if stop_price is None or required_stop is None or stop_price != required_stop:
            continue
        matching.append(order)
    return {
        "verified": bool(matching),
        "symbol": symbol,
        "required_side": required_side,
        "required_quantity": str(required_quantity) if required_quantity is not None else None,
        "required_stop": str(required_stop) if required_stop is not None else None,
        "matching_stops": matching,
        "orders_count": len(raw_orders),
    }


def _first_nested(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            item = value[key]
            if isinstance(item, dict) and "value" in item:
                return item["value"]
            return item
        for item in value.values():
            found = _first_nested(item, key)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = _first_nested(item, key)
            if found is not None:
                return found
    return None


def _asset_matches(response: dict[str, Any], *, query: str, mics: set[str], limit: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    limit = max(1, min(limit, 100))
    for asset in _assets_list(response):
        symbol = str(_first_nested(asset, "symbol") or "").upper()
        ticker = str(_first_nested(asset, "ticker") or "").upper()
        mic = str(_first_nested(asset, "mic") or "").upper()
        isin = str(_first_nested(asset, "isin") or "").upper()
        name = str(_first_nested(asset, "name") or "")
        haystack = " ".join([symbol, ticker, mic, isin, name.upper()])
        if mics and mic not in mics:
            continue
        if query not in haystack:
            continue
        matches.append(
            {
                "symbol": symbol,
                "ticker": ticker or None,
                "mic": mic or None,
                "isin": isin or None,
                "type": _first_nested(asset, "type"),
                "name": name or None,
                "is_archived": bool(_first_nested(asset, "is_archived")),
            }
        )
        if len(matches) >= limit:
            break
    return matches


def _asset_probe_matches(client: Any, jwt: str | None, *, query: str, mics: set[str], limit: int) -> list[dict[str, Any]]:
    if not jwt:
        return []
    matches: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for symbol in _asset_probe_symbols(query, mics):
        if len(matches) >= max(1, min(limit, 100)):
            break
        try:
            response = client.bars(
                jwt,
                symbol,
                interval="TIME_FRAME_H4",
                start_time=(now - timedelta(days=21)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                end_time=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
        except Exception:
            continue
        if not isinstance(response.get("bars"), list) or not response["bars"]:
            continue
        ticker, mic = symbol.split("@", maxsplit=1)
        matches.append(
            {
                "symbol": symbol,
                "ticker": ticker,
                "mic": mic,
                "isin": None,
                "type": "TYPE_STOCK",
                "name": None,
                "is_archived": False,
                "source": "market_data_probe",
            }
        )
    return matches


def _asset_probe_symbols(query: str, mics: set[str]) -> list[str]:
    raw = query.strip().upper()
    if "@" in raw:
        ticker, mic = raw.split("@", maxsplit=1)
        if ticker and mic and (not mics or mic in mics):
            return [f"{ticker}@{mic}"]
        return []
    ticker = raw.split()[0] if raw.split() else raw
    if not ticker:
        return []
    probe_mics = tuple(sorted(mics)) if mics else ARENA_ASSET_PROBE_MICS
    symbols = [f"{ticker}@{mic}" for mic in probe_mics]
    if not mics or "MISX" in mics:
        symbols.append(f"{ticker}-RM@MISX")
    return list(dict.fromkeys(symbols))


def _assets_list(response: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("assets", "items", "securities"):
        items = response.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _arena_check_stops_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str = "",
    symbol: str = "",
    live: bool,
    client: Any | None = None,
    env: Any = os.environ,
    execute_triggered_stops: bool = False,
) -> dict[str, Any]:
    command = "arena-execute-triggered-stops" if execute_triggered_stops else "arena-check-stops"
    normalized_account = str(account_id or "").strip()
    normalized_symbol = str(symbol or "").strip().upper()
    records = [
        item
        for item in _arena_soft_stop_records(env=env)
        if (not normalized_account or str(item.get("account_id") or "") == normalized_account)
        and (not normalized_symbol or str(item.get("symbol") or "").upper() == normalized_symbol)
    ]
    if not records and client is not None and not hasattr(client, "get_account"):
        return {
            "status": "NO_STOPS",
            "command": "arena-check-stops",
            "policy_path": str(policy_path),
            "account_id": normalized_account or None,
            "symbol": normalized_symbol or None,
            "live_requested": live,
            "checks": [],
            "safety": _safety_payload(),
        }
    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    secret = str(env.get(secret_env) or "").strip()
    if not secret:
        if not records:
            return {
                "status": "NO_STOPS",
                "command": "arena-check-stops",
                "policy_path": str(policy_path),
                "account_id": normalized_account or None,
                "symbol": normalized_symbol or None,
                "live_requested": live,
                "checks": [],
                "safety": _safety_payload(),
            }
        return {
            "status": "NO_TRADE",
            "command": "arena-check-stops",
            "policy_path": str(policy_path),
            "reason": f"{secret_env}_not_set",
            "live_requested": live,
            "checks": records,
            "safety": _safety_payload(),
        }
    if live and execute_triggered_stops:
        auto_trade_env = str(policy.get("auto_trade_env") or "FINAM_ARENA_AUTO_TRADE_ENABLED")
        if str(env.get(auto_trade_env, "")).strip().lower() != "true":
            return {
                "status": "LIVE_GATE_REQUIRED",
                "command": "arena-check-stops",
                "policy_path": str(policy_path),
                "reason": f"{auto_trade_env}_not_true",
                "required_env": auto_trade_env,
                "live_requested": live,
                "checks": records,
                "safety": _safety_payload(),
            }

    client = client or FinamClient(base_url=arena_base_url(policy, env=env))
    try:
        jwt = client.create_session(secret)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "BROKER_ERROR",
            "command": "arena-check-stops",
            "policy_path": str(policy_path),
            "reason": "arena_session_create_failed",
            "error": str(exc),
            "live_requested": live,
            "checks": records,
            "safety": _safety_payload(trading_mutations=False),
        }

    checks: list[dict[str, Any]] = []
    triggered: list[dict[str, Any]] = []
    checked_accounts: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for record in records:
        record_account = str(record.get("account_id") or "")
        record_symbol = str(record.get("symbol") or "").upper()
        try:
            account = checked_accounts.get(record_account)
            if account is None:
                account = client.get_account(jwt, record_account)
                _enrich_arena_account_position_prices([account], warnings=warnings, env=env)
                checked_accounts[record_account] = account
        except Exception as exc:  # noqa: BLE001
            checks.append(record | {"check_status": "ERROR", "error": str(exc)})
            continue
        position = _arena_position(account, record_symbol)
        if position is None:
            if execute_triggered_stops:
                _remove_arena_soft_stop(account_id=record_account, symbol=record_symbol, env=env)
                checks.append(record | {"check_status": "NO_POSITION", "soft_stop_removed": True})
            else:
                checks.append(record | {"check_status": "NO_POSITION", "would_remove_soft_stop": True})
            continue
        position_qty = _arena_position_quantity(account, record_symbol)
        expected_stop_side = _arena_expected_stop_side(position)
        record_qty = _decimal(record.get("quantity"))
        record_side = str(record.get("side") or "").upper()
        coverage_base = record | {
            "position_quantity": decimal_payload(position_qty, min_scale=1),
            "soft_stop_quantity": decimal_payload(record_qty, min_scale=1) if record_qty is not None else None,
            "expected_stop_side": expected_stop_side,
            "required_recovery_confirmation": f"CONFIRM_ARENA_RECOVER {record_symbol} {record_account}",
        }
        if expected_stop_side and record_side != expected_stop_side:
            checks.append(coverage_base | {"check_status": "SOFT_STOP_SIDE_MISMATCH", "coverage_status": "side_mismatch", "reason": "arena_soft_stop_side_mismatch"})
            continue
        if record_qty is None or record_qty < position_qty:
            checks.append(coverage_base | {"check_status": "SOFT_STOP_UNDER_COVERED", "coverage_status": "under_covered", "reason": "arena_soft_stop_under_covered"})
            continue
        is_triggered = _arena_stop_triggered(record, position)
        check = coverage_base | {
            "check_status": "TRIGGERED" if is_triggered else "ACTIVE",
            "coverage_status": "ok",
            "current_price": str(
                _first_nested(position, "current_price")
                or _first_nested(position, "last_price")
                or _first_nested(position, "market_price")
                or _first_nested(position, "price")
                or ""
            )
            or None,
        }
        if not is_triggered:
            checks.append(check)
            continue
        if not (live and execute_triggered_stops):
            checks.append(check)
            triggered.append(check)
            continue
        with _arena_stop_execution_lock(env=env):
            active_record = _active_arena_soft_stop_record(
                account_id=record_account,
                symbol=record_symbol,
                env=env,
            )
            if active_record is None:
                checks.append(
                    check
                    | {
                        "check_status": "SOFT_STOP_ALREADY_HANDLED",
                        "soft_stop_already_removed": True,
                    }
                )
                continue
            if not _arena_stop_triggered(active_record, position):
                checks.append(
                    check
                    | {
                        "check_status": "ACTIVE_AFTER_RECHECK",
                        "soft_stop_rechecked_under_execution_lock": True,
                    }
                )
                continue
            exit_payload = _arena_exit_order_payload(active_record, position_qty)
            try:
                exit_response = client.place_order(jwt, record_account, exit_payload)
            except Exception as exc:  # noqa: BLE001
                output = {
                    "status": "BROKER_ERROR",
                    "command": "arena-check-stops",
                    "policy_path": str(policy_path),
                    "account_id": record_account,
                    "symbol": record_symbol,
                    "reason": "arena_soft_stop_exit_failed",
                    "error": str(exc),
                    "soft_stop": active_record,
                    "exit_order_payload": exit_payload,
                    "halt_new_entries": True,
                    "safety": _safety_payload(trading_mutations=True),
                }
                _write_trade_safety_state(output, env=env)
                return output
            _remove_arena_soft_stop(account_id=record_account, symbol=record_symbol, env=env)
            executed = check | {
                "check_status": "EXIT_SUBMITTED",
                "exit_order_payload": exit_payload,
                "exit_order_response": exit_response,
                "soft_stop_removed": True,
            }
            ledger_run = {
                "status": "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
                "command": command,
                "policy_path": str(policy_path),
                "account_id": record_account,
                "symbol": record_symbol,
                "action": "SOFT_STOP_TRIGGERED",
                "exit_order_payload": exit_payload,
                "exit_order_response": exit_response,
                "broker_mutation": True,
                "safety": _safety_payload(trading_mutations=True),
            }
            _record_arena_execution_ledger(ledger_run, policy=policy, env=env)
            if ledger_run.get("execution_ledger_delivery"):
                executed["execution_ledger_delivery"] = ledger_run.get("execution_ledger_delivery")
            if ledger_run.get("execution_ledger_warning"):
                executed["execution_ledger_warning"] = ledger_run.get("execution_ledger_warning")
            checks.append(executed)
            triggered.append(executed)

    missing_checks = _arena_missing_soft_stop_checks(
        policy,
        client=client,
        jwt=jwt,
        records=records,
        account_id=normalized_account,
        symbol=normalized_symbol,
        account_cache=checked_accounts,
    )
    checks.extend(missing_checks)

    if triggered and live and execute_triggered_stops:
        output = {
            "status": "ARENA_SOFT_STOP_TRIGGERED",
            "command": "arena-check-stops",
            "policy_path": str(policy_path),
            "live_requested": live,
            "checks": checks,
            "triggered": triggered,
            "safety": _safety_payload(trading_mutations=True),
        }
        if warnings:
            output["warnings"] = warnings
        return output
    if triggered:
        output = {
            "status": "STOP_TRIGGERED_DRY_RUN",
            "command": "arena-check-stops",
            "policy_path": str(policy_path),
            "live_requested": live,
            "checks": checks,
            "triggered": triggered,
            "safety": _safety_payload(),
        }
        if warnings:
            output["warnings"] = warnings
        return output
    protection_halts = [check for check in checks if str(check.get("check_status") or "") in ARENA_PROTECTION_HALT_STATUSES]
    if protection_halts:
        reason = str(protection_halts[0].get("reason") or _arena_protection_halt_reason(str(protection_halts[0].get("check_status") or "")))
        output = {
            "status": str(protection_halts[0].get("check_status") or "HALT"),
            "command": "arena-check-stops",
            "policy_path": str(policy_path),
            "reason": reason,
            "live_requested": live,
            "checks": checks,
            "safety": _safety_payload(),
        }
        if warnings:
            output["warnings"] = warnings
        return output
    if any(check.get("check_status") == "ERROR" for check in checks):
        output = {
            "status": "DEGRADED",
            "command": "arena-check-stops",
            "policy_path": str(policy_path),
            "live_requested": live,
            "checks": checks,
            "safety": _safety_payload(),
        }
        if warnings:
            output["warnings"] = warnings
        return output
    output = {
        "status": "OK",
        "command": "arena-check-stops",
        "policy_path": str(policy_path),
        "live_requested": live,
        "checks": checks,
        "safety": _safety_payload(),
    }
    if warnings:
        output["warnings"] = warnings
    return output


def _arena_missing_soft_stop_checks(
    policy: dict[str, Any],
    *,
    client: Any,
    jwt: str,
    records: list[dict[str, Any]],
    account_id: str,
    symbol: str,
    account_cache: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    record_keys = {
        (str(record.get("account_id") or ""), str(record.get("symbol") or "").upper())
        for record in records
        if isinstance(record, dict)
    }
    account_ids = [account_id] if account_id else _arena_account_ids(policy)
    checks: list[dict[str, Any]] = []
    for current_account_id in account_ids:
        if not current_account_id:
            continue
        try:
            account = account_cache.get(current_account_id)
            if account is None:
                account = client.get_account(jwt, current_account_id)
                account_cache[current_account_id] = account
        except Exception as exc:  # noqa: BLE001
            checks.append({"account_id": current_account_id, "symbol": symbol or None, "check_status": "ERROR", "error": str(exc)})
            continue
        positions = account.get("positions") if isinstance(account.get("positions"), list) else []
        for position in positions:
            if not isinstance(position, dict):
                continue
            position_symbol = str(_first_nested(position, "symbol") or _first_nested(position, "security_code") or _first_nested(position, "ticker") or "").upper()
            if not position_symbol or (symbol and position_symbol != symbol):
                continue
            position_qty = _arena_position_quantity(account, position_symbol)
            if position_qty <= 0 or (current_account_id, position_symbol) in record_keys:
                continue
            checks.append(
                {
                    "account_id": current_account_id,
                    "symbol": position_symbol,
                    "check_status": "MISSING_SOFT_STOP",
                    "coverage_status": "missing",
                    "reason": "arena_soft_stop_missing_for_position",
                    "position_quantity": decimal_payload(position_qty, min_scale=1),
                    "expected_stop_side": _arena_expected_stop_side(position),
                    "required_recovery_confirmation": f"CONFIRM_ARENA_RECOVER {position_symbol} {current_account_id}",
                }
            )
    return checks


def _arena_expected_stop_side(position: dict[str, Any]) -> str:
    raw_quantity = _decimal(_first_nested(position, "quantity") or _first_nested(position, "balance") or _first_nested(position, "qty"))
    if raw_quantity is not None and raw_quantity < 0:
        return "BUY"
    side = str(_first_nested(position, "side") or _first_nested(position, "position_side") or "").upper()
    if "SHORT" in side or side.endswith("SELL"):
        return "BUY"
    return "SELL"


def _arena_protection_halt_reason(status: str) -> str:
    return {
        "MISSING_SOFT_STOP": "arena_soft_stop_missing_for_position",
        "SOFT_STOP_UNDER_COVERED": "arena_soft_stop_under_covered",
        "SOFT_STOP_SIDE_MISMATCH": "arena_soft_stop_side_mismatch",
    }.get(status, "arena_soft_stop_unverified")


def _arena_recover_protection_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
    symbol: str,
    live: bool,
    confirmation: str = "",
    client: Any | None = None,
    env: Any = os.environ,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if account_id not in _arena_account_ids(policy):
        return {
            "status": "INVALID",
            "command": "arena-recover-protection",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": normalized_symbol,
            "reason": "account_not_allowed",
            "allowed_accounts": _arena_account_ids(policy),
            "safety": _safety_payload(),
        }
    safety_state = _read_trade_safety_state(env=env)
    stop_price = _arena_recovery_stop_price(safety_state, account_id=account_id, symbol=normalized_symbol)
    stop_side = _arena_recovery_stop_side(safety_state) or "SELL"
    if stop_price is None:
        return {
            "status": "MANUAL_PROTECTION_REQUIRED",
            "command": "arena-recover-protection",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": normalized_symbol,
            "reason": "protective_stop_price_unavailable",
            "safety_state": safety_state,
            "live_requested": live,
            "safety": _safety_payload(),
        }

    secret_env = str(policy.get("session_secret_env") or "FINAM_ARENA_API")
    secret = str(env.get(secret_env) or "").strip()
    if not secret:
        return {
            "status": "NO_TRADE",
            "command": "arena-recover-protection",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": normalized_symbol,
            "reason": f"{secret_env}_not_set",
            "safety": _safety_payload(),
        }
    client = client or FinamClient(base_url=arena_base_url(policy, env=env))
    try:
        jwt = client.create_session(secret)
        account = client.get_account(jwt, account_id)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "MANUAL_PROTECTION_REQUIRED",
            "command": "arena-recover-protection",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": normalized_symbol,
            "reason": "arena_recovery_read_failed",
            "error": str(exc),
            "safety_state": safety_state,
            "live_requested": live,
            "safety": _safety_payload(),
        }

    quantity = _arena_position_quantity(account, normalized_symbol)
    if quantity <= 0:
        _clear_trade_safety_state(env=env)
        return {
            "status": "NO_POSITION",
            "command": "arena-recover-protection",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": normalized_symbol,
            "reason": "position_not_found",
            "safety_state_cleared": True,
            "safety": _safety_payload(),
        }

    existing = next(
        (
            item
            for item in _arena_soft_stop_records(env=env)
            if str(item.get("account_id") or "") == account_id and str(item.get("symbol") or "").upper() == normalized_symbol
        ),
        None,
    )
    protected_proposal = {
        "symbol": normalized_symbol,
        "quantity": quantity,
        "protective_stop": {"side": stop_side, "stop_price": stop_price},
    }
    if existing and _arena_soft_stop_covers_position(existing, position_quantity=quantity, stop_side=stop_side, stop_price=stop_price):
        return {
            "status": "PROTECTED_SOFT",
            "command": "arena-recover-protection",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": normalized_symbol,
            "position_quantity": decimal_payload(quantity, min_scale=1),
            "soft_stop": existing,
            "stop_protection_mode": "arena_soft_stop",
            "broker_mutation": False,
            "safety": _safety_payload(),
        }

    protective_stop_payload = {
        "mode": "arena_soft_stop",
        "symbol": normalized_symbol,
        "side": stop_side,
        "quantity": {"value": decimal_payload(quantity, min_scale=1)},
        "stop_price": {"value": str(stop_price)},
    }
    manual_payload = {
        "account_id": account_id,
        "symbol": normalized_symbol,
        "side": stop_side,
        "quantity": decimal_payload(quantity, min_scale=1),
        "stop_price": str(stop_price),
    }
    required_confirmation = f"CONFIRM_ARENA_RECOVER {normalized_symbol} {account_id}"
    if not live:
        return {
            "status": "SOFT_PROTECTION_REQUIRED",
            "command": "arena-recover-protection",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": normalized_symbol,
            "reason": "arena_soft_stop_under_covered" if existing else "arena_native_sltp_unavailable_soft_stop_not_active",
            "existing_soft_stop": existing,
            "manual_protection": manual_payload,
            "protective_stop_payload": protective_stop_payload,
            "required_confirmation": required_confirmation,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    if confirmation != required_confirmation:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "command": "arena-recover-protection",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "symbol": normalized_symbol,
            "required_confirmation": required_confirmation,
            "received_confirmation": confirmation,
            "manual_protection": manual_payload,
            "live_requested": live,
            "safety": _safety_payload(),
        }
    soft_stop = _arena_soft_stop_record(
        account_id=account_id,
        proposal=protected_proposal,
        entry_result={},
        fill_state={"executed_quantity": decimal_payload(quantity, min_scale=1)},
    )
    _upsert_arena_soft_stop(soft_stop, env=env)
    return {
        "status": "PROTECTED_SOFT",
        "command": "arena-recover-protection",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "symbol": normalized_symbol,
        "position_quantity": decimal_payload(quantity, min_scale=1),
        "protective_stop_payload": protective_stop_payload,
        "soft_stop": soft_stop,
        "stop_protection_mode": "arena_soft_stop",
        "broker_mutation": False,
        "safety": _safety_payload(),
    }


def _arena_recovery_stop_price(safety_state: dict[str, Any], *, account_id: str, symbol: str) -> str | None:
    if str(safety_state.get("account_id") or "") not in {"", account_id}:
        return None
    proposal = safety_state.get("proposal") if isinstance(safety_state.get("proposal"), dict) else {}
    trade_proposal = proposal.get("proposal") if isinstance(proposal.get("proposal"), dict) else {}
    if trade_proposal and str(trade_proposal.get("symbol") or "").upper() != symbol:
        return None
    stop = trade_proposal.get("protective_stop") if isinstance(trade_proposal.get("protective_stop"), dict) else {}
    value = stop.get("stop_price") or _first_nested(safety_state.get("protective_stop_payload") or {}, "sl_price")
    return str(value) if value is not None else None


def _arena_recovery_stop_side(safety_state: dict[str, Any]) -> str | None:
    proposal = safety_state.get("proposal") if isinstance(safety_state.get("proposal"), dict) else {}
    trade_proposal = proposal.get("proposal") if isinstance(proposal.get("proposal"), dict) else {}
    stop = trade_proposal.get("protective_stop") if isinstance(trade_proposal.get("protective_stop"), dict) else {}
    side = str(stop.get("side") or _first_nested(safety_state.get("protective_stop_payload") or {}, "side") or "").upper()
    if side.endswith("BUY"):
        return "BUY"
    if side.endswith("SELL"):
        return "SELL"
    return None


def _arena_soft_stop_covers_position(
    record: dict[str, Any],
    *,
    position_quantity: Decimal,
    stop_side: str,
    stop_price: str,
) -> bool:
    record_quantity = _decimal(record.get("quantity"))
    record_side = str(record.get("side") or "").upper()
    record_stop = _decimal(record.get("stop_price"))
    required_stop = _decimal(stop_price)
    if record_quantity is None or record_quantity < position_quantity:
        return False
    if record_side != str(stop_side or "").upper():
        return False
    if required_stop is not None and record_stop != required_stop:
        return False
    return True


def _arena_position_quantity(account: dict[str, Any], symbol: str) -> Decimal:
    position = _arena_position(account, symbol)
    if position is None:
        return Decimal("0")
    quantity = _decimal(_first_nested(position, "quantity") or _first_nested(position, "balance") or _first_nested(position, "qty"))
    return abs(quantity) if quantity is not None else Decimal("0")


def _arena_position(account: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    positions = account.get("positions") if isinstance(account.get("positions"), list) else []
    for position in positions:
        if not isinstance(position, dict):
            continue
        position_symbol = str(_first_nested(position, "symbol") or _first_nested(position, "security_code") or _first_nested(position, "ticker") or "")
        if position_symbol == symbol:
            return position
    return None


def _arena_run_all_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    live: bool,
    market_client: Any | None = None,
    research_mode: str = "cache_only",
    scan: dict[str, Any] | None = None,
    include_scan: bool = False,
) -> dict[str, Any]:
    accounts = _arena_account_ids(policy)
    runs: list[dict[str, Any]] = []
    stop_check = _arena_check_stops_output(policy, policy_path=policy_path, live=live, execute_triggered_stops=live)
    if stop_check.get("status") not in {"NO_STOPS", "OK"}:
        runs.append(stop_check)
        if live and (str(stop_check.get("status") or "") in {"ARENA_SOFT_STOP_TRIGGERED", "BROKER_ERROR", "LIVE_GATE_REQUIRED"} or str(stop_check.get("status") or "") in ARENA_PROTECTION_HALT_STATUSES):
            status = _arena_run_all_status(runs)
            return {
                "status": status,
                "command": "arena-run-all",
                "policy_path": str(policy_path),
                "live_requested": live,
                "runs": runs,
                "safety": _safety_payload(trading_mutations=any((run.get("safety") or {}).get("trading_mutations") for run in runs)),
            }
    if scan is None:
        ledger = _read_arena_execution_ledger(env=os.environ)
        scan = build_arena_scan(
            policy,
            soft_stops=_arena_soft_stop_records(),
            market_client=market_client,
            research_mode=research_mode,
            execution_ledger=ledger,
        )
    portfolio_run = _arena_portfolio_run_output(
        policy,
        policy_path=policy_path,
        live=live,
        market_client=market_client,
        research_mode=research_mode,
        scan=scan,
    )
    if portfolio_run.get("status") != "NO_ACTION":
        runs.append(portfolio_run)
    if live and str(portfolio_run.get("status") or "") in {"HALT", "LIVE_GATE_REQUIRED", "CONFIRMATION_REQUIRED", "WAIT_MARKET_CLOSED"} | ARENA_UNRESOLVED_EXECUTION_STATUSES:
        status = _arena_run_all_status(runs)
        return {
            "status": status,
            "command": "arena-run-all",
            "policy_path": str(policy_path),
            "live_requested": live,
            "runs": runs,
                "safety": _safety_payload(trading_mutations=any((run.get("safety") or {}).get("trading_mutations") for run in runs)),
            }
    if live and bool(portfolio_run.get("broker_mutation")):
        status = _arena_run_all_status(runs)
        return {
            "status": status,
            "command": "arena-run-all",
            "policy_path": str(policy_path),
            "live_requested": live,
            "reason": "portfolio_mutation_completed_new_entries_deferred",
            "runs": runs,
            "safety": _safety_payload(trading_mutations=any((run.get("safety") or {}).get("trading_mutations") for run in runs)),
        }
    for account_id in accounts:
        run = _arena_run_output(
            policy,
            policy_path=policy_path,
            account_id=account_id,
            live=live,
            market_client=market_client,
            research_mode=research_mode,
            scan=scan,
        )
        runs.append(run)
        if live and str(run.get("status") or "") in ARENA_UNRESOLVED_EXECUTION_STATUSES:
            break
    status = _arena_run_all_status(runs)
    output = {
        "status": status,
        "command": "arena-run-all",
        "policy_path": str(policy_path),
        "live_requested": live,
        "runs": runs,
        "safety": _safety_payload(trading_mutations=any((run.get("safety") or {}).get("trading_mutations") for run in runs)),
    }
    if include_scan:
        output["scan"] = scan
    return output


def _arena_run_all_status(runs: list[dict[str, Any]]) -> str:
    statuses = {str(run.get("status") or "") for run in runs}
    if statuses & ARENA_UNRESOLVED_EXECUTION_STATUSES:
        return "HALT"
    if "WAIT_MARKET_CLOSED" in statuses:
        return "WAIT_MARKET_CLOSED"
    if "ARENA_SOFT_STOP_TRIGGERED" in statuses:
        return "ARENA_SOFT_STOP_TRIGGERED"
    if "EXECUTED_ARENA_PORTFOLIO" in statuses or "EXECUTED_ARENA_REPLACEMENT" in statuses or "EXECUTED_ARENA_EXIT_CASH" in statuses:
        return "EXECUTED_ARENA_PORTFOLIO"
    if statuses & ARENA_EXECUTED_STATUSES:
        return "EXECUTED_ARENA"
    if "CONFIRMATION_REQUIRED" in statuses:
        return "CONFIRMATION_REQUIRED"
    if "LIVE_GATE_REQUIRED" in statuses:
        return "LIVE_GATE_REQUIRED"
    if "HALT" in statuses:
        return "HALT"
    if "BLOCKED" in statuses:
        return "BLOCKED"
    if "DRY_RUN" in statuses:
        return "DRY_RUN"
    return "NO_ORDER"


def _arena_growth_loop_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    apply: bool,
    daily: bool,
    max_reviews: int,
    review_timeout_seconds: int,
    env: Any = os.environ,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state_path = _arena_growth_loop_state_path(env=env)
    audit_path = _arena_growth_loop_audit_path(env=env)
    state = _read_json_object(state_path)
    today = now.astimezone(ARENA_MSK_TZ).date().isoformat()
    policy_apply_locked = bool(daily and state.get("last_policy_apply_date_msk") == today)

    ledger = _read_arena_execution_ledger(env=env)
    scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(env=env), research_mode="budgeted", execution_ledger=ledger)
    reviews = _arena_growth_codex_review_step(
        policy,
        scan=scan,
        max_reviews=max(0, max_reviews),
        timeout_seconds=max(5, review_timeout_seconds),
        apply=apply,
        env=env,
    )

    try:
        news_scan = _arena_news_scan_output(
            policy,
            policy_path=policy_path,
            event_path=_arena_event_candidates_path(env=env),
            limit=20,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - growth loop must fail closed and continue other guarded steps.
        news_scan = {"status": "FAILED", "command": "arena-news-scan", "error": str(exc), "safety": _safety_payload()}
    try:
        news_proposal = _arena_news_propose_output(
            policy,
            policy_path=policy_path,
            event_path=_arena_event_candidates_path(env=env),
            limit=20,
        )
    except Exception as exc:  # noqa: BLE001
        news_proposal = {"status": "FAILED", "command": "arena-news-propose", "error": str(exc), "safety": _safety_payload()}

    current_policy = policy
    policy_writes: list[dict[str, Any]] = []
    if policy_apply_locked:
        learning = {
            "status": "BLOCKED",
            "command": "arena-growth-learning",
            "reason": "daily_policy_apply_already_done",
            "date_msk": today,
            "safety": _safety_payload(),
        }
    else:
        learning = _arena_growth_learning_step(current_policy, policy_path=policy_path, apply=apply)
        if learning.get("write_applied"):
            policy_writes.append({"step": "learning", "status": learning.get("status"), "backup_path": learning.get("backup_path")})
            current_policy = learning.get("applied_policy") if isinstance(learning.get("applied_policy"), dict) else load_arena_policy(policy_path)

    if policy_apply_locked and not policy_writes:
        universe = {
            "status": "BLOCKED",
            "command": "arena-growth-universe",
            "reason": "daily_policy_apply_already_done",
            "date_msk": today,
            "accounts": [],
            "safety": _safety_payload(),
        }
    else:
        universe = _arena_growth_universe_step(current_policy, policy_path=policy_path, apply=apply, env=env)
        if universe.get("write_applied"):
            policy_writes.extend(
                {"step": "universe", "account_id": item.get("account_id"), "status": item.get("status"), "backup_path": item.get("backup_path")}
                for item in universe.get("accounts") or []
                if item.get("write_applied")
            )

    write_applied = bool(policy_writes)
    output = {
        "status": _arena_growth_loop_status(reviews, learning, universe, write_applied=write_applied),
        "command": "arena-growth-loop",
        "mode": "apply" if apply else "dry_run",
        "date_msk": today,
        "policy_path": str(policy_path),
        "scan_status": scan.get("status"),
        "codex_reviews": reviews,
        "news_scan": news_scan,
        "news_proposal": news_proposal,
        "learning": learning,
        "universe": universe,
        "write_applied": write_applied,
        "policy_writes": policy_writes,
        "broker_mutation": False,
        "safety": _safety_payload(policy_write=write_applied),
    }
    _append_arena_growth_audit(audit_path, output, now=now)
    if apply:
        next_state = dict(state)
        next_state.update(
            {
                "last_run_at": now.isoformat(timespec="seconds"),
                "last_run_date_msk": today,
                "last_status": output["status"],
                "last_audit_path": str(audit_path),
            }
        )
        if write_applied:
            next_state["last_policy_apply_at"] = now.isoformat(timespec="seconds")
            next_state["last_policy_apply_date_msk"] = today
            next_state["last_policy_writes"] = policy_writes
        _write_json_file(state_path, next_state)
        output["state_path"] = str(state_path)
    output["audit_path"] = str(audit_path)
    return output


def _arena_growth_loop_status(
    reviews: dict[str, Any],
    learning: dict[str, Any],
    universe: dict[str, Any],
    *,
    write_applied: bool,
) -> str:
    failed_statuses = {"FAILED", "INVALID", "BLOCKED"}
    if write_applied or reviews.get("records_written"):
        return "APPLIED"
    if str(reviews.get("status") or "") in failed_statuses:
        return "FAIL_CLOSED"
    if str(learning.get("status") or "") in failed_statuses and str(learning.get("reason") or "") != "daily_policy_apply_already_done":
        return "FAIL_CLOSED"
    if str(universe.get("status") or "") in failed_statuses and str(universe.get("reason") or "") != "daily_policy_apply_already_done":
        return "FAIL_CLOSED"
    return "DRY_RUN" if not write_applied else "APPLIED"


def _arena_growth_codex_review_step(
    policy: dict[str, Any],
    *,
    scan: dict[str, Any],
    max_reviews: int,
    timeout_seconds: int,
    apply: bool,
    env: Any,
) -> dict[str, Any]:
    candidates = _arena_growth_review_candidates(policy, scan, max_reviews=max_reviews)
    records: list[dict[str, Any]] = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").upper()
        context_output = _codex_review_context_output(_arena_scan_as_review_report(scan, candidate), policy=policy, symbol=symbol)
        if context_output.get("status") != "OK":
            records.append({"symbol": symbol, "status": "BLOCKED", "reason": "context_unavailable", "context_status": context_output.get("status")})
            continue
        context = context_output["context"]
        existing = _load_fresh_codex_review(symbol, str(context.get("context_hash") or ""), policy)
        if existing is not None:
            records.append(
                {
                    "symbol": symbol,
                    "status": "EXISTS",
                    "context_hash": context.get("context_hash"),
                    "verdict": (existing.get("review") or {}).get("verdict"),
                    "path": existing.get("_path"),
                }
            )
            continue
        if not apply:
            records.append({"symbol": symbol, "status": "PLANNED", "context_hash": context.get("context_hash")})
            continue
        generated = _run_codex_review_command(context, timeout_seconds=timeout_seconds, env=env)
        if generated.get("status") != "OK":
            records.append({"symbol": symbol, "status": "FAIL_CLOSED", "reason": generated.get("reason"), "error": generated.get("error")})
            continue
        record = _codex_review_record_output(
            _arena_scan_as_review_report(scan, candidate),
            policy=policy,
            symbol=symbol,
            review_json=str(generated["review_json"]),
            confirm=CODEX_REVIEW_CONFIRM,
        )
        records.append(
            {
                "symbol": symbol,
                "status": record.get("status"),
                "context_hash": record.get("context_hash") or context.get("context_hash"),
                "review": record.get("review"),
                "path": record.get("path"),
                "error": record.get("error"),
            }
        )
    return {
        "status": "OK",
        "command": "arena-growth-codex-review",
        "candidate_count": len(candidates),
        "records_written": len([item for item in records if item.get("status") == "OK"]),
        "records": records,
        "safety": _safety_payload(),
    }


def _arena_executor_codex_review_step(
    policy: dict[str, Any],
    *,
    scan: dict[str, Any] | None,
    live: bool,
    env: Any,
) -> dict[str, Any]:
    if not live:
        return {"status": "SKIPPED", "command": "arena-executor-codex-review", "reason": "not_live"}
    if not isinstance(scan, dict) or not scan.get("candidates"):
        return {"status": "SKIPPED", "command": "arena-executor-codex-review", "reason": "no_scan_candidates"}
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    max_reviews = _positive_int(
        research.get("arena_executor_codex_review_max_reviews")
        or research.get("arena_h4_max_symbols_per_scan")
        or research.get("h4_max_candidates"),
        default=3,
    )
    timeout_seconds = _positive_int(
        research.get("arena_executor_codex_review_timeout_seconds") or research.get("timeout_seconds"),
        default=70,
    )
    result = _arena_growth_codex_review_step(
        policy,
        scan=scan,
        max_reviews=max_reviews,
        timeout_seconds=max(5, timeout_seconds),
        apply=True,
        env=env,
    )
    return dict(result) | {"command": "arena-executor-codex-review"}


def _arena_scan_with_codex_reviews(policy: dict[str, Any], scan: dict[str, Any] | None, reviews: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(scan, dict) or not isinstance(reviews, dict):
        return scan
    review_by_symbol: dict[str, dict[str, Any]] = {}
    for record in reviews.get("records") or []:
        if not isinstance(record, dict):
            continue
        symbol = str(record.get("symbol") or "").upper()
        if not symbol:
            continue
        verdict = _codex_review_record_verdict(record)
        if verdict:
            review_by_symbol[symbol] = dict(record) | {"verdict": verdict}
    if not review_by_symbol:
        return scan
    updated = copy.deepcopy(scan)
    changed = False
    for candidate in updated.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        review = review_by_symbol.get(str(candidate.get("symbol") or "").upper())
        if not review:
            continue
        if _apply_codex_review_to_candidate(policy, candidate, review=review):
            changed = True
    if changed:
        updated["codex_review_status"] = {
            "status": reviews.get("status"),
            "records_written": reviews.get("records_written"),
            "records": reviews.get("records") or [],
        }
    return updated


def _codex_review_record_verdict(record: dict[str, Any]) -> str:
    review = record.get("review") if isinstance(record.get("review"), dict) else {}
    verdict = str(review.get("verdict") or record.get("verdict") or "").upper()
    return verdict if verdict in CODEX_REVIEW_VERDICTS else ""


def _apply_codex_review_to_candidate(policy: dict[str, Any], candidate: dict[str, Any], *, review: dict[str, Any]) -> bool:
    verdict = str(review.get("verdict") or "").upper()
    if verdict not in CODEX_REVIEW_VERDICTS:
        return False
    gates = [str(item) for item in candidate.get("gate_reasons") or [] if str(item)]
    gates = [item for item in gates if item != "research_unavailable_below_exceptional_score"]
    candidate["codex_review"] = {
        "status": review.get("status"),
        "verdict": verdict,
        "context_hash": review.get("context_hash"),
        "path": review.get("path"),
    }
    score_payload = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    components = score_payload.get("components") if isinstance(score_payload.get("components"), dict) else {}
    if verdict == "OK":
        previous_research = int(components.get("research_regime") or 0)
        components["research_regime"] = 15
        score = max(0, min(100, int(score_payload.get("score") or 0) + 15 - previous_research))
        score_payload["score"] = score
        score_payload["label"] = "HIGH" if score >= 75 else ("MEDIUM" if score >= 60 else "LOW")
        score_payload["research_verdict"] = "OK"
        score_payload["components"] = components
        candidate["arena_growth_score"] = score_payload
    elif verdict == "RISK":
        if "research_risk_requires_manual_review" not in gates:
            gates.insert(0, "research_risk_requires_manual_review")
        score_payload["research_verdict"] = "RISK"
        candidate["arena_growth_score"] = score_payload
    elif verdict == "AVOID":
        if "research_avoid" not in gates:
            gates.insert(0, "research_avoid")
        score_payload["research_verdict"] = "AVOID"
        candidate["arena_growth_score"] = score_payload
    else:
        if "research_provider_returned_unavailable" not in gates:
            gates.insert(0, "research_provider_returned_unavailable")
        gates = [item for item in gates if item != "research_unavailable_below_exceptional_score"]
        score_payload["research_verdict"] = "UNAVAILABLE"
        candidate["arena_growth_score"] = score_payload
    account_id = str(candidate.get("account_id") or "")
    min_score = _arena_growth_min_score(policy, account_id=account_id)
    score_value = _arena_candidate_score_value(candidate)
    gates = [item for item in gates if item != "candidate_score_below_min"]
    if str(candidate.get("side") or "BUY").upper() == "BUY" and score_value < min_score:
        gates.append("candidate_score_below_min")
    candidate["gate_reasons"] = gates
    candidate["execution_allowed"] = not gates and str(candidate.get("side") or "BUY").upper() == "BUY"
    return True


def _arena_growth_review_candidates(policy: dict[str, Any], scan: dict[str, Any], *, max_reviews: int) -> list[dict[str, Any]]:
    if max_reviews <= 0:
        return []
    candidates = [item for item in scan.get("candidates") or [] if isinstance(item, dict)]
    selected: list[dict[str, Any]] = []
    seen_accounts: set[str] = set()
    for candidate in sorted(candidates, key=_arena_candidate_score_value, reverse=True):
        account_id = str(candidate.get("account_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        if not account_id or not symbol or account_id in seen_accounts:
            continue
        gates = {str(item) for item in candidate.get("gate_reasons") or []}
        if not gates or not gates.issubset(ARENA_GROWTH_REVIEW_GATES):
            continue
        score = _arena_candidate_score_value(candidate)
        min_score = _arena_growth_min_score(policy, account_id=account_id)
        if score < max(0, min_score - 10):
            continue
        selected.append(candidate)
        seen_accounts.add(account_id)
        if len(selected) >= max_reviews:
            break
    return selected


def _arena_scan_as_review_report(scan: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    account_id = str(candidate.get("account_id") or "")
    account = next(
        (item for item in scan.get("accounts") or [] if isinstance(item, dict) and str(item.get("account_id") or "") == account_id),
        {},
    )
    return dict(scan) | {"account": account, "candidates": [candidate]}


def _run_codex_review_command(context: dict[str, Any], *, timeout_seconds: int, env: Any) -> dict[str, Any]:
    command_text = str(env.get(ARENA_CODEX_REVIEW_COMMAND_ENV) or DEFAULT_ARENA_CODEX_REVIEW_COMMAND).strip()
    if not command_text:
        return {"status": "FAILED", "reason": "codex_review_command_empty"}
    try:
        command = shlex.split(command_text)
    except ValueError as exc:
        return {"status": "FAILED", "reason": "codex_review_command_invalid", "error": str(exc)}
    prompt = (
        "Return ONLY raw JSON with keys verdict, confidence, reasons, blocking_flags, summary, context_hash. "
        "Use verdict OK only when there are no material blocking flags. "
        "Use RISK for mixed but tradable risk, AVOID for hard negative, UNAVAILABLE when evidence is insufficient.\n"
        + _compact_json(context)
    )
    try:
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"status": "FAILED", "reason": "codex_review_command_not_found", "error": str(exc)}
    except subprocess.TimeoutExpired:
        return {"status": "FAILED", "reason": "codex_review_timeout"}
    if result.returncode != 0:
        return {
            "status": "FAILED",
            "reason": "codex_review_command_failed",
            "error": (result.stderr or result.stdout or "").strip()[:400],
        }
    review_json = _extract_codex_review_json(result.stdout)
    if review_json is None:
        return {"status": "FAILED", "reason": "codex_review_json_missing", "error": result.stdout.strip()[:400]}
    try:
        parsed = _parse_codex_review_json(review_json)
    except ValueError as exc:
        return {"status": "FAILED", "reason": "codex_review_json_invalid", "error": str(exc)}
    if parsed.get("verdict") == "OK" and int(parsed.get("confidence") or 0) < 70:
        return {"status": "FAILED", "reason": "codex_review_ok_confidence_too_low"}
    return {"status": "OK", "review_json": review_json}


def _extract_codex_review_json(stdout: str) -> str | None:
    raw = stdout.strip()
    if not raw:
        return None
    try:
        _parse_codex_review_json(raw)
        return raw
    except ValueError:
        pass
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        nested = _extract_codex_review_json_from_event(parsed)
        if nested is not None:
            return nested
    return None


def _extract_codex_review_json_from_event(value: Any) -> str | None:
    if isinstance(value, dict):
        if {"verdict", "confidence", "reasons", "blocking_flags", "summary"} <= set(value):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for key in ("review", "output_json"):
            nested = value.get(key)
            if isinstance(nested, dict):
                extracted = _extract_codex_review_json_from_event(nested)
                if extracted is not None:
                    return extracted
        for key in ("output_text", "text"):
            output = value.get(key)
            if isinstance(output, str):
                nested = _extract_codex_review_json(output)
                if nested is not None:
                    return nested
        for nested in value.values():
            extracted = _extract_codex_review_json_from_event(nested)
            if extracted is not None:
                return extracted
    elif isinstance(value, list):
        for item in value:
            extracted = _extract_codex_review_json_from_event(item)
            if extracted is not None:
                return extracted
    return None


def _arena_growth_learning_step(policy: dict[str, Any], *, policy_path: Path, apply: bool) -> dict[str, Any]:
    paths = _arena_learning_paths()
    proposal = build_learning_proposal(policy, read_jsonl(paths["decisions"]), read_jsonl(paths["outcomes"]))
    proposal = dict(proposal)
    if isinstance(proposal.get("report"), dict):
        proposal["report"] = _learning_report_with_research_budget(proposal["report"])
    guard = _arena_growth_learning_guard(proposal)
    if not guard["ok"]:
        return {
            "status": "BLOCKED",
            "command": "arena-growth-learning",
            "reason": guard["reason"],
            "proposal": proposal,
            "safety": _safety_payload(),
        }
    if not proposal.get("patch"):
        return {
            "status": "NO_CHANGE",
            "command": "arena-growth-learning",
            "reason": proposal.get("reason") or "empty_patch",
            "proposal": proposal,
            "safety": _safety_payload(),
        }
    if not apply:
        return {
            "status": "PLANNED",
            "command": "arena-growth-learning",
            "proposal": proposal,
            "rejected_auto_patch_keys": guard.get("rejected_top_keys") or [],
            "write_applied": False,
            "safety": _safety_payload(),
        }
    applied = _arena_learning_apply_output(
        policy,
        policy_path=policy_path,
        confirm="APPLY_ARENA_LEARNING",
        backup_dir=ROOT / "data" / "runtime" / "policy_backups" / "growth_loop",
    )
    applied["command"] = "arena-growth-learning"
    if guard.get("rejected_top_keys"):
        applied["rejected_auto_patch_keys"] = guard["rejected_top_keys"]
    return applied


def _arena_growth_learning_guard(proposal: dict[str, Any]) -> dict[str, Any]:
    patch = proposal.get("patch") if isinstance(proposal.get("patch"), dict) else {}
    if not patch:
        return {"ok": True}
    top_keys = set(patch)
    if "learning" not in top_keys:
        return {"ok": False, "reason": "non_learning_patch_rejected"}
    rejected_top_keys = sorted(top_keys - {"learning"})
    learning = patch.get("learning") if isinstance(patch.get("learning"), dict) else {}
    unknown = set(learning) - ARENA_GROWTH_LEARNING_ALLOWED_KEYS
    if unknown:
        return {"ok": False, "reason": "learning_patch_key_not_auto_safe", "keys": sorted(unknown)}
    return {"ok": True, "rejected_top_keys": rejected_top_keys}


def _arena_growth_universe_step(policy: dict[str, Any], *, policy_path: Path, apply: bool, env: Any) -> dict[str, Any]:
    active_symbols = _arena_active_symbols_by_account(policy)
    accounts: list[dict[str, Any]] = []
    current_policy = policy
    write_applied = False
    for account_id in _arena_account_ids(policy):
        proposal = _arena_universe_refresh_propose_output(current_policy, policy_path=policy_path, account_id=account_id)
        guard = _arena_growth_universe_guard(account_id, proposal, active_symbols)
        if not guard["ok"]:
            accounts.append(
                {
                    "account_id": account_id,
                    "status": "BLOCKED",
                    "reason": guard["reason"],
                    "proposal": proposal,
                    "write_applied": False,
                    "safety": _safety_payload(),
                }
            )
            continue
        add_universe = guard["add_universe"]
        remove_universe = guard["remove_universe"]
        if not add_universe and not remove_universe:
            accounts.append({"account_id": account_id, "status": "NO_CHANGE", "proposal": proposal, "write_applied": False, "safety": _safety_payload()})
            continue
        if not apply:
            accounts.append(
                {
                    "account_id": account_id,
                    "status": "PLANNED",
                    "add_universe": add_universe,
                    "remove_universe": remove_universe,
                    "write_applied": False,
                    "safety": _safety_payload(),
                }
            )
            continue
        applied = _arena_strategy_apply_output(
            current_policy,
            policy_path=policy_path,
            account_id=account_id,
            mode=None,
            risk_multiplier=None,
            pause=False,
            resume=False,
            trade_mode=None,
            add_universe=add_universe,
            remove_universe=remove_universe,
            confirm="APPLY_ARENA_STRATEGY",
            backup_dir=policy_path.parent / "backups" / "growth_loop",
        )
        accounts.append(
            {
                "account_id": account_id,
                "status": applied.get("status"),
                "add_universe": add_universe,
                "remove_universe": remove_universe,
                "write_applied": bool(applied.get("write_applied")),
                "backup_path": applied.get("backup_path"),
                "validation": applied.get("validation"),
                "safety": applied.get("safety") or _safety_payload(),
            }
        )
        if applied.get("write_applied"):
            write_applied = True
            current_policy = applied.get("applied_policy") if isinstance(applied.get("applied_policy"), dict) else load_arena_policy(policy_path)
    blocked = [item for item in accounts if item.get("status") == "BLOCKED"]
    return {
        "status": "BLOCKED" if blocked else ("APPLIED" if write_applied else "OK"),
        "command": "arena-growth-universe",
        "active_symbol_source": "arena-status" if active_symbols is not None else "unavailable_fail_closed_for_removals",
        "accounts": accounts,
        "write_applied": write_applied,
        "safety": _safety_payload(policy_write=write_applied),
    }


def _arena_growth_universe_guard(
    account_id: str,
    proposal: dict[str, Any],
    active_symbols_by_account: dict[str, set[str]] | None,
) -> dict[str, Any]:
    raw_add = [str(item).upper() for item in proposal.get("add_universe") or []]
    raw_remove = [str(item).upper() for item in proposal.get("remove_universe") or []]
    if account_id == "DEMO-RU":
        raw_add = []
        raw_remove = []
    elif account_id == "DEMO-US":
        raw_add = raw_add[:2]
        raw_remove = raw_remove[:2]
    elif account_id == "DEMO-AI":
        raw_add = []
        raw_remove = raw_remove[:2]
    else:
        raw_add = []
        raw_remove = []
    if raw_remove:
        if active_symbols_by_account is None:
            return {"ok": False, "reason": "active_positions_unavailable_for_removal_check", "add_universe": [], "remove_universe": []}
        active = active_symbols_by_account.get(account_id, set())
        blocked = sorted(set(raw_remove).intersection(active))
        if blocked:
            return {
                "ok": False,
                "reason": "cannot_remove_held_or_open_symbol",
                "symbols": blocked,
                "add_universe": [],
                "remove_universe": [],
            }
    return {"ok": True, "add_universe": raw_add, "remove_universe": raw_remove}


def _arena_active_symbols_by_account(policy: dict[str, Any]) -> dict[str, set[str]] | None:
    try:
        status = build_arena_status(policy, soft_stops=_arena_soft_stop_records())
    except Exception:  # noqa: BLE001 - removals fail closed if live status cannot be read.
        return None
    result: dict[str, set[str]] = {}
    for account in status.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("account_id") or "")
        symbols: set[str] = set()
        for key in ("positions", "open_orders", "orders"):
            rows = account.get(key)
            if isinstance(rows, dict):
                rows = rows.get("items") or rows.get("orders") or rows.get("positions")
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or row.get("ticker") or "").upper()
                if symbol:
                    symbols.add(symbol)
        if account_id:
            result[account_id] = symbols
    return result


def _arena_candidate_score_value(candidate: dict[str, Any]) -> int:
    score = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    raw = score.get("score", candidate.get("score"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _arena_growth_min_score(policy: dict[str, Any], *, account_id: str) -> int:
    portfolio = policy.get("portfolio") if isinstance(policy.get("portfolio"), dict) else {}
    default = int(portfolio.get("min_candidate_score_for_buy") or 75)
    by_account = portfolio.get("min_candidate_score_for_buy_by_account")
    if isinstance(by_account, dict):
        try:
            return int(by_account.get(account_id, default))
        except (TypeError, ValueError):
            return default
    return default


def _arena_growth_loop_state_path(*, env: Any = os.environ) -> Path:
    raw = str(env.get(ARENA_GROWTH_LOOP_STATE_ENV) or DEFAULT_ARENA_GROWTH_LOOP_STATE_PATH)
    return Path(raw)


def _arena_growth_loop_audit_path(*, env: Any = os.environ) -> Path:
    raw = str(env.get(ARENA_GROWTH_LOOP_AUDIT_ENV) or DEFAULT_ARENA_GROWTH_LOOP_AUDIT_PATH)
    return Path(raw)


def _arena_event_candidates_path(*, env: Any = os.environ) -> Path:
    raw = str(env.get(ARENA_EVENT_CANDIDATES_ENV) or DEFAULT_ARENA_EVENT_CANDIDATES_PATH)
    return Path(raw)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _append_arena_growth_audit(path: Path, output: dict[str, Any], *, now: datetime) -> None:
    record = {
        "timestamp": now.isoformat(timespec="seconds"),
        "status": output.get("status"),
        "mode": output.get("mode"),
        "write_applied": output.get("write_applied"),
        "policy_writes": output.get("policy_writes") or [],
        "codex_review_records_written": (output.get("codex_reviews") or {}).get("records_written"),
        "learning_status": (output.get("learning") or {}).get("status"),
        "universe_status": (output.get("universe") or {}).get("status"),
        "broker_mutation": False,
    }
    append_jsonl(path, [record])


def _arena_universe_refresh_propose_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
    decisions: list[dict[str, Any]] | None = None,
    outcomes: list[dict[str, Any]] | None = None,
    execution_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    account = _arena_policy_account(policy, account_id)
    if account is None:
        return {
            "status": "INVALID",
            "command": "arena-universe-refresh-propose",
            "policy_path": str(policy_path),
            "account_id": account_id,
            "reason": "account_not_allowed",
            "allowed_accounts": _arena_account_ids(policy),
            "write_applied": False,
            "safety": _safety_payload(),
        }

    if decisions is None or outcomes is None:
        paths = _arena_learning_paths()
        decisions = read_jsonl(paths["decisions"]) if decisions is None else decisions
        outcomes = read_jsonl(paths["outcomes"]) if outcomes is None else outcomes
    if execution_ledger is None:
        execution_ledger = _read_arena_execution_ledger(env=os.environ)

    account_decisions = [
        item for item in decisions or [] if str(item.get("account_id") or "") == account_id and not bool(item.get("exclude_from_learning"))
    ]
    account_outcomes = [
        item for item in outcomes or [] if str(item.get("account_id") or "") == account_id and not bool(item.get("exclude_from_learning"))
    ]
    gate_counts = Counter(gate for item in account_decisions for gate in item.get("gate_reasons") or [])
    symbol_counts = Counter(str(item.get("symbol") or "").upper() for item in account_decisions if item.get("symbol"))
    score_by_symbol = _arena_refresh_scores_by_symbol(account_decisions)
    non_universe_gates = [
        {"gate": gate, "count": count}
        for gate, count in gate_counts.most_common()
        if gate not in ARENA_UNIVERSE_LEARNING_GATES
    ]
    universe = [str(symbol).upper() for symbol in account.get("universe") or []]
    learning = policy.get("learning") if isinstance(policy.get("learning"), dict) else {}
    deprioritized = {str(symbol).upper() for symbol in learning.get("deprioritize_symbols") or []}
    attribution_uncertain = {str(symbol).upper() for symbol in learning.get("attribution_uncertain_symbols") or []}
    pattern_report = build_repeat_pattern_loss_report(policy, execution_ledger or [])
    pattern_events = [event for event in pattern_report.get("events") or [] if str(event.get("account_id") or "") == account_id]

    role = _arena_universe_refresh_role(account)
    diagnosis = _arena_universe_refresh_diagnosis(
        account_id=account_id,
        role=role,
        decisions_count=len(account_decisions),
        gate_counts=gate_counts,
        non_universe_gates=non_universe_gates,
        pattern_events=pattern_events,
        outcomes=account_outcomes,
    )
    remove_universe: list[str] = []
    add_universe: list[str] = []
    keep_but_deprioritize = sorted([symbol for symbol in universe if symbol in deprioritized or symbol in attribution_uncertain])

    if account_id == "DEMO-US":
        add_universe = _arena_us_refresh_additions(account, universe=universe, limit=5)
        remove_universe = _arena_us_refresh_removals(
            universe=universe,
            outcomes=account_outcomes,
            pattern_events=pattern_events,
            deprioritized=deprioritized,
            score_by_symbol=score_by_symbol,
            symbol_counts=symbol_counts,
            limit=len(add_universe),
        )
        keep_but_deprioritize = [symbol for symbol in keep_but_deprioritize if symbol not in set(remove_universe)]
    elif account_id == "DEMO-RU":
        diagnosis["universe_refresh_priority"] = "secondary_after_exposure_rotation"
    elif account_id == "DEMO-AI":
        diagnosis["universe_refresh_priority"] = "cleanup_quality_first_no_risk_expansion"

    learning_cleanup = _arena_learning_cleanup_proposal(learning)
    return {
        "status": "OK",
        "command": "arena-universe-refresh-propose",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "role": role,
        "diagnosis": diagnosis,
        "remove_universe": remove_universe,
        "add_universe": add_universe,
        "keep_but_deprioritize": keep_but_deprioritize,
        "learning_cleanup": learning_cleanup,
        "blocked_by_non_universe_gates": non_universe_gates[:8],
        "top_symbols": [{"symbol": symbol, "count": count} for symbol, count in symbol_counts.most_common(8)],
        "repeat_pattern": _arena_refresh_repeat_pattern_summary(pattern_events),
        "apply_commands": {
            "strategy": {
                "review": _arena_strategy_review_command(account_id=account_id, add_universe=add_universe, remove_universe=remove_universe),
                "apply": _arena_strategy_apply_command(account_id=account_id, add_universe=add_universe, remove_universe=remove_universe),
            },
            "learning": {
                "review": "python scripts/hermes_operator.py arena-learning-propose",
                "apply": "python scripts/hermes_operator.py arena-learning-apply --confirm APPLY_ARENA_LEARNING"
                if learning_cleanup.get("changes")
                else None,
            },
        },
        "write_applied": False,
        "broker_mutation": False,
        "safety": _safety_payload(),
    }


def _arena_universe_refresh_role(account: dict[str, Any]) -> str:
    account_id = str(account.get("account_id") or "")
    if account_id == "DEMO-RU":
        return "ru_anchor_rotation_first"
    if account_id == "DEMO-US":
        return "us_universe_rotation_first"
    if account_id == "DEMO-AI":
        return "ai_cross_market_cleanup_quality_first"
    return str(account.get("strategy") or "arena_account")


def _arena_universe_refresh_diagnosis(
    *,
    account_id: str,
    role: str,
    decisions_count: int,
    gate_counts: Counter[str],
    non_universe_gates: list[dict[str, Any]],
    pattern_events: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    pnl_values = [_decimal(item.get("net_pnl_rub")) for item in outcomes]
    pnl_values = [item for item in pnl_values if item is not None]
    primary_gate = non_universe_gates[0]["gate"] if non_universe_gates else (gate_counts.most_common(1)[0][0] if gate_counts else None)
    if account_id == "DEMO-US":
        priority = "primary"
        summary = "static_us_universe_and_learning_penalties_are_primary_review_target"
    elif account_id == "DEMO-RU":
        priority = "secondary_after_exposure_rotation"
        summary = "growth_currently_blocked_more_by_exposure_rotation_than_universe"
    else:
        priority = "cleanup_quality_first_no_risk_expansion" if account_id == "DEMO-AI" else "review"
        summary = "cross_market_learning_cleanup_before_any_universe_expansion"
    return {
        "summary": summary,
        "role": role,
        "universe_refresh_priority": priority,
        "primary_growth_blocker": primary_gate,
        "decisions_count": decisions_count,
        "closed_pnl_records": len(pnl_values),
        "net_pnl_rub": decimal_payload(sum(pnl_values, Decimal("0"))) if pnl_values else "0",
        "repeat_pattern_events_count": len(pattern_events),
        "top_gate_reasons": [{"gate": gate, "count": count} for gate, count in gate_counts.most_common(8)],
    }


def _arena_refresh_scores_by_symbol(decisions: list[dict[str, Any]]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for item in decisions:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            score = int(item.get("score"))
        except (TypeError, ValueError):
            continue
        result.setdefault(symbol, []).append(score)
    return result


def _arena_us_refresh_additions(account: dict[str, Any], *, universe: list[str], limit: int) -> list[str]:
    allowed_markets = {str(market).upper() for market in account.get("markets") or []}
    universe_set = set(universe)
    additions: list[str] = []
    for seed in ARENA_US_UNIVERSE_REFRESH_SEEDS:
        symbol = _normalize_arena_symbol(seed)
        if symbol is None or symbol in universe_set:
            continue
        market = symbol.split("@", 1)[1]
        if market not in allowed_markets:
            continue
        additions.append(symbol)
        if len(additions) >= limit:
            break
    return additions


def _arena_us_refresh_removals(
    *,
    universe: list[str],
    outcomes: list[dict[str, Any]],
    pattern_events: list[dict[str, Any]],
    deprioritized: set[str],
    score_by_symbol: dict[str, list[int]],
    symbol_counts: Counter[str],
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []
    pnl_by_symbol: dict[str, Decimal] = {}
    for outcome in outcomes:
        symbol = str(outcome.get("symbol") or "").upper()
        pnl = _decimal(outcome.get("net_pnl_rub"))
        if symbol and pnl is not None:
            pnl_by_symbol[symbol] = pnl_by_symbol.get(symbol, Decimal("0")) + pnl
    losing_symbols = [symbol for symbol, pnl in sorted(pnl_by_symbol.items(), key=lambda item: item[1]) if pnl < 0]
    pattern_symbols = [str(event.get("symbol") or "").upper() for event in pattern_events if event.get("symbol")]
    weak_score_symbols = [
        symbol
        for symbol, scores in score_by_symbol.items()
        if scores and (sum(scores) / len(scores)) < 65 and symbol_counts.get(symbol, 0) >= 2
    ]
    priority = list(dict.fromkeys(losing_symbols + pattern_symbols + sorted(deprioritized) + weak_score_symbols))
    universe_set = set(universe)
    return [symbol for symbol in priority if symbol in universe_set][:limit]


def _arena_learning_cleanup_proposal(learning: dict[str, Any]) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    patch: dict[str, Any] = {}
    for key, proposed in ARENA_LEARNING_CLEANUP_TARGET.items():
        current = [str(item).upper() for item in learning.get(key) or []]
        current_set = set(current)
        proposed_set = {str(item).upper() for item in proposed}
        remove = [symbol for symbol in current if symbol not in proposed_set]
        add = [symbol for symbol in proposed if symbol not in current_set]
        if remove or add or current != proposed:
            changes.append({"path": f"learning.{key}", "remove": remove, "add": add, "proposed": proposed})
            patch[key] = proposed
    return {"changes": changes, "patch": {"learning": patch} if patch else {}, "policy_write": False, "broker_mutation": False}


def _arena_refresh_repeat_pattern_summary(pattern_events: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cooldowns = [
        event
        for event in pattern_events
        if (_parse_iso_datetime(event.get("cooldown_until")) or datetime.min.replace(tzinfo=timezone.utc)) >= now
    ]
    return {
        "events_count": len(pattern_events),
        "active_cooldowns_count": len(cooldowns),
        "source_symbols": sorted({str(event.get("symbol") or "").upper() for event in pattern_events if event.get("symbol")}),
    }


def _arena_strategy_review_command(*, account_id: str, add_universe: list[str], remove_universe: list[str]) -> str | None:
    if not add_universe and not remove_universe:
        return None
    args = ["python scripts/hermes_operator.py arena-strategy-propose", f"--account {account_id}"]
    args.extend(f"--remove-universe {symbol}" for symbol in remove_universe)
    args.extend(f"--add-universe {symbol}" for symbol in add_universe)
    return " ".join(args)


def _arena_strategy_apply_command(*, account_id: str, add_universe: list[str], remove_universe: list[str]) -> str | None:
    review = _arena_strategy_review_command(account_id=account_id, add_universe=add_universe, remove_universe=remove_universe)
    return f"{review} --confirm APPLY_ARENA_STRATEGY".replace("arena-strategy-propose", "arena-strategy-apply") if review else None


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _arena_strategy_propose_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
    mode: str | None,
    risk_multiplier: str | None,
    pause: bool,
    resume: bool,
    trade_mode: str | None,
    add_universe: list[str],
    remove_universe: list[str],
) -> dict[str, Any]:
    proposed = copy.deepcopy(policy)
    changes: list[dict[str, Any]] = []
    errors: list[str] = []
    account = _arena_policy_account(proposed, account_id)

    if account is None:
        errors.append(f"account_not_allowed: {account_id}")
    if pause and resume:
        errors.append("--pause and --resume are mutually exclusive")
    if mode is not None:
        old = proposed.get("mode")
        proposed["mode"] = mode
        changes.append({"path": "mode", "old": old, "new": mode})

    if account is not None:
        if risk_multiplier is not None:
            parsed = _decimal(risk_multiplier)
            if parsed is None or parsed <= 0:
                errors.append("risk_multiplier must be a positive number")
            elif parsed > Decimal("2"):
                errors.append("risk_multiplier above 2 requires code-level risk review")
            else:
                old = account.get("risk_multiplier")
                account["risk_multiplier"] = format(parsed, "f")
                changes.append({"path": f"accounts.{account_id}.risk_multiplier", "old": old, "new": account["risk_multiplier"]})
        if pause:
            old = account.get("paused", False)
            account["paused"] = True
            changes.append({"path": f"accounts.{account_id}.paused", "old": old, "new": True})
        if resume:
            old = account.get("paused", False)
            account["paused"] = False
            changes.append({"path": f"accounts.{account_id}.paused", "old": old, "new": False})
        if trade_mode is not None:
            old = account.get("trade_mode")
            account["trade_mode"] = trade_mode
            changes.append({"path": f"accounts.{account_id}.trade_mode", "old": old, "new": trade_mode})

        universe = account.get("universe")
        if not isinstance(universe, list):
            universe = []
            account["universe"] = universe
        for symbol in add_universe:
            normalized = _normalize_arena_symbol(symbol)
            if not normalized:
                errors.append(f"invalid universe symbol: {symbol}")
                continue
            if normalized not in universe:
                universe.append(normalized)
                changes.append({"path": f"accounts.{account_id}.universe", "action": "add", "symbol": normalized})
        for symbol in remove_universe:
            normalized = _normalize_arena_symbol(symbol)
            if not normalized:
                errors.append(f"invalid universe symbol: {symbol}")
                continue
            if normalized in universe:
                universe.remove(normalized)
                changes.append({"path": f"accounts.{account_id}.universe", "action": "remove", "symbol": normalized})
            else:
                errors.append(f"universe symbol not found for {account_id}: {normalized}")

    validation = validate_arena_policy(proposed)
    all_errors = errors + validation["errors"]
    return {
        "status": "OK" if not all_errors else "INVALID",
        "command": "arena-strategy-propose",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "changes": changes,
        "proposed_policy": proposed,
        "validation": {"errors": all_errors, "warnings": validation["warnings"]},
        "write_applied": False,
        "next_step": "Review diff, then run arena-strategy-apply with --confirm APPLY_ARENA_STRATEGY.",
        "safety": _safety_payload(),
    }


def _arena_strategy_apply_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    account_id: str,
    mode: str | None,
    risk_multiplier: str | None,
    pause: bool,
    resume: bool,
    trade_mode: str | None,
    add_universe: list[str],
    remove_universe: list[str],
    confirm: str,
    backup_dir: Path | None,
) -> dict[str, Any]:
    proposal = _arena_strategy_propose_output(
        policy,
        policy_path=policy_path,
        account_id=account_id,
        mode=mode,
        risk_multiplier=risk_multiplier,
        pause=pause,
        resume=resume,
        trade_mode=trade_mode,
        add_universe=add_universe,
        remove_universe=remove_universe,
    )
    output = {
        "status": proposal["status"],
        "command": "arena-strategy-apply",
        "policy_path": str(policy_path),
        "account_id": account_id,
        "changes": proposal["changes"],
        "validation": proposal["validation"],
        "write_applied": False,
        "backup_path": None,
        "safety": _safety_payload(policy_write=False),
    }
    if proposal["status"] != "OK":
        output["next_step"] = "Fix validation errors before applying Arena strategy."
        return output
    if not proposal["changes"]:
        output["status"] = "NOOP"
        output["next_step"] = "No Arena strategy changes requested; nothing was written."
        return output
    if confirm != "APPLY_ARENA_STRATEGY":
        output["status"] = "CONFIRMATION_REQUIRED"
        output["next_step"] = "Re-run with --confirm APPLY_ARENA_STRATEGY after reviewing diff."
        return output

    target_backup_dir = backup_dir or (policy_path.parent / "backups")
    backup_path = _backup_policy_file(policy_path, target_backup_dir)
    _write_policy_file(policy_path, proposal["proposed_policy"])
    output.update(
        {
            "status": "OK",
            "write_applied": True,
            "backup_path": str(backup_path),
            "applied_policy": proposal["proposed_policy"],
            "next_step": "Run arena-status and arena-scan to verify the active Arena policy.",
            "safety": _safety_payload(policy_write=True),
        }
    )
    return output


def _arena_emergency_stop_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    confirm: str,
    backup_dir: Path | None,
) -> dict[str, Any]:
    proposed = copy.deepcopy(policy)
    changes: list[dict[str, Any]] = []
    if proposed.get("mode") != "approval":
        changes.append({"path": "mode", "old": proposed.get("mode"), "new": "approval"})
        proposed["mode"] = "approval"
    if proposed.get("emergency_stop") is not True:
        changes.append({"path": "emergency_stop", "old": proposed.get("emergency_stop"), "new": True})
        proposed["emergency_stop"] = True
    for account in proposed.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("account_id") or "")
        if account.get("paused") is not True:
            changes.append({"path": f"accounts.{account_id}.paused", "old": account.get("paused"), "new": True})
            account["paused"] = True
        if account.get("trade_mode") != "manual":
            changes.append({"path": f"accounts.{account_id}.trade_mode", "old": account.get("trade_mode"), "new": "manual"})
            account["trade_mode"] = "manual"

    validation = validate_arena_policy(proposed)
    output = {
        "status": "CONFIRMATION_REQUIRED",
        "command": "arena-emergency-stop",
        "policy_path": str(policy_path),
        "changes": changes,
        "validation": validation,
        "write_applied": False,
        "backup_path": None,
        "accounts": _arena_account_ids(policy),
        "required_next_step": "Re-run with --confirm ARENA_EMERGENCY_STOP to pause all Arena accounts in policy.",
        "safety": _safety_payload(policy_write=False),
    }
    if validation["errors"]:
        output["status"] = "INVALID"
        output["required_next_step"] = "Fix policy validation errors before emergency stop write."
        return output
    if not changes:
        output["status"] = "NOOP"
        output["required_next_step"] = "Arena emergency_stop is already enabled."
        return output
    if confirm != "ARENA_EMERGENCY_STOP":
        return output

    target_backup_dir = backup_dir or (policy_path.parent / "backups")
    backup_path = _backup_policy_file(policy_path, target_backup_dir)
    _write_policy_file(policy_path, proposed)
    output.update(
        {
            "status": "OK",
            "write_applied": True,
            "backup_path": str(backup_path),
            "applied_policy": proposed,
            "required_next_step": "Run arena-status; status should show HALT/emergency_stop before any further trading.",
            "safety": _safety_payload(policy_write=True),
        }
    )
    return output


def _arena_account_ids(policy: dict[str, Any]) -> list[str]:
    accounts = policy.get("accounts") if isinstance(policy.get("accounts"), list) else []
    return [str(item.get("account_id")) for item in accounts if isinstance(item, dict) and item.get("account_id")]


def _arena_policy_account(policy: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    accounts = policy.get("accounts") if isinstance(policy.get("accounts"), list) else []
    for account in accounts:
        if isinstance(account, dict) and str(account.get("account_id")) == account_id:
            return account
    return None


def _arena_account(report: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    accounts = report.get("accounts") if isinstance(report.get("accounts"), list) else []
    for account in accounts:
        if isinstance(account, dict) and str(account.get("account_id")) == account_id:
            return account
    return None


def _trade_proposal_output(
    report: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    normalized_symbol = _normalize_symbol(symbol) if symbol else None
    if symbol and not normalized_symbol:
        return {
            "status": "INVALID",
            "command": "trade-proposal",
            "error": f"invalid symbol: {symbol}",
            "safety": _safety_payload(),
        }

    matching = [
        item for item in candidates
        if isinstance(item, dict) and (normalized_symbol is None or item.get("symbol") == normalized_symbol)
    ]
    active = [item for item in matching if item.get("status") != "BLOCKED"]
    blocked = [item for item in matching if item.get("status") == "BLOCKED"]
    active_policy = policy or {"research": {}}

    if report.get("errors"):
        return {
            "status": "NO_PROPOSAL",
            "command": "trade-proposal",
            "reason": "report_errors",
            "errors": report.get("errors") or [],
            "warnings": report.get("warnings") or [],
            "safety": _safety_payload(),
        }

    if not active:
        return {
            "status": "NO_PROPOSAL",
            "command": "trade-proposal",
            "reason": "no_unblocked_candidates",
            "symbol": normalized_symbol,
            "blocked_candidates": blocked,
            "blocked_count": len(blocked),
            "codex_review": {"status": "not_required_without_unblocked_candidate"},
            "safety": _safety_payload(),
        }

    candidate = active[0]
    context_output = _codex_review_context_output(report, policy=active_policy, symbol=str(candidate.get("symbol")))
    if context_output.get("status") != "OK":
        return {
            "status": "CODEX_REVIEW_REQUIRED",
            "command": "trade-proposal",
            "reason": "codex_review_context_unavailable",
            "symbol": candidate.get("symbol"),
            "codex_review_context": context_output,
            "safety": _safety_payload(),
        }
    context = context_output["context"]
    review = _load_fresh_codex_review(
        str(candidate.get("symbol")),
        str(context.get("context_hash")),
        active_policy,
    )
    if review is None:
        return {
            "status": "CODEX_REVIEW_REQUIRED",
            "command": "trade-proposal",
            "reason": "fresh_codex_review_missing",
            "symbol": candidate.get("symbol"),
            "context_hash": context.get("context_hash"),
            "codex_review_context": context,
            "required_next_step": (
                "Run codex-review-context, ask Codex to return only compact JSON, then record it with "
                "codex-review-record --confirm RECORD_CODEX_REVIEW."
            ),
            "safety": _safety_payload(),
        }
    verdict = str((review.get("review") or {}).get("verdict") or "")
    if verdict == "AVOID":
        return {
            "status": "NO_PROPOSAL",
            "command": "trade-proposal",
            "reason": "codex_review_avoid",
            "symbol": candidate.get("symbol"),
            "context_hash": context.get("context_hash"),
            "codex_review": _codex_review_payload(review),
            "safety": _safety_payload(),
        }
    if verdict == "UNAVAILABLE":
        return {
            "status": "CODEX_REVIEW_REQUIRED",
            "command": "trade-proposal",
            "reason": "codex_review_unavailable_manual_only",
            "symbol": candidate.get("symbol"),
            "context_hash": context.get("context_hash"),
            "codex_review": _codex_review_payload(review),
            "safety": _safety_payload(),
        }
    proposal = _candidate_trade_proposal(candidate, report=report, codex_review=review)
    return {
        "status": "PROPOSE_ONLY",
        "command": "trade-proposal",
        "time_msk": report.get("time_msk"),
        "proposal": proposal,
        "confirmation_text": _confirmation_text(proposal),
        "safety": _safety_payload(),
        "next_step": "Show this proposal to Алексей. Do not submit broker orders without a separate explicit confirmation flow.",
    }


def _trade_confirm_output(
    report: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    symbol: str,
    confirmation: str,
) -> dict[str, Any]:
    proposal_output = _trade_proposal_output(report, policy=policy, symbol=symbol)
    if proposal_output.get("status") != "PROPOSE_ONLY":
        return {
            "status": "NO_ORDER",
            "command": "trade-confirm",
            "reason": "no_active_trade_proposal",
            "proposal_status": proposal_output.get("status"),
            "proposal": proposal_output,
            "safety": _safety_payload(),
        }

    proposal = proposal_output["proposal"]
    required_confirmation = proposal["execution"]["confirmation_phrase"]
    if confirmation != required_confirmation:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "command": "trade-confirm",
            "required_confirmation": required_confirmation,
            "received_confirmation": confirmation,
            "safety": _safety_payload(),
        }

    order = _order_from_trade_proposal(proposal)
    notional = _float_value(proposal.get("risk", {}).get("notional")) or order.notional
    risk_config = RiskConfig(
        max_order_value=max(notional, order.notional),
        max_position_value=max(notional, order.notional),
        allowed_tickers={proposal["symbol"]},
    )
    executor = GuardedOrderExecutor(
        risk_config=risk_config,
        guard_config=OrderGuardConfig(dry_run=True, allow_orders=False, confirmation_phrase=required_confirmation),
    )
    try:
        planned_order = executor.submit(order, confirmation=confirmation)
    except OrderNotAllowed as exc:
        return {
            "status": "BLOCKED",
            "command": "trade-confirm",
            "reason": str(exc),
            "proposal": proposal,
            "safety": _safety_payload(),
        }

    return {
        "status": "DRY_RUN",
        "command": "trade-confirm",
        "proposal": proposal,
        "planned_order": planned_order,
        "protective_stop_plan": {
            "required": True,
            "side": "SELL",
            "quantity": proposal["quantity"],
            "stop_price": proposal["protective_stop"]["stop_price"],
            "must_be_placed_immediately_after_fill": True,
            "live_submission_enabled": False,
        },
        "safety": _safety_payload(),
        "next_step": "Legacy demo trade-confirm is dry-run only. Use Arena commands for guarded live contest execution.",
    }


def _trade_execute_demo_output(
    report: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    symbol: str,
    confirmation: str,
    live: bool,
    client: Any | None = None,
    env: Any = os.environ,
) -> dict[str, Any]:
    confirm_output = _trade_confirm_output(report, policy=policy, symbol=symbol, confirmation=confirmation)
    if confirm_output.get("status") != "DRY_RUN":
        return {
            "status": "NO_ORDER",
            "command": "trade-execute-demo",
            "reason": "dry_run_confirmation_not_ready",
            "confirm_status": confirm_output.get("status"),
            "confirm_output": confirm_output,
            "safety": _safety_payload(),
        }

    proposal = confirm_output["proposal"]
    expected_account_id = _configured_h4_account_id(policy=policy, env=env)
    if proposal.get("candidate_source") == "held_scale_in":
        return {
            "status": "BLOCKED",
            "command": "trade-execute-demo",
            "reason": "held_scale_in_live_execution_not_supported",
            "proposal": proposal,
            "required_next_step": "implement_full_position_stop_rebuild_before_live_scale_in",
            "safety": _safety_payload(),
        }
    if proposal.get("account_id") != expected_account_id:
        return {
            "status": "BLOCKED",
            "command": "trade-execute-demo",
            "reason": "demo_account_only",
            "expected_account_id": expected_account_id,
            "proposal_account_id": proposal.get("account_id"),
            "safety": _safety_payload(),
        }

    if not live:
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "trade-execute-demo",
            "reason": "missing_--live",
            "required": ["--live", f"{LIVE_DEMO_GATE_ENV}=true", proposal["execution"]["confirmation_phrase"]],
            "proposal": proposal,
            "safety": _safety_payload(),
        }

    if str(env.get(LIVE_DEMO_GATE_ENV, "")).strip().lower() != "true":
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "trade-execute-demo",
            "reason": f"{LIVE_DEMO_GATE_ENV}_not_true",
            "required_env": LIVE_DEMO_GATE_ENV,
            "proposal": proposal,
            "safety": _safety_payload(),
        }

    token = str(env.get("FINAM_TOKEN") or "").strip()
    account_id = str(env.get("FINAM_ACCOUNT_ID") or proposal.get("account_id") or "").strip()
    if not token:
        return {
            "status": "BLOCKED",
            "command": "trade-execute-demo",
            "reason": "FINAM_TOKEN_missing",
            "safety": _safety_payload(),
        }
    if account_id != expected_account_id:
        return {
            "status": "BLOCKED",
            "command": "trade-execute-demo",
            "reason": "FINAM_ACCOUNT_ID_must_match_demo_account",
            "expected_account_id": expected_account_id,
            "received_account_id": account_id,
            "safety": _safety_payload(),
        }

    blocking_state = _blocking_trade_safety_state(env=env)
    if blocking_state is not None:
        return {
            "status": "BLOCKED",
            "command": "trade-execute-demo",
            "reason": "unresolved_trade_safety_state",
            "safety_state": blocking_state,
            "proposal": proposal,
            "halt_new_buys": True,
            "safety": _safety_payload(),
        }

    client = client or FinamClient()

    try:
        jwt = client.create_session(token)
    except Exception as exc:  # pragma: no cover - exact client errors vary by transport
        return _broker_error_output("session_create_failed", exc, proposal)

    try:
        rules = _broker_instrument_rules(client, jwt, account_id, proposal["symbol"])
        assert_broker_buy_allowed(rules)
        proposal = _normalize_proposal_for_broker(proposal, rules)
    except Exception as exc:
        return _broker_error_output("finam_contract_validation_failed", exc, proposal)

    order = _order_from_trade_proposal(proposal)
    notional = _float_value(proposal.get("risk", {}).get("notional")) or order.notional
    risk_config = RiskConfig(
        max_order_value=max(notional, order.notional),
        max_position_value=max(notional, order.notional),
        allowed_tickers={proposal["symbol"]},
    )
    client_order_ids = _client_order_ids(proposal["symbol"])
    buy_payload = _buy_order_payload(proposal, client_order_id=client_order_ids["buy"])
    _write_trade_safety_state(
        {
            "halt_new_buys": True,
            "status": "BUY_SUBMITTING",
            "symbol": proposal["symbol"],
            "client_order_id": client_order_ids["buy"],
        },
        env=env,
    )

    def submit_buy(_: OrderProposal) -> dict[str, Any]:
        return client.place_order(jwt, account_id, buy_payload)

    executor = GuardedOrderExecutor(
        risk_config=risk_config,
        guard_config=OrderGuardConfig(
            dry_run=False,
            allow_orders=True,
            confirmation_phrase=proposal["execution"]["confirmation_phrase"],
        ),
        broker_submit=submit_buy,
    )

    try:
        buy_result = executor.submit(order, confirmation=confirmation)
    except Exception as exc:
        _write_trade_safety_state(
            {
                "halt_new_buys": True,
                "status": "BUY_SUBMIT_FAILED",
                "symbol": proposal["symbol"],
                "client_order_id": client_order_ids["buy"],
                "error": str(exc),
            },
            env=env,
        )
        return _broker_error_output("buy_submit_failed", exc, proposal)

    _write_trade_safety_state(
        {
            "halt_new_buys": True,
            "status": "BUY_SUBMITTED",
            "symbol": proposal["symbol"],
            "client_order_id": client_order_ids["buy"],
            "buy_order_id": buy_result.get("order_id"),
        },
        env=env,
    )
    fill_state = _wait_for_buy_execution(client, jwt, account_id, buy_result, proposal, env=env)
    if fill_state["status"] == "pending":
        output = {
            "status": "BUY_PENDING_NO_SL",
            "command": "trade-execute-demo",
            "reason": "buy_order_not_filled_yet",
            "proposal": proposal,
            "buy_order_payload": buy_payload,
            "buy_order_response": buy_result,
            "buy_fill_state": fill_state,
            "halt_new_buys": True,
            "safety": _safety_payload(trading_mutations=True),
        }
        output["telegram_summary"] = h4_monitor_notify.format_trade_execution_result(output)
        return output
    if fill_state["status"] not in {"filled", "partial"}:
        output = _broker_error_output("buy_fill_check_failed", RuntimeError(str(fill_state)), proposal)
        output["buy_order_payload"] = buy_payload
        output["buy_order_response"] = buy_result
        return output

    filled_quantity = _decimal(fill_state["executed_quantity"]) or Decimal("0")
    protected_proposal = proposal | {"quantity": int(filled_quantity)}
    stop_payload = _sltp_order_payload(
        protected_proposal,
        quantity=filled_quantity,
        client_order_id=client_order_ids["sltp"],
    )
    _write_trade_safety_state(
        {
            "halt_new_buys": True,
            "status": "BUY_FILLED_SL_PENDING",
            "symbol": proposal["symbol"],
            "client_order_id": client_order_ids["buy"],
            "buy_order_id": buy_result.get("order_id"),
            "executed_quantity": decimal_payload(filled_quantity, min_scale=1),
        },
        env=env,
    )

    try:
        stop_result = client.place_sltp_order(jwt, account_id, stop_payload)
    except Exception as exc:
        output = _broker_error_output("protective_stop_submit_failed", exc, proposal)
        output["buy_order_response"] = buy_result
        output["buy_fill_state"] = fill_state
        output["protective_stop_payload"] = stop_payload
        output["halt_new_buys"] = True
        _write_trade_safety_state(output, env=env)
        return output

    try:
        orders = client.orders(jwt, account_id)
    except Exception as exc:
        output = _broker_error_output("protective_stop_verification_failed", exc, proposal)
        output["buy_order_response"] = buy_result
        output["protective_stop_response"] = stop_result
        output["buy_fill_state"] = fill_state
        output["halt_new_buys"] = True
        _write_trade_safety_state(output, env=env)
        return output

    stop_verification = _verify_protective_stop_watching(orders, protected_proposal)
    if not stop_verification["verified"]:
        output = {
            "status": "STOP_NOT_VERIFIED",
            "command": "trade-execute-demo",
            "reason": "protective_sell_sl_not_found_in_ORDER_STATUS_WATCHING",
            "proposal": proposal,
            "buy_order_payload": buy_payload,
            "protective_stop_payload": stop_payload,
            "buy_order_response": buy_result,
            "protective_stop_response": stop_result,
            "buy_fill_state": fill_state,
            "stop_verification": stop_verification,
            "halt_new_buys": True,
            "safety": _safety_payload(trading_mutations=True),
        }
        output["telegram_summary"] = h4_monitor_notify.format_trade_execution_result(output)
        _write_trade_safety_state(output, env=env)
        return output

    output = {
        "status": "EXECUTED_DEMO" if fill_state["status"] == "filled" else "EXECUTED_DEMO_PARTIAL",
        "command": "trade-execute-demo",
        "proposal": proposal,
        "buy_order_payload": buy_payload,
        "protective_stop_payload": stop_payload,
        "buy_order_response": buy_result,
        "protective_stop_response": stop_result,
        "buy_fill_state": fill_state,
        "stop_verification": stop_verification,
        "halt_new_buys": False,
        "safety": _safety_payload(trading_mutations=True),
        "next_step": "Report execution result and keep monitoring protective SELL SL.",
    }
    output["telegram_summary"] = h4_monitor_notify.format_trade_execution_result(output)
    _clear_trade_safety_state(env=env)
    return output


def _trade_buy_output(
    report: dict[str, Any],
    *,
    symbol: str,
    intent: str,
    confirmation: str = "",
    live: bool,
    client: Any | None = None,
    env: Any = os.environ,
) -> dict[str, Any]:
    normalized_symbol = _normalize_symbol(symbol)
    intent_symbol = _intent_buy_symbol(intent)
    if not normalized_symbol:
        return {
            "status": "INVALID",
            "command": "trade-buy",
            "error": f"invalid symbol: {symbol}",
            "safety": _safety_payload(),
        }
    if intent_symbol != normalized_symbol:
        return {
            "status": "INTENT_REQUIRED",
            "command": "trade-buy",
            "reason": "direct_buy_intent_did_not_match_symbol",
            "symbol": normalized_symbol,
            "received_intent": intent,
            "accepted_examples": [f"купи {normalized_symbol}", f"купить {normalized_symbol}", f"{normalized_symbol} подтверждаю"],
            "safety": _safety_payload(),
        }
    raw_research = report.get("research")
    research: dict[str, Any] = raw_research if isinstance(raw_research, dict) else {}
    if research.get("classification") == "provider_client_failure":
        return {
            "status": "PROVIDER_FAILURE_NO_BROKER_COMMAND",
            "command": "trade-buy",
            "reason": "provider_client_failure_was_not_a_trading_signal",
            "symbol": normalized_symbol,
            "research": _research_payload(research),
            "safety": _safety_payload(),
        }

    required_confirmation = f"CONFIRM_BUY {normalized_symbol}"
    if live and confirmation != required_confirmation:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "command": "trade-buy",
            "reason": "exact_trade_buy_confirmation_required",
            "symbol": normalized_symbol,
            "required_confirmation": required_confirmation,
            "received_confirmation": confirmation,
            "safety": _safety_payload(),
        }

    output = _trade_execute_demo_output(
        report,
        symbol=normalized_symbol,
        confirmation=required_confirmation,
        live=live,
        client=client,
        env=env,
    )
    output["delegated_command"] = output.get("command")
    output["command"] = "trade-buy"
    return output


def _autonomous_run_output(
    report: dict[str, Any],
    *,
    policy: dict[str, Any],
    live: bool,
    client: Any | None = None,
    env: Any = os.environ,
    runtime_check: Any | None = None,
) -> dict[str, Any]:
    pre_report_gate = _autonomous_pre_report_gate(policy, live=live, env=env)
    if pre_report_gate is not None:
        return pre_report_gate

    preflight = runtime_check() if runtime_check is not None else _autonomous_runtime_preflight(env=env)
    if preflight.get("status") != "OK":
        return {
            "status": "BLOCKED",
            "command": "autonomous-run",
            "reason": "runtime_preflight_not_ok",
            "runtime_status": preflight.get("status"),
            "runtime_errors": preflight.get("errors") or [],
            "runtime_warnings": preflight.get("warnings") or [],
            "runtime_checks": preflight.get("checks") or {},
            "safety": _safety_payload(),
        }

    if report.get("errors"):
        return {
            "status": "NO_ORDER",
            "command": "autonomous-run",
            "reason": "report_errors",
            "errors": report.get("errors") or [],
            "warnings": report.get("warnings") or [],
            "safety": _safety_payload(),
        }

    research = report.get("research") if isinstance(report.get("research"), dict) else {}
    if research.get("classification") == "provider_client_failure":
        return {
            "status": "PROVIDER_FAILURE_NO_BROKER_COMMAND",
            "command": "autonomous-run",
            "reason": "provider_client_failure_was_not_a_trading_signal",
            "research": _research_payload(research),
            "safety": _safety_payload(),
        }
    if research.get("status") != "ok" or research.get("verdict") not in {"OK", "RISK"}:
        return {
            "status": "NO_ORDER",
            "command": "autonomous-run",
            "reason": "research_not_autonomous_eligible",
            "research": _research_payload(research),
            "dynamic_promotions": _dynamic_promotion_suggestions(report),
            "safety": _safety_payload(),
        }

    candidate, rejected = _select_autonomous_candidate(report, policy=policy)
    if candidate is None:
        return {
            "status": "NO_ORDER",
            "command": "autonomous-run",
            "reason": "no_autonomous_candidate",
            "rejected_candidates": rejected,
            "dynamic_promotions": _dynamic_promotion_suggestions(report),
            "research": _research_payload(research),
            "safety": _safety_payload(),
        }

    research_symbols = [str(item) for item in research.get("symbols") or []]
    if research_symbols and candidate.get("symbol") not in research_symbols:
        return {
            "status": "NO_ORDER",
            "command": "autonomous-run",
            "reason": "selected_symbol_not_in_research_gate",
            "selected_symbol": candidate.get("symbol"),
            "research_symbols": research_symbols,
            "safety": _safety_payload(),
        }

    symbol = str(candidate["symbol"])
    output = _trade_execute_demo_output(
        report,
        symbol=symbol,
        confirmation=f"CONFIRM_BUY {symbol}",
        live=True,
        client=client,
        env=env,
    )
    output["delegated_command"] = output.get("command")
    output["command"] = "autonomous-run"
    output["autonomous"] = {
        "policy_mode": policy.get("mode"),
        "selected_symbol": symbol,
        "decision_id": (candidate.get("decision_record") or {}).get("decision_id"),
        "candidate_source": candidate.get("candidate_source") or "static",
        "research_verdict": research.get("verdict"),
        "runtime_preflight_status": preflight.get("status"),
    }
    return output


def _autonomous_pre_report_gate(policy: dict[str, Any], *, live: bool, env: Any = os.environ) -> dict[str, Any] | None:
    if policy.get("mode") != "autonomous_demo":
        return {
            "status": "BLOCKED",
            "command": "autonomous-run",
            "reason": "policy_mode_not_autonomous_demo",
            "policy_mode": policy.get("mode"),
            "safety": _safety_payload(),
        }
    if not live:
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "autonomous-run",
            "reason": "missing_--live",
            "required": ["--live", f"{AUTONOMOUS_DEMO_GATE_ENV}=true", f"{LIVE_DEMO_GATE_ENV}=true"],
            "safety": _safety_payload(),
        }
    if str(env.get(AUTONOMOUS_DEMO_GATE_ENV, "")).strip().lower() != "true":
        return {
            "status": "LIVE_GATE_REQUIRED",
            "command": "autonomous-run",
            "reason": f"{AUTONOMOUS_DEMO_GATE_ENV}_not_true",
            "required_env": AUTONOMOUS_DEMO_GATE_ENV,
            "safety": _safety_payload(),
        }
    return None


def _autonomous_runtime_preflight(*, env: Any = os.environ) -> dict[str, Any]:
    return runtime_doctor.run_diagnostics(
        env=env,
        skip_network=False,
        outbox_root=runtime_doctor.DEFAULT_OUTBOX_ROOT,
        safety_state_path=_trade_safety_state_path(env=env),
    )


def _select_autonomous_candidate(
    report: dict[str, Any],
    *,
    policy: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    rejected: list[dict[str, Any]] = []
    risk = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    max_new = _positive_int(risk.get("max_new_trades_per_run"), default=1)
    selected_count = 0
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        symbol = candidate.get("symbol")
        source = candidate.get("candidate_source") or "static"
        reasons = _autonomous_candidate_reject_reasons(candidate, source=source)
        if reasons:
            rejected.append({"symbol": symbol, "candidate_source": source, "reasons": reasons})
            continue
        if selected_count >= max_new:
            rejected.append({"symbol": symbol, "candidate_source": source, "reasons": ["max_new_trades_per_run"]})
            continue
        selected_count += 1
        return candidate, rejected
    return None, rejected


def _autonomous_candidate_reject_reasons(candidate: dict[str, Any], *, source: str) -> list[str]:
    reasons: list[str] = []
    if source == "held_scale_in":
        reasons.append("scale_in_not_supported")
    elif source != "static":
        reasons.append("dynamic_requires_static_universe_promotion")
    if candidate.get("status") == "BLOCKED":
        reasons.append("candidate_blocked")
    if candidate.get("actionable_this_run") is not True:
        reasons.append("not_actionable_this_run")
    if candidate.get("gate_reasons"):
        reasons.append("candidate_has_gate_reasons")
    if candidate.get("requires_confirmation") is False:
        return reasons
    return reasons


def _dynamic_promotion_suggestions(report: dict[str, Any]) -> list[dict[str, str]]:
    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    suggestions: list[dict[str, str]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("candidate_source") != "dynamic":
            continue
        symbol = str(candidate.get("symbol") or "")
        if not symbol:
            continue
        suggestions.append(
            {
                "symbol": symbol,
                "status": str(candidate.get("status") or "UNKNOWN"),
                "propose_command": f"python scripts/hermes_operator.py policy-propose --add-universe {symbol}",
                "apply_command": f"python scripts/hermes_operator.py policy-apply --add-universe {symbol} --confirm APPLY_POLICY",
            }
        )
    return suggestions


def _broker_error_output(reason: str, exc: Exception, proposal: dict[str, Any]) -> dict[str, Any]:
    output = {
        "status": "BROKER_ERROR",
        "command": "trade-execute-demo",
        "reason": reason,
        "error": str(exc),
        "proposal": proposal,
        "halt_new_buys": True,
        "safety": _safety_payload(trading_mutations=True),
    }
    output["telegram_summary"] = h4_monitor_notify.format_trade_execution_result(output)
    return output


def _broker_instrument_rules(client: Any, jwt: str, account_id: str, symbol: str) -> Any:
    asset = client.asset(jwt, symbol, account_id=account_id)
    params = client.asset_params(jwt, symbol, account_id=account_id)
    return rules_from_finam(symbol, asset, params)


def _normalize_proposal_for_broker(proposal: dict[str, Any], rules: Any) -> dict[str, Any]:
    quantity = normalize_quantity_to_lot(proposal.get("quantity"), rules)
    limit_price = normalize_price(proposal.get("entry", {}).get("limit_price"), rules, direction="floor")
    stop_price = normalize_price(proposal.get("protective_stop", {}).get("stop_price"), rules, direction="ceil")
    take_profit = normalize_price(proposal.get("take_profit", {}).get("price"), rules, direction="floor")
    if quantity <= 0:
        raise FinamContractError(f"{proposal.get('symbol')}: normalized quantity is zero")
    if stop_price >= limit_price:
        raise FinamContractError(f"{proposal.get('symbol')}: normalized stop is not below limit price")
    normalized = copy.deepcopy(proposal)
    normalized["quantity"] = int(quantity)
    normalized["entry"]["limit_price"] = decimal_payload(limit_price, min_scale=rules.decimals)
    normalized["protective_stop"]["stop_price"] = decimal_payload(stop_price, min_scale=rules.decimals)
    normalized["take_profit"]["price"] = decimal_payload(take_profit, min_scale=rules.decimals)
    normalized["risk"]["finam_contract"] = rules.to_payload()
    normalized["risk"]["lot"] = {"lot_size": decimal_payload(rules.lot_size)}
    normalized["risk"]["notional"] = decimal_payload(quantity * limit_price, min_scale=rules.decimals)
    return normalized


def _client_order_ids(symbol: str) -> dict[str, str]:
    ticker = symbol.split("@", 1)[0][:6]
    stamp = datetime.now(timezone.utc).strftime("%d%H%M%S")
    return {
        "buy": f"H4B{stamp}{ticker}"[:20],
        "sltp": f"H4S{stamp}{ticker}"[:20],
    }


def _buy_order_payload(proposal: dict[str, Any], *, client_order_id: str | None = None) -> dict[str, Any]:
    payload = {
        "symbol": proposal["symbol"],
        "side": "SIDE_BUY",
        "type": "ORDER_TYPE_LIMIT",
        "time_in_force": "TIME_IN_FORCE_DAY",
        "quantity": {"value": decimal_payload(proposal["quantity"], min_scale=1)},
        "limit_price": {"value": str(proposal["entry"]["limit_price"])},
    }
    if client_order_id:
        payload["client_order_id"] = client_order_id
        payload["comment"] = "h4 demo buy"
    return payload


def _sltp_order_payload(
    proposal: dict[str, Any],
    *,
    quantity: Any | None = None,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "symbol": proposal["symbol"],
        "side": "SIDE_SELL",
        "quantity_sl": {"value": decimal_payload(proposal["quantity"] if quantity is None else quantity, min_scale=1)},
        "sl_price": {"value": str(proposal["protective_stop"]["stop_price"])},
        "valid_before": "VALID_BEFORE_GOOD_TILL_CANCEL",
    }
    if client_order_id:
        payload["client_order_id"] = client_order_id
        payload["comment"] = "h4 protective sl"
    return payload


def _wait_for_buy_execution(
    client: Any,
    jwt: str,
    account_id: str,
    buy_result: dict[str, Any],
    proposal: dict[str, Any],
    *,
    env: Any,
) -> dict[str, Any]:
    attempts = max(1, _int_env(env, "FINAM_BUY_FILL_CHECKS", 5))
    sleep_seconds = max(0, _int_env(env, "FINAM_BUY_FILL_SLEEP_SECONDS", 2))
    order_id = str(buy_result.get("order_id") or "")
    latest = buy_result
    initial_state = _buy_execution_state(latest, proposal)
    initial_state["attempts"] = 0
    initial_state["order_id"] = order_id or initial_state.get("order_id")
    if initial_state["status"] in {"filled", "partial"}:
        initial_state["source"] = "entry_submit_response"
        return initial_state
    for attempt in range(attempts):
        if attempt > 0 and sleep_seconds:
            time.sleep(sleep_seconds)
        if order_id and hasattr(client, "get_order"):
            try:
                latest = client.get_order(jwt, account_id, order_id)
            except Exception as exc:  # noqa: BLE001
                recovered = _recover_buy_execution_state(client, jwt, account_id, proposal, order_id=order_id)
                if recovered["status"] in {"filled", "partial"}:
                    recovered["attempts"] = attempt + 1
                    recovered["order_id"] = order_id or recovered.get("order_id")
                    recovered["primary_check_error"] = str(exc)
                    return recovered
                return {"status": "error", "error": str(exc), "order_id": order_id}
        state = _buy_execution_state(latest, proposal)
        state["attempts"] = attempt + 1
        state["order_id"] = order_id or state.get("order_id")
        if state["status"] in {"filled", "partial"}:
            return state
    state = _buy_execution_state(latest, proposal)
    state["status"] = "pending"
    state["attempts"] = attempts
    state["order_id"] = order_id or state.get("order_id")
    return state


def _buy_execution_state(order_state: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    order = order_state.get("order") if isinstance(order_state.get("order"), dict) else {}
    status = str(order_state.get("status") or order.get("status") or "")
    expected_quantity = _decimal(proposal.get("quantity")) or Decimal("0")
    execution_price = _decimal(_first_nested(order_state, "execution_price"))
    executed = (
        _decimal(order_state.get("executed_quantity"))
        or _decimal(order_state.get("filled_quantity"))
        or _decimal(_first_nested(order, "executed_quantity"))
        or _decimal(_first_nested(order, "filled_quantity"))
        or (_decimal(_first_nested(order, "quantity")) if execution_price is not None else None)
        or (expected_quantity if status == "ORDER_STATUS_FILLED" else Decimal("0"))
    )
    remaining = _decimal(order_state.get("remaining_quantity"))
    if status == "ORDER_STATUS_FILLED" and executed > 0:
        normalized = "filled"
    elif execution_price is not None and executed > 0:
        normalized = "filled"
    elif status in {"ORDER_STATUS_PARTIALLY_EXECUTED", "ORDER_STATUS_PARTIALLY_FILLED"} and executed > 0:
        normalized = "partial"
    elif executed > 0 and remaining is not None and remaining > 0:
        normalized = "partial"
    elif status in {"ORDER_STATUS_CANCELLED", "ORDER_STATUS_REJECTED", "ORDER_STATUS_EXPIRED"}:
        normalized = "terminal_unfilled"
    else:
        normalized = "pending"
    return {
        "status": normalized,
        "broker_status": status or None,
        "executed_quantity": decimal_payload(executed, min_scale=1),
        "execution_price": decimal_payload(execution_price) if execution_price is not None else None,
        "remaining_quantity": decimal_payload(remaining, min_scale=1) if remaining is not None else None,
        "order_id": order_state.get("order_id") or order.get("order_id"),
    }


def _recover_buy_execution_state(
    client: Any,
    jwt: str,
    account_id: str,
    proposal: dict[str, Any],
    *,
    order_id: str,
) -> dict[str, Any]:
    for source, fetch in (
        ("orders", lambda: client.orders(jwt, account_id) if hasattr(client, "orders") else {}),
        ("trades", lambda: client.trades(jwt, account_id, limit=10) if hasattr(client, "trades") else {}),
        ("positions", lambda: client.get_account(jwt, account_id) if hasattr(client, "get_account") else {}),
    ):
        try:
            response = fetch()
        except Exception:  # noqa: BLE001
            continue
        recovered = _buy_execution_state_from_response(response, proposal, order_id=order_id, source=source)
        if recovered["status"] in {"filled", "partial"}:
            return recovered
    return {"status": "error", "source": "fallback_unavailable", "order_id": order_id}


def _buy_execution_state_from_response(
    response: dict[str, Any],
    proposal: dict[str, Any],
    *,
    order_id: str,
    source: str,
) -> dict[str, Any]:
    symbol = str(proposal.get("symbol") or "")
    expected_quantity = _decimal(proposal.get("quantity")) or Decimal("0")
    side = str(proposal.get("side") or "BUY").upper()
    if source == "orders":
        for order in _orders_list(response):
            if not isinstance(order, dict):
                continue
            found_id = str(_first_nested(order, "order_id") or _first_nested(order, "id") or "")
            found_symbol = str(_first_nested(order, "symbol") or _first_nested(order, "security_code") or "")
            if order_id and found_id and found_id != order_id:
                continue
            if found_symbol and found_symbol != symbol:
                continue
            state = _buy_execution_state(order, proposal)
            state["source"] = "orders_fallback"
            if state["status"] in {"filled", "partial"}:
                return state
        return {"status": "error", "source": "orders_fallback", "order_id": order_id}

    if source == "trades":
        total = Decimal("0")
        for trade in _response_items(response, ("trades", "items", "data")):
            trade_symbol = str(_first_nested(trade, "symbol") or _first_nested(trade, "security_code") or _first_nested(trade, "ticker") or "")
            trade_side = str(_first_nested(trade, "side") or _first_nested(trade, "operation") or _first_nested(trade, "buy_sell") or "").upper()
            if trade_symbol and trade_symbol != symbol:
                continue
            if side == "BUY" and "SELL" in trade_side:
                continue
            if side == "SELL" and "BUY" in trade_side:
                continue
            quantity = _decimal(_first_nested(trade, "quantity") or _first_nested(trade, "qty") or _first_nested(trade, "balance"))
            if quantity is not None:
                total += abs(quantity)
        return _quantity_execution_state(total, expected_quantity, order_id=order_id, source="trades_fallback")

    if source == "positions":
        total = Decimal("0")
        positions = response.get("positions") if isinstance(response.get("positions"), list) else []
        for position in positions:
            if not isinstance(position, dict):
                continue
            position_symbol = str(_first_nested(position, "symbol") or _first_nested(position, "security_code") or _first_nested(position, "ticker") or "")
            if position_symbol != symbol:
                continue
            quantity = _decimal(_first_nested(position, "quantity") or _first_nested(position, "balance") or _first_nested(position, "qty"))
            if quantity is not None:
                total += abs(quantity)
        return _quantity_execution_state(total, expected_quantity, order_id=order_id, source="positions_fallback")

    return {"status": "error", "source": source, "order_id": order_id}


def _quantity_execution_state(quantity: Decimal, expected_quantity: Decimal, *, order_id: str, source: str) -> dict[str, Any]:
    if quantity <= 0:
        status = "error"
    elif expected_quantity > 0 and quantity < expected_quantity:
        status = "partial"
    else:
        status = "filled"
    remaining = expected_quantity - quantity if expected_quantity > quantity else Decimal("0")
    return {
        "status": status,
        "broker_status": None,
        "executed_quantity": decimal_payload(quantity, min_scale=1),
        "remaining_quantity": decimal_payload(remaining, min_scale=1) if status == "partial" else None,
        "order_id": order_id,
        "source": source,
    }


def _response_items(response: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = response.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _verify_protective_stop_watching(orders_response: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    raw_orders = _orders_list(orders_response)
    active_orders = h4_monitor._active_orders(raw_orders)
    stops_by_symbol = h4_monitor._watching_sell_order_details_by_symbol(active_orders)
    symbol = proposal["symbol"]
    required_quantity = _decimal(proposal.get("quantity"))
    required_stop = _decimal(proposal.get("protective_stop", {}).get("stop_price"))
    matching: list[dict[str, Any]] = []

    for stop in stops_by_symbol.get(symbol, []):
        quantity = _decimal(stop.get("quantity"))
        stop_price = _decimal(stop.get("stop"))
        if quantity is None or required_quantity is None or quantity < required_quantity:
            continue
        if stop_price is None or required_stop is None or stop_price != required_stop:
            continue
        matching.append(stop)

    return {
        "verified": bool(matching),
        "symbol": symbol,
        "required_quantity": str(required_quantity) if required_quantity is not None else None,
        "required_stop": str(required_stop) if required_stop is not None else None,
        "matching_stops": matching,
        "watching_sell_sltp_by_symbol": {key: len(value) for key, value in stops_by_symbol.items()},
    }


def _orders_list(orders_response: dict[str, Any]) -> list[Any]:
    for key in ("orders", "items", "data"):
        value = orders_response.get(key)
        if isinstance(value, list):
            return value
    return []


def _candidate_trade_proposal(
    candidate: dict[str, Any],
    *,
    report: dict[str, Any],
    codex_review: dict[str, Any],
) -> dict[str, Any]:
    symbol = str(candidate.get("symbol") or "UNKNOWN")
    return {
        "symbol": symbol,
        "side": "BUY",
        "candidate_source": candidate.get("candidate_source") or "static",
        "discovery_score": candidate.get("discovery_score"),
        "discovery_reasons": candidate.get("discovery_reasons") or [],
        "account_id": (report.get("account") or {}).get("account_id"),
        "quantity": _int_value(candidate.get("quantity")),
        "entry": {
            "type": "LIMIT",
            "limit_price": candidate.get("current_price"),
            "reference_price": candidate.get("reference_price") or candidate.get("current_price"),
        },
        "protective_stop": {
            "side": "SELL",
            "stop_price": candidate.get("stop"),
            "must_place_immediately_after_fill": True,
        },
        "take_profit": {
            "type": "TP_2R",
            "price": candidate.get("tp_2r"),
        },
        "risk": {
            "risk_rub": candidate.get("risk_rub"),
            "notional": candidate.get("notional"),
            "atr14_h4": candidate.get("atr14"),
            "nearest_target": candidate.get("nearest_target"),
            "nearest_target_r": candidate.get("nearest_target_r"),
            "lot": candidate.get("lot"),
            "finam_contract": candidate.get("finam_contract"),
        },
        "scale_in": {
            "enabled": candidate.get("candidate_source") == "held_scale_in",
            "current_position": candidate.get("current_position"),
            "projected_position": candidate.get("projected_position"),
            "live_execution_enabled": False,
            "live_execution_block_reason": "requires_full_position_stop_rebuild",
        }
        if candidate.get("candidate_source") == "held_scale_in"
        else None,
        "codex_review": _codex_review_payload(codex_review),
        "research": _codex_review_payload(codex_review),
        "gates": {
            "status": candidate.get("status"),
            "requires_confirmation": True,
            "gate_reasons": candidate.get("gate_reasons") or [],
            "decision_record": candidate.get("decision_record") or {},
        },
        "execution": {
            "broker_mutation": False,
            "telegram_production_send": False,
            "confirmation_required": True,
            "confirmation_phrase": f"CONFIRM_BUY {symbol}",
        },
    }


def _research_payload(research: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": research.get("status"),
        "provider": research.get("provider"),
        "model": research.get("model"),
        "requested_max_tokens": research.get("requested_max_tokens"),
        "usage": research.get("usage"),
        "cache": research.get("cache"),
        "verdict": research.get("verdict") or ("UNAVAILABLE" if research.get("status") != "ok" else "UNKNOWN"),
        "symbols": research.get("symbols") or [],
        "summary": research.get("summary"),
        "classification": research.get("classification"),
        "error": research.get("error") or research.get("reason"),
    }


def _codex_review_payload(review_artifact: dict[str, Any]) -> dict[str, Any]:
    review = review_artifact.get("review") if isinstance(review_artifact.get("review"), dict) else {}
    return {
        "status": "ok",
        "provider": "codex_review",
        "source": review_artifact.get("source") or CODEX_REVIEW_SOURCE,
        "context_hash": review_artifact.get("context_hash"),
        "expires_at": review_artifact.get("expires_at"),
        "verdict": review.get("verdict") or "UNAVAILABLE",
        "confidence": review.get("confidence"),
        "reasons": review.get("reasons") or [],
        "blocking_flags": review.get("blocking_flags") or [],
        "summary": review.get("summary"),
    }


def _codex_review_context_output(
    report: dict[str, Any],
    *,
    policy: dict[str, Any],
    symbol: str,
) -> dict[str, Any]:
    normalized_symbol = _normalize_symbol(symbol)
    if not normalized_symbol:
        return {
            "status": "INVALID",
            "command": "codex-review-context",
            "error": f"invalid symbol: {symbol}",
            "safety": _safety_payload(),
        }

    candidates = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    candidate = next(
        (item for item in candidates if isinstance(item, dict) and item.get("symbol") == normalized_symbol),
        None,
    )
    positions = report.get("positions") if isinstance(report.get("positions"), list) else []
    position = next((item for item in positions if isinstance(item, dict) and item.get("symbol") == normalized_symbol), None)
    account = report.get("account") if isinstance(report.get("account"), dict) else {}
    risk = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    research_policy = policy.get("research") if isinstance(policy.get("research"), dict) else {}

    core = {
        "schema_version": CODEX_REVIEW_SCHEMA_VERSION,
        "symbol": normalized_symbol,
        "account": _compact_dict(
            account,
            ["account_id", "equity", "cash", "available_cash", "open_risk_rub", "open_risk_pct", "max_total_open_risk_pct"],
        ),
        "position": _compact_position(position),
        "candidate": _compact_candidate(candidate),
        "risk": _compact_dict(
            risk,
            [
                "risk_per_trade_pct",
                "stop_atr_multiplier",
                "take_profit_r",
                "min_target_r",
                "max_total_open_risk_pct",
                "max_open_positions",
                "max_new_trades_per_run",
            ],
        ),
        "gates": {
            "status": candidate.get("status") if isinstance(candidate, dict) else "NO_CANDIDATE",
            "gate_reasons": [str(item)[:80] for item in ((candidate or {}).get("gate_reasons") or [])][:8]
            if isinstance(candidate, dict)
            else [],
            "report_status": report.get("status"),
            "operator_mode": report.get("operator_mode"),
            "errors": [str(item)[:120] for item in report.get("errors") or []][:3],
            "warnings": [str(item)[:120] for item in report.get("warnings") or []][:3],
        },
        "recent_outcomes": _recent_outcomes(normalized_symbol, _int_policy(research_policy, "recent_outcomes_limit", 5)),
        "rss_flags": _rss_flags(normalized_symbol, policy, _int_policy(research_policy, "rss_flags_limit", 5)),
    }
    context_hash = _context_hash(core)
    context = {
        **core,
        "context_hash": context_hash,
        "evidence_refs": _evidence_refs(normalized_symbol),
        "required_output_schema": {
            "verdict": "OK|RISK|AVOID|UNAVAILABLE",
            "confidence": "0..100",
            "reasons": "array max 3, each <=120 chars",
            "blocking_flags": "array of strings",
            "summary": "<=240 chars",
        },
    }
    max_bytes = _int_policy(research_policy, "context_max_bytes", DEFAULT_CODEX_CONTEXT_MAX_BYTES)
    context = _fit_compact_context(context, max_bytes=max_bytes)
    return {
        "status": "OK",
        "command": "codex-review-context",
        "format": "compact-json",
        "context": context,
        "bytes": len(_compact_json(context).encode("utf-8")),
        "safety": _safety_payload(),
    }


def _codex_review_record_output(
    report: dict[str, Any],
    *,
    policy: dict[str, Any],
    symbol: str,
    review_json: str,
    confirm: str,
) -> dict[str, Any]:
    if confirm != CODEX_REVIEW_CONFIRM:
        return {
            "status": "CONFIRMATION_REQUIRED",
            "command": "codex-review-record",
            "required_confirmation": CODEX_REVIEW_CONFIRM,
            "safety": _safety_payload(),
        }
    context_output = _codex_review_context_output(report, policy=policy, symbol=symbol)
    if context_output.get("status") != "OK":
        return context_output | {"command": "codex-review-record"}
    context = context_output["context"]
    try:
        review = _parse_codex_review_json(review_json)
    except ValueError as exc:
        return {
            "status": "INVALID",
            "command": "codex-review-record",
            "error": str(exc),
            "safety": _safety_payload(),
        }
    supplied_hash = review.pop("context_hash", None)
    if supplied_hash is not None and supplied_hash != context.get("context_hash"):
        return {
            "status": "INVALID",
            "command": "codex-review-record",
            "error": "review_json context_hash does not match current compact context",
            "expected_context_hash": context.get("context_hash"),
            "received_context_hash": supplied_hash,
            "safety": _safety_payload(),
        }

    now = datetime.now(timezone.utc)
    ttl_minutes = _int_policy(policy.get("research") if isinstance(policy.get("research"), dict) else {}, "codex_review_ttl_minutes", DEFAULT_CODEX_REVIEW_TTL_MINUTES)
    artifact = {
        "schema_version": CODEX_REVIEW_SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(timespec="seconds"),
        "source": CODEX_REVIEW_SOURCE,
        "provider": "openai-codex",
        "symbol": context.get("symbol"),
        "context_hash": context.get("context_hash"),
        "review": review,
    }
    path = _write_codex_review_artifact(artifact, policy)
    return {
        "status": "OK",
        "command": "codex-review-record",
        "symbol": artifact["symbol"],
        "context_hash": artifact["context_hash"],
        "review": _codex_review_payload(artifact),
        "path": str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path),
        "safety": _safety_payload(),
    }


def _parse_codex_review_json(raw: str) -> dict[str, Any]:
    stripped = raw.strip()
    if not stripped:
        raise ValueError("review-json is empty")
    if stripped.startswith("```") or "\n```" in stripped:
        raise ValueError("review-json must be raw JSON, not Markdown")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"review-json must be a JSON object: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("review-json must be a JSON object")
    allowed = {"verdict", "confidence", "reasons", "blocking_flags", "summary", "context_hash"}
    extra = set(parsed) - allowed
    if extra:
        raise ValueError("review-json contains unsupported keys: " + ", ".join(sorted(extra)))
    verdict = str(parsed.get("verdict") or "").upper()
    if verdict not in CODEX_REVIEW_VERDICTS:
        raise ValueError("verdict must be OK, RISK, AVOID, or UNAVAILABLE")
    confidence = parsed.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or confidence < 0 or confidence > 100:
        raise ValueError("confidence must be a number from 0 to 100")
    reasons = parsed.get("reasons")
    if not isinstance(reasons, list) or len(reasons) > 3:
        raise ValueError("reasons must be an array with at most 3 items")
    clean_reasons = []
    for reason in reasons:
        if not isinstance(reason, str) or len(reason) > 120 or _looks_like_markdown(reason):
            raise ValueError("each reason must be plain text <=120 chars")
        clean_reasons.append(reason)
    blocking_flags = parsed.get("blocking_flags")
    if not isinstance(blocking_flags, list) or len(blocking_flags) > 8:
        raise ValueError("blocking_flags must be an array with at most 8 items")
    clean_flags = []
    for flag in blocking_flags:
        if not isinstance(flag, str) or len(flag) > 80 or _looks_like_markdown(flag):
            raise ValueError("each blocking flag must be plain text <=80 chars")
        clean_flags.append(flag)
    summary = parsed.get("summary")
    if not isinstance(summary, str) or len(summary) > 240 or _looks_like_markdown(summary):
        raise ValueError("summary must be plain text <=240 chars")
    result = {
        "verdict": verdict,
        "confidence": int(confidence),
        "reasons": clean_reasons,
        "blocking_flags": clean_flags,
        "summary": summary,
    }
    if isinstance(parsed.get("context_hash"), str):
        result["context_hash"] = parsed["context_hash"]
    return result


def _load_fresh_codex_review(symbol: str, context_hash: str, policy: dict[str, Any]) -> dict[str, Any] | None:
    review_dir = _codex_review_dir(policy)
    if not review_dir.exists():
        return None
    now = datetime.now(timezone.utc)
    matching: list[dict[str, Any]] = []
    for path in review_dir.glob("*.json"):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if artifact.get("symbol") != symbol or artifact.get("context_hash") != context_hash:
            continue
        expires_at = _parse_iso_datetime(str(artifact.get("expires_at") or ""))
        if expires_at is None or expires_at <= now:
            continue
        review = artifact.get("review") if isinstance(artifact.get("review"), dict) else {}
        if review.get("verdict") not in CODEX_REVIEW_VERDICTS:
            continue
        artifact["_path"] = str(path)
        matching.append(artifact)
    matching.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return matching[0] if matching else None


def _load_latest_fresh_codex_review_for_symbol(symbol: str, policy: dict[str, Any]) -> dict[str, Any] | None:
    review_dir = _codex_review_dir(policy)
    if not review_dir.exists():
        return None
    now = datetime.now(timezone.utc)
    matching: list[dict[str, Any]] = []
    for path in review_dir.glob("*.json"):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if artifact.get("symbol") != symbol:
            continue
        expires_at = _parse_iso_datetime(str(artifact.get("expires_at") or ""))
        if expires_at is None or expires_at <= now:
            continue
        review = artifact.get("review") if isinstance(artifact.get("review"), dict) else {}
        if review.get("verdict") not in CODEX_REVIEW_VERDICTS:
            continue
        artifact["_path"] = str(path)
        matching.append(artifact)
    matching.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return matching[0] if matching else None


def _write_codex_review_artifact(artifact: dict[str, Any], policy: dict[str, Any]) -> Path:
    review_dir = _codex_review_dir(policy)
    review_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    symbol = _artifact_symbol(str(artifact.get("symbol") or "UNKNOWN"))
    context_hash = str(artifact.get("context_hash") or "nohash")
    path = review_dir / f"{stamp}_{symbol}_{context_hash}.json"
    suffix = 1
    while path.exists():
        path = review_dir / f"{stamp}_{symbol}_{context_hash}_{suffix}.json"
        suffix += 1
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _codex_review_dir(policy: dict[str, Any]) -> Path:
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    raw = str(research.get("review_dir") or DEFAULT_CODEX_REVIEW_DIR)
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _compact_candidate(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    return _compact_dict(
        candidate,
        [
            "symbol",
            "status",
            "current_price",
            "quantity",
            "notional",
            "atr14",
            "stop",
            "tp_2r",
            "nearest_target",
            "nearest_target_r",
            "risk_rub",
            "requires_confirmation",
        ],
    )


def _compact_position(position: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(position, dict):
        return None
    return _compact_dict(
        position,
        [
            "symbol",
            "quantity",
            "average_price",
            "current_price",
            "unrealized_pnl",
            "status",
            "calculated_stop",
            "tp_2r",
            "progress_r",
            "has_watching_sell_sltp",
        ],
    )


def _compact_dict(data: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: _json_scalar(data.get(key)) for key in keys if data.get(key) is not None}


def _json_scalar(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _context_hash(context_core: dict[str, Any]) -> str:
    return hashlib.sha256(_compact_json(context_core).encode("utf-8")).hexdigest()[:24]


def _compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fit_compact_context(context: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    compact = copy.deepcopy(context)
    max_bytes = max(1200, max_bytes)
    for key in ("rss_flags", "recent_outcomes", "evidence_refs"):
        while len(_compact_json(compact).encode("utf-8")) > max_bytes and isinstance(compact.get(key), list) and compact[key]:
            compact[key] = compact[key][:-1]
    if len(_compact_json(compact).encode("utf-8")) > max_bytes:
        compact["gates"]["warnings"] = []
        compact["gates"]["errors"] = []
    return compact


def _recent_outcomes(symbol: str, limit: int) -> list[dict[str, Any]]:
    path = ROOT / "data" / "runtime" / "arena_execution_ledger.jsonl"
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in reversed(raw_lines[-250:]):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or str(item.get("symbol") or "") != symbol:
            continue
        rows.append(
            {
                "ts": item.get("timestamp") or item.get("time_utc") or item.get("created_at"),
                "action": item.get("action") or item.get("status") or item.get("event"),
                "reason": str(item.get("reason") or item.get("error") or "")[:80] or None,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _rss_flags(symbol: str, policy: dict[str, Any], limit: int) -> list[str]:
    research = research_candidates([{"symbol": symbol}], policy)
    flags = research.get("rss_flags") if isinstance(research.get("rss_flags"), list) else []
    return [str(item)[:160] for item in flags[:limit]]


def _evidence_refs(symbol: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for relative in ("data/runtime/arena_execution_ledger.jsonl", "data/runtime/arena_decision_outcomes.jsonl"):
        path = ROOT / relative
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        matched = [line for line in lines if symbol in line]
        if not matched:
            continue
        digest = hashlib.sha256("\n".join(matched[-20:]).encode("utf-8")).hexdigest()[:16]
        refs.append({"path": relative, "count": len(matched), "sha256": digest})
    return refs[:4]


def _int_policy(data: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _looks_like_markdown(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(("#", "* ", "- ", "> ", "```")) or "```" in stripped


def _artifact_symbol(symbol: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in symbol.upper())


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _confirmation_text(proposal: dict[str, Any]) -> str:
    symbol = proposal["symbol"]
    return "\n".join(
        [
            f"Предложение сделки: {symbol}",
            f"BUY {proposal['quantity']} шт. LIMIT {proposal['entry']['limit_price']}",
            f"Сумма: {proposal['risk']['notional']} RUB",
            f"Риск: {proposal['risk']['risk_rub']} RUB",
            f"SL: {proposal['protective_stop']['stop_price']}",
            f"TP 2R: {proposal['take_profit']['price']}",
            f"Codex review: {proposal['codex_review']['verdict']}",
            f"Для подтверждения напиши: {proposal['execution']['confirmation_phrase']}",
        ]
    )


def _order_from_trade_proposal(proposal: dict[str, Any]) -> OrderProposal:
    quantity = proposal.get("quantity")
    price = _float_value(proposal.get("entry", {}).get("limit_price"))
    if not isinstance(quantity, int) or quantity <= 0 or price is None or price <= 0:
        raise OrderNotAllowed("invalid_trade_proposal")
    return OrderProposal(ticker=proposal["symbol"], side="buy", quantity=quantity, price=price)


def _policy_check_output(policy: dict[str, Any], *, policy_path: Path, verbose: bool) -> dict[str, Any]:
    validation = validate_policy(policy)
    output = {
        "status": "OK" if not validation["errors"] else "INVALID",
        "command": "policy-check",
        "policy_path": str(policy_path),
        "policy": h4_monitor._public_policy(policy),
        "validation": validation,
        "safety": _safety_payload(),
    }
    if verbose:
        output["full_policy"] = policy
        output["editable_paths"] = editable_policy_paths()
    return output


def _policy_validate_output(policy: dict[str, Any], *, policy_path: Path) -> dict[str, Any]:
    validation = validate_policy(policy)
    return {
        "status": "OK" if not validation["errors"] else "INVALID",
        "command": "policy-validate",
        "policy_path": str(policy_path),
        "validation": validation,
        "safety": _safety_payload(),
    }


def _policy_propose_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    mode: str | None,
    set_values: list[str],
    add_universe: list[str],
    remove_universe: list[str],
) -> dict[str, Any]:
    proposed = copy.deepcopy(policy)
    changes: list[dict[str, Any]] = []
    errors: list[str] = []

    if mode is not None:
        old = proposed.get("mode")
        proposed["mode"] = mode
        changes.append({"path": "mode", "old": old, "new": mode})

    for item in set_values:
        try:
            path, raw_value = _parse_set_arg(item)
            _assert_editable_path(path)
            old = _get_path(proposed, path)
            new = _parse_policy_value(raw_value)
            _set_path(proposed, path, new)
            changes.append({"path": ".".join(path), "old": old, "new": new})
        except ValueError as exc:
            errors.append(str(exc))

    for symbol in add_universe:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            errors.append(f"invalid universe symbol: {symbol}")
            continue
        universe = _universe(proposed)
        if normalized not in universe:
            universe.append(normalized)
            changes.append({"path": "universe", "action": "add", "symbol": normalized})

    for symbol in remove_universe:
        normalized = _normalize_symbol(symbol)
        universe = _universe(proposed)
        if normalized in universe:
            universe.remove(normalized)
            changes.append({"path": "universe", "action": "remove", "symbol": normalized})
        else:
            errors.append(f"universe symbol not found: {symbol}")

    validation = validate_policy(proposed)
    all_errors = errors + validation["errors"]
    return {
        "status": "OK" if not all_errors else "INVALID",
        "command": "policy-propose",
        "policy_path": str(policy_path),
        "changes": changes,
        "proposed_policy": proposed,
        "validation": {"errors": all_errors, "warnings": validation["warnings"]},
        "write_applied": False,
        "next_step": "Review this proposal. A future explicit apply command is required to write policy.",
        "safety": _safety_payload(),
    }


def _policy_apply_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    mode: str | None,
    set_values: list[str],
    add_universe: list[str],
    remove_universe: list[str],
    confirm: str,
    backup_dir: Path | None,
) -> dict[str, Any]:
    proposal = _policy_propose_output(
        policy,
        policy_path=policy_path,
        mode=mode,
        set_values=set_values,
        add_universe=add_universe,
        remove_universe=remove_universe,
    )
    output = {
        "status": proposal["status"],
        "command": "policy-apply",
        "policy_path": str(policy_path),
        "changes": proposal["changes"],
        "validation": proposal["validation"],
        "write_applied": False,
        "backup_path": None,
        "safety": _safety_payload(policy_write=False),
    }

    if proposal["status"] != "OK":
        output["next_step"] = "Fix validation errors before applying policy."
        return output

    if not proposal["changes"]:
        output["status"] = "NOOP"
        output["next_step"] = "No policy changes requested; nothing was written."
        return output

    if confirm != "APPLY_POLICY":
        output["status"] = "CONFIRMATION_REQUIRED"
        output["next_step"] = "Re-run with --confirm APPLY_POLICY to write config after reviewing diff."
        return output

    target_backup_dir = backup_dir or (policy_path.parent / "backups")
    backup_path = _backup_policy_file(policy_path, target_backup_dir)
    _write_policy_file(policy_path, proposal["proposed_policy"])

    output.update(
        {
            "status": "OK",
            "write_applied": True,
            "backup_path": str(backup_path),
            "applied_policy": proposal["proposed_policy"],
            "next_step": "Run policy-check and normal read-only operator commands to verify the active policy.",
            "safety": _safety_payload(policy_write=True),
        }
    )
    return output


def validate_policy(policy: dict[str, Any]) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if policy.get("mode") not in {"supervised", "autonomous_demo"}:
        errors.append("mode must be supervised or autonomous_demo")

    risk = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    permissions = policy.get("permissions") if isinstance(policy.get("permissions"), dict) else {}
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    growth_mode = policy.get("growth_mode") if isinstance(policy.get("growth_mode"), dict) else {}
    dynamic_universe = policy.get("dynamic_universe") if isinstance(policy.get("dynamic_universe"), dict) else {}
    universe = policy.get("universe") if isinstance(policy.get("universe"), list) else []

    for path in (
        "risk.risk_per_trade_pct",
        "risk.stop_atr_multiplier",
        "risk.take_profit_r",
        "risk.min_target_r",
        "risk.max_total_open_risk_pct",
    ):
        value = _get_path(policy, path.split("."))
        if _decimal(value) is None or _decimal(value) <= 0:
            errors.append(f"{path} must be a positive number")

    for path in ("risk.atr_period", "risk.max_open_positions", "risk.max_new_trades_per_run"):
        value = _get_path(policy, path.split("."))
        if not isinstance(value, int) or value <= 0:
            errors.append(f"{path} must be a positive integer")

    if _decimal(risk.get("risk_per_trade_pct")) and _decimal(risk.get("risk_per_trade_pct")) > Decimal("2"):
        warnings.append("risk.risk_per_trade_pct above 2% is aggressive")
    if _decimal(risk.get("max_total_open_risk_pct")) and _decimal(risk.get("max_total_open_risk_pct")) > Decimal("5"):
        warnings.append("risk.max_total_open_risk_pct above 5% is aggressive")

    required_permissions = (
        "protective_stops_auto",
        "new_buys_require_telegram_confirmation",
        "manual_sells_require_confirmation",
        "shorts_require_separate_confirmation",
        "second_tier_requires_confirmation",
        "unknown_target_requires_confirmation",
        "incomplete_candidate_blocks_trade",
        "propose_only_when_manual_confirmation_required",
    )
    for key in required_permissions:
        if not isinstance(permissions.get(key), bool):
            errors.append(f"permissions.{key} must be boolean")

    if not universe:
        errors.append("universe must contain at least one symbol")
    seen: set[str] = set()
    for symbol in universe:
        if not isinstance(symbol, str) or _normalize_symbol(symbol) != symbol:
            errors.append(f"invalid universe symbol: {symbol}")
        if symbol in seen:
            errors.append(f"duplicate universe symbol: {symbol}")
        seen.add(str(symbol))

    if not isinstance(research.get("enabled"), bool):
        errors.append("research.enabled must be boolean")
    if not isinstance(research.get("max_candidates"), int) or research.get("max_candidates", 0) <= 0:
        errors.append("research.max_candidates must be a positive integer")
    if not isinstance(research.get("timeout_seconds"), int) or research.get("timeout_seconds", 0) <= 0:
        errors.append("research.timeout_seconds must be a positive integer")
    if research.get("provider") != "codex_review":
        errors.append("research.provider must be codex_review")
    for key in (
        "h4_max_candidates",
        "h4_max_tokens",
        "h4_cache_ttl_seconds",
        "daily_max_tokens",
        "weekly_max_tokens",
        "pretrade_max_tokens",
        "daily_max_symbols",
        "weekly_max_symbols",
    ):
        if key in research and (not isinstance(research.get(key), int) or research.get(key, 0) <= 0):
            errors.append(f"research.{key} must be a positive integer")
    for path in (
        "research.context_max_bytes",
        "research.codex_review_ttl_minutes",
        "research.recent_outcomes_limit",
        "research.rss_flags_limit",
    ):
        value = _get_path(policy, path.split("."))
        if value is not None and (not isinstance(value, int) or value <= 0):
            errors.append(f"{path} must be a positive integer")
    if "review_dir" in research:
        if isinstance(research.get("review_dir"), str):
            if not research.get("review_dir"):
                errors.append("research.review_dir must not be empty")
        else:
            errors.append("research.review_dir must not be empty")

    if dynamic_universe:
        for key in (
            "enabled",
            "require_finam_longable",
            "dynamic_candidates_require_confirmation",
            "auto_add_to_static_universe",
        ):
            if key in dynamic_universe and not isinstance(dynamic_universe.get(key), bool):
                errors.append(f"dynamic_universe.{key} must be boolean")
        for path in (
            "dynamic_universe.max_scan_symbols",
            "dynamic_universe.max_dynamic_candidates",
            "dynamic_universe.max_research_symbols",
            "dynamic_universe.min_daily_turnover_rub",
            "dynamic_universe.min_trades_today",
            "dynamic_universe.min_price_rub",
        ):
            value = _get_path(policy, path.split("."))
            if value is not None and (_decimal(value) is None or _decimal(value) <= 0):
                errors.append(f"{path} must be a positive number")
        if dynamic_universe.get("auto_add_to_static_universe") is True:
            errors.append("dynamic_universe.auto_add_to_static_universe must remain false")
        if dynamic_universe.get("openrouter_budget_policy") not in {None, "no_increase_h4_default"}:
            warnings.append("dynamic_universe.openrouter_budget_policy should be no_increase_h4_default")

    failure_policy = research.get("failure_policy") if isinstance(research.get("failure_policy"), dict) else {}
    if not isinstance(failure_policy.get("explicit_avoid_blocks_trade"), bool):
        errors.append("research.failure_policy.explicit_avoid_blocks_trade must be boolean")
    if not isinstance(failure_policy.get("research_unavailable_requires_confirmation"), bool):
        errors.append("research.failure_policy.research_unavailable_requires_confirmation must be boolean")

    for key in ("enabled", "report_only", "allow_second_tier", "allow_short_analysis", "allow_short_orders"):
        if key in growth_mode and not isinstance(growth_mode.get(key), bool):
            errors.append(f"growth_mode.{key} must be boolean")
    for path in ("growth_mode.target_annual_return", "growth_mode.stretch_annual_return", "growth_mode.risk_per_trade_pct"):
        value = _get_path(policy, path.split("."))
        if value is not None and (_decimal(value) is None or _decimal(value) <= 0):
            errors.append(f"{path} must be a positive number")
    for path in (
        "growth_mode.min_growth_score_for_buy",
        "growth_mode.min_growth_score_for_watch",
        "growth_mode.max_new_trades_per_run",
        "growth_mode.max_open_positions",
    ):
        value = _get_path(policy, path.split("."))
        if value is not None and (not isinstance(value, int) or value <= 0):
            errors.append(f"{path} must be a positive integer")
    for path in ("growth_mode.min_growth_score_for_buy", "growth_mode.min_growth_score_for_watch"):
        value = _get_path(policy, path.split("."))
        if isinstance(value, int) and value > 100:
            errors.append(f"{path} must be <= 100")
    if growth_mode.get("enabled") is True:
        errors.append("growth_mode.enabled must remain false until growth execution has a separate approved gate")
    if growth_mode.get("report_only") is False:
        errors.append("growth_mode.report_only must remain true until growth execution has a separate approved gate")
    if growth_mode.get("allow_short_orders") is True:
        errors.append("growth_mode.allow_short_orders must remain false until short availability is separately approved")

    return {"errors": errors, "warnings": warnings}


def editable_policy_paths() -> list[str]:
    return [
        "mode",
        "risk.risk_per_trade_pct",
        "risk.atr_period",
        "risk.stop_atr_multiplier",
        "risk.take_profit_r",
        "risk.max_open_positions",
        "risk.max_new_trades_per_run",
        "risk.min_target_r",
        "risk.max_total_open_risk_pct",
        "research.enabled",
        "research.provider",
        "research.timeout_seconds",
        "research.max_candidates",
        "research.h4_model",
        "research.pretrade_model",
        "research.daily_model",
        "research.weekly_model",
        "research.h4_max_candidates",
        "research.h4_max_tokens",
        "research.h4_cache_ttl_seconds",
        "research.daily_max_tokens",
        "research.weekly_max_tokens",
        "research.pretrade_max_tokens",
        "research.daily_max_symbols",
        "research.weekly_max_symbols",
        "research.context_max_bytes",
        "research.codex_review_ttl_minutes",
        "research.recent_outcomes_limit",
        "research.rss_flags_limit",
        "research.review_dir",
        "research.finam_rss_enabled",
        "dynamic_universe.enabled",
        "dynamic_universe.max_scan_symbols",
        "dynamic_universe.max_dynamic_candidates",
        "dynamic_universe.max_research_symbols",
        "dynamic_universe.min_daily_turnover_rub",
        "dynamic_universe.min_trades_today",
        "dynamic_universe.min_price_rub",
        "dynamic_universe.require_finam_longable",
        "dynamic_universe.dynamic_candidates_require_confirmation",
        "research.failure_policy.allow_reduced_risk_trade_if_research_unavailable",
        "research.failure_policy.reduced_risk_per_trade_pct",
        "research.failure_policy.reduced_risk_max_trades_per_day",
        "research.failure_policy.explicit_avoid_blocks_trade",
        "research.failure_policy.research_unavailable_requires_confirmation",
    ]


def _parse_set_arg(item: str) -> tuple[list[str], str]:
    if "=" not in item:
        raise ValueError(f"--set must be PATH=VALUE: {item}")
    path, value = item.split("=", 1)
    parts = [part.strip() for part in path.split(".") if part.strip()]
    if not parts:
        raise ValueError(f"--set path is empty: {item}")
    return parts, value.strip()


def _assert_editable_path(path: list[str]) -> None:
    joined = ".".join(path)
    if joined not in editable_policy_paths():
        raise ValueError(f"policy path is not editable through operator CLI: {joined}")


def _parse_policy_value(value: str) -> Any:
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "null":
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, (dict, list)):
        raise ValueError("complex JSON values are not supported by --set")
    return parsed


def _int_value(value: Any) -> int | None:
    decimal = _decimal(value)
    if decimal is None:
        return None
    return int(decimal)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float_value(value: Any) -> float | None:
    decimal = _decimal(value)
    if decimal is None:
        return None
    return float(decimal)


def _get_path(data: dict[str, Any], path: list[str]) -> Any:
    current: Any = data
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_path(data: dict[str, Any], path: list[str], value: Any) -> None:
    current: Any = data
    for part in path[:-1]:
        if not isinstance(current.get(part), dict):
            current[part] = {}
        current = current[part]
    current[path[-1]] = value


def _universe(policy: dict[str, Any]) -> list[str]:
    if not isinstance(policy.get("universe"), list):
        policy["universe"] = []
    return policy["universe"]


def _normalize_symbol(symbol: str) -> str | None:
    normalized = symbol.strip().upper()
    if not normalized or "@" not in normalized:
        return None
    ticker, market = normalized.split("@", 1)
    if not ticker or market not in {"MISX", "XNGS", "XNYS"}:
        return None
    if not all(ch.isalnum() for ch in ticker):
        return None
    return normalized


def _normalize_arena_symbol(symbol: str) -> str | None:
    normalized = symbol.strip().upper()
    if not normalized or "@" not in normalized:
        return None
    ticker, market = normalized.split("@", 1)
    if not ticker or not market:
        return None
    if not all(ch.isalnum() or ch in {"-", "."} for ch in ticker):
        return None
    if not all(ch.isalnum() for ch in market):
        return None
    return normalized


def _intent_buy_symbol(intent: str) -> str | None:
    text = " ".join(intent.strip().upper().replace(",", " ").split())
    if not text:
        return None

    tokens = text.split()
    for index, token in enumerate(tokens):
        if token == "CONFIRM_BUY" and index + 1 < len(tokens):
            return _normalize_symbol(_with_default_market(tokens[index + 1]))

    buy_intent_tokens = {"КУПИ", "КУПИТЬ", "ПОКУПАЮ", "BUY"}
    has_buy_intent = any(token in buy_intent_tokens for token in tokens)
    has_confirm_intent = any(token.startswith("ПОДТВЕР") or token == "CONFIRM" for token in tokens)
    if not has_buy_intent and not has_confirm_intent:
        return None

    for token in tokens:
        if token in buy_intent_tokens or token == "CONFIRM" or token.startswith("ПОДТВЕР"):
            continue
        normalized = _normalize_symbol(_with_default_market(token))
        if normalized:
            return normalized
    return None


def _with_default_market(token: str) -> str:
    cleaned = token.strip(" .:;!?)(")
    return cleaned if "@" in cleaned else f"{cleaned}@MISX"


def _blocking_trade_safety_state(*, env: Any = os.environ) -> dict[str, Any] | None:
    state = _read_trade_safety_state(env=env)
    if not state or state.get("halt_new_buys") is not True:
        return None
    return state


def _read_trade_safety_state(*, env: Any = os.environ) -> dict[str, Any]:
    path = _trade_safety_state_path(env=env)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        return {"halt_new_buys": True, "status": "INVALID_SAFETY_STATE", "path": str(path)}
    return data if isinstance(data, dict) else {"halt_new_buys": True, "status": "INVALID_SAFETY_STATE", "path": str(path)}


def _write_trade_safety_state(state: dict[str, Any], *, env: Any = os.environ) -> None:
    path = _trade_safety_state_path(env=env)
    with _json_state_lock(path):
        _write_trade_safety_state_unlocked(state, env=env)


def _write_trade_safety_state_unlocked(state: dict[str, Any], *, env: Any = os.environ) -> None:
    path = _trade_safety_state_path(env=env)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_soft_stops: list[dict[str, Any]] = []
    if "arena_soft_stops" not in state:
        existing = _read_trade_safety_state(env=env)
        records = existing.get("arena_soft_stops")
        if isinstance(records, list):
            existing_soft_stops = [item for item in records if isinstance(item, dict)]
        elif existing.get("mode") == "arena_soft_stop" and existing.get("symbol") and existing.get("account_id"):
            existing_soft_stops = [existing]
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "halt_new_buys": True,
    }
    payload.update(state)
    if existing_soft_stops and "arena_soft_stops" not in payload:
        payload["arena_soft_stops"] = existing_soft_stops
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _clear_trade_safety_state(*, env: Any = os.environ) -> None:
    path = _trade_safety_state_path(env=env)
    with _json_state_lock(path):
        _clear_trade_safety_state_unlocked(env=env)


def _clear_trade_safety_state_unlocked(*, env: Any = os.environ) -> None:
    path = _trade_safety_state_path(env=env)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _trade_safety_state_path(*, env: Any = os.environ) -> Path:
    configured = str(env.get("HERMES_TRADE_SAFETY_STATE") or "").strip()
    return Path(configured) if configured else DEFAULT_SAFETY_STATE_PATH


def _int_env(env: Any, key: str, default: int) -> int:
    try:
        return int(str(env.get(key, default)))
    except (TypeError, ValueError):
        return default


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _decimal_order_value(value: Any, *, scale: int = 1) -> str:
    parsed = _decimal(value)
    if parsed is None:
        return str(value)
    quantum = Decimal(1).scaleb(-scale)
    return format(parsed.quantize(quantum), "f")


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None



def _arena_learning_paths() -> dict[str, Path]:
    return {
        "decisions": DEFAULT_ARENA_LEARNING_EVENTS_PATH,
        "outcomes": DEFAULT_ARENA_LEARNING_OUTCOMES_PATH,
        "suggestions": DEFAULT_ARENA_LEARNING_SUGGESTIONS_PATH,
    }


def _arena_learning_update_output(policy: dict[str, Any], *, policy_path: Path, research_mode: str = "cache_only") -> dict[str, Any]:
    ledger = _read_arena_execution_ledger(env=os.environ)
    scan = build_arena_scan(policy, soft_stops=_arena_soft_stop_records(), research_mode=research_mode, execution_ledger=ledger)
    decisions = decision_snapshots_from_scan(scan, policy)
    outcomes = outcome_records_from_execution_ledger(ledger, policy)
    paths = _arena_learning_paths()
    existing_decision_keys = {_arena_learning_decision_key(item) for item in read_jsonl(paths["decisions"])}
    new_decisions = [item for item in decisions if _arena_learning_decision_key(item) not in existing_decision_keys]
    existing_outcome_keys = {_arena_learning_outcome_key(item) for item in read_jsonl(paths["outcomes"])}
    new_outcomes = [item for item in outcomes if _arena_learning_outcome_key(item) not in existing_outcome_keys]
    decisions_written = append_jsonl(paths["decisions"], new_decisions)
    outcomes_written = append_jsonl(paths["outcomes"], new_outcomes)
    report = build_learning_report(policy, read_jsonl(paths["decisions"]), read_jsonl(paths["outcomes"]))
    report = _learning_report_with_research_budget(report)
    return {
        "status": "OK",
        "command": "arena-learning-update",
        "policy_path": str(policy_path),
        "paths": {key: str(value) for key, value in paths.items()},
        "scan_status": scan.get("status"),
        "decisions_seen": len(decisions),
        "decisions_written": decisions_written,
        "outcomes_seen": len(outcomes),
        "outcomes_written": outcomes_written,
        "learning_report": report,
        "safety": _safety_payload(),
    }


def _arena_learning_report_output(policy: dict[str, Any], *, policy_path: Path) -> dict[str, Any]:
    paths = _arena_learning_paths()
    report = build_learning_report(policy, read_jsonl(paths["decisions"]), read_jsonl(paths["outcomes"]))
    report = _learning_report_with_research_budget(report)
    return {
        "status": report.get("status"),
        "command": "arena-learning-report",
        "policy_path": str(policy_path),
        "paths": {key: str(value) for key, value in paths.items()},
        "learning_report": report,
        "safety": _safety_payload(),
    }


def _arena_learning_propose_output(policy: dict[str, Any], *, policy_path: Path) -> dict[str, Any]:
    paths = _arena_learning_paths()
    proposal = build_learning_proposal(policy, read_jsonl(paths["decisions"]), read_jsonl(paths["outcomes"]))
    if isinstance(proposal.get("report"), dict):
        proposal = dict(proposal)
        proposal["report"] = _learning_report_with_research_budget(proposal["report"])
    append_jsonl(
        paths["suggestions"],
        [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": proposal.get("status"),
                "objective": proposal.get("objective"),
                "patch": proposal.get("patch") or {},
                "explanations": proposal.get("explanations") or [],
                "policy_write": False,
                "broker_mutation": False,
            }
        ],
    )
    return {
        "status": proposal.get("status"),
        "command": "arena-learning-propose",
        "policy_path": str(policy_path),
        "suggestion_path": str(paths["suggestions"]),
        "proposal": proposal,
        "safety": _safety_payload(),
    }


def _learning_report_with_research_budget(report: dict[str, Any]) -> dict[str, Any]:
    budget = _research_budget_report_output(root=DEFAULT_RESEARCH_ROOT, date=None, now=datetime.now(timezone.utc))
    totals = budget.get("totals") if isinstance(budget.get("totals"), dict) else {}
    return dict(report) | {
        "research_cost_usd": totals.get("estimated_cost_usd"),
        "research_budget_today": {
            "date": budget.get("date"),
            "provider_call_records": budget.get("provider_call_records"),
            "budget_attempts": budget.get("budget_attempts"),
            "totals": totals,
        },
    }


def _arena_learning_apply_output(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    confirm: str,
    backup_dir: Path | None,
) -> dict[str, Any]:
    if confirm != "APPLY_ARENA_LEARNING":
        return {
            "status": "BLOCKED",
            "command": "arena-learning-apply",
            "reason": "confirmation_required",
            "required_confirmation": "APPLY_ARENA_LEARNING",
            "policy_path": str(policy_path),
            "safety": _safety_payload(),
        }
    paths = _arena_learning_paths()
    proposal = build_learning_proposal(policy, read_jsonl(paths["decisions"]), read_jsonl(paths["outcomes"]))
    if not proposal.get("patch"):
        return {
            "status": "NO_CHANGE",
            "command": "arena-learning-apply",
            "reason": proposal.get("reason") or "empty_patch",
            "proposal": proposal,
            "policy_path": str(policy_path),
            "safety": _safety_payload(),
        }
    updated = apply_learning_patch(policy, proposal)
    validation = validate_arena_policy(updated)
    if validation["errors"]:
        return {
            "status": "BLOCKED",
            "command": "arena-learning-apply",
            "reason": "proposal_validation_failed",
            "validation": validation,
            "proposal": proposal,
            "policy_path": str(policy_path),
            "safety": _safety_payload(),
        }
    backup = _backup_policy_file(policy_path, backup_dir or (ROOT / "data" / "runtime" / "policy_backups"))
    _write_policy_file(policy_path, updated)
    return {
        "status": "APPLIED",
        "command": "arena-learning-apply",
        "policy_path": str(policy_path),
        "backup_path": str(backup),
        "write_applied": True,
        "proposal": proposal,
        "applied_policy": updated,
        "validation": validation,
        "safety": _safety_payload(policy_write=True),
    }


def _arena_learning_summary(policy: dict[str, Any]) -> dict[str, Any]:
    paths = _arena_learning_paths()
    report = build_learning_report(policy, read_jsonl(paths["decisions"]), read_jsonl(paths["outcomes"]))
    return {
        "mode": report.get("mode"),
        "objective": report.get("objective"),
        "decisions_count": report.get("decisions_count"),
        "outcomes_count": report.get("outcomes_count"),
        "closed_trades_count": report.get("closed_trades_count"),
        "net_pnl_rub": report.get("net_pnl_rub"),
        "top_gate_reasons": report.get("top_gate_reasons"),
        "suggestions_ready": report.get("suggestions_ready"),
        "next_commands": [
            "python scripts/hermes_operator.py arena-learning-update",
            "python scripts/hermes_operator.py arena-learning-report",
            "python scripts/hermes_operator.py arena-learning-propose",
        ],
    }


def _arena_learning_decision_key(record: dict[str, Any]) -> str:
    return "|".join(str(record.get(key) or "") for key in ("decision_id", "timestamp", "account_id", "symbol"))


def _arena_learning_outcome_key(record: dict[str, Any]) -> str:
    return "|".join(str(record.get(key) or "") for key in ("entry_order_id", "exit_order_id", "account_id", "symbol", "quantity"))


RESEARCH_LISTED_PRICES_PER_MILLION: dict[str, dict[str, Decimal]] = {
    "perplexity/sonar": {"input": Decimal("1"), "output": Decimal("1")},
    "perplexity/sonar-pro": {"input": Decimal("3"), "output": Decimal("15")},
}


def _research_budget_report_output(
    *,
    root: Path,
    date: str | None,
    now: datetime,
) -> dict[str, Any]:
    report_date = date or now.date().isoformat()
    try:
        target_date = datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        return {
            "status": "INVALID",
            "command": "research-budget-report",
            "reason": "invalid_date",
            "date": report_date,
            "expected_format": "YYYY-MM-DD",
            "safety": _safety_payload(),
        }
    records: list[dict[str, Any]] = []
    if root.exists():
        for path in sorted(root.rglob("*.json")):
            if "openrouter_budget" in path.relative_to(root).parts:
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if modified.date() != target_date:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                records.append(_research_budget_record(path, payload, modified=modified, root=root))
    groups = _research_budget_groups(records)
    totals = _research_budget_totals(groups)
    budget_attempts = _research_budget_attempts(root=root, target_date=target_date)
    return {
        "status": "OK",
        "command": "research-budget-report",
        "date": target_date.isoformat(),
        "root": str(root),
        "artifact_records": len(records),
        "provider_call_records": len([item for item in records if item.get("provider_call")]),
        "budget_attempts": budget_attempts,
        "groups": groups,
        "totals": totals,
        "pricing": {
            "basis": "listed OpenRouter token prices stored locally; provider-side search routing may differ",
            "per_million_tokens": {
                model: {key: decimal_payload(value) for key, value in prices.items()}
                for model, prices in RESEARCH_LISTED_PRICES_PER_MILLION.items()
            },
        },
        "safety": _safety_payload(),
    }


def _research_budget_record(path: Path, payload: dict[str, Any], *, modified: datetime, root: Path) -> dict[str, Any]:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    prompt_tokens = _int_value(usage.get("prompt_tokens")) or 0
    completion_tokens = _int_value(usage.get("completion_tokens")) or 0
    total_tokens = _int_value(usage.get("total_tokens")) or 0
    model = str(payload.get("model") or "unknown")
    provider = str(payload.get("provider") or "unknown")
    return {
        "path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
        "modified_at": modified.isoformat(timespec="seconds"),
        "provider": provider,
        "model": model,
        "mode": str(payload.get("mode") or "unknown"),
        "status": str(payload.get("status") or "unknown"),
        "symbols": [str(item) for item in payload.get("symbols") or []],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "provider_call": bool(payload.get("provider_call")) if "provider_call" in payload else provider == "openrouter" and total_tokens > 0,
        "cache_hit": _research_cache_hit(payload),
        "estimated_cost_usd": decimal_payload(
            _research_estimated_cost_usd(model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        ),
    }


def _research_budget_groups(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        key = (str(record.get("mode")), str(record.get("provider")), str(record.get("model")))
        item = grouped.setdefault(
            key,
            {
                "mode": key[0],
                "provider": key[1],
                "model": key[2],
                "artifact_records": 0,
                "provider_call_records": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "estimated_cost_usd": Decimal("0"),
            },
        )
        item["artifact_records"] += 1
        item["provider_call_records"] += 1 if record.get("provider_call") else 0
        item["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
        item["completion_tokens"] += int(record.get("completion_tokens") or 0)
        item["total_tokens"] += int(record.get("total_tokens") or 0)
        if record.get("cache_hit") is True:
            item["cache_hits"] += 1
        elif record.get("cache_hit") is False:
            item["cache_misses"] += 1
        item["estimated_cost_usd"] += _decimal(record.get("estimated_cost_usd")) or Decimal("0")
    result = []
    for item in grouped.values():
        copy_item = dict(item)
        copy_item["estimated_cost_usd"] = decimal_payload(copy_item["estimated_cost_usd"])
        result.append(copy_item)
    return sorted(result, key=lambda item: (str(item.get("mode")), str(item.get("provider")), str(item.get("model"))))


def _research_budget_totals(groups: list[dict[str, Any]]) -> dict[str, Any]:
    total_cost = Decimal("0")
    totals = {
        "artifact_records": 0,
        "provider_call_records": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hits": 0,
        "cache_misses": 0,
    }
    for item in groups:
        for key in totals:
            totals[key] += int(item.get(key) or 0)
        total_cost += _decimal(item.get("estimated_cost_usd")) or Decimal("0")
    return {**totals, "estimated_cost_usd": decimal_payload(total_cost)}


def _research_cache_hit(payload: dict[str, Any]) -> bool | None:
    if "cache_hit" in payload:
        return bool(payload.get("cache_hit"))
    cache = payload.get("cache") if isinstance(payload.get("cache"), dict) else {}
    status = str(cache.get("status") or "")
    if status == "hit":
        return True
    if status == "miss":
        return False
    return None


def _research_budget_attempts(*, root: Path, target_date: Any) -> dict[str, Any]:
    budget_root = root / "openrouter_budget"
    attempts: list[dict[str, Any]] = []
    if budget_root.exists():
        for path in sorted(budget_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for call in payload.get("calls") or []:
                if not isinstance(call, dict):
                    continue
                timestamp = _research_parse_datetime(str(call.get("timestamp") or ""))
                if timestamp is None or timestamp.date() != target_date:
                    continue
                attempts.append(
                    {
                        "timestamp": timestamp.isoformat(timespec="seconds"),
                        "mode": str(call.get("mode") or "unknown"),
                        "model": str(call.get("model") or "unknown"),
                        "symbols": [str(item) for item in call.get("symbols") or []],
                    }
                )
    grouped: dict[tuple[str, str], int] = {}
    for attempt in attempts:
        key = (str(attempt.get("mode")), str(attempt.get("model")))
        grouped[key] = grouped.get(key, 0) + 1
    return {
        "records": len(attempts),
        "groups": [
            {"mode": mode, "model": model, "attempts": count}
            for (mode, model), count in sorted(grouped.items())
        ],
    }


def _research_parse_datetime(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _research_estimated_cost_usd(model: str, *, prompt_tokens: int, completion_tokens: int) -> Decimal:
    prices = RESEARCH_LISTED_PRICES_PER_MILLION.get(model)
    if not prices:
        return Decimal("0")
    return (
        Decimal(prompt_tokens) * prices["input"] / Decimal("1000000")
        + Decimal(completion_tokens) * prices["output"] / Decimal("1000000")
    )


def _backup_policy_file(policy_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{policy_path.stem}.{timestamp}.json"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"{policy_path.stem}.{timestamp}.{suffix}.json"
        suffix += 1
    backup_path.write_text(policy_path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup_path


def _write_policy_file(policy_path: Path, policy: dict[str, Any]) -> None:
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = policy_path.with_name(f".{policy_path.name}.tmp")
    tmp_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(policy_path)


def _safety_payload(*, policy_write: bool = False, trading_mutations: bool = False) -> dict[str, bool]:
    return {
        "read_only_operator_cli": not (policy_write or trading_mutations),
        "trading_mutations": trading_mutations,
        "production_send": False,
        "policy_write": policy_write,
    }


def _journal(command: str, output: dict[str, Any], *, policy_path: Path) -> None:
    safe_output = redact_environment_values(output)
    candidates = safe_output.get("candidates") or safe_output.get("buy_candidates_now") or []
    safety = safe_output.get("safety") or {}
    write_event(
        {
            "command": command,
            "policy_path": str(policy_path),
            "status": safe_output.get("status"),
            "read_only": not bool(
                safety.get("policy_write") or safety.get("trading_mutations") or safety.get("production_send")
            ),
            "policy_write": bool(safety.get("policy_write")),
            "trading_mutations": bool(safety.get("trading_mutations")),
            "production_send": bool(safety.get("production_send")),
            "candidates_found": len(candidates) if isinstance(candidates, list) else 0,
            "gates_applied": _gate_reasons(candidates) if isinstance(candidates, list) else [],
            "errors": safe_output.get("errors") or [],
            "warnings": safe_output.get("warnings") or [],
        }
    )


def _gate_reasons(candidates: list[Any]) -> list[str]:
    reasons: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for reason in candidate.get("gate_reasons") or []:
            if reason not in reasons:
                reasons.append(str(reason))
    return reasons


if __name__ == "__main__":
    raise SystemExit(main())
