"""Small Finam REST client.

Read-only methods are used by the scheduled monitor. Mutating methods are thin
endpoint wrappers and must stay behind the guarded execution/operator layer.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from finam_trading_bot.redaction import redact_text
from finam_trading_bot.safety import assert_live_mutation_allowed


class FinamClientError(RuntimeError):
    """Raised when Finam API communication fails."""


class FinamHTTPError(FinamClientError):
    """Raised when Finam returns a non-2xx HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        path: str,
        detail: str = "",
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds


DEFAULT_RATE_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "runtime" / "finam_api_rate_limit.json"
DEFAULT_API_RATE_LIMIT_PER_MINUTE = 180
DEFAULT_429_RETRIES = 2


@dataclass(frozen=True)
class FinamClient:
    base_url: str = "https://api.finam.ru"
    timeout: int = 30

    def create_session(self, secret: str) -> str:
        data = self._post_json("/v1/sessions", {"secret": secret})
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise FinamClientError("Finam session response did not contain a token")
        return token

    def session_details(self, token: str) -> dict[str, Any]:
        data = self._post_json("/v1/sessions/details", {"token": token})
        if not isinstance(data, dict):
            raise FinamClientError("Finam details response was not an object")
        return data

    def get_account(self, token: str, account_id: str) -> dict[str, Any]:
        return self._get_json(token, f"/v1/accounts/{self._quote(account_id)}")

    def trades(
        self,
        token: str,
        account_id: str,
        *,
        limit: int | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        return self._get_json(
            token,
            f"/v1/accounts/{self._quote(account_id)}/trades",
            self._interval_query(limit=limit, start_time=start_time, end_time=end_time),
        )

    def transactions(
        self,
        token: str,
        account_id: str,
        *,
        limit: int | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, Any]:
        return self._get_json(
            token,
            f"/v1/accounts/{self._quote(account_id)}/transactions",
            self._interval_query(limit=limit, start_time=start_time, end_time=end_time),
        )

    def orders(self, token: str, account_id: str) -> dict[str, Any]:
        return self._get_json(token, f"/v1/accounts/{self._quote(account_id)}/orders")

    def get_order(self, token: str, account_id: str, order_id: str) -> dict[str, Any]:
        return self._get_json(token, f"/v1/accounts/{self._quote(account_id)}/orders/{self._quote(order_id)}")

    def last_quote(self, token: str, symbol: str) -> dict[str, Any]:
        return self._get_json(token, f"/v1/instruments/{self._quote(symbol)}/quotes/latest")

    def asset(self, token: str, symbol: str, *, account_id: str) -> dict[str, Any]:
        return self._get_json(token, f"/v1/assets/{self._quote(symbol)}", {"account_id": account_id})

    def asset_params(self, token: str, symbol: str, *, account_id: str) -> dict[str, Any]:
        return self._get_json(token, f"/v1/assets/{self._quote(symbol)}/params", {"account_id": account_id})

    def order_book(self, token: str, symbol: str, *, depth: int | None = None) -> dict[str, Any]:
        data = self._get_json(token, f"/v1/instruments/{self._quote(symbol)}/orderbook")
        if depth is not None:
            orderbook = data.get("orderbook")
            if isinstance(orderbook, dict) and isinstance(orderbook.get("rows"), list):
                data = dict(data)
                data["orderbook"] = dict(orderbook)
                data["orderbook"]["rows"] = orderbook["rows"][:depth]
        return data

    def bars(
        self,
        token: str,
        symbol: str,
        *,
        interval: str,
        start_time: str,
        end_time: str,
    ) -> dict[str, Any]:
        return self._get_json(
            token,
            f"/v1/instruments/{self._quote(symbol)}/bars",
            {
                "timeframe": self._normalize_timeframe(interval),
                "interval.start_time": start_time,
                "interval.end_time": end_time,
            },
        )

    def latest_trades(self, token: str, symbol: str, *, limit: int | None = None) -> dict[str, Any]:
        data = self._get_json(token, f"/v1/instruments/{self._quote(symbol)}/trades/latest")
        if limit is not None and isinstance(data.get("trades"), list):
            data = dict(data)
            data["trades"] = data["trades"][:limit]
        return data

    def assets(self, token: str) -> dict[str, Any]:
        return self._get_json(token, "/v1/assets")

    def usage(self, token: str) -> dict[str, Any]:
        return self._get_json(token, "/v1/usage")

    def place_order(self, token: str, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert_live_mutation_allowed(account_id=account_id)
        return self._post_json_auth(token, f"/v1/accounts/{self._quote(account_id)}/orders", payload)

    def place_sltp_order(self, token: str, account_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert_live_mutation_allowed(account_id=account_id)
        return self._post_json_auth(token, f"/v1/accounts/{self._quote(account_id)}/sltp-orders", payload)

    def cancel_order(self, token: str, account_id: str, order_id: str) -> dict[str, Any]:
        assert_live_mutation_allowed(account_id=account_id)
        return self._delete_json_auth(token, f"/v1/accounts/{self._quote(account_id)}/orders/{self._quote(order_id)}")

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        return self._open_json(request, path)

    def _get_json(
        self,
        token: str,
        path: str,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        request = Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        return self._open_json(request, path)

    def _post_json_auth(self, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        return self._open_json(request, path)

    def _delete_json_auth(self, token: str, path: str) -> dict[str, Any]:
        request = Request(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="DELETE",
        )
        return self._open_json(request, path)

    def _open_json(self, request: Request, path: str) -> dict[str, Any]:
        max_retries = _max_429_retries(request)
        for attempt in range(max_retries + 1):
            try:
                _wait_for_finam_api_budget(request)
                return self._open_json_once(request, path)
            except FinamHTTPError as exc:
                if exc.status_code != 429 or attempt >= max_retries:
                    raise
                time.sleep(_retry_delay_seconds(exc, attempt))
        raise FinamClientError("unreachable Finam retry state")

    def _open_json_once(self, request: Request, path: str) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = _read_error_body(exc)
            retry_after = _retry_after_seconds(exc)
            message = f"Finam HTTP {exc.code} while calling {path}"
            if detail:
                message = f"{message}: {redact_text(detail)}"
            raise FinamHTTPError(
                message,
                status_code=exc.code,
                path=path,
                detail=detail,
                retry_after_seconds=retry_after,
            ) from exc
        except URLError as exc:
            raise FinamClientError(f"Finam connection failed while calling {path}: {redact_text(exc.reason)}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FinamClientError("Finam response was not valid JSON") from exc

        if not isinstance(parsed, dict):
            raise FinamClientError("Finam response was not a JSON object")
        return parsed

    @staticmethod
    def _quote(value: str) -> str:
        return quote(value, safe="")

    @staticmethod
    def _normalize_timeframe(value: str) -> str:
        if value.startswith("INTRADAYCANDLE_TIMEFRAME_"):
            return "TIME_FRAME_" + value.removeprefix("INTRADAYCANDLE_TIMEFRAME_")
        if value.startswith("TIME_FRAME_"):
            return value
        return value

    @staticmethod
    def _interval_query(
        *,
        limit: int | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, str]:
        query: dict[str, str] = {}
        if limit is not None:
            query["limit"] = str(limit)
        if start_time is not None:
            query["interval.start_time"] = start_time
        if end_time is not None:
            query["interval.end_time"] = end_time
        return query


def _read_error_body(exc: HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    return " ".join(raw.split())[:500]


def _wait_for_finam_api_budget(request: Request) -> None:
    if not _is_primary_finam_api_request(request):
        return
    limit = _positive_int_env("FINAM_API_RATE_LIMIT_PER_MINUTE", DEFAULT_API_RATE_LIMIT_PER_MINUTE)
    if limit <= 0:
        return
    state_path = Path(os.environ.get("FINAM_API_RATE_LIMIT_STATE_PATH") or DEFAULT_RATE_STATE_PATH)
    _consume_rate_slot(state_path, limit=limit, window_seconds=60.0)


def _is_primary_finam_api_request(request: Request) -> bool:
    host = urlsplit(request.full_url).netloc.lower()
    return host == "api.finam.ru"


def _consume_rate_slot(path: Path, *, limit: int, window_seconds: float) -> None:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        while True:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            now = time.monotonic()
            handle.seek(0)
            try:
                data = json.load(handle)
            except Exception:
                data = {}
            timestamps = [
                float(item)
                for item in data.get("timestamps", [])
                if isinstance(item, (int, float)) and now - float(item) < window_seconds
            ]
            if len(timestamps) < limit:
                timestamps.append(now)
                handle.seek(0)
                handle.truncate()
                json.dump({"timestamps": timestamps}, handle)
                handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return
            sleep_for = max(0.0, window_seconds - (now - min(timestamps))) + random.uniform(0.05, 0.25)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            time.sleep(sleep_for)


def _max_429_retries(request: Request) -> int:
    method = request.get_method().upper()
    if method not in {"GET"}:
        return 0
    return _positive_int_env("FINAM_HTTP_429_RETRIES", DEFAULT_429_RETRIES)


def _retry_delay_seconds(exc: FinamHTTPError, attempt: int) -> float:
    if exc.retry_after_seconds is not None:
        return max(0.0, exc.retry_after_seconds) + random.uniform(0.05, 0.25)
    return min(20.0, (2**attempt)) + random.uniform(0.1, 0.5)


def _retry_after_seconds(exc: HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value is None:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except ValueError:
        return None


def _positive_int_env(key: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(key) or default))
    except ValueError:
        return default
    return max(0, value)
