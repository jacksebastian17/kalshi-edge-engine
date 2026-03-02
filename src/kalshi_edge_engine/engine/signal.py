"""Signal scoring and reason code generation."""

import time
from dataclasses import dataclass
from typing import Any

from kalshi_edge_engine.engine.orderbook import (
    get_best_bid_size,
    get_inside_width,
    parse_orderbook,
)

# Signal reason codes (required for every decision)
SPREAD_TOO_SMALL = "SPREAD_TOO_SMALL"
LOW_LIQUIDITY = "LOW_LIQUIDITY"
MARKET_INELIGIBLE = "MARKET_INELIGIBLE"
DATA_STALE = "DATA_STALE"
SIGNAL_VALID = "SIGNAL_VALID"

VALID_REASON_CODES = {
    SPREAD_TOO_SMALL,
    LOW_LIQUIDITY,
    MARKET_INELIGIBLE,
    DATA_STALE,
    SIGNAL_VALID,
}


@dataclass
class Signal:
    """Scored signal output with reason code and edge estimate."""

    ticker: str
    reason_code: str  # One of VALID_REASON_CODES
    net_edge_yes: float | None  # inside_width_yes - fee_estimate - safety_buffer (cents)
    net_edge_no: float | None  # inside_width_no - fee_estimate - safety_buffer (cents)
    inside_width_yes: float | None  # cross_yes - bid_yes (market-making room on YES)
    inside_width_no: float | None  # cross_no - bid_no (market-making room on NO)
    bid_yes: float | None  # Best YES bid
    cross_yes: float | None  # Cross price for YES (100 - best_no_bid)
    bid_no: float | None  # Best NO bid
    cross_no: float | None  # Cross price for NO (100 - best_yes_bid)
    bid_sum: float | None  # Structural sum of best bids (best_yes_bid + best_no_bid)
    imbalance_gap: float | None  # Structural gap from par (100 - bid_sum)
    liq_yes: int  # Liquidity at best YES bid (contracts)
    liq_no: int  # Liquidity at best NO bid (contracts)
    top_liquidity: int  # Size at best bid level across both sides (contracts)
    timestamp_ms: int  # When signal was computed


def score_signal(
    ticker: str,
    orderbook: dict[str, Any],
    fee_estimate: float = 0.5,  # Default: 0.5 cents (50 bps)
    min_edge: float = 0.1,  # Minimum net edge (cents) to consider valid
    min_liquidity: int = 10,  # Minimum contracts at best level per side
    safety_buffer: float = 0.1,  # Safety margin below inside width (cents)
    imbalance_threshold: float = 1.5,  # Minimum structural imbalance gap (cents)
) -> Signal:
    """Score an orderbook snapshot and emit a structured signal with reason code.

     Strategy:
     1. Parse orderbook to extract best bids and cross prices for YES and NO
     2. Calculate inside width on each side (cross - bid)
     3. Compute structural metrics: bid_sum and imbalance_gap
     4. Compute net edge (inside_width - fee_estimate - safety_buffer)
     5. Check eligibility:
         - If either best bid missing: MARKET_INELIGIBLE
         - If bid_sum > 100: MARKET_INELIGIBLE (crossed/distorted)
         - If a side is usable but has insufficient liquidity: LOW_LIQUIDITY
         - If both inside widths <= 1 tick: SPREAD_TOO_SMALL
         - If imbalance_gap < imbalance_threshold: SPREAD_TOO_SMALL
         - If any net_edge >= min_edge and liquidity OK for that side: SIGNAL_VALID
         - Else: SPREAD_TOO_SMALL or LOW_LIQUIDITY

    Args:
        ticker: Market ticker symbol.
        orderbook: Raw orderbook dict from Kalshi API (supports nested or flat shape).
        fee_estimate: Estimated round-trip fees (cents). Default 0.5¢.
        min_edge: Minimum net edge to consider signal valid (cents). Default 0.1¢.
        min_liquidity: Minimum contracts at best bid level per side. Default 10.
        safety_buffer: Safety margin to subtract from inside width (cents). Default 0.1¢.
        imbalance_threshold: Minimum structural imbalance gap in cents. Default 1.5¢.

    Returns:
        Signal with reason_code and computed edges.
    """
    now_ms = int(time.time() * 1000)

    # Parse orderbook
    parsed = parse_orderbook(orderbook)
    bid_yes = parsed.bid_yes.price if parsed.bid_yes else None
    cross_yes = parsed.cross_yes.price if parsed.cross_yes else None
    bid_no = parsed.bid_no.price if parsed.bid_no else None
    cross_no = parsed.cross_no.price if parsed.cross_no else None

    # Track liquidity per side
    liq_yes = get_best_bid_size(parsed.bid_yes)
    liq_no = get_best_bid_size(parsed.bid_no)

    # Calculate inside widths (room to quote between bid and cross)
    inside_width_yes = get_inside_width(bid_yes, cross_yes)
    inside_width_no = get_inside_width(bid_no, cross_no)

    # Determine top liquidity (max across all best levels)
    top_liquidity = max(liq_yes, liq_no)

    # Default: no edge
    net_edge_yes: float | None = None
    net_edge_no: float | None = None
    bid_sum: float | None = None
    imbalance_gap: float | None = None
    reason_code = MARKET_INELIGIBLE
    one_tick = 1.0

    # A) If either best bid missing, market is ineligible
    if bid_yes is None or bid_no is None:
        reason_code = MARKET_INELIGIBLE
    else:
        bid_sum = bid_yes + bid_no
        imbalance_gap = 100.0 - bid_sum

    # Missing structural metrics or crossed structure is ineligible
    if bid_sum is None or bid_sum > 100.0:
        reason_code = MARKET_INELIGIBLE
    elif imbalance_gap is not None and imbalance_gap < imbalance_threshold:
        # Structural imbalance too small
        reason_code = SPREAD_TOO_SMALL
    else:
        # Determine side usability (width > 1 tick)
        usable_yes = inside_width_yes is not None and inside_width_yes > one_tick
        usable_no = inside_width_no is not None and inside_width_no > one_tick

        # Determine eligibility: usable + meets min_liquidity
        eligible_yes = usable_yes and liq_yes >= min_liquidity
        eligible_no = usable_no and liq_no >= min_liquidity

        # Check if we have any eligible sides
        if not eligible_yes and not eligible_no:
            # No eligible sides; if usable but illiquid -> LOW_LIQUIDITY, else SPREAD_TOO_SMALL
            reason_code = LOW_LIQUIDITY if usable_yes or usable_no else SPREAD_TOO_SMALL
        else:
            # Calculate net edges only for eligible sides
            if eligible_yes and inside_width_yes is not None:
                net_edge_yes = inside_width_yes - fee_estimate - safety_buffer
            if eligible_no and inside_width_no is not None:
                net_edge_no = inside_width_no - fee_estimate - safety_buffer

            # Signal valid if any eligible side meets edge threshold
            valid_yes = eligible_yes and net_edge_yes is not None and net_edge_yes >= min_edge
            valid_no = eligible_no and net_edge_no is not None and net_edge_no >= min_edge
            reason_code = SIGNAL_VALID if (valid_yes or valid_no) else SPREAD_TOO_SMALL

    return Signal(
        ticker=ticker,
        reason_code=reason_code,
        net_edge_yes=net_edge_yes,
        net_edge_no=net_edge_no,
        inside_width_yes=inside_width_yes,
        inside_width_no=inside_width_no,
        bid_yes=bid_yes,
        cross_yes=cross_yes,
        bid_no=bid_no,
        cross_no=cross_no,
        bid_sum=bid_sum,
        imbalance_gap=imbalance_gap,
        liq_yes=liq_yes,
        liq_no=liq_no,
        top_liquidity=top_liquidity,
        timestamp_ms=now_ms,
    )
