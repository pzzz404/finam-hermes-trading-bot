#!/usr/bin/env python3
"""Send the Arena Telegram report from trusted runtime env files.

Hermes terminal sandbox no longer passes Hermes-managed credentials such as
TELEGRAM_BOT_TOKEN through terminal.env_passthrough. This wrapper keeps the
Arena report command stable for Hermes by loading the repo-owned runtime env
explicitly before calling hermes_operator.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILES = (
    Path.home() / ".config" / "finam-hermes-trading-bot" / "runtime.env",
    Path.home() / ".config" / "hermes" / ".env",
)
REQUIRED_ENV = ("TELEGRAM_BOT_TOKEN",)
CHAT_ENV = ("FINAM_H4_TELEGRAM_CHAT_ID", "H4_MONITOR_TELEGRAM_CHAT_ID", "TELEGRAM_HOME_CHANNEL")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", action="append", default=[], help="Runtime env file to load; later files win.")
    parser.add_argument("--dry-run", action="store_true", help="Build the Telegram payload but do not send it.")
    parser.add_argument("--full-json", action="store_true", help="Keep full arena-status JSON in dry-run mode.")
    args = parser.parse_args()

    env_files = tuple(Path(item).expanduser() for item in args.env_file) or DEFAULT_ENV_FILES
    env = load_env_files(env_files)
    env.update(os.environ)

    missing = missing_required_env(env)
    if missing:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "command": "arena-telegram-report",
                    "telegram_delivery": "failed",
                    "reason": "missing_required_env",
                    "missing_env": missing,
                    "env_files_checked": [str(path) for path in env_files],
                    "safety": {"read_only": True, "trading_mutations": False, "policy_write": False},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1

    command = [sys.executable, str(ROOT / "scripts" / "hermes_operator.py"), "arena-status"]
    if args.dry_run:
        command.append("--dry-run")
    else:
        command.append("--send-telegram")
    if args.full_json:
        command.append("--full-json")

    result = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    payload = _parse_json(stdout)
    if isinstance(payload, dict):
        payload["wrapper_command"] = "arena-telegram-report"
        payload["env_files_loaded"] = [str(path) for path in env_files if path.exists()]
        markup = payload.get("telegram_reply_markup")
        if isinstance(markup, dict):
            rows = markup.get("inline_keyboard")
            if isinstance(rows, list):
                payload["reply_markup_buttons"] = sum(len(row) for row in rows if isinstance(row, list))
        if stderr:
            payload["stderr"] = stderr
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        if stdout:
            print(stdout)
        if stderr:
            print(stderr, file=sys.stderr)
    return result.returncode


def load_env_files(paths: tuple[Path, ...]) -> dict[str, str]:
    env: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def missing_required_env(env: dict[str, Any]) -> list[str]:
    missing = [key for key in REQUIRED_ENV if not str(env.get(key) or "").strip()]
    if not any(str(env.get(key) or "").strip() for key in CHAT_ENV):
        missing.append("FINAM_H4_TELEGRAM_CHAT_ID|H4_MONITOR_TELEGRAM_CHAT_ID|TELEGRAM_HOME_CHANNEL")
    return missing


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
