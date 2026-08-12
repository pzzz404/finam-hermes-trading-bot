"""Guarded order execution layer.

Default behavior is safe: DRY_RUN=true and ALLOW_ORDERS=false. The executor can
be wired to a real broker submit function later, but will not call it unless all
guards pass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from finam_trading_bot.risk import OrderProposal, RiskConfig, validate_order


CONFIRMATION_PHRASE = "CONFIRM_ORDER"


class OrderNotAllowed(RuntimeError):
    """Raised when an order is blocked by guard rails."""


@dataclass(frozen=True)
class OrderGuardConfig:
    dry_run: bool = True
    allow_orders: bool = False
    confirmation_phrase: str = CONFIRMATION_PHRASE


class GuardedOrderExecutor:
    def __init__(
        self,
        *,
        risk_config: RiskConfig,
        guard_config: OrderGuardConfig | None = None,
        broker_submit: Callable[[OrderProposal], dict[str, Any]] | None = None,
    ) -> None:
        self.risk_config = risk_config
        self.guard_config = guard_config or OrderGuardConfig()
        self.broker_submit = broker_submit

    def submit(
        self,
        proposal: OrderProposal,
        *,
        current_position_value: float = 0.0,
        realized_pnl_today: float = 0.0,
        confirmation: str | None = None,
    ) -> dict[str, Any]:
        decision = validate_order(
            proposal,
            self.risk_config,
            current_position_value=current_position_value,
            realized_pnl_today=realized_pnl_today,
        )
        if not decision.allowed:
            raise OrderNotAllowed(decision.reason)

        planned = {
            "status": "dry_run",
            "reason": "not_sent_to_broker",
            "proposal": asdict(proposal),
            "risk": asdict(decision),
        }
        if self.guard_config.dry_run:
            return planned
        if not self.guard_config.allow_orders:
            raise OrderNotAllowed("allow_orders_false")
        if confirmation != self.guard_config.confirmation_phrase:
            raise OrderNotAllowed("confirmation_required")
        if self.broker_submit is None:
            raise OrderNotAllowed("broker_submit_not_configured")
        return self.broker_submit(proposal)
