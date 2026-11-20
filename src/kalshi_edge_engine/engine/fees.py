"""Versioned paper fee profiles and the legacy whole-cent estimator.

Kalshi fees formula:
    fee = round_up(rate * C * P * (1-P))  in dollars

Where:
    - rate: maker (1.75%) or taker (7%)
    - C: number of contracts
    - P: price in dollars (e.g., 50 cents => 0.50)
    - round_up: ceiling to next cent in dollars

All internal calculations use cents for consistency with the rest of the engine.
"""

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

# Kalshi fee rates (as decimals)
MAKER_RATE = 0.0175  # 1.75%
TAKER_RATE = 0.07  # 7%

SERIES_FEE_VERSION = "kalshi_quadratic_direct_2026_07_v1"
LEGACY_FEE_VERSION = "legacy_whole_cent_v1"
_MAKER_RATES = {
    "quadratic": Decimal("0"),
    "quadratic_with_maker_fees": Decimal("0.0175"),
    "quadratic_with_combo_maker_fees": Decimal("0.035"),
}


@dataclass(frozen=True)
class FeeProfile:
    version: str
    series_ticker: str | None
    event_ticker: str | None
    fee_type: str
    multiplier: Decimal
    source: str
    observed_ts_ms: int | None = None

    @property
    def series_aware(self) -> bool:
        return self.version == SERIES_FEE_VERSION

    def audit(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "series_ticker": self.series_ticker,
            "event_ticker": self.event_ticker,
            "fee_type": self.fee_type,
            "multiplier": str(self.multiplier),
            "source": self.source,
            "observed_ts_ms": self.observed_ts_ms,
        }

    def fee_cents(
        self, price_cents: float, contracts: int, *, liquidity: str, sale: bool = False
    ) -> float:
        if self.version == LEGACY_FEE_VERSION:
            rate = MAKER_RATE if liquidity == "maker" else TAKER_RATE
            return estimate_fee_cents(price_cents, contracts, rate)
        price = Decimal(str(price_cents)) / 100
        rate_decimal = _MAKER_RATES[self.fee_type] if liquidity == "maker" else Decimal("0.07")
        model_fee = (rate_decimal * self.multiplier * contracts * price * (1 - price)).quantize(
            Decimal("0.000001"), rounding=ROUND_CEILING
        )
        revenue = price * contracts * (1 if sale else -1)
        aligned_change = (revenue - model_fee).quantize(Decimal("0.0001"), rounding=ROUND_FLOOR)
        # Single-fill estimate at direct-member precision, before any order-level rebate.
        return float((revenue - aligned_change) * 100)


LEGACY_FEE_PROFILE = FeeProfile(
    LEGACY_FEE_VERSION, None, None, "quadratic_with_maker_fees", Decimal(1), "legacy_assumption"
)


def series_fee_profile(
    series: dict[str, Any], event: dict[str, Any], *, observed_ts_ms: int
) -> FeeProfile | None:
    """Resolve current series metadata and the event's explicit overrides."""
    series_ticker, event_ticker = series.get("ticker"), event.get("event_ticker")
    if not series_ticker or not event_ticker or event.get("series_ticker") != series_ticker:
        return None
    override_type = event.get("fee_type_override")
    override_multiplier = event.get("fee_multiplier_override")
    fee_type = override_type if override_type is not None else series.get("fee_type")
    multiplier = (
        override_multiplier if override_multiplier is not None else series.get("fee_multiplier")
    )
    if not isinstance(fee_type, str) or fee_type not in _MAKER_RATES:
        return None
    try:
        decimal_multiplier = Decimal(str(multiplier))
    except InvalidOperation:
        return None
    if not decimal_multiplier.is_finite() or decimal_multiplier < 0:
        return None
    return FeeProfile(
        SERIES_FEE_VERSION,
        series_ticker,
        event_ticker,
        fee_type,
        decimal_multiplier,
        "kalshi_event_override"
        if override_type is not None or override_multiplier is not None
        else "kalshi_series",
        observed_ts_ms,
    )


def estimate_fee_cents(price_cents: float, contracts: int, rate: float) -> float:
    """Calculate Kalshi fee in cents using their published formula.

    Args:
        price_cents: Market price in cents (e.g., 50.0 for 50¢).
        contracts: Number of contracts.
        rate: Fee rate as decimal (e.g., 0.07 for 7%).

    Returns:
        Fee in cents, rounded up to next cent. Returns 0 when price is 0 or 100
        (where P*(1-P) = 0). Otherwise minimum 1 cent for positive fees.
    """
    # Convert price to dollars
    price_dollars = price_cents / 100.0

    # fee_dollars = rate * contracts * P * (1-P)
    fee_dollars = rate * contracts * price_dollars * (1.0 - price_dollars)

    # If fee is zero or negative, return 0
    if fee_dollars <= 0:
        return 0.0

    # Round up to next cent in dollars, then convert to cents
    # For positive fees, ensure at least 1 cent
    fee_cents = math.ceil(fee_dollars * 100.0)
    return float(max(fee_cents, 1))


def estimate_maker_fee_cents(price_cents: float, contracts: int = 1) -> float:
    """Calculate maker fee in cents (1.75% rate).

    Args:
        price_cents: Market price in cents.
        contracts: Number of contracts (default 1).

    Returns:
        Maker fee in cents, rounded up to next cent.
    """
    return estimate_fee_cents(price_cents, contracts, MAKER_RATE)


def estimate_taker_fee_cents(price_cents: float, contracts: int = 1) -> float:
    """Calculate taker fee in cents (7% rate).

    Args:
        price_cents: Market price in cents.
        contracts: Number of contracts (default 1).

    Returns:
        Taker fee in cents, rounded up to next cent.
    """
    return estimate_fee_cents(price_cents, contracts, TAKER_RATE)
