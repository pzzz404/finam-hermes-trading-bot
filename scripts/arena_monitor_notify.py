#!/usr/bin/env python3
"""Build and deliver compact Finam Arena Pulse reports.

This entrypoint is intentionally read-only: it scans Arena accounts, formats the
Telegram pulse, and optionally sends it with inline navigation buttons.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import h4_monitor_notify  # noqa: E402
import hermes_operator  # noqa: E402
from finam_trading_bot.arena import load_arena_policy  # noqa: E402
from finam_trading_bot.redaction import redact_environment_values  # noqa: E402


DEFAULT_ARENA_POLICY_PATH = ROOT / "config" / "finam_arena_policy.json"
ARENA_POLICY_ENV = "FINAM_ARENA_POLICY_PATH"


def main() -> int:
    hermes_operator._load_default_operator_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--arena-policy", default=str(_default_arena_policy_path()))
    parser.add_argument("--once", action="store_true", help="Run one Arena Pulse pass.")
    parser.add_argument("--read-only", action="store_true", help="Document that broker mutations are forbidden.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload and do not send Telegram.")
    parser.add_argument("--full-json", action="store_true", help="Print full dry-run payload including accounts and reply markup.")
    args = parser.parse_args()

    if not args.once:
        parser.error("Only --once mode is supported")
    output = build_arena_pulse_output(Path(args.arena_policy), dry_run=bool(args.dry_run), full_json=bool(args.full_json))
    if args.dry_run and not args.full_json:
        # Diagnostic output is recursively redacted before this sink; covered by sentinel-secret tests.
        # codeql[py/clear-text-logging-sensitive-data]
        print(json.dumps(redact_environment_values(output), ensure_ascii=False, default=str, separators=(",", ":")))
    else:
        # Diagnostic output is recursively redacted before this sink; covered by sentinel-secret tests.
        # codeql[py/clear-text-logging-sensitive-data]
        print(json.dumps(redact_environment_values(output), ensure_ascii=False, default=str, indent=2))
    return 0 if output.get("telegram_delivery") != "failed" else 1


def _default_arena_policy_path() -> Path:
    raw = str(os.environ.get(ARENA_POLICY_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_ARENA_POLICY_PATH


def build_arena_pulse_output(policy_path: Path, *, dry_run: bool, full_json: bool = False) -> dict[str, Any]:
    policy = load_arena_policy(policy_path)
    report = hermes_operator._arena_status_output(policy, policy_path=policy_path, full_json=True)
    text = report["telegram_pulse"]
    h4_monitor_notify.validate_telegram_html(text)
    markup = report["telegram_reply_markup"]
    output = {
        "status": report.get("status"),
        "command": "arena-pulse",
        "policy_path": str(policy_path),
        "telegram_text": text,
        "telegram_reply_markup": markup,
        "telegram_delivery": "dry_run" if dry_run else "pending",
        "accounts": report.get("accounts") or [],
        "source_command": "arena-status",
        "errors": report.get("errors") or [],
        "warnings": report.get("warnings") or [],
        "safety": {
            "read_only": True,
            "trading_mutations": False,
            "policy_write": False,
        },
    }
    if dry_run:
        if full_json:
            return output
        return {
            "status": output.get("status"),
            "command": output.get("command"),
            "policy_path": output.get("policy_path"),
            "telegram_delivery": output.get("telegram_delivery"),
            "telegram_text_chars": len(text),
            "reply_markup_buttons": sum(len(row) for row in markup.get("inline_keyboard", [])),
            "accounts_count": len(output.get("accounts") or []),
            "source_command": output.get("source_command"),
            "errors": output.get("errors") or [],
            "warnings": output.get("warnings") or [],
            "safety": output.get("safety"),
            "full_detail_command": "python scripts/arena_monitor_notify.py --once --dry-run --full-json",
        }
    try:
        h4_monitor_notify.send_telegram_message(text, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001 - systemd should surface delivery failures.
        output["telegram_delivery"] = "failed"
        output["error"] = redact_environment_values(exc)
        return output
    output["telegram_delivery"] = "ok"
    return output


if __name__ == "__main__":
    raise SystemExit(main())
