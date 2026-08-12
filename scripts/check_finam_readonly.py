#!/usr/bin/env python3
"""Read-only Finam API check.

The script never prints the FINAM_TOKEN value or JWT value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.client import FinamClient
from finam_trading_bot.config import get_finam_token


def _extract_accounts(details: dict[str, Any]) -> list[dict[str, Any]]:
    account_ids = details.get("account_ids")
    if isinstance(account_ids, list):
        return [{"account_id": item} for item in account_ids if isinstance(item, str)]
    for key in ("accounts", "Accounts"):
        value = details.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _safe_account(account: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "account_id",
        "accountId",
        "clientId",
        "type",
        "status",
        "market",
        "currency",
        "exchange",
    }
    return {key: value for key, value in account.items() if key in allowed}


def main() -> None:
    secret = get_finam_token()
    client = FinamClient()
    jwt = client.create_session(secret)
    print("Finam JWT received:", bool(jwt))
    details = client.session_details(jwt)
    accounts = _extract_accounts(details)
    print("accounts_count:", len(accounts))
    print(json.dumps([_safe_account(item) for item in accounts], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
