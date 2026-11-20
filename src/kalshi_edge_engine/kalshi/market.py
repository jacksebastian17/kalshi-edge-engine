"""Interpret authoritative fields from Kalshi's read-only market records."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_ENTRY_OBSERVATION_AGE_MS = 2_000
NONTRADING_STATUSES = frozenset(
    {
        "initialized",
        "inactive",
        "closed",
        "determined",
        "disputed",
        "amended",
        "finalized",
        "settled",
    }
)
FINAL_STATUSES = frozenset({"finalized", "settled"})


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
class MarketObservation:
    ticker: str
    status: str
    requested_ts_ms: int
    observed_ts_ms: int
    close_ts_ms: int | None
    updated_ts_ms: int | None = None
    settlement_ts_ms: int | None = None

    def tradable_at(self, now_ms: int) -> bool:
        return (
            self.status == "active"
            and self.close_ts_ms is not None
            and self.requested_ts_ms <= self.observed_ts_ms <= now_ms < self.close_ts_ms
            and now_ms - self.requested_ts_ms <= MAX_ENTRY_OBSERVATION_AGE_MS
        )

    def can_replace(self, previous: "MarketObservation") -> bool:
        if previous == self:
            return True
        if previous.status in FINAL_STATUSES and self.status not in FINAL_STATUSES:
            return False
        if (
            self.updated_ts_ms is not None
            and previous.updated_ts_ms is not None
            and self.updated_ts_ms < previous.updated_ts_ms
        ):
            return False
        if self.requested_ts_ms < previous.requested_ts_ms and not (
            self.status in NONTRADING_STATUSES and self.observed_ts_ms >= previous.observed_ts_ms
        ):
            return False
        if self.status == "active" and previous.status in NONTRADING_STATUSES:
            newer_exchange_update = (
                self.updated_ts_ms is not None
                and previous.updated_ts_ms is not None
                and self.updated_ts_ms > previous.updated_ts_ms
            )
            moved_close = (
                self.close_ts_ms is not None
                and previous.close_ts_ms is not None
                and self.close_ts_ms > previous.close_ts_ms
            )
            return self.requested_ts_ms > previous.observed_ts_ms and (
                newer_exchange_update or moved_close
            )
        return self.observed_ts_ms >= previous.observed_ts_ms


def read_market_observation(
    market: dict[str, Any], ticker: str, *, requested_ts_ms: int, observed_ts_ms: int
) -> MarketObservation | None:
    status = market.get("status")
    if (
        market.get("ticker") != ticker
        or not isinstance(status, str)
        or (status != "active" and status not in NONTRADING_STATUSES)
        or observed_ts_ms < requested_ts_ms
    ):
        return None
    return MarketObservation(
        ticker,
        status,
        requested_ts_ms,
        observed_ts_ms,
        timestamp_ms(market.get("close_time")),
        timestamp_ms(market.get("updated_time")),
        timestamp_ms(market.get("settlement_ts")),
    )


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
