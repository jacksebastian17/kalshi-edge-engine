"""Paper trading module for simulating fills and tracking PnL.

CURRENT STATE:
- build_quote_intents(): Active - Used to create intents from signals
- QuoteIntent dataclass: Active - Core data structure for intents
- simulate_fill(): DEPRECATED - Use RealisticOrderSimulator from paper_sim.py instead
- PaperFill/PaperOutcome: LEGACY - Kept for backwards compatibility with old tests

NEW PATH (RECOMMENDED):
- Use RealisticOrderSimulator from paper_sim.py for realistic fill simulation
- Read from quote_intents/paper_fills/paper_positions tables in PaperStore

SEMANTICS NOTE:
- take_profit is modeled as cents-per-contract threshold
    (`take_profit_cents_per_contract`) in lifecycle logic.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

from kalshi_edge_engine.engine.signal import (
    SIGNAL_VALID,
    Signal,
    compute_intended_inside_quote_price_cents,
)
from kalshi_edge_engine.kalshi.client import KalshiClient

logger = logging.getLogger(__name__)

# Bounded concurrency for paper trading simulations
_paper_trading_semaphore: asyncio.Semaphore | None = None


def _get_paper_semaphore(max_concurrent: int = 5) -> asyncio.Semaphore:
    """Get or create the paper trading semaphore for bounded concurrency."""
    global _paper_trading_semaphore
    if _paper_trading_semaphore is None:
        _paper_trading_semaphore = asyncio.Semaphore(max_concurrent)
    return _paper_trading_semaphore


@dataclass
class QuoteIntent:
    """Representation of a quote intent in paper trading."""

    id: str  # UUID string
    ticker: str
    side: str  # "YES" or "NO"
    quote_price_cents: float
    size_contracts: int
    created_ts_ms: int
    net_edge: float  # net_edge from signal
    inside_width: float  # inside_width from signal
    imbalance_gap: float | None  # imbalance_gap from signal
    fee_mode: str  # "maker" or "taker"
    round_trip: bool  # Whether round-trip fees were modeled
    universe: str = "NBA"
    capturable_edge_to_cross_cents: float | None = None
    net_edge_after_entry_fee_cents: float | None = None
    net_edge_after_round_trip_fee_cents: float | None = None


@dataclass
class PaperFill:
    """Represents a fill event in paper trading."""

    intent_id: str
    filled: bool
    fill_ts_ms: int
    fill_price_cents: float | None  # None if not filled
    time_to_fill_ms: int | None  # None if not filled


@dataclass
class PaperOutcome:
    """Final outcome of a paper trade."""

    intent_id: str
    exit_ts_ms: int
    exit_price_cents: float | None
    total_fees_cents: float
    pnl_cents: float
    pnl_dollars: float
    note: str  # "filled", "timeout", "error"


def build_quote_intents(
    signal: Signal,
    min_edge: float = 0.1,
) -> list[QuoteIntent]:
    """Build quote intents from a signal if it is SIGNAL_VALID.

    Rules:
    - Only if signal.reason_code == SIGNAL_VALID
    - YES intent if net_edge_yes >= min_edge and bid_yes/cross_yes exist
    - NO intent if net_edge_no >= min_edge and bid_no/cross_no exist
    - Quote price = bid + 1 tick (integer cents only) when width >= 2 cents
    - Size = 1 contract
    - Ensure price bounds 1 <= price <= 99

    Args:
        signal: The signal to process.
        min_edge: Minimum net edge threshold.
    Returns:
        List of QuoteIntent objects (0, 1, or 2 items).
    """
    intents: list[QuoteIntent] = []

    # Only process SIGNAL_VALID
    if signal.reason_code != SIGNAL_VALID:
        return intents

    now_ms = int(time.time() * 1000)

    # Check YES side
    yes_quote_price_cents = (
        signal.intended_quote_yes_cents
        if signal.intended_quote_yes_cents is not None
        else compute_intended_inside_quote_price_cents(
            bid_price_cents=signal.bid_yes,
            cross_price_cents=signal.cross_yes,
        )
    )
    if (
        signal.net_edge_yes is not None
        and signal.net_edge_yes >= min_edge
        and signal.inside_width_yes is not None
        and yes_quote_price_cents is not None
    ):
        intent = QuoteIntent(
            id=str(uuid.uuid4()),
            ticker=signal.ticker,
            universe=signal.universe,
            side="YES",
            quote_price_cents=float(yes_quote_price_cents),
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=signal.net_edge_yes,
            inside_width=signal.inside_width_yes,
            imbalance_gap=signal.imbalance_gap,
            fee_mode=signal.fee_mode,
            round_trip=signal.round_trip,
            capturable_edge_to_cross_cents=signal.capturable_edge_to_cross_yes_cents,
            net_edge_after_entry_fee_cents=signal.net_edge_after_entry_fee_yes_cents,
            net_edge_after_round_trip_fee_cents=signal.net_edge_after_round_trip_fee_yes_cents,
        )
        intents.append(intent)
        logger.info(
            "paper_intent_created",
            extra={
                "intent_id": intent.id,
                "ticker": signal.ticker,
                "side": "YES",
                "quote_price": yes_quote_price_cents,
                "net_edge": signal.net_edge_yes,
            },
        )

    # Check NO side
    no_quote_price_cents = (
        signal.intended_quote_no_cents
        if signal.intended_quote_no_cents is not None
        else compute_intended_inside_quote_price_cents(
            bid_price_cents=signal.bid_no,
            cross_price_cents=signal.cross_no,
        )
    )
    if (
        signal.net_edge_no is not None
        and signal.net_edge_no >= min_edge
        and signal.inside_width_no is not None
        and no_quote_price_cents is not None
    ):
        intent = QuoteIntent(
            id=str(uuid.uuid4()),
            ticker=signal.ticker,
            universe=signal.universe,
            side="NO",
            quote_price_cents=float(no_quote_price_cents),
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=signal.net_edge_no,
            inside_width=signal.inside_width_no,
            imbalance_gap=signal.imbalance_gap,
            fee_mode=signal.fee_mode,
            round_trip=signal.round_trip,
            capturable_edge_to_cross_cents=signal.capturable_edge_to_cross_no_cents,
            net_edge_after_entry_fee_cents=signal.net_edge_after_entry_fee_no_cents,
            net_edge_after_round_trip_fee_cents=signal.net_edge_after_round_trip_fee_no_cents,
        )
        intents.append(intent)
        logger.info(
            "paper_intent_created",
            extra={
                "intent_id": intent.id,
                "ticker": signal.ticker,
                "side": "NO",
                "quote_price": no_quote_price_cents,
                "net_edge": signal.net_edge_no,
            },
        )

    return intents


async def simulate_fill(
    client: KalshiClient,
    intent: QuoteIntent,
    horizon_seconds: int = 90,
    poll_interval_seconds: int = 3,
) -> tuple[PaperFill, PaperOutcome]:
    """DEPRECATED: Use RealisticOrderSimulator instead.

    This legacy function is kept for backwards compatibility with old tests.
    New code should use RealisticOrderSimulator from paper_sim.py for realistic
    fill simulation with queue modeling.

    Simulate a fill for a quote intent by polling the orderbook.

    Logic:
    - Loop until horizon reached
    - Re-fetch orderbook every poll_interval_seconds
    - Parse best bids
    - YES intent considered filled if best_yes_bid >= quote_price
    - NO intent considered filled if best_no_bid >= quote_price
    - On fill:
        - record PaperFill
        - price a paper liquidation against displayed bid depth
        - compute entry fee using existing fee model
        - compute unrealized mark-to-market pnl
        - record PaperOutcome (API-compatible fields)
        - stop loop
    - If horizon ends without fill:
        - record PaperFill with filled=False
        - record PaperOutcome with pnl=0
        - note="timeout"

    Args:
        client: KalshiClient instance.
        intent: QuoteIntent to simulate.
        horizon_seconds: How long to wait for a fill.
        poll_interval_seconds: Interval between orderbook polls.

    Returns:
        Tuple of (PaperFill, PaperOutcome).
    """
    logger.warning(
        "simulate_fill_deprecated",
        extra={
            "intent_id": intent.id,
            "notice": "simulate_fill is deprecated, use RealisticOrderSimulator instead",
        },
    )
    sem = _get_paper_semaphore()
    async with sem:
        try:
            start_ts_ms = int(time.time() * 1000)
            horizon_ms = horizon_seconds * 1000
            poll_interval_ms = poll_interval_seconds * 1000

            elapsed_ms = 0
            fill_event: PaperFill | None = None
            outcome: PaperOutcome | None = None

            while elapsed_ms < horizon_ms:
                try:
                    # Fetch current orderbook
                    orderbook = await client.get_orderbook(intent.ticker)

                    from kalshi_edge_engine.engine.orderbook import (
                        canonical_book,
                        quote_liquidation,
                    )

                    depth = canonical_book(orderbook)
                    entry_price = intent.quote_price_cents
                    # Require enough whole capacity at or above the sampled limit.
                    is_filled = (
                        sum(
                            level.capacity
                            for level in depth.levels(intent.side)
                            if level.price >= entry_price
                        )
                        >= intent.size_contracts
                    )
                    fill_ts_ms = int(time.time() * 1000)
                    liquidation = quote_liquidation(
                        depth, intent.side, intent.size_contracts, fill_ts_ms
                    )
                    if is_filled and liquidation is not None:
                        mark_price = liquidation.price_cents
                        time_to_fill_ms = fill_ts_ms - start_ts_ms

                        # Record fill
                        fill_event = PaperFill(
                            intent_id=intent.id,
                            filled=True,
                            fill_ts_ms=fill_ts_ms,
                            fill_price_cents=entry_price,
                            time_to_fill_ms=time_to_fill_ms,
                        )

                        # Compute fees and PnL
                        from kalshi_edge_engine.engine.fees import (
                            estimate_maker_fee_cents,
                            estimate_taker_fee_cents,
                        )

                        # Entry fee (using fee mode from intent)
                        fee_func = (
                            estimate_maker_fee_cents
                            if intent.fee_mode == "maker"
                            else estimate_taker_fee_cents
                        )
                        entry_fee = fee_func(entry_price, intent.size_contracts)

                        total_fees = entry_fee + liquidation.exit_fee_cents
                        unrealized_pnl_cents = (
                            mark_price - entry_price
                        ) * intent.size_contracts - total_fees

                        # Record outcome
                        exit_ts_ms = fill_ts_ms
                        outcome = PaperOutcome(
                            intent_id=intent.id,
                            exit_ts_ms=exit_ts_ms,
                            exit_price_cents=mark_price,
                            total_fees_cents=total_fees,
                            pnl_cents=unrealized_pnl_cents,
                            pnl_dollars=unrealized_pnl_cents / 100.0,
                            note="filled",
                        )

                        logger.info(
                            "paper_fill_detected",
                            extra={
                                "intent_id": intent.id,
                                "ticker": intent.ticker,
                                "side": intent.side,
                                "entry_price": entry_price,
                                "mark_price": mark_price,
                                "time_to_fill_ms": time_to_fill_ms,
                                "unrealized_pnl_cents": unrealized_pnl_cents,
                            },
                        )

                        break

                except Exception as e:
                    logger.debug(
                        "paper_fill_poll_error",
                        extra={"intent_id": intent.id, "error": str(e)},
                    )

                # Sleep before next poll
                sleep_time = min(poll_interval_ms, horizon_ms - elapsed_ms) / 1000.0
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

                elapsed_ms = int(time.time() * 1000) - start_ts_ms

            # If no fill by end of horizon
            if fill_event is None:
                timeout_ts_ms = int(time.time() * 1000)
                time_to_fill_ms = timeout_ts_ms - start_ts_ms

                fill_event = PaperFill(
                    intent_id=intent.id,
                    filled=False,
                    fill_ts_ms=timeout_ts_ms,
                    fill_price_cents=None,
                    time_to_fill_ms=time_to_fill_ms,
                )

                outcome = PaperOutcome(
                    intent_id=intent.id,
                    exit_ts_ms=timeout_ts_ms,
                    exit_price_cents=None,
                    total_fees_cents=0.0,
                    pnl_cents=0.0,
                    pnl_dollars=0.0,
                    note="timeout",
                )

                logger.info(
                    "paper_fill_timeout",
                    extra={
                        "intent_id": intent.id,
                        "ticker": intent.ticker,
                        "side": intent.side,
                        "quote_price": intent.quote_price_cents,
                    },
                )

            assert fill_event is not None
            assert outcome is not None
            logger.info(
                "paper_outcome_computed",
                extra={
                    "intent_id": intent.id,
                    "ticker": intent.ticker,
                    "pnl_cents": outcome.pnl_cents,
                    "total_fees_cents": outcome.total_fees_cents,
                    "note": outcome.note,
                },
            )
            return fill_event, outcome

        except Exception as e:
            logger.exception(
                "paper_fill_simulation_error",
                extra={"intent_id": intent.id, "error": str(e)},
            )
            # Return error outcome
            error_ts_ms = int(time.time() * 1000)
            fill_event = PaperFill(
                intent_id=intent.id,
                filled=False,
                fill_ts_ms=error_ts_ms,
                fill_price_cents=None,
                time_to_fill_ms=None,
            )
            outcome = PaperOutcome(
                intent_id=intent.id,
                exit_ts_ms=error_ts_ms,
                exit_price_cents=None,
                total_fees_cents=0.0,
                pnl_cents=0.0,
                pnl_dollars=0.0,
                note=f"error: {str(e)}",
            )
            return fill_event, outcome
