#!/usr/bin/env python3
"""Run the H4 monitor and deliver its report to Telegram.

The scheduler owns timing; this wrapper owns delivery. Keeping delivery outside
the monitor makes the host timer self-contained and observable by systemd.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import h4_monitor  # noqa: E402


TELEGRAM_MAX_MESSAGE = 4096
TELEGRAM_SAFE_MESSAGE = 3900
DEFAULT_OUTBOX_ROOT = ROOT / "data" / "runtime" / "report_outbox"
OUTBOX_RESUME_WINDOW_SECONDS = 3 * 60 * 60
ALLOWED_TELEGRAM_TAG_RE = re.compile(r"</?b>")
HTML_ENTITY_RE = re.compile(r"&(amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only", action="store_true", help="Required. Never mutates broker state.")
    parser.add_argument("--once", action="store_true", help="Run one monitoring pass.")
    parser.add_argument("--dry-run", action="store_true", help="Print the Telegram report without sending it.")
    parser.add_argument("--skip-research", action="store_true", help="Skip Codex-review facts collection.")
    parser.add_argument("--build-outbox", action="store_true", help="Build and persist a report payload without sending.")
    parser.add_argument("--send-outbox", help="Send an already persisted outbox run id without rebuilding the report.")
    parser.add_argument("--outbox-dir", default=str(DEFAULT_OUTBOX_ROOT), help="Report outbox directory.")
    parser.add_argument("--no-resume", action="store_true", help="Do not resume recent pending/failed outbox delivery.")
    args = parser.parse_args()

    if not args.read_only:
        print(json.dumps({"status": "NO_TRADE", "error": "--read-only is required"}, ensure_ascii=False, indent=2))
        return 2

    outbox_root = Path(args.outbox_dir)

    if args.send_outbox:
        return _send_outbox_cli(args.send_outbox, outbox_root=outbox_root, dry_run=args.dry_run)

    if not args.no_resume and not args.dry_run and not args.build_outbox:
        pending = latest_sendable_outbox_run(outbox_root)
        if pending:
            print(json.dumps({"outbox_resume": pending}, ensure_ascii=False))
            return _send_outbox_cli(pending, outbox_root=outbox_root, dry_run=False)

    item = build_outbox_report(outbox_root=outbox_root, include_research=not args.skip_research)
    report = item["report"]
    message = item["message"]

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n--- telegram_report ---")
    print(message)
    print(json.dumps({"outbox_run_id": item["run_id"], "outbox_path": str(item["path"])}, ensure_ascii=False))

    if args.dry_run:
        _atomic_write_json(
            Path(item["path"]) / "delivery.json",
            _delivery_payload(item["run_id"], status="dry_run", message=message),
        )
        print(json.dumps({"telegram_delivery": "dry_run"}, ensure_ascii=False))
        return 0

    if args.build_outbox:
        print(json.dumps({"telegram_delivery": "dry_run"}, ensure_ascii=False))
        return 0

    return send_outbox_report(item["run_id"], outbox_root=outbox_root)


def _send_outbox_cli(run_id: str, *, outbox_root: Path, dry_run: bool) -> int:
    item = read_outbox_report(run_id, outbox_root=outbox_root)
    print(item["message"])
    if dry_run:
        print(json.dumps({"telegram_delivery": "dry_run", "outbox_run_id": run_id}, ensure_ascii=False))
        return 0
    return send_outbox_report(run_id, outbox_root=outbox_root)


def build_outbox_report(*, outbox_root: Path = DEFAULT_OUTBOX_ROOT, include_research: bool = True) -> dict[str, Any]:
    report = h4_monitor.build_report(include_research=include_research)
    message = format_telegram_report(report)
    validate_telegram_html(message)
    run_id = _run_id(report)
    run_path = outbox_root / run_id
    run_path.mkdir(parents=True, exist_ok=False)
    _atomic_write_json(run_path / "report.json", report)
    _atomic_write_text(run_path / "telegram.html", message)
    _atomic_write_json(
        run_path / "delivery.json",
        {
            "status": "preview",
            "sendable": False,
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "report_status": report.get("status"),
            "chunks": len(split_telegram_messages(message)),
        },
    )
    return {"run_id": run_id, "path": run_path, "report": report, "message": message}


def read_outbox_report(run_id: str, *, outbox_root: Path = DEFAULT_OUTBOX_ROOT) -> dict[str, Any]:
    if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
        raise RuntimeError("invalid outbox run id")
    run_path = outbox_root / run_id
    report = json.loads((run_path / "report.json").read_text(encoding="utf-8"))
    message = (run_path / "telegram.html").read_text(encoding="utf-8")
    validate_telegram_html(message)
    return {"run_id": run_id, "path": run_path, "report": report, "message": message}


def send_outbox_report(run_id: str, *, outbox_root: Path = DEFAULT_OUTBOX_ROOT) -> int:
    item = read_outbox_report(run_id, outbox_root=outbox_root)
    message = item["message"]
    run_path = item["path"]
    delivery_path = run_path / "delivery.json"
    try:
        send_telegram_message(message)
    except urllib.error.HTTPError as exc:
        error = str(exc)
        body = exc.read().decode("utf-8", errors="replace")
        if body:
            error = f"{error}: {body}"
        _atomic_write_json(
            delivery_path,
            _delivery_payload(run_id, status="failed", error=error, message=message),
        )
        print(json.dumps({"telegram_delivery": "failed", "outbox_run_id": run_id, "error": error}, ensure_ascii=False), file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 - systemd should see delivery failure.
        _atomic_write_json(
            delivery_path,
            _delivery_payload(run_id, status="failed", error=str(exc), message=message),
        )
        print(json.dumps({"telegram_delivery": "failed", "outbox_run_id": run_id, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 4

    messages = split_telegram_messages(message)
    _atomic_write_json(delivery_path, _delivery_payload(run_id, status="ok", message=message))
    print(json.dumps({"telegram_delivery": "ok", "outbox_run_id": run_id, "messages_sent": len(messages)}, ensure_ascii=False))
    return 0


def latest_sendable_outbox_run(outbox_root: Path = DEFAULT_OUTBOX_ROOT, *, now: datetime | None = None) -> str | None:
    if not outbox_root.exists():
        return None
    now_utc = now or datetime.now(timezone.utc)
    candidates: list[tuple[datetime, str]] = []
    for run_path in outbox_root.iterdir():
        if not run_path.is_dir():
            continue
        delivery_path = run_path / "delivery.json"
        try:
            delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if delivery.get("status") not in {"pending", "failed"} or delivery.get("sendable") is not True:
            continue
        created = _parse_time(delivery.get("created_at") or delivery.get("updated_at"))
        if created is None or (now_utc - created).total_seconds() > OUTBOX_RESUME_WINDOW_SECONDS:
            continue
        candidates.append((created, run_path.name))
    if not candidates:
        return None
    return sorted(candidates)[-1][1]


def validate_telegram_html(text: str) -> None:
    for chunk in split_telegram_messages(text):
        stripped = ALLOWED_TELEGRAM_TAG_RE.sub("", chunk)
        stripped = HTML_ENTITY_RE.sub("", stripped)
        if "<" in stripped:
            raise RuntimeError("Telegram HTML contains raw '<' outside allowed tags/entities")


def _delivery_payload(run_id: str, *, status: str, message: str, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "run_id": run_id,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chunks": len(split_telegram_messages(message)),
    }
    if error:
        payload["error"] = error
    return payload


def _run_id(report: dict[str, Any]) -> str:
    timestamp = _parse_time(report.get("time_utc")) or datetime.now(timezone.utc)
    return f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def format_telegram_report(report: dict[str, Any]) -> str:
    account: dict[str, Any] = _dict_value(report.get("account"))
    policy: dict[str, Any] = _dict_value(report.get("policy"))
    risk: dict[str, Any] = _dict_value(policy.get("risk"))
    stop_atr_label = _stop_atr_label(risk.get("stop_atr_multiplier"))
    orders: dict[str, Any] = _dict_value(report.get("orders"))
    errors: list[Any] = _list_value(report.get("errors"))
    warnings: list[Any] = _list_value(report.get("warnings"))
    display_warnings = _telegram_display_warnings(warnings)
    positions: list[Any] = _list_value(report.get("positions"))
    zero_positions: list[Any] = _list_value(report.get("zero_positions"))
    candidates: list[Any] = _list_value(report.get("candidates"))
    dynamic_watchlist: list[Any] = _list_value(report.get("dynamic_watchlist"))
    short_candidates: list[Any] = _list_value(report.get("short_candidates"))
    research: dict[str, Any] = _dict_value(report.get("research"))
    growth: dict[str, Any] = _dict_value(report.get("growth"))
    stop_details = orders.get("watching_sell_sltp_details_by_symbol")
    if not isinstance(stop_details, dict):
        stop_details = {}
    active_orders_count = orders.get("orders_count")
    if active_orders_count is None:
        active_orders_count = sum(len(items) for items in stop_details.values() if isinstance(items, list))
    total_unrealized = account.get("unrealized_pnl")
    if total_unrealized is None:
        total_unrealized = _sum_decimal(position.get("unrealized_pnl") for position in positions if isinstance(position, dict))

    lines = [
        _bold(f"Finam H4 демо-монитор — {_value(report.get('time_msk'))} МСК"),
        "",
        _bold("Сводка счёта"),
        f"💼 Счёт: {_value(account.get('account_id'))}",
        f"💰 Стоимость портфеля: {_money_rub(account.get('equity'))}",
        f"💵 Свободные деньги: {_money_rub(account.get('cash'))}",
        f"📈/📉 Нереализованный результат: {_money_rub(total_unrealized)}",
        f"📌 Активных заявок: {active_orders_count}",
        f"📦 Лонг-позиций: {len(positions)}",
    ]

    for position in zero_positions:
        if not isinstance(position, dict):
            continue
        note = position.get("closed_note") or "позиция закрыта"
        lines.append(f"🛑 {_bold(position.get('symbol', 'UNKNOWN'))}: позиция 0 шт.; {_value(note)}")

    if growth:
        lines.extend(_growth_lines(growth))
        lines.extend(_growth_diagnostics_lines(account, positions, candidates, dynamic_watchlist, policy))

    lines.extend(
        [
            "",
            _bold("🛡️ Защитные стопы"),
        ]
    )

    if positions:
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = str(position.get("symbol", "UNKNOWN"))
            stops = stop_details.get(symbol) if isinstance(stop_details.get(symbol), list) else []
            stop = stops[0] if stops and isinstance(stops[0], dict) else {}
            if position.get("has_watching_sell_sltp"):
                status = f"🛡️ Защита: {_status_badge('OK')} — защитный стоп уже был, новый не дублировал"
            else:
                status = f"🛡️ Защита: {_status_badge('RISK')} — активный защитный стоп на продажу не найден"
            lines.extend(
                [
                    "",
                    _bold(symbol),
                    status,
                    f"📦 Кол-во: {_qty(position.get('quantity'))} шт.",
                    f"🛑 Направление: {_side(stop.get('side'))}",
                    f"🛡️ Стоп: {_price_plain(stop.get('stop'))}",
                    f"🧾 Заявка: {_value(stop.get('order_id'))}",
                ]
            )
    else:
        lines.extend(["", "⏳ Лонг-позиций нет"])

    lines.extend(["", _bold("Позиции")])

    if positions:
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = str(position.get("symbol", "UNKNOWN"))
            stops = stop_details.get(symbol) if isinstance(stop_details.get(symbol), list) else []
            stop = stops[0] if stops and isinstance(stops[0], dict) else {}
            current = position.get("current_price")
            entry = position.get("average_price")
            calculated_stop = position.get("calculated_stop")
            tp = position.get("tp_2r") or _target_2r(entry, calculated_stop)
            pnl = position.get("unrealized_pnl")
            status_text = _position_status(position.get("status"))
            lines.extend(
                [
                    "",
                    _bold(symbol),
                    f"📌 Статус: {status_text}",
                    f"📦 Кол-во: {_qty(position.get('quantity'))} шт.",
                    f"🛒 Вход: {_price_plain(entry)}",
                    f"📍 Текущая цена: {_price_plain(current)}",
                    f"🛡️ Стоп {stop_atr_label}: {_price_plain(calculated_stop)}",
                    f"🎯 Цель 2R: {_price_plain(tp)}",
                    f"📈/📉 Результат: {_money_rub(pnl)}",
                    f"⚠️ До стопа: {_distance_to_price(current, calculated_stop)}",
                    f"🚀 До цели: {_distance_to_price(current, tp)}",
                    f"📏 ATR(14) H4: {_price_plain(position.get('atr14'))}; прогресс: {_progress_value(position.get('progress_r'), entry, current, calculated_stop)}",
                ]
            )
            advisory_lines = _advisory_action_lines(position)
            if advisory_lines:
                lines.extend(advisory_lines)
    else:
        lines.extend(["", "⏳ Лонг-позиций нет"])

    for position in zero_positions:
        if not isinstance(position, dict):
            continue
        lines.extend(
            [
                "",
                _bold(position.get("symbol", "UNKNOWN")),
                f"📌 Статус: {_position_status('SELL/STOP')}",
                "📦 Кол-во: 0 шт.",
                f"📍 Текущая цена: {_price_plain(position.get('current_price'))}",
                f"📈/📉 {_value(position.get('closed_note') or 'Позиция закрыта')}; новых покупок не делал",
            ]
        )

    if candidates:
        lines.extend(["", _bold("Кандидаты на покупку")])
        for candidate in _display_candidates(candidates):
            if not isinstance(candidate, dict):
                continue
            gate_reasons = candidate.get("gate_reasons") if isinstance(candidate.get("gate_reasons"), list) else []
            lines.extend(
                [
                    "",
                    _bold(candidate.get("symbol", "UNKNOWN")),
                    f"📌 Статус: {_candidate_status_line(candidate)}",
                    _growth_score_line(candidate),
                    f"📦 Кол-во: {_qty(candidate.get('quantity'))} шт.",
                    f"📍 Цена: {_price_plain(candidate.get('current_price'))}",
                    f"💰 Сумма: {_money_rub(candidate.get('notional'))}",
                    f"🛡️ Стоп {stop_atr_label}: {_price_plain(candidate.get('stop'))}",
                    f"🎯 Цель 2R: {_price_plain(candidate.get('tp_2r'))}",
                    f"🚀 Ближайшая цель: {_price_plain(candidate.get('nearest_target'))}; запас до цели: {_r_value(candidate.get('nearest_target_r'))}",
                    f"📉 Риск: {_money_rub(candidate.get('risk_rub'))}",
                ]
            )
            lines.extend(_rr_strategy_lines(candidate))
            if gate_reasons:
                lines.append("⚠️ Ограничения: " + _value("; ".join(_gate_reason_label(item) for item in gate_reasons)))
            if candidate.get("candidate_source"):
                source = _value(_candidate_source_label(candidate.get("candidate_source")))
                score = _score_plain(candidate.get("discovery_score"))
                lines.append(f"🧭 Источник: {source}; рейтинг поиска: {score}")
            lines.extend(_growth_score_breakdown_lines(candidate))
            lines.extend(_entry_blocker_lines(candidate))
        if research.get("status") in {"ok", "facts"}:
            lines.extend(
                [
                    "",
                    _bold("🧠 Codex review"),
                    f"🧠 Вердикт: {_research_verdict_badge(research.get('verdict'))}",
                ]
            )
            lines.extend(_research_detail_lines(research))
        elif research.get("status") not in {None, "skipped"}:
            lines.extend(
                [
                    "",
                    _bold("🧠 Codex review"),
                    f"🧠 Вердикт: {_research_verdict_badge(research.get('verdict'))}",
                    f"⚠️ Проверка не выполнена: {_value(research.get('reason') or research.get('error'))}",
                ]
            )

    if dynamic_watchlist:
        lines.extend(["", _bold("Динамический список наблюдения")])
        for item in _display_dynamic_watchlist(dynamic_watchlist):
            if not isinstance(item, dict):
                continue
            status = _status_badge(item.get("status"))
            lines.extend(
                [
                    "",
                    _bold(item.get("symbol", "UNKNOWN")),
                    f"📌 Статус: {status}",
                    f"📊 Рейтинг: {_score_plain(item.get('discovery_score'))}/100",
                    f"💰 Оборот: {_money_rub(item.get('turnover_rub'))}",
                    f"🔁 Сделок: {_integer_plain(item.get('trades_today'))}",
                    f"🚀 Ближайшая цель: {_price_plain(item.get('nearest_target'))}; запас до цели: {_r_value(item.get('nearest_target_r'))}",
                ]
            )
            lines.extend(_rr_strategy_lines(item))
            rejects = item.get("reject_reasons") if isinstance(item.get("reject_reasons"), list) else []
            if rejects:
                lines.append("⛔ Причина: " + _value("; ".join(_gate_reason_label(reason) for reason in rejects)))

    if short_candidates:
        lines.extend(["", _bold("📉 Анализ шорт-сценариев")])
        for item in short_candidates:
            if not isinstance(item, dict):
                continue
            raw_signal = item.get("short_signal")
            signal = raw_signal if isinstance(raw_signal, dict) else {}
            lines.extend(
                [
                    "",
                    _bold(item.get("symbol", "UNKNOWN")),
                    f"📌 Статус: {_status_badge(signal.get('status') or 'WATCH')}",
                    "📉 Кандидат в шорт: да",
                    "⚠️ Шорт-заявка: запрещена — доступность шорта не подтверждена",
                    f"Причина: {_value(_signal_reason_label(signal.get('reason')))}",
                ]
            )

    lines.extend(
        [
            "",
            _bold("Риски/пропуски данных"),
        ]
    )

    if errors or display_warnings:
        lines.extend(f"⚠️ {_value(item)}" for item in errors)
        lines.extend(f"⚠️ {_value(item)}" for item in display_warnings)
    else:
        lines.append("✅ Снимок Finam получен")
        lines.append("✅ H4 свежесть: ок для проверенных тикеров")
        lines.append("✅ ATR пересчитан для доступных LONG-позиций")
        lines.append("✅ Активные LONG-позиции проверены на наличие SELL SL")
        lines.append(f"⚠️ Расчётный стоп = вход − {stop_atr_label}; уже активные стопы не изменял и не дублировал")
        lines.append("⚠️ Новые докупки, продажи и шорты не выполнялись")

    lines.extend(
        [
            "",
            _bold("Действие сейчас"),
            _value(_action_line(positions, candidates, errors, warnings)),
            _value(_recommendation_line(positions, candidates)),
        ]
    )

    return "\n".join(lines)


def _growth_lines(growth: dict[str, Any]) -> list[str]:
    mode = "только отчёт, без автоторговли"
    lines = [
        "",
        _bold("🎯 Ориентир роста портфеля"),
        (
            "📈 50% годовых: "
            f"+{_value(growth.get('target_required_monthly_pct'))}%/мес, "
            f"+{_value(growth.get('target_required_weekly_pct'))}%/нед, "
            f"+{_value(growth.get('target_required_trading_day_pct'))}%/торг.день"
        ),
        (
            "🚀 100% годовых: "
            f"+{_value(growth.get('stretch_required_monthly_pct'))}%/мес, "
            f"+{_value(growth.get('stretch_required_weekly_pct'))}%/нед, "
            f"+{_value(growth.get('stretch_required_trading_day_pct'))}%/торг.день"
        ),
        f"💼 Стоимость портфеля для трекинга: {_money_rub(growth.get('current_equity'))}",
    ]
    lines.append(f"📊 Месяц/год: {_signed_pct(growth.get('mtd_return_pct'))} / {_signed_pct(growth.get('ytd_return_pct'))}")
    if growth.get("accumulated_r") not in {None, ""}:
        lines.append(f"📐 Накоплено R: {_signed_r(growth.get('accumulated_r'))}")
    elif growth.get("open_position_r") not in {None, ""}:
        lines.append(f"📐 Открытый R: {_signed_r(growth.get('open_position_r'))}")
    else:
        lines.append("📐 Накоплено R: н/д")
    lines.extend(
        [
            f"🧭 Статус к цели: {_status_badge(growth.get('goal_status') or growth.get('tracking_status'))}",
            f"📉 Разрыв до 50% цели: {_value(growth.get('target_gap_pct') or 'н/д')}% / {_money_rub(growth.get('target_gap_rub'))}",
        ]
    )
    lines.append(f"🧪 Режим: {_value(mode)}")
    return lines


def _growth_diagnostics_lines(
    account: dict[str, Any],
    positions: list[Any],
    candidates: list[Any],
    dynamic_watchlist: list[Any],
    policy: dict[str, Any],
) -> list[str]:
    candidate_items = [item for item in candidates if isinstance(item, dict)]
    blocked_candidates = [item for item in candidate_items if str(item.get("status") or "").upper() == "BLOCKED"]
    clean_candidates = [item for item in candidate_items if str(item.get("status") or "").upper() != "BLOCKED"]
    target_r_blocks = sum(
        1
        for item in blocked_candidates
        if "target_r_below_min_1.5" in (item.get("gate_reasons") if isinstance(item.get("gate_reasons"), list) else [])
    )
    discovered_dynamic = [
        str(item.get("symbol"))
        for item in _display_dynamic_watchlist(dynamic_watchlist)
        if isinstance(item, dict) and str(item.get("status") or "").upper() in {"DISCOVERED", "DYNAMIC_CANDIDATE", "CANDIDATE"}
    ]
    risk = policy.get("risk") if isinstance(policy.get("risk"), dict) else {}
    max_positions = _positive_int_display(risk.get("max_open_positions"), default=5)
    cash = _decimal(account.get("cash"))
    equity = _decimal(account.get("equity"))
    cash_share = (cash / equity) if cash is not None and equity and equity > 0 else None
    open_risk_pct = _decimal(account.get("open_risk_pct"))
    max_open_risk_pct = _decimal(account.get("max_total_open_risk_pct"))

    if candidate_items and not clean_candidates:
        main_drag = "мало чистых входов"
    elif not candidate_items:
        main_drag = "нет рассчитанных кандидатов на вход"
    elif cash_share is not None and cash_share >= Decimal("0.50"):
        main_drag = "капитал в основном не задействован"
    elif open_risk_pct is not None and max_open_risk_pct is not None and open_risk_pct < max_open_risk_pct * Decimal("0.50"):
        main_drag = "используется малая часть разрешённого риска"
    else:
        main_drag = "нужен разбор качества входов и удержания"

    lines = ["", _bold("📉 Почему портфель не растёт"), f"📉 Основной тормоз: {main_drag}"]
    if candidate_items:
        lines.append(
            f"⛔ {len(blocked_candidates)}/{len(candidate_items)} кандидатов заблокированы; "
            f"R/R ниже 1.5R: {target_r_blocks}"
        )
    else:
        lines.append("⛔ Чистых кандидатов на покупку в этом запуске нет")

    if cash_share is not None and cash_share >= Decimal("0.50"):
        lines.append(f"💤 Свободные деньги: {_money_rub(account.get('cash'))} — капитал в основном не работает")
    elif cash is not None:
        lines.append(f"💵 Свободные деньги: {_money_rub(account.get('cash'))}")

    lines.append(f"📦 Позиций: {len(positions)} из {max_positions} лимита")
    if open_risk_pct is not None and max_open_risk_pct is not None:
        lines.append(f"📐 Открытый риск: {_score_plain(open_risk_pct)}% из {_score_plain(max_open_risk_pct)}% лимита")
    if discovered_dynamic:
        lines.append(f"🧭 Динамические кандидаты для ручного разбора: {_value(', '.join(discovered_dynamic[:5]))}")
    if target_r_blocks:
        lines.append("🎯 Что улучшать: искать входы с запасом до цели ≥ 1.5R, не догонять импульс после ближнего сопротивления")
    else:
        lines.append("🎯 Что улучшать: проверить качество universe, относительную силу и удержание позиций")
    lines.append("🧪 Статус: только отчёт; торговые решения не меняет")
    return lines


def _positive_int_display(value: Any, *, default: int) -> int:
    decimal = _decimal(value)
    if decimal is None or decimal <= 0:
        return default
    return int(decimal)


def _display_candidates(candidates: list[Any]) -> list[Any]:
    def _score(item: Any) -> int:
        if not isinstance(item, dict):
            return -1
        score = item.get("growth_score")
        if not isinstance(score, dict):
            return -1
        try:
            return int(score.get("score") or 0)
        except (TypeError, ValueError):
            return -1

    return sorted(candidates, key=_score, reverse=True)


def _display_dynamic_watchlist(watchlist: list[Any]) -> list[Any]:
    def _sort_key(item: Any) -> tuple[int, Decimal, int]:
        if not isinstance(item, dict):
            return (0, Decimal("-999999"), -1)
        rr = _decimal(item.get("nearest_target_r"))
        try:
            score = int(item.get("discovery_score") or 0)
        except (TypeError, ValueError):
            score = -1
        if rr is None:
            return (0, Decimal("-999999"), score)
        return (1, rr, score)

    return sorted(watchlist, key=_sort_key, reverse=True)[:8]


def _growth_score_line(candidate: dict[str, Any]) -> str:
    score = candidate.get("growth_score") if isinstance(candidate.get("growth_score"), dict) else {}
    if not score:
        return "🚀 Оценка роста: н/д"
    return f"🚀 Оценка роста: {_score_plain(score.get('score'))}/100 {_score_label(score.get('label'))}"


def _research_detail_lines(research: dict[str, Any]) -> list[str]:
    items = _research_items(research)
    if items:
        lines: list[str] = []
        for item in items:
            symbol = _bold(item.get("symbol") or "UNKNOWN")
            verdict = _research_verdict_badge(item.get("verdict"))
            reason = _value(item.get("reason") or "без деталей")
            line = f"• {symbol}: {verdict} — {reason}"
            source = item.get("source_hint")
            if source:
                line += f" ({_value(source)})"
            lines.append(line)
        return lines

    summary = str(research.get("summary") or "").strip()
    if not summary or _looks_like_json_payload(summary):
        return []
    return [_value(summary)]


def _research_items(research: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = research.get("items")
    if isinstance(raw_items, list):
        return [item for item in raw_items if isinstance(item, dict)]

    summary = str(research.get("summary") or "").strip()
    if not _looks_like_json_payload(summary):
        return []
    try:
        payload = json.loads(summary)
    except json.JSONDecodeError:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _looks_like_json_payload(value: str) -> bool:
    text = value.strip()
    return (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]"))


def _rr_strategy_lines(item: dict[str, Any]) -> list[str]:
    strategy = item.get("rr_strategy") if isinstance(item.get("rr_strategy"), dict) else {}
    if not strategy:
        return []
    tier = str(strategy.get("tier") or "UNKNOWN")
    risk_multiplier = _decimal(strategy.get("risk_multiplier"))
    if tier == "A":
        risk_text = "полный базовый риск; проходит минимум 1.5R"
    elif tier in {"B", "C"} and risk_multiplier is not None:
        risk_text = f"{_format_decimal(risk_multiplier, places=2, group=False)} базового риска; только вручную/отчёт"
    elif tier == "WAIT":
        risk_text = "0.00 базового риска; ждать откат/новую цель"
    else:
        risk_text = "только отчёт; RR не рассчитан"

    lines = [f"🧭 RR-режим: {_value(tier)} — {_value(risk_text)}"]
    entry_price = item.get("entry_price_for_min_r")
    if entry_price not in {None, ""}:
        lines.append(
            f"🎯 Цена для 1.5R: {_price_plain(entry_price)}; нужен откат: {_percent_plain(item.get('required_pullback_pct'))}"
        )
    return lines


def _growth_score_breakdown_lines(candidate: dict[str, Any]) -> list[str]:
    score = candidate.get("growth_score") if isinstance(candidate.get("growth_score"), dict) else {}
    raw_components = score.get("components") if isinstance(score, dict) else None
    components = raw_components if isinstance(raw_components, dict) else {}
    if not components:
        return []
    return [
        "📊 Разбор оценки: "
        f"H4-импульс {_score_plain(components.get('h4_momentum'))}/25; "
        f"риск/цель {_score_plain(components.get('risk_reward'))}/25; "
        f"сила к рынку {_score_plain(components.get('relative_strength'))}/15; "
        f"ликвидность {_score_plain(components.get('liquidity'))}/10; "
        f"ATR {_score_plain(components.get('atr'))}/10; "
        f"новости {_score_plain(components.get('research'))}; "
        f"контекст {_score_plain(components.get('research_context'))}; "
        f"штрафы {_score_plain(components.get('gate_penalty'))}"
    ]


def _entry_blocker_lines(candidate: dict[str, Any]) -> list[str]:
    blockers = candidate.get("entry_blockers") if isinstance(candidate.get("entry_blockers"), list) else []
    if not blockers:
        return []
    return ["⛔ Что мешает входу:"] + [f"- {_value(item)}" for item in blockers]


def _advisory_action_lines(position: dict[str, Any]) -> list[str]:
    action = position.get("advisory_action") if isinstance(position.get("advisory_action"), dict) else {}
    if not action:
        return []
    return [
        f"🛡️ Действие: {_status_badge(action.get('action'))}",
        f"⚠️ Комментарий: {_value(action.get('reason'))}",
    ]


def _signed_pct(value: Any) -> str:
    decimal = _decimal(value)
    if decimal is None:
        return "н/д"
    sign = "+" if decimal >= 0 else ""
    return f"{sign}{decimal:.2f}%"


def _signed_r(value: Any) -> str:
    decimal = _decimal(value)
    if decimal is None:
        return "н/д"
    sign = "+" if decimal >= 0 else ""
    return f"{sign}{decimal:.2f}R"


def _telegram_display_warnings(warnings: list[Any]) -> list[str]:
    display: list[str] = []
    finam_contract_symbols: set[str] = set()
    for item in warnings:
        text = str(item)
        if ": Finam instrument contract недоступен:" not in text:
            continue
        symbol = text.split(":", 1)[0]
        finam_contract_symbols.add(symbol)
        display.append(f"{symbol}: контракт Finam не подтверждён, покупка заблокирована до следующей проверки")
    for item in warnings:
        text = str(item)
        symbol = text.split(":", 1)[0]
        if symbol in finam_contract_symbols and ": MOEX lot size " in text:
            continue
        if ": Finam instrument contract недоступен:" in text:
            continue
        display.append(text)
    return display


def format_trade_execution_result(output: dict[str, Any]) -> str:
    """Format a guarded demo trade result as a compact Telegram block.

    Presentation-only: no broker calls and no operator decisions. The operator
    CLI attaches this text to JSON output so Hermes can relay a readable Russian
    summary instead of raw broker payloads.
    """
    status = output.get("status")
    proposal = output.get("proposal") if isinstance(output.get("proposal"), dict) else {}
    symbol = proposal.get("symbol") or _nested(output, "buy_order_response", "order", "symbol") or "UNKNOWN"
    buy_response = output.get("buy_order_response") if isinstance(output.get("buy_order_response"), dict) else {}
    stop_response = output.get("protective_stop_response") if isinstance(output.get("protective_stop_response"), dict) else {}
    stop_verification = output.get("stop_verification") if isinstance(output.get("stop_verification"), dict) else {}
    buy_order = buy_response.get("order") if isinstance(buy_response.get("order"), dict) else {}

    if status in {"EXECUTED_DEMO", "EXECUTED_DEMO_PARTIAL"}:
        lines = [
            _bold(symbol),
            "✅ BUY выполнен / DEMO" if status == "EXECUTED_DEMO" else "✅ BUY частично выполнен / DEMO",
            f"📦 Кол-во: {_qty(_first_non_empty(_nested(buy_order, 'quantity', 'value'), proposal.get('quantity')))} шт.",
            f"💰 Цена LIMIT: {_price_plain(_first_non_empty(_nested(buy_order, 'limit_price', 'value'), _nested(proposal, 'entry', 'limit_price')))}",
            f"🧾 BUY Order: {_value(_first_non_empty(buy_response.get('order_id'), buy_order.get('order_id')))}",
            f"📌 Статус BUY: {_order_status(_first_non_empty(buy_response.get('status'), buy_order.get('status')))}",
            f"🛡️ SL: {_price_plain(_first_non_empty(_nested(proposal, 'protective_stop', 'stop_price'), _nested(output, 'protective_stop_payload', 'sl_price', 'value')))}",
            f"🧾 SL Order: {_value(stop_response.get('order_id'))}",
            f"✅ SL проверен: {_yes_no(stop_verification.get('verified'))}",
            f"🚦 Новые покупки: {'разрешены' if output.get('halt_new_buys') is False else 'заблокированы'}",
        ]
        return "\n".join(lines)

    if status == "BUY_PENDING_NO_SL":
        return "\n".join(
            [
                _bold(symbol),
                "⏳ BUY выставлен, исполнения пока нет",
                f"🧾 BUY Order: {_value(buy_response.get('order_id'))}",
                f"📌 Статус BUY: {_order_status(_nested(output, 'buy_fill_state', 'broker_status'))}",
                "🛡️ SL не выставлялся: нет исполненного объёма",
                "🚦 Новые покупки: заблокированы",
            ]
        )

    if status == "STOP_NOT_VERIFIED":
        return "\n".join(
            [
                _bold(symbol),
                "⚠️ BUY выполнен, но SL не подтверждён",
                f"🧾 BUY Order: {_value(buy_response.get('order_id'))}",
                f"🛡️ SL план: {_price_plain(_nested(proposal, 'protective_stop', 'stop_price'))}",
                "🚦 Новые покупки: заблокированы",
            ]
        )

    if status == "BROKER_ERROR":
        return "\n".join(
            [
                _bold(symbol),
                "❌ BUY не выполнен / ошибка брокера",
                f"📌 Причина: {_value(output.get('reason'))}",
                f"⚠️ Ошибка: {_value(output.get('error'))}",
                "🚦 Новые покупки: заблокированы",
            ]
        )

    return "\n".join(
        [
            _bold(symbol),
            f"⏳ Сделка не исполнена: {_value(status)}",
            f"📌 Причина: {_value(output.get('reason'))}",
        ]
    )


def format_arena_pulse(report: dict[str, Any]) -> str:
    """Compact multi-account Arena checkpoint for Telegram.

    The detailed H4 report stays available for the legacy demo runtime; Arena
    needs a scan-friendly overview because it has three concurrent accounts.
    """
    accounts = report.get("accounts") if isinstance(report.get("accounts"), list) else []
    errors = _list_value(report.get("errors"))
    warnings = _list_value(report.get("warnings"))
    total_equity = _sum_decimal(account.get("equity") for account in accounts if isinstance(account, dict))
    total_pnl = _sum_decimal(account.get("pnl_rub") for account in accounts if isinstance(account, dict))
    total_pnl_pct = None
    if total_equity is not None and total_pnl is not None:
        starting = total_equity - total_pnl
        if starting > 0:
            total_pnl_pct = total_pnl / starting * Decimal("100")

    lines = [
        _bold("🏟️ Finam Arena Pulse"),
        f"📌 Режим: {_value(report.get('mode'))}; статус: {_status_badge(report.get('status'))}",
        f"💼 Стоимость портфеля: {_money_rub(total_equity)}",
        f"📈 P&L: {_money_rub(total_pnl)} / {_signed_pct(total_pnl_pct)}",
        "",
        _bold("Счета"),
    ]
    pending_by_account: dict[str, list[dict[str, Any]]] = {}
    for approval in _list_value(report.get("pending_approvals")):
        if not isinstance(approval, dict):
            continue
        if str(approval.get("status") or "") != "pending":
            continue
        account_id = str(approval.get("account_id") or "")
        if not account_id:
            continue
        pending_by_account.setdefault(account_id, []).append(approval)

    if not accounts:
        lines.append("⏳ Счета не загружены")
    for account in accounts:
        if not isinstance(account, dict):
            continue
        status = "STOP" if account.get("halt") else _arena_account_status(account.get("status"))
        signal = account.get("top_signal") or "нет активного сигнала"
        if lines and lines[-1] != _bold("Счета"):
            lines.append("")
        lines.append(
            " ".join(
                [
                    _arena_account_icon(account),
                    f"{_bold(account.get('account_id'))} {_value(account.get('label'))}:",
                    f"{_money_rub(account.get('equity'))}",
                    f"({_signed_pct(account.get('pnl_pct'))})",
                ]
            )
        )
        lines.append(
            " ".join(
                [
                    f"📦 {account.get('positions_count', 0)}",
                    f"⚠️ риск {_percent_plain(account.get('open_risk_pct'))}",
                    _status_badge(status),
                    f"🎯 {_value(signal)}",
                ]
            )
        )
        entry_limits = _dict_value(account.get("entry_limits"))
        if entry_limits:
            lines.append(
                "   лимиты: "
                f"primary {entry_limits.get('primary_used', 0)}/{entry_limits.get('primary_limit', 0)}; "
                f"replacement {entry_limits.get('replacement_used', 0)}/{entry_limits.get('replacement_limit', 0)}"
            )
        for approval in pending_by_account.get(str(account.get("account_id") or ""), [])[:2]:
            status = str(approval.get("status") or "")
            icon = "⏳" if status == "pending" else "⌛"
            confirmation = _value(approval.get("confirmation"))
            expires_at = _value(approval.get("expires_at"))
            status_label = "ожидает подтверждения" if status == "pending" else _value(status)
            lines.append(f"   {icon} {status_label}: {confirmation} до {expires_at}")

    lines.extend(["", _bold("Безопасность")])
    if errors:
        lines.extend(f"🔴 {_value(item)}" for item in errors[:3])
    elif warnings:
        lines.extend(f"🟠 {_value(item)}" for item in _arena_warning_summary(warnings, limit=3))
    else:
        lines.append("🟢 API/счета доступны; broker mutations только через Arena gates")
    lines.append("🧭 Детали и управление: кнопки под сообщением")
    return "\n".join(lines)


def arena_reply_markup() -> dict[str, Any]:
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


def arena_strategy_reply_markup(policy: dict[str, Any]) -> dict[str, Any]:
    rows = [[{"text": "📊 Обзор", "callback_data": "arena:overview"}]]
    for account in _list_value(policy.get("accounts")):
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("account_id") or "")
        if not account_id:
            continue
        icon = _arena_account_icon(account)
        pause_action = "resume" if account.get("paused") is True else "pause"
        pause_text = "▶️" if pause_action == "resume" else "⏸️"
        trade_action = "manual" if account.get("trade_mode") == "auto" else "auto"
        trade_text = "manual" if trade_action == "manual" else "auto"
        rows.append(
            [
                {"text": f"{icon} {pause_text}", "callback_data": f"arena:st:{account_id}:{pause_action}"},
                {"text": f"{icon} {trade_text}", "callback_data": f"arena:st:{account_id}:{trade_action}"},
                {"text": f"{icon} risk+", "callback_data": f"arena:st:{account_id}:risk_up"},
                {"text": f"{icon} risk-", "callback_data": f"arena:st:{account_id}:risk_down"},
            ]
        )
    rows.append([{"text": "🚨 Риски", "callback_data": "arena:risks"}, {"text": "⚙️ Стратегия", "callback_data": "arena:strategy"}])
    return {"inline_keyboard": rows}


def format_arena_account_detail(report: dict[str, Any], account_id: str) -> str:
    account = _arena_find_account(report, account_id)
    if account is None:
        return "\n".join([_bold("🏟️ Finam Arena"), f"🔴 Счёт {_value(account_id)} не найден"])

    positions = _list_value(account.get("positions"))
    stops = _list_value(account.get("stop_orders") or account.get("active_stop_orders"))
    trades = _list_value(account.get("recent_trades"))
    candidates = [
        item for item in _list_value(report.get("candidates"))
        if isinstance(item, dict) and str(item.get("account_id")) == str(account_id)
    ]
    pending_approvals = [
        item for item in _list_value(report.get("pending_approvals"))
        if isinstance(item, dict)
        and str(item.get("status") or "") == "pending"
        and str(item.get("account_id") or "") == str(account_id)
    ]
    lines = [
        f"{_arena_account_icon(account)} {_bold(account.get('account_id'))} {_value(account.get('label'))}",
        f"💼 Портфель: {_money_rub(account.get('equity'))} / cash {_money_rub(account.get('cash') or account.get('available_cash'))}",
        f"📈 P&L: {_money_rub(account.get('pnl_rub'))} / {_signed_pct(account.get('pnl_pct'))}",
        f"⚠️ Открытый риск: {_percent_plain(account.get('open_risk_pct'))}",
        f"🧠 Стратегия: {_value(account.get('strategy'))}",
    ]
    if pending_approvals:
        lines.extend(["", _bold("Ожидает подтверждения")])
        for approval in pending_approvals[:5]:
            confirmation = _value(approval.get("confirmation"))
            expires_at = _value(approval.get("expires_at"))
            lines.append(f"• {confirmation} до {expires_at}")
            lines.append(f"  команда: python scripts/hermes_operator.py arena-confirm --live --confirmation \"{confirmation}\"")
    lines.extend(["", _bold("Позиции")])
    lines.extend(_arena_position_lines(positions, empty="нет открытых позиций"))
    lines.extend(["", _bold("Стоп-защита")])
    lines.extend(_arena_stop_lines(stops, empty="нет активных стопов"))
    lines.extend(["", _bold("Последние сделки")])
    lines.extend(_arena_trade_lines(trades, empty="нет сделок"))
    lines.extend(["", _bold("Кандидаты")])
    lines.extend(_arena_candidate_lines(candidates, empty=account.get("top_signal") or "нет активного сигнала"))
    return "\n".join(lines)


def format_arena_risks_view(report: dict[str, Any]) -> str:
    errors = _list_value(report.get("errors"))
    warnings = _list_value(report.get("warnings"))
    accounts = [item for item in _list_value(report.get("accounts")) if isinstance(item, dict)]
    lines = [_bold("🚨 Arena Risks")]
    for account in accounts:
        status = "STOP" if account.get("halt") else _arena_account_status(account.get("status"))
        lines.append(
            f"{_arena_account_icon(account)} {_bold(account.get('account_id'))} "
            f"{_status_badge(status)} risk {_percent_plain(account.get('open_risk_pct'))}"
        )
    lines.append("")
    if errors:
        lines.extend(f"🔴 {_value(item)}" for item in errors[:6])
    elif warnings:
        lines.extend(f"🟠 {_value(item)}" for item in _arena_warning_summary(warnings, limit=6))
    else:
        lines.append("🟢 Hard halt причин нет")
    return "\n".join(lines)


def format_arena_portfolio_review(review: dict[str, Any]) -> str:
    accounts = [item for item in _list_value(review.get("accounts")) if isinstance(item, dict)]
    replacements = [item for item in _list_value(review.get("replacement_proposals")) if isinstance(item, dict)]
    exits = [item for item in _list_value(review.get("exit_proposals")) if isinstance(item, dict)]
    cost_model = _dict_value(review.get("cost_model"))
    lines = [
        _bold("🔁 Arena Rotation"),
        f"📌 Status: {_status_badge(review.get('status'))}",
        f"💸 Комиссия: {_arena_commission_grid_text(cost_model)}; источник {_value(cost_model.get('source'))}",
        "",
        _bold("Счета"),
    ]
    for account in accounts:
        entry_limits = _dict_value(account.get("entry_limits"))
        limit_text = ""
        if entry_limits:
            limit_text = (
                f"; primary {entry_limits.get('primary_used', 0)}/{entry_limits.get('primary_limit', 0)}; "
                f"replacement {entry_limits.get('replacement_used', 0)}/{entry_limits.get('replacement_limit', 0)}"
            )
        lines.append(
            f"{_bold(account.get('account_id'))}: cash {_money_rub(account.get('cash'))}; "
            f"gross {_percent_plain(account.get('gross_exposure_pct'))}{limit_text}"
        )
        replacement = _dict_value(account.get("replacement"))
        if replacement:
            lines.append(
                f"   🔁 заменить {_value(replacement.get('sell_symbol'))} -> {_value(replacement.get('buy_symbol'))}; "
                f"score +{_value(replacement.get('score_delta'))}; cost {_money_rub(replacement.get('estimated_cost_rub'))}"
            )
        else:
            block_reason = account.get("replacement_block_reason")
            reason = f": {_value(block_reason)}" if block_reason else ": преимущество не покрывает риск/комиссию"
            lines.append(f"   замены нет{reason}")
    lines.extend(["", _bold("Выходы")])
    if not exits:
        lines.append("нет exit/TP/trailing proposals")
    for item in exits[:8]:
        lines.append(
            f"{_value(item.get('account_id'))} {_value(item.get('symbol'))}: "
            f"{_value(item.get('action'))}; R {_value(item.get('progress_r'))}; {_value(item.get('reason'))}"
        )
    confirmations = [
        str(item.get("confirmation_phrase") or "").strip()
        for item in [*replacements, *exits]
        if isinstance(item, dict) and str(item.get("confirmation_phrase") or "").strip()
    ]
    if confirmations:
        lines.extend(["", _bold("Подтверждения")])
        for confirmation in confirmations[:8]:
            lines.append(_value(confirmation))
    return "\n".join(lines)


def format_arena_attribution(report: dict[str, Any]) -> str:
    accounts = [item for item in _list_value(report.get("accounts")) if isinstance(item, dict)]
    totals = _dict_value(report.get("totals"))
    cost_model = _dict_value(report.get("cost_model"))
    lines = [
        _bold("📊 Arena Attribution"),
        f"📌 Status: {_status_badge(report.get('status'))}",
        f"💸 Комиссия: {_arena_commission_grid_text(cost_model)}",
        f"💱 Валюта: виртуальные рубли Arena, без FX",
        "",
        _bold("Итого"),
        f"оборот {_money_rub(totals.get('turnover'))}; комиссия {_money_rub(totals.get('estimated_commission'))}; "
        f"realized P&L ≈ {_money_rub(totals.get('realized_pnl_estimate'))}; "
        f"unrealized P&L {_money_rub(totals.get('unrealized_pnl'))}",
        "",
        _bold("Счета"),
    ]
    for account in accounts:
        incomplete = _value(account.get("incomplete_trades_count"))
        source = _value(account.get("attribution_source"))
        lines.append(
            f"{_bold(account.get('account_id'))}: ops {_value(account.get('operations_count'))}; "
            f"BUY {_money_rub(account.get('buy_turnover'))}; SELL {_money_rub(account.get('sell_turnover'))}; "
            f"комиссия {_money_rub(account.get('estimated_commission'))}; "
            f"realized ≈ {_money_rub(account.get('realized_pnl_estimate'))}; "
            f"unrealized {_money_rub(account.get('unrealized_pnl'))}; "
            f"source {source}; incomplete {incomplete}"
        )
    incomplete_total = sum(int(account.get("incomplete_trades_count") or 0) for account in accounts)
    if incomplete_total:
        lines.extend(
            [
                "",
                f"⚠️ {incomplete_total} сделок из Arena recent trades без quantity не включены в оборот/комиссию.",
            ]
        )
    return "\n".join(lines)


def _arena_commission_grid_text(cost_model: dict[str, Any]) -> str:
    by_mic = cost_model.get("commission_pct_by_mic") if isinstance(cost_model.get("commission_pct_by_mic"), dict) else {}
    if by_mic:
        return "MISX/RUSX 0.035%, RTSX 0.001%, США 0.1%; от полной суммы сделки, без ГО"
    return f"{_percent_plain(cost_model.get('commission_pct_per_side'))} за сторону"


def format_arena_strategy_view(policy: dict[str, Any]) -> str:
    accounts = _list_value(policy.get("accounts"))
    risk = _dict_value(policy.get("risk"))
    lines = [
        _bold("⚙️ Arena Strategy"),
        f"📌 Mode: {_value(policy.get('mode'))}",
        f"🎚️ Risk/trade: {_percent_plain(risk.get('risk_per_trade_pct'))}",
        f"🛡️ Daily loss halt: {_percent_plain(risk.get('max_daily_loss_pct'))}",
        f"📉 Drawdown halt: {_percent_plain(risk.get('max_account_drawdown_pct'))}",
        "",
    ]
    for account in accounts:
        if not isinstance(account, dict):
            continue
        lines.append(
            f"{_arena_account_icon(account)} {_bold(account.get('account_id'))} "
            f"{_value(account.get('label'))}: {_value(account.get('strategy'))}; "
            f"mode {_value(account.get('trade_mode') or 'manual')}; "
            f"risk x{_value(account.get('risk_multiplier') or '1.0')}; "
            f"paused {_value(account.get('paused') is True)}; "
            f"universe {len(_list_value(account.get('universe')))}"
        )
    lines.extend(["", "🔒 Изменения только через proposal + confirm"])
    return "\n".join(lines)


def format_arena_strategy_proposal(proposal: dict[str, Any]) -> str:
    changes = _list_value(proposal.get("changes"))
    validation = _dict_value(proposal.get("validation"))
    errors = _list_value(validation.get("errors"))
    warnings = _list_value(validation.get("warnings"))
    lines = [
        _bold("⚙️ Arena Strategy Proposal"),
        f"📌 Status: {_value(proposal.get('status'))}",
        f"💼 Account: {_value(proposal.get('account_id'))}",
        "",
        _bold("Diff"),
    ]
    if changes:
        for change in changes[:8]:
            if isinstance(change, dict):
                if change.get("action") in {"add", "remove"}:
                    sign = "+" if change.get("action") == "add" else "-"
                    lines.append(f"• {_value(change.get('path'))}: {sign}{_value(change.get('symbol'))}")
                else:
                    lines.append(
                        f"• {_value(change.get('path'))}: "
                        f"{_value(change.get('old'))} → {_value(change.get('new'))}"
                    )
            else:
                lines.append(f"• {_value(change)}")
    else:
        lines.append("• изменений нет")
    if errors:
        lines.extend(["", _bold("Errors")])
        lines.extend(f"🔴 {_value(item)}" for item in errors[:5])
    elif warnings:
        lines.extend(["", _bold("Warnings")])
        lines.extend(f"🟠 {_value(item)}" for item in warnings[:5])
    lines.extend(["", "🔒 Для применения нужен отдельный confirm"])
    return "\n".join(lines)


def _arena_account_icon(account: dict[str, Any]) -> str:
    account_id = str(account.get("account_id") or "")
    if account_id == "DEMO-RU":
        return "🇷🇺"
    if account_id == "DEMO-US":
        return "🇺🇸"
    if account_id == "DEMO-AI":
        return "🧪"
    return "💼"


def _arena_account_status(value: Any) -> str:
    if value in {None, "", "ACCOUNT_ACTIVE"}:
        return "OK"
    return str(value)


def _arena_find_account(report: dict[str, Any], account_id: str) -> dict[str, Any] | None:
    for account in _list_value(report.get("accounts")):
        if isinstance(account, dict) and str(account.get("account_id")) == str(account_id):
            return account
    return None


def _arena_compact_items(items: list[Any], *, empty: Any) -> list[str]:
    if not items:
        return [f"• {_value(empty)}"]
    lines: list[str] = []
    for item in items[:5]:
        if isinstance(item, dict):
            symbol = item.get("symbol") or item.get("ticker") or item.get("instrument") or item.get("id") or "item"
            qty = item.get("quantity") or item.get("qty") or item.get("balance")
            side = item.get("side") or item.get("status") or item.get("type")
            price = item.get("price") or item.get("entry_price") or item.get("stop_price")
            parts = [_value(symbol)]
            if side:
                parts.append(_value(side))
            if qty:
                parts.append(f"x{_value(qty)}")
            if price:
                parts.append(f"@ {_value(price)}")
            lines.append("• " + " ".join(parts))
        else:
            lines.append(f"• {_value(item)}")
    return lines




def _arena_display_symbol(value: Any) -> str:
    symbol = str(value or "").strip()
    if not symbol:
        return "н/д"
    return _value(symbol.split("@", maxsplit=1)[0])


def _arena_qty_plain(value: Any) -> str:
    decimal = _decimal(value)
    if decimal is None:
        return "н/д"
    if decimal == decimal.to_integral_value():
        return f"{decimal.to_integral_value():,}".replace(",", " ")
    return _format_decimal(decimal.normalize(), places=2)


def _arena_side_action(value: Any) -> str:
    side = str(value or "").upper()
    if "SELL" in side:
        return "продать"
    if "BUY" in side:
        return "купить"
    return _value(value)


def _arena_trade_action(value: Any) -> str:
    side = str(value or "").upper()
    if "SELL" in side:
        return "Продано"
    if "BUY" in side:
        return "Куплено"
    return "Сделка"


def _arena_price_value(item: dict[str, Any]) -> Any:
    return _first_non_empty(
        item.get("price"),
        item.get("entry_price"),
        item.get("average_price"),
        item.get("current_price"),
        item.get("stop_price"),
        item.get("stop"),
    )


def _arena_notional_value(item: dict[str, Any]) -> Decimal | None:
    explicit = _decimal(_first_non_empty(item.get("notional"), item.get("amount"), item.get("turnover")))
    if explicit is not None:
        return explicit
    quantity = _decimal(_first_non_empty(item.get("quantity"), item.get("qty"), item.get("balance")))
    price = _decimal(_arena_price_value(item))
    if quantity is None or price is None:
        return None
    return abs(quantity) * price


def _arena_amount_suffix(item: dict[str, Any]) -> str:
    notional = _arena_notional_value(item)
    if notional is None:
        return "сумма н/д"
    return f"сумма ≈ {_money_rub(notional)}"


def _arena_position_lines(items: list[Any], *, empty: Any) -> list[str]:
    if not items:
        return [f"• {_value(empty)}"]
    lines: list[str] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            lines.append(f"• {_value(item)}")
            continue
        symbol = _arena_display_symbol(item.get("symbol") or item.get("ticker") or item.get("instrument"))
        quantity = _arena_qty_plain(_first_non_empty(item.get("quantity"), item.get("qty"), item.get("balance")))
        side = _value(item.get("side") or "LONG")
        entry = _price_plain(_first_non_empty(item.get("average_price"), item.get("entry_price"), item.get("price")))
        current = _price_plain(item.get("current_price"))
        line = f"• {symbol}: {side} {quantity} шт."
        if entry != "н/д":
            line += f"; вход {entry}"
        if current != "н/д":
            line += f"; сейчас {current}"
        lines.append(line)
    return lines


def _arena_stop_lines(items: list[Any], *, empty: Any) -> list[str]:
    if not items:
        return [f"• {_value(empty)}"]
    lines: list[str] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            lines.append(f"• {_value(item)}")
            continue
        symbol = _arena_display_symbol(item.get("symbol") or item.get("ticker") or item.get("instrument"))
        action = _arena_side_action(item.get("side"))
        quantity = _arena_qty_plain(_first_non_empty(item.get("quantity"), item.get("qty"), item.get("balance")))
        price = _price_plain(_first_non_empty(item.get("stop_price"), item.get("stop"), item.get("price")))
        lines.append(f"• {symbol}: стоп — {action} {quantity} шт. по {price}; {_arena_amount_suffix(item)}")
    return lines


def _arena_trade_lines(items: list[Any], *, empty: Any) -> list[str]:
    if not items:
        return [f"• {_value(empty)}"]
    lines: list[str] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            lines.append(f"• {_value(item)}")
            continue
        symbol = _arena_display_symbol(item.get("symbol") or item.get("ticker") or item.get("instrument"))
        action = _arena_trade_action(item.get("side"))
        quantity = _arena_qty_plain(_first_non_empty(item.get("quantity"), item.get("qty"), item.get("balance")))
        price = _price_plain(_arena_price_value(item))
        pnl_value = _first_non_empty(item.get("pnl"), item.get("realized_pnl"), item.get("profit"))
        estimated_pnl = item.get("realized_pnl_estimate")
        pnl = _money_rub(pnl_value)
        line = f"• {action} {symbol}: {quantity} шт."
        if price != "н/д":
            line += f" по {price}"
        if pnl != "н/д RUB":
            pnl_text = f"P&L: {pnl}"
        elif estimated_pnl not in {None, ""}:
            pnl_text = f"P&L ≈ {_money_rub(estimated_pnl)}"
        else:
            pnl_text = "P&L: н/д"
        line += f"; {_arena_amount_suffix(item)}; {pnl_text}"
        lines.append(line)
    return lines


def _arena_candidate_lines(items: list[Any], *, empty: Any) -> list[str]:
    if not items:
        return [f"• {_value(empty)}"]
    lines: list[str] = []
    for item in items[:5]:
        if not isinstance(item, dict):
            lines.append(f"• {_value(item)}")
            continue
        symbol = _arena_display_symbol(item.get("symbol") or item.get("ticker") or item.get("instrument"))
        action = _arena_side_action(item.get("side") or "BUY")
        quantity = _arena_qty_plain(_first_non_empty(item.get("quantity"), item.get("qty"), item.get("balance")))
        price = _price_plain(_arena_price_value(item))
        line = f"• {symbol}: кандидат — {action} {quantity} шт."
        if price != "н/д":
            line += f" по {price}"
        line += f"; {_arena_amount_suffix(item)}"
        score = item.get("growth_score") if isinstance(item.get("growth_score"), dict) else {}
        if score:
            line += f"; оценка {_score_plain(score.get('score'))}/100"
        lines.append(line)
    return lines


def _arena_warning_summary(warnings: list[Any], *, limit: int) -> list[str]:
    market_data_by_account: dict[str, list[str]] = {}
    other: list[str] = []
    for warning in warnings:
        text = str(warning)
        match = re.match(r"^(?P<account>[^:]+):(?P<symbol>[^:]+): market data unavailable:", text)
        if match:
            market_data_by_account.setdefault(match.group("account"), []).append(match.group("symbol"))
        else:
            other.append(text)

    summary: list[str] = []
    for account_id, symbols in market_data_by_account.items():
        preview = ", ".join(symbols[:4])
        suffix = "" if len(symbols) <= 4 else f" +{len(symbols) - 4}"
        summary.append(f"{account_id}: market data unavailable for {len(symbols)} symbols: {preview}{suffix}")
    summary.extend(other)
    return summary[:limit]


def split_telegram_messages(text: str, *, limit: int = TELEGRAM_SAFE_MESSAGE) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0

    for line in text.splitlines():
        if len(line) > limit:
            flush()
            chunks.extend(_split_long_line(line, limit=limit))
            continue

        extra = len(line) if not current else len(line) + 1
        if current and current_len + extra > limit:
            flush()

        current.append(line)
        current_len = len(line) if current_len == 0 else current_len + extra

    flush()
    return chunks


def _split_long_line(line: str, *, limit: int) -> list[str]:
    return [line[index:index + limit] for index in range(0, len(line), limit)]


def send_telegram_message(text: str, *, reply_markup: dict[str, Any] | None = None) -> None:
    validate_telegram_html(text)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = (
        os.environ.get("FINAM_H4_TELEGRAM_CHAT_ID")
        or os.environ.get("H4_MONITOR_TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_HOME_CHANNEL")
    )
    thread_id = os.environ.get("FINAM_H4_TELEGRAM_THREAD_ID") or os.environ.get("TELEGRAM_HOME_CHANNEL_THREAD_ID")

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not chat_id:
        raise RuntimeError("Telegram chat id is not set")

    for message in split_telegram_messages(text):
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        if thread_id:
            payload["message_thread_id"] = thread_id

        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {parsed.get('description', 'unknown error')}")


def _format_msk(value: datetime) -> str:
    return value.astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _value(value: Any) -> str:
    if value is None or value == "":
        return "н/д"
    return html.escape(_sanitize_display_text(str(value)), quote=False)


def _format_decimal(decimal: Decimal, *, places: int = 2, group: bool = True) -> str:
    text = f"{decimal:,.{places}f}" if group else f"{decimal:.{places}f}"
    return text.replace(",", " ")


def _integer_plain(value: Any) -> str:
    decimal = _decimal(value)
    if decimal is None:
        return "н/д"
    return f"{decimal.to_integral_value():,}".replace(",", " ")


def _score_plain(value: Any) -> str:
    decimal = _decimal(value)
    if decimal is None:
        return "н/д"
    if decimal == decimal.to_integral_value():
        return f"{decimal.to_integral_value():,}".replace(",", " ")
    return _format_decimal(decimal, places=2)


def _r_value(value: Any) -> str:
    decimal = _decimal(value)
    if decimal is None:
        return "н/д"
    return f"{decimal:.2f}R"


def _percent_plain(value: Any) -> str:
    decimal = _decimal(value)
    if decimal is None:
        return "н/д"
    return f"{decimal:.2f}%"


def _score_label(value: Any) -> str:
    mapping = {
        "LOW": "НИЗКАЯ",
        "MEDIUM": "СРЕДНЯЯ",
        "HIGH": "ВЫСОКАЯ",
        "VERY_HIGH": "ОЧЕНЬ ВЫСОКАЯ",
    }
    return _value(mapping.get(str(value or "").upper(), value or "н/д"))


def _candidate_source_label(value: Any) -> str:
    mapping = {
        "static": "статический список",
        "dynamic": "динамический поиск",
        "held_scale_in": "докупка текущей позиции",
    }
    return mapping.get(str(value or ""), str(value or "н/д"))


def _signal_reason_label(value: Any) -> str:
    mapping = {
        "h4_bearish_breakdown_candidate": "H4 показывает риск пробоя вниз",
        "h4_bullish_breakout_candidate": "H4 показывает возможный пробой вверх",
        "latest_h4_not_bullish": "последняя H4-свеча не подтверждает рост",
    }
    return mapping.get(str(value or ""), str(value or "н/д"))


def _gate_reason_label(value: Any) -> str:
    mapping = {
        "target_r_below_min_1.5": "R/R ниже минимума 1.5R",
        "scale_in_live_execution_disabled": "live-докупка отключена",
        "scale_in_progress_below_1r": "докупка заблокирована: позиция ещё не дошла до +1R",
        "second_tier_requires_confirmation": "докупка требует отдельного подтверждения",
        "finam_contract_unverified": "контракт Finam не подтверждён",
        "finam_contract_not_longable": "инструмент не подтверждён для LONG",
        "low_price": "цена ниже допустимого минимума",
        "low_turnover": "оборот ниже минимума",
        "low_trades": "мало сделок",
        "stale_marketdata": "устаревшие рыночные данные",
        "unknown_target_resistance": "не найдена ближайшая цель/сопротивление",
        "scale_in_unknown_target_resistance": "для докупки не найдена ближайшая цель/сопротивление",
        "current_protective_stop_missing": "не найден текущий защитный SELL SL",
        "perplexity_news_avoid": "новостная проверка дала AVOID/RISK",
        "codex_review_avoid": "новостная проверка дала AVOID/RISK",
        "research_unavailable_requires_manual_confirmation": "исследование недоступно, нужно ручное подтверждение",
        "research_unavailable_below_exceptional_score": "нет свежего Codex-review, а идея не exceptional",
        "entry_strength_confirmation_missing": "нет силы к рынку и нет M30-подтверждения",
        "provider_client_failure_no_signal": "сбой provider/client, торгового сигнала нет",
        "max_new_trades_per_run_selection_limit": "не выбран лимитом сделок на текущий запуск",
        "incomplete_sl_tp_quantity_or_risk": "неполный расчёт стопа/цели/количества/риска",
        "cross_market_role_misx_concentration": "счёт уже перегружен российскими позициями по своей роли",
        "single_symbol_anchor_rotation_candidate": "кандидат на ротацию якорной позиции; требуется отдельное решение",
    }
    return mapping.get(str(value), str(value))


def _sanitize_display_text(value: str) -> str:
    lowered = value.lower()
    if "nonetype" in lowered and "iterable" in lowered:
        return "Сбой provider/client до получения ответа; торговый сигнал не сформирован."
    if "openai-codex" in lowered and "typeerror" in lowered:
        return "Сбой provider/client до получения ответа; торговый сигнал не сформирован."
    return value


def _nested(data: Any, *keys: str) -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in {None, ""}:
            return value
    return None


def _yes_no(value: Any) -> str:
    if value is True:
        return "да"
    if value is False:
        return "нет"
    return "n/a"


def _order_status(value: Any) -> str:
    mapping = {
        "ORDER_STATUS_NEW": "NEW",
        "ORDER_STATUS_WATCHING": "WATCHING",
        "ORDER_STATUS_MATCHED": "MATCHED",
        "ORDER_STATUS_CANCELLED": "CANCELLED",
        "ORDER_STATUS_REJECTED": "REJECTED",
    }
    return _value(mapping.get(str(value), value))


def _research_verdict_badge(value: Any) -> str:
    verdict = str(value or "UNAVAILABLE").upper()
    mapping = {
        "OK": _status_badge("OK"),
        "RISK": _status_badge("RISK"),
        "AVOID": _status_badge("AVOID"),
        "UNAVAILABLE": _status_badge("UNAVAILABLE"),
    }
    return mapping.get(verdict, f"⚪ {_bold(verdict)}")


def _status_badge(value: Any) -> str:
    """Unified Telegram status badge: emoji + bold Russian label for every ticker block."""
    status = str(value or "INFO").upper()
    mapping = {
        "OK": "🟢 <b>ОК</b>",
        "LONG": "🟢 <b>ЛОНГ</b>",
        "HOLD": "🟢 <b>ДЕРЖАТЬ</b>",
        "AHEAD": "🟢 <b>ВЫШЕ ПЛАНА</b>",
        "ADD": "🟢 <b>ДОБАВИТЬ</b>",
        "WATCH": "🟡 <b>НАБЛЮДАТЬ</b>",
        "DISCOVERED": "🟡 <b>НАЙДЕНО</b>",
        "DYNAMIC_CANDIDATE": "🟡 <b>КАНДИДАТ</b>",
        "WAIT": "🟡 <b>ЖДАТЬ</b>",
        "CANDIDATE": "🟡 <b>КАНДИДАТ</b>",
        "BREAKEVEN_CANDIDATE": "🟡 <b>Б/У КАНДИДАТ</b>",
        "TRAIL_CANDIDATE": "🟡 <b>ТРЕЙЛ КАНДИДАТ</b>",
        "REDUCE_CANDIDATE": "🟡 <b>СОКРАТИТЬ?</b>",
        "ON_TRACK": "🟡 <b>ПО ПЛАНУ</b>",
        "BEHIND": "🟠 <b>ОТСТАЁМ</b>",
        "RISK": "🟠 <b>РИСК</b>",
        "DRAWDOWN": "🔴 <b>ПРОСАДКА</b>",
        "EXIT_CANDIDATE": "🔴 <b>ВЫХОД?</b>",
        "BLOCK": "🔴 <b>БЛОК</b>",
        "BLOCKED": "🔴 <b>БЛОК</b>",
        "STOP": "🔴 <b>СТОП</b>",
        "SELL/STOP": "🔴 <b>СТОП</b>",
        "AVOID": "🔴 <b>ИЗБЕГАТЬ</b>",
        "REJECTED": "🔴 <b>ОТКЛОНЕНО</b>",
        "UNAVAILABLE": "⚪ <b>НЕТ ДАННЫХ</b>",
        "REPORT_ONLY": "⚪ <b>ТОЛЬКО ОТЧЁТ</b>",
        "INFO": "⚪ <b>ИНФО</b>",
    }
    return mapping.get(status, f"⚪ {_bold(status)}")


def _bold(value: Any) -> str:
    return f"<b>{_value(value)}</b>"


def _qty(value: Any) -> str:
    decimal = _decimal(value)
    if decimal is None:
        return "n/a"
    if decimal == decimal.to_integral_value():
        return str(decimal.to_integral_value())
    return f"{decimal.normalize():f}".rstrip("0").rstrip(".")


def _amount(value: Any) -> str:
    decimal = _decimal(value)
    return "н/д" if decimal is None else _format_decimal(decimal, places=2)


def _money_rub(value: Any) -> str:
    amount = _amount(value)
    return f"{amount} RUB" if amount != "н/д" else "н/д RUB"


def _price_plain(value: Any) -> str:
    return _amount(value)


def _stop_atr_label(value: Any) -> str:
    multiplier = _decimal(value) or Decimal("2")
    return f"{multiplier.normalize()}ATR"


def _side(value: Any) -> str:
    if value == "SIDE_SELL":
        return "продажа"
    if value == "SIDE_BUY":
        return "покупка"
    return _value(value)


def _position_status(value: Any) -> str:
    if value == "HOLD":
        return _status_badge("HOLD")
    if value == "SELL/STOP":
        return _status_badge("SELL/STOP")
    if value == "ADD":
        return _status_badge("ADD")
    if value == "WAIT":
        return _status_badge("WAIT")
    return _status_badge("WATCH")


def _target_2r(entry: Any, stop: Any) -> Decimal | None:
    entry_decimal = _decimal(entry)
    stop_decimal = _decimal(stop)
    if entry_decimal is None or stop_decimal is None or entry_decimal <= stop_decimal:
        return None
    return entry_decimal + (entry_decimal - stop_decimal) * Decimal("2")


def _distance_plain(upper: Any, lower: Any) -> str:
    upper_decimal = _decimal(upper)
    lower_decimal = _decimal(lower)
    if upper_decimal is None or lower_decimal is None:
        return "n/a"
    diff = upper_decimal - lower_decimal
    if upper_decimal == 0:
        return f"{_price_plain(diff)} (n/a%)"
    pct = diff / upper_decimal * Decimal("100")
    return f"{_price_plain(diff)} ({_price_plain(pct)}%)"


def _distance_to_price(current: Any, target: Any) -> str:
    current_decimal = _decimal(current)
    target_decimal = _decimal(target)
    if current_decimal is None or target_decimal is None:
        return "n/a"
    diff = abs(target_decimal - current_decimal)
    if current_decimal == 0:
        return f"{_price_plain(diff)} (n/a%)"
    pct = diff / current_decimal * Decimal("100")
    return f"{_price_plain(diff)} ({_price_plain(pct)}%)"


def _progress_value(value: Any, entry: Any, current: Any, stop: Any) -> str:
    progress = _decimal(value)
    if progress is not None:
        return f"{progress:.2f}R"
    return _progress_r(entry, current, stop)


def _progress_r(entry: Any, current: Any, stop: Any) -> str:
    entry_decimal = _decimal(entry)
    current_decimal = _decimal(current)
    stop_decimal = _decimal(stop)
    if entry_decimal is None or current_decimal is None or stop_decimal is None:
        return "n/a"
    risk = entry_decimal - stop_decimal
    if risk <= 0:
        return "n/a"
    return f"{((current_decimal - entry_decimal) / risk):.2f}R"


def _sum_decimal(values: Any) -> Decimal | None:
    total = Decimal("0")
    seen = False
    for value in values:
        decimal = _decimal(value)
        if decimal is None:
            continue
        total += decimal
        seen = True
    return total if seen else None


def _action_line(positions: list[Any], candidates: list[Any], errors: list[Any], warnings: list[Any]) -> str:
    if errors:
        return "⏳ Ничего не делать: Finam/API проверка не завершена, новые сделки запрещены"
    missing_stops = [
        item for item in positions
        if isinstance(item, dict) and item.get("quantity") not in {None, "0", "0.0"} and not item.get("has_watching_sell_sltp")
    ]
    if missing_stops:
        return "⚠️ Проверить защитные стопы: по части лонг-позиций активный стоп на продажу не найден"
    active_candidates = [item for item in candidates if isinstance(item, dict) and item.get("status") != "BLOCKED"]
    blocked_candidates = [item for item in candidates if isinstance(item, dict) and item.get("status") == "BLOCKED"]
    if active_candidates:
        return "⏳ Новую покупку не выполнять без подтверждения в Telegram"
    if blocked_candidates:
        return "⏳ Ничего не делать: кандидаты на покупку заблокированы правилами"
    if warnings:
        return "⏳ Ничего не делать: есть пропуски данных, новые сделки запрещены"
    return "⏳ Ничего не делать: лонг-позиции защищены активными стопами"


def _recommendation_line(positions: list[Any], candidates: list[Any]) -> str:
    active_candidates = [item for item in candidates if isinstance(item, dict) and item.get("status") != "BLOCKED"]
    blocked_candidates = [item for item in candidates if isinstance(item, dict) and item.get("status") == "BLOCKED"]
    if active_candidates:
        return "✅ Рекомендация: держать текущие позиции; кандидаты — только после ручного подтверждения"
    if blocked_candidates:
        return "✅ Рекомендация: держать; заблокированные кандидаты не покупать"
    watch_count = sum(1 for item in positions if isinstance(item, dict) and item.get("status") == "WATCH")
    if watch_count:
        return "✅ Рекомендация: держать защищённые позиции; наблюдать бумаги с прогрессом < 0.25R"
    if any(isinstance(item, dict) and item.get("quantity") not in {None, "0", "0.0"} for item in positions):
        return "✅ Рекомендация: держать оставшиеся позиции"
    return "✅ Рекомендация: держать оставшиеся позиции"



def _candidate_status_line(candidate: dict[str, Any]) -> str:
    if candidate.get("status") == "BLOCKED":
        return f"{_status_badge('BLOCK')} — заблокировано правилом; не покупать"
    if candidate.get("requires_confirmation"):
        return f"{_status_badge('WAIT')} — требуется подтверждение перед покупкой"
    return f"{_status_badge('WATCH')} — кандидат к проверке"


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
