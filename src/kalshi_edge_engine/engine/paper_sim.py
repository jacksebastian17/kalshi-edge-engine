"""Enhanced paper trading simulation with realistic fill modeling."""

import asyncio
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from kalshi_edge_engine.engine.orderbook import (
    Book,
    ParsedOrderbook,
    canonical_book,
    displayed_quote_size,
    parse_orderbook,
)
from kalshi_edge_engine.engine.position_exit import compute_price_delta_cents_per_contract
from kalshi_edge_engine.kalshi.client import KalshiClient

logger = logging.getLogger(__name__)

_PAPER_SIM_LOG_EVERY_N = max(1, int(os.getenv("PAPER_SIM_LOG_EVERY_N", "1")))


def _build_fill_id(order_id: str, fill_ts_ms: int, sequence: int) -> str:
    """Build deterministic fill ID for idempotent reprocessing."""
    return f"sim:{order_id}:{fill_ts_ms}:{sequence}"


def _conservative_participation_fill_size(
    *,
    remaining_size: int,
    available_volume_estimate: int,
    participation_rate: float,
    per_poll_cap_contracts: int,
) -> int:
    """Compute conservative fill size from available volume proxy.

    This is a heuristic sizing rule used for paths where full queue state /
    true matched volume is not directly observable.
    """
    if remaining_size <= 0 or available_volume_estimate <= 0:
        return 0

    size_from_participation = int(available_volume_estimate * max(0.0, participation_rate))
    conservative_size = max(1, size_from_participation)

    if per_poll_cap_contracts > 0:
        conservative_size = min(conservative_size, per_poll_cap_contracts)

    conservative_size = min(conservative_size, available_volume_estimate)
    return min(remaining_size, conservative_size)


def _estimate_moved_through_available_volume(
    *,
    traded_volume_estimate: int,
    simulated_volume_per_poll: int,
) -> int:
    """Estimate available fill volume when market moves through our quote.

    Observability is incomplete on this path, so we use a conservative proxy
    and explicitly avoid assuming a full sweep of our remaining size.
    """
    observed_volume_component = max(0, traded_volume_estimate)
    if observed_volume_component > 0:
        return min(observed_volume_component, max(1, simulated_volume_per_poll))

    # Heuristic fallback when no direct volume signal exists.
    return max(1, simulated_volume_per_poll // 4)


def _estimate_price_improving_arrival_volume(
    *,
    simulated_volume_per_poll: int,
) -> int:
    """Estimate aggressive seller-arrival volume for price-improving quotes.

    This is purely heuristic and intentionally conservative.
    """
    return max(1, simulated_volume_per_poll // 5)


def extract_top_of_book_snapshot(orderbook: dict[str, Any]) -> dict[str, Any]:
    """Extract top-of-book bids from normalized orderbook for YES/NO sides."""
    return _top_snapshot(parse_orderbook(orderbook))


def _top_snapshot(parsed: ParsedOrderbook) -> dict[str, Any]:
    yes_price = parsed.bid_yes.price if parsed.bid_yes else None
    yes_size = parsed.bid_yes.size if parsed.bid_yes else None
    no_price = parsed.bid_no.price if parsed.bid_no else None
    no_size = parsed.bid_no.size if parsed.bid_no else None

    return {
        "yes": {"best_bid_price": yes_price, "best_bid_size": yes_size},
        "no": {"best_bid_price": no_price, "best_bid_size": no_size},
    }


def compute_simulator_diagnostics(
    order: "OrderRequest",
    orderbook: dict[str, Any] | Book,
    queue_ahead: int,
    last_best_size_at_bid: int | Decimal | None,
    simulated_volume_per_poll: int,
    *,
    historical_view: ParsedOrderbook | None = None,
) -> dict[str, Any]:
    """Compute side-aware simulator diagnostics from one orderbook snapshot."""
    book = canonical_book(orderbook) if historical_view is None else None
    parsed = parse_orderbook(book) if book is not None else historical_view
    assert parsed is not None
    raw_top = _top_snapshot(parsed)

    best_yes_bid = parsed.bid_yes.price if parsed.bid_yes else None
    best_no_bid = parsed.bid_no.price if parsed.bid_no else None
    cross_yes = parsed.cross_yes.price if parsed.cross_yes else None
    cross_no = parsed.cross_no.price if parsed.cross_no else None

    market_best_bid = best_yes_bid if order.side == "YES" else best_no_bid
    cross_for_side = cross_yes if order.side == "YES" else cross_no

    level = book.top(order.side) if book is not None else None
    bid = level.price if level else market_best_bid
    ask = book.ask(order.side) if book is not None else cross_for_side
    limit = Decimal(str(order.quote_price_cents)) if book is not None else order.quote_price_cents
    price_improves = bid is not None and ask is not None and bid < limit < ask
    effective_best_bid = order.quote_price_cents if price_improves else market_best_bid

    best_size_at_bid: int | None = None
    if price_improves:
        # We are alone at the improved price level (synthetic, doesn't exist in orderbook).
        # Setting best_size_at_bid=0 is correct, but this is NOT the real book level.
        best_size_at_bid = 0
    elif order.side == "YES" and parsed.bid_yes:
        best_size_at_bid = parsed.bid_yes.size
    elif order.side == "NO" and parsed.bid_no:
        best_size_at_bid = parsed.bid_no.size

    reported = level.quantity if level else best_size_at_bid
    at_limit = (
        book.at(order.side, Decimal(str(order.quote_price_cents))) if book is not None else None
    )

    # Compute price movement ONLY from effective_best_bid
    at_best_price = price_improves or bid == limit
    price_moved_through = bid is not None and bid > limit
    price_moved_away = bid is not None and bid < limit and not price_improves
    has_best_bid = effective_best_bid is not None

    # Invariant: If at_best_price is true, the other two must be false
    if at_best_price and (price_moved_through or price_moved_away):
        logger.warning(
            "simulator_invariant_violation",
            extra={
                "order_id": order.id,
                "ticker": order.ticker,
                "at_best_price": at_best_price,
                "price_moved_through": price_moved_through,
                "price_moved_away": price_moved_away,
                "effective_best_bid": effective_best_bid,
                "quote_price_cents": order.quote_price_cents,
            },
        )
        # Force correct state: at_best_price wins
        price_moved_through = False
        price_moved_away = False

    # Estimate traded volume and annotate source quality.
    traded_volume_source = "none_not_at_best"
    if at_best_price:
        if price_improves:
            # Price-improving orders have no actual orderbook volume to trade against.
            # But they CAN fill via seller arrivals hitting our bid.
            # traded_volume_estimate represents potential arrival volume, not book delta.
            # We use simulated_volume_per_poll as a proxy for arrival rate.
            traded_volume = simulated_volume_per_poll
            traded_volume_source = "heuristic_seller_arrival_proxy"
        elif last_best_size_at_bid is not None and reported is not None:
            # Use size delta if available (from real book level)
            traded_volume = int(max(0, last_best_size_at_bid - reported))
            traded_volume_source = "observed_size_delta"
        else:
            traded_volume = 0
            traded_volume_source = "no_consecutive_quote_level_observation"
    else:
        traded_volume = 0

    queue_ahead_after = queue_ahead

    return {
        "chosen_side": order.side,
        **(
            {
                "reported_best_size": str(reported) if reported is not None else None,
                "reported_quote_size": str(at_limit.quantity) if at_limit else None,
                "initial_queue_ahead": int(
                    Decimal(reported).to_integral_value(rounding=ROUND_CEILING)
                )
                if reported is not None
                else None,
            }
            if book is not None
            else {}
        ),
        "raw_top_of_book": raw_top,
        "best_yes_bid": best_yes_bid,
        "best_no_bid": best_no_bid,
        "cross_yes": cross_yes,
        "cross_no": cross_no,
        "market_best_bid": market_best_bid,
        "cross_for_side": cross_for_side,
        "price_improves": price_improves,
        "effective_best_bid": effective_best_bid,
        "best_bid": effective_best_bid,
        "best_size_at_bid": best_size_at_bid,
        "traded_volume_estimate": traded_volume,
        "traded_volume_source": traded_volume_source,
        "volume_is_confirmed_trade_data": False,
        "queue_ahead_before": queue_ahead,
        "queue_ahead_after": queue_ahead_after,
        "eligibility": {
            "has_best_bid": has_best_bid,
            "price_moved_through": price_moved_through,
            "at_best_price": at_best_price,
            "price_moved_away": price_moved_away,
            "queue_cleared": at_best_price and not price_improves and queue_ahead <= 0,
        },
    }


def displayed_queue_transition(
    d: dict[str, Any], ahead: int, *, initialized: bool
) -> tuple[int, int]:
    """Apply one witnessed poll. The caller resets stored state after an off-quote poll."""
    displayed = d["eligibility"]["at_best_price"] and not d["price_improves"]
    before = ahead
    if displayed and not initialized:
        before = d.get("initial_queue_ahead", d["best_size_at_bid"])
    after = max(0, before - d["traded_volume_estimate"]) if displayed else ahead
    d.update(
        queue_ahead_before_poll=ahead,
        queue_ahead_before_decrement=before,
        queue_ahead_after=after,
        queue_ahead_after_poll=after,
    )
    d["eligibility"]["queue_cleared"] = displayed and after <= 0
    return before, after


@dataclass
class OrderRequest:
    """Request to place a resting entry order.

    Notes:
        `RealisticOrderSimulator` is entry-only. Exit-related fields are kept
        only for backward compatibility with existing call sites/tests.
    """

    id: str
    ticker: str
    side: str  # "YES" or "NO"
    quote_price_cents: float
    size_contracts: int
    created_ts_ms: int
    net_edge: float
    entry_fee_cents: float
    # Legacy compatibility field; unused by entry-only simulator.
    max_hold_seconds: int
    # Legacy compatibility fields; unused by entry-only simulator.
    take_profit_cents_per_contract: float | None = None
    # Backward-compat alias: interpreted as cents PER CONTRACT.
    take_profit_cents: float | None = None
    entry_fee_per_contract_cents: float | None = None
    may_poll: Callable[[], bool] | None = None
    on_unprocessed_poll: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        """Normalize take-profit semantics to per-contract cents."""
        if self.take_profit_cents_per_contract is None:
            self.take_profit_cents_per_contract = self.take_profit_cents or 0.0
        if self.take_profit_cents is None:
            self.take_profit_cents = self.take_profit_cents_per_contract

    @property
    def effective_entry_fee_per_contract_cents(self) -> float:
        """Entry fee per contract, with backward-compatible fallback."""
        if self.entry_fee_per_contract_cents is not None:
            return self.entry_fee_per_contract_cents
        if self.size_contracts <= 0:
            return 0.0
        return self.entry_fee_cents / self.size_contracts


@dataclass
class Fill:
    """A fill event for an order."""

    id: str
    order_id: str
    fill_ts_ms: int
    fill_price_cents: float
    fill_size: int  # contracts filled
    remaining_size: int  # contracts still pending
    queue_ahead_at_fill: int | None
    fill_fee_cents: float
    best_bid_at_fill_cents: float | None = None
    cross_at_fill_cents: float | None = None
    fill_path_type: str | None = None
    execution_source: str = "simulated_polling_inference"


@dataclass
class Position:
    """An open position."""

    ticker: str
    side: str
    net_contracts: int
    avg_entry_price_cents: float
    realized_pnl_cents: float = 0.0
    mark_price_cents: float | None = None

    @property
    def unrealized_pnl_cents(self) -> float | None:
        """Compute unrealized PnL if mark available."""
        if self.mark_price_cents is None:
            return None
        return (
            compute_price_delta_cents_per_contract(
                side=self.side,
                avg_entry_price_cents=self.avg_entry_price_cents,
                mark_price_cents=self.mark_price_cents,
            )
            * self.net_contracts
        )

    @property
    def total_pnl_cents(self) -> float | None:
        """Total PnL (realized + unrealized)."""
        unrealized = self.unrealized_pnl_cents
        return None if unrealized is None else self.realized_pnl_cents + unrealized


class RealisticOrderSimulator:
    """Realistic order fill simulator with queue-based model."""

    def __init__(
        self,
        poll_interval_seconds: float = 3.0,
        fill_probability_price_moved: float = 0.8,
        fill_probability_at_best: float = 0.15,
        fill_probability_price_improves: float = 0.05,
        simulated_volume_per_poll: int = 50,
    ):
        """Initialize the simulator.

        Args:
            poll_interval_seconds: How often to poll orderbook.
            fill_probability_price_moved: Probability of fill when price moves through us.
            fill_probability_at_best: Probability of fill when at best and queue is cleared.
            fill_probability_price_improves: Probability of fill per poll for price-improving orders.
            simulated_volume_per_poll: Est. volume traded per poll interval at best level.
        """
        self.poll_interval_seconds = poll_interval_seconds
        self.fill_probability_price_moved = fill_probability_price_moved
        self.fill_probability_at_best = fill_probability_at_best
        self.fill_probability_price_improves = fill_probability_price_improves
        self.simulated_volume_per_poll = simulated_volume_per_poll
        self.moved_through_participation_rate = 0.35
        self.price_improve_participation_rate = 0.25
        self.moved_through_fill_cap_contracts_per_poll = 3
        self.price_improve_fill_cap_contracts_per_poll = 2

    async def simulate_order_fills(
        self,
        client: KalshiClient,
        order: OrderRequest,
        horizon_seconds: int = 90,
        on_poll: Callable[[dict[str, Any]], Any] | None = None,
    ) -> tuple[list[Fill], dict[str, Any]]:
        """Simulate fills for a resting order with queue model.

        Args:
            client: KalshiClient to fetch orderbooks.
            order: OrderRequest to simulate.
            horizon_seconds: Max time to wait for fills.

        Returns:
            Tuple of (fills list, order state dict with status and diagnostics).

        Notes:
            This simulator models ENTRY fill behavior only. It does not evaluate
            position exits such as take-profit or max-hold.
        """
        start_ts_ms = int(time.time() * 1000)
        horizon_ms = horizon_seconds * 1000
        poll_interval_ms = self.poll_interval_seconds * 1000

        fills: list[Fill] = []
        remaining_size = order.size_contracts
        queue_ahead = 0
        last_best_size_at_bid = None
        elapsed_ms = 0
        exit_reason = "timeout"
        last_seen_best_bid: float | None = None
        last_update_ts_ms = start_ts_ms
        latest_diagnostics: dict[str, Any] | None = None
        poll_count = 0
        fill_sequence = 0

        while elapsed_ms < horizon_ms and remaining_size > 0:
            awaiting_decision = False
            try:
                if order.may_poll is not None and not order.may_poll():
                    exit_reason = "entry_canceled"
                    break
                # Fetch current orderbook
                awaiting_decision = True
                orderbook = canonical_book(await client.get_orderbook(order.ticker))
                if order.may_poll is not None and not order.may_poll():
                    if order.on_unprocessed_poll:
                        order.on_unprocessed_poll("entry_inactive_after_read")
                    exit_reason = "entry_canceled"
                    break

                diagnostics = compute_simulator_diagnostics(
                    order=order,
                    orderbook=orderbook,
                    queue_ahead=queue_ahead,
                    last_best_size_at_bid=last_best_size_at_bid,
                    simulated_volume_per_poll=self.simulated_volume_per_poll,
                )
                latest_diagnostics = diagnostics
                poll_count += 1

                best_bid = diagnostics["market_best_bid"]
                cross_yes = diagnostics["cross_yes"]
                cross_no = diagnostics["cross_no"]
                eligibility = diagnostics["eligibility"]
                price_improves = diagnostics["price_improves"]
                traded_volume = diagnostics["traded_volume_estimate"]
                queue_ahead_before_decrement, queue_ahead = displayed_queue_transition(
                    diagnostics, queue_ahead, initialized=last_best_size_at_bid is not None
                )

                now_ms = int(time.time() * 1000)
                last_update_ts_ms = now_ms
                last_seen_best_bid = best_bid

                if poll_count % _PAPER_SIM_LOG_EVERY_N == 0:
                    logger.info(
                        "paper_simulator_poll",
                        extra={
                            "intent_id": order.id,
                            "ticker": order.ticker,
                            "side": order.side,
                            "chosen_side": diagnostics["chosen_side"],
                            "raw_yes_best_bid_price": diagnostics["raw_top_of_book"]["yes"][
                                "best_bid_price"
                            ],
                            "raw_yes_best_bid_size": diagnostics["raw_top_of_book"]["yes"][
                                "best_bid_size"
                            ],
                            "raw_no_best_bid_price": diagnostics["raw_top_of_book"]["no"][
                                "best_bid_price"
                            ],
                            "raw_no_best_bid_size": diagnostics["raw_top_of_book"]["no"][
                                "best_bid_size"
                            ],
                            "best_yes_bid": diagnostics["best_yes_bid"],
                            "best_no_bid": diagnostics["best_no_bid"],
                            "best_bid": best_bid,
                            "quote_price_cents": order.quote_price_cents,
                            "queue_ahead_before": diagnostics["queue_ahead_before"],
                            "queue_ahead_after": diagnostics["queue_ahead_after"],
                            "queue_ahead_before_poll": diagnostics["queue_ahead_before_poll"],
                            "queue_ahead_after_poll": diagnostics["queue_ahead_after_poll"],
                            "traded_volume_estimate": diagnostics["traded_volume_estimate"],
                            "price_improves": diagnostics["price_improves"],
                            "eligibility": eligibility,
                        },
                    )

                fill_size = 0
                fill_path: str | None = None
                source: str | None = None
                at_displayed_quote = eligibility["at_best_price"] and not price_improves
                if eligibility["price_moved_through"]:
                    if random.random() < self.fill_probability_price_moved:
                        available = _estimate_moved_through_available_volume(
                            traded_volume_estimate=traded_volume,
                            simulated_volume_per_poll=self.simulated_volume_per_poll,
                        )
                        fill_size = _conservative_participation_fill_size(
                            remaining_size=remaining_size,
                            available_volume_estimate=available,
                            participation_rate=self.moved_through_participation_rate,
                            per_poll_cap_contracts=self.moved_through_fill_cap_contracts_per_poll,
                        )
                        fill_path = "moved_through"
                        source = "simulated_higher_bid_heuristic"
                elif price_improves:
                    if random.random() < self.fill_probability_price_improves:
                        available = _estimate_price_improving_arrival_volume(
                            simulated_volume_per_poll=self.simulated_volume_per_poll,
                        )
                        fill_size = _conservative_participation_fill_size(
                            remaining_size=remaining_size,
                            available_volume_estimate=available,
                            participation_rate=self.price_improve_participation_rate,
                            per_poll_cap_contracts=self.price_improve_fill_cap_contracts_per_poll,
                        )
                        fill_path = "price_improves"
                        source = "simulated_seller_arrival_heuristic"
                elif at_displayed_quote:
                    remaining_proxy_volume = traded_volume - queue_ahead_before_decrement
                    if (
                        remaining_proxy_volume > 0
                        and random.random() < self.fill_probability_at_best
                    ):
                        fill_size = min(remaining_size, remaining_proxy_volume)
                        fill_path = "queue_cleared"
                        source = "simulated_displayed_size_decrease"

                last_best_size_at_bid = (
                    Decimal(diagnostics["reported_best_size"]) if at_displayed_quote else None
                )
                if not at_displayed_quote:
                    queue_ahead = 0
                fill = None
                if fill_size > 0 and fill_path is not None and source is not None:
                    fill_sequence += 1
                    fill = Fill(
                        id=_build_fill_id(order.id, now_ms, fill_sequence),
                        order_id=order.id,
                        fill_ts_ms=now_ms,
                        fill_price_cents=order.quote_price_cents,
                        fill_size=fill_size,
                        remaining_size=remaining_size - fill_size,
                        queue_ahead_at_fill=0 if price_improves else queue_ahead,
                        fill_fee_cents=order.effective_entry_fee_per_contract_cents * fill_size,
                        best_bid_at_fill_cents=best_bid,
                        cross_at_fill_cents=cross_yes if order.side == "YES" else cross_no,
                        fill_path_type=fill_path,
                        execution_source=source,
                    )

                accepted = None
                if on_poll is not None:
                    if price_improves:
                        poll_class = "price_improving"
                    elif at_displayed_quote:
                        poll_class = "at_displayed_quote"
                    elif eligibility["price_moved_through"]:
                        poll_class = "moved_through"
                    else:
                        poll_class = "ineligible"
                    diagnostics["poll_class"] = poll_class
                    diagnostics["displayed_quote_size"] = displayed_quote_size(
                        orderbook, order.side, order.quote_price_cents
                    )
                    if fill is None:
                        if at_displayed_quote and traded_volume <= queue_ahead_before_decrement:
                            reason = "insufficient_volume_beyond_queue"
                        elif poll_class == "ineligible":
                            reason = "missing_bid" if best_bid is None else "price_ineligible"
                        else:
                            reason = "probability_draw_missed"
                        diagnostics["no_candidate_reason"] = reason
                    awaiting_decision = False
                    accepted = on_poll(
                        {
                            "intent_id": order.id,
                            "ticker": order.ticker,
                            "side": order.side,
                            "last_seen_best_bid": best_bid,
                            "queue_ahead_contracts": queue_ahead,
                            "last_update_ts_ms": now_ms,
                            "diagnostics": diagnostics,
                            "fill": fill,
                        }
                    )
                    if asyncio.iscoroutine(accepted):
                        accepted = await accepted
                if fill is not None and accepted is not False:
                    fills.append(fill)
                    remaining_size = fill.remaining_size
                    logger.info(
                        "paper_simulated_entry_fill",
                        extra={
                            "fill_id": fill.id,
                            "fill_size": fill_size,
                            "execution_source": source,
                        },
                    )

                if remaining_size == 0:
                    exit_reason = "filled"
                    break

            except asyncio.CancelledError:
                if awaiting_decision and order.on_unprocessed_poll:
                    order.on_unprocessed_poll("poll_interrupted")
                raise
            except Exception as e:
                if awaiting_decision and order.on_unprocessed_poll:
                    order.on_unprocessed_poll(f"poll_error:{type(e).__name__}")
                last_best_size_at_bid = None
                queue_ahead = 0
                logger.debug(
                    "paper_realistic_fill_poll_error",
                    extra={
                        "intent_id": order.id,
                        "ticker": order.ticker,
                        "side": order.side,
                        "error": str(e),
                    },
                )

            # Sleep before next poll
            sleep_time = min(poll_interval_ms, horizon_ms - elapsed_ms) / 1000.0
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

            elapsed_ms = int(time.time() * 1000) - start_ts_ms

        logger.info(
            "paper_simulator_exit",
            extra={
                "intent_id": order.id,
                "ticker": order.ticker,
                "side": order.side,
                "exit_reason": exit_reason,
                "total_filled": order.size_contracts - remaining_size,
                "remaining_size": remaining_size,
            },
        )

        # Final state
        return fills, {
            "order_id": order.id,
            "status": "filled" if remaining_size == 0 else exit_reason,
            "total_filled_contracts": order.size_contracts - remaining_size,
            "remaining_unfilled_size": remaining_size,
            "elapsed_ms": elapsed_ms,
            "exit_reason": exit_reason,
            "last_seen_best_bid": last_seen_best_bid,
            "queue_ahead_contracts": queue_ahead,
            "last_update_ts_ms": last_update_ts_ms,
            "latest_diagnostics": latest_diagnostics,
        }
