"""Interpret authoritative fields from Kalshi's read-only market records."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any


def timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value)
        if date.tzinfo is None:
            return None
        return (date - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class MarketSettlement:
    ticker: str
    result: str
    yes_payout_cents: float
    settlement_ts_ms: int
    close_ts_ms: int | None = None


def read_settlement(market: dict[str, Any]) -> MarketSettlement | None:
    """Require terminal status and an authoritative payout for a $1 contract."""
    if market.get("status") not in ("finalized", "settled"):
        return None
    if market.get("is_provisional") is True:
        return None
    ticker, result = market.get("ticker"), market.get("result")
    settled_at = timestamp_ms(market.get("settlement_ts"))
    if not isinstance(ticker, str) or not ticker or settled_at is None:
        return None
    if result not in ("yes", "no", "scalar"):
        return None
    try:
        notional = Decimal(str(market.get("notional_value_dollars", "1")))
        if notional != 1:
            return None
        value = market.get("settlement_value_dollars")
        if value is None or value == "":
            if result == "scalar":
                return None
            payout = Decimal(1 if result == "yes" else 0)
        else:
            payout = Decimal(str(value))
        if not payout.is_finite() or not 0 <= payout <= 1:
            return None
        if (result == "yes" and payout != 1) or (result == "no" and payout != 0):
            return None
    except InvalidOperation:
        return None
    return MarketSettlement(
        ticker, result, float(payout * 100), settled_at, timestamp_ms(market.get("close_time"))
    )
