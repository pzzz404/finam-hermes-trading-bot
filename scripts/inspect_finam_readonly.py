#!/usr/bin/env python3
"""Print sanitized Finam read-only response shape for diagnostics."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.client import FinamClient
from finam_trading_bot.config import get_finam_token


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower()
            if any(marker in lower for marker in ("token", "secret", "key", "password")):
                result[key] = "[REDACTED]"
            else:
                result[key] = scrub(item)
        return result
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def main() -> None:
    client = FinamClient()
    jwt = client.create_session(get_finam_token())
    details = client.session_details(jwt)
    print("jwt_received:", bool(jwt))
    print("details_type:", type(details).__name__)
    print("details_keys:", sorted(details.keys()))
    print(json.dumps(scrub(details), ensure_ascii=False, indent=2)[:6000])


if __name__ == "__main__":
    main()
