"""Shared safety gates for broker mutations and public demo configuration."""

from __future__ import annotations

import os
from collections.abc import Mapping


TRADING_MODE_ENV = "TRADING_MODE"
PAPER_MODE = "paper"
LIVE_MODE = "live"
PLACEHOLDER_MARKERS = ("demo", "example", "replace_me", "changeme", "your_")


class LiveTradingBlocked(RuntimeError):
    """Raised before a broker mutation when the global live gate is closed."""


def trading_mode(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = str(source.get(TRADING_MODE_ENV) or PAPER_MODE).strip().lower()
    if value not in {PAPER_MODE, LIVE_MODE}:
        raise LiveTradingBlocked(f"{TRADING_MODE_ENV} must be 'paper' or 'live'")
    return value


def is_placeholder(value: str | None) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return not normalized or any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def assert_live_mutation_allowed(
    *,
    account_id: str | None,
    env: Mapping[str, str] | None = None,
) -> None:
    if trading_mode(env) != LIVE_MODE:
        raise LiveTradingBlocked("broker mutation blocked: TRADING_MODE is not live")
    if is_placeholder(account_id):
        raise LiveTradingBlocked("broker mutation blocked: account id is empty or a placeholder")
