#!/usr/bin/env python3
"""Print sanitized read-only Finam account snapshot.

This script authenticates, calls only read-only account endpoints, and never
prints FINAM_TOKEN or JWT values.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.client import FinamClient
from finam_trading_bot.config import get_finam_account_id, get_finam_token
from finam_trading_bot.snapshot import build_account_snapshot


def main() -> None:
    client = FinamClient()
    jwt = client.create_session(get_finam_token())
    print("jwt_received:", bool(jwt), "len:", len(jwt) if jwt else 0)
    snapshot = build_account_snapshot(client, jwt, history_limit=10, account_id=get_finam_account_id())
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
