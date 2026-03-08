"""Paper position mark/exit and fill telemetry runtime helpers."""

import logging

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import estimate_taker_fee_cents
from kalshi_edge_engine.engine.orderbook import parse_orderbook
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.position_exit import (
    compute_price_delta_cents_per_contract,
    compute_realized_exit_pnl_cents,
    should_take_profit,
)
from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.runtime import config as runtime_config
from kalshi_edge_engine.runtime import state as runtime_state

logger = logging.getLogger(__name__)


def _get_mark_price_for_position(
    ticker: str,
    side: str,
    snapshot: object | None,
) -> float | None:
    """Get current mark price for a position from top-signals snapshot."""
    if snapshot is None:
        return None

    signals = getattr(snapshot, "signals", None)
    if signals is None:
        return None

    for signal_resp in signals:
        if signal_resp.ticker != ticker:
            continue
        mark_price = signal_resp.cross_yes if side == "YES" else signal_resp.cross_no

        if mark_price is None:
            return None
        return float(mark_price)
    return None


async def _fetch_mark_price_cents(
    client: KalshiClient,
    ticker: str,
    side: str,
) -> float | None:
    """Fetch current mark price from live orderbook for a ticker+side.

    Returns:
        Cross price in cents for the requested side, or None when unavailable.
    """
    try:
        orderbook = await client.get_orderbook(ticker)
        parsed_orderbook = parse_orderbook(orderbook)
        if side == "YES":
            return parsed_orderbook.cross_yes.price if parsed_orderbook.cross_yes else None
        return parsed_orderbook.cross_no.price if parsed_orderbook.cross_no else None
    except Exception as e:
        logger.warning(
            "paper_mark_price_fetch_failed",
            extra={"ticker": ticker, "side": side, "error": str(e)},
        )
        return None


async def _update_fill_30s_outcomes(*, client: KalshiClient, now_ms: int) -> None:
    """Backfill 30s post-fill mark and side-aware short-horizon PnL telemetry."""
    if runtime_state.paper_store is None:
        return

    cutoff_ts_ms = now_ms - 30_000
    pending = runtime_state.paper_store.get_pending_fill_30s_outcome_updates(
        cutoff_ts_ms=cutoff_ts_ms,
        limit=200,
    )
    if not pending:
        return

    updated_count = 0
    for row in pending:
        mark_price = _get_mark_price_for_position(
            ticker=row["ticker"],
            side=row["side"],
            snapshot=runtime_state.top_signals_snapshot,
        )
        if mark_price is None:
            mark_price = await _fetch_mark_price_cents(
                client,
                row["ticker"],
                row["side"],
            )
        if mark_price is None:
            continue

        pnl_30s_after_fill_cents = PaperStore.compute_short_horizon_fill_pnl_cents(
            side=str(row["side"]),
            fill_price_cents=float(row["fill_price_cents"]),
            fill_size=int(row["fill_size"]),
            mark_price_cents=float(mark_price),
            fill_fee_cents=(
                float(row["fill_fee_cents"]) if row.get("fill_fee_cents") is not None else None
            ),
        )
        did_update = runtime_state.paper_store.update_fill_30s_outcome(
            fill_id=str(row["fill_id"]),
            mark_30s_after_fill_cents=float(mark_price),
            pnl_30s_after_fill_cents=pnl_30s_after_fill_cents,
        )
        if did_update:
            updated_count += 1

    if updated_count > 0:
        logger.info(
            "paper_fill_30s_outcomes_updated",
            extra={
                "updated_count": updated_count,
                "pending_count": len(pending),
            },
        )


def _close_position(
    *,
    ticker: str,
    side: str,
    net_contracts: int,
    avg_entry_price_cents: float,
    realized_pnl_cents: float,
    first_open_ts_ms: int,
    mark_price_cents: float | None,
    now_ms: int,
    reason: str,
) -> bool:
    """Close an open position and persist fill/position/intent lifecycle updates."""
    if runtime_state.paper_store is None or net_contracts <= 0 or mark_price_cents is None:
        return False

    intent_id = f"exit_intent:{reason}:{ticker}:{side}:{now_ms}"
    runtime_state.paper_store.insert_intent(
        QuoteIntent(
            id=intent_id,
            ticker=ticker,
            side=side,
            quote_price_cents=mark_price_cents,
            size_contracts=net_contracts,
            created_ts_ms=now_ms,
            net_edge=0.0,
            inside_width=0.0,
            imbalance_gap=None,
            fee_mode="taker",
            round_trip=False,
        ),
        entry_fee_cents=0.0,
        take_profit_cents_per_contract=runtime_config.PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT,
        max_hold_seconds=runtime_config.PAPER_MAX_HOLD_SECONDS,
    )

    exit_fee_cents = estimate_taker_fee_cents(mark_price_cents, net_contracts)
    price_delta = compute_price_delta_cents_per_contract(
        side=side,
        avg_entry_price_cents=avg_entry_price_cents,
        mark_price_cents=mark_price_cents,
    )
    realized_increment_cents = compute_realized_exit_pnl_cents(
        side=side,
        avg_entry_price_cents=avg_entry_price_cents,
        mark_price_cents=mark_price_cents,
        net_contracts=net_contracts,
        exit_fee_cents=exit_fee_cents,
    )
    entry_fees_cents = runtime_state.paper_store.get_open_lifecycle_entry_fees_cents(
        ticker=ticker,
        side=side,
        first_open_ts_ms=first_open_ts_ms,
    )
    realized_increment_cents -= entry_fees_cents

    exit_fill_result = runtime_state.paper_store.insert_fill_result(
        fill_id=f"exit:{reason}:{ticker}:{side}:{now_ms}",
        intent_id=intent_id,
        fill_ts_ms=now_ms,
        fill_price_cents=mark_price_cents,
        fill_size=net_contracts,
        remaining_size=0,
        queue_ahead_at_fill=0,
        fill_fee_cents=exit_fee_cents,
        best_bid_at_fill_cents=mark_price_cents,
        cross_at_fill_cents=mark_price_cents,
        note=f"exit_{reason}",
    )
    if not exit_fill_result.inserted_new_fill:
        if exit_fill_result.already_exists:
            logger.info(
                "paper_close_position_exit_fill_idempotent",
                extra={"reason": reason, "ticker": ticker, "side": side},
            )
            return True
        logger.warning(
            "paper_close_position_exit_fill_rejected",
            extra={
                "reason": reason,
                "ticker": ticker,
                "side": side,
                "insert_status": exit_fill_result.status,
            },
        )
        return False

    runtime_state.paper_store.upsert_position(
        ticker=ticker,
        side=side,
        net_contracts=0,
        avg_entry_price_cents=avg_entry_price_cents,
        realized_pnl_cents=realized_pnl_cents + realized_increment_cents,
        first_open_ts_ms=first_open_ts_ms,
    )
    runtime_state.paper_store.mark_exited_intents_for_ticker_side(
        ticker=ticker,
        side=side,
        exit_ts_ms=now_ms,
        mark_price_cents=mark_price_cents,
    )

    logger.info(
        "paper_position_closed",
        extra={
            "reason": reason,
            "ticker": ticker,
            "side": side,
            "net_contracts": net_contracts,
            "avg_entry_price_cents": avg_entry_price_cents,
            "mark_price_cents": mark_price_cents,
            "price_delta_cents": price_delta,
            "exit_fee_cents": exit_fee_cents,
            "entry_fees_cents": entry_fees_cents,
            "realized_increment_cents": realized_increment_cents,
        },
    )
    return True


def _maybe_close_position_take_profit(
    *,
    ticker: str,
    side: str,
    net_contracts: int,
    avg_entry_price_cents: float,
    realized_pnl_cents: float,
    first_open_ts_ms: int,
    mark_price_cents: float,
    now_ms: int,
) -> bool:
    """Legacy compatibility helper for tests; active loop uses `decide_position_exit`."""
    if runtime_state.paper_store is None or net_contracts <= 0:
        return False

    if not should_take_profit(
        side=side,
        avg_entry_price_cents=avg_entry_price_cents,
        mark_price_cents=mark_price_cents,
        take_profit_cents_per_contract=runtime_config.PAPER_TAKE_PROFIT_CENTS,
    ):
        return False

    return _close_position(
        ticker=ticker,
        side=side,
        net_contracts=net_contracts,
        avg_entry_price_cents=avg_entry_price_cents,
        realized_pnl_cents=realized_pnl_cents,
        first_open_ts_ms=first_open_ts_ms,
        mark_price_cents=mark_price_cents,
        now_ms=now_ms,
        reason="take_profit",
    )
