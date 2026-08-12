#!/usr/bin/env python3
"""Deterministic Arena confirmation text handler for a Hermes gateway."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


CONFIRM_RE = re.compile(r"^\s*(CONFIRM_ARENA_[A-Z_]+\b.+?)\s*$", re.IGNORECASE)
ACCOUNT_ID_PATTERN = r"([A-Z0-9._-]+)"
ARENA_CONFIRM_RE = re.compile(
    rf"^CONFIRM_ARENA_(?:BUY|SELL|EXIT)\s+([A-Z0-9._-]+@[A-Z0-9._-]+)\s+{ACCOUNT_ID_PATTERN}$",
    re.IGNORECASE,
)
ARENA_REPLACE_RE = re.compile(
    rf"^CONFIRM_ARENA_REPLACE\s+([A-Z0-9._-]+@[A-Z0-9._-]+)\s*->\s*([A-Z0-9._-]+@[A-Z0-9._-]+)\s+{ACCOUNT_ID_PATTERN}$",
    re.IGNORECASE,
)
ARENA_BUY_OVERRIDE_RE = re.compile(
    rf"^CONFIRM_ARENA_BUY_OVERRIDE\s+([A-Z0-9._-]+@[A-Z0-9._-]+)\s+{ACCOUNT_ID_PATTERN}$",
    re.IGNORECASE,
)
ARENA_RECOVER_RE = re.compile(
    rf"^CONFIRM_ARENA_RECOVER\s+([A-Z0-9._-]+@[A-Z0-9._-]+)\s+{ACCOUNT_ID_PATTERN}$",
    re.IGNORECASE,
)
DEFAULT_ENV_FILES = (
    Path.home() / ".config" / "hermes" / ".env",
    Path.home() / ".config" / "finam-hermes-trading-bot" / "runtime.env",
)
DEFAULT_EVENT_LOG = Path.home() / ".local" / "state" / "hermes" / "arena-confirm-events.jsonl"


@dataclass(frozen=True)
class ArenaConfirmationResult:
    handled: bool
    authorized: bool
    command: list[str]
    returncode: int | None
    event: dict[str, object]


def handle_arena_confirmation_text(
    text: str,
    chat_id: str | int,
    authorized_chats: set[str],
    *,
    event_log: Path = DEFAULT_EVENT_LOG,
    env_files: tuple[Path, ...] = DEFAULT_ENV_FILES,
    runner=subprocess.run,
) -> ArenaConfirmationResult:
    match = CONFIRM_RE.match(text or "")
    base_event = {"ts": utc_now(), "chat_id": str(chat_id), "text": text}
    if not match:
        return ArenaConfirmationResult(False, False, [], None, {**base_event, "event": "ignored"})
    if str(chat_id) not in authorized_chats:
        event = {**base_event, "event": "unauthorized"}
        append_event(event_log, event)
        return ArenaConfirmationResult(True, False, [], None, event)

    env = load_env_files(env_files)
    env.update(os.environ)
    confirmation = match.group(1)
    command = arena_confirmation_command(confirmation)
    if not command:
        event = {**base_event, "event": "unsupported", "confirmation": confirmation}
        append_event(event_log, event)
        return ArenaConfirmationResult(True, True, [], None, event)
    result = runner(command, text=True, capture_output=True, check=False, env=env)
    event = {
        **base_event,
        "event": "executed",
        "command": command,
        "returncode": result.returncode,
        "stdout": sanitize(result.stdout),
        "stderr": sanitize(result.stderr),
    }
    append_event(event_log, event)
    return ArenaConfirmationResult(True, True, command, result.returncode, event)


def arena_confirmation_command(confirmation: str) -> list[str]:
    value = str(confirmation or "").strip()
    if ARENA_CONFIRM_RE.fullmatch(value) or ARENA_REPLACE_RE.fullmatch(value):
        return ["arena-confirm", "--live", "--confirmation", value]
    match = ARENA_BUY_OVERRIDE_RE.fullmatch(value)
    if match:
        _symbol, account_id = match.groups()
        return ["arena-run", "--account", account_id, "--live", "--confirmation", value]
    match = ARENA_RECOVER_RE.fullmatch(value)
    if match:
        symbol, account_id = match.groups()
        return [
            "arena-recover-protection",
            "--account",
            account_id,
            "--symbol",
            symbol.upper(),
            "--live",
            "--confirmation",
            value,
        ]
    return []


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


def append_event(path: Path, event: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def sanitize(value: str) -> str:
    return " ".join((value or "").replace("\n", " ").split())[:240]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
