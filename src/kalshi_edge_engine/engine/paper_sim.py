"""Enhanced paper trading simulation with realistic fill modeling."""

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kalshi_edge_engine.kalshi.client import KalshiClient

logger = logging.getLogger(__name__)


def _extract_best_level_raw(levels: Any) -> tuple[float | None, int | None]:
    """Extract best bid level (price,size) from a YES/NO ladder."""
    if not isinstance(levels, list):
        return (None, None)

    valid_levels: list[tuple[float, int]] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        try:
            price = float(level[0])
            size = int(level[1])
            if 0.0 <= price <= 100.0 and size >= 0:
                valid_levels.append((price, size))
        except (TypeError, ValueError):
            continue

    if not valid_levels:
        return (None, None)

    best_price, best_size = max(valid_levels, key=lambda item: item[0])
    return best_price, best_size


def extract_top_of_book_snapshot(orderbook: dict[str, Any]) -> dict[str, Any]:
    """Extract top-of-book bids from raw orderbook for YES/NO sides."""
    container = (
        orderbook.get("orderbook") if isinstance(orderbook.get("orderbook"), dict) else orderbook
    )
    yes_levels = container.get("yes", []) if isinstance(container, dict) else []
    no_levels = container.get("no", []) if isinstance(container, dict) else []

    yes_price, yes_size = _extract_best_level_raw(yes_levels)
    no_price, no_size = _extract_best_level_raw(no_levels)

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

    # Estimate traded volume
    if at_best_price:
        if price_improves:
            # Price-improving orders have no actual orderbook volume to trade against.
            # But they CAN fill via seller arrivals hitting our bid.
            # traded_volume_estimate represents potential arrival volume, not book delta.
            # We use simulated_volume_per_poll as a proxy for arrival rate.
            traded_volume = simulated_volume_per_poll
        elif last_best_size_at_bid is not None and best_size_at_bid is not None:
            # Use size delta if available (from real book level)
            traded_volume = max(0, last_best_size_at_bid - best_size_at_bid)
        else:
            # Fall back to simulated volume only when no size delta available
            traded_volume = simulated_volume_per_poll
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
    """Request to place a resting order."""

    id: str
    ticker: str
    side: str  # "YES" or "NO"
    quote_price_cents: float
    size_contracts: int
    created_ts_ms: int
    net_edge: float
    entry_fee_cents: float
    take_profit_cents: float
    max_hold_seconds: int
    entry_fee_per_contract_cents: float | None = None

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
            Tuple of (fills list, order state dict with status, unfilled_size, realized_pnl).
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

                best_bid = diagnostics["best_bid"]
                best_size_at_bid = diagnostics["best_size_at_bid"]
                cross_yes = diagnostics["cross_yes"]
                cross_no = diagnostics["cross_no"]
                eligibility = diagnostics["eligibility"]
                price_improves = diagnostics["price_improves"]
                traded_volume = diagnostics["traded_volume_estimate"]

                # Queue model: only update when at best price AND order not price-improving
                # Queue decreases when we have traded volume at our price level
                if eligibility["at_best_price"] and not price_improves:
                    # Initialize queue on first time seeing this price level
                    if (
                        queue_ahead == 0
                        and best_size_at_bid is not None
                        and last_best_size_at_bid is None
                    ):
                        queue_ahead = best_size_at_bid
                    # Queue decreases with traded volume
                    queue_ahead = max(0, queue_ahead - traded_volume)

                diagnostics["queue_ahead_after"] = queue_ahead
                diagnostics["eligibility"]["queue_cleared"] = (
                    eligibility["at_best_price"] and not price_improves and queue_ahead <= 0
                )

                now_ms = int(time.time() * 1000)
                last_update_ts_ms = now_ms
                last_seen_best_bid = best_bid

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
                        # Price moved through us - high probability fill (with probability).
                        # Sweep through our level: fill full remaining size.
                        if random.random() < self.fill_probability_price_moved:
                            fill_size = remaining_size
                            fill_ts_ms = now_ms
                            fill = Fill(
                                id=str(uuid.uuid4()),
                                order_id=order.id,
                                fill_ts_ms=fill_ts_ms,
                                fill_price_cents=order.quote_price_cents,
                                fill_size=fill_size,
                                remaining_size=remaining_size - fill_size,
                                queue_ahead_at_fill=queue_ahead,
                                fill_fee_cents=order.effective_entry_fee_per_contract_cents
                                * fill_size,
                            )
                            fills.append(fill)
                            remaining_size = fill.remaining_size
                            logger.info(
                                "paper_realistic_fill_price_moved",
                                extra={
                                    "intent_id": order.id,
                                    "ticker": order.ticker,
                                    "side": order.side,
                                    "fill_price": order.quote_price_cents,
                                    "remaining": remaining_size,
                                },
                            )

                    elif eligibility["at_best_price"]:
                        if price_improves:
                            # Price-improving order: explicit seller arrival fill path.
                            # We're alone at improved price; fills happen when aggressive seller arrives.
                            # No orderbook delta volume, but we simulate arrival events.
                            if (
                                remaining_size > 0
                                and random.random() < self.fill_probability_price_improves
                            ):
                                # Seller arrival! Simulate arrival volume.
                                arrival_volume = max(
                                    1, min(remaining_size, self.simulated_volume_per_poll)
                                )
                                fill_size = min(remaining_size, arrival_volume)
                                fill_ts_ms = now_ms
                                fill = Fill(
                                    id=str(uuid.uuid4()),
                                    order_id=order.id,
                                    fill_ts_ms=fill_ts_ms,
                                    fill_price_cents=order.quote_price_cents,
                                    fill_size=fill_size,
                                    remaining_size=remaining_size - fill_size,
                                    queue_ahead_at_fill=0,
                                    fill_fee_cents=order.effective_entry_fee_per_contract_cents
                                    * fill_size,
                                )
                                fills.append(fill)
                                remaining_size = fill.remaining_size
                                logger.info(
                                    "paper_realistic_fill_price_improves_arrival",
                                    extra={
                                        "intent_id": order.id,
                                        "ticker": order.ticker,
                                        "side": order.side,
                                        "fill_price": order.quote_price_cents,
                                        "fill_size": fill_size,
                                        "arrival_volume": arrival_volume,
                                        "remaining": remaining_size,
                                    },
                                )
                        else:
                            # Non-price-improving order: queue model fill path.
                            # Separate queue consumption from our fill eligibility:
                            # traded_volume is first consumed by queue ahead, remaining goes to us.
                            consumed_by_ahead = min(queue_ahead, traded_volume)
                            remaining_trade = traded_volume - consumed_by_ahead

                            # Only fill if queue is cleared AND we have remaining trade volume
                            if (
                                queue_ahead <= 0
                                and remaining_trade > 0
                                and remaining_size > 0
                                and random.random() < self.fill_probability_at_best
                            ):
                                # We're at front of queue, fill is probabilistic
                                fill_size = min(remaining_size, max(1, remaining_trade))
                                fill_ts_ms = now_ms
                                fill = Fill(
                                    id=str(uuid.uuid4()),
                                    order_id=order.id,
                                    fill_ts_ms=fill_ts_ms,
                                    fill_price_cents=order.quote_price_cents,
                                    fill_size=fill_size,
                                    remaining_size=remaining_size - fill_size,
                                    queue_ahead_at_fill=queue_ahead,
                                    fill_fee_cents=order.effective_entry_fee_per_contract_cents
                                    * fill_size,
                                )
                                fills.append(fill)
                                remaining_size = fill.remaining_size
                                logger.info(
                                    "paper_realistic_fill_queue_cleared",
                                    extra={
                                        "intent_id": order.id,
                                        "ticker": order.ticker,
                                        "side": order.side,
                                        "fill_price": order.quote_price_cents,
                                        "remaining": remaining_size,
                                        "traded_volume": traded_volume,
                                        "queue_ahead_at_fill": queue_ahead,
                                    },
                                )

                    elif eligibility["price_moved_away"]:
                        # Price moved away - no fill
                        pass

                    # Update for next iteration
                    last_best_size_at_bid = best_size_at_bid

                # Check exit conditions
                # Note: Check take_profit before "filled" to allow take_profit to trigger
                # even when order is fully filled

                time_held_seconds = (now_ms - order.created_ts_ms) / 1000.0
                if time_held_seconds >= order.max_hold_seconds:
                    exit_reason = "max_hold_reached"
                    break

                # Check take_profit if we have mark price
                if order.take_profit_cents > 0.0:
                    exit_mark = cross_yes if order.side == "YES" else cross_no
                    if exit_mark is not None:
                        filled_contracts = sum(fill.fill_size for fill in fills)
                        if filled_contracts > 0:
                            # Side-aware PnL: YES profits when mark > entry, NO profits when entry > mark
                            if order.side == "YES":
                                unrealized_pnl = (
                                    exit_mark - order.quote_price_cents
                                ) * filled_contracts
                            else:
                                unrealized_pnl = (
                                    order.quote_price_cents - exit_mark
                                ) * filled_contracts
                        else:
                            unrealized_pnl = 0.0
                        if unrealized_pnl >= order.take_profit_cents:
                            exit_reason = "take_profit"
                            break

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
