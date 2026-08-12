"""Paper-trading execution layer.

This module simulates fills and keeps an in-memory audit journal. It never calls
broker order endpoints.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from finam_trading_bot.risk import OrderProposal, RiskConfig, validate_order


@dataclass(frozen=True)
class PaperFill:
    ticker: str
    side: str
    quantity: int
    price: float
    notional: float
    timestamp: str


@dataclass(frozen=True)
class PaperOrderResult:
    accepted: bool
    reason: str
    fill: PaperFill | None


class PaperBroker:
    def __init__(self, *, cash: float, risk_config: RiskConfig) -> None:
        self.cash = cash
        self.risk_config = risk_config
        self.positions: dict[str, int] = {}
        self.journal: list[dict] = []

    def submit(self, proposal: OrderProposal, *, realized_pnl_today: float = 0.0) -> PaperOrderResult:
        ticker = proposal.ticker.upper()
        current_position_value = self.positions.get(ticker, 0) * proposal.price
        decision = validate_order(
            proposal,
            self.risk_config,
            current_position_value=current_position_value,
            realized_pnl_today=realized_pnl_today,
        )
        if not decision.allowed:
            self.journal.append(
                {
                    "mode": "paper",
                    "accepted": False,
                    "reason": decision.reason,
                    "proposal": asdict(proposal),
                }
            )
            return PaperOrderResult(accepted=False, reason=decision.reason, fill=None)

        fill = PaperFill(
            ticker=ticker,
            side=proposal.side,
            quantity=proposal.quantity,
            price=proposal.price,
            notional=proposal.notional,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        signed_quantity = proposal.quantity if proposal.side == "buy" else -proposal.quantity
        signed_cash = -proposal.notional if proposal.side == "buy" else proposal.notional
        self.positions[ticker] = self.positions.get(ticker, 0) + signed_quantity
        if self.positions[ticker] == 0:
            del self.positions[ticker]
        self.cash += signed_cash
        self.journal.append(
            {
                "mode": "paper",
                "accepted": True,
                "reason": "ok",
                "proposal": asdict(proposal),
                "fill": asdict(fill),
            }
        )
        return PaperOrderResult(accepted=True, reason="ok", fill=fill)
