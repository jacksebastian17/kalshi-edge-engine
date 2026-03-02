"""Kalshi fee calculations based on their published fee schedule.

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

# Kalshi fee rates (as decimals)
MAKER_RATE = 0.0175  # 1.75%
TAKER_RATE = 0.07  # 7%


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
