#!/usr/bin/env python3
"""Read-only Finam runtime healthcheck for the host trading worker."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.client import FinamClient  # noqa: E402
from finam_trading_bot.config import get_finam_account_id, get_finam_token  # noqa: E402


def main() -> int:
    result: dict[str, Any] = {
        "status": "OK",
        "checks": {},
        "errors": [],
    }

    try:
        secret = get_finam_token()
        result["checks"]["finam_token"] = {"ok": True, "length": len(secret)}
    except Exception as exc:  # noqa: BLE001 - healthcheck reports all failures.
        result["checks"]["finam_token"] = {"ok": False}
        result["errors"].append(f"FINAM_TOKEN: {exc}")
        return _finish(result, "NO_TRADE")

    account_id = get_finam_account_id()
    result["checks"]["finam_account_id"] = {
        "ok": bool(account_id),
        "length": len(account_id or ""),
    }
    if not account_id:
        result["errors"].append("FINAM_ACCOUNT_ID is not set")
        return _finish(result, "NO_TRADE")

    client = FinamClient()
    try:
        jwt = client.create_session(secret)
        result["checks"]["jwt"] = {"ok": True, "length": len(jwt)}
    except Exception as exc:  # noqa: BLE001
        result["checks"]["jwt"] = {"ok": False}
        result["errors"].append(f"Finam session: {exc}")
        return _finish(result, "NO_TRADE")

    try:
        details = client.session_details(jwt)
        readonly = details.get("readonly")
        account_ids = details.get("account_ids") if isinstance(details.get("account_ids"), list) else []
        result["checks"]["session_details"] = {
            "ok": True,
            "readonly": readonly,
            "account_id_present": account_id in account_ids if account_ids else None,
        }
        if readonly is not False:
            result["errors"].append(f"Finam session readonly is not false: {readonly!r}")
    except Exception as exc:  # noqa: BLE001
        result["checks"]["session_details"] = {"ok": False}
        result["errors"].append(f"Finam session details: {exc}")
        return _finish(result, "NO_TRADE")

    try:
        account = client.get_account(jwt, account_id)
        result["checks"]["account"] = {
            "ok": True,
            "status": account.get("status"),
            "positions_count": len(account.get("positions") or []),
        }
        if account.get("status") != "ACCOUNT_ACTIVE":
            result["errors"].append(f"Account is not active: {account.get('status')!r}")
    except Exception as exc:  # noqa: BLE001
        result["checks"]["account"] = {"ok": False}
        result["errors"].append(f"Finam account: {exc}")
        return _finish(result, "NO_TRADE")

    try:
        orders = client.orders(jwt, account_id)
        result["checks"]["orders"] = {
            "ok": True,
            "orders_count": len(orders.get("orders") or orders.get("items") or []),
        }
    except Exception as exc:  # noqa: BLE001
        result["checks"]["orders"] = {"ok": False}
        result["errors"].append(f"Finam orders: {exc}")
        return _finish(result, "NO_TRADE")

    start = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
    end = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        quote = client.last_quote(jwt, "PLZL@MISX")
        bars = client.bars(jwt, "PLZL@MISX", interval="TIME_FRAME_H4", start_time=start, end_time=end)
        result["checks"]["market_data"] = {
            "ok": True,
            "quote_keys": sorted(quote.keys()),
            "bars_count": len(bars.get("bars") or []),
        }
    except Exception as exc:  # noqa: BLE001
        result["checks"]["market_data"] = {"ok": False}
        result["errors"].append(f"Finam market data: {exc}")
        return _finish(result, "NO_TRADE")

    status = "NO_TRADE" if result["errors"] else "OK"
    return _finish(result, status)


def _finish(result: dict[str, Any], status: str) -> int:
    result["status"] = status
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if status in {"OK", "DEGRADED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
