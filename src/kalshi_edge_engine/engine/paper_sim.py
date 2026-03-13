"""Enhanced paper trading simulation with realistic fill modeling."""

import asyncio
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
    from kalshi_edge_engine.engine.orderbook import parse_orderbook

    parsed = parse_orderbook(orderbook)
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
    orderbook: dict[str, Any],
    queue_ahead: int,
    last_best_size_at_bid: int | None,
    simulated_volume_per_poll: int,
) -> dict[str, Any]:
    """Compute side-aware simulator diagnostics from one orderbook snapshot."""
    from kalshi_edge_engine.engine.orderbook import parse_orderbook

    parsed = parse_orderbook(orderbook)
    raw_top = extract_top_of_book_snapshot(orderbook)

    best_yes_bid = parsed.bid_yes.price if parsed.bid_yes else None
    best_no_bid = parsed.bid_no.price if parsed.bid_no else None
    cross_yes = parsed.cross_yes.price if parsed.cross_yes else None
    cross_no = parsed.cross_no.price if parsed.cross_no else None

    market_best_bid = best_yes_bid if order.side == "YES" else best_no_bid
    cross_for_side = cross_yes if order.side == "YES" else cross_no

    price_improves = (
        market_best_bid is not None
        and cross_for_side is not None
        and order.quote_price_cents > market_best_bid
        and order.quote_price_cents < cross_for_side
    )
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

    # Compute price movement ONLY from effective_best_bid
    at_best_price = effective_best_bid is not None and effective_best_bid == order.quote_price_cents
    price_moved_through = (
        effective_best_bid is not None and effective_best_bid > order.quote_price_cents
    )
    price_moved_away = (
        effective_best_bid is not None and effective_best_bid < order.quote_price_cents
    )
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
        elif last_best_size_at_bid is not None and best_size_at_bid is not None:
            # Use size delta if available (from real book level)
            traded_volume = max(0, last_best_size_at_bid - best_size_at_bid)
            traded_volume_source = "observed_size_delta"
        else:
            # Fall back to simulated volume only when no size delta available
            traded_volume = simulated_volume_per_poll
            traded_volume_source = "heuristic_fallback_proxy"
    else:
        traded_volume = 0

    queue_ahead_after = queue_ahead

    return {
        "chosen_side": order.side,
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
    def unrealized_pnl_cents(self) -> float:
        """Compute unrealized PnL if mark available."""
        if self.mark_price_cents is None:
            return 0.0
        if self.side == "YES":
            return (self.mark_price_cents - self.avg_entry_price_cents) * self.net_contracts
        else:
            return (self.avg_entry_price_cents - self.mark_price_cents) * self.net_contracts

    @property
    def total_pnl_cents(self) -> float:
        """Total PnL (realized + unrealized)."""
        return self.realized_pnl_cents + self.unrealized_pnl_cents


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
        queue_initialized = False
        poll_count = 0

        while elapsed_ms < horizon_ms and remaining_size > 0:
            try:
                # Fetch current orderbook
                orderbook = await client.get_orderbook(order.ticker)

                diagnostics = compute_simulator_diagnostics(
                    order=order,
                    orderbook=orderbook,
                    queue_ahead=queue_ahead,
                    last_best_size_at_bid=last_best_size_at_bid,
                    simulated_volume_per_poll=self.simulated_volume_per_poll,
                )
                latest_diagnostics = diagnostics
                poll_count += 1

                best_bid = diagnostics["best_bid"]
                best_size_at_bid = diagnostics["best_size_at_bid"]
                cross_yes = diagnostics["cross_yes"]
                cross_no = diagnostics["cross_no"]
                eligibility = diagnostics["eligibility"]
                price_improves = diagnostics["price_improves"]
                traded_volume = diagnostics["traded_volume_estimate"]
                queue_ahead_before_poll = queue_ahead
                queue_ahead_before_decrement = queue_ahead

                # Queue model: only update when at best price AND order not price-improving
                # Queue decreases when we have traded volume at our price level
                if eligibility["at_best_price"] and not price_improves:
                    # Initialize queue on first time seeing this price level
                    if not queue_initialized and best_size_at_bid is not None:
                        queue_ahead = best_size_at_bid
                        queue_initialized = True
                    queue_ahead_before_decrement = queue_ahead
                    # Queue decreases with traded volume
                    queue_ahead = max(0, queue_ahead - traded_volume)

                diagnostics["queue_ahead_after"] = queue_ahead
                diagnostics["queue_ahead_before_poll"] = queue_ahead_before_poll
                diagnostics["queue_ahead_before_decrement"] = queue_ahead_before_decrement
                diagnostics["queue_ahead_after_poll"] = queue_ahead
                diagnostics["eligibility"]["queue_cleared"] = (
                    eligibility["at_best_price"] and not price_improves and queue_ahead <= 0
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

                if on_poll is not None:
                    callback_result = on_poll(
                        {
                            "intent_id": order.id,
                            "ticker": order.ticker,
                            "side": order.side,
                            "last_seen_best_bid": best_bid,
                            "queue_ahead_contracts": queue_ahead,
                            "last_update_ts_ms": now_ms,
                            "diagnostics": diagnostics,
                        }
                    )
                    if asyncio.iscoroutine(callback_result):
                        await callback_result

                # Check for fill
                if eligibility["has_best_bid"]:
                    if eligibility["price_moved_through"]:
                        # Price moved through us.
                        # Old behavior filled full remaining size, which was optimistic because
                        # moved-through doesn't guarantee all of our queue was matched.
                        # New behavior uses a conservative volume-based proxy and per-poll cap.
                        if random.random() < self.fill_probability_price_moved:
                            available_volume_estimate = _estimate_moved_through_available_volume(
                                traded_volume_estimate=traded_volume,
                                simulated_volume_per_poll=self.simulated_volume_per_poll,
                            )
                            fill_size = _conservative_participation_fill_size(
                                remaining_size=remaining_size,
                                available_volume_estimate=available_volume_estimate,
                                participation_rate=self.moved_through_participation_rate,
                                per_poll_cap_contracts=self.moved_through_fill_cap_contracts_per_poll,
                            )
                            if fill_size <= 0:
                                continue
                            fill_ts_ms = now_ms
                            # Capture cross at fill time for telemetry
                            cross_at_fill = cross_yes if order.side == "YES" else cross_no
                            fill = Fill(
                                id=_build_fill_id(order.id, fill_ts_ms, len(fills) + 1),
                                order_id=order.id,
                                fill_ts_ms=fill_ts_ms,
                                fill_price_cents=order.quote_price_cents,
                                fill_size=fill_size,
                                remaining_size=remaining_size - fill_size,
                                queue_ahead_at_fill=queue_ahead,
                                fill_fee_cents=order.effective_entry_fee_per_contract_cents
                                * fill_size,
                                best_bid_at_fill_cents=best_bid,
                                cross_at_fill_cents=cross_at_fill,
                                fill_path_type="moved_through",
                            )
                            fills.append(fill)
                            remaining_size = fill.remaining_size
                            logger.info(
                                "paper_realistic_fill_price_moved",
                                extra={
                                    "intent_id": order.id,
                                    "ticker": order.ticker,
                                    "side": order.side,
                                    "fill_path_type": "moved_through",
                                    "fill_price": order.quote_price_cents,
                                    "fill_size": fill_size,
                                    "remaining_size": remaining_size,
                                    "estimated_traded_volume": traded_volume,
                                    "available_volume_estimate": available_volume_estimate,
                                    "fill_sizing_rule": "heuristic_volume_capped_participation",
                                    "remaining": remaining_size,
                                },
                            )

                    elif eligibility["at_best_price"]:
                        if price_improves:
                            # Price-improving quote fill path (heuristic):
                            # no observed queue delta exists at our synthetic improved level,
                            # so fills are modeled as conservative seller-arrival events.
                            if (
                                remaining_size > 0
                                and random.random() < self.fill_probability_price_improves
                            ):
                                arrival_volume_estimate = _estimate_price_improving_arrival_volume(
                                    simulated_volume_per_poll=self.simulated_volume_per_poll,
                                )
                                fill_size = _conservative_participation_fill_size(
                                    remaining_size=remaining_size,
                                    available_volume_estimate=arrival_volume_estimate,
                                    participation_rate=self.price_improve_participation_rate,
                                    per_poll_cap_contracts=self.price_improve_fill_cap_contracts_per_poll,
                                )
                                if fill_size <= 0:
                                    continue
                                fill_ts_ms = now_ms
                                # Capture cross at fill time for telemetry
                                cross_at_fill = cross_yes if order.side == "YES" else cross_no
                                fill = Fill(
                                    id=_build_fill_id(order.id, fill_ts_ms, len(fills) + 1),
                                    order_id=order.id,
                                    fill_ts_ms=fill_ts_ms,
                                    fill_price_cents=order.quote_price_cents,
                                    fill_size=fill_size,
                                    remaining_size=remaining_size - fill_size,
                                    queue_ahead_at_fill=0,
                                    fill_fee_cents=order.effective_entry_fee_per_contract_cents
                                    * fill_size,
                                    best_bid_at_fill_cents=best_bid,
                                    cross_at_fill_cents=cross_at_fill,
                                    fill_path_type="price_improves",
                                )
                                fills.append(fill)
                                remaining_size = fill.remaining_size
                                logger.info(
                                    "paper_realistic_fill_price_improves_arrival",
                                    extra={
                                        "intent_id": order.id,
                                        "ticker": order.ticker,
                                        "side": order.side,
                                        "fill_path_type": "price_improves",
                                        "fill_price": order.quote_price_cents,
                                        "fill_size": fill_size,
                                        "arrival_volume_estimate": arrival_volume_estimate,
                                        "estimated_traded_volume": traded_volume,
                                        "fill_sizing_rule": "heuristic_seller_arrival_capped_participation",
                                        "remaining": remaining_size,
                                    },
                                )
                        else:
                            # Non-price-improving order: queue model fill path.
                            # Separate queue consumption from our fill eligibility:
                            # traded_volume is first consumed by queue ahead, remaining goes to us.
                            queue_ahead_before_fill_check = queue_ahead_before_decrement
                            consumed_by_ahead = min(queue_ahead_before_fill_check, traded_volume)
                            remaining_trade = traded_volume - consumed_by_ahead

                            # Only fill if queue is cleared AND we have remaining trade volume
                            if (
                                queue_ahead_before_fill_check - consumed_by_ahead <= 0
                                and remaining_trade > 0
                                and remaining_size > 0
                                and random.random() < self.fill_probability_at_best
                            ):
                                # We're at front of queue, fill is probabilistic
                                fill_size = min(remaining_size, max(1, remaining_trade))
                                fill_ts_ms = now_ms
                                # Capture cross at fill time for telemetry
                                cross_at_fill = cross_yes if order.side == "YES" else cross_no
                                fill = Fill(
                                    id=_build_fill_id(order.id, fill_ts_ms, len(fills) + 1),
                                    order_id=order.id,
                                    fill_ts_ms=fill_ts_ms,
                                    fill_price_cents=order.quote_price_cents,
                                    fill_size=fill_size,
                                    remaining_size=remaining_size - fill_size,
                                    queue_ahead_at_fill=queue_ahead,
                                    fill_fee_cents=order.effective_entry_fee_per_contract_cents
                                    * fill_size,
                                    best_bid_at_fill_cents=best_bid,
                                    cross_at_fill_cents=cross_at_fill,
                                    fill_path_type="queue_cleared",
                                )
                                fills.append(fill)
                                remaining_size = fill.remaining_size
                                logger.info(
                                    "paper_realistic_fill_queue_cleared",
                                    extra={
                                        "intent_id": order.id,
                                        "ticker": order.ticker,
                                        "side": order.side,
                                        "fill_path_type": "queue_cleared",
                                        "fill_price": order.quote_price_cents,
                                        "fill_size": fill_size,
                                        "remaining": remaining_size,
                                        "traded_volume": traded_volume,
                                        "queue_ahead_at_fill": queue_ahead,
                                        "queue_ahead_before_poll": queue_ahead_before_poll,
                                        "queue_ahead_after_poll": queue_ahead,
                                        "remaining_trade_after_queue": remaining_trade,
                                        "fill_sizing_rule": "observed_queue_and_volume",
                                    },
                                )

                    elif eligibility["price_moved_away"]:
                        # Price moved away - no fill
                        pass

                    # Update for next iteration
                    last_best_size_at_bid = best_size_at_bid

                if remaining_size == 0:
                    exit_reason = "filled"
                    break

            except Exception as e:
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
