"""FastAPI application for Kalshi Edge Engine."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import (
    estimate_maker_fee_cents,
    estimate_taker_fee_cents,
)
from kalshi_edge_engine.engine.orderbook import parse_orderbook
from kalshi_edge_engine.engine.paper import QuoteIntent, build_quote_intents
from kalshi_edge_engine.engine.paper_sim import (
    OrderRequest,
    RealisticOrderSimulator,
    compute_simulator_diagnostics,
    extract_top_of_book_snapshot,
)
from kalshi_edge_engine.engine.signal import Signal, score_signal
from kalshi_edge_engine.engine.universe import discover_nba_universe
from kalshi_edge_engine.kalshi.client import (
    KalshiAPIError,
    KalshiAuthError,
    KalshiClient,
)

# Load environment variables from .env file
load_dotenv()

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Pydantic models
class HealthResponse(BaseModel):
    """Health check response."""

    status: str


class VersionResponse(BaseModel):
    """Version information response."""

    name: str
    version: str


class SignalResponse(BaseModel):
    """Signal scoring response."""

    ticker: str
    reason_code: str
    net_edge_yes: float | None
    net_edge_no: float | None
    inside_width_yes: float | None
    inside_width_no: float | None
    bid_yes: float | None
    cross_yes: float | None
    bid_no: float | None
    cross_no: float | None
    bid_sum: float | None
    imbalance_gap: float | None
    fee_yes: float | None
    fee_no: float | None
    fee_exit_yes: float | None
    fee_exit_no: float | None
    total_fee_yes: float | None
    total_fee_no: float | None
    fee_mode: str
    round_trip: bool
    liq_yes: int
    liq_no: int
    top_liquidity: int
    timestamp_ms: int


class NBAUniverseResponse(BaseModel):
    """NBA market universe response."""

    series_ticker: str
    event_tickers: list[str]
    market_tickers: list[str]


class TopSignalsSnapshot(BaseModel):
    """Top signals snapshot response."""

    timestamp_ms: int
    signals: list[SignalResponse]
    market_count: int


class ScannerStatusResponse(BaseModel):
    """Scanner status response."""

    enabled: bool
    last_scan_ms: int | None
    markets_discovered: int
    markets_scanned_last_cycle: int
    last_cycle_duration_ms: int | None
    tickers_failed_last_cycle: list[str]
    tickers_skipped_cooldown_last_cycle: list[str]
    snapshot_timestamp_ms: int | None
    snapshot_age_ms: int | None
    last_error: str | None


class ScannerStartResponse(BaseModel):
    """Response for POST /scanner/start."""

    started: bool
    reason: str | None = None


class ScannerStopResponse(BaseModel):
    """Response for POST /scanner/stop."""

    stopped: bool
    reason: str | None = None


class PaperStartResponse(BaseModel):
    """Response for POST /paper/start."""

    started: bool
    reason: str | None = None


class PaperStopResponse(BaseModel):
    """Response for POST /paper/stop."""

    stopped: bool
    reason: str | None = None


class ForceFillRequest(BaseModel):
    """Request body for POST /paper/debug/force-fill."""

    intent_id: str
    fill_size: int = 1


# Paper trading response models
class PaperIntentResponse(BaseModel):
    """Response model for paper trading intent."""

    id: str
    ticker: str
    side: str
    quote_price_cents: float
    size_contracts: int
    created_ts_ms: int
    status: str
    last_seen_best_bid: float | None
    queue_ahead_contracts: int | None
    last_update_ts_ms: int | None


class PaperFillResponse(BaseModel):
    """Response model for a paper fill event."""

    id: str
    intent_id: str
    fill_ts_ms: int
    fill_price_cents: float
    fill_size: int
    remaining_size: int
    queue_ahead_at_fill: int | None
    fill_fee_cents: float | None
    note: str | None
    best_bid_at_fill_cents: float | None
    cross_at_fill_cents: float | None
    ticker: str
    side: str


class PaperPositionResponse(BaseModel):
    """Response model for a paper position."""

    ticker: str
    side: str
    net_contracts: int
    avg_entry_price_cents: float
    realized_pnl_cents: float
    first_open_ts_ms: int
    last_update_ts_ms: int


class PaperPnLSnapshotResponse(BaseModel):
    """Response model for a PnL snapshot."""

    ts_ms: int
    realized_pnl_cents: float
    unrealized_pnl_cents: float
    total_pnl_cents: float
    open_positions_count: int
    open_orders_count: int


class PaperTradingSummaryResponse(BaseModel):
    """Summary statistics for paper trading."""

    total_intents_created: int
    total_fills: int
    total_positions: int
    realized_pnl_cents: float
    unrealized_pnl_cents: float
    total_pnl_cents: float
    latest_snapshot_ts_ms: int | None


# Scanner state
class _ScannerState:
    """Mutable state for the background NBA scanner."""

    def __init__(self) -> None:
        self.enabled = False
        self.last_scan_ms: int | None = None
        self.markets_discovered = 0
        self.markets_scanned_last_cycle = 0
        self.last_cycle_duration_ms: int | None = None
        self.tickers_failed_last_cycle: list[str] = []
        self.tickers_skipped_cooldown_last_cycle: list[str] = []
        self.last_error: str | None = None
        self.rotating_index = 0
        self.market_tickers: list[str] = []
        self.last_universe_refresh_ms = 0
        self.cooldown_until_ms: dict[str, int] = {}

    def reset(self) -> None:
        """Reset scanner state."""
        self.last_scan_ms = None
        self.markets_discovered = 0
        self.markets_scanned_last_cycle = 0
        self.last_cycle_duration_ms = None
        self.tickers_failed_last_cycle = []
        self.tickers_skipped_cooldown_last_cycle = []
        self.last_error = None
        self.rotating_index = 0
        self.market_tickers = []
        self.last_universe_refresh_ms = 0
        self.cooldown_until_ms = {}


# Global in-memory stores
_top_signals_snapshot: TopSignalsSnapshot | None = None
_scanner_state = _ScannerState()

# Paper trading configuration (v2 - realistic)
_ENABLE_PAPER_TRADING = os.getenv("ENABLE_PAPER_TRADING", "false").lower() in (
    "true",
    "1",
    "yes",
)
_PAPER_MIN_EDGE_CENTS = float(os.getenv("PAPER_MIN_EDGE_CENTS", "1.0"))
_PAPER_MAX_CONCURRENT_INTENTS = int(os.getenv("PAPER_MAX_CONCURRENT_INTENTS", "5"))
_PAPER_MAX_OPEN_ORDERS = int(os.getenv("PAPER_MAX_OPEN_ORDERS", "20"))
_PAPER_MAX_POS_PER_TICKER = int(os.getenv("PAPER_MAX_POS_PER_TICKER", "3"))
_PAPER_MAX_OPEN_POSITIONS = int(os.getenv("PAPER_MAX_OPEN_POSITIONS", "20"))
_PAPER_TAKE_PROFIT_CENTS = float(os.getenv("PAPER_TAKE_PROFIT_CENTS", "2.0"))
_PAPER_MAX_HOLD_SECONDS = int(os.getenv("PAPER_MAX_HOLD_SECONDS", "600"))
_PAPER_FILL_HORIZON_SECONDS = int(os.getenv("PAPER_FILL_HORIZON_SECONDS", "90"))
_PAPER_POLL_INTERVAL_SECONDS = float(os.getenv("PAPER_POLL_INTERVAL_SECONDS", "3.0"))
_PAPER_FILL_PROBABILITY = float(os.getenv("PAPER_FILL_PROBABILITY", "0.8"))
_PAPER_FILL_PROBABILITY_AT_BEST = float(os.getenv("PAPER_FILL_PROBABILITY_AT_BEST", "0.15"))
_PAPER_FILL_PROBABILITY_PRICE_IMPROVES = float(
    os.getenv("PAPER_FILL_PROBABILITY_PRICE_IMPROVES", "0.05")
)
_PAPER_SIMULATED_VOLUME_PER_POLL = int(os.getenv("PAPER_SIMULATED_VOLUME_PER_POLL", "50"))
_PAPER_MAX_INTENTS_PER_MINUTE = int(os.getenv("PAPER_MAX_INTENTS_PER_MINUTE", "10"))
_PAPER_TICKER_COOLDOWN_SECONDS = int(os.getenv("PAPER_TICKER_COOLDOWN_SECONDS", "120"))
_PAPER_MAX_DAILY_LOSS_CENTS = float(os.getenv("PAPER_MAX_DAILY_LOSS_CENTS", "0"))  # 0 = disabled

# Paper trading state tracking
_paper_store: PaperStore | None = None
_paper_enabled: bool = False  # Runtime flag: starts disabled, can be toggled via API
_paper_simulation_tasks: set[asyncio.Task[Any]] = set()
_paper_pnl_snapshot_interval_ms = 30_000  # Take snapshot every 30 seconds
_last_pnl_snapshot_ts_ms = 0
_paper_ticker_cooldown: dict[str, int] = {}  # ticker -> cooldown_until_ms
_paper_intent_timestamps: list[int] = []  # Timestamps of intents created in last minute
_paper_daily_loss_breached: bool = False  # True if MAX_DAILY_LOSS breached

PAPER_INTENT_STATUS_OPEN = "OPEN"
PAPER_INTENT_STATUS_FILLED = "FILLED"
PAPER_INTENT_STATUS_TAKE_PROFIT = "TAKE_PROFIT"
PAPER_INTENT_STATUS_MAX_HOLD_REACHED = "MAX_HOLD_REACHED"
PAPER_INTENT_STATUS_EXPIRED = "EXPIRED"
PAPER_INTENT_STATUS_ERROR = "ERROR"
PAPER_INTENT_ALLOWED_STATUSES = {
    PAPER_INTENT_STATUS_OPEN,
    PAPER_INTENT_STATUS_FILLED,
    PAPER_INTENT_STATUS_TAKE_PROFIT,
    PAPER_INTENT_STATUS_MAX_HOLD_REACHED,
    PAPER_INTENT_STATUS_EXPIRED,
    PAPER_INTENT_STATUS_ERROR,
}


# Paper trading helpers
def _init_paper_store() -> None:
    """Initialize paper store if enabled."""
    global _paper_store
    if _ENABLE_PAPER_TRADING and _paper_store is None:
        try:
            _paper_store = PaperStore()
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
    # Check if paper trading is enabled
    if not _paper_enabled:
        return False

    # Check MAX_DAILY_LOSS gate
    if _paper_daily_loss_breached:
        return False

    # Check ticker cooldown
    cooldown_until = _paper_ticker_cooldown.get(ticker)
    if cooldown_until is not None and now_ms < cooldown_until:
        return False

    # Check rate limit (max intents per minute)
    minute_ago_ms = now_ms - 60_000
    _paper_intent_timestamps[:] = [
        ts for ts in _paper_intent_timestamps if ts >= minute_ago_ms
    ]  # Prune old timestamps

    return len(_paper_intent_timestamps) < _PAPER_MAX_INTENTS_PER_MINUTE


def _get_rate_limit_or_cooldown_reason(ticker: str, now_ms: int) -> str | None:
    """Get skip reason for intent gating by cooldown/rate-limit/safety gates."""
    if not _paper_enabled:
        return "paper_disabled"

    if _paper_daily_loss_breached:
        return "max_daily_loss_breached"

    cooldown_until = _paper_ticker_cooldown.get(ticker)
    if cooldown_until is not None and now_ms < cooldown_until:
        return "cooldown"

    minute_ago_ms = now_ms - 60_000
    _paper_intent_timestamps[:] = [ts for ts in _paper_intent_timestamps if ts >= minute_ago_ms]

    if len(_paper_intent_timestamps) >= _PAPER_MAX_INTENTS_PER_MINUTE:
        return "rate_limit"

    return None


def _get_mark_price_for_position(
    ticker: str,
    side: str,
    snapshot: TopSignalsSnapshot | None,
) -> float | None:
    """Get current mark price for a position from top-signals snapshot."""
    if snapshot is None:
        return None

    for signal_resp in snapshot.signals:
        if signal_resp.ticker != ticker:
            continue
        if side == "YES":
            return signal_resp.cross_yes
        return signal_resp.cross_no
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
    if _paper_store is None or net_contracts <= 0 or mark_price_cents is None:
        return False

    intent_id = f"exit_intent:{reason}:{ticker}:{side}:{now_ms}"
    _paper_store.insert_intent(
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
        take_profit_cents=_PAPER_TAKE_PROFIT_CENTS,
        max_hold_seconds=_PAPER_MAX_HOLD_SECONDS,
    )

    price_delta = (
        mark_price_cents - avg_entry_price_cents
        if side == "YES"
        else avg_entry_price_cents - mark_price_cents
    )
    exit_fee_cents = estimate_taker_fee_cents(mark_price_cents, net_contracts)
    realized_increment_cents = (price_delta * net_contracts) - exit_fee_cents

    did_insert_exit_fill = _paper_store.insert_fill(
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
    if not did_insert_exit_fill:
        logger.warning(
            "paper_close_position_exit_fill_rejected",
            extra={"reason": reason, "ticker": ticker, "side": side},
        )
        return False

    _paper_store.upsert_position(
        ticker=ticker,
        side=side,
        net_contracts=0,
        avg_entry_price_cents=avg_entry_price_cents,
        realized_pnl_cents=realized_pnl_cents + realized_increment_cents,
        first_open_ts_ms=first_open_ts_ms,
    )
    _paper_store.mark_exited_intents_for_ticker_side(
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
    """Close open position at mark when take-profit threshold is reached."""
    if _paper_store is None or net_contracts <= 0:
        return False

    price_delta = (
        mark_price_cents - avg_entry_price_cents
        if side == "YES"
        else avg_entry_price_cents - mark_price_cents
    )
    if price_delta < _PAPER_TAKE_PROFIT_CENTS:
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


async def _process_paper_intents(
    signal: Signal,
    client: KalshiClient,
) -> None:
    """Process signal to create and simulate paper intents.

    Args:
        signal: Signal to process.
        client: KalshiClient instance.
    """
    if not _ENABLE_PAPER_TRADING or _paper_store is None or not _paper_enabled:
        return

    now_ms = int(time.time() * 1000)

    # Build intents from signal
    intents = build_quote_intents(signal)

    for task in list(_paper_simulation_tasks):
        if task.done():
            _paper_simulation_tasks.discard(task)

    for intent in intents:
        if intent.net_edge < _PAPER_MIN_EDGE_CENTS:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "min_edge",
                    "net_edge": intent.net_edge,
                    "min_edge": _PAPER_MIN_EDGE_CENTS,
                },
            )
            continue

        open_intent_count = _paper_store.count_intents_by_status(PAPER_INTENT_STATUS_OPEN)
        if open_intent_count >= _PAPER_MAX_OPEN_ORDERS:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "max_open_orders",
                },
            )
            continue

        active_sim_tasks = len(_paper_simulation_tasks)
        if active_sim_tasks >= _PAPER_MAX_CONCURRENT_INTENTS:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "max_concurrent_intents",
                },
            )
            continue

        ticker_position_count = _paper_store.count_open_positions_for_ticker(intent.ticker)
        if ticker_position_count >= _PAPER_MAX_POS_PER_TICKER:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "max_pos_per_ticker",
                },
            )
            continue

        open_positions_count = len(_paper_store.get_positions())
        if open_positions_count >= _PAPER_MAX_OPEN_POSITIONS:
            logger.info(
                "paper_intent_skipped",
                extra={
                    "ticker": intent.ticker,
                    "side": intent.side,
                    "reason": "max_open_positions",
                },
            )
            continue

        if _paper_store.has_open_intent_for_ticker_side(intent.ticker, intent.side):
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

        # Store intent - precompute parameters first
        try:
            # Compute entry fee based on fee_mode
            fee_func = (
                estimate_maker_fee_cents if intent.fee_mode == "maker" else estimate_taker_fee_cents
            )
            entry_fee_cents = fee_func(intent.quote_price_cents, intent.size_contracts)

            # Capture telemetry: best_bid and cross at submit time
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

            # Persist intent with runtime parameters and telemetry
            _paper_store.insert_intent(
                intent,
                entry_fee_cents=entry_fee_cents,
                take_profit_cents=_PAPER_TAKE_PROFIT_CENTS,
                max_hold_seconds=_PAPER_MAX_HOLD_SECONDS,
                best_bid_at_submit_cents=best_bid_at_submit_cents,
                cross_at_submit_cents=cross_at_submit_cents,
            )
            _paper_store.update_intent_status(intent.id, PAPER_INTENT_STATUS_OPEN)
            _paper_intent_timestamps.append(now_ms)
            _paper_ticker_cooldown[intent.ticker] = now_ms + (_PAPER_TICKER_COOLDOWN_SECONDS * 1000)
        except Exception as e:
            logger.exception(
                "paper_intent_store_error",
                extra={"intent_id": intent.id, "error": str(e)},
            )
            continue

        # Create background simulation task
        async def _simulate_task(intent_param, entry_fee_param: float) -> None:
            try:
                entry_fee_cents = entry_fee_param

                # Create OrderRequest for RealisticOrderSimulator
                order_request = OrderRequest(
                    id=intent_param.id,
                    ticker=intent_param.ticker,
                    side=intent_param.side,
                    quote_price_cents=intent_param.quote_price_cents,
                    size_contracts=intent_param.size_contracts,
                    created_ts_ms=intent_param.created_ts_ms,
                    net_edge=intent_param.net_edge,
                    entry_fee_cents=entry_fee_cents,
                    take_profit_cents=_PAPER_TAKE_PROFIT_CENTS,
                    max_hold_seconds=_PAPER_MAX_HOLD_SECONDS,
                    entry_fee_per_contract_cents=(
                        entry_fee_cents / intent_param.size_contracts
                        if intent_param.size_contracts > 0
                        else 0.0
                    ),
                )

                # Run realistic fill simulation
                simulator = RealisticOrderSimulator(
                    poll_interval_seconds=_PAPER_POLL_INTERVAL_SECONDS,
                    fill_probability_price_moved=_PAPER_FILL_PROBABILITY,
                    fill_probability_at_best=_PAPER_FILL_PROBABILITY_AT_BEST,
                    fill_probability_price_improves=_PAPER_FILL_PROBABILITY_PRICE_IMPROVES,
                    simulated_volume_per_poll=_PAPER_SIMULATED_VOLUME_PER_POLL,
                )

                def _on_simulator_poll(runtime_update: dict[str, Any]) -> None:
                    if _paper_store is None:
                        return
                    _paper_store.update_intent_status(
                        intent_param.id,
                        PAPER_INTENT_STATUS_OPEN,
                        last_seen_best_bid=runtime_update.get("last_seen_best_bid"),
                        queue_ahead=runtime_update.get("queue_ahead_contracts"),
                    )

                fills, order_state = await simulator.simulate_order_fills(
                    client,
                    order_request,
                    horizon_seconds=_PAPER_FILL_HORIZON_SECONDS,
                    on_poll=_on_simulator_poll,
                )

                # Process fills and update positions
                total_filled = 0
                for fill in fills:
                    # Duplicate fill prevention: check if intent is already fully filled
                    intent_record = _paper_store.get_intent_record(fill.order_id)
                    if intent_record is None:
                        logger.warning(
                            "paper_fill_skip_intent_not_found",
                            extra={"intent_id": fill.order_id},
                        )
                        continue

                    size_contracts = int(intent_record["size_contracts"])
                    filled_so_far = _paper_store.get_filled_size_for_intent(fill.order_id)

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

                    # Calculate remaining after this fill
                    remaining_after = max(0, size_contracts - (filled_so_far + fill.fill_size))

                    # Insert fill to database with telemetry
                    _paper_store.insert_fill(
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
                        note=None,
                    )
                    total_filled += fill.fill_size

                    # Update position
                    if fill.fill_size > 0:
                        # Get current position
                        positions = _paper_store.get_positions()
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
                            # Update average price (simple average for now)
                            total_cost = (
                                current_pos["avg_entry_price_cents"] * current_pos["net_contracts"]
                                + fill.fill_price_cents * fill.fill_size
                            )
                            new_contracts = current_pos["net_contracts"] + fill.fill_size
                            new_avg_price = total_cost / new_contracts if new_contracts > 0 else 0.0
                            _paper_store.upsert_position(
                                ticker=intent_param.ticker,
                                side=intent_param.side,
                                net_contracts=new_contracts,
                                avg_entry_price_cents=new_avg_price,
                                realized_pnl_cents=current_pos["realized_pnl_cents"],
                                first_open_ts_ms=current_pos["first_open_ts_ms"],
                            )
                        else:
                            # New position
                            _paper_store.upsert_position(
                                ticker=intent_param.ticker,
                                side=intent_param.side,
                                net_contracts=fill.fill_size,
                                avg_entry_price_cents=fill.fill_price_cents,
                                realized_pnl_cents=0.0,
                                first_open_ts_ms=intent_param.created_ts_ms,
                            )

                # Update intent status based on fill status
                exit_reason = order_state.get("exit_reason", "timeout")
                last_seen_best_bid = order_state.get("last_seen_best_bid")
                queue_ahead_contracts = order_state.get("queue_ahead_contracts")

                # Check if intent is now fully filled
                total_filled_now = _paper_store.get_filled_size_for_intent(intent_param.id)
                intent_size = intent_param.size_contracts
                current_intent_status = _paper_store.get_intent_record(intent_param.id)

                if total_filled_now >= intent_size:
                    # Only mark as FILLED if not already filled
                    existing_status = (
                        current_intent_status.get("status", PAPER_INTENT_STATUS_OPEN)
                        if current_intent_status
                        else PAPER_INTENT_STATUS_OPEN
                    )
                    if existing_status != PAPER_INTENT_STATUS_FILLED:
                        _paper_store.update_intent_status(
                            intent_param.id,
                            PAPER_INTENT_STATUS_FILLED,
                            last_seen_best_bid=last_seen_best_bid,
                            queue_ahead=queue_ahead_contracts,
                        )
                elif exit_reason == "take_profit":
                    _paper_store.update_intent_status(
                        intent_param.id,
                        PAPER_INTENT_STATUS_TAKE_PROFIT,
                        last_seen_best_bid=last_seen_best_bid,
                        queue_ahead=queue_ahead_contracts,
                    )
                elif exit_reason == "max_hold_reached":
                    _paper_store.update_intent_status(
                        intent_param.id,
                        PAPER_INTENT_STATUS_MAX_HOLD_REACHED,
                        last_seen_best_bid=last_seen_best_bid,
                        queue_ahead=queue_ahead_contracts,
                    )
                else:
                    _paper_store.update_intent_status(
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
                        "exit_reason": exit_reason,
                    },
                )

            except Exception as e:
                logger.exception(
                    "paper_simulation_task_error",
                    extra={"intent_id": intent_param.id, "error": str(e)},
                )
                _paper_store.update_intent_status(intent_param.id, PAPER_INTENT_STATUS_ERROR)

        task = asyncio.create_task(_simulate_task(intent, entry_fee_cents))
        _paper_simulation_tasks.add(task)

        def _cleanup(t: asyncio.Task[Any]) -> None:
            _paper_simulation_tasks.discard(t)

        task.add_done_callback(_cleanup)


# Background scanner
async def _nba_scanner_loop(client: KalshiClient) -> None:
    """Background loop that scans NBA markets incrementally with predictable cadence.

    - Discovers NBA universe (cached for 60s)
    - Each cycle, scans a rotating slice of N=100 tickers
    - Skips tickers in cooldown after failure (60s TTL)
    - Maintains predictable 2s target cycle time
    - Updates top 20 signals from slice results
    - Maintains scanner state for /scanner/status endpoint

    Args:
        client: KalshiClient instance.
    """
    global _top_signals_snapshot

    TARGET_CYCLE_SECONDS = 2.0
    COOLDOWN_MS = 60_000  # 60 seconds
    scan_slice_size = 100
    universe_refresh_ttl_ms = 300_000  # Refresh universe every 300s

    logger.info("nba_scanner_loop_started")
    _scanner_state.enabled = True

    while True:
        cycle_start_ms = int(time.time() * 1000)

        try:
            # Clean expired cooldowns
            now_ms = int(time.time() * 1000)
            expired_tickers = [
                t for t, until_ms in _scanner_state.cooldown_until_ms.items() if now_ms >= until_ms
            ]
            for t in expired_tickers:
                del _scanner_state.cooldown_until_ms[t]

            # Determine if we need to refresh universe
            should_refresh_universe = (
                now_ms - _scanner_state.last_universe_refresh_ms > universe_refresh_ttl_ms
            )

            if should_refresh_universe or not _scanner_state.market_tickers:
                logger.info("nba_scanner_refreshing_universe")
                universe = await discover_nba_universe(client)
                _scanner_state.market_tickers = universe.market_tickers
                _scanner_state.markets_discovered = len(universe.market_tickers)
                _scanner_state.last_universe_refresh_ms = now_ms
                _scanner_state.rotating_index = 0

            market_tickers = _scanner_state.market_tickers

            if not market_tickers:
                logger.warning("nba_scanner_no_markets")
                _scanner_state.last_error = "No markets in universe"
                cycle_end_ms = int(time.time() * 1000)
                cycle_duration_ms = cycle_end_ms - cycle_start_ms
                _scanner_state.last_cycle_duration_ms = cycle_duration_ms
                sleep_ms = max(0, (TARGET_CYCLE_SECONDS * 1000) - cycle_duration_ms)
                await asyncio.sleep(sleep_ms / 1000.0)
                continue

            # Build rotating slice, skipping cooldown tickers
            _scanner_state.tickers_failed_last_cycle = []
            _scanner_state.tickers_skipped_cooldown_last_cycle = []

            start_idx = _scanner_state.rotating_index
            available_tickers: list[str] = []
            skipped_count = 0

            # Collect tickers not in cooldown, with adaptive sizing to maintain slice_size
            idx = start_idx
            while len(available_tickers) < scan_slice_size and len(
                available_tickers
            ) + skipped_count < len(market_tickers):
                ticker = market_tickers[idx % len(market_tickers)]
                if ticker not in _scanner_state.cooldown_until_ms:
                    available_tickers.append(ticker)
                else:
                    skipped_count += 1
                    _scanner_state.tickers_skipped_cooldown_last_cycle.append(ticker)
                idx += 1

            # Update rotating index for next cycle
            _scanner_state.rotating_index = idx % len(market_tickers)
            slice_tickers = available_tickers

            # Scan slice in batches with concurrency limit
            semaphore = asyncio.Semaphore(5)
            all_signals: list[Signal] = []

            async def fetch_and_score(
                ticker: str, sem: asyncio.Semaphore, api_client: KalshiClient
            ) -> tuple[str, Signal | None]:
                """Fetch orderbook and score signal. Returns (ticker, signal or None)."""
                async with sem:
                    try:
                        orderbook = await api_client.get_orderbook(ticker)
                        return (ticker, score_signal(ticker, orderbook))
                    except Exception as e:
                        logger.debug(
                            "nba_scanner_ticker_failed",
                            extra={"ticker": ticker, "error": str(e)},
                        )
                        return (ticker, None)

            # Process slice tickers
            results = await asyncio.gather(
                *[fetch_and_score(t, semaphore, client) for t in slice_tickers]
            )

            # Track failures and update cooldowns (use fresh timestamp for accuracy)
            for ticker, signal in results:
                if signal is None:
                    _scanner_state.tickers_failed_last_cycle.append(ticker)
                    fail_now_ms = int(time.time() * 1000)
                    _scanner_state.cooldown_until_ms[ticker] = fail_now_ms + COOLDOWN_MS
                else:
                    all_signals.append(signal)

                    # Process paper trading if enabled
                    if _ENABLE_PAPER_TRADING:
                        await _process_paper_intents(signal, client)

            _scanner_state.markets_scanned_last_cycle = len(slice_tickers)

            # Sort by best edge and keep top 20
            all_signals.sort(
                key=lambda s: (
                    -max(
                        s.net_edge_yes if s.net_edge_yes is not None else float("-inf"),
                        s.net_edge_no if s.net_edge_no is not None else float("-inf"),
                    )
                )
            )
            top_signals = all_signals[:20]

            # Update snapshot
            snapshot_ts_ms = int(time.time() * 1000)
            _top_signals_snapshot = TopSignalsSnapshot(
                timestamp_ms=snapshot_ts_ms,
                signals=[SignalResponse(**_signal_to_response(s)) for s in top_signals],
                market_count=len(market_tickers),
            )

            _scanner_state.last_scan_ms = snapshot_ts_ms
            _scanner_state.last_error = None

            # Compute cycle duration before logging
            cycle_end_ms = int(time.time() * 1000)
            cycle_duration_ms = cycle_end_ms - cycle_start_ms
            _scanner_state.last_cycle_duration_ms = cycle_duration_ms

            logger.info(
                "nba_scanner_cycle_complete",
                extra={
                    "total_market_count": len(market_tickers),
                    "slice_start": start_idx,
                    "slice_size": len(slice_tickers),
                    "signal_count": len(all_signals),
                    "top_count": len(top_signals),
                    "failed_count": len(_scanner_state.tickers_failed_last_cycle),
                    "skipped_cooldown_count": len(
                        _scanner_state.tickers_skipped_cooldown_last_cycle
                    ),
                    "duration_ms": cycle_duration_ms,
                },
            )

            # Periodic PnL snapshot writer (every 30 seconds)
            global _last_pnl_snapshot_ts_ms, _paper_daily_loss_breached
            if (
                _ENABLE_PAPER_TRADING
                and _paper_store is not None
                and snapshot_ts_ms - _last_pnl_snapshot_ts_ms >= _paper_pnl_snapshot_interval_ms
            ):
                try:
                    # Get current open positions
                    positions = _paper_store.get_positions()

                    # Close positions that reached take-profit or time-stop.
                    for pos in positions:
                        mark_price = _get_mark_price_for_position(
                            ticker=pos["ticker"],
                            side=pos["side"],
                            snapshot=_top_signals_snapshot,
                        )
                        if mark_price is None:
                            mark_price = await _fetch_mark_price_cents(
                                client,
                                pos["ticker"],
                                pos["side"],
                            )
                        if mark_price is None:
                            continue

                        closed_for_take_profit = _maybe_close_position_take_profit(
                            ticker=pos["ticker"],
                            side=pos["side"],
                            net_contracts=pos["net_contracts"],
                            avg_entry_price_cents=pos["avg_entry_price_cents"],
                            realized_pnl_cents=pos.get("realized_pnl_cents", 0.0),
                            first_open_ts_ms=pos["first_open_ts_ms"],
                            mark_price_cents=mark_price,
                            now_ms=snapshot_ts_ms,
                        )
                        if closed_for_take_profit:
                            continue

                        hold_elapsed_ms = snapshot_ts_ms - pos["first_open_ts_ms"]
                        if hold_elapsed_ms >= (_PAPER_MAX_HOLD_SECONDS * 1000):
                            _close_position(
                                ticker=pos["ticker"],
                                side=pos["side"],
                                net_contracts=pos["net_contracts"],
                                avg_entry_price_cents=pos["avg_entry_price_cents"],
                                realized_pnl_cents=pos.get("realized_pnl_cents", 0.0),
                                first_open_ts_ms=pos["first_open_ts_ms"],
                                mark_price_cents=mark_price,
                                now_ms=snapshot_ts_ms,
                                reason="time_stop",
                            )

                    # Reload open positions after optional TP closes.
                    positions = _paper_store.get_positions()

                    # Build mark_price_dict from top_signals_snapshot for metric computation
                    mark_price_dict: dict[str, dict[str, float]] = {}
                    if _top_signals_snapshot:
                        for signal_resp in _top_signals_snapshot.signals:
                            ticker = signal_resp.ticker
                            if ticker not in mark_price_dict:
                                mark_price_dict[ticker] = {}
                            if signal_resp.cross_yes is not None:
                                mark_price_dict[ticker]["YES"] = signal_resp.cross_yes
                            if signal_resp.cross_no is not None:
                                mark_price_dict[ticker]["NO"] = signal_resp.cross_no

                    # Compute all metrics from authoritative source using compute_paper_metrics
                    metrics = _paper_store.compute_paper_metrics(mark_price_dict or None)
                    realized_pnl_cents = metrics["realized_pnl_cents"]
                    unrealized_pnl_cents = metrics["unrealized_pnl_cents"]
                    total_pnl_cents = metrics["total_pnl_cents"]
                    open_positions_count = metrics["open_positions_count"]
                    open_orders_count = metrics["open_orders_count"]

                    # Check MAX_DAILY_LOSS gate (if configured)
                    if (
                        _PAPER_MAX_DAILY_LOSS_CENTS > 0
                        and total_pnl_cents < -_PAPER_MAX_DAILY_LOSS_CENTS
                        and not _paper_daily_loss_breached
                    ):
                        _paper_daily_loss_breached = True
                        logger.warning(
                            "paper_max_daily_loss_breached",
                            extra={
                                "total_pnl_cents": total_pnl_cents,
                                "max_daily_loss_cents": _PAPER_MAX_DAILY_LOSS_CENTS,
                                "notice": "Stopping new intent creation due to MAX_DAILY_LOSS breach",
                            },
                        )

                    # Insert PnL snapshot - ensures invariant: total = realized + unrealized
                    _paper_store.insert_pnl_snapshot(
                        ts_ms=snapshot_ts_ms,
                        realized_pnl_cents=realized_pnl_cents,
                        unrealized_pnl_cents=unrealized_pnl_cents,
                        open_positions_count=open_positions_count,
                        open_orders_count=open_orders_count,
                    )

                    _last_pnl_snapshot_ts_ms = snapshot_ts_ms

                    logger.info(
                        "paper_pnl_snapshot_recorded",
                        extra={
                            "realized_pnl_cents": realized_pnl_cents,
                            "unrealized_pnl_cents": unrealized_pnl_cents,
                            "total_pnl_cents": total_pnl_cents,
                            "open_positions_count": open_positions_count,
                            "open_orders_count": open_orders_count,
                            "daily_loss_breached": _paper_daily_loss_breached,
                        },
                    )
                except Exception as e:
                    logger.exception(
                        "paper_pnl_snapshot_error",
                        extra={"error": str(e)},
                    )

        except asyncio.CancelledError:
            logger.info("scanner_cancelled")
            break

        except Exception as e:
            logger.exception(
                "nba_scanner_loop_error",
                extra={"error": str(e)},
            )
            _scanner_state.last_error = str(e)

        # Compute cycle duration once, then sleep to maintain target cadence
        cycle_end_ms = int(time.time() * 1000)
        cycle_duration_ms = cycle_end_ms - cycle_start_ms
        _scanner_state.last_cycle_duration_ms = cycle_duration_ms
        sleep_ms = max(0, (TARGET_CYCLE_SECONDS * 1000) - cycle_duration_ms)
        await asyncio.sleep(sleep_ms / 1000.0)


# Helper functions
def _signal_to_response(signal: Signal) -> dict[str, Any]:
    """Convert Signal dataclass to API response payload."""
    return {
        "ticker": signal.ticker,
        "reason_code": signal.reason_code,
        "net_edge_yes": signal.net_edge_yes,
        "net_edge_no": signal.net_edge_no,
        "inside_width_yes": signal.inside_width_yes,
        "inside_width_no": signal.inside_width_no,
        "bid_yes": signal.bid_yes,
        "cross_yes": signal.cross_yes,
        "bid_no": signal.bid_no,
        "cross_no": signal.cross_no,
        "bid_sum": signal.bid_sum,
        "imbalance_gap": signal.imbalance_gap,
        "fee_yes": signal.fee_yes,
        "fee_no": signal.fee_no,
        "fee_exit_yes": signal.fee_exit_yes,
        "fee_exit_no": signal.fee_exit_no,
        "total_fee_yes": signal.total_fee_yes,
        "total_fee_no": signal.total_fee_no,
        "fee_mode": signal.fee_mode,
        "round_trip": signal.round_trip,
        "liq_yes": signal.liq_yes,
        "liq_no": signal.liq_no,
        "top_liquidity": signal.top_liquidity,
        "timestamp_ms": signal.timestamp_ms,
    }


def _parse_tickers(tickers: str) -> list[str]:
    """Parse, normalize, filter-empty, and deduplicate a CSV ticker list."""
    parsed: list[str] = []
    seen: set[str] = set()

    for raw_ticker in tickers.split(","):
        normalized = raw_ticker.strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        parsed.append(normalized)

    return parsed


# Route handlers (defined as regular functions)
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {"status": "ok"}


async def version() -> dict[str, Any]:
    """Version information endpoint."""
    return {
        "name": "kalshi-edge-engine",
        "version": "0.1.0",
    }


async def debug_orderbook(ticker: str, request: Request) -> dict[str, Any]:
    """Fetch and return raw orderbook data for debugging.

    Args:
        ticker: Market ticker symbol (e.g., 'TRUMP-ELECTION').

    Returns:
        Raw orderbook data from Kalshi API.

    Raises:
        HTTPException: If request is invalid or API call fails.
    """
    # Validate ticker
    if not ticker or not ticker.strip():
        logger.warning("debug_orderbook_invalid_ticker", extra={"ticker": ticker})
        raise HTTPException(status_code=400, detail="Ticker cannot be empty")

    ticker = ticker.strip().upper()
    start_time = time.time()

    try:
        # Use singleton client from app state
        client = request.app.state.kalshi_client

        # Fetch orderbook
        orderbook = await client.get_orderbook(ticker)

        latency_ms = (time.time() - start_time) * 1000
        logger.info(
            "debug_orderbook_success",
            extra={
                "ticker": ticker,
                "latency_ms": latency_ms,
            },
        )

        return {
            "ticker": ticker,
            "orderbook": orderbook,
            "latency_ms": latency_ms,
            "success": True,
        }

    except KalshiAuthError as e:
        logger.error(
            "debug_orderbook_auth_error",
            extra={
                "ticker": ticker,
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=502,
            detail=f"Auth error: {str(e)}",
        ) from e

    except KalshiAPIError as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.error(
            "debug_orderbook_api_error",
            extra={
                "ticker": ticker,
                "error": str(e),
                "latency_ms": latency_ms,
            },
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream API error: {str(e)}",
        ) from e

    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.exception(
            "debug_orderbook_unexpected_error",
            extra={
                "ticker": ticker,
                "error": str(e),
                "latency_ms": latency_ms,
            },
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error",
        ) from e


async def get_live_signal(ticker: str, request: Request) -> dict[str, Any]:
    """Get live signal for a market.

    Fetches the current orderbook and scores it to emit a structured signal
    with reason code and computed edges.

    Args:
        ticker: Market ticker symbol (e.g., 'KXNBAGAME-26MAR01SASNYK-SAS').

    Returns:
        Signal with reason_code, spreads, and net edges.

    Raises:
        HTTPException: If request is invalid or API call fails.
    """
    # Validate ticker
    if not ticker or not ticker.strip():
        logger.warning("signal_invalid_ticker", extra={"ticker": ticker})
        raise HTTPException(status_code=400, detail="Ticker cannot be empty")

    ticker = ticker.strip().upper()
    start_time = time.time()

    try:
        # Use singleton client from app state
        client = request.app.state.kalshi_client

        # Fetch orderbook
        orderbook = await client.get_orderbook(ticker)

        # Score signal
        signal = score_signal(ticker, orderbook)

        latency_ms = (time.time() - start_time) * 1000
        logger.info(
            "signal_computed",
            extra={
                "ticker": ticker,
                "reason_code": signal.reason_code,
                "net_edge_yes": signal.net_edge_yes,
                "net_edge_no": signal.net_edge_no,
                "latency_ms": latency_ms,
            },
        )

        return _signal_to_response(signal)

    except KalshiAuthError as e:
        logger.error(
            "signal_auth_error",
            extra={
                "ticker": ticker,
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=502,
            detail=f"Auth error: {str(e)}",
        ) from e

    except KalshiAPIError as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.error(
            "signal_api_error",
            extra={
                "ticker": ticker,
                "error": str(e),
                "latency_ms": latency_ms,
            },
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream API error: {str(e)}",
        ) from e

    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.exception(
            "signal_unexpected_error",
            extra={
                "ticker": ticker,
                "error": str(e),
                "latency_ms": latency_ms,
            },
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error",
        ) from e


async def scan_live_signals(tickers: str, request: Request) -> list[dict[str, Any]]:
    """Scan multiple tickers and return sorted signal opportunities.

    Uses parallel requests with concurrency limit to improve performance.
    Automatically retries failed tickers gracefully.
    """
    parsed_tickers = _parse_tickers(tickers)
    if not parsed_tickers:
        logger.warning("scan_invalid_tickers", extra={"tickers": tickers})
        raise HTTPException(status_code=400, detail="At least one valid ticker is required")
    if len(parsed_tickers) > 20:
        logger.warning("scan_too_many_tickers", extra={"count": len(parsed_tickers)})
        raise HTTPException(status_code=400, detail="Maximum 20 tickers per request")

    start_time = time.time()

    try:
        client = request.app.state.kalshi_client

        # Semaphore to limit concurrent requests (max 5)
        semaphore = asyncio.Semaphore(5)

        async def fetch_and_score(ticker: str) -> Signal | None:
            """Fetch orderbook and score signal for a ticker.

            Returns None if fetch fails (KalshiAPIError).
            """
            async with semaphore:
                try:
                    orderbook = await client.get_orderbook(ticker)
                    return score_signal(ticker, orderbook)
                except KalshiAPIError as e:
                    logger.warning(
                        "scan_ticker_failed",
                        extra={
                            "ticker": ticker,
                            "error": str(e),
                        },
                    )
                    return None

        # Fetch all tickers in parallel
        results = await asyncio.gather(*[fetch_and_score(t) for t in parsed_tickers])

        # Filter out failed tickers (None results)
        scored_signals: list[Signal] = [r for r in results if r is not None]

        if not scored_signals:
            raise HTTPException(status_code=502, detail="Upstream API error")

        # Sort by best net edge (descending), then by ticker (ascending) for deterministic ordering
        scored_signals.sort(
            key=lambda signal: (
                -max(
                    signal.net_edge_yes if signal.net_edge_yes is not None else float("-inf"),
                    signal.net_edge_no if signal.net_edge_no is not None else float("-inf"),
                ),
                signal.ticker,
            )
        )

        latency_ms = (time.time() - start_time) * 1000
        logger.info(
            "scan_computed",
            extra={
                "ticker_count": len(parsed_tickers),
                "latency_ms": latency_ms,
            },
        )

        return [_signal_to_response(signal) for signal in scored_signals]

    except KalshiAuthError as e:
        logger.error(
            "scan_auth_error",
            extra={
                "ticker_count": len(parsed_tickers),
                "error": str(e),
            },
        )
        raise HTTPException(
            status_code=502,
            detail=f"Auth error: {str(e)}",
        ) from e

    except HTTPException:
        raise

    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.exception(
            "scan_unexpected_error",
            extra={
                "ticker_count": len(parsed_tickers),
                "error": str(e),
                "latency_ms": latency_ms,
            },
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error",
        ) from e


async def get_nba_universe(request: Request) -> dict[str, Any]:
    """Get NBA market universe (series, events, markets).

    Returns cached data if fresh (TTL 60s), otherwise refreshes from Kalshi API.

    Returns:
        NBA universe with series_ticker, event_tickers, and market_tickers.

    Raises:
        HTTPException: If API call fails.
    """
    start_time = time.time()

    try:
        client = request.app.state.kalshi_client

        # Discover NBA universe (with caching)
        universe = await discover_nba_universe(client)

        latency_ms = (time.time() - start_time) * 1000
        logger.info(
            "nba_universe_returned",
            extra={
                "series_ticker": universe.series_ticker,
                "event_count": len(universe.event_tickers),
                "market_count": len(universe.market_tickers),
                "latency_ms": latency_ms,
            },
        )

        return {
            "series_ticker": universe.series_ticker,
            "event_tickers": universe.event_tickers,
            "market_tickers": universe.market_tickers,
        }

    except KalshiAuthError as e:
        logger.error(
            "nba_universe_auth_error",
            extra={"error": str(e)},
        )
        raise HTTPException(
            status_code=502,
            detail=f"Auth error: {str(e)}",
        ) from e

    except KalshiAPIError as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.error(
            "nba_universe_api_error",
            extra={
                "error": str(e),
                "latency_ms": latency_ms,
            },
        )
        raise HTTPException(
            status_code=502,
            detail=f"Upstream API error: {str(e)}",
        ) from e

    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.exception(
            "nba_universe_unexpected_error",
            extra={
                "error": str(e),
                "latency_ms": latency_ms,
            },
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error",
        ) from e


async def get_top_signals(n: int = 20) -> dict[str, Any]:
    """Get top NBA signals from background scanner snapshot.

    Args:
        n: Maximum number of signals to return (default 20).

    Returns:
        Snapshot with timestamp, signals list, and market count.

    Raises:
        HTTPException: If snapshot is not available yet.
    """
    global _top_signals_snapshot

    if _top_signals_snapshot is None:
        raise HTTPException(
            status_code=503,
            detail="Signal snapshot not available yet (scanner starting up)",
        )

    # Limit to requested count
    signals = _top_signals_snapshot.signals[:n]

    return {
        "timestamp_ms": _top_signals_snapshot.timestamp_ms,
        "signals": [s.model_dump() for s in signals],
        "market_count": _top_signals_snapshot.market_count,
    }


async def start_scanner(request: Request) -> dict[str, Any]:
    """Start the NBA background scanner.

    If scanner is not running, creates a background task and starts it.
    If already running, returns a status indicating it's already running.

    Returns:
        Response indicating whether scanner was started or if it was already running.
    """
    # Check if a valid task is already running
    current_task = getattr(request.app.state, "scanner_task", None)
    if current_task and not current_task.done():
        logger.info("Scanner already running, ignoring start request")
        return {"started": False, "reason": "already_running"}
    # If task is done or does not exist, treat as not running - proceed to start

    # Start new scanner task
    client = request.app.state.kalshi_client
    scanner_task = asyncio.create_task(_nba_scanner_loop(client))
    request.app.state.scanner_task = scanner_task
    _scanner_state.enabled = True

    logger.info("NBA scanner started via POST /scanner/start")
    return {"started": True}


async def stop_scanner(request: Request) -> dict[str, Any]:
    """Stop the NBA background scanner.

    Cancels the scanner task cleanly and updates state.

    Returns:
        Response indicating whether scanner was stopped or if it wasn't running.
    """
    current_task = getattr(request.app.state, "scanner_task", None)
    if not current_task:
        logger.info("No scanner task found, ignoring stop request")
        return {"stopped": False, "reason": "not_running"}

    if current_task.done():
        logger.info("Scanner task already done, clearing task reference")
        _scanner_state.enabled = False
        request.app.state.scanner_task = None
        return {"stopped": False, "reason": "not_running"}

    # Cancel task
    current_task.cancel()
    try:
        await current_task
    except asyncio.CancelledError:
        logger.info("Scanner task cancelled successfully")

    _scanner_state.enabled = False
    request.app.state.scanner_task = None
    global _top_signals_snapshot
    _top_signals_snapshot = None

    logger.info("NBA scanner stopped via POST /scanner/stop")
    return {"stopped": True}


async def start_paper_trading() -> dict[str, Any]:
    """Start paper trading intents processing.

    Enables the paper trading flag so that intents are processed and simulated
    when signals are generated. Does nothing if paper trading is not enabled
    in environment or store is not initialized.

    Returns:
        Response indicating whether paper trading was started.
    """
    global _paper_enabled

    if not _ENABLE_PAPER_TRADING or _paper_store is None:
        logger.info("Paper trading not available (not enabled or no store)")
        return {"started": False, "reason": "not_available"}

    if _paper_enabled:
        logger.info("Paper trading already running, ignoring start request")
        return {"started": False, "reason": "already_running"}

    _paper_enabled = True
    logger.info("Paper trading started via POST /paper/start")
    return {"started": True}


async def stop_paper_trading() -> dict[str, Any]:
    """Stop paper trading and cancel pending intent simulations.

    Disables the paper trading flag and cancels any pending simulation tasks.

    Returns:
        Response indicating whether paper trading was stopped.
    """
    global _paper_enabled

    if not _paper_enabled:
        logger.info("Paper trading not running, ignoring stop request")
        return {"stopped": False, "reason": "not_running"}

    _paper_enabled = False

    # Cancel all pending simulation tasks
    for task in list(_paper_simulation_tasks):
        if not task.done():
            task.cancel()

    if _paper_simulation_tasks:
        # Gather with return_exceptions to avoid raising on cancelled tasks
        await asyncio.gather(*_paper_simulation_tasks, return_exceptions=True)
        logger.info("paper_trading_tasks_cancelled", extra={"count": len(_paper_simulation_tasks)})
        _paper_simulation_tasks.clear()

    logger.info("Paper trading stopped via POST /paper/stop")
    return {"stopped": True}


# Paper Trading Endpoints
async def get_paper_intents(
    status: str = PAPER_INTENT_STATUS_OPEN, limit: int = 50
) -> dict[str, Any]:
    """Get paper trading intents by status.

    Args:
        status: Filter by status (OPEN, FILLED, TAKE_PROFIT, MAX_HOLD_REACHED, EXPIRED, ERROR).
        limit: Max intents to return.

    Returns:
        List of intents with current status.
    """
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    normalized_status = status.strip().upper()
    if normalized_status not in PAPER_INTENT_ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid paper intent status")

    intents = _paper_store.get_intents_by_status(normalized_status, limit)
    return {"status": normalized_status, "intents": intents, "count": len(intents)}


async def get_paper_fills(limit: int = 50) -> dict[str, Any]:
    """Get recent paper fill events.

    Args:
        limit: Max fills to return.

    Returns:
        List of recent fills.
    """
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    fills = _paper_store.get_recent_fills(limit)
    return {"fills": fills, "count": len(fills)}


async def get_paper_positions() -> dict[str, Any]:
    """Get current open positions from paper trading.

    Returns:
        List of positions with PnL.
    """
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    positions = _paper_store.get_positions()
    # Use authoritative compute_paper_metrics for consistency
    metrics = _paper_store.compute_paper_metrics()
    return {
        "positions": positions,
        "count": metrics["open_positions_count"],
        "total_realized_pnl_cents": metrics["realized_pnl_cents"],
    }


async def get_paper_pnl_snapshot() -> dict[str, Any]:
    """Get latest PnL snapshot.

    Returns:
        Latest PnL snapshot with aggregated metrics.
    """
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    snapshot = _paper_store.get_latest_pnl_snapshot()
    if not snapshot:
        return {
            "ts_ms": None,
            "realized_pnl_cents": 0.0,
            "unrealized_pnl_cents": 0.0,
            "total_pnl_cents": 0.0,
            "open_positions_count": 0,
            "open_orders_count": 0,
        }
    return snapshot


async def get_paper_summary() -> dict[str, Any]:
    """Get summary statistics for paper trading.

    Returns:
        Aggregated stats across all paper trading activity.
    """
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    # Use authoritative compute_paper_metrics for consistency
    metrics = _paper_store.compute_paper_metrics()
    snapshot = _paper_store.get_latest_pnl_snapshot()

    return {
        "enabled": _paper_enabled,
        "total_intents_created": _paper_store.count_intents_total(),
        "total_fills": len(_paper_store.get_recent_fills(limit=1000)),
        "total_positions": metrics["open_positions_count"],
        "realized_pnl_cents": metrics["realized_pnl_cents"],
        "unrealized_pnl_cents": metrics.get("unrealized_pnl_cents", 0.0),
        "total_pnl_cents": metrics["total_pnl_cents"],
        "latest_snapshot_ts_ms": snapshot["ts_ms"] if snapshot else None,
    }


async def get_paper_metrics() -> dict[str, Any]:
    """Get lightweight paper activity metrics for dashboard observability."""
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    metrics = _paper_store.get_activity_metrics(window_seconds=3600)
    return {
        "fill_rate": metrics["fill_rate"],
        "avg_time_to_fill_seconds": metrics["avg_time_to_fill_seconds"],
        "fills_last_1h": metrics["fills_last_1h"],
        "intents_last_1h": metrics["intents_last_1h"],
    }


async def get_paper_config() -> dict[str, Any]:
    """Return current paper trading configuration.

    Returns:
        Configuration dict with:
        - configured: Whether paper trading is enabled in env (ENABLE_PAPER_TRADING)
        - enabled: Runtime enabled flag (can be toggled by POST /paper/start/stop)
        - active: Whether paper trading is actually processing fills right now
                  (enabled=true AND has running simulation tasks)
    """
    # Check if there are active simulation tasks
    active = _paper_enabled and any(not t.done() for t in _paper_simulation_tasks)

    return {
        "configured": _ENABLE_PAPER_TRADING,
        "enabled": _paper_enabled,
        "active": active,
        "min_edge_cents": _PAPER_MIN_EDGE_CENTS,
        "fill_horizon_seconds": _PAPER_FILL_HORIZON_SECONDS,
        "poll_interval_seconds": _PAPER_POLL_INTERVAL_SECONDS,
        "fill_probability_price_moved": _PAPER_FILL_PROBABILITY,
        "fill_probability_at_best": _PAPER_FILL_PROBABILITY_AT_BEST,
        "fill_probability_price_improves": _PAPER_FILL_PROBABILITY_PRICE_IMPROVES,
        "simulated_volume_per_poll": _PAPER_SIMULATED_VOLUME_PER_POLL,
        "max_hold_seconds": _PAPER_MAX_HOLD_SECONDS,
        "take_profit_cents": _PAPER_TAKE_PROFIT_CENTS,
        "ticker_cooldown_seconds": _PAPER_TICKER_COOLDOWN_SECONDS,
        "max_intents_per_minute": _PAPER_MAX_INTENTS_PER_MINUTE,
        "max_concurrent_intents": _PAPER_MAX_CONCURRENT_INTENTS,
        "max_open_orders": _PAPER_MAX_OPEN_ORDERS,
        "max_pos_per_ticker": _PAPER_MAX_POS_PER_TICKER,
    }


async def get_paper_debug_intent(intent_id: str, request: Request) -> dict[str, Any]:
    """Debug simulator inputs/diagnostics for a single paper intent."""
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    intent = _paper_store.get_intent_record(intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="Intent not found")

    client = request.app.state.kalshi_client
    orderbook = await client.get_orderbook(intent["ticker"])
    queue_ahead = intent["queue_ahead_contracts"] or 0

    order_request = OrderRequest(
        id=intent["id"],
        ticker=intent["ticker"],
        side=intent["side"],
        quote_price_cents=float(intent["quote_price_cents"]),
        size_contracts=int(intent["size_contracts"]),
        created_ts_ms=int(intent["created_ts_ms"]),
        net_edge=float(intent["net_edge"]),
        entry_fee_cents=float(intent["entry_fee_cents"] or 0.0),
        take_profit_cents=float(intent["take_profit_cents"] or _PAPER_TAKE_PROFIT_CENTS),
        max_hold_seconds=int(intent["max_hold_seconds"] or _PAPER_MAX_HOLD_SECONDS),
        entry_fee_per_contract_cents=(
            float(intent["entry_fee_cents"] or 0.0) / int(intent["size_contracts"])
            if int(intent["size_contracts"]) > 0
            else 0.0
        ),
    )

    diagnostics = compute_simulator_diagnostics(
        order=order_request,
        orderbook=orderbook,
        queue_ahead=queue_ahead,
        last_best_size_at_bid=None,
        simulated_volume_per_poll=_PAPER_SIMULATED_VOLUME_PER_POLL,
    )

    return {
        "intent": intent,
        "top_of_book": extract_top_of_book_snapshot(orderbook),
        "simulator": {
            "best_yes_bid": diagnostics["best_yes_bid"],
            "best_no_bid": diagnostics["best_no_bid"],
            "cross_yes": diagnostics["cross_yes"],
            "cross_no": diagnostics["cross_no"],
            "best_bid": diagnostics["best_bid"],
            "best_size_at_bid": diagnostics["best_size_at_bid"],
            "queue_ahead_before": diagnostics["queue_ahead_before"],
            "queue_ahead_after": diagnostics["queue_ahead_after"],
            "eligibility": diagnostics["eligibility"],
            "chosen_side": diagnostics["chosen_side"],
            "price_improves": diagnostics["price_improves"],
            "traded_volume_estimate": diagnostics["traded_volume_estimate"],
            "market_best_bid": diagnostics["market_best_bid"],
            "cross_for_side": diagnostics["cross_for_side"],
            "effective_best_bid": diagnostics["effective_best_bid"],
        },
    }


async def post_paper_debug_force_fill(request: ForceFillRequest) -> dict[str, Any]:
    """Force-insert a synthetic fill for debug purposes."""
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")
    intent_id = request.intent_id
    fill_size = request.fill_size
    if fill_size <= 0:
        raise HTTPException(status_code=400, detail="fill_size must be > 0")

    intent = _paper_store.get_intent_record(intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="Intent not found")

    already_filled = _paper_store.get_filled_size_for_intent(intent_id)
    size_contracts = int(intent["size_contracts"])
    remaining_before = max(0, size_contracts - already_filled)
    actual_fill_size = min(fill_size, remaining_before)

    if actual_fill_size <= 0:
        _paper_store.update_intent_status(
            intent_id,
            PAPER_INTENT_STATUS_FILLED,
            last_seen_best_bid=intent.get("last_seen_best_bid"),
            queue_ahead=intent.get("queue_ahead_contracts"),
        )
        return {
            "intent_id": intent_id,
            "filled_size": 0,
            "remaining_size": 0,
            "status": PAPER_INTENT_STATUS_FILLED,
        }

    remaining_after = remaining_before - actual_fill_size
    fill_ts_ms = int(time.time() * 1000)
    _paper_store.insert_fill(
        fill_id=f"debug:{intent_id}:{fill_ts_ms}",
        intent_id=intent_id,
        fill_ts_ms=fill_ts_ms,
        fill_price_cents=float(intent["quote_price_cents"]),
        fill_size=actual_fill_size,
        remaining_size=remaining_after,
        queue_ahead_at_fill=intent.get("queue_ahead_contracts"),
        fill_fee_cents=0.0,
        best_bid_at_fill_cents=intent.get("last_seen_best_bid"),
        cross_at_fill_cents=intent.get("cross_at_submit_cents"),
        note="debug_fill",
    )

    positions = _paper_store.get_positions()
    current_pos = next(
        (p for p in positions if p["ticker"] == intent["ticker"] and p["side"] == intent["side"]),
        None,
    )

    if current_pos:
        total_cost = (
            current_pos["avg_entry_price_cents"] * current_pos["net_contracts"]
            + float(intent["quote_price_cents"]) * actual_fill_size
        )
        new_contracts = current_pos["net_contracts"] + actual_fill_size
        new_avg_price = total_cost / new_contracts if new_contracts > 0 else 0.0
        _paper_store.upsert_position(
            ticker=intent["ticker"],
            side=intent["side"],
            net_contracts=new_contracts,
            avg_entry_price_cents=new_avg_price,
            realized_pnl_cents=current_pos["realized_pnl_cents"],
            first_open_ts_ms=current_pos["first_open_ts_ms"],
        )
    else:
        _paper_store.upsert_position(
            ticker=intent["ticker"],
            side=intent["side"],
            net_contracts=actual_fill_size,
            avg_entry_price_cents=float(intent["quote_price_cents"]),
            realized_pnl_cents=0.0,
            first_open_ts_ms=int(intent["created_ts_ms"]),
        )

    _paper_store.update_intent_status(
        intent_id,
        PAPER_INTENT_STATUS_FILLED,
        last_seen_best_bid=intent.get("last_seen_best_bid"),
        queue_ahead=intent.get("queue_ahead_contracts"),
    )

    return {
        "intent_id": intent_id,
        "filled_size": actual_fill_size,
        "remaining_size": remaining_after,
        "status": PAPER_INTENT_STATUS_FILLED,
    }


async def get_scanner_status(request: Request) -> dict[str, Any]:
    """Get NBA scanner status.

    Returns:
        Scanner status including enabled flag, market counts, timing,
        failure tracking, and snapshot age.
    """
    now_ms = int(time.time() * 1000)
    snapshot_age_ms: int | None = None

    if _top_signals_snapshot is not None:
        snapshot_age_ms = now_ms - _top_signals_snapshot.timestamp_ms

    # Enabled only if state says so AND task exists and is running
    task_running = False
    current_task = getattr(request.app.state, "scanner_task", None)
    if current_task and not current_task.done():
        task_running = True

    return {
        "enabled": _scanner_state.enabled and task_running,
        "last_scan_ms": _scanner_state.last_scan_ms,
        "markets_discovered": _scanner_state.markets_discovered,
        "markets_scanned_last_cycle": _scanner_state.markets_scanned_last_cycle,
        "last_cycle_duration_ms": _scanner_state.last_cycle_duration_ms,
        "tickers_failed_last_cycle": _scanner_state.tickers_failed_last_cycle,
        "tickers_skipped_cooldown_last_cycle": _scanner_state.tickers_skipped_cooldown_last_cycle,
        "snapshot_timestamp_ms": _top_signals_snapshot.timestamp_ms
        if _top_signals_snapshot
        else None,
        "snapshot_age_ms": snapshot_age_ms,
        "last_error": _scanner_state.last_error,
    }


# Lifespan managers
@asynccontextmanager
async def lifespan_production(app: FastAPI) -> Any:
    """Manage app lifecycle: initialize singleton client on startup, close on shutdown."""
    # Startup
    # Note: scanner task may be started here via auto-start, or later via POST /scanner/start.
    # Always check app.state.scanner_task in shutdown, not a local variable.
    try:
        client = KalshiClient()
        app.state.kalshi_client = client
        app.state.scanner_task = None  # Initialize scanner_task; may be updated below
        logger.info("KalshiClient initialized and stored on app.state")

        # Initialize paper trading store if enabled
        if _ENABLE_PAPER_TRADING:
            _init_paper_store()

        # Check if NBA scanner is enabled via environment variable
        enable_scanner_str = os.getenv("ENABLE_NBA_SCANNER", "false").lower()
        enable_scanner = enable_scanner_str in ("true", "1", "yes")

        if enable_scanner:
            # Start background NBA scanner
            scanner_task = asyncio.create_task(_nba_scanner_loop(client))
            app.state.scanner_task = scanner_task
            logger.info("NBA scanner background task started (ENABLE_NBA_SCANNER=true)")
        else:
            _scanner_state.enabled = False
            logger.info("NBA scanner disabled (ENABLE_NBA_SCANNER=false)")

    except Exception as e:
        logger.exception("Failed to initialize KalshiClient on startup", extra={"error": str(e)})
        raise

    yield

    # Shutdown
    try:
        # Cancel paper trading simulation tasks
        for task in _paper_simulation_tasks:
            if not task.done():
                task.cancel()

        if _paper_simulation_tasks:
            await asyncio.gather(*_paper_simulation_tasks, return_exceptions=True)
            logger.info("paper_simulation_tasks_cancelled")

        # Cancel scanner task from app.state, not local variable, to catch tasks
        # started via POST /scanner/start
        current_task = getattr(app.state, "scanner_task", None)
        if current_task and not current_task.done():
            current_task.cancel()
            try:
                await current_task
            except asyncio.CancelledError:
                logger.info("NBA scanner task cancelled on shutdown")

        if hasattr(app.state, "kalshi_client"):
            await app.state.kalshi_client.aclose()
            logger.info("KalshiClient closed on shutdown")
    except Exception as e:
        logger.exception("Error during shutdown", extra={"error": str(e)})


@asynccontextmanager
async def lifespan_testing(_app: FastAPI) -> Any:
    """Testing lifespan: skip KalshiClient initialization but initialize scanner_task."""
    _app.state.scanner_task = None
    yield


# App factory
def create_app(testing: bool = False) -> FastAPI:
    """Create and configure FastAPI application.

    Args:
        testing: If True, skip KalshiClient initialization (for testing).

    Returns:
        Configured FastAPI application instance.
    """
    app_instance = FastAPI(
        title="Kalshi Edge Engine",
        description="Real-time signal generation and dashboard backend",
        version="0.1.0",
        lifespan=lifespan_testing if testing else lifespan_production,
    )

    # Enable CORS for local development dashboard
    # Allow local Vite dev server origins and GET/POST methods
    app_instance.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Register routes
    app_instance.get("/health", response_model=HealthResponse)(health)
    app_instance.get("/version", response_model=VersionResponse)(version)
    app_instance.get("/debug/orderbook/{ticker}")(debug_orderbook)
    app_instance.get("/signals/live", response_model=SignalResponse)(get_live_signal)
    app_instance.get("/signals/scan", response_model=list[SignalResponse])(scan_live_signals)
    app_instance.get("/signals/top")(get_top_signals)
    app_instance.get("/universe/nba", response_model=NBAUniverseResponse)(get_nba_universe)
    app_instance.get("/scanner/status", response_model=ScannerStatusResponse)(get_scanner_status)
    app_instance.post("/scanner/start", response_model=ScannerStartResponse)(start_scanner)
    app_instance.post("/scanner/stop", response_model=ScannerStopResponse)(stop_scanner)

    # Paper trading routes
    app_instance.post("/paper/start", response_model=PaperStartResponse)(start_paper_trading)
    app_instance.post("/paper/stop", response_model=PaperStopResponse)(stop_paper_trading)
    app_instance.get("/paper/intents")(get_paper_intents)
    app_instance.get("/paper/fills")(get_paper_fills)
    app_instance.get("/paper/positions")(get_paper_positions)
    app_instance.get("/paper/pnl-snapshot")(get_paper_pnl_snapshot)
    app_instance.get("/paper/summary")(get_paper_summary)
    app_instance.get("/paper/metrics")(get_paper_metrics)
    app_instance.get("/paper/config")(get_paper_config)
    app_instance.get("/paper/debug/intent/{intent_id}")(get_paper_debug_intent)
    app_instance.post("/paper/debug/force-fill")(post_paper_debug_force_fill)

    return app_instance


# Create default app instance for production use
app = create_app()


# Development server entry point
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
