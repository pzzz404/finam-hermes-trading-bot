#!/usr/bin/env python3
"""Handle Telegram inline navigation callbacks for Finam Arena views."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import h4_monitor_notify  # noqa: E402
import hermes_operator  # noqa: E402
from finam_trading_bot.arena import load_arena_policy  # noqa: E402


DEFAULT_ARENA_POLICY_PATH = ROOT / "config" / "finam_arena_policy.json"
DEFAULT_OFFSET_PATH = ROOT / "data" / "runtime" / "arena_telegram_callbacks_offset.json"
POLLING_FLAG_ENV = "FINAM_ARENA_CALLBACK_POLLING_ENABLED"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arena-policy", default=str(DEFAULT_ARENA_POLICY_PATH))
    parser.add_argument("--offset-state", default=str(DEFAULT_OFFSET_PATH))
    parser.add_argument("--once", action="store_true", help="Poll Telegram once and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Build actions but do not call Telegram write APIs.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args()
    if not args.once:
        parser.error("Only --once mode is supported")
    output = process_callbacks(
        policy_path=Path(args.arena_policy),
        offset_state=Path(args.offset_state),
        dry_run=bool(args.dry_run),
        limit=args.limit,
        timeout=args.timeout,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output.get("status") != "FAILED" else 1


def process_callbacks(
    *,
    policy_path: Path,
    offset_state: Path,
    dry_run: bool,
    limit: int = 20,
    timeout: int = 10,
    env: Any = os.environ,
    opener: Any = urllib.request.urlopen,
) -> dict[str, Any]:
    token = str(env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return _output("NO_DATA", policy_path, offset_state, dry_run, reason="TELEGRAM_BOT_TOKEN_not_set")
    if str(env.get(POLLING_FLAG_ENV) or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return _output(
            "NO_DATA",
            policy_path,
            offset_state,
            dry_run,
            reason=f"{POLLING_FLAG_ENV}_not_enabled",
        )

    offset = _read_offset(offset_state)
    try:
        updates = _telegram_api(
            token,
            "getUpdates",
            {
                "offset": str(offset),
                "limit": str(max(1, min(limit, 100))),
                "timeout": str(max(0, min(timeout, 50))),
                "allowed_updates": json.dumps(["callback_query"]),
            },
            opener=opener,
        )
    except Exception as exc:  # noqa: BLE001
        return _output("FAILED", policy_path, offset_state, dry_run, reason="telegram_getUpdates_failed", error=str(exc))

    actions: list[dict[str, Any]] = []
    next_offset = offset
    for update in updates.get("result") or []:
        if not isinstance(update, dict):
            continue
        update_id = int(update.get("update_id") or 0)
        next_offset = max(next_offset, update_id + 1)
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            continue
        actions.append(_handle_callback(callback, policy_path=policy_path, token=token, dry_run=dry_run, opener=opener))

    _write_offset(offset_state, next_offset)
    status = "OK" if all(action.get("status") != "FAILED" for action in actions) else "DEGRADED"
    return {
        "status": status,
        "command": "arena-telegram-callbacks",
        "policy_path": str(policy_path),
        "offset_state": str(offset_state),
        "offset": next_offset,
        "updates_count": len(updates.get("result") or []),
        "actions": actions,
        "telegram_delivery": "dry_run" if dry_run else "ok",
        "safety": {"read_only": True, "trading_mutations": False, "policy_write": False},
    }


def _handle_callback(callback: dict[str, Any], *, policy_path: Path, token: str, dry_run: bool, opener: Any) -> dict[str, Any]:
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if not data.startswith("arena:"):
        if not dry_run and callback_id:
            _telegram_api(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": "Неизвестная кнопка"}, opener=opener)
        return {"status": "IGNORED", "callback_data": data, "reason": "unsupported_callback"}

    policy = load_arena_policy(policy_path)
    view = hermes_operator._arena_view_output(policy, policy_path=policy_path, callback=data)
    text = str(view.get("telegram_text") or view.get("reason") or "Arena view unavailable")
    h4_monitor_notify.validate_telegram_html(text)
    markup = view.get("telegram_reply_markup") or h4_monitor_notify.arena_reply_markup()
    action = {
        "status": view.get("status"),
        "callback_data": data,
        "chat_id": str(chat_id) if chat_id is not None else None,
        "message_id": message_id,
        "text_length": len(text),
    }
    if dry_run:
        return action | {"telegram_delivery": "dry_run"}
    try:
        if callback_id:
            _telegram_api(token, "answerCallbackQuery", {"callback_query_id": callback_id}, opener=opener)
        _telegram_api(
            token,
            "editMessageText",
            {
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
                "reply_markup": json.dumps(markup, ensure_ascii=False),
            },
            opener=opener,
        )
    except Exception as exc:  # noqa: BLE001
        return action | {"status": "FAILED", "telegram_delivery": "failed", "error": str(exc)}
    return action | {"telegram_delivery": "ok"}


def _telegram_api(token: str, method: str, payload: dict[str, str], *, opener: Any) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data, method="POST")
    with opener(request, timeout=60) as response:
        parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(parsed, dict) or not parsed.get("ok"):
        description = parsed.get("description") if isinstance(parsed, dict) else "invalid response"
        raise RuntimeError(f"Telegram {method} failed: {description}")
    return parsed


def _read_offset(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0
    except Exception:
        return 0
    try:
        return int(data.get("offset") or 0) if isinstance(data, dict) else 0
    except (TypeError, ValueError):
        return 0


def _write_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": int(offset)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _output(
    status: str,
    policy_path: Path,
    offset_state: Path,
    dry_run: bool,
    *,
    reason: str,
    error: str | None = None,
) -> dict[str, Any]:
    output = {
        "status": status,
        "command": "arena-telegram-callbacks",
        "policy_path": str(policy_path),
        "offset_state": str(offset_state),
        "reason": reason,
        "telegram_delivery": "dry_run" if dry_run else "not_started",
        "safety": {"read_only": True, "trading_mutations": False, "policy_write": False},
    }
    if error:
        output["error"] = error
    return output


if __name__ == "__main__":
    raise SystemExit(main())
