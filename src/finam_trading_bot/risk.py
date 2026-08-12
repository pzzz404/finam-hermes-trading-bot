"""Risk controls for simulated and future broker orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RiskViolation(str, Enum):
    TICKER_NOT_ALLOWED = "ticker_not_allowed"
    INVALID_ORDER = "invalid_order"
    ORDER_VALUE_LIMIT = "order_value_limit"
    POSITION_VALUE_LIMIT = "position_value_limit"
    DAILY_LOSS_LIMIT = "daily_loss_limit"


@dataclass(frozen=True)
class RiskConfig:
    max_order_value: float = 10_000.0
    max_position_value: float = 50_000.0
    max_daily_loss: float = 5_000.0
    allowed_tickers: set[str] = field(default_factory=set)
    allow_short: bool = False


@dataclass(frozen=True)
class OrderProposal:
    ticker: str
    side: str
    quantity: int
    price: float

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    order_value: float


def validate_order(
    proposal: OrderProposal,
    config: RiskConfig,
    *,
    current_position_value: float,
    realized_pnl_today: float,
) -> RiskDecision:
    """Validate an order proposal before paper or real execution."""
    if proposal.quantity <= 0 or proposal.price <= 0 or proposal.side not in {"buy", "sell"}:
        return _reject(RiskViolation.INVALID_ORDER, proposal.notional)

    ticker = proposal.ticker.upper()
    if config.allowed_tickers and ticker not in {item.upper() for item in config.allowed_tickers}:
        return _reject(RiskViolation.TICKER_NOT_ALLOWED, proposal.notional)

    if realized_pnl_today <= -abs(config.max_daily_loss):
        return _reject(RiskViolation.DAILY_LOSS_LIMIT, proposal.notional)

    if proposal.notional > config.max_order_value:
        return _reject(RiskViolation.ORDER_VALUE_LIMIT, proposal.notional)

    projected_position = current_position_value + proposal.notional if proposal.side == "buy" else current_position_value
    if projected_position > config.max_position_value:
        return _reject(RiskViolation.POSITION_VALUE_LIMIT, proposal.notional)

    return RiskDecision(allowed=True, reason="ok", order_value=proposal.notional)


def _reject(violation: RiskViolation, order_value: float) -> RiskDecision:
    return RiskDecision(allowed=False, reason=violation.value, order_value=order_value)
