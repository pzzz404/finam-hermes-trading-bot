"""Read-only FINAM MCP assessment helpers.

This module intentionally does not execute MCP tools.  It classifies discovered
tool metadata and highlights where an MCP server may improve broker-state
analytics before any Hermes/Hermes integration is considered.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from finam_trading_bot.snapshot import scrub


READONLY_CATEGORIES: dict[str, tuple[str, ...]] = {
    "accounts": ("account", "accounts", "portfolio", "balance", "cash", "equity", "счёт", "счет", "счёта", "счета", "баланс"),
    "positions": ("position", "positions", "holding", "holdings", "позиции", "позиция", "количество", "цена входа", "нереализованный"),
    "orders": ("orders", "active_orders", "list_orders", "get_orders", "order_history"),
    "trades": ("trades", "fills", "deals", "executions", "trade_history"),
    "transactions": ("transactions", "operations", "payments", "cashflow"),
    "market_data": ("quote", "quotes", "bars", "candles", "orderbook", "assets", "instruments", "котировки", "котировка", "инструменты", "инструмент", "тикер", "marketdata", "screener"),
    "watchlists": ("watchlist", "watchlists", "favorites", "favourites", "избранное", "избранного", "списки избранного"),
    "availability": ("available", "availability", "margin", "short", "longable", "shortable", "params"),
    "costs": ("commission", "commissions", "fee", "fees", "tariff", "costs"),
    "rejections": ("reject", "rejected", "error", "status", "reason"),
}

MUTATING_PHRASES = (
    "place_order",
    "submit_order",
    "send_order",
    "create_order",
    "cancel_order",
    "replace_order",
    "modify_order",
    "update_order",
    "delete_order",
    "execute_order",
    "market_order",
    "limit_order",
    "stop_order",
    "sltp_order",
    "buy",
    "sell",
    "withdraw",
    "deposit",
    "transfer",
)

MUTATING_TOKENS = {
    "place",
    "submit",
    "send",
    "create",
    "cancel",
    "replace",
    "modify",
    "update",
    "delete",
    "execute",
    "buy",
    "sell",
    "withdraw",
    "deposit",
    "transfer",
}


def classify_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one MCP tool from list_tools metadata."""
    name = str(tool.get("name") or "").strip()
    description = str(tool.get("description") or "").strip()
    text = f"{name} {description}".lower()
    tokens = _tokens(text)
    mutating_reasons = _mutating_reasons(name, text, tokens)
    categories = _categories(text, tokens)
    safety = "mutating_blocked" if mutating_reasons else "read_only_candidate" if categories else "unknown_review"
    return {
        "name": name,
        "description": description,
        "safety": safety,
        "categories": categories,
        "mutating_reasons": mutating_reasons,
    }


def build_tool_safety_report(tools: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return a safety and coverage report for discovered MCP tools."""
    classified = [classify_tool(tool) for tool in tools]
    read_only = [item for item in classified if item["safety"] == "read_only_candidate"]
    blocked = [item for item in classified if item["safety"] == "mutating_blocked"]
    unknown = [item for item in classified if item["safety"] == "unknown_review"]
    coverage = sorted({category for item in read_only for category in item["categories"]})
    required = {"accounts"}
    missing_required = sorted(required.difference(coverage))
    return {
        "status": "READY_FOR_READ_ONLY_TEST" if read_only and not missing_required else "INSUFFICIENT_READ_ONLY_COVERAGE",
        "tools_count": len(classified),
        "read_only_tools": _public_tools(read_only),
        "blocked_mutating_tools": _public_tools(blocked),
        "unknown_tools_requiring_review": _public_tools(unknown),
        "coverage": coverage,
        "missing_required_coverage": missing_required,
        "safety": {
            "read_only_only": False,
            "mutating_tools_available": bool(blocked),
            "tool_calls_executed": False,
            "broker_mutation": False,
        },
    }


def build_analytics_assessment(
    tools: Iterable[Mapping[str, Any]],
    *,
    baseline_snapshot: Mapping[str, Any] | None = None,
    mcp_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assess whether MCP metadata/snapshots can improve Hermes analytics."""
    safety = build_tool_safety_report(tools)
    coverage = set(safety["coverage"])
    improvements = _coverage_improvements(coverage)
    diff: dict[str, Any] | None = None
    if baseline_snapshot is not None or mcp_snapshot is not None:
        diff = compare_snapshot_shape(baseline_snapshot or {}, mcp_snapshot or {})
        if diff["mcp_only_paths"]:
            improvements.append(
                {
                    "area": "new_broker_fields",
                    "impact": "MCP exposes fields not present in the current snapshot; inspect them for attribution and explanations.",
                    "evidence": diff["mcp_only_paths"][:20],
                }
            )
    return {
        "status": _assessment_status(safety, improvements),
        "tool_safety": safety,
        "analytics_improvements": improvements,
        "snapshot_shape_diff": diff,
        "recommendation": _recommendation(safety, improvements),
    }


def compare_snapshot_shape(baseline_snapshot: Mapping[str, Any], mcp_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Compare sanitized field paths without comparing private values."""
    baseline_paths = _leaf_paths(scrub(baseline_snapshot))
    mcp_paths = _leaf_paths(scrub(mcp_snapshot))
    return {
        "baseline_paths_count": len(baseline_paths),
        "mcp_paths_count": len(mcp_paths),
        "shared_paths_count": len(baseline_paths.intersection(mcp_paths)),
        "mcp_only_paths": sorted(mcp_paths.difference(baseline_paths)),
        "baseline_only_paths": sorted(baseline_paths.difference(mcp_paths)),
    }


def _mutating_reasons(name: str, text: str, tokens: set[str]) -> list[str]:
    normalized_name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    reasons: list[str] = []
    for phrase in MUTATING_PHRASES:
        if phrase in normalized_name or phrase in text:
            reasons.append(phrase)
    if "order" in tokens and tokens.intersection(MUTATING_TOKENS):
        reasons.append("order_mutation_token")
    return sorted(set(reasons))


def _categories(text: str, tokens: set[str]) -> list[str]:
    categories: list[str] = []
    for category, markers in READONLY_CATEGORIES.items():
        if any(marker in tokens or marker in text for marker in markers):
            categories.append(category)
    return categories


def _coverage_improvements(coverage: set[str]) -> list[dict[str, Any]]:
    improvements: list[dict[str, Any]] = []
    if {"accounts", "positions", "orders"}.issubset(coverage):
        improvements.append(
            {
                "area": "broker_state_reconciliation",
                "impact": "Hermes can cross-check account state, open positions, and active orders before explaining blocks or clearing safety state.",
                "evidence": ["accounts", "positions", "orders"],
            }
        )
    if {"trades", "transactions"}.issubset(coverage):
        improvements.append(
            {
                "area": "attribution_and_learning",
                "impact": "Fills and cash operations can improve realized PnL, commission estimates, and Arena learning samples.",
                "evidence": ["trades", "transactions"],
            }
        )
    if "availability" in coverage:
        improvements.append(
            {
                "area": "instrument_availability",
                "impact": "Availability metadata may reduce false shortability or instrument-permission blockers if it works for Arena accounts.",
                "evidence": ["availability"],
            }
        )
    if "costs" in coverage:
        improvements.append(
            {
                "area": "cost_aware_decisions",
                "impact": "Commission or fee metadata can tighten breakeven stops and realized PnL explanations.",
                "evidence": ["costs"],
            }
        )
    if "rejections" in coverage:
        improvements.append(
            {
                "area": "failure_explanations",
                "impact": "Broker rejection/status details can make BLOCKED/NO_TRADE explanations more concrete.",
                "evidence": ["rejections"],
            }
        )
    if {"market_data", "watchlists"}.issubset(coverage):
        improvements.append(
            {
                "area": "user_context_and_quotes",
                "impact": "Hermes can answer account-adjacent questions from the user's FinamTrade watchlists and live quote context.",
                "evidence": ["market_data", "watchlists"],
            }
        )
    return improvements


def _assessment_status(safety: Mapping[str, Any], improvements: list[dict[str, Any]]) -> str:
    if safety["status"] != "READY_FOR_READ_ONLY_TEST":
        return "NOT_READY"
    if improvements:
        return "PROMISING_READ_ONLY"
    return "DUPLICATIVE_OR_LOW_VALUE"


def _recommendation(safety: Mapping[str, Any], improvements: list[dict[str, Any]]) -> str:
    if safety["status"] != "READY_FOR_READ_ONLY_TEST":
        return "Do not integrate with Hermes yet; read-only coverage is incomplete."
    if not improvements:
        return "Do not integrate yet; discovered tools do not show a clear analytics improvement."
    if safety["blocked_mutating_tools"]:
        return "Continue only with an adapter allowlist; mutating MCP tools must stay unavailable to Hermes."
    return "Proceed to an isolated read-only adapter test before any Hermes profile change."


def _public_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": item["name"],
            "safety": item["safety"],
            "categories": item["categories"],
            "mutating_reasons": item["mutating_reasons"],
        }
        for item in tools
    ]


def _leaf_paths(value: Any, prefix: str = "") -> set[str]:
    if isinstance(value, Mapping):
        paths: set[str] = set()
        for key, item in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.update(_leaf_paths(item, next_prefix))
        return paths
    if isinstance(value, list):
        paths = {prefix or "[]"}
        for item in value[:3]:
            paths.update(_leaf_paths(item, f"{prefix}[]"))
        return paths
    return {prefix}


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", text.lower()) if token}
