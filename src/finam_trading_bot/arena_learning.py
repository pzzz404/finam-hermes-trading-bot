"""Arena learning loop helpers for Hermes/Hermes.

The learning layer is intentionally propose-only by default. It records
machine-readable decisions and outcomes, then produces explainable policy
suggestions without bypassing Arena hard gates.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finam_trading_bot.contract import decimal_payload

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEARNING_ROOT = ROOT / "data" / "runtime"
DEFAULT_DECISION_EVENTS_PATH = DEFAULT_LEARNING_ROOT / "arena_learning_events.jsonl"
DEFAULT_OUTCOMES_PATH = DEFAULT_LEARNING_ROOT / "arena_decision_outcomes.jsonl"
DEFAULT_SUGGESTIONS_PATH = DEFAULT_LEARNING_ROOT / "arena_learning_suggestions.jsonl"

DEFAULT_PROTECTED_HARD_GATES = (
    "emergency_stop",
    "daily_loss",
    "account_drawdown",
    "unresolved_order_state",
    "missing_protective_stop",
    "open_risk_limit",
    "api_degradation",
    "research_provider_failure",
    "market_data_degraded_429",
    "arena_stop_check_not_clear",
    "unresolved_trade_safety_state",
    "approval_price_revalidation_failed",
    "approval_price_revalidation_unavailable",
    "short_availability_not_confirmed",
    "arena_margin_trading_not_supported",
    "repeat_pattern_loss_cooldown",
    "pretrade_budget_exhausted_blocks_autonomy",
    "pretrade_event_check_unavailable",
    "pretrade_event_check_risk",
)

DEFAULT_LEARNING_POLICY: dict[str, Any] = {
    "enabled": True,
    "mode": "propose_only",
    "objective": "risk_adjusted_growth",
    "horizons_minutes": [60, 240, 1440, 4320],
    "min_samples_for_suggestion": 20,
    "max_score_weight_delta_per_week": 5,
    "max_universe_rank_shift_per_week": 5,
    "protected_hard_gates": list(DEFAULT_PROTECTED_HARD_GATES),
    "deprioritized_score_penalty": 0,
    "deprioritized_entry_action": "score_only",
    "attribution_uncertain_score_penalty": 0,
    "attribution_uncertain_entry_action": "score_only",
    "repeat_pattern_loss": {
        "enabled": True,
        "account_ids": ["DEMO-US"],
        "markets": ["XNGS"],
        "scopes": [
            {"name": "DEMO-US_xngs", "account_ids": ["DEMO-US"], "markets": ["XNGS"], "side": "BUY"},
        ],
        "side": "BUY",
        "exit_reasons": ["EXIT_WEAK"],
        "quick_exit_max_hold_minutes": 240,
        "cooldown_hours": 24,
        "penalty_trading_days": 0,
        "score_penalty": 0,
    },
}

SCORE_COMPONENTS = (
    "trend_momentum",
    "expected_edge_after_cost",
    "relative_strength",
    "volatility_risk",
    "liquidity_execution",
    "research_regime",
    "technical_confirmation",
    "risk_adjusted_momentum",
    "news_event_context",
    "pair_relative_value",
    "learning_deprioritized",
    "learning_attribution_uncertain",
)


def learning_settings(policy: dict[str, Any]) -> dict[str, Any]:
    configured = policy.get("learning") if isinstance(policy.get("learning"), dict) else {}
    merged = dict(DEFAULT_LEARNING_POLICY)
    merged.update(configured)
    merged["horizons_minutes"] = [int(item) for item in merged.get("horizons_minutes") or []]
    merged["protected_hard_gates"] = [str(item) for item in merged.get("protected_hard_gates") or []]
    return merged


def validate_learning_policy(policy: dict[str, Any]) -> dict[str, list[str]]:
    settings = learning_settings(policy)
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(settings.get("enabled"), bool):
        errors.append("learning.enabled must be boolean")
    if settings.get("mode") not in {"propose_only", "auto_safe", "auto_aggressive"}:
        errors.append("learning.mode must be propose_only, auto_safe, or auto_aggressive")
    if settings.get("objective") not in {"risk_adjusted_growth", "max_growth", "execution_quality"}:
        errors.append("learning.objective must be risk_adjusted_growth, max_growth, or execution_quality")
    if not settings.get("horizons_minutes") or any(int(item) <= 0 for item in settings.get("horizons_minutes") or []):
        errors.append("learning.horizons_minutes must contain positive integers")
    for key in ("min_samples_for_suggestion", "max_score_weight_delta_per_week", "max_universe_rank_shift_per_week"):
        value = settings.get(key)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"learning.{key} must be a positive integer")
    protected = set(settings.get("protected_hard_gates") or [])
    if any(gate not in protected for gate in DEFAULT_PROTECTED_HARD_GATES):
        errors.append("learning.protected_hard_gates must include default hard gates")
    if settings.get("mode") != "propose_only":
        warnings.append("learning non-propose mode requires separate operator review before live rollout")
    repeat_pattern = settings.get("repeat_pattern_loss")
    if repeat_pattern is not None:
        if not isinstance(repeat_pattern, dict):
            errors.append("learning.repeat_pattern_loss must be an object")
        else:
            if not isinstance(repeat_pattern.get("enabled", True), bool):
                errors.append("learning.repeat_pattern_loss.enabled must be boolean")
            scopes = repeat_pattern.get("scopes")
            if scopes is not None:
                if not isinstance(scopes, list):
                    errors.append("learning.repeat_pattern_loss.scopes must be a list")
                else:
                    for index, scope in enumerate(scopes, start=1):
                        if not isinstance(scope, dict):
                            errors.append(f"learning.repeat_pattern_loss.scopes.{index} must be an object")
                            continue
                        if not scope.get("account_ids") or not isinstance(scope.get("account_ids"), list):
                            errors.append(f"learning.repeat_pattern_loss.scopes.{index}.account_ids must be a non-empty list")
                        if not scope.get("markets") or not isinstance(scope.get("markets"), list):
                            errors.append(f"learning.repeat_pattern_loss.scopes.{index}.markets must be a non-empty list")
                        if str(scope.get("side") or repeat_pattern.get("side") or "BUY").upper() not in {"BUY", "SELL"}:
                            errors.append(f"learning.repeat_pattern_loss.scopes.{index}.side must be BUY or SELL")
                        if str(scope.get("match_mode") or "setup_signature") not in {"setup_signature", "symbol_then_cluster"}:
                            errors.append(
                                f"learning.repeat_pattern_loss.scopes.{index}.match_mode must be setup_signature or symbol_then_cluster"
                            )
                        if scope.get("cluster_penalty_max_pre_score") is not None:
                            value = scope.get("cluster_penalty_max_pre_score")
                            if not isinstance(value, int) or value < 0 or value > 100:
                                errors.append(
                                    f"learning.repeat_pattern_loss.scopes.{index}.cluster_penalty_max_pre_score must be an integer from 0 to 100"
                                )
    return {"errors": errors, "warnings": warnings}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            records.append({"source": "arena_learning_jsonl_error", "path": str(path), "line": line_number})
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> int:
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return len(records)


def decision_snapshots_from_scan(scan: dict[str, Any], policy: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    settings = learning_settings(policy)
    if not settings.get("enabled", True):
        return []
    run_id = str(scan.get("run_id") or scan.get("generated_at") or current.isoformat(timespec="seconds"))
    accounts = {str(item.get("account_id") or ""): item for item in scan.get("accounts") or [] if isinstance(item, dict)}
    events: list[dict[str, Any]] = []
    for index, candidate in enumerate(scan.get("candidates") or [], start=1):
        if not isinstance(candidate, dict):
            continue
        account_id = str(candidate.get("account_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        if not account_id or not symbol:
            continue
        score = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
        components = score.get("components") if isinstance(score.get("components"), dict) else {}
        gates = [str(item) for item in candidate.get("gate_reasons") or []]
        decision_status = _decision_status(candidate)
        decision_id = str(candidate.get("decision_id") or _decision_id(run_id, account_id, symbol, index))
        event = {
            "timestamp": current.isoformat(timespec="seconds"),
            "run_id": run_id,
            "decision_id": decision_id,
            "account_id": account_id,
            "account_label": accounts.get(account_id, {}).get("label"),
            "symbol": symbol,
            "side": str(candidate.get("side") or "BUY").upper(),
            "decision_status": decision_status,
            "execution_allowed": bool(candidate.get("execution_allowed")),
            "gate_reasons": gates,
            "protected_gate_hit": bool(set(gates) & set(settings.get("protected_hard_gates") or [])),
            "entry_price": candidate.get("entry_price"),
            "current_price": candidate.get("current_price"),
            "stop_price": candidate.get("stop_price"),
            "take_profit_price": candidate.get("take_profit_price"),
            "risk_rub": candidate.get("risk_rub"),
            "risk_per_share": candidate.get("risk_per_share"),
            "notional": candidate.get("notional"),
            "entry_timeframe": candidate.get("entry_timeframe"),
            "score": score.get("score"),
            "score_label": score.get("label"),
            "score_components": {key: components.get(key) for key in SCORE_COMPONENTS if key in components},
            "research_verdict": score.get("research_verdict") or candidate.get("research_verdict"),
            "objective": settings.get("objective"),
            "source": "arena_scan",
        }
        events.append({key: value for key, value in event.items() if value is not None})
    return events


def outcome_records_from_execution_ledger(ledger: list[dict[str, Any]], policy: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    settings = learning_settings(policy)
    if not settings.get("enabled", True):
        return []
    current = now or datetime.now(timezone.utc)
    buys: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    outcomes: list[dict[str, Any]] = []
    for record in sorted([item for item in ledger if isinstance(item, dict)], key=lambda item: str(item.get("timestamp") or "")):
        account_id = str(record.get("account_id") or "")
        symbol = str(record.get("symbol") or "").upper()
        side = str(record.get("side") or "").upper()
        quantity = _decimal(record.get("quantity"))
        price = _decimal(record.get("price"))
        if not account_id or not symbol or quantity is None or quantity <= 0 or price is None or price <= 0:
            continue
        key = (account_id, symbol)
        if side == "BUY":
            buys[key].append(record)
            continue
        if side != "SELL":
            continue
        if not buys[key]:
            if str(record.get("action") or "").upper() == "EXIT_WEAK":
                outcomes.append(
                    {
                        "timestamp": current.isoformat(timespec="seconds"),
                        "account_id": account_id,
                        "symbol": symbol,
                        "side": "LONG_EXIT_WITHOUT_LEDGER_ENTRY",
                        "source": "arena_execution_ledger_unmatched_exit",
                        "exit_timestamp": record.get("timestamp"),
                        "exit_order_id": record.get("order_id"),
                        "quantity": decimal_payload(quantity, min_scale=1),
                        "exit_price": decimal_payload(price),
                        "exit_reason": record.get("action") or record.get("status"),
                        "learning_attribution_uncertain_symbol": True,
                        "reason": "exit_weak_seen_without_matching_buy_in_ledger; require manual review until full attribution is available",
                        "objective": settings.get("objective"),
                    }
                )
            continue
        buy = buys[key].pop(0)
        buy_qty = _decimal(buy.get("quantity")) or Decimal("0")
        buy_price = _decimal(buy.get("price")) or Decimal("0")
        matched_qty = min(quantity, buy_qty)
        if matched_qty <= 0:
            continue
        commission = (_decimal(record.get("estimated_commission")) or Decimal("0")) + (_decimal(buy.get("estimated_commission")) or Decimal("0"))
        pnl = (price - buy_price) * matched_qty - commission
        risk_rub = _decimal(buy.get("risk_rub"))
        mae_mfe_horizons = _mae_mfe_horizons(record, buy, settings=settings)
        outcome = {
            "timestamp": current.isoformat(timespec="seconds"),
            "account_id": account_id,
            "symbol": symbol,
            "side": "LONG_ROUND_TRIP",
            "source": "arena_execution_ledger",
            "entry_timestamp": buy.get("timestamp"),
            "exit_timestamp": record.get("timestamp"),
            "entry_order_id": buy.get("order_id"),
            "exit_order_id": record.get("order_id"),
            "quantity": decimal_payload(matched_qty, min_scale=1),
            "entry_price": decimal_payload(buy_price),
            "exit_price": decimal_payload(price),
            "net_pnl_rub": decimal_payload(pnl),
            "r_multiple": decimal_payload(pnl / risk_rub) if risk_rub and risk_rub > 0 else None,
            "mae_r": record.get("mae_r"),
            "mfe_r": record.get("mfe_r"),
            "mae_mfe_horizons": mae_mfe_horizons,
            "exit_reason": record.get("action") or record.get("status"),
            "objective": settings.get("objective"),
        }
        outcomes.append({key: value for key, value in outcome.items() if value is not None})
    return outcomes


def build_learning_report(policy: dict[str, Any], decisions: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    settings = learning_settings(policy)
    decisions = _learning_active_records(decisions)
    outcomes = _learning_active_records(outcomes)
    gate_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    symbol_counter: Counter[str] = Counter()
    component_totals: dict[str, list[int]] = defaultdict(list)
    for decision in decisions:
        status_counter[str(decision.get("decision_status") or "UNKNOWN")] += 1
        symbol = str(decision.get("symbol") or "")
        if symbol:
            symbol_counter[symbol] += 1
        for gate in decision.get("gate_reasons") or []:
            gate_counter[str(gate)] += 1
        components = decision.get("score_components") if isinstance(decision.get("score_components"), dict) else {}
        for key, value in components.items():
            dec = _decimal(value)
            if dec is not None:
                component_totals[key].append(int(dec))
    pnl_values = [_decimal(item.get("net_pnl_rub")) for item in outcomes]
    pnl_values = [item for item in pnl_values if item is not None]
    total_pnl = sum(pnl_values, Decimal("0"))
    wins = len([item for item in pnl_values if item > 0])
    closed = len(pnl_values)
    mae_samples = len([item for item in outcomes if _outcome_has_mae_mfe_sample(item)])
    repeat_cooldowns = gate_counter.get("repeat_pattern_loss_cooldown", 0)
    repeat_penalties = gate_counter.get("repeat_pattern_loss_score_below_min", 0)
    closed_pnls = [item for item in pnl_values if item is not None]
    average_closed_pnl = total_pnl / Decimal(closed) if closed else None
    equity_slope = _equity_slope_per_trade(closed_pnls)
    portfolio = policy.get("portfolio") if isinstance(policy.get("portfolio"), dict) else {}
    weak_exit_min_mae_samples = int(portfolio.get("weak_exit_min_mae_samples") or 30)
    return {
        "status": "OK",
        "mode": settings.get("mode"),
        "objective": settings.get("objective"),
        "decisions_count": len(decisions),
        "outcomes_count": len(outcomes),
        "closed_trades_count": closed,
        "win_rate_pct": decimal_payload(Decimal(wins) / Decimal(closed) * Decimal("100")) if closed else None,
        "net_pnl_rub": decimal_payload(total_pnl),
        "average_closed_trade_pnl_rub": decimal_payload(average_closed_pnl) if average_closed_pnl is not None else None,
        "equity_slope_rub_per_trade": decimal_payload(equity_slope) if equity_slope is not None else None,
        "avoided_repeat_loss_setups": repeat_cooldowns,
        "blocked_loss_recurrence": repeat_cooldowns + repeat_penalties,
        "research_cost_source": "research-budget-report",
        "research_cost_usd": None,
        "decision_status_counts": dict(status_counter),
        "top_gate_reasons": gate_counter.most_common(8),
        "top_symbols": symbol_counter.most_common(8),
        "score_component_averages": {
            key: decimal_payload(sum(Decimal(item) for item in values) / Decimal(len(values)))
            for key, values in component_totals.items()
            if values
        },
        "suggestions_ready": len(decisions) >= int(settings.get("min_samples_for_suggestion") or 20),
        "samples_required": int(settings.get("min_samples_for_suggestion") or 20),
        "protected_hard_gates": settings.get("protected_hard_gates"),
        "weak_exit_threshold_source": portfolio.get("weak_exit_threshold_source"),
        "weak_exit_mae_samples_count": mae_samples,
        "weak_exit_min_mae_samples": weak_exit_min_mae_samples,
        "weak_exit_threshold_ready": mae_samples >= weak_exit_min_mae_samples,
    }


def build_learning_proposal(policy: dict[str, Any], decisions: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    settings = learning_settings(policy)
    decisions = _learning_active_records(decisions)
    outcomes = _learning_active_records(outcomes)
    report = build_learning_report(policy, decisions, outcomes)
    if not settings.get("enabled", True):
        return {"status": "DISABLED", "reason": "learning_disabled", "patch": {}, "report": report}
    if len(decisions) < int(settings.get("min_samples_for_suggestion") or 20):
        return {
            "status": "NO_SUGGESTION",
            "reason": "not_enough_samples",
            "samples": len(decisions),
            "samples_required": int(settings.get("min_samples_for_suggestion") or 20),
            "patch": {},
            "report": report,
        }
    gate_counts = Counter(gate for decision in decisions for gate in decision.get("gate_reasons") or [])
    component_delta_limit = int(settings.get("max_score_weight_delta_per_week") or 5)
    patch: dict[str, Any] = {"learning": {"last_objective": settings.get("objective")}}
    explanations: list[str] = []
    if report.get("weak_exit_threshold_ready") is not True:
        patch.setdefault("learning", {})["weak_exit_threshold_status"] = "provisional_pending_mae_samples"
        explanations.append("weak-exit threshold remains provisional until enough MAE/MFE samples are collected")
    if gate_counts.get("candidate_score_below_min", 0) >= max(3, len(decisions) // 4):
        patch.setdefault("portfolio", {})["min_candidate_score_for_buy"] = "review:lower_by_5_or_recalibrate_score"
        explanations.append("candidate_score_below_min dominates; review whether score threshold is too strict for current universe")
    losing_symbols = _losing_symbols(outcomes)
    if losing_symbols:
        patch.setdefault("learning", {})["deprioritize_symbols"] = losing_symbols[:5]
        explanations.append("closed outcomes show negative net PnL for these symbols; lower universe priority, do not remove automatically")
    if int(report.get("avoided_repeat_loss_setups") or 0) > 0:
        patch.setdefault("learning", {})["repeat_pattern_loss_status"] = "active"
        explanations.append("repeat-pattern guard blocked fresh analog setups, helping the equity curve by avoiding recently losing regimes")
    attribution_uncertain_symbols = [symbol for symbol in _attribution_uncertain_symbols(outcomes) if symbol not in set(losing_symbols)]
    if attribution_uncertain_symbols:
        patch.setdefault("learning", {})["attribution_uncertain_symbols"] = attribution_uncertain_symbols[:5]
        explanations.append("unmatched weak exits have incomplete attribution; require manual review without classifying them as confirmed losers")
    net_pnl = _decimal(report.get("net_pnl_rub"))
    if report.get("closed_trades_count") and net_pnl is not None and net_pnl < 0:
        patch.setdefault("learning", {})["score_weight_delta_cap"] = component_delta_limit
        patch.setdefault("learning", {})["suggested_score_adjustments"] = {"volatility_risk": min(3, component_delta_limit)}
        explanations.append("negative realised PnL; tighten volatility risk contribution before increasing risk")
    if not explanations:
        patch.setdefault("learning", {})["observation"] = "sample_threshold_met_no_safe_policy_change"
        explanations.append("sample threshold met, but no safe explainable policy change crossed confidence thresholds")
    return {
        "status": "PROPOSED",
        "mode": settings.get("mode"),
        "objective": settings.get("objective"),
        "patch": patch,
        "explanations": explanations,
        "protected_hard_gates_unchanged": True,
        "broker_mutation": False,
        "policy_write": False,
        "report": report,
    }


def identify_positive_learning_candidates(
    policy: dict[str, Any],
    outcomes: list[dict[str, Any]],
    *,
    min_samples: int | None = None,
) -> dict[str, Any]:
    settings = learning_settings(policy)
    active = _learning_active_records(outcomes)
    threshold = min_samples
    if threshold is None:
        raw_threshold = settings.get("min_samples_for_promotion") or settings.get("min_samples_for_suggestion") or 20
        try:
            threshold = int(raw_threshold)
        except (TypeError, ValueError):
            threshold = 20
    threshold = max(2, int(threshold))
    demotion_threshold = settings.get("min_samples_for_demotion")
    try:
        demotion_threshold = int(demotion_threshold) if demotion_threshold is not None else min(3, threshold)
    except (TypeError, ValueError):
        demotion_threshold = min(3, threshold)
    demotion_threshold = max(2, demotion_threshold)
    max_acceptable_mae = _decimal(settings.get("promotion_max_mae_r") or "1.5") or Decimal("1.5")
    groups = _learning_outcome_groups(active)
    promotion: list[dict[str, Any]] = []
    probation: list[dict[str, Any]] = []
    demotion: list[dict[str, Any]] = []
    for key, records in groups.items():
        summary = _learning_group_summary(key, records, max_acceptable_mae=max_acceptable_mae)
        net_pnl = _decimal(summary.get("net_pnl_rub"))
        if net_pnl is not None and net_pnl < 0 and summary["sample_count"] >= demotion_threshold:
            summary["reason"] = "negative_realised_edge"
            demotion.append(summary)
        elif summary["sample_count"] < threshold:
            summary["reason"] = "not_enough_samples_for_promotion"
            probation.append(summary)
        elif net_pnl is not None and net_pnl > 0 and summary["mae_status"] != "unacceptable":
            summary["reason"] = "positive_edge_with_min_samples"
            promotion.append(summary)
        else:
            summary["reason"] = "attribution_uncertain_or_flat"
            probation.append(summary)
    promotion.sort(key=lambda item: (_decimal(item.get("net_pnl_rub")) or Decimal("0"), item.get("sample_count") or 0), reverse=True)
    demotion.sort(key=lambda item: (_decimal(item.get("net_pnl_rub")) or Decimal("0"), item.get("sample_count") or 0))
    probation.sort(key=lambda item: (item.get("sample_count") or 0, _decimal(item.get("net_pnl_rub")) or Decimal("0")), reverse=True)
    return {
        "status": "OK",
        "min_samples_for_promotion": threshold,
        "min_samples_for_demotion": demotion_threshold,
        "promotion_candidates": promotion,
        "probation_candidates": probation[:20],
        "demotion_candidates": demotion[:20],
        "summary": "no positive edge yet" if not promotion else "positive edge candidates require operator review before any policy change",
        "broker_mutation": False,
        "policy_write": False,
    }


def apply_learning_patch(policy: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    patch = proposal.get("patch") if isinstance(proposal.get("patch"), dict) else {}
    updated = json.loads(json.dumps(policy, ensure_ascii=False))
    learning_patch = patch.get("learning") if isinstance(patch.get("learning"), dict) else {}
    if learning_patch:
        updated.setdefault("learning", {})
        for key, value in learning_patch.items():
            updated["learning"][key] = value
    return updated


def _learning_outcome_groups(outcomes: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        account_id = str(outcome.get("account_id") or "")
        symbol = str(outcome.get("symbol") or "").upper()
        if not account_id or not symbol:
            continue
        setup = str(outcome.get("setup_signature") or outcome.get("setup") or outcome.get("side") or "LONG_ROUND_TRIP").upper()
        groups[(account_id, symbol, setup)].append(outcome)
    return groups


def _learning_group_summary(
    key: tuple[str, str, str],
    records: list[dict[str, Any]],
    *,
    max_acceptable_mae: Decimal,
) -> dict[str, Any]:
    account_id, symbol, setup = key
    pnl_values = [_decimal(item.get("net_pnl_rub")) for item in records]
    pnl_values = [item for item in pnl_values if item is not None]
    wins = len([item for item in pnl_values if item > 0])
    total = sum(pnl_values, Decimal("0"))
    mae_values = [_decimal(item.get("mae_r")) for item in records]
    mae_values = [item for item in mae_values if item is not None]
    worst_mae = max(mae_values) if mae_values else None
    mae_status = "unknown"
    if worst_mae is not None:
        mae_status = "acceptable" if worst_mae <= max_acceptable_mae else "unacceptable"
    equity_slope = _equity_slope_per_trade(pnl_values)
    return {
        "account_id": account_id,
        "symbol": symbol,
        "setup": setup,
        "sample_count": len(pnl_values),
        "win_rate_pct": decimal_payload(Decimal(wins) / Decimal(len(pnl_values)) * Decimal("100")) if pnl_values else None,
        "net_pnl_rub": decimal_payload(total),
        "equity_slope_rub_per_trade": decimal_payload(equity_slope) if equity_slope is not None else None,
        "mae_status": mae_status,
        "worst_mae_r": decimal_payload(worst_mae) if worst_mae is not None else None,
    }


def _decision_status(candidate: dict[str, Any]) -> str:
    status = str(candidate.get("status") or "").upper()
    if status in {"BLOCKED", "PROPOSE_ONLY", "EXECUTED"}:
        return status
    if candidate.get("execution_allowed") is True:
        return "EXECUTABLE"
    if candidate.get("gate_reasons"):
        return "BLOCKED"
    return "PROPOSE_ONLY"


def _learning_active_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return records that should participate in future learning decisions.

    Raw learning JSONL files are append-only audit artifacts. When the operator
    later identifies a policy/setup incident, records can be marked with
    ``exclude_from_learning=true`` instead of deleting history. Reports and
    proposals ignore those records so incident outcomes do not bias score
    calibration or risk-policy suggestions.
    """

    return [item for item in records if not bool(item.get("exclude_from_learning"))]


def _decision_id(run_id: str, account_id: str, symbol: str, index: int) -> str:
    seed = f"{run_id}|{account_id}|{symbol}|{index}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _losing_symbols(outcomes: list[dict[str, Any]]) -> list[str]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for outcome in outcomes:
        symbol = str(outcome.get("symbol") or "")
        pnl = _decimal(outcome.get("net_pnl_rub"))
        if symbol and pnl is not None:
            totals[symbol] += pnl
    return [symbol for symbol, pnl in sorted(totals.items(), key=lambda item: item[1]) if pnl < 0]


def _attribution_uncertain_symbols(outcomes: list[dict[str, Any]]) -> list[str]:
    symbols = {
        str(outcome.get("symbol") or "")
        for outcome in outcomes
        if (outcome.get("learning_attribution_uncertain_symbol") is True or outcome.get("learning_penalty_symbol") is True)
        and str(outcome.get("symbol") or "").strip()
    }
    return sorted(symbols)


def _mae_mfe_horizons(record: dict[str, Any], buy: dict[str, Any], *, settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    source = _first_dict(record.get("mae_mfe_horizons"), record.get("mae_mfe_by_horizon"), buy.get("mae_mfe_horizons"), buy.get("mae_mfe_by_horizon"))
    if not source:
        return {}
    horizons: dict[str, dict[str, Any]] = {}
    for horizon in settings.get("horizons_minutes") or []:
        key = str(int(horizon))
        raw = source.get(key) or source.get(int(horizon))
        if not isinstance(raw, dict):
            continue
        mae = raw.get("mae_r")
        mfe = raw.get("mfe_r")
        if _decimal(mae) is None or _decimal(mfe) is None:
            continue
        horizons[key] = {
            "mae_r": decimal_payload(_decimal(mae)),
            "mfe_r": decimal_payload(_decimal(mfe)),
            "source": raw.get("source") or "arena_execution_ledger",
        }
    return horizons


def _outcome_has_mae_mfe_sample(outcome: dict[str, Any]) -> bool:
    if _decimal(outcome.get("mae_r")) is not None and _decimal(outcome.get("mfe_r")) is not None:
        return True
    horizons = outcome.get("mae_mfe_horizons") if isinstance(outcome.get("mae_mfe_horizons"), dict) else {}
    for item in horizons.values():
        if isinstance(item, dict) and _decimal(item.get("mae_r")) is not None and _decimal(item.get("mfe_r")) is not None:
            return True
    return False


def _first_dict(*values: Any) -> dict[str, Any] | None:
    for value in values:
        if isinstance(value, dict):
            return value
    return None


def _equity_slope_per_trade(pnls: list[Decimal]) -> Decimal | None:
    if len(pnls) < 2:
        return None
    cumulative: list[Decimal] = []
    running = Decimal("0")
    for pnl in pnls:
        running += pnl
        cumulative.append(running)
    return (cumulative[-1] - cumulative[0]) / Decimal(len(cumulative) - 1)
