"""Read-only account snapshot helpers."""

from __future__ import annotations

from typing import Any, Protocol

from finam_trading_bot.redaction import scrub


class ReadOnlyFinamClient(Protocol):
    def session_details(self, token: str) -> dict[str, Any]: ...

    def get_account(self, token: str, account_id: str) -> dict[str, Any]: ...

    def orders(self, token: str, account_id: str) -> dict[str, Any]: ...

    def trades(self, token: str, account_id: str, *, limit: int | None = None) -> dict[str, Any]: ...

    def transactions(self, token: str, account_id: str, *, limit: int | None = None) -> dict[str, Any]: ...


def extract_first_account_id(details: dict[str, Any]) -> str:
    account_ids = details.get("account_ids")
    if isinstance(account_ids, list):
        for item in account_ids:
            if isinstance(item, str) and item:
                return item

    for key in ("accounts", "Accounts"):
        accounts = details.get(key)
        if not isinstance(accounts, list):
            continue
        for account in accounts:
            if not isinstance(account, dict):
                continue
            for id_key in ("account_id", "accountId", "id"):
                account_id = account.get(id_key)
                if isinstance(account_id, str) and account_id:
                    return account_id

    raise RuntimeError("No Finam account id found in session details")


def build_account_snapshot(
    client: ReadOnlyFinamClient,
    jwt: str,
    *,
    history_limit: int = 10,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Call only read-only Finam endpoints and return scrubbed snapshot data."""
    details = client.session_details(jwt)
    resolved_account_id = account_id or extract_first_account_id(details)

    snapshot = {
        "account_id": resolved_account_id,
        "session_details": details,
        "account": _capture(lambda: client.get_account(jwt, resolved_account_id)),
        "orders": _capture(lambda: client.orders(jwt, resolved_account_id)),
        "trades": _capture(lambda: client.trades(jwt, resolved_account_id, limit=history_limit)),
        "transactions": _capture(lambda: client.transactions(jwt, resolved_account_id, limit=history_limit)),
    }
    return scrub(snapshot)


def _capture(call) -> Any:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - snapshot must preserve partial read-only results.
        return {"error": str(exc)}
