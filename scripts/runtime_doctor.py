#!/usr/bin/env python3
"""Safe runtime diagnostics for the Finam report and supervised trading layer."""

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
from finam_trading_bot.client import FinamClient  # noqa: E402


DEFAULT_SAFETY_STATE_PATH = ROOT / "data" / "runtime" / "finam_h4_safety_state.json"
DEFAULT_OUTBOX_ROOT = ROOT / "data" / "runtime" / "report_outbox"
REQUIRED_ENV = ("FINAM_TOKEN", "FINAM_ARENA_API", "TELEGRAM_BOT_TOKEN", "FINAM_H4_TELEGRAM_CHAT_ID")
OPTIONAL_ENV = (
    "FINAM_ACCOUNT_ID",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "FINAM_EXPECTED_PROXY",
)
FINAM_DIRECT_HOSTS = ("finam.ru", ".finam.ru", "api.finam.ru", "www.finam.ru")
DEFAULT_ARENA_BASE_URL = "https://arena.finam.ru"
DEFAULT_ARENA_ACCOUNT_IDS = ("DEMO-RU", "DEMO-US", "DEMO-AI")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--skip-network", action="store_true", help="Only validate local env/files.")
    parser.add_argument("--outbox-dir", default=str(DEFAULT_OUTBOX_ROOT))
    parser.add_argument("--safety-state", default=str(DEFAULT_SAFETY_STATE_PATH))
    args = parser.parse_args()

    result = run_diagnostics(
        env=os.environ,
        skip_network=args.skip_network,
        outbox_root=Path(args.outbox_dir),
        safety_state_path=Path(args.safety_state),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_human_summary(result))
    return 0 if result["status"] in {"OK", "DEGRADED"} else 2


def run_diagnostics(
    *,
    env: Any,
    skip_network: bool,
    outbox_root: Path,
    safety_state_path: Path,
    opener: Any = urllib.request.urlopen,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "OK", "checks": {}, "errors": [], "warnings": []}
    _check_env(result, env)
    _check_proxy(result, env)
    _check_safety_state(result, safety_state_path)
    _check_outbox(result, outbox_root)

    if not skip_network:
        _check_finam(result, env)
        _check_arena(result, env)
        _check_telegram(result, env, opener=opener)
        _check_finam_usage(result, env)

    if result["errors"]:
        result["status"] = "NO_TRADE"
    elif result["warnings"]:
        result["status"] = "DEGRADED"
    return result


def _check_env(result: dict[str, Any], env: Any) -> None:
    checks: dict[str, Any] = {}
    for key in REQUIRED_ENV:
        value = str(env.get(key) or "")
        checks[key] = {"ok": bool(value), "length": len(value)}
        if not value:
            result["errors"].append(f"{key} is not set")
    for key in OPTIONAL_ENV:
        value = str(env.get(key) or "")
        checks[key] = {"ok": bool(value), "length": len(value)}
    result["checks"]["env"] = checks


def _check_proxy(result: dict[str, Any], env: Any) -> None:
    http_proxy = str(env.get("HTTP_PROXY") or env.get("http_proxy") or "")
    https_proxy = str(env.get("HTTPS_PROXY") or env.get("https_proxy") or "")
    all_proxy = str(env.get("ALL_PROXY") or env.get("all_proxy") or "")
    telegram_proxy = str(env.get("TELEGRAM_PROXY") or "")
    proxies = {
        "HTTP_PROXY": _proxy_public(http_proxy),
        "HTTPS_PROXY": _proxy_public(https_proxy),
        "ALL_PROXY": _proxy_public(all_proxy),
        "TELEGRAM_PROXY": _proxy_public(telegram_proxy),
    }
    no_proxy = str(env.get("NO_PROXY") or env.get("no_proxy") or "")
    finam_direct = {host: _host_bypassed_by_no_proxy(host, no_proxy) for host in FINAM_DIRECT_HOSTS}
    proxy_values = {"HTTP_PROXY": http_proxy, "HTTPS_PROXY": https_proxy, "ALL_PROXY": all_proxy}
    expected_proxy = str(env.get("FINAM_EXPECTED_PROXY") or "")
    proxy_configured = any(proxy_values.values())
    proxy_exact = {
        key: not expected_proxy or not value or value == expected_proxy
        for key, value in proxy_values.items()
    }
    finam_route_ok = not proxy_configured or all(finam_direct.values())
    proxy_ok = all(proxy_exact.values()) and finam_route_ok
    result["checks"]["proxy"] = {
        "ok": proxy_ok,
        "values": proxies,
        "expected_proxy": _proxy_public(expected_proxy),
        "proxy_configured": proxy_configured,
        "proxy_exact": proxy_exact,
        "no_proxy_hosts": _no_proxy_entries(no_proxy),
        "finam_direct": finam_direct,
        "route_matrix": {
            "telegram": {"mode": "environment_default", "ok": True},
            "finam": {"mode": "direct_via_no_proxy_if_proxy_configured", "ok": finam_route_ok, "hosts": finam_direct},
        },
    }
    for key, ok in proxy_exact.items():
        if not ok:
            result["errors"].append(f"{key} must match FINAM_EXPECTED_PROXY")
    if not finam_route_ok:
        result["errors"].append("Finam hosts must bypass proxy/VPN via NO_PROXY")
    if telegram_proxy and https_proxy and telegram_proxy != https_proxy:
        result["warnings"].append("TELEGRAM_PROXY differs from HTTPS_PROXY")


def _check_safety_state(result: dict[str, Any], path: Path) -> None:
    if not path.exists():
        result["checks"]["safety_state"] = {"ok": True, "exists": False, "halt_new_buys": False}
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        result["checks"]["safety_state"] = {"ok": False, "exists": True, "path": str(path)}
        result["errors"].append("trade safety state is unreadable")
        return
    halt = isinstance(data, dict) and data.get("halt_new_buys") is True
    result["checks"]["safety_state"] = {
        "ok": not halt,
        "exists": True,
        "halt_new_buys": halt,
        "status": data.get("status") if isinstance(data, dict) else "INVALID",
    }
    if halt:
        result["errors"].append("trade safety state blocks new buys")


def _check_outbox(result: dict[str, Any], root: Path) -> None:
    pending = h4_monitor_notify.latest_sendable_outbox_run(root)
    count = 0
    if root.exists():
        count = sum(1 for item in root.iterdir() if item.is_dir())
    result["checks"]["outbox"] = {"ok": pending is None, "path": str(root), "runs_count": count, "latest_sendable": pending}
    if pending is not None:
        result["warnings"].append(f"report outbox has undelivered run: {pending}")


def _check_finam(result: dict[str, Any], env: Any) -> None:
    secret = str(env.get("FINAM_TOKEN") or "").strip()
    account_id = str(env.get("FINAM_ACCOUNT_ID") or "").strip()
    arena_mode = _arena_mode_enabled(env)
    if not secret:
        result["checks"]["finam"] = {"ok": False}
        result["errors"].append("FINAM_TOKEN is not set")
        return
    if not account_id:
        result["checks"]["finam"] = {"ok": False}
        if arena_mode:
            result["checks"]["finam"] = {"ok": False, "fatal": False, "arena_mode": True, "skipped": True}
        else:
            result["errors"].append("FINAM_ACCOUNT_ID is not set")
        return
    client = FinamClient()
    try:
        jwt = client.create_session(secret)
        details = client.session_details(jwt)
        account = client.get_account(jwt, account_id)
        orders = client.orders(jwt, account_id)
    except Exception as exc:
        result["checks"]["finam"] = {"ok": False, "fatal": not arena_mode, "arena_mode": arena_mode}
        message = f"Finam API: {exc}"
        if arena_mode:
            result["warnings"].append(f"{message}; ignored for Arena-first runtime")
        else:
            result["errors"].append(message)
        return
    result["checks"]["finam"] = {
        "ok": account.get("status") == "ACCOUNT_ACTIVE",
        "account_status": account.get("status"),
        "readonly": details.get("readonly"),
        "orders_count": len(orders.get("orders") or orders.get("items") or []),
    }
    if account.get("status") != "ACCOUNT_ACTIVE":
        message = f"Finam account is not active: {account.get('status')!r}"
        if arena_mode:
            result["warnings"].append(f"{message}; ignored for Arena-first runtime")
        else:
            result["errors"].append(message)


def _arena_mode_enabled(env: Any) -> bool:
    return bool(str(env.get("FINAM_ARENA_API") or "").strip())


def _check_arena(result: dict[str, Any], env: Any) -> None:
    secret = str(env.get("FINAM_ARENA_API") or "").strip()
    if not secret:
        result["checks"]["arena"] = {"ok": False}
        result["errors"].append("FINAM_ARENA_API is not set")
        return
    base_url = str(env.get("FINAM_ARENA_BASE_URL") or DEFAULT_ARENA_BASE_URL).strip() or DEFAULT_ARENA_BASE_URL
    account_ids = _arena_account_ids(env)
    client = FinamClient(base_url=base_url)
    try:
        jwt = client.create_session(secret)
        accounts = [client.get_account(jwt, account_id) for account_id in account_ids]
    except Exception as exc:
        result["checks"]["arena"] = {"ok": False, "base_url": base_url, "account_ids": account_ids}
        result["errors"].append(f"Arena API: {exc}")
        return

    account_statuses = {account_id: _arena_account_status(account) for account_id, account in zip(account_ids, accounts, strict=False)}
    active = all(status == "ACCOUNT_ACTIVE" for status in account_statuses.values())
    result["checks"]["arena"] = {
        "ok": active,
        "base_url": base_url,
        "account_ids": account_ids,
        "account_statuses": account_statuses,
    }
    if not active:
        result["errors"].append(f"Arena account is not active: {account_statuses}")


def _check_finam_usage(result: dict[str, Any], env: Any) -> None:
    secret = str(env.get("FINAM_TOKEN") or "").strip()
    if not secret:
        result["checks"]["finam_usage"] = {"ok": False, "configured": False}
        return
    client = FinamClient()
    try:
        jwt = client.create_session(secret)
        usage = client.usage(jwt)
    except Exception as exc:  # noqa: BLE001 - usage endpoint is informational.
        result["checks"]["finam_usage"] = {"ok": False, "configured": True, "error": str(exc)}
        return
    quotas = usage.get("quotas") if isinstance(usage.get("quotas"), list) else []
    result["checks"]["finam_usage"] = {"ok": True, "configured": True, "quotas": quotas}


def _check_telegram(result: dict[str, Any], env: Any, *, opener: Any) -> None:
    token = str(env.get("TELEGRAM_BOT_TOKEN") or "")
    chat_id = str(env.get("FINAM_H4_TELEGRAM_CHAT_ID") or env.get("TELEGRAM_HOME_CHANNEL") or "")
    if not token or not chat_id:
        result["checks"]["telegram"] = {"ok": False}
        return
    try:
        get_me = _telegram_api(token, "getMe", {}, opener=opener)
        get_chat = _telegram_api(token, "getChat", {"chat_id": chat_id}, opener=opener)
    except Exception as exc:
        result["checks"]["telegram"] = {"ok": False, "chat_id_present": bool(chat_id)}
        result["errors"].append(f"Telegram API: {exc}")
        return
    result["checks"]["telegram"] = {
        "ok": bool(get_me.get("ok")) and bool(get_chat.get("ok")),
        "chat_id_present": bool(chat_id),
        "bot_ok": bool(get_me.get("ok")),
        "chat_ok": bool(get_chat.get("ok")),
    }
    if not result["checks"]["telegram"]["ok"]:
        result["errors"].append("Telegram API getMe/getChat did not return ok")


def _telegram_api(token: str, method: str, payload: dict[str, str], *, opener: Any) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode("utf-8") if payload else None
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data, method="POST")
    with opener(request, timeout=10) as response:
        parsed = json.loads(response.read().decode("utf-8", errors="replace"))
    return parsed if isinstance(parsed, dict) else {"ok": False}


def _proxy_public(value: str) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme:
        return value
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


def _no_proxy_entries(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _host_bypassed_by_no_proxy(host: str, no_proxy: str) -> bool:
    normalized_host = host.lower().strip(".")
    for entry in _no_proxy_entries(no_proxy):
        normalized_entry = entry.lstrip(".")
        if entry == "*":
            return True
        if normalized_host == normalized_entry:
            return True
        if entry.startswith(".") and normalized_host.endswith("." + normalized_entry):
            return True
    return False


def _arena_account_ids(env: Any) -> list[str]:
    raw = str(env.get("FINAM_ARENA_ACCOUNT_IDS") or "").strip()
    if raw:
        parsed = [item.strip() for item in raw.split(",") if item.strip()]
        if parsed:
            return parsed
    return list(DEFAULT_ARENA_ACCOUNT_IDS)


def _arena_account_status(account: dict[str, Any]) -> str | None:
    status = account.get("status")
    if isinstance(status, str) and status:
        return status
    nested = account.get("account") if isinstance(account.get("account"), dict) else {}
    nested_status = nested.get("status")
    if isinstance(nested_status, str) and nested_status:
        return nested_status
    if account:
        return "ACCOUNT_ACTIVE"
    return None


def _human_summary(result: dict[str, Any]) -> str:
    lines = [f"status: {result['status']}"]
    for item in result.get("errors", []):
        lines.append(f"ERROR: {item}")
    for item in result.get("warnings", []):
        lines.append(f"WARN: {item}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
