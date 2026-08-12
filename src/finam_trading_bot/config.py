"""Runtime configuration helpers."""

from __future__ import annotations

import os

from finam_trading_bot.safety import trading_mode


def get_finam_token() -> str:
    token = os.environ.get("FINAM_TOKEN", "")
    if not token:
        raise RuntimeError("FINAM_TOKEN is not set")
    return token


def get_finam_account_id() -> str | None:
    account_id = os.environ.get("FINAM_ACCOUNT_ID", "").strip()
    return account_id or None


def finam_token_status() -> tuple[bool, int]:
    token = os.environ.get("FINAM_TOKEN")
    return bool(token), len(token) if token else 0


def runtime_config_status() -> dict[str, object]:
    """Return value-free local configuration diagnostics."""
    token_ok, token_length = finam_token_status()
    account_id = get_finam_account_id()
    return {
        "trading_mode": trading_mode(),
        "finam_token": {"configured": token_ok, "length": token_length},
        "finam_account_id": {"configured": bool(account_id)},
    }
