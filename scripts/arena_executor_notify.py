#!/usr/bin/env python3
"""Run Arena executor and send compact event-driven Telegram alerts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import h4_monitor_notify  # noqa: E402
import hermes_operator  # noqa: E402
from finam_trading_bot.arena import load_arena_policy  # noqa: E402
from finam_trading_bot.redaction import redact_environment_values  # noqa: E402


DEFAULT_ARENA_POLICY_PATH = ROOT / "config" / "finam_arena_policy.json"
ARENA_POLICY_ENV = "FINAM_ARENA_POLICY_PATH"
DEFAULT_ALERT_STATE_PATH = ROOT / "data" / "runtime" / "arena_executor_alert_state.json"
ARENA_ALERT_TIMEZONE = ZoneInfo("Europe/Moscow")
BLOCKED_ALERT_COOLDOWN_SECONDS = 4 * 60 * 60
SUPPRESSIBLE_BLOCKED_GATES = {
    "account_gross_exposure_limit_reached",
    "candidate_score_below_min",
    "daily_new_notional_limit_exceeded",
    "entry_strength_confirmation_missing",
    "learning_attribution_uncertain_requires_manual_review",
    "learning_deprioritized_requires_manual_review",
    "pretrade_budget_exhausted_blocks_autonomy",
    "pretrade_event_check_risk",
    "pretrade_event_check_unavailable",
    "primary_daily_limit_reached",
    "repeat_pattern_loss_cooldown",
    "repeat_pattern_loss_score_below_min",
    "same_symbol_position_open",
    "same_symbol_trade_today",
    "symbol_exposure_limit_exceeded",
}


def main() -> int:
    hermes_operator._load_default_operator_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--arena-policy", default=str(_default_arena_policy_path()))
    parser.add_argument("--alert-state", default=str(DEFAULT_ALERT_STATE_PATH))
    parser.add_argument("--once", action="store_true", help="Run one Arena executor pass.")
    parser.add_argument("--live", action="store_true", help="Request live Arena executor mode.")
    parser.add_argument("--execute-live", action="store_true", help="Explicitly allow this wrapper to execute live mutations when env gate is also enabled.")
    parser.add_argument("--dry-run", action="store_true", help="Build alert but do not send Telegram.")
    args = parser.parse_args()
    if not args.once:
        parser.error("Only --once mode is supported")
    output = build_arena_executor_alert_output(
        policy_path=Path(args.arena_policy),
        alert_state_path=Path(args.alert_state),
        live=bool(args.live),
        execute_live=bool(args.execute_live),
        dry_run=bool(args.dry_run),
    )
    # Diagnostic output is recursively redacted before this sink; covered by sentinel-secret tests.
    print(json.dumps(redact_environment_values(output), ensure_ascii=False, default=str, indent=2))  # lgtm [py/clear-text-logging-sensitive-data]
    return 0 if output.get("status") not in {"FAILED"} else 1


def _default_arena_policy_path() -> Path:
    raw = str(os.environ.get(ARENA_POLICY_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_ARENA_POLICY_PATH


def build_arena_executor_alert_output(
    *,
    policy_path: Path,
    alert_state_path: Path,
    live: bool,
    dry_run: bool,
    execute_live: bool = False,
) -> dict[str, Any]:
    policy = load_arena_policy(policy_path)
    effective_live = bool(live and not dry_run)
    if effective_live:
        executor_gate = "FINAM_ARENA_EXECUTOR_MUTATIONS_ENABLED"
        if str(os.environ.get(executor_gate, "")).strip().lower() != "true":
            return {
                "status": "LIVE_GATE_REQUIRED",
                "command": "arena-executor-notify",
                "policy_path": str(policy_path),
                "alert_state_path": str(alert_state_path),
                "reason": f"{executor_gate}_not_true",
                "required_env": executor_gate,
                "live_requested": True,
                "safety": {"read_only": False, "trading_mutations": False, "policy_write": False},
            }
        if not execute_live:
            return {
                "status": "LIVE_GATE_REQUIRED",
                "command": "arena-executor-notify",
                "policy_path": str(policy_path),
                "alert_state_path": str(alert_state_path),
                "reason": "missing_--execute-live",
                "required_flag": "--execute-live",
                "live_requested": True,
                "safety": {"read_only": False, "trading_mutations": False, "policy_write": False},
            }
    pretrade_alert: dict[str, Any] | None = None
    codex_reviews: dict[str, Any] | None = None
    reviewed_scan: dict[str, Any] | None = None
    live_research_mode = "budgeted" if effective_live else "cache_only"
    if effective_live:
        pretrade_preview = hermes_operator._arena_run_all_output(
            policy,
            policy_path=policy_path,
            live=False,
            research_mode=live_research_mode,
            include_scan=True,
        )
        preview_scan = pretrade_preview.get("scan") if isinstance(pretrade_preview.get("scan"), dict) else None
        codex_reviews = hermes_operator._arena_executor_codex_review_step(
            policy,
            scan=preview_scan,
            live=effective_live,
            env=os.environ,
        )
        reviewed_scan = hermes_operator._arena_scan_with_codex_reviews(policy, preview_scan, codex_reviews)
        if reviewed_scan is not None and reviewed_scan is not preview_scan:
            pretrade_preview = hermes_operator._arena_run_all_output(
                policy,
                policy_path=policy_path,
                live=False,
                research_mode=live_research_mode,
                scan=reviewed_scan,
            )
        if codex_reviews:
            pretrade_preview["codex_reviews"] = codex_reviews
        pretrade_text = format_arena_executor_pretrade_alert(pretrade_preview)
        if pretrade_text:
            h4_monitor_notify.validate_telegram_html(pretrade_text)
            pretrade_alert = {
                "status": pretrade_preview.get("status"),
                "telegram_text": pretrade_text,
                "telegram_delivery": "pending",
                "run_all": pretrade_preview,
            }
            if dry_run:
                pretrade_alert["telegram_delivery"] = "dry_run"
            else:
                try:
                    h4_monitor_notify.send_telegram_message(pretrade_text)
                except Exception as exc:  # noqa: BLE001 - do not trade silently if Telegram is unavailable.
                    return {
                        "status": "FAILED",
                        "command": "arena-executor-notify",
                        "policy_path": str(policy_path),
                        "alert_state_path": str(alert_state_path),
                        "reason": "pretrade_alert_delivery_failed",
                        "pretrade_alert": pretrade_alert | {"telegram_delivery": "failed", "error": redact_environment_values(exc)},
                        "telegram_delivery": "failed",
                        "safety": {"read_only": False, "trading_mutations": False, "policy_write": False},
                    }
                pretrade_alert["telegram_delivery"] = "ok"
    run_all_kwargs: dict[str, Any] = {
        "policy_path": policy_path,
        "live": effective_live,
        "research_mode": live_research_mode,
    }
    if reviewed_scan is not None:
        run_all_kwargs["scan"] = reviewed_scan
    run_all = hermes_operator._arena_run_all_output(policy, **run_all_kwargs)
    if codex_reviews:
        run_all["codex_reviews"] = codex_reviews
    alert_run_all = _alert_run_all_with_readonly_preview(
        policy,
        policy_path=policy_path,
        run_all=run_all,
        live=effective_live,
    )
    _attach_execution_ledger_tail(alert_run_all)
    alert_text = format_arena_executor_alert(alert_run_all)
    output = {
        "status": run_all.get("status"),
        "command": "arena-executor-notify",
        "policy_path": str(policy_path),
        "alert_state_path": str(alert_state_path),
        "live_requested": effective_live,
        "run_all": run_all,
        "telegram_delivery": "not_needed",
        "safety": {
            "read_only": False,
            "trading_mutations": bool((run_all.get("safety") or {}).get("trading_mutations")),
            "policy_write": False,
        },
    }
    if pretrade_alert:
        output["pretrade_alert"] = pretrade_alert
    if codex_reviews:
        output["codex_reviews"] = codex_reviews
    if alert_run_all is not run_all:
        output["alert_run_all"] = alert_run_all
    if not alert_text:
        return output
    h4_monitor_notify.validate_telegram_html(alert_text)
    fingerprint = _alert_fingerprint(alert_run_all, alert_text)
    output["telegram_text"] = alert_text
    output["alert_fingerprint"] = fingerprint
    blocked_suppression = _blocked_alert_suppression(alert_state_path, alert_run_all)
    if blocked_suppression.get("suppressed"):
        output["telegram_delivery"] = "suppressed"
        output["suppressed_reason"] = "stable_blocked_gate"
        output["blocked_alert"] = blocked_suppression
        return output
    if _is_duplicate_alert(alert_state_path, fingerprint) and _approval_alert_has_active_pending(alert_run_all, env=os.environ):
        output["telegram_delivery"] = "deduped"
        return output
    if dry_run:
        output["telegram_delivery"] = "dry_run"
        return output
    pending = hermes_operator.persist_arena_pending_approvals_from_run_all(
        alert_run_all,
        policy_path=policy_path,
    )
    if pending:
        output["pending_approvals"] = [
            {
                "confirmation": item.get("confirmation"),
                "account_id": item.get("account_id"),
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "expires_at": item.get("expires_at"),
                "proposal_hash": item.get("proposal_hash"),
            }
            for item in pending
        ]
    try:
        h4_monitor_notify.send_telegram_message(alert_text)
    except Exception as exc:  # noqa: BLE001 - systemd should surface delivery failures.
        output["status"] = "FAILED"
        output["telegram_delivery"] = "failed"
        output["error"] = redact_environment_values(exc)
        return output
    _write_alert_state(alert_state_path, fingerprint, blocked_signature=blocked_suppression.get("signature"))
    output["telegram_delivery"] = "ok"
    return output


def _alert_run_all_with_readonly_preview(
    policy: dict[str, Any],
    *,
    policy_path: Path,
    run_all: dict[str, Any],
    live: bool,
) -> dict[str, Any]:
    if not live:
        return run_all
    reason = _live_gate_reason(run_all)
    if not reason:
        return run_all
    preview = hermes_operator._arena_run_all_output(policy, policy_path=policy_path, live=False, research_mode="cache_only")
    if not _has_alertable_run(preview):
        return run_all
    marked = copy.deepcopy(preview)
    marked["status"] = "LIVE_GATE_REQUIRED"
    marked["live_requested"] = True
    marked["live_gate_reason"] = reason
    for run in marked.get("runs") or []:
        if not isinstance(run, dict):
            continue
        if run.get("command") == "arena-portfolio-run" and isinstance(run.get("planned_actions"), list) and run["planned_actions"]:
            run["status"] = "LIVE_GATE_REQUIRED"
            run["reason"] = reason
            run["live_requested"] = True
    return marked


def _live_gate_reason(run_all: dict[str, Any]) -> str:
    if run_all.get("status") != "LIVE_GATE_REQUIRED":
        return ""
    for run in run_all.get("runs") or []:
        if isinstance(run, dict) and run.get("status") == "LIVE_GATE_REQUIRED":
            return str(run.get("reason") or "live_gate_required")
    return "live_gate_required"


def _has_alertable_run(run_all: dict[str, Any]) -> bool:
    return any(_alert_kind(run) for run in _iter_alert_runs(run_all))


def format_arena_executor_alert(run_all: dict[str, Any]) -> str:
    runs = list(_iter_alert_runs(run_all))
    important = [run for run in runs if _alert_kind(run)]
    if not important:
        return ""
    lines = [
        "<b>🏟️ Arena: торговый контур</b>",
        f"📌 Состояние: {_esc(_status_label(run_all.get('status')))}",
    ]
    account_lines = _account_summary_lines(run_all)
    if account_lines:
        lines.extend(["", "<b>Счета</b>"])
        lines.extend(account_lines)
    review_lines = _codex_review_status_lines(run_all)
    if review_lines:
        lines.extend(["", "<b>Codex-review</b>"])
        lines.extend(review_lines)
    for run in important[:6]:
        kind = _alert_kind(run)
        if kind == "planned":
            lines.extend(["", "<b>План по портфелю</b>", f"📌 {_esc(_status_label(run.get('status')))}"])
            lines.extend(_planned_action_lines(run))
            if run.get("status") == "LIVE_GATE_REQUIRED":
                lines.append(f"🚦 Автоторги выключены: {_esc(_reason_label(run.get('reason')))}")
            elif run.get("live_requested") is False:
                lines.append("🧪 Проверочный прогон: заявка брокеру не отправлялась")
            continue
        raw_reason = str(run.get("reason") or "")
        raw_symbol = _run_symbol(run)
        if kind == "halt" and raw_reason == "arena_stop_check_not_clear" and raw_symbol == "UNKNOWN":
            account_id = "Защитные стопы"
            symbol = "проверка"
        else:
            account_id = _esc(run.get("account_id") or "n/a")
            symbol = _esc(raw_symbol)
        raw_status = str(run.get("status") or "")
        status = _esc(_status_label(run.get("status")))
        reason = _esc(_reason_label(run.get("reason")))
        lines.extend(["", f"<b>{account_id}</b> · {symbol}", f"📌 {status}"])
        if kind == "approval":
            confirmation = _esc(run.get("required_confirmation"))
            lines.extend(_approval_explanation_lines(run))
            lines.append(f"✋ Ожидает подтверждения: {confirmation}")
            lines.append(f"▶️ Команда: python scripts/hermes_operator.py arena-confirm --live --confirmation \"{confirmation}\"")
            lines.append("⏱️ Подтверждение действует 10 минут; исполнение пойдёт по сохранённому снимку идеи после свежей revalidation.")
        elif kind == "executed":
            fill = _run_fill_state(run)
            soft_stop = run.get("soft_stop") if isinstance(run.get("soft_stop"), dict) else {}
            action = run.get("action")
            if action:
                lines.append(f"✅ Действие по портфелю: {_esc(_action_label(action))}; объём: {_esc(fill.get('executed_quantity') or 'n/a')}")
            else:
                lines.append(f"✅ Сделка исполнена; объём: {_esc(fill.get('executed_quantity') or 'n/a')}")
            if raw_status == "ARENA_SOFT_STOP_TRIGGERED":
                lines.append("🛡️ Мягкий стоп сработал: отправлена заявка на выход")
            elif soft_stop:
                lines.append(
                    f"🛡️ Мягкий стоп активен: {_esc(_side_label(soft_stop.get('side')))} {_esc(_price(soft_stop.get('stop_price')))}"
                )
            elif isinstance(run.get("exit_order_payload"), dict):
                payload = run["exit_order_payload"]
                lines.append(f"🧾 Заявка на выход: {_esc(_side_label(payload.get('side')))} {_esc(payload.get('symbol'))}")
            else:
                stop = run.get("stop_verification") if isinstance(run.get("stop_verification"), dict) else {}
                lines.append(f"🛡️ Защитный стоп проверен: {_yes_no(stop.get('verified'))}")
        elif kind == "halt":
            lines.append(f"🚨 Остановка: {reason}")
            lines.append("🚦 Новые входы заблокированы до разбора")
            if raw_reason == "arena_stop_check_not_clear":
                lines.extend(_stop_check_halt_context_lines(run, run_all))
        elif kind == "blocked":
            lines.append(f"⏸️ Заявка не отправлена: {reason}")
            lines.extend(_blocked_explanation_lines(run, run_all=run_all))
        else:
            lines.append(f"⚠️ {reason}")
    if len(important) > 6:
        lines.append("")
        lines.append(f"… ещё {len(important) - 6} событий")
    return "\n".join(lines)


def _attach_execution_ledger_tail(run_all: dict[str, Any], *, limit: int = 40) -> None:
    if "_execution_ledger_tail" in run_all:
        return
    try:
        ledger = hermes_operator._read_arena_execution_ledger(env=os.environ)
    except Exception:  # noqa: BLE001 - alert formatting must not fail because ledger is unavailable.
        return
    records = [item for item in ledger if isinstance(item, dict)]
    if records:
        run_all["_execution_ledger_tail"] = records[-limit:]


def format_arena_executor_pretrade_alert(run_all: dict[str, Any]) -> str:
    runs = _pretrade_alert_runs(run_all)
    if not runs:
        return ""
    lines = [
        "<b>🏟️ Arena: план входа</b>",
        "⚠️ Сейчас будет live BUY, если последняя проверка цены и стопов останется зелёной.",
    ]
    review_lines = _codex_review_status_lines(run_all)
    if review_lines:
        lines.extend(["", "<b>Codex-review</b>"])
        lines.extend(review_lines)
    for run in runs[:3]:
        account_id = _esc(run.get("account_id") or "n/a")
        symbol = _esc(_run_symbol(run))
        lines.extend(["", f"<b>{account_id}</b> · {symbol}", "📌 До отправки брокеру"])
        lines.extend(
            _approval_explanation_lines(
                run,
                green_gate_line="✅ Риск-гейты зелёные; следующий шаг — live BUY",
            )
        )
        proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
        proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
        candidate = proposal_output.get("candidate") if isinstance(proposal_output.get("candidate"), dict) else {}
        score = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
        if score:
            lines.append(f"📊 Скоринг: {_esc(score.get('score'))} / {_esc(_score_label(score.get('label')))}")
        research_line = _research_source_line(candidate)
        if research_line:
            lines.append(research_line)
        confirmation = str(proposal.get("execution", {}).get("confirmation_phrase") or "")
        if confirmation:
            lines.append(f"🔐 Авторизация live-route: {_esc(confirmation)}")
    if len(runs) > 3:
        lines.append("")
        lines.append(f"… ещё {len(runs) - 3} входов")
    return "\n".join(lines)


def _codex_review_status_lines(run_all: dict[str, Any]) -> list[str]:
    reviews = run_all.get("codex_reviews") if isinstance(run_all.get("codex_reviews"), dict) else {}
    if not reviews:
        return []
    records = reviews.get("records") if isinstance(reviews.get("records"), list) else []
    if not records:
        reason = reviews.get("reason")
        if reason:
            return [f"🧠 {_esc(_review_status_label(reviews.get('status')))}: {_esc(_reason_label(reason))}"]
        return [f"🧠 {_esc(_review_status_label(reviews.get('status')))}"]
    lines: list[str] = []
    for record in records[:4]:
        if not isinstance(record, dict):
            continue
        symbol = record.get("symbol") or "n/a"
        review = record.get("review") if isinstance(record.get("review"), dict) else {}
        verdict = record.get("verdict") or review.get("verdict")
        status = _review_status_label(record.get("status"))
        if verdict:
            lines.append(f"🧠 {_esc(symbol)}: {_esc(status)}, verdict {_esc(verdict)}")
        else:
            reason = record.get("reason")
            suffix = f", {_reason_label(reason)}" if reason else ""
            lines.append(f"🧠 {_esc(symbol)}: {_esc(status)}{_esc(suffix)}")
    if len(records) > 4:
        lines.append(f"… ещё {len(records) - 4} review-событий")
    return lines


def _review_status_label(status: Any) -> str:
    labels = {
        "OK": "review записан",
        "EXISTS": "свежий review найден",
        "PLANNED": "review запрошен",
        "FAIL_CLOSED": "review не получен, fail-closed",
        "SKIPPED": "review не требовался",
    }
    return labels.get(str(status or ""), str(status or "n/a"))


def _iter_alert_runs(run_all: dict[str, Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for run in run_all.get("runs") or []:
        if not isinstance(run, dict):
            continue
        if run.get("command") == "arena-portfolio-run" and isinstance(run.get("results"), list):
            results = [item for item in run["results"] if isinstance(item, dict)]
            flattened.extend(results or [run])
            continue
        flattened.append(run)
    return flattened


def _pretrade_alert_runs(run_all: dict[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    if _has_portfolio_plan(run_all):
        return runs
    current = datetime.now(timezone.utc)
    for run in _iter_alert_runs(run_all):
        if not isinstance(run, dict):
            continue
        if str(run.get("status") or "") != "DRY_RUN" or str(run.get("command") or "") != "arena-run":
            continue
        proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
        proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
        if str(proposal.get("side") or "").upper() != "BUY":
            continue
        execution = proposal.get("execution") if isinstance(proposal.get("execution"), dict) else {}
        if str(execution.get("route") or "") != "auto_direct":
            continue
        gates = proposal.get("gates") if isinstance(proposal.get("gates"), dict) else {}
        if gates.get("execution_allowed") is not True:
            continue
        if hermes_operator._arena_market_session_block(proposal_output, now=current) is not None:
            continue
        runs.append(run)
    return runs


def _has_portfolio_plan(run_all: dict[str, Any]) -> bool:
    for run in run_all.get("runs") or []:
        if isinstance(run, dict) and str(run.get("command") or "") == "arena-portfolio-run":
            actions = run.get("planned_actions") if isinstance(run.get("planned_actions"), list) else []
            if actions:
                return True
    return False


def _alert_kind(run: dict[str, Any]) -> str:
    status = str(run.get("status") or "")
    planned_actions = run.get("planned_actions") if isinstance(run.get("planned_actions"), list) else []
    if status in {"DRY_RUN", "LIVE_GATE_REQUIRED"} and planned_actions:
        return "planned"
    if status in {
        "EXECUTED_ARENA",
        "EXECUTED_ARENA_PARTIAL",
        "EXECUTED_ARENA_SOFT_STOP_ACTIVE",
        "EXECUTED_ARENA_PARTIAL_SOFT_STOP_ACTIVE",
        "EXECUTED_ARENA_PORTFOLIO",
        "EXECUTED_ARENA_REPLACEMENT",
        "EXECUTED_ARENA_EXIT_CASH",
        "ARENA_SOFT_STOP_TRIGGERED",
    }:
        return "executed"
    if status in {"BROKER_ERROR", "STOP_NOT_VERIFIED", "ENTRY_PENDING_NO_STOP", "HALT", "REPLACE_SELL_DONE_BUY_BLOCKED"}:
        return "halt"
    if status == "CONFIRMATION_REQUIRED":
        return "approval"
    if status == "BLOCKED" and run.get("gate_reasons"):
        return "blocked"
    return ""


def _run_fill_state(run: dict[str, Any]) -> dict[str, Any]:
    for key in ("entry_fill_state", "exit_fill_state"):
        value = run.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _run_symbol(run: dict[str, Any]) -> str:
    proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
    candidate = proposal_output.get("candidate") if isinstance(proposal_output.get("candidate"), dict) else {}
    replacement = run.get("replacement") if isinstance(run.get("replacement"), dict) else {}
    if str(run.get("action") or "") == "REPLACE":
        return f"{replacement.get('sell_symbol') or '?'}→{replacement.get('buy_symbol') or '?'}"
    return str(proposal.get("symbol") or candidate.get("symbol") or run.get("symbol") or "UNKNOWN")


def _stop_check_halt_context_lines(run: dict[str, Any], run_all: dict[str, Any]) -> list[str]:
    stop_check = _stop_check_payload(run, run_all)
    lines = ["🧯 Повторный BUY не отправлять: сначала подтвердить broker/session и защитные стопы."]
    error = str(stop_check.get("error") or run.get("error") or "").strip()
    if error:
        lines.append(f"🔌 Broker/session: {_esc(error)}")
    active_stops = _active_soft_stop_checks(stop_check)
    latest_buys = _latest_buy_records_for_stops(run_all, active_stops)
    for record in latest_buys[:3]:
        lines.append(
            "✅ Последний BUY: "
            f"{_esc(record.get('account_id') or 'n/a')} {_esc(record.get('symbol') or 'n/a')}, "
            f"{_esc(record.get('quantity') or 'n/a')} @ {_esc(_price(record.get('price')))}"
        )
        order_id = record.get("order_id")
        if order_id:
            lines.append(f"   🧾 order_id: {_esc(order_id)}")
    for item in active_stops[:4]:
        account_id = item.get("account_id") or "n/a"
        symbol = item.get("symbol") or "n/a"
        quantity = item.get("quantity") or item.get("executed_quantity") or "n/a"
        lines.append(
            "🛡️ Активный soft-stop: "
            f"{_esc(account_id)} {_esc(symbol)}, {_esc(_side_label(item.get('side')))} "
            f"{_esc(quantity)} @ {_esc(_price(item.get('stop_price')))}"
        )
        entry_order_id = item.get("entry_order_id")
        if entry_order_id:
            lines.append(f"   ↳ entry_order_id: {_esc(entry_order_id)}")
    if len(active_stops) > 4:
        lines.append(f"… ещё {len(active_stops) - 4} активных soft-stop")
    if error:
        lines.append("🧭 Следующий шаг: повторить arena-check-stops после восстановления Finam session.")
    else:
        lines.append("🧭 Следующий шаг: разобрать stop-check payload; safety руками не чистить.")
    return lines


def _stop_check_payload(run: dict[str, Any], run_all: dict[str, Any]) -> dict[str, Any]:
    stop_check = run.get("stop_check")
    if isinstance(stop_check, dict):
        return stop_check
    if str(run.get("command") or "") == "arena-check-stops":
        return run
    for item in run_all.get("runs") or []:
        if not isinstance(item, dict):
            continue
        nested = item.get("stop_check")
        if isinstance(nested, dict):
            return nested
        if str(item.get("command") or "") == "arena-check-stops":
            return item
    return {}


def _active_soft_stop_checks(stop_check: dict[str, Any]) -> list[dict[str, Any]]:
    checks = stop_check.get("checks") if isinstance(stop_check.get("checks"), list) else []
    return [
        item
        for item in checks
        if isinstance(item, dict)
        and str(item.get("mode") or "") == "arena_soft_stop"
        and str(item.get("status") or "") == "ACTIVE"
    ]


def _latest_buy_records_for_stops(run_all: dict[str, Any], stops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stop_keys = {
        (str(item.get("account_id") or ""), str(item.get("symbol") or ""))
        for item in stops
        if item.get("account_id") and item.get("symbol")
    }
    records = run_all.get("_execution_ledger_tail")
    if not isinstance(records, list):
        records = run_all.get("execution_ledger")
    if not isinstance(records, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in reversed(records):
        if not isinstance(item, dict) or str(item.get("side") or "").upper() != "BUY":
            continue
        key = (str(item.get("account_id") or ""), str(item.get("symbol") or ""))
        if key not in stop_keys or key in seen:
            continue
        result.append(item)
        seen.add(key)
    return result


def _account_summary_lines(run_all: dict[str, Any]) -> list[str]:
    accounts: dict[str, dict[str, Any]] = {}
    run_status: dict[str, str] = {}
    for run in run_all.get("runs") or []:
        if not isinstance(run, dict):
            continue
        raw_proposal_output = run.get("proposal")
        proposal_output = raw_proposal_output if isinstance(raw_proposal_output, dict) else {}
        raw_account = proposal_output.get("account")
        account = raw_account if isinstance(raw_account, dict) else {}
        account_id = str(account.get("account_id") or run.get("account_id") or "").strip()
        if not account_id:
            continue
        if account:
            accounts[account_id] = account
        elif account_id not in accounts:
            accounts[account_id] = {"account_id": account_id}
        run_status[account_id] = str(run.get("status") or "")
    lines: list[str] = []
    for account_id in sorted(accounts):
        account = accounts[account_id]
        label = account.get("label") or ""
        equity = _money(account.get("equity"))
        pnl = _pct(account.get("pnl_pct"))
        positions = account.get("positions_count")
        risk = _pct(account.get("open_risk_pct"))
        status = _status_label(run_status.get(account_id))
        line = f"• <b>{_esc(account_id)}</b> {_esc(label)}"
        if equity != "n/a":
            line += f" {_esc(equity)} RUB"
        if pnl != "n/a":
            line += f" ({_esc(pnl)})"
        line += f" 📦 {_esc(positions)}"
        if risk != "n/a":
            line += f" ⚠️ риск {_esc(risk)}"
        line += f" — {_esc(status)}"
        lines.append(line)
        top_signal = account.get("top_signal")
        lines.append(f"   🎯 {_esc(_top_signal_label(top_signal) if top_signal else 'нет активного сигнала')}")
    return lines


def _money(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):,.2f}".replace(",", " ")
    except Exception:
        return "n/a"


def _price(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):.2f}"
    except Exception:
        return "n/a"


def _pct(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):.2f}%"
    except Exception:
        return "n/a"



def _approval_explanation_lines(
    run: dict[str, Any],
    *,
    green_gate_line: str = "✅ Риск-гейты зелёные; заявка брокеру только после подтверждения",
) -> list[str]:
    if str(run.get("command") or "") == "arena-portfolio-run" and str(run.get("action") or ""):
        action = str(run.get("action") or "")
        replacement = run.get("replacement") if isinstance(run.get("replacement"), dict) else {}
        if action == "REPLACE":
            return [
                f"🔁 Ротация: продать {_esc(replacement.get('sell_symbol') or 'n/a')} → купить {_esc(replacement.get('buy_symbol') or 'n/a')}",
                f"🧭 Причина: {_esc(_reason_label(run.get('reason')))}",
            ]
        return [
            f"📤 Действие по портфелю: {_esc(_action_label(action))}; {_esc(run.get('symbol') or 'n/a')}; объём {_esc(run.get('quantity') or 'n/a')}",
            f"🧭 Причина: {_esc(_reason_label(run.get('reason')))}",
        ]
    proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    proposal = proposal_output.get("proposal") if isinstance(proposal_output.get("proposal"), dict) else {}
    candidate = proposal_output.get("candidate") if isinstance(proposal_output.get("candidate"), dict) else {}
    risk = proposal.get("risk") if isinstance(proposal.get("risk"), dict) else {}
    entry = proposal.get("entry") if isinstance(proposal.get("entry"), dict) else {}
    stop = proposal.get("protective_stop") if isinstance(proposal.get("protective_stop"), dict) else {}
    take_profit = proposal.get("take_profit") if isinstance(proposal.get("take_profit"), dict) else {}
    gates = proposal.get("gates") if isinstance(proposal.get("gates"), dict) else {}
    signal = candidate.get("signal") if isinstance(candidate.get("signal"), dict) else {}
    costs = candidate.get("costs") if isinstance(candidate.get("costs"), dict) else {}

    quantity = proposal.get("quantity") or candidate.get("quantity")
    entry_price = entry.get("limit_price") or candidate.get("entry_price")
    notional = risk.get("notional") or candidate.get("notional")
    risk_rub = risk.get("risk_rub") or candidate.get("risk_rub")
    risk_per_share = risk.get("risk_per_share") or candidate.get("risk_per_share")
    tp_price = take_profit.get("price") or candidate.get("take_profit_price")
    stop_price = stop.get("stop_price") or candidate.get("stop_price")
    rr = _rr(entry_price, stop_price, tp_price, side=str(proposal.get("side") or candidate.get("side") or "BUY"))

    lines = [
        f"💰 Цена/объём: {_esc(_price(entry_price))} × {_esc(quantity)}; сумма {_esc(_money(notional))}",
        f"🛡️ Stop: {_esc(_price(stop_price))}; риск {_esc(_money(risk_rub))} ({_esc(_price(risk_per_share))} на акцию)",
        f"🎯 TP: {_esc(_price(tp_price))}; R/R {_esc(rr)}",
    ]
    if signal:
        lines.append(
            "📈 Сигнал: "
            f"H4 {_esc(_direction_label(signal.get('h4')))}, "
            f"H1 {_esc(_direction_label(signal.get('h1')))}, "
            f"M30 {_esc(_direction_label(signal.get('m30')))}"
        )
    if costs:
        lines.append(
            f"💸 Комиссия+проскальзывание: вход {_esc(costs.get('entry_cost_rub'))}; "
            f"круг {_esc(costs.get('round_trip_cost_rub'))}; безубыток {_esc(costs.get('break_even_move_pct'))}%"
        )
    gate_reasons = gates.get("gate_reasons") if isinstance(gates.get("gate_reasons"), list) else []
    if gate_reasons:
        lines.append(f"⚠️ Блокирующие условия: {_esc(_reason_list(gate_reasons))}")
    else:
        lines.append(green_gate_line)
    return lines


def _research_source_line(candidate: dict[str, Any]) -> str:
    pretrade = candidate.get("pretrade_check") if isinstance(candidate.get("pretrade_check"), dict) else {}
    if pretrade:
        status = str(pretrade.get("status") or "unknown")
        verdict = str(pretrade.get("verdict") or "UNAVAILABLE")
        provider_call = bool(pretrade.get("provider_call"))
        reason = str(pretrade.get("reason") or "")
        budget = pretrade.get("budget") if isinstance(pretrade.get("budget"), dict) else {}
        used = budget.get("used")
        limit = budget.get("limit")
        budget_text = f"; бюджет {used}/{limit}" if used is not None or limit is not None else ""
        provider = "внешний provider вызван" if provider_call else "внешний provider не вызывался"
        if reason:
            return f"🧠 Pretrade: {_esc(verdict)}; {_esc(provider)}; статус {_esc(status)}; причина {_esc(reason)}{budget_text}"
        return f"🧠 Pretrade: {_esc(verdict)}; {_esc(provider)}; статус {_esc(status)}{budget_text}"
    score = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
    verdict = score.get("research_verdict") or candidate.get("research_verdict")
    if verdict:
        return f"🧠 Research: {_esc(verdict)}; отдельного pretrade snapshot нет"
    return "🧠 Research: нет сохранённого verdict/pretrade snapshot"


def _planned_action_lines(run: dict[str, Any]) -> list[str]:
    actions = run.get("planned_actions") if isinstance(run.get("planned_actions"), list) else []
    if not actions:
        return ["⏳ Плановых действий нет"]
    lines = ["📋 Плановые действия:"]
    for action in actions[:6]:
        if not isinstance(action, dict):
            continue
        account_id = _esc(action.get("account_id"))
        action_name = _esc(_action_label(action.get("action")))
        symbol = _esc(action.get("symbol"))
        quantity = _esc(action.get("quantity"))
        reason = _esc(_reason_label(action.get("reason")))
        lines.append(f"• {account_id}: {symbol} — {action_name}; объём {quantity}; причина: {reason}")
    if len(actions) > 6:
        lines.append(f"… ещё {len(actions) - 6} действий")
    return lines


def _blocked_explanation_lines(run: dict[str, Any], *, run_all: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    gate_reasons = run.get("gate_reasons") if isinstance(run.get("gate_reasons"), list) else []
    if gate_reasons:
        lines.append(f"🚧 Блокирующие условия: {_esc(_reason_list(gate_reasons))}")
    proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    account = proposal_output.get("account") if isinstance(proposal_output.get("account"), dict) else {}
    candidate = proposal_output.get("candidate") if isinstance(proposal_output.get("candidate"), dict) else {}
    top_signal = account.get("top_signal")
    if top_signal:
        lines.append(f"🎯 {_esc(_top_signal_label(top_signal))}")
    if candidate:
        score = candidate.get("arena_growth_score") if isinstance(candidate.get("arena_growth_score"), dict) else {}
        if score:
            lines.append(f"📈 Оценка идеи: {_esc(score.get('score'))} / {_esc(_score_label(score.get('label')))}")
        signal = candidate.get("signal") if isinstance(candidate.get("signal"), dict) else {}
        if signal:
            lines.append(
                "📊 Сигнал: "
                f"H4 {_esc(_direction_label(signal.get('h4')))}, "
                f"H1 {_esc(_direction_label(signal.get('h1')))}, "
                f"M30 {_esc(_direction_label(signal.get('m30')))}"
            )
        budget_line = _pretrade_budget_line(candidate)
        if budget_line:
            lines.append(budget_line)
        if "single_symbol_anchor_rotation_candidate" in {str(item) for item in gate_reasons}:
            lines.extend(_anchor_rotation_lines(run, candidate=candidate, run_all=run_all))
    lines.extend(_blocked_operator_path_lines(gate_reasons))
    return lines


def _pretrade_budget_line(candidate: dict[str, Any]) -> str:
    pretrade = candidate.get("pretrade_check") if isinstance(candidate.get("pretrade_check"), dict) else {}
    budget = pretrade.get("budget") if isinstance(pretrade.get("budget"), dict) else {}
    limit = budget.get("limit")
    used = budget.get("used")
    remaining = budget.get("remaining")
    if limit is None and used is None and remaining is None:
        return ""
    spent = budget.get("spent_symbols") if isinstance(budget.get("spent_symbols"), list) else []
    spent_text = ", ".join(str(item) for item in spent if str(item)) or "n/a"
    return f"🧮 Pretrade budget: {_esc(used)}/{_esc(limit)}, осталось {_esc(remaining)}; уже потрачено: {_esc(spent_text)}"


def _anchor_rotation_lines(run: dict[str, Any], *, candidate: dict[str, Any], run_all: dict[str, Any]) -> list[str]:
    rotation = candidate.get("anchor_rotation") if isinstance(candidate.get("anchor_rotation"), dict) else {}
    source_symbol = str(rotation.get("source_symbol") or "").upper()
    target_symbol = str(candidate.get("symbol") or run.get("symbol") or "").upper()
    account_id = str(run.get("account_id") or candidate.get("account_id") or "")
    if not source_symbol or not target_symbol:
        return []
    candidate_score = rotation.get("candidate_score")
    holding_score = rotation.get("source_holding_score")
    score_delta = _score_delta(candidate_score, holding_score)
    lines = [
        f"🔁 Кандидат на ротацию: {_esc(source_symbol)} -> {_esc(target_symbol)}; требуется отдельное решение",
        (
            f"   текущая доля {_esc(_pct(rotation.get('source_notional_pct')))}; "
            f"прогресс {_esc(_r_value(rotation.get('source_progress_r')))}; "
            f"score удержания {_esc(holding_score)}; score идеи {_esc(candidate_score)}; Δ {_esc(score_delta)}"
        ),
    ]
    partial_sell_command = _anchor_rotation_partial_sell_command(rotation, account_id=account_id, source_symbol=source_symbol)
    if partial_sell_command:
        lines.append(f"   partial sell route: {_esc(partial_sell_command)}")
    replacement = _matching_replacement(run_all, account_id=account_id, sell_symbol=source_symbol, buy_symbol=target_symbol)
    if replacement:
        lines.append(
            f"   replacement: cost {_esc(_money(replacement.get('estimated_cost_rub')))} RUB; "
            f"лимит {_esc(replacement.get('replacement_entries_used'))}/{_esc(replacement.get('replacement_entries_limit'))}"
        )
        confirmation = str(replacement.get("confirmation_phrase") or "").strip()
        if confirmation:
            lines.append(f"   путь: создать snapshot ротации через arena-portfolio-run; фраза: {_esc(confirmation)}")
        lines.append("   pending confirmation не создан: arena-confirm применять нельзя до создания snapshot")
        lines.append("   варианты: оставить якорную позицию; создать pending REPLACE snapshot через arena-portfolio-run; заблокировать ротацию на сегодня")
    else:
        lines.append(
            f"   replacement snapshot не создан: {_esc(target_symbol)} не лучше {_esc(source_symbol)} по текущей оценке; нужен ручной разбор"
            if score_delta == "+0"
            else "   replacement snapshot не создан: нет actionable replacement proposal; нужен ручной разбор"
        )
        lines.append("   варианты: оставить якорную позицию; ручной разбор ротации; заблокировать ротацию на сегодня")
    return lines


def _anchor_rotation_partial_sell_command(rotation: dict[str, Any], *, account_id: str, source_symbol: str) -> str:
    quantity = str(
        rotation.get("suggested_partial_sell_quantity")
        or rotation.get("manual_partial_sell_quantity")
        or rotation.get("source_trim_quantity")
        or ""
    ).strip()
    if not quantity or quantity in {"0", "0.0"}:
        return ""
    confirmation = f"CONFIRM_ARENA_SELL_PARTIAL {source_symbol} {quantity} {account_id}"
    return (
        "python scripts/hermes_operator.py arena-position-sell "
        f"--account {account_id} --symbol {source_symbol} --quantity {quantity} "
        f'--live --confirmation "{confirmation}"'
    )


def _matching_replacement(run_all: dict[str, Any], *, account_id: str, sell_symbol: str, buy_symbol: str) -> dict[str, Any]:
    for run in run_all.get("runs") or []:
        if not isinstance(run, dict):
            continue
        review = run.get("review") if isinstance(run.get("review"), dict) else {}
        for item in review.get("replacement_proposals") or []:
            if not isinstance(item, dict):
                continue
            if (
                str(item.get("account_id") or "") == account_id
                and str(item.get("sell_symbol") or "").upper() == sell_symbol
                and str(item.get("buy_symbol") or "").upper() == buy_symbol
            ):
                return item
    return {}


def _blocked_operator_path_lines(gate_reasons: list[Any]) -> list[str]:
    paths = {
        "pretrade_budget_exhausted_blocks_autonomy": "🧭 Путь: ждать нового бюджета предторговой проверки; ручной override для этого блока не предусмотрен.",
        "symbol_exposure_limit_exceeded": "🧭 Путь: уменьшить текущую позицию по бумаге или выбрать другой символ; лимит доли не обходится.",
        "cross_market_role_misx_concentration": "🧭 Путь: освободить слот РФ-позиции на этом счёте или не покупать новую MISX-бумагу.",
    }
    result: list[str] = []
    for reason in gate_reasons:
        line = paths.get(str(reason))
        if line and line not in result:
            result.append(line)
    return result


def _score_delta(candidate_score: Any, holding_score: Any) -> str:
    try:
        return f"{int(candidate_score) - int(holding_score):+d}"
    except Exception:
        return "n/a"


def _r_value(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):.2f}R"
    except Exception:
        return "n/a"


def _status_label(value: Any) -> str:
    return {
        "LIVE_GATE_REQUIRED": "ожидает включения автоторгов",
        "BLOCKED": "заблокировано правилами",
        "CONFIRMATION_REQUIRED": "нужно подтверждение",
        "DRY_RUN": "проверочный прогон",
        "NO_ORDER": "нет заявки",
        "NO_PROPOSAL": "нет торговой идеи",
        "HALT": "остановлено",
        "BROKER_ERROR": "ошибка брокера",
        "STOP_NOT_VERIFIED": "стоп не подтверждён",
        "ENTRY_PENDING_NO_STOP": "вход без подтверждённой защиты",
        "REPLACE_SELL_DONE_BUY_BLOCKED": "ротация частично выполнена, покупка заблокирована",
        "EXECUTED_ARENA": "исполнено",
        "EXECUTED_ARENA_PARTIAL": "частично исполнено",
        "EXECUTED_ARENA_SOFT_STOP_ACTIVE": "исполнено, мягкий стоп активен",
        "EXECUTED_ARENA_PARTIAL_SOFT_STOP_ACTIVE": "частично исполнено, мягкий стоп активен",
        "EXECUTED_ARENA_PORTFOLIO": "портфельное действие исполнено",
        "EXECUTED_ARENA_REPLACEMENT": "замена позиции исполнена",
        "EXECUTED_ARENA_EXIT_CASH": "выход в кэш исполнен",
        "ARENA_SOFT_STOP_TRIGGERED": "мягкий стоп сработал",
    }.get(str(value or ""), str(value or "n/a"))


def _action_label(value: Any) -> str:
    return {
        "TAKE_PARTIAL_PROFIT": "частично зафиксировать прибыль",
        "TRAIL_STOP": "подтянуть стоп",
        "BREAKEVEN_STOP": "перенести стоп в безубыток",
        "EXIT_WEAK": "выйти из слабой позиции",
        "TRIM_OVEREXPOSURE": "снизить перегруз позиции",
        "HOLD": "держать",
        "BUY": "купить",
        "SELL": "продать",
    }.get(str(value or ""), str(value or "n/a"))


def _reason_label(value: Any) -> str:
    return {
        "FINAM_ARENA_AUTO_TRADE_ENABLED_not_true": "рубильник автоторгов сейчас выключен",
        "take_partial_profit_threshold_reached": "достигнут порог частичной фиксации прибыли",
        "trail_stop_threshold_reached": "достигнут порог для подтягивания стопа",
        "breakeven_stop_threshold_reached": "можно перенести стоп в безубыток",
        "arena_execution_gate_blocked": "исполнение заблокировано правилами риска",
        "arena_replace_requires_supervised_confirmation": "ротация позиции требует отдельного подтверждения",
        "arena_exit_requires_supervised_confirmation": "действие по позиции требует отдельного подтверждения",
        "account_gross_exposure_limit_reached": "счёт уже набрал лимит общей загрузки",
        "symbol_exposure_limit_exceeded": "лимит доли этой бумаги в счёте будет превышен",
        "same_symbol_position_open": "по этой бумаге уже открыта позиция",
        "same_symbol_trade_today": "по этой бумаге сегодня уже была сделка",
        "daily_new_notional_limit_exceeded": "дневной лимит объёма новых покупок будет превышен",
        "arena_shortability_api_unavailable": "нельзя подтвердить доступность шорта через API",
        "arena_margin_trading_not_supported": "шорт недоступен: в Arena нет торговли под обеспечение",
        "candidate_score_below_min": "оценка идеи ниже минимального порога",
        "research_unavailable_below_exceptional_score": "нет свежего Codex-review, а идея не exceptional",
        "entry_strength_confirmation_missing": "нет силы к рынку и нет M30-подтверждения",
        "cross_market_role_misx_concentration": "счёт уже перегружен российскими позициями по своей роли",
        "single_symbol_anchor_rotation_candidate": "кандидат на ротацию якорной позиции; требуется отдельное решение",
        "research_risk_requires_manual_review": "исследовательский фильтр видит повышенный риск, нужен ручной разбор",
        "research_provider_failure": "поставщик research-проверки недоступен",
        "research_avoid": "research-фильтр рекомендует избегать сделки",
        "learning_attribution_uncertain_requires_manual_review": "обучение не уверено в причине результата, нужен ручной разбор",
        "learning_deprioritized_requires_manual_review": "обучение понизило приоритет идеи, нужен ручной разбор",
        "repeat_pattern_loss_cooldown": "похожий паттерн недавно давал убыток, действует пауза",
        "repeat_pattern_loss_score_below_min": "похожий убыточный паттерн снизил оценку ниже порога",
        "pretrade_budget_exhausted_blocks_autonomy": "исчерпан бюджет предторговой проверки, автономный вход заблокирован",
        "pretrade_event_check_risk": "предторговая проверка событий показала повышенный риск",
        "pretrade_event_check_unavailable": "предторговая проверка событий недоступна",
        "primary_daily_limit_reached": "дневной лимит основных входов уже выбран",
        "market_data_degraded_429": "рыночные данные Finam временно ограничены",
        "short_availability_not_confirmed": "шорт не подтверждён",
        "no_active_arena_proposal": "нет активной торговой идеи",
        "no_arena_h4_regime_h1_m30_candidate": "нет подходящего H4/H1/M30 сигнала",
    }.get(str(value or ""), str(value or "n/a"))


def _reason_list(values: list[Any]) -> str:
    return ", ".join(_reason_label(item) for item in values)



def _top_signal_label(value: Any) -> str:
    text = str(value or "").strip()
    prefix = "нет исполнимого сигнала: "
    if text.startswith(prefix):
        return prefix + _reason_label(text[len(prefix):])
    return text

def _score_label(value: Any) -> str:
    return {"HIGH": "сильная", "MEDIUM": "средняя", "LOW": "слабая"}.get(str(value or ""), str(value or "n/a"))


def _direction_label(value: Any) -> str:
    return {"up": "вверх", "down": "вниз", "flat": "боковик", "unavailable": "нет данных"}.get(
        str(value or ""), str(value or "n/a")
    )


def _side_label(value: Any) -> str:
    return {"SELL": "продажа", "SIDE_SELL": "продажа", "BUY": "покупка", "SIDE_BUY": "покупка"}.get(
        str(value or ""), str(value or "n/a")
    )


def _rr(entry: Any, stop: Any, take_profit: Any, *, side: str) -> str:
    try:
        entry_d = Decimal(str(entry))
        stop_d = Decimal(str(stop))
        tp_d = Decimal(str(take_profit))
    except Exception:
        return "n/a"
    if side.upper() == "SELL":
        risk = stop_d - entry_d
        reward = entry_d - tp_d
    else:
        risk = entry_d - stop_d
        reward = tp_d - entry_d
    if risk <= 0:
        return "n/a"
    return format(reward / risk, ".2f")


def _alert_fingerprint(run_all: dict[str, Any], text: str) -> str:
    payload = {
        "status": run_all.get("status"),
        "runs": [
            {
                "account_id": run.get("account_id"),
                "status": run.get("status"),
                "reason": run.get("reason"),
                "symbol": _run_symbol(run),
                "required_confirmation": run.get("required_confirmation"),
                "proposal_hash": _run_proposal_hash(run),
            }
            for run in _iter_alert_runs(run_all)
            if _alert_kind(run)
        ],
        "text": text,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _approval_alert_has_active_pending(run_all: dict[str, Any], *, env: Any = os.environ) -> bool:
    confirmations = [
        str(run.get("required_confirmation") or "").strip()
        for run in _iter_alert_runs(run_all)
        if isinstance(run, dict) and _alert_kind(run) == "approval" and str(run.get("required_confirmation") or "").strip()
    ]
    if not confirmations:
        return False
    path = hermes_operator._arena_pending_approvals_path(env=env)
    current = datetime.now(timezone.utc)
    for confirmation in confirmations:
        approval, _missing = hermes_operator._find_arena_pending_approval(path, confirmation, now=current)
        if approval is None:
            return False
    return True


def _run_proposal_hash(run: dict[str, Any]) -> str | None:
    proposal_output = run.get("proposal") if isinstance(run.get("proposal"), dict) else {}
    if not proposal_output:
        return None
    return hermes_operator._arena_proposal_hash(proposal_output)


def _blocked_alert_suppression(path: Path, run_all: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    signature = _stable_blocked_signature(run_all)
    if not signature:
        return {"suppressed": False, "reason": "not_stable_blocked"}
    state = _read_alert_state(path)
    blocked_raw = state.get("blocked_alerts")
    blocked: dict[str, Any] = blocked_raw if isinstance(blocked_raw, dict) else {}
    date_msk = current.astimezone(ARENA_ALERT_TIMEZONE).date().isoformat()
    if blocked.get("date_msk") == date_msk:
        if blocked.get("signature") == signature:
            return {
                "suppressed": True,
                "signature": signature,
                "date_msk": date_msk,
                "last_sent_at": blocked.get("last_sent_at"),
                "status": blocked.get("status") or "sent",
                "reason": "same_blocked_signature",
            }
        last_sent = _parse_datetime(blocked.get("last_sent_at"))
        if last_sent is not None and (current - last_sent).total_seconds() < BLOCKED_ALERT_COOLDOWN_SECONDS:
            return {
                "suppressed": True,
                "signature": signature,
                "date_msk": date_msk,
                "last_sent_at": blocked.get("last_sent_at"),
                "status": blocked.get("status") or "sent",
                "reason": "blocked_cooldown",
                "cooldown_seconds": BLOCKED_ALERT_COOLDOWN_SECONDS,
            }
    return {"suppressed": False, "signature": signature, "date_msk": date_msk, "reason": "first_or_changed_blocked"}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stable_blocked_signature(run_all: dict[str, Any]) -> str | None:
    alert_runs = [run for run in _iter_alert_runs(run_all) if _alert_kind(run)]
    if not alert_runs or any(_alert_kind(run) != "blocked" for run in alert_runs):
        return None
    parts: list[dict[str, Any]] = []
    for run in alert_runs:
        gate_reasons = sorted({str(item) for item in run.get("gate_reasons") or [] if str(item)})
        if not gate_reasons or any(reason not in SUPPRESSIBLE_BLOCKED_GATES for reason in gate_reasons):
            return None
        parts.append(
            {
                "account_id": str(run.get("account_id") or ""),
                "gate_reasons": gate_reasons,
            }
        )
    encoded = json.dumps(sorted(parts, key=lambda item: item["account_id"]), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_alert_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_duplicate_alert(path: Path, fingerprint: str) -> bool:
    return _read_alert_state(path).get("last_fingerprint") == fingerprint


def _write_alert_state(path: Path, fingerprint: str, *, blocked_signature: Any | None = None) -> None:
    state = _read_alert_state(path)
    state["last_fingerprint"] = fingerprint
    if blocked_signature:
        current = datetime.now(timezone.utc)
        state["blocked_alerts"] = {
            "date_msk": current.astimezone(ARENA_ALERT_TIMEZONE).date().isoformat(),
            "last_sent_at": current.isoformat(timespec="seconds"),
            "signature": str(blocked_signature),
            "status": "sent",
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _yes_no(value: Any) -> str:
    return "да" if value is True else "нет"


def _esc(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    return html.escape(str(value), quote=False)


if __name__ == "__main__":
    raise SystemExit(main())
