"""Paper intent orchestration runtime helpers."""

import asyncio
import logging
import time
from typing import Any

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import estimate_maker_fee_cents, estimate_taker_fee_cents
from kalshi_edge_engine.engine.orderbook import parse_orderbook
from kalshi_edge_engine.engine.paper import QuoteIntent, build_quote_intents
from kalshi_edge_engine.engine.paper_sim import Fill, OrderRequest, RealisticOrderSimulator
from kalshi_edge_engine.engine.signal import SIGNAL_VALID, Signal
from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.runtime import config as runtime_config
from kalshi_edge_engine.runtime import state as runtime_state

logger = logging.getLogger(__name__)

PAPER_INTENT_STATUS_OPEN = "OPEN"
PAPER_INTENT_STATUS_FILLED = "FILLED"
PAPER_INTENT_STATUS_EXPIRED = "EXPIRED"
PAPER_INTENT_STATUS_ERROR = "ERROR"

# Global gates are intentionally portfolio-level safety controls shared across
# all universes. Universe-specific gates are resolved via
# runtime_config.get_effective_paper_config(universe).
#
# Global controls:
# - ENABLE_PAPER_TRADING / runtime_state.paper_enabled
# - PAPER_MAX_CONCURRENT_INTENTS
# - PAPER_MAX_OPEN_ORDERS
# - PAPER_MAX_OPEN_POSITIONS
# - PAPER_MAX_POS_PER_TICKER (intentional global decision)
# - PAPER_MAX_INTENTS_PER_MINUTE
# - PAPER_MAX_DAILY_LOSS_CENTS


def _is_entry_fill_allowed_by_path_policy(
    *,
    fill_path_type: str | None,
    intent_net_edge: float,
    config: runtime_config.PaperRuntimeConfig,
) -> tuple[bool, str | None]:
    """Return whether an entry fill is accepted under configured path policy."""
    path = (fill_path_type or "unknown").strip().lower()

    if path == "price_improves":
        if not config.allow_price_improves:
            return (False, "fill_path_price_improves_disabled")
        min_edge_for_path = config.min_edge_cents_price_improves
        if min_edge_for_path is not None and float(intent_net_edge) < float(min_edge_for_path):
            return (False, "fill_path_price_improves_min_edge")

    if path == "moved_through":
        if not config.allow_moved_through:
            return (False, "fill_path_moved_through_disabled")
        min_edge_for_path = config.min_edge_cents_moved_through
        if min_edge_for_path is not None and float(intent_net_edge) < float(min_edge_for_path):
            return (False, "fill_path_moved_through_min_edge")

    return (True, None)


def _init_paper_store() -> None:
    """Initialize paper store if enabled."""
    if runtime_config.ENABLE_PAPER_TRADING and runtime_state.paper_store is None:
        try:
            store = PaperStore()
            recovered = store.recover_interrupted_intents()
            runtime_state.paper_store = store
            runtime_state.paper_liquidation_marks = {}
            logger.info("paper_intents_recovered", extra={"count": recovered})
            logger.info("paper_store_initialized")
        except Exception as e:
            logger.exception("paper_store_init_error", extra={"error": str(e)})


def _get_rate_limit_or_cooldown_reason(ticker: str, now_ms: int) -> str | None:
    """Get skip reason for intent gating by cooldown/rate-limit/safety gates."""
    if not runtime_state.paper_enabled:
        return "paper_disabled"

    store = runtime_state.paper_store
    limit = runtime_config.PAPER_MAX_DAILY_LOSS_CENTS
    if store is not None and limit > 0:
        risk = store.evaluate_daily_loss(
            now_ms=now_ms,
            max_loss_cents=limit,
            liquidation_marks=runtime_state.paper_liquidation_marks,
        )
        if risk["daily_loss_breached"]:
            return "max_daily_loss_breached"

    cooldown_until = runtime_state.paper_ticker_cooldown.get(ticker)
    if cooldown_until is not None and now_ms < cooldown_until:
        return "cooldown"

    minute_ago_ms = now_ms - 60_000
    runtime_state.paper_intent_timestamps[:] = [
        ts for ts in runtime_state.paper_intent_timestamps if ts >= minute_ago_ms
    ]

    # Intentionally global limiter, shared across all universes.
    if len(runtime_state.paper_intent_timestamps) >= runtime_config.PAPER_MAX_INTENTS_PER_MINUTE:
        return "rate_limit"

    return None


async def _process_paper_intents(
    signal: Signal,
    client: KalshiClient,
) -> None:
    """Process signal to create and simulate paper intents.

    Args:
        signal: Signal to process.
        client: KalshiClient instance.
    """
    if (
        not runtime_config.ENABLE_PAPER_TRADING
        or runtime_state.paper_store is None
        or not runtime_state.paper_enabled
    ):
        return

    now_ms = int(time.time() * 1000)

    effective_signal_config = runtime_config.get_effective_paper_config(signal.universe)

    if effective_signal_config.require_signal_valid_only and signal.reason_code != SIGNAL_VALID:
        logger.info(
            "paper_intent_skipped",
            extra={
                "ticker": signal.ticker,
                "universe": signal.universe,
                "reason": "signal_not_valid",
                "signal_reason_code": signal.reason_code,
            },
        )
        return

    intents = build_quote_intents(signal)

    for task in list(runtime_state.paper_simulation_tasks):
        if task.done():
            runtime_state.paper_simulation_tasks.discard(task)

    for intent in intents:
        universe_key = getattr(intent, "universe", signal.universe)
        effective_config = runtime_config.get_effective_paper_config(universe_key)

        if intent.side == "YES" and not effective_config.allow_yes_entries:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "universe": universe_key,
                    "reason": "side_disabled",
                },
            )
            continue

        if intent.side == "NO" and not effective_config.allow_no_entries:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "universe": universe_key,
                    "reason": "side_disabled",
                },
            )
            continue

        if intent.side == "YES":
            capturable_edge_to_cross = signal.capturable_edge_to_cross_yes_cents
            intended_quote = signal.intended_quote_yes_cents

            if capturable_edge_to_cross is None or intended_quote is None:
                logger.info(
                    "paper_intent_skipped",
                    extra={
                        "ticker": intent.ticker,
                        "side": intent.side,
                        "reason": "missing_entry_selectivity_metric",
                        "capturable_edge_to_cross_yes_cents": capturable_edge_to_cross,
                        "intended_quote_yes_cents": intended_quote,
                    },
                )
                continue

            if capturable_edge_to_cross < effective_config.min_capturable_edge_to_cross_cents:
                logger.info(
                    "paper_intent_skipped",
                    extra={
                        "ticker": intent.ticker,
                        "side": intent.side,
                        "universe": universe_key,
                        "reason": "min_capturable_edge_to_cross",
                        "capturable_edge_to_cross_yes_cents": capturable_edge_to_cross,
                        "min_capturable_edge_to_cross_cents": (
                            effective_config.min_capturable_edge_to_cross_cents
                        ),
                    },
                )
                continue

            abs_distance_from_50_cents = abs(float(intended_quote) - 50.0)
            if abs_distance_from_50_cents < effective_config.min_abs_distance_from_50_cents:
                logger.info(
                    "paper_intent_skipped",
                    extra={
                        "ticker": intent.ticker,
                        "side": intent.side,
                        "universe": universe_key,
                        "reason": "min_abs_distance_from_50",
                        "abs_distance_from_50_cents": abs_distance_from_50_cents,
                        "min_abs_distance_from_50_cents": (
                            effective_config.min_abs_distance_from_50_cents
                        ),
                    },
                )
                continue

        if signal.top_liquidity < effective_config.min_top_liquidity:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "universe": universe_key,
                    "reason": "min_top_liquidity",
                    "top_liquidity": signal.top_liquidity,
                    "min_top_liquidity": effective_config.min_top_liquidity,
                },
            )
            continue

        if intent.net_edge < effective_config.min_edge_cents:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "universe": universe_key,
                    "reason": "min_edge",
                    "net_edge": intent.net_edge,
                    "min_edge": effective_config.min_edge_cents,
                },
            )
            continue

        # Portfolio-level cap on all open intents, intentionally global.
        open_intent_count = runtime_state.paper_store.count_intents_by_status(
            PAPER_INTENT_STATUS_OPEN
        )
        if open_intent_count >= runtime_config.PAPER_MAX_OPEN_ORDERS:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "max_open_orders",
                },
            )
            continue

        # Portfolio-level cap on active simulation tasks, intentionally global.
        active_sim_tasks = len(runtime_state.paper_simulation_tasks)
        if active_sim_tasks >= runtime_config.PAPER_MAX_CONCURRENT_INTENTS:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "max_concurrent_intents",
                },
            )
            continue

        # Intentional global-by-ticker cap: a ticker is treated as one exposure
        # bucket regardless of universe label so concentration limits remain
        # conservative and easy to reason about at the portfolio level.
        ticker_position_count = runtime_state.paper_store.count_open_positions_for_ticker(
            intent.ticker
        )
        if ticker_position_count >= runtime_config.PAPER_MAX_POS_PER_TICKER:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "max_pos_per_ticker",
                },
            )
            continue

        # Portfolio-level cap on total open positions, intentionally global.
        open_positions_count = len(runtime_state.paper_store.get_positions())
        if open_positions_count >= runtime_config.PAPER_MAX_OPEN_POSITIONS:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "max_open_positions",
                },
            )
            continue

        if runtime_state.paper_store.has_open_intent_for_ticker_side(intent.ticker, intent.side):
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "duplicate_open_intent",
                },
            )
            continue

        max_entry_samples_per_ticker_per_hour = (
            effective_config.max_entry_samples_per_ticker_per_hour
        )
        if max_entry_samples_per_ticker_per_hour is not None:
            recent_entry_samples_for_ticker = (
                runtime_state.paper_store.count_recent_entry_samples_for_ticker(
                    ticker=intent.ticker,
                    within_seconds=3600,
                )
            )
            if recent_entry_samples_for_ticker >= int(max_entry_samples_per_ticker_per_hour):
                logger.info(
                    "paper_intent_skipped",
                    extra={
                        "ticker": intent.ticker,
                        "side": intent.side,
                        "universe": universe_key,
                        "reason": "max_entry_samples_per_ticker_per_hour",
                        "recent_entry_samples_for_ticker": recent_entry_samples_for_ticker,
                        "max_entry_samples_per_ticker_per_hour": (
                            max_entry_samples_per_ticker_per_hour
                        ),
                    },
                )
                continue

        max_entry_samples_per_ticker_side_per_hour = (
            effective_config.max_entry_samples_per_ticker_side_per_hour
        )
        if max_entry_samples_per_ticker_side_per_hour is not None:
            recent_entry_samples_for_ticker_side = (
                runtime_state.paper_store.count_recent_entry_samples_for_ticker_side(
                    ticker=intent.ticker,
                    side=intent.side,
                    within_seconds=3600,
                )
            )
            if recent_entry_samples_for_ticker_side >= int(
                max_entry_samples_per_ticker_side_per_hour
            ):
                logger.info(
                    "paper_intent_skipped",
                    extra={
                        "ticker": intent.ticker,
                        "side": intent.side,
                        "universe": universe_key,
                        "reason": "max_entry_samples_per_ticker_side_per_hour",
                        "recent_entry_samples_for_ticker_side": (
                            recent_entry_samples_for_ticker_side
                        ),
                        "max_entry_samples_per_ticker_side_per_hour": (
                            max_entry_samples_per_ticker_side_per_hour
                        ),
                    },
                )
                continue

        min_reentry_delay_seconds = effective_config.min_reentry_delay_seconds_same_ticker_side
        if min_reentry_delay_seconds > 0:
            latest_entry_created_ts_ms = (
                runtime_state.paper_store.get_latest_entry_created_ts_ms_for_ticker_side(
                    intent.ticker,
                    intent.side,
                )
            )
            if latest_entry_created_ts_ms is not None:
                elapsed_ms = now_ms - int(latest_entry_created_ts_ms)
                if elapsed_ms < (int(min_reentry_delay_seconds) * 1000):
                    logger.info(
                        "paper_intent_skipped",
                        extra={
                            "ticker": intent.ticker,
                            "side": intent.side,
                            "reason": "reentry_delay_same_ticker_side",
                            "elapsed_ms": elapsed_ms,
                            "min_reentry_delay_seconds_same_ticker_side": (
                                min_reentry_delay_seconds
                            ),
                        },
                    )
                    continue

        gating_reason = _get_rate_limit_or_cooldown_reason(intent.ticker, now_ms)
        if gating_reason is not None:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": gating_reason,
                },
            )
            continue

        try:
            fee_func = (
                estimate_maker_fee_cents if intent.fee_mode == "maker" else estimate_taker_fee_cents
            )
            entry_fee_cents = fee_func(intent.quote_price_cents, intent.size_contracts)

            best_bid_at_submit_cents: float | None = None
            cross_at_submit_cents: float | None = None
            try:
                orderbook_snapshot = await client.get_orderbook(intent.ticker)
                parsed_ob = parse_orderbook(orderbook_snapshot)
                if intent.side == "YES":
                    best_bid_at_submit_cents = (
                        parsed_ob.bid_yes.price if parsed_ob.bid_yes else None
                    )
                    cross_at_submit_cents = (
                        parsed_ob.cross_yes.price if parsed_ob.cross_yes else None
                    )
                else:
                    best_bid_at_submit_cents = parsed_ob.bid_no.price if parsed_ob.bid_no else None
                    cross_at_submit_cents = parsed_ob.cross_no.price if parsed_ob.cross_no else None
            except Exception as e:
                logger.warning(
                    "paper_intent_telemetry_snapshot_failed",
                    extra={"intent_id": intent.id, "error": str(e)},
                )

            now_ms = int(time.time() * 1000)
            if _get_rate_limit_or_cooldown_reason(intent.ticker, now_ms) is not None:
                continue

            runtime_state.paper_store.insert_intent(
                intent,
                universe=intent.universe,
                entry_fee_cents=entry_fee_cents,
                take_profit_cents_per_contract=effective_config.take_profit_cents_per_contract,
                max_hold_seconds=effective_config.max_hold_seconds,
                best_bid_at_submit_cents=best_bid_at_submit_cents,
                cross_at_submit_cents=cross_at_submit_cents,
            )
            runtime_state.paper_intent_timestamps.append(now_ms)
            runtime_state.paper_ticker_cooldown[intent.ticker] = now_ms + (
                effective_config.ticker_cooldown_seconds * 1000
            )
        except Exception as e:
            logger.exception(
                "paper_intent_store_error",
                extra={"intent_id": intent.id, "error": str(e)},
            )
            continue

        async def _simulate_task(
            intent_param: QuoteIntent,
            entry_fee_param: float,
            effective_config_param: runtime_config.PaperRuntimeConfig,
        ) -> None:
            try:
                store = runtime_state.paper_store
                if store is None:
                    return

                entry_fee_cents = entry_fee_param

                order_request = OrderRequest(
                    id=intent_param.id,
                    ticker=intent_param.ticker,
                    side=intent_param.side,
                    quote_price_cents=intent_param.quote_price_cents,
                    size_contracts=intent_param.size_contracts,
                    created_ts_ms=intent_param.created_ts_ms,
                    net_edge=intent_param.net_edge,
                    entry_fee_cents=entry_fee_cents,
                    max_hold_seconds=effective_config_param.max_hold_seconds,
                    take_profit_cents_per_contract=effective_config_param.take_profit_cents_per_contract,
                    entry_fee_per_contract_cents=(
                        entry_fee_cents / intent_param.size_contracts
                        if intent_param.size_contracts > 0
                        else 0.0
                    ),
                )

                simulator = RealisticOrderSimulator(
                    poll_interval_seconds=effective_config_param.poll_interval_seconds,
                    fill_probability_price_moved=effective_config_param.fill_probability_price_moved,
                    fill_probability_at_best=effective_config_param.fill_probability_at_best,
                    fill_probability_price_improves=effective_config_param.fill_probability_price_improves,
                    simulated_volume_per_poll=effective_config_param.simulated_volume_per_poll,
                )

                total_filled = 0
                processed_fill_ids: set[str] = set()
                fee_for_fill = (
                    estimate_maker_fee_cents
                    if intent_param.fee_mode == "maker"
                    else estimate_taker_fee_cents
                )

                def persist_fill(fill: Fill) -> bool:
                    nonlocal total_filled
                    if fill.id in processed_fill_ids:
                        return False
                    processed_fill_ids.add(fill.id)
                    allowed, reason = _is_entry_fill_allowed_by_path_policy(
                        fill_path_type=fill.fill_path_type,
                        intent_net_edge=intent_param.net_edge,
                        config=effective_config_param,
                    )
                    if not allowed:
                        logger.info(
                            "paper_fill_rejected_by_path_policy",
                            extra={"fill_id": fill.id, "reason": reason},
                        )
                        return False
                    result = store.insert_fill_result(
                        fill_id=fill.id,
                        intent_id=fill.order_id,
                        fill_ts_ms=fill.fill_ts_ms,
                        fill_price_cents=fill.fill_price_cents,
                        fill_size=fill.fill_size,
                        remaining_size=fill.remaining_size,
                        queue_ahead_at_fill=fill.queue_ahead_at_fill,
                        fill_fee_cents=fee_for_fill(fill.fill_price_cents, fill.fill_size),
                        best_bid_at_fill_cents=fill.best_bid_at_fill_cents,
                        cross_at_fill_cents=fill.cross_at_fill_cents,
                        note=f"entry_{fill.fill_path_type or 'unknown'}",
                        execution_source=fill.execution_source,
                        apply_to_position=True,
                    )
                    if result.inserted_new_fill:
                        total_filled += fill.fill_size
                        return True
                    logger.info(
                        "paper_fill_insert_skipped",
                        extra={"fill_id": fill.id, "insert_status": result.status},
                    )
                    return False

                def _on_simulator_poll(runtime_update: dict[str, Any]) -> bool | None:
                    fill = runtime_update.get("fill")
                    if isinstance(fill, Fill):
                        return persist_fill(fill)
                    store.update_intent_status(
                        intent_param.id,
                        PAPER_INTENT_STATUS_OPEN,
                        last_seen_best_bid=runtime_update.get("last_seen_best_bid"),
                        queue_ahead=runtime_update.get("queue_ahead_contracts"),
                        only_if_open=True,
                    )
                    return None

                fills, order_state = await simulator.simulate_order_fills(
                    client,
                    order_request,
                    horizon_seconds=effective_config_param.fill_horizon_seconds,
                    on_poll=_on_simulator_poll,
                )
                for fill in fills:
                    persist_fill(fill)

                entry_order_exit_reason = order_state.get("exit_reason", "timeout")
                store.update_intent_status(
                    intent_param.id,
                    PAPER_INTENT_STATUS_EXPIRED,
                    last_seen_best_bid=order_state.get("last_seen_best_bid"),
                    queue_ahead=order_state.get("queue_ahead_contracts"),
                    only_if_open=True,
                )

                logger.info(
                    "paper_order_simulation_complete",
                    extra={
                        "intent_id": intent_param.id,
                        "ticker": intent_param.ticker,
                        "side": intent_param.side,
                        "total_filled": total_filled,
                        "entry_order_exit_reason": entry_order_exit_reason,
                    },
                )

            except Exception as e:
                logger.exception(
                    "paper_simulation_task_error",
                    extra={"intent_id": intent_param.id, "error": str(e)},
                )
                if runtime_state.paper_store is not None:
                    runtime_state.paper_store.update_intent_status(
                        intent_param.id, PAPER_INTENT_STATUS_ERROR, only_if_open=True
                    )

        task = asyncio.create_task(_simulate_task(intent, entry_fee_cents, effective_config))
        runtime_state.paper_simulation_tasks.add(task)

        def _cleanup(
            t: asyncio.Task[Any],
            intent_id: str = intent.id,
            store: PaperStore | None = runtime_state.paper_store,
        ) -> None:
            if t.cancelled() and store is not None:
                store.update_intent_status(intent_id, "CANCELED", only_if_open=True)
            runtime_state.paper_simulation_tasks.discard(t)

        task.add_done_callback(_cleanup)
