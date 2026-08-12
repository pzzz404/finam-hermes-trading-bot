#!/usr/bin/env python3
"""Generate a compact Arena missed-opportunities report from cached H1 bars.

Read-only: no broker or policy mutations.  Intended for the daily "what did we
miss?" review after the RU medium-high contour change.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

RUNTIME = Path.home() / ".local/state/finam-trading-bot/runtime"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _pct(start: Decimal, end: Decimal) -> Decimal:
    return (end - start) / start * Decimal("100")


def _payload(value: Decimal | None, scale: str = "0.01") -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal(scale)))


def load_h1_bars(runtime: Path) -> dict[str, list[dict[str, Any]]]:
    bars_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for path in (runtime / "finam_market_data_cache/bars").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("interval") != "TIME_FRAME_H1":
            continue
        symbol = str(data.get("symbol") or "")
        parsed: list[dict[str, Any]] = []
        for bar in data.get("bars") or []:
            close = _dec(bar.get("close"))
            high = _dec(bar.get("high"))
            low = _dec(bar.get("low"))
            if close is None or high is None or low is None:
                continue
            parsed.append({"time": _dt(str(bar["time"])), "close": close, "high": high, "low": low})
        if not symbol or len(parsed) < 2:
            continue
        old = bars_by_symbol.get(symbol)
        if old is None or parsed[-1]["time"] > old[-1]["time"] or len(parsed) > len(old):
            bars_by_symbol[symbol] = parsed
    return bars_by_symbol


def event_mentions(runtime: Path, since: datetime) -> Counter[str]:
    counter: Counter[str] = Counter()
    path = runtime / "arena_event_candidates.jsonl"
    if not path.exists():
        return counter
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            ts = _dt(str(item.get("timestamp") or item.get("published_at")))
        except Exception:  # noqa: BLE001
            continue
        if ts >= since and item.get("symbol"):
            counter[str(item["symbol"])] += 1
    return counter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=RUNTIME)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--medium-high-multiplier", type=Decimal, default=Decimal("1.5"))
    parser.add_argument("--base-notional", type=Decimal, default=Decimal("250000"))
    parser.add_argument("--output-dir", type=Path, default=RUNTIME / "reports")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=args.days)
    bars_by_symbol = load_h1_bars(args.runtime)
    mentions = event_mentions(args.runtime, since)

    rows: list[dict[str, Any]] = []
    for symbol, bars in bars_by_symbol.items():
        window = [bar for bar in bars if bar["time"] >= since]
        if len(window) < 2:
            continue
        start = window[0]["close"]
        latest = window[-1]["close"]
        high = max(bar["high"] for bar in window)
        low = min(bar["low"] for bar in window)
        ret = _pct(start, latest)
        rows.append(
            {
                "symbol": symbol,
                "from": window[0]["time"].isoformat(),
                "to": window[-1]["time"].isoformat(),
                "return_pct": _payload(ret),
                "mfe_pct": _payload(_pct(start, high)),
                "mae_pct": _payload(_pct(start, low)),
                "pnl_base_rub": _payload(args.base_notional * ret / Decimal("100")),
                "pnl_medium_high_rub": _payload(args.base_notional * args.medium_high_multiplier * ret / Decimal("100")),
                "event_mentions": mentions.get(symbol, 0),
            }
        )

    rows.sort(key=lambda item: (item["return_pct"] is not None, item["return_pct"]), reverse=True)
    report = {
        "generated_at": now.isoformat(),
        "period_days": args.days,
        "symbols": len(rows),
        "positive": sum(1 for row in rows if (row.get("return_pct") or 0) > 0),
        "negative": sum(1 for row in rows if (row.get("return_pct") or 0) <= 0),
        "top_10": rows[:10],
        "bottom_10": rows[-10:],
        "event_focus": sorted(
            [row for row in rows if row.get("event_mentions")],
            key=lambda item: (item["event_mentions"], item["return_pct"] or -999),
            reverse=True,
        )[:10],
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    json_path = args.output_dir / f"arena_missed_opportunities_{stamp}.json"
    md_path = args.output_dir / f"arena_missed_opportunities_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [f"# Arena missed opportunities {stamp}", f"Period: last {args.days} days", "", "## Top 10"]
    for row in report["top_10"]:
        lines.append(
            f"- {row['symbol']}: {row['return_pct']}%, medium-high PnL {row['pnl_medium_high_rub']} RUB, "
            f"MFE {row['mfe_pct']}%, MAE {row['mae_pct']}%, events {row['event_mentions']}"
        )
    lines.append("\n## Bottom 10")
    for row in report["bottom_10"]:
        lines.append(
            f"- {row['symbol']}: {row['return_pct']}%, medium-high PnL {row['pnl_medium_high_rub']} RUB, "
            f"MFE {row['mfe_pct']}%, MAE {row['mae_pct']}%, events {row['event_mentions']}"
        )
    lines.append("\n## Event focus")
    for row in report["event_focus"]:
        lines.append(f"- {row['symbol']}: events {row['event_mentions']}, return {row['return_pct']}%, medium-high PnL {row['pnl_medium_high_rub']} RUB")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({"status": "OK", "json_path": str(json_path), "md_path": str(md_path), "summary": {k: report[k] for k in ("symbols", "positive", "negative")}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
