"""Signal scoring and reason code generation."""

import time
from dataclasses import dataclass
from typing import Any, Literal

from kalshi_edge_engine.engine.fees import (
    estimate_maker_fee_cents,
    estimate_taker_fee_cents,
)
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
    net_edge_yes: float | None  # inside_width_yes - fees - safety_buffer (cents)
    net_edge_no: float | None  # inside_width_no - fees - safety_buffer (cents)
    inside_width_yes: float | None  # cross_yes - bid_yes (market-making room on YES)
    inside_width_no: float | None  # cross_no - bid_no (market-making room on NO)
    bid_yes: float | None  # Best YES bid
    cross_yes: float | None  # Cross price for YES (100 - best_no_bid)
    bid_no: float | None  # Best NO bid
    cross_no: float | None  # Cross price for NO (100 - best_yes_bid)
    bid_sum: float | None  # Structural sum of best bids (best_yes_bid + best_no_bid)
    imbalance_gap: float | None  # Structural gap from par (100 - bid_sum)
    fee_yes: float | None  # Entry fee for YES side (cents)
    fee_no: float | None  # Entry fee for NO side (cents)
    fee_exit_yes: float | None  # Exit fee for YES side (cents, if round_trip=True)
    fee_exit_no: float | None  # Exit fee for NO side (cents, if round_trip=True)
    total_fee_yes: float | None  # Total fee for YES side (entry + exit if applicable)
    total_fee_no: float | None  # Total fee for NO side (entry + exit if applicable)
    fee_mode: str  # Fee mode used ("maker" or "taker")
    round_trip: bool  # Whether round-trip fees were modeled
    liq_yes: int  # Liquidity at best YES bid (contracts)
    liq_no: int  # Liquidity at best NO bid (contracts)
    top_liquidity: int  # Size at best bid level across both sides (contracts)
    timestamp_ms: int  # When signal was computed


def score_signal(
    ticker: str,
    orderbook: dict[str, Any],
    fee_mode: Literal["maker", "taker"] = "maker",  # Fee mode (maker or taker)
    contracts_for_fee: int = 1,  # Number of contracts for fee calculation
    round_trip: bool = False,  # If True, model both entry and exit fees
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
     4. Compute dynamic Kalshi fees based on bid prices for each side
     5. Compute net edge (inside_width - total_fees - safety_buffer)
        - If round_trip=False: total_fees = entry_fee only
        - If round_trip=True: total_fees = entry_fee + exit_fee (taker at cross price)
     6. Check eligibility:
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
        fee_mode: Fee mode ("maker" or "taker"). Default "maker".
        contracts_for_fee: Number of contracts for fee calculation. Default 1.
        round_trip: If True, model both entry and exit fees (exit uses taker at cross).
                    If False, model only entry fee. Default False.
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

    # Select fee function based on mode
    fee_func = estimate_maker_fee_cents if fee_mode == "maker" else estimate_taker_fee_cents

    # Compute entry fees for each side (based on bid price)
    fee_yes: float | None = None
    fee_no: float | None = None
    if bid_yes is not None:
        fee_yes = fee_func(bid_yes, contracts_for_fee)
    if bid_no is not None:
        fee_no = fee_func(bid_no, contracts_for_fee)

    # Compute exit fees if round_trip=True (use taker rate at cross price)
    fee_exit_yes: float | None = None
    fee_exit_no: float | None = None
    if round_trip:
        # Exit fee uses cross price (where we'd hit to exit), not entry bid
        if cross_yes is not None:
            fee_exit_yes = estimate_taker_fee_cents(cross_yes, contracts_for_fee)
        if cross_no is not None:
            fee_exit_no = estimate_taker_fee_cents(cross_no, contracts_for_fee)

    # Calculate total fees per side
    total_fee_yes: float | None = None
    total_fee_no: float | None = None
    if fee_yes is not None:
        total_fee_yes = fee_yes + (fee_exit_yes if fee_exit_yes is not None else 0.0)
    if fee_no is not None:
        total_fee_no = fee_no + (fee_exit_no if fee_exit_no is not None else 0.0)

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

        # Determine eligibility: usable + meets min_liquidity + (if round_trip, requires cross)
        eligible_yes = (
            usable_yes and liq_yes >= min_liquidity and (not round_trip or cross_yes is not None)
        )
        eligible_no = (
            usable_no and liq_no >= min_liquidity and (not round_trip or cross_no is not None)
        )

        # Check if we have any eligible sides
        if not eligible_yes and not eligible_no:
            # No eligible sides; if usable but illiquid -> LOW_LIQUIDITY, else SPREAD_TOO_SMALL
            reason_code = LOW_LIQUIDITY if usable_yes or usable_no else SPREAD_TOO_SMALL
        else:
            # Calculate net edges only for eligible sides
            if eligible_yes and inside_width_yes is not None and total_fee_yes is not None:
                net_edge_yes = inside_width_yes - total_fee_yes - safety_buffer
            if eligible_no and inside_width_no is not None and total_fee_no is not None:
                net_edge_no = inside_width_no - total_fee_no - safety_buffer

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
        fee_yes=fee_yes,
        fee_no=fee_no,
        fee_exit_yes=fee_exit_yes,
        fee_exit_no=fee_exit_no,
        total_fee_yes=total_fee_yes,
        total_fee_no=total_fee_no,
        fee_mode=fee_mode,
        round_trip=round_trip,
        liq_yes=liq_yes,
        liq_no=liq_no,
        top_liquidity=top_liquidity,
        timestamp_ms=now_ms,
    )
