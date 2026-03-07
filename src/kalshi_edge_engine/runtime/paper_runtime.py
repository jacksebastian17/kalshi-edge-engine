"""Paper intent orchestration runtime helpers."""

import asyncio
import logging
import time
from typing import Any

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import estimate_maker_fee_cents, estimate_taker_fee_cents
from kalshi_edge_engine.engine.orderbook import parse_orderbook
from kalshi_edge_engine.engine.paper import build_quote_intents
from kalshi_edge_engine.engine.paper_sim import OrderRequest, RealisticOrderSimulator
from kalshi_edge_engine.engine.signal import Signal
from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.runtime import config as runtime_config
from kalshi_edge_engine.runtime import state as runtime_state

logger = logging.getLogger(__name__)

PAPER_INTENT_STATUS_OPEN = "OPEN"
PAPER_INTENT_STATUS_FILLED = "FILLED"
PAPER_INTENT_STATUS_EXPIRED = "EXPIRED"
PAPER_INTENT_STATUS_ERROR = "ERROR"


def _init_paper_store() -> None:
    """Initialize paper store if enabled."""
    if runtime_config.ENABLE_PAPER_TRADING and runtime_state.paper_store is None:
        try:
            runtime_state.paper_store = PaperStore()
            logger.info("paper_store_initialized")
        except Exception as e:
            logger.exception("paper_store_init_error", extra={"error": str(e)})


def _can_create_intent(ticker: str, now_ms: int) -> bool:
    """Check if a new intent can be created for ticker.

    Enforces:
    - Paper trading must be enabled
    - MAX_DAILY_LOSS not breached (if configured)
    - No more than max intents per minute
    - Ticker not in cooldown

    Args:
        ticker: Market ticker.
        now_ms: Current time in milliseconds.

    Returns:
        True if intent can be created, False otherwise.
    """
    if not runtime_state.paper_enabled:
        return False

    if runtime_state.paper_daily_loss_breached:
        return False

    cooldown_until = runtime_state.paper_ticker_cooldown.get(ticker)
    if cooldown_until is not None and now_ms < cooldown_until:
        return False

    minute_ago_ms = now_ms - 60_000
    runtime_state.paper_intent_timestamps[:] = [
        ts for ts in runtime_state.paper_intent_timestamps if ts >= minute_ago_ms
    ]

    return len(runtime_state.paper_intent_timestamps) < runtime_config.PAPER_MAX_INTENTS_PER_MINUTE


def _get_rate_limit_or_cooldown_reason(ticker: str, now_ms: int) -> str | None:
    """Get skip reason for intent gating by cooldown/rate-limit/safety gates."""
    if not runtime_state.paper_enabled:
        return "paper_disabled"

    if runtime_state.paper_daily_loss_breached:
        return "max_daily_loss_breached"

    cooldown_until = runtime_state.paper_ticker_cooldown.get(ticker)
    if cooldown_until is not None and now_ms < cooldown_until:
        return "cooldown"

    minute_ago_ms = now_ms - 60_000
    runtime_state.paper_intent_timestamps[:] = [
        ts for ts in runtime_state.paper_intent_timestamps if ts >= minute_ago_ms
    ]

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

    intents = build_quote_intents(signal)

    for task in list(runtime_state.paper_simulation_tasks):
        if task.done():
            runtime_state.paper_simulation_tasks.discard(task)

    for intent in intents:
        if intent.net_edge < runtime_config.PAPER_MIN_EDGE_CENTS:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "min_edge",
                    "net_edge": intent.net_edge,
                    "min_edge": runtime_config.PAPER_MIN_EDGE_CENTS,
                },
            )
            continue

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

            runtime_state.paper_store.insert_intent(
                intent,
                entry_fee_cents=entry_fee_cents,
                take_profit_cents_per_contract=runtime_config.PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT,
                max_hold_seconds=runtime_config.PAPER_MAX_HOLD_SECONDS,
                best_bid_at_submit_cents=best_bid_at_submit_cents,
                cross_at_submit_cents=cross_at_submit_cents,
            )
            runtime_state.paper_store.update_intent_status(intent.id, PAPER_INTENT_STATUS_OPEN)
            runtime_state.paper_intent_timestamps.append(now_ms)
            runtime_state.paper_ticker_cooldown[intent.ticker] = now_ms + (
                runtime_config.PAPER_TICKER_COOLDOWN_SECONDS * 1000
            )
        except Exception as e:
            logger.exception(
                "paper_intent_store_error",
                extra={"intent_id": intent.id, "error": str(e)},
            )
            continue

        async def _simulate_task(intent_param, entry_fee_param: float) -> None:
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
                    max_hold_seconds=runtime_config.PAPER_MAX_HOLD_SECONDS,
                    take_profit_cents_per_contract=runtime_config.PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT,
                    entry_fee_per_contract_cents=(
                        entry_fee_cents / intent_param.size_contracts
                        if intent_param.size_contracts > 0
                        else 0.0
                    ),
                )

                simulator = RealisticOrderSimulator(
                    poll_interval_seconds=runtime_config.PAPER_POLL_INTERVAL_SECONDS,
                    fill_probability_price_moved=runtime_config.PAPER_FILL_PROBABILITY,
                    fill_probability_at_best=runtime_config.PAPER_FILL_PROBABILITY_AT_BEST,
                    fill_probability_price_improves=runtime_config.PAPER_FILL_PROBABILITY_PRICE_IMPROVES,
                    simulated_volume_per_poll=runtime_config.PAPER_SIMULATED_VOLUME_PER_POLL,
                )

                def _on_simulator_poll(runtime_update: dict[str, Any]) -> None:
                    if store is None:
                        return
                    store.update_intent_status(
                        intent_param.id,
                        PAPER_INTENT_STATUS_OPEN,
                        last_seen_best_bid=runtime_update.get("last_seen_best_bid"),
                        queue_ahead=runtime_update.get("queue_ahead_contracts"),
                    )

                fills, order_state = await simulator.simulate_order_fills(
                    client,
                    order_request,
                    horizon_seconds=runtime_config.PAPER_FILL_HORIZON_SECONDS,
                    on_poll=_on_simulator_poll,
                )

                total_filled = 0
                for fill in fills:
                    intent_record = store.get_intent_record(fill.order_id)
                    if intent_record is None:
                        logger.warning(
                            "paper_fill_skip_intent_not_found",
                            extra={"intent_id": fill.order_id},
                        )
                        continue

                    size_contracts = int(intent_record["size_contracts"])
                    filled_so_far = store.get_filled_size_for_intent(fill.order_id)

                    if filled_so_far >= size_contracts:
                        logger.warning(
                            "paper_fill_skip_already_filled",
                            extra={
                                "intent_id": fill.order_id,
                                "ticker": intent_param.ticker,
                                "side": intent_param.side,
                                "filled_so_far": filled_so_far,
                                "size_contracts": size_contracts,
                            },
                        )
                        continue

                    remaining_after = max(0, size_contracts - (filled_so_far + fill.fill_size))

                    fill_insert_result = store.insert_fill_result(
                        fill_id=fill.id,
                        intent_id=fill.order_id,
                        fill_ts_ms=fill.fill_ts_ms,
                        fill_price_cents=fill.fill_price_cents,
                        fill_size=fill.fill_size,
                        remaining_size=remaining_after,
                        queue_ahead_at_fill=fill.queue_ahead_at_fill,
                        fill_fee_cents=fill.fill_fee_cents,
                        best_bid_at_fill_cents=fill.best_bid_at_fill_cents,
                        cross_at_fill_cents=fill.cross_at_fill_cents,
                        note=(
                            f"entry_{fill.fill_path_type}"
                            if fill.fill_path_type is not None
                            else None
                        ),
                    )

                    if not fill_insert_result.inserted_new_fill:
                        logger.warning(
                            "paper_fill_insert_rejected_skip_position_mutation",
                            extra={
                                "intent_id": fill.order_id,
                                "fill_id": fill.id,
                                "ticker": intent_param.ticker,
                                "side": intent_param.side,
                                "fill_size": fill.fill_size,
                                "insert_status": fill_insert_result.status,
                            },
                        )
                        continue

                    total_filled += fill.fill_size

                    if fill.fill_size > 0:
                        positions = store.get_positions()
                        current_pos = next(
                            (
                                p
                                for p in positions
                                if p["ticker"] == intent_param.ticker
                                and p["side"] == intent_param.side
                            ),
                            None,
                        )

                        if current_pos:
                            total_cost = (
                                current_pos["avg_entry_price_cents"] * current_pos["net_contracts"]
                                + fill.fill_price_cents * fill.fill_size
                            )
                            new_contracts = current_pos["net_contracts"] + fill.fill_size
                            new_avg_price = total_cost / new_contracts if new_contracts > 0 else 0.0
                            store.upsert_position(
                                ticker=intent_param.ticker,
                                side=intent_param.side,
                                net_contracts=new_contracts,
                                avg_entry_price_cents=new_avg_price,
                                realized_pnl_cents=current_pos["realized_pnl_cents"],
                                first_open_ts_ms=current_pos["first_open_ts_ms"],
                            )
                        else:
                            store.upsert_position(
                                ticker=intent_param.ticker,
                                side=intent_param.side,
                                net_contracts=fill.fill_size,
                                avg_entry_price_cents=fill.fill_price_cents,
                                realized_pnl_cents=0.0,
                                first_open_ts_ms=intent_param.created_ts_ms,
                            )

                entry_order_exit_reason = order_state.get("exit_reason", "timeout")
                last_seen_best_bid = order_state.get("last_seen_best_bid")
                queue_ahead_contracts = order_state.get("queue_ahead_contracts")

                total_filled_now = store.get_filled_size_for_intent(intent_param.id)
                intent_size = intent_param.size_contracts
                current_intent_status = store.get_intent_record(intent_param.id)

                if total_filled_now >= intent_size:
                    existing_status = (
                        current_intent_status.get("status", PAPER_INTENT_STATUS_OPEN)
                        if current_intent_status
                        else PAPER_INTENT_STATUS_OPEN
                    )
                    if existing_status != PAPER_INTENT_STATUS_FILLED:
                        store.update_intent_status(
                            intent_param.id,
                            PAPER_INTENT_STATUS_FILLED,
                            last_seen_best_bid=last_seen_best_bid,
                            queue_ahead=queue_ahead_contracts,
                        )
                else:
                    store.update_intent_status(
                        intent_param.id,
                        PAPER_INTENT_STATUS_EXPIRED,
                        last_seen_best_bid=last_seen_best_bid,
                        queue_ahead=queue_ahead_contracts,
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
                        intent_param.id, PAPER_INTENT_STATUS_ERROR
                    )

        task = asyncio.create_task(_simulate_task(intent, entry_fee_cents))
        runtime_state.paper_simulation_tasks.add(task)

        def _cleanup(t: asyncio.Task[Any]) -> None:
            runtime_state.paper_simulation_tasks.discard(t)

        task.add_done_callback(_cleanup)
