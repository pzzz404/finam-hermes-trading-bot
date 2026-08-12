#!/usr/bin/env python3
"""Draw a Finam Arena portfolio growth chart for the 3 contest accounts.

Read-only: parses existing Arena pulse/executor journal messages and optionally
adds the latest factual equity from `arena-status --full-json`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "runtime" / "arena_portfolio_growth_3_accounts.png"
DEFAULT_CSV = ROOT / "data" / "runtime" / "arena_portfolio_growth_3_accounts.csv"
ACCOUNTS = {"DEMO-RU": "РФ", "DEMO-US": "США", "DEMO-AI": "AI"}
COLORS = {"DEMO-RU": (31, 119, 180), "DEMO-US": (255, 127, 14), "DEMO-AI": (44, 160, 44)}


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _decode_telegram_text(line: str) -> str | None:
    match = re.search(r'"telegram_text"\s*:\s*"(.*)"\s*,?$', line)
    if not match:
        return None
    fragment = '"' + match.group(1).rstrip(',')
    try:
        return str(json.loads(fragment))
    except json.JSONDecodeError:
        return match.group(1).encode("utf-8").decode("unicode_escape", errors="ignore")


def _journal_lines(since: str) -> list[str]:
    cmd = [
        "journalctl",
        "--user",
        "--since",
        since,
        "-u",
        "finam-arena-pulse.service",
        "-u",
        "finam-arena-executor.service",
        "--no-pager",
        "-o",
        "short-iso",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "journalctl failed")
    return proc.stdout.splitlines()


def _parse_journal_points(since: str) -> list[dict[str, Any]]:
    patterns = {
        aid: re.compile(rf"<b>{aid}</b>\s+[^:\n]*:?\s+([0-9][0-9\s]*[.,][0-9]+)\s+RUB\s+\(([-+]?\d+(?:[.,]\d+)?)%\)")
        for aid in ACCOUNTS
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for line in _journal_lines(since):
        if "telegram_text" not in line or not any(aid in line for aid in ACCOUNTS):
            continue
        try:
            timestamp = dt.datetime.fromisoformat(line[:25].strip())
        except ValueError:
            continue
        text = _decode_telegram_text(line)
        if not text:
            continue
        values: dict[str, tuple[float, float]] = {}
        for aid, pattern in patterns.items():
            match = pattern.search(text)
            if match:
                equity = float(match.group(1).replace(" ", "").replace(",", "."))
                pnl_pct = float(match.group(2).replace(",", "."))
                values[aid] = (equity, pnl_pct)
        if len(values) != len(ACCOUNTS):
            continue
        # Dedupe repeated JSON fragments from the same systemd run.
        key = (
            timestamp.replace(second=0, microsecond=0),
            tuple((aid, round(values[aid][0], 2), round(values[aid][1], 4)) for aid in sorted(values)),
        )
        if key in seen:
            continue
        seen.add(key)
        for aid, label in ACCOUNTS.items():
            rows.append(
                {"ts": timestamp, "account_id": aid, "label": label, "equity": values[aid][0], "pnl_pct": values[aid][1], "source": "journal"}
            )
    return rows


def _latest_status_points() -> list[dict[str, Any]]:
    cmd = ["python", "scripts/hermes_operator.py", "arena-status", "--full-json"]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "arena-status failed")
    data = json.loads(proc.stdout)
    timestamp = dt.datetime.now(dt.timezone.utc).astimezone()
    rows: list[dict[str, Any]] = []
    for account in data.get("accounts", []):
        aid = str(account.get("account_id"))
        if aid in ACCOUNTS:
            rows.append(
                {
                    "ts": timestamp,
                    "account_id": aid,
                    "label": ACCOUNTS[aid],
                    "equity": float(account["equity"]),
                    "pnl_pct": float(account["pnl_pct"]),
                    "source": "arena-status",
                }
            )
    return rows


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("timestamp,account_id,label,equity_rub,pnl_pct,source\n")
        for row in rows:
            fh.write(
                f"{row['ts'].isoformat()},{row['account_id']},{row['label']},{row['equity']:.2f},{row['pnl_pct']:.6f},{row['source']}\n"
            )


def _draw(rows: list[dict[str, Any]], out: Path) -> None:
    width, height = 1600, 900
    left, right, top, bottom = 120, 130, 75, 115
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = _font(30, bold=True)
    font = _font(18)
    small = _font(15)
    bold = _font(18, bold=True)

    timestamps = [row["ts"].timestamp() for row in rows]
    t_min, t_max = min(timestamps), max(timestamps)
    y_min = min(float(row["pnl_pct"]) for row in rows)
    y_max = max(float(row["pnl_pct"]) for row in rows)
    y_pad = max(0.20, (y_max - y_min) * 0.14)
    y_min, y_max = min(0.0, y_min - y_pad), max(0.0, y_max + y_pad)

    def xmap(ts: dt.datetime) -> float:
        if t_max == t_min:
            return float(left)
        return left + (ts.timestamp() - t_min) / (t_max - t_min) * (width - left - right)

    def ymap(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * (height - top - bottom)

    # Grid and y labels.
    span = y_max - y_min
    step = 0.5 if span <= 5 else 1.0
    tick = math.floor(y_min / step) * step
    while tick <= y_max + 1e-9:
        y = ymap(tick)
        draw.line((left, y, width - right, y), fill=(220, 220, 220), width=1)
        draw.text((28, y - 10), f"{tick:+.1f}", fill=(70, 70, 70), font=small)
        tick += step

    # X ticks: about 10 evenly spaced observed timestamps.
    unique_ts = sorted({row["ts"] for row in rows})
    stride = max(1, len(unique_ts) // 10)
    for ts in unique_ts[::stride]:
        x = xmap(ts)
        draw.line((x, top, x, height - bottom), fill=(228, 228, 228), width=1)
        draw.multiline_text((x - 28, height - bottom + 12), ts.strftime("%d.%m\n%H:%M"), fill=(75, 75, 75), font=small, align="center")

    zero_y = ymap(0.0)
    draw.line((left, zero_y, width - right, zero_y), fill=(130, 130, 130), width=2)
    draw.rectangle((left, top, width - right, height - bottom), outline=(190, 190, 190), width=2)

    latest_lines: list[str] = []
    for aid, label in ACCOUNTS.items():
        series = [row for row in rows if row["account_id"] == aid]
        points = [(xmap(row["ts"]), ymap(float(row["pnl_pct"]))) for row in series]
        if len(points) > 1:
            draw.line(points, fill=COLORS[aid], width=4, joint="curve")
        for x, y in points:
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=COLORS[aid], outline="white", width=1)
        last = series[-1]
        last_x, last_y = points[-1]
        endpoint = f"{float(last['pnl_pct']):+.2f}%"
        draw.text((last_x + 8, last_y - 9), endpoint, fill=COLORS[aid], font=bold)
        latest_lines.append(f"{aid} {label}: {last['equity']:,.0f} RUB ({endpoint})".replace(",", " "))

    # Legend bottom-left.
    legend_x, legend_y = left + 12, height - bottom - 85
    legend_w, legend_h = 260, 76
    draw.rounded_rectangle((legend_x, legend_y, legend_x + legend_w, legend_y + legend_h), radius=3, fill=(255, 255, 255), outline=(210, 210, 210))
    for index, (aid, label) in enumerate(ACCOUNTS.items()):
        y = legend_y + 10 + index * 22
        last = [row for row in rows if row["account_id"] == aid][-1]
        draw.line((legend_x + 12, y + 9, legend_x + 42, y + 9), fill=COLORS[aid], width=4)
        draw.ellipse((legend_x + 25, y + 5, legend_x + 33, y + 13), fill=COLORS[aid])
        draw.text((legend_x + 52, y), f"{aid} {label} {float(last['pnl_pct']):+.2f}%", fill=(45, 45, 45), font=small)

    # Latest values box top-right.
    box_text = "\n".join(latest_lines)
    bbox = draw.multiline_textbbox((0, 0), box_text, font=font, spacing=3)
    box_w, box_h = bbox[2] - bbox[0] + 24, bbox[3] - bbox[1] + 20
    box_x, box_y = width - right - box_w - 15, top + 12
    draw.rounded_rectangle((box_x, box_y, box_x + box_w, box_y + box_h), radius=6, fill=(255, 255, 255), outline=(205, 205, 205))
    draw.multiline_text((box_x + 12, box_y + 10), box_text, fill=(45, 45, 45), font=font, spacing=3, align="right")

    draw.text((width / 2 - 330, 20), "Динамика портфеля на ФИНАМ Арене", fill=(30, 30, 30), font=title_font)
    draw.text((18, height / 2 - 115), "Доходность от 1 000 000 RUB, %", fill=(65, 65, 65), font=font)
    draw.text((width / 2 - 55, height - 45), "Время (UTC)", fill=(65, 65, 65), font=font)
    since = min(row["ts"] for row in rows).strftime("%d.%m")
    footnote = f"Линии: Arena pulse/executor journal с {since} + последняя фактическая equity из Arena-status. График read-only, без торговых мутаций."
    draw.text((left, height - 18), footnote, fill=(85, 85, 85), font=small)

    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-06-01", help="journalctl --since value")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--no-latest-status", action="store_true", help="Do not append fresh arena-status point")
    args = parser.parse_args()

    rows = _parse_journal_points(args.since)
    if not args.no_latest_status:
        try:
            rows.extend(_latest_status_points())
        except Exception as exc:  # keep historical chart useful when API is degraded
            print(json.dumps({"warning": f"latest arena-status skipped: {exc}"}, ensure_ascii=False))
    if not rows:
        raise SystemExit("no Arena equity points parsed")
    rows.sort(key=lambda row: (row["ts"], row["account_id"]))
    _write_csv(rows, Path(args.csv))
    _draw(rows, Path(args.out))
    latest = {aid: [row for row in rows if row["account_id"] == aid][-1]["pnl_pct"] for aid in ACCOUNTS}
    print(
        json.dumps(
            {
                "status": "OK",
                "png": str(Path(args.out).resolve()),
                "csv": str(Path(args.csv).resolve()),
                "points": len(rows),
                "snapshots": len(rows) // len(ACCOUNTS),
                "from": min(row["ts"] for row in rows).isoformat(),
                "to": max(row["ts"] for row in rows).isoformat(),
                "latest_pct": latest,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
