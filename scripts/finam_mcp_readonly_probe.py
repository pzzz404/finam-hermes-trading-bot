#!/usr/bin/env python3
"""Probe a FINAM MCP server without calling broker tools.

The probe performs only MCP discovery (`initialize` and `tools/list`) for stdio
servers, or reads an exported tools JSON file.  It never calls discovered tools
and is intended for pre-integration analytics assessment outside Hermes's live
profile.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.mcp_readonly import build_analytics_assessment  # noqa: E402

SAFE_ENV_KEYS = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tools-json", type=Path, help="JSON file containing a list_tools result or a raw tools list.")
    source.add_argument("--config-json", type=Path, help="MCP server config: stdio command/args or HTTP/SSE url/headers.")
    parser.add_argument("--baseline-json", type=Path, help="Current Trade API/Arena snapshot to compare against.")
    parser.add_argument("--mcp-snapshot-json", type=Path, help="Optional MCP read-only snapshot captured by a separate approved test.")
    args = parser.parse_args()

    if args.tools_json:
        tools = _extract_tools(_read_json(args.tools_json))
        source_name = str(args.tools_json)
    else:
        config = _read_json(args.config_json)
        try:
            tools = discover_tools(config)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        source_name = str(args.config_json)

    baseline = _read_json(args.baseline_json) if args.baseline_json else None
    mcp_snapshot = _read_json(args.mcp_snapshot_json) if args.mcp_snapshot_json else None
    output = build_analytics_assessment(tools, baseline_snapshot=baseline, mcp_snapshot=mcp_snapshot)
    output["source"] = source_name
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


def discover_tools(config: dict[str, Any]) -> list[dict[str, Any]]:
    if config.get("url"):
        return discover_http_sse_tools(config)
    return discover_stdio_tools(config)


def discover_stdio_tools(config: dict[str, Any]) -> list[dict[str, Any]]:
    command = str(config.get("command") or "").strip()
    if not command:
        raise SystemExit("config-json must contain a non-empty command")
    args = [str(item) for item in config.get("args") or []]
    timeout = float(config.get("connect_timeout") or config.get("timeout") or 30)
    env = _probe_env(_resolve_templates(config.get("env") or {}))
    proc = subprocess.Popen(  # noqa: S603 - command is supplied by explicit test config, no shell.
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _initialize_params()})
        _read_response(proc, timeout=timeout)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        response = _read_response(proc, timeout=timeout)
        return _extract_tools(response)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def discover_http_sse_tools(config: dict[str, Any]) -> list[dict[str, Any]]:
    url = str(_resolve_templates(config.get("url") or "")).strip()
    if not url:
        raise SystemExit("config-json url must be non-empty")
    timeout = float(config.get("connect_timeout") or config.get("timeout") or 30)
    use_proxy = bool(config.get("use_proxy", True))
    headers = _resolve_templates(config.get("headers") or {})
    headers = {str(key): str(value) for key, value in headers.items()}
    try:
        return discover_streamable_http_tools(url=url, headers=headers, timeout=timeout, use_proxy=use_proxy)
    except RuntimeError as exc:
        if "missing mcp-session-id" not in str(exc).lower():
            raise
    client = SseMcpClient(url=url, headers=headers, timeout=timeout, use_proxy=use_proxy)
    with client:
        client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _initialize_params()})
        client.read_response(1)
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        return _extract_tools(client.read_response(2))


def discover_streamable_http_tools(
    *,
    url: str,
    headers: dict[str, str],
    timeout: float,
    use_proxy: bool = True,
) -> list[dict[str, Any]]:
    session_headers = open_streamable_http_session(url=url, headers=headers, timeout=timeout, use_proxy=use_proxy)
    _, tools_response = _http_post_json(
        url,
        headers=session_headers,
        timeout=timeout,
        use_proxy=use_proxy,
        payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    if tools_response.get("error"):
        raise RuntimeError(f"MCP error: {tools_response['error']}")
    return _extract_tools(tools_response)


def call_streamable_http_tool(
    *,
    url: str,
    headers: dict[str, str],
    timeout: float,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    use_proxy: bool = True,
) -> dict[str, Any]:
    session_headers = open_streamable_http_session(url=url, headers=headers, timeout=timeout, use_proxy=use_proxy)
    _, response = _http_post_json(
        url,
        headers=session_headers,
        timeout=timeout,
        use_proxy=use_proxy,
        payload={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        },
    )
    if response.get("error"):
        raise RuntimeError(f"MCP error: {response['error']}")
    return response


def open_streamable_http_session(
    *,
    url: str,
    headers: dict[str, str],
    timeout: float,
    use_proxy: bool = True,
) -> dict[str, str]:
    init_headers, init_response = _http_post_json(
        url,
        headers=headers,
        timeout=timeout,
        use_proxy=use_proxy,
        payload={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": _initialize_params()},
    )
    if init_response.get("error"):
        raise RuntimeError(f"MCP error: {init_response['error']}")
    session_id = init_headers.get("mcp-session-id")
    if not session_id:
        raise RuntimeError("missing mcp-session-id")
    session_headers = {**headers, "Mcp-Session-Id": session_id}
    _http_post_json(
        url,
        headers=session_headers,
        timeout=timeout,
        use_proxy=use_proxy,
        payload={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    )
    return session_headers


def _http_post_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    payload: dict[str, Any],
    use_proxy: bool = True,
) -> tuple[Any, dict[str, Any]]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            **headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if not use_proxy else None
        open_request = opener.open if opener else urllib.request.urlopen
        with open_request(request, timeout=timeout) as response:  # noqa: S310 - explicit user-supplied MCP URL.
            raw = response.read().decode("utf-8")
            if not raw:
                return response.headers, {}
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                return response.headers, _json_from_sse(raw)
            return response.headers, json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MCP HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"MCP network error: {exc.reason}") from exc


def _json_from_sse(raw: str) -> dict[str, Any]:
    for block in raw.split("\n\n"):
        data_lines = [line.split(":", 1)[1].lstrip() for line in block.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        return json.loads("\n".join(data_lines))
    return {}


@dataclass
class SseMcpClient:
    url: str
    headers: dict[str, str]
    timeout: float
    use_proxy: bool = True

    def __enter__(self) -> "SseMcpClient":
        request = urllib.request.Request(
            self.url,
            headers={**self.headers, "Accept": "text/event-stream"},
            method="GET",
        )
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if not self.use_proxy else None
        open_request = self.opener.open if self.opener else urllib.request.urlopen
        self.response = open_request(request, timeout=self.timeout)  # noqa: S310 - explicit user-supplied MCP URL.
        self.post_url = self._read_endpoint()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.response.close()

    def send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.post_url,
            data=body,
            headers={
                **self.headers,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        open_request = self.opener.open if self.opener else urllib.request.urlopen
        with open_request(request, timeout=self.timeout) as response:  # noqa: S310 - explicit user-supplied MCP URL.
            response.read()

    def read_response(self, response_id: int) -> dict[str, Any]:
        while True:
            event = self._read_event()
            data = event.get("data", "").strip()
            if not data:
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if payload.get("id") != response_id:
                continue
            if "error" in payload:
                raise RuntimeError(f"MCP error: {payload['error']}")
            return payload

    def _read_endpoint(self) -> str:
        while True:
            event = self._read_event()
            data = event.get("data", "").strip()
            if event.get("event") == "endpoint" and data:
                return urljoin(self.url, data)

    def _read_event(self) -> dict[str, str]:
        event: dict[str, list[str]] = {}
        while True:
            raw = self.response.readline()
            if not raw:
                raise RuntimeError("MCP SSE stream closed before discovery completed")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                return {key: "\n".join(value) for key, value in event.items()}
            if line.startswith(":") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            event.setdefault(key, []).append(value.lstrip())


def _initialize_params() -> dict[str, Any]:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "finam-trading-bot-readonly-probe", "version": "0.1.0"},
    }


def _send(proc: subprocess.Popen, payload: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("MCP process stdin is unavailable")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    proc.stdin.flush()


def _read_response(proc: subprocess.Popen, *, timeout: float) -> dict[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("MCP process stdout is unavailable")
    header = bytearray()
    while b"\r\n\r\n" not in header:
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("Timed out waiting for MCP response header")
        chunk = proc.stdout.read(1)
        if not chunk:
            raise RuntimeError(_stderr_tail(proc) or "MCP server closed stdout")
        header.extend(chunk)
    headers = header.decode("ascii", errors="replace").split("\r\n")
    length = 0
    for line in headers:
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
            break
    if length <= 0:
        raise RuntimeError(f"MCP response missing Content-Length: {headers!r}")
    body = proc.stdout.read(length)
    response = json.loads(body.decode("utf-8"))
    if "error" in response:
        raise RuntimeError(f"MCP error: {response['error']}")
    return response


def _stderr_tail(proc: subprocess.Popen) -> str:
    if proc.stderr is None:
        return ""
    ready, _, _ = select.select([proc.stderr], [], [], 0)
    if not ready:
        return ""
    return proc.stderr.read(4096).decode("utf-8", errors="replace")


def _extract_tools(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        raise SystemExit("tools JSON must be an object or list")
    result = value.get("result") if isinstance(value.get("result"), dict) else value
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise SystemExit("tools JSON must contain a tools list")
    return [item for item in tools if isinstance(item, dict)]


def _probe_env(extra: dict[str, Any]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in SAFE_ENV_KEYS or key.startswith("XDG_")}
    for key, value in extra.items():
        env[str(key)] = str(value)
    return env


def _resolve_templates(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_templates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_templates(item) for item in value]
    if isinstance(value, str):
        value = re.sub(
            r"\$\{urlencode:([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda match: quote(os.environ.get(match.group(1), ""), safe=""),
            value,
        )
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: os.environ.get(match.group(1), ""), value)
    return value


def _read_json(path: Path | None) -> Any:
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    main()
