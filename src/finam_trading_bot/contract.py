"""Finam Trade API contract helpers.

The broker-facing code should derive quantities, price ticks, and tradability
from Finam instrument metadata instead of hard-coding MOEX assumptions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any


class FinamContractError(RuntimeError):
    """Raised when Finam metadata is missing or does not allow a buy."""


@dataclass(frozen=True)
class FinamInstrumentRules:
    symbol: str
    lot_size: Decimal
    decimals: int
    min_step: Decimal
    price_step: Decimal
    is_tradable: bool | None = None
    longable: str | None = None
    long_initial_margin: Decimal | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, Decimal):
                payload[key] = format(value, "f")
        return payload

    @property
    def broker_buy_allowed(self) -> bool:
        if self.is_tradable is not True:
            return False
        return self.longable == "AVAILABLE"


def rules_from_finam(symbol: str, asset: dict[str, Any], params: dict[str, Any] | None = None) -> FinamInstrumentRules:
    lot_size = _positive_decimal(_first_value_for_key(asset, "lot_size"))
    if lot_size is None:
        raise FinamContractError(f"{symbol}: Finam GetAsset response does not contain positive lot_size")

    decimals = _int_value(_first_value_for_key(asset, "decimals"))
    min_step = _positive_decimal(_first_value_for_key(asset, "min_step"))
    if decimals is None or decimals < 0:
        raise FinamContractError(f"{symbol}: Finam GetAsset response does not contain valid decimals")
    if min_step is None:
        raise FinamContractError(f"{symbol}: Finam GetAsset response does not contain positive min_step")

    price_step = min_step / (Decimal(10) ** decimals)
    if price_step <= 0:
        raise FinamContractError(f"{symbol}: calculated Finam price_step is not positive")

    params = params or {}
    is_tradable = _bool_value(_first_value_for_key(params, "is_tradable"))
    if is_tradable is None:
        is_tradable = _bool_value(_first_value_for_key(params, "tradeable"))
    longable = _string_value(_first_value_for_key(_first_value_for_key(params, "longable"), "value"))

    return FinamInstrumentRules(
        symbol=symbol,
        lot_size=lot_size,
        decimals=decimals,
        min_step=min_step,
        price_step=price_step,
        is_tradable=is_tradable,
        longable=longable,
        long_initial_margin=_decimal(_first_value_for_key(params, "long_initial_margin")),
    )


def fallback_rules(symbol: str, *, lot_size: Decimal, price_step: Decimal = Decimal("0.01")) -> FinamInstrumentRules:
    return FinamInstrumentRules(
        symbol=symbol,
        lot_size=lot_size,
        decimals=max(0, -price_step.normalize().as_tuple().exponent),
        min_step=price_step,
        price_step=price_step,
    )


def assert_broker_buy_allowed(rules: FinamInstrumentRules) -> None:
    if rules.is_tradable is not True:
        raise FinamContractError(f"{rules.symbol}: Finam GetAssetParams reports instrument is not tradable")
    if rules.longable != "AVAILABLE":
        raise FinamContractError(f"{rules.symbol}: Finam GetAssetParams longable is {rules.longable!r}")


def normalize_quantity_to_lot(quantity: Any, rules: FinamInstrumentRules) -> Decimal:
    parsed = _decimal(quantity)
    if parsed is None:
        raise FinamContractError(f"{rules.symbol}: quantity is not a decimal value")
    if parsed <= 0:
        return Decimal("0")
    lots = (parsed / rules.lot_size).to_integral_value(rounding=ROUND_FLOOR)
    return lots * rules.lot_size


def normalize_price(value: Any, rules: FinamInstrumentRules, *, direction: str) -> Decimal:
    parsed = _decimal(value)
    if parsed is None or parsed <= 0:
        raise FinamContractError(f"{rules.symbol}: price is not a positive decimal value")
    if direction == "floor":
        rounding = ROUND_FLOOR
    elif direction == "ceil":
        rounding = ROUND_CEILING
    else:
        raise ValueError("direction must be floor or ceil")
    ticks = (parsed / rules.price_step).to_integral_value(rounding=rounding)
    normalized = ticks * rules.price_step
    quantum = Decimal(1).scaleb(-rules.decimals)
    return normalized.quantize(quantum)


def decimal_payload(value: Any, *, min_scale: int = 0) -> str:
    parsed = _decimal(value)
    if parsed is None:
        return str(value)
    if min_scale > 0:
        quantum = Decimal(1).scaleb(-min_scale)
        parsed = parsed.quantize(quantum)
    return format(parsed, "f")


def _first_value_for_key(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _first_value_for_key(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_value_for_key(item, key)
            if found is not None:
                return found
    return None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, dict):
        if "units" in value or "nanos" in value:
            units = Decimal(str(value.get("units") or "0"))
            nanos = Decimal(str(value.get("nanos") or "0")) / Decimal("1000000000")
            return units + nanos
        value = value.get("value") or value.get("num") or value.get("amount")
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int_value(value: Any) -> int | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return int(parsed)


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
