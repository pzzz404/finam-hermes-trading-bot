"""Append-only audit journal for Hermes operator commands."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from finam_trading_bot.redaction import scrub


DEFAULT_JOURNAL_PATH = Path("/tmp/finam-hermes-actions.jsonl")


def journal_path() -> Path:
    configured = os.environ.get("HERMES_OPERATOR_JOURNAL", "").strip()
    return Path(configured) if configured else DEFAULT_JOURNAL_PATH


def write_event(event: dict[str, Any], *, path: Path | None = None) -> Path:
    target = path or journal_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "read_only": True,
        "trading_mutations": False,
        "production_send": False,
    }
    payload.update(scrub(event))
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return target
