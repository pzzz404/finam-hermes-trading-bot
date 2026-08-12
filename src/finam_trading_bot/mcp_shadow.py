"""FINAM MCP read-only shadow context for Arena/Hermes.

The shadow layer is intentionally advisory. It never places broker orders and
returns compact summaries instead of raw MCP payloads.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from finam_trading_bot.mcp_readonly import classify_tool


ALLOWED_FINAM_MCP_TOOLS = {
    "get-account",
    "get-accounts-list",
    "get-quote",
    "get-instrument",
    "get-full_quote",
    "search-instruments",
    "get-watchlists",
}

MCP_URL_ENV = "FINAM_MCP_URL"
LEGACY_MCP_URL_ENV = "FINAM_MCP_TOKEN"
SYMBOL_RE = re.compile(r"\b[A-Z0-9][A-Z0-9.\-]{0,15}@[A-Z0-9]{3,6}\b")


@dataclass
class FinamMcpConfig:
    url: str
    timeout: float = 30.0
    use_proxy: bool = False


class FinamMcpClient:
    """Minimal streamable HTTP MCP client for read-only FINAM tools."""

    def __init__(self, config: FinamMcpConfig):
        self.config = config
        self.session_headers: dict[str, str] = {}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if not config.use_proxy else None

    def __enter__(self) -> "FinamMcpClient":
        init_headers, init_response = self._post_json(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _initialize_params()}
        )
        if init_response.get("error"):
            raise RuntimeError(f"MCP error: {_safe_text(init_response['error'], self.config.url)}")
        session_id = init_headers.get("mcp-session-id")
        if not session_id:
            raise RuntimeError("MCP error: missing mcp-session-id")
        self.session_headers = {"Mcp-Session-Id": session_id}
        self._post_json({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.session_headers = {}

    def list_tools(self) -> list[dict[str, Any]]:
        _, response = self._post_json({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        if response.get("error"):
            raise RuntimeError(f"MCP error: {_safe_text(response['error'], self.config.url)}")
        tools = (response.get("result") or {}).get("tools")
        return [item for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if name not in ALLOWED_FINAM_MCP_TOOLS:
            raise RuntimeError(f"MCP tool is not allowlisted: {name}")
        _, response = self._post_json(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments or {})},
            }
        )
        if response.get("error"):
            raise RuntimeError(f"MCP error: {_safe_text(response['error'], self.config.url)}")
        return response

    def _post_json(self, payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.config.url,
            data=body,
            headers={
                **self.session_headers,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            open_request = self._opener.open if self._opener else urllib.request.urlopen
            with open_request(request, timeout=self.config.timeout) as response:  # noqa: S310 - explicit MCP URL.
                raw = response.read().decode("utf-8")
                if not raw:
                    return response.headers, {}
                if "text/event-stream" in response.headers.get("content-type", ""):
                    return response.headers, _json_from_sse(raw)
                return response.headers, json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MCP HTTP {exc.code}: {_safe_text(detail, self.config.url)}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MCP network error: {_safe_text(exc.reason, self.config.url)}") from exc


def config_from_env(env: Mapping[str, str] | None = None) -> FinamMcpConfig | None:
    env = env or os.environ
    url = str(env.get(MCP_URL_ENV) or "").strip()
    if not url:
        legacy = str(env.get(LEGACY_MCP_URL_ENV) or "").strip()
        if legacy.startswith(("http://", "https://")):
            url = legacy
    if not url:
        return None
    return FinamMcpConfig(url=url, timeout=_float_env(env, "FINAM_MCP_TIMEOUT_SECONDS", 30.0), use_proxy=False)


def build_mcp_shadow_review(
    policy: Mapping[str, Any],
    *,
    arena_scan: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    client: Any | None = None,
    max_bytes: int = 8000,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    candidates = _arena_candidates(arena_scan)
    unavailable = _unavailable_candidate_notes(candidates)
    config = config_from_env(env)
    if config is None and client is None:
        return _base_shadow("MCP_UNAVAILABLE", current) | {
            "reason": "FINAM_MCP_URL_not_set",
            "matched_arena_accounts": [],
            "unmatched_mcp_accounts": [],
            "watchlists_summary": {"status": "MCP_UNAVAILABLE"},
            "quote_context": {"status": "MCP_UNAVAILABLE", "symbols": []},
            "portfolio_warnings": [{"type": "mcp_unavailable", "reason": "FINAM_MCP_URL_not_set"}],
            "candidate_context_notes": unavailable,
            "raw_content_omitted": True,
        }
    try:
        if client is None:
            assert config is not None
            with FinamMcpClient(config) as opened:
                output = _build_shadow_with_client(policy, arena_scan=arena_scan, client=opened, current=current)
        else:
            output = _build_shadow_with_client(policy, arena_scan=arena_scan, client=client, current=current)
    except Exception as exc:  # noqa: BLE001 - advisory layer must not break Arena.
        return _base_shadow("MCP_UNAVAILABLE", current) | {
            "reason": _safe_text(exc, config.url if config else ""),
            "matched_arena_accounts": [],
            "unmatched_mcp_accounts": [],
            "watchlists_summary": {"status": "MCP_UNAVAILABLE"},
            "quote_context": {"status": "MCP_UNAVAILABLE", "symbols": []},
            "portfolio_warnings": [{"type": "mcp_unavailable", "reason": "mcp_shadow_exception"}],
            "candidate_context_notes": unavailable,
            "raw_content_omitted": True,
        }
    return _fit_max_bytes(output, max_bytes=max_bytes)


def validate_mcp_tools(tools: list[Mapping[str, Any]]) -> dict[str, Any]:
    classified = [classify_tool(tool) for tool in tools]
    unexpected = [
        {"name": item["name"], "safety": item["safety"], "mutating_reasons": item["mutating_reasons"]}
        for item in classified
        if item["name"] not in ALLOWED_FINAM_MCP_TOOLS or item["safety"] != "read_only_candidate"
    ]
    return {
        "ok": not unexpected,
        "tools_count": len(classified),
        "allowlisted_tools": sorted(item["name"] for item in classified if item["name"] in ALLOWED_FINAM_MCP_TOOLS),
        "unexpected_tools": unexpected,
    }


def _build_shadow_with_client(
    policy: Mapping[str, Any],
    *,
    arena_scan: Mapping[str, Any],
    client: Any,
    current: datetime,
) -> dict[str, Any]:
    tools = client.list_tools()
    validation = validate_mcp_tools(tools)
    if not validation["ok"]:
        return _base_shadow("MCP_UNSAFE_TOOLS", current) | {
            "tool_validation": validation,
            "matched_arena_accounts": [],
            "unmatched_mcp_accounts": [],
            "watchlists_summary": {"status": "SKIPPED_UNSAFE_TOOLS"},
            "quote_context": {"status": "SKIPPED_UNSAFE_TOOLS", "symbols": []},
            "portfolio_warnings": [{"type": "mcp_unsafe_tools", "unexpected_tools": validation["unexpected_tools"]}],
            "candidate_context_notes": _unavailable_candidate_notes(_arena_candidates(arena_scan), reason="mcp_unsafe_tools"),
            "raw_content_omitted": True,
        }
    accounts_payload = _tool_payload(client.call_tool("get-accounts-list"))
    mcp_accounts = _account_records(accounts_payload)
    arena_accounts = _arena_accounts(arena_scan)
    matched, unmatched, account_details = _match_accounts(client, arena_accounts, mcp_accounts)
    watchlists_payload = _tool_payload(client.call_tool("get-watchlists"))
    watchlists = _watchlists_summary(watchlists_payload, policy=policy)
    candidates = _arena_candidates(arena_scan)
    quote_context = _quote_context(client, candidates)
    portfolio_warnings = _portfolio_warnings(arena_accounts, matched, account_details)
    notes = _candidate_notes(candidates, arena_accounts, watchlists, quote_context)
    return _base_shadow("OK", current) | {
        "tool_validation": validation,
        "matched_arena_accounts": matched,
        "unmatched_mcp_accounts": unmatched,
        "watchlists_summary": watchlists,
        "quote_context": quote_context,
        "portfolio_warnings": portfolio_warnings,
        "candidate_context_notes": notes,
        "raw_content_omitted": True,
    }


def _base_shadow(status: str, current: datetime) -> dict[str, Any]:
    return {
        "status": status,
        "fetched_at": current.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "mode": "shadow_report",
        "broker_mutation": False,
        "trading_gate_effect": "none",
    }


def _match_accounts(client: Any, arena_accounts: list[dict[str, Any]], mcp_accounts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    arena_ids = {str(item.get("account_id") or "") for item in arena_accounts}
    by_id = {_account_id(item): item for item in mcp_accounts if _account_id(item)}
    matched: list[dict[str, Any]] = []
    details: dict[str, dict[str, Any]] = {}
    for account in arena_accounts:
        account_id = str(account.get("account_id") or "")
        mcp_record = by_id.get(account_id)
        if not mcp_record:
            continue
        detail_payload = _tool_payload(client.call_tool("get-account", {"account_id": account_id}))
        detail = _first_mapping(detail_payload) or mcp_record
        details[account_id] = detail
        matched.append(
            {
                "account_id": account_id,
                "arena_positions_count": len(account.get("positions") or []),
                "mcp_positions_count": len(_positions(detail)),
                "arena_cash": account.get("cash") or account.get("available_cash"),
                "mcp_cash_present": _first_existing(detail, ("cash", "available_cash", "money")) is not None,
                "mcp_equity_present": _first_existing(detail, ("equity", "portfolio", "portfolio_value")) is not None,
            }
        )
    unmatched = [
        _compact_account_record(item)
        for item in mcp_accounts
        if _account_id(item) and _account_id(item) not in arena_ids
    ]
    return matched, unmatched[:10], details


def _portfolio_warnings(arena_accounts: list[dict[str, Any]], matched: list[dict[str, Any]], details: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    matched_ids = {item["account_id"] for item in matched}
    for account in arena_accounts:
        account_id = str(account.get("account_id") or "")
        if account_id not in matched_ids:
            warnings.append({"type": "arena_account_not_visible_in_mcp", "account_id": account_id})
            continue
        arena_symbols = {_position_symbol(item) for item in account.get("positions") or [] if isinstance(item, dict)}
        mcp_symbols = {_position_symbol(item) for item in _positions(details.get(account_id) or {})}
        if arena_symbols and mcp_symbols and arena_symbols.symmetric_difference(mcp_symbols):
            warnings.append(
                {
                    "type": "position_set_diff",
                    "account_id": account_id,
                    "arena_only_symbols": sorted(arena_symbols.difference(mcp_symbols)),
                    "mcp_only_symbols": sorted(mcp_symbols.difference(arena_symbols)),
                }
            )
    return warnings


def _candidate_notes(
    candidates: list[dict[str, Any]],
    arena_accounts: list[dict[str, Any]],
    watchlists: Mapping[str, Any],
    quote_context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    accounts_by_id = {str(item.get("account_id") or ""): item for item in arena_accounts}
    watch_symbols = set(watchlists.get("symbols") or [])
    quoted_symbols = {str(item.get("symbol") or "") for item in quote_context.get("symbols") or [] if isinstance(item, dict)}
    notes: list[dict[str, Any]] = []
    for candidate in candidates:
        account_id = str(candidate.get("account_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        positions = accounts_by_id.get(account_id, {}).get("positions") or []
        position_symbols = {_position_symbol(item) for item in positions if isinstance(item, dict)}
        already_exposed = symbol in position_symbols
        note = {
            "account_id": account_id,
            "symbol": symbol,
            "already_exposed": already_exposed,
            "new_exposure": not already_exposed,
            "watchlist_context": "in_watchlist" if symbol in watch_symbols else "not_in_watchlist",
            "quote_context": "available" if symbol in quoted_symbols else str(quote_context.get("status") or "unavailable"),
        }
        if len(position_symbols) >= 5:
            note["concentration_warning"] = "many_open_positions"
        notes.append(note)
    return notes


def _unavailable_candidate_notes(candidates: list[dict[str, Any]], *, reason: str = "mcp_unavailable") -> list[dict[str, Any]]:
    return [
        {
            "account_id": str(candidate.get("account_id") or ""),
            "symbol": str(candidate.get("symbol") or "").upper(),
            "mcp_unavailable": True,
            "reason": reason,
        }
        for candidate in candidates
    ]


def _quote_context(client: Any, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    symbols = sorted({str(item.get("symbol") or "").upper() for item in candidates if item.get("symbol")})[:10]
    if not symbols:
        return {"status": "NO_CANDIDATES", "symbols": []}
    try:
        payload = _tool_payload(client.call_tool("get-quote", {"symbols": symbols}))
    except Exception as exc:  # noqa: BLE001
        return {"status": "QUOTE_UNAVAILABLE", "symbols": [{"symbol": symbol, "status": "unavailable"} for symbol in symbols], "error": _safe_text(exc)}
    records = _records(payload)
    by_symbol = {_record_symbol(record): record for record in records if _record_symbol(record)}
    result = []
    for symbol in symbols:
        record = by_symbol.get(symbol) or {}
        result.append(
            {
                "symbol": symbol,
                "status": "available" if record else "missing",
                "last_price": _first_existing(record, ("last_price", "price", "last", "close")),
                "time": _first_existing(record, ("time", "timestamp", "updated_at")),
            }
        )
    return {"status": "OK", "symbols": result}


def _watchlists_summary(payload: Any, *, policy: Mapping[str, Any]) -> dict[str, Any]:
    records = _records(payload)
    symbols = sorted(_symbols(payload))
    universe = {str(symbol).upper() for account in policy.get("accounts") or [] for symbol in (account.get("universe") or []) if isinstance(account, dict)}
    return {
        "status": "OK",
        "lists_count": len(records) if records else (1 if symbols else 0),
        "symbols_count": len(symbols),
        "symbols": symbols[:50],
        "arena_universe_overlap": sorted(set(symbols).intersection(universe))[:50],
    }


def _tool_payload(response: Mapping[str, Any]) -> Any:
    result = response.get("result") or {}
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        parsed: list[Any] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = str(item.get("text") or "")
                parsed.append(_parse_json_text(text))
        if len(parsed) == 1:
            return parsed[0]
        return parsed
    return result


def _parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text_summary": text[:200], "raw_text_omitted": True}


def _account_records(payload: Any) -> list[dict[str, Any]]:
    return [record for record in _records(payload) if _account_id(record)]


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for value in payload for item in _records(value)]
    if not isinstance(payload, dict):
        return []
    for key in ("accounts", "items", "data", "result", "watchlists", "lists", "quotes"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _records(value)
            if nested:
                return nested
    return [payload]


def _first_mapping(payload: Any) -> dict[str, Any] | None:
    records = _records(payload)
    return records[0] if records else payload if isinstance(payload, dict) else None


def _account_id(item: Mapping[str, Any]) -> str | None:
    value = _first_existing(item, ("account_id", "accountId", "id", "account"))
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _compact_account_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "account_id": _account_id(item),
            "type": _first_existing(item, ("type", "account_type")),
            "status": _first_existing(item, ("status", "state")),
            "currency": _first_existing(item, ("currency", "base_currency")),
        }.items()
        if value is not None
    }


def _positions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("positions", "holdings", "securities"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _arena_accounts(arena_scan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [item for item in arena_scan.get("accounts") or [] if isinstance(item, dict)]


def _arena_candidates(arena_scan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [item for item in arena_scan.get("candidates") or [] if isinstance(item, dict) and item.get("symbol")]


def _position_symbol(item: Mapping[str, Any]) -> str:
    return str(_first_existing(item, ("symbol", "security_code", "ticker", "figi")) or "").upper()


def _record_symbol(item: Mapping[str, Any]) -> str:
    return str(_first_existing(item, ("symbol", "security_code", "ticker")) or "").upper()


def _symbols(payload: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, str):
        found.update(match.group(0).upper() for match in SYMBOL_RE.finditer(payload.upper()))
        return found
    if isinstance(payload, list):
        for item in payload:
            found.update(_symbols(item))
        return found
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in {"symbol", "ticker", "security_code"} and isinstance(value, str):
                found.add(value.upper() if "@" in value else value.upper())
            found.update(_symbols(value))
    return found


def _first_existing(item: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item and item.get(key) not in (None, ""):
            return item.get(key)
    return None


def _json_from_sse(raw: str) -> dict[str, Any]:
    for block in raw.split("\n\n"):
        data_lines = [line.split(":", 1)[1].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if data_lines:
            return json.loads("\n".join(data_lines))
    return {}


def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "finam-trading-bot-shadow", "version": "1.0.0"},
    }


def _safe_text(value: Any, secret: str = "") -> str:
    text = str(value)
    if secret:
        text = text.replace(secret, "<redacted>")
    text = re.sub(r"https://mcp\.finam\.ru/[^\s\"']+", "https://mcp.finam.ru/<redacted>", text)
    return text[:500]


def _fit_max_bytes(output: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    max_bytes = max(1000, int(max_bytes))
    encoded = json.dumps(output, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    output["json_bytes"] = len(encoded)
    if len(encoded) <= max_bytes:
        return output
    compact = dict(output)
    if isinstance(compact.get("watchlists_summary"), dict):
        compact["watchlists_summary"] = dict(compact["watchlists_summary"])
        compact["watchlists_summary"].pop("symbols", None)
        compact["watchlists_summary"].pop("arena_universe_overlap", None)
    compact["candidate_context_notes"] = list(compact.get("candidate_context_notes") or [])[:10]
    compact["portfolio_warnings"] = list(compact.get("portfolio_warnings") or [])[:10]
    encoded = json.dumps(compact, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
    compact["json_bytes"] = len(encoded)
    compact["truncated_for_max_bytes"] = True
    return compact


def _float_env(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(str(env.get(key) or default))
    except (TypeError, ValueError):
        return default


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
