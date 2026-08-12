"""Safe public command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

from finam_trading_bot.config import runtime_config_status
from finam_trading_bot.paper import PaperBroker
from finam_trading_bot.risk import OrderProposal, RiskConfig
from finam_trading_bot.safety import LiveTradingBlocked, PAPER_MODE, is_placeholder, trading_mode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finam-trading-bot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="Validate local configuration without network calls.")
    doctor.add_argument("--json", action="store_true")
    demo = subparsers.add_parser("paper-demo", help="Run a deterministic local paper-trading example.")
    demo.add_argument("--json", action="store_true")
    return parser


def doctor() -> dict[str, object]:
    try:
        status = runtime_config_status()
    except LiveTradingBlocked as exc:
        return {"status": "ERROR", "network_calls": False, "broker_mutations": False, "error": str(exc)}

    mode = str(status["trading_mode"])
    account_id = str(os.environ.get("FINAM_ACCOUNT_ID") or "")
    errors: list[str] = []
    if mode == "live":
        token = status["finam_token"]
        account = status["finam_account_id"]
        if not isinstance(token, dict) or not token.get("configured"):
            errors.append("FINAM_TOKEN is required in live mode")
        if not isinstance(account, dict) or not account.get("configured") or is_placeholder(account_id):
            errors.append("a non-placeholder FINAM_ACCOUNT_ID is required in live mode")
    return {
        "status": "OK" if not errors else "NO_TRADE",
        "network_calls": False,
        "broker_mutations": False,
        "config": status,
        "errors": errors,
    }


def paper_demo() -> dict[str, object]:
    if trading_mode() != PAPER_MODE:
        raise LiveTradingBlocked("paper-demo requires TRADING_MODE=paper")
    broker = PaperBroker(
        cash=100_000.0,
        risk_config=RiskConfig(max_order_value=10_000.0, max_position_value=25_000.0, allowed_tickers={"DEMO"}),
    )
    order = OrderProposal(ticker="DEMO", side="buy", quantity=10, price=100.0)
    result = broker.submit(order)
    return {
        "status": "OK" if result.accepted else "REJECTED",
        "mode": "paper",
        "network_calls": False,
        "broker_mutations": False,
        "result": asdict(result),
        "cash": broker.cash,
        "positions": broker.positions,
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        payload = doctor() if args.command == "doctor" else paper_demo()
    except LiveTradingBlocked as exc:
        payload = {"status": "ERROR", "network_calls": False, "broker_mutations": False, "error": str(exc)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"{payload['status']}: mode={payload.get('mode') or payload.get('config', {}).get('trading_mode', 'unknown')}")
        for error in payload.get("errors", []):
            print(f"- {error}")
        if payload.get("error"):
            print(f"- {payload['error']}")
    return 0 if payload["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
