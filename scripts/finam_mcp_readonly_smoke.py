#!/usr/bin/env python3
"""Run one approved FINAM MCP read-only tool call without printing account data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from finam_mcp_readonly_probe import (  # noqa: E402
    _read_json,
    _resolve_templates,
    call_streamable_http_tool,
    discover_tools,
)
from finam_trading_bot.mcp_readonly import classify_tool  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-json", required=True, type=Path)
    parser.add_argument("--tool", default="get-accounts-list")
    parser.add_argument("--arguments-json", default="{}")
    args = parser.parse_args()

    config = _read_json(args.config_json)
    arguments = json.loads(args.arguments_json)
    if not isinstance(arguments, dict):
        raise SystemExit("--arguments-json must decode to an object")

    tools = discover_tools(config)
    selected = next((tool for tool in tools if tool.get("name") == args.tool), None)
    if not selected:
        raise SystemExit(f"tool is not available: {args.tool}")

    classified = classify_tool(selected)
    if classified["safety"] != "read_only_candidate":
        raise SystemExit(f"tool is not approved for read-only smoke: {args.tool}")

    url, headers, timeout, use_proxy = _http_config(config)
    response = call_streamable_http_tool(
        url=url,
        headers=headers,
        timeout=timeout,
        use_proxy=use_proxy,
        tool_name=args.tool,
        arguments=arguments,
    )
    summary = _summarize_tool_response(args.tool, response)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if summary["status"] != "READONLY_CALL_OK":
        raise SystemExit(2)


def _http_config(config: dict[str, Any]) -> tuple[str, dict[str, str], float, bool]:
    url = str(_resolve_templates(config.get("url") or "")).strip()
    if not url:
        raise SystemExit("config-json url must be non-empty")
    headers = _resolve_templates(config.get("headers") or {})
    timeout = float(config.get("connect_timeout") or config.get("timeout") or 30)
    use_proxy = bool(config.get("use_proxy", True))
    return url, {str(key): str(value) for key, value in headers.items()}, timeout, use_proxy


def _summarize_tool_response(tool_name: str, response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result") or {}
    content = result.get("content") or []
    content_types = sorted({str(item.get("type")) for item in content if isinstance(item, dict) and item.get("type")})
    is_error = bool(result.get("isError"))
    return {
        "status": "READONLY_TOOL_ERROR" if is_error else "READONLY_CALL_OK",
        "tool": tool_name,
        "is_error": is_error,
        "content_count": len(content) if isinstance(content, list) else 0,
        "content_types": content_types,
        "error_text": _first_text(content) if is_error else None,
        "raw_content_omitted": True,
    }


def _first_text(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = str(item.get("text") or "")
            return text[:500]
    return None


if __name__ == "__main__":
    main()
