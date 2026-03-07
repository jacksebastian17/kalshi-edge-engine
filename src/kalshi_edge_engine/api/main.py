"""FastAPI application for Kalshi Edge Engine."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import build_quote_intents
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
from kalshi_edge_engine.runtime import config as runtime_config
from kalshi_edge_engine.runtime import paper_runtime as _runtime_paper_runtime_module
from kalshi_edge_engine.runtime import state as runtime_state
from kalshi_edge_engine.runtime.paper_exit_runtime import (
    _close_position as _runtime_close_position,
)
from kalshi_edge_engine.runtime.paper_exit_runtime import (
    _fetch_mark_price_cents as _runtime_fetch_mark_price_cents,
)
from kalshi_edge_engine.runtime.paper_exit_runtime import (
    _get_mark_price_for_position as _runtime_get_mark_price_for_position,
)
from kalshi_edge_engine.runtime.paper_exit_runtime import (
    _maybe_close_position_take_profit as _runtime_maybe_close_position_take_profit,
)
from kalshi_edge_engine.runtime.paper_exit_runtime import (
    _update_fill_30s_outcomes as _runtime_update_fill_30s_outcomes,
)
from kalshi_edge_engine.runtime.paper_runtime import (
    _can_create_intent as _runtime_can_create_intent,
)
from kalshi_edge_engine.runtime.paper_runtime import (
    _get_rate_limit_or_cooldown_reason as _runtime_get_rate_limit_or_cooldown_reason,
)
from kalshi_edge_engine.runtime.paper_runtime import _init_paper_store as _runtime_init_paper_store
from kalshi_edge_engine.runtime.paper_runtime import (
    _process_paper_intents as _runtime_process_paper_intents,
)
from kalshi_edge_engine.runtime.scanner_runtime import (
    _nba_scanner_loop as _runtime_nba_scanner_loop,
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
    intended_quote_yes_cents: int | None = None
    intended_quote_no_cents: int | None = None
    capturable_edge_to_cross_yes_cents: float | None = None
    capturable_edge_to_cross_no_cents: float | None = None
    net_edge_after_entry_fee_yes_cents: float | None = None
    net_edge_after_entry_fee_no_cents: float | None = None
    net_edge_after_round_trip_fee_yes_cents: float | None = None
    net_edge_after_round_trip_fee_no_cents: float | None = None
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
_ScannerState = runtime_state._ScannerState

# Global in-memory stores (legacy compatibility aliases)
_top_signals_snapshot: TopSignalsSnapshot | None = runtime_state.top_signals_snapshot
_scanner_state = runtime_state.scanner_state

# Paper trading configuration (v2 - realistic)
_ENABLE_PAPER_TRADING = runtime_config.ENABLE_PAPER_TRADING
_PAPER_MIN_EDGE_CENTS = runtime_config.PAPER_MIN_EDGE_CENTS
_PAPER_MAX_CONCURRENT_INTENTS = runtime_config.PAPER_MAX_CONCURRENT_INTENTS
_PAPER_MAX_OPEN_ORDERS = runtime_config.PAPER_MAX_OPEN_ORDERS
_PAPER_MAX_POS_PER_TICKER = runtime_config.PAPER_MAX_POS_PER_TICKER
_PAPER_MAX_OPEN_POSITIONS = runtime_config.PAPER_MAX_OPEN_POSITIONS
_PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT = runtime_config.PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT
# Backward-compatible alias (semantics are cents PER CONTRACT).
_PAPER_TAKE_PROFIT_CENTS = runtime_config.PAPER_TAKE_PROFIT_CENTS
_PAPER_MAX_HOLD_SECONDS = runtime_config.PAPER_MAX_HOLD_SECONDS
_PAPER_FILL_HORIZON_SECONDS = runtime_config.PAPER_FILL_HORIZON_SECONDS
_PAPER_POLL_INTERVAL_SECONDS = runtime_config.PAPER_POLL_INTERVAL_SECONDS
_PAPER_FILL_PROBABILITY = runtime_config.PAPER_FILL_PROBABILITY
_PAPER_FILL_PROBABILITY_AT_BEST = runtime_config.PAPER_FILL_PROBABILITY_AT_BEST
_PAPER_FILL_PROBABILITY_PRICE_IMPROVES = runtime_config.PAPER_FILL_PROBABILITY_PRICE_IMPROVES
_PAPER_SIMULATED_VOLUME_PER_POLL = runtime_config.PAPER_SIMULATED_VOLUME_PER_POLL
_PAPER_MAX_INTENTS_PER_MINUTE = runtime_config.PAPER_MAX_INTENTS_PER_MINUTE
_PAPER_TICKER_COOLDOWN_SECONDS = runtime_config.PAPER_TICKER_COOLDOWN_SECONDS
_PAPER_MAX_DAILY_LOSS_CENTS = runtime_config.PAPER_MAX_DAILY_LOSS_CENTS

# Paper trading state tracking
_paper_store: PaperStore | None = runtime_state.paper_store
_paper_enabled: bool = runtime_state.paper_enabled
_paper_simulation_tasks: set[asyncio.Task[Any]] = runtime_state.paper_simulation_tasks
_paper_pnl_snapshot_interval_ms = runtime_config.PAPER_PNL_SNAPSHOT_INTERVAL_MS
_last_pnl_snapshot_ts_ms = runtime_state.last_pnl_snapshot_ts_ms
_paper_ticker_cooldown: dict[str, int] = runtime_state.paper_ticker_cooldown
_paper_intent_timestamps: list[int] = runtime_state.paper_intent_timestamps
_paper_daily_loss_breached: bool = runtime_state.paper_daily_loss_breached

PAPER_INTENT_STATUS_OPEN = "OPEN"
PAPER_INTENT_STATUS_FILLED = "FILLED"
# Legacy statuses kept for backward-compatible querying of historical rows.
# Active lifecycle no longer assigns these after entry-fill vs position-exit split.
PAPER_INTENT_STATUS_TAKE_PROFIT = "TAKE_PROFIT"
PAPER_INTENT_STATUS_MAX_HOLD_REACHED = "MAX_HOLD_REACHED"
PAPER_INTENT_STATUS_EXPIRED = "EXPIRED"
PAPER_INTENT_STATUS_ERROR = "ERROR"

PAPER_INTENT_ACTIVE_STATUSES = {
    PAPER_INTENT_STATUS_OPEN,
    PAPER_INTENT_STATUS_FILLED,
    PAPER_INTENT_STATUS_EXPIRED,
    PAPER_INTENT_STATUS_ERROR,
}
PAPER_INTENT_LEGACY_STATUSES = {
    PAPER_INTENT_STATUS_TAKE_PROFIT,
    PAPER_INTENT_STATUS_MAX_HOLD_REACHED,
}
PAPER_INTENT_ALLOWED_STATUSES = PAPER_INTENT_ACTIVE_STATUSES | PAPER_INTENT_LEGACY_STATUSES


def _sync_runtime_from_legacy() -> None:
    runtime_config.ENABLE_PAPER_TRADING = _ENABLE_PAPER_TRADING
    runtime_config.PAPER_MIN_EDGE_CENTS = _PAPER_MIN_EDGE_CENTS
    runtime_config.PAPER_MAX_CONCURRENT_INTENTS = _PAPER_MAX_CONCURRENT_INTENTS
    runtime_config.PAPER_MAX_OPEN_ORDERS = _PAPER_MAX_OPEN_ORDERS
    runtime_config.PAPER_MAX_POS_PER_TICKER = _PAPER_MAX_POS_PER_TICKER
    runtime_config.PAPER_MAX_OPEN_POSITIONS = _PAPER_MAX_OPEN_POSITIONS
    runtime_config.PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT = _PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT
    runtime_config.PAPER_TAKE_PROFIT_CENTS = _PAPER_TAKE_PROFIT_CENTS
    runtime_config.PAPER_MAX_HOLD_SECONDS = _PAPER_MAX_HOLD_SECONDS
    runtime_config.PAPER_FILL_HORIZON_SECONDS = _PAPER_FILL_HORIZON_SECONDS
    runtime_config.PAPER_POLL_INTERVAL_SECONDS = _PAPER_POLL_INTERVAL_SECONDS
    runtime_config.PAPER_FILL_PROBABILITY = _PAPER_FILL_PROBABILITY
    runtime_config.PAPER_FILL_PROBABILITY_AT_BEST = _PAPER_FILL_PROBABILITY_AT_BEST
    runtime_config.PAPER_FILL_PROBABILITY_PRICE_IMPROVES = _PAPER_FILL_PROBABILITY_PRICE_IMPROVES
    runtime_config.PAPER_SIMULATED_VOLUME_PER_POLL = _PAPER_SIMULATED_VOLUME_PER_POLL
    runtime_config.PAPER_MAX_INTENTS_PER_MINUTE = _PAPER_MAX_INTENTS_PER_MINUTE
    runtime_config.PAPER_TICKER_COOLDOWN_SECONDS = _PAPER_TICKER_COOLDOWN_SECONDS
    runtime_config.PAPER_MAX_DAILY_LOSS_CENTS = _PAPER_MAX_DAILY_LOSS_CENTS
    runtime_config.PAPER_PNL_SNAPSHOT_INTERVAL_MS = _paper_pnl_snapshot_interval_ms

    if _top_signals_snapshot is not None:
        runtime_state.top_signals_snapshot = _top_signals_snapshot
    runtime_state.paper_store = _paper_store
    runtime_state.paper_enabled = _paper_enabled
    runtime_state.paper_simulation_tasks = _paper_simulation_tasks
    runtime_state.last_pnl_snapshot_ts_ms = _last_pnl_snapshot_ts_ms
    runtime_state.paper_ticker_cooldown = _paper_ticker_cooldown
    runtime_state.paper_intent_timestamps = _paper_intent_timestamps
    runtime_state.paper_daily_loss_breached = _paper_daily_loss_breached


def _sync_legacy_from_runtime() -> None:
    global _ENABLE_PAPER_TRADING
    global _PAPER_MIN_EDGE_CENTS
    global _PAPER_MAX_CONCURRENT_INTENTS
    global _PAPER_MAX_OPEN_ORDERS
    global _PAPER_MAX_POS_PER_TICKER
    global _PAPER_MAX_OPEN_POSITIONS
    global _PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT
    global _PAPER_TAKE_PROFIT_CENTS
    global _PAPER_MAX_HOLD_SECONDS
    global _PAPER_FILL_HORIZON_SECONDS
    global _PAPER_POLL_INTERVAL_SECONDS
    global _PAPER_FILL_PROBABILITY
    global _PAPER_FILL_PROBABILITY_AT_BEST
    global _PAPER_FILL_PROBABILITY_PRICE_IMPROVES
    global _PAPER_SIMULATED_VOLUME_PER_POLL
    global _PAPER_MAX_INTENTS_PER_MINUTE
    global _PAPER_TICKER_COOLDOWN_SECONDS
    global _PAPER_MAX_DAILY_LOSS_CENTS
    global _paper_pnl_snapshot_interval_ms
    global _top_signals_snapshot
    global _paper_store
    global _paper_enabled
    global _paper_simulation_tasks
    global _last_pnl_snapshot_ts_ms
    global _paper_ticker_cooldown
    global _paper_intent_timestamps
    global _paper_daily_loss_breached

    _ENABLE_PAPER_TRADING = runtime_config.ENABLE_PAPER_TRADING
    _PAPER_MIN_EDGE_CENTS = runtime_config.PAPER_MIN_EDGE_CENTS
    _PAPER_MAX_CONCURRENT_INTENTS = runtime_config.PAPER_MAX_CONCURRENT_INTENTS
    _PAPER_MAX_OPEN_ORDERS = runtime_config.PAPER_MAX_OPEN_ORDERS
    _PAPER_MAX_POS_PER_TICKER = runtime_config.PAPER_MAX_POS_PER_TICKER
    _PAPER_MAX_OPEN_POSITIONS = runtime_config.PAPER_MAX_OPEN_POSITIONS
    _PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT = runtime_config.PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT
    _PAPER_TAKE_PROFIT_CENTS = runtime_config.PAPER_TAKE_PROFIT_CENTS
    _PAPER_MAX_HOLD_SECONDS = runtime_config.PAPER_MAX_HOLD_SECONDS
    _PAPER_FILL_HORIZON_SECONDS = runtime_config.PAPER_FILL_HORIZON_SECONDS
    _PAPER_POLL_INTERVAL_SECONDS = runtime_config.PAPER_POLL_INTERVAL_SECONDS
    _PAPER_FILL_PROBABILITY = runtime_config.PAPER_FILL_PROBABILITY
    _PAPER_FILL_PROBABILITY_AT_BEST = runtime_config.PAPER_FILL_PROBABILITY_AT_BEST
    _PAPER_FILL_PROBABILITY_PRICE_IMPROVES = runtime_config.PAPER_FILL_PROBABILITY_PRICE_IMPROVES
    _PAPER_SIMULATED_VOLUME_PER_POLL = runtime_config.PAPER_SIMULATED_VOLUME_PER_POLL
    _PAPER_MAX_INTENTS_PER_MINUTE = runtime_config.PAPER_MAX_INTENTS_PER_MINUTE
    _PAPER_TICKER_COOLDOWN_SECONDS = runtime_config.PAPER_TICKER_COOLDOWN_SECONDS
    _PAPER_MAX_DAILY_LOSS_CENTS = runtime_config.PAPER_MAX_DAILY_LOSS_CENTS
    _paper_pnl_snapshot_interval_ms = runtime_config.PAPER_PNL_SNAPSHOT_INTERVAL_MS

    _top_signals_snapshot = runtime_state.top_signals_snapshot
    _paper_store = runtime_state.paper_store
    _paper_enabled = runtime_state.paper_enabled
    _paper_simulation_tasks = runtime_state.paper_simulation_tasks
    _last_pnl_snapshot_ts_ms = runtime_state.last_pnl_snapshot_ts_ms
    _paper_ticker_cooldown = runtime_state.paper_ticker_cooldown
    _paper_intent_timestamps = runtime_state.paper_intent_timestamps
    _paper_daily_loss_breached = runtime_state.paper_daily_loss_breached


def _init_paper_store() -> None:
    _sync_runtime_from_legacy()
    _runtime_init_paper_store()
    _sync_legacy_from_runtime()


def _can_create_intent(ticker: str, now_ms: int) -> bool:
    _sync_runtime_from_legacy()
    allowed = _runtime_can_create_intent(ticker, now_ms)
    _sync_legacy_from_runtime()
    return allowed


def _get_rate_limit_or_cooldown_reason(ticker: str, now_ms: int) -> str | None:
    _sync_runtime_from_legacy()
    reason = _runtime_get_rate_limit_or_cooldown_reason(ticker, now_ms)
    _sync_legacy_from_runtime()
    return reason


def _get_mark_price_for_position(
    ticker: str,
    side: str,
    snapshot: TopSignalsSnapshot | None,
) -> float | None:
    return _runtime_get_mark_price_for_position(ticker, side, snapshot)


async def _fetch_mark_price_cents(
    client: KalshiClient,
    ticker: str,
    side: str,
) -> float | None:
    return await _runtime_fetch_mark_price_cents(client, ticker, side)


async def _update_fill_30s_outcomes(*, client: KalshiClient, now_ms: int) -> None:
    _sync_runtime_from_legacy()
    await _runtime_update_fill_30s_outcomes(client=client, now_ms=now_ms)
    _sync_legacy_from_runtime()


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
    _sync_runtime_from_legacy()
    closed = _runtime_close_position(
        ticker=ticker,
        side=side,
        net_contracts=net_contracts,
        avg_entry_price_cents=avg_entry_price_cents,
        realized_pnl_cents=realized_pnl_cents,
        first_open_ts_ms=first_open_ts_ms,
        mark_price_cents=mark_price_cents,
        now_ms=now_ms,
        reason=reason,
    )
    _sync_legacy_from_runtime()
    return closed


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
    _sync_runtime_from_legacy()
    closed = _runtime_maybe_close_position_take_profit(
        ticker=ticker,
        side=side,
        net_contracts=net_contracts,
        avg_entry_price_cents=avg_entry_price_cents,
        realized_pnl_cents=realized_pnl_cents,
        first_open_ts_ms=first_open_ts_ms,
        mark_price_cents=mark_price_cents,
        now_ms=now_ms,
    )
    _sync_legacy_from_runtime()
    return closed


async def _process_paper_intents(
    signal: Signal,
    client: KalshiClient,
) -> None:
    _sync_runtime_from_legacy()
    build_quote_intents_attr = "build_quote_intents"
    simulator_cls_attr = "RealisticOrderSimulator"
    setattr(_runtime_paper_runtime_module, build_quote_intents_attr, build_quote_intents)
    setattr(_runtime_paper_runtime_module, simulator_cls_attr, RealisticOrderSimulator)
    await _runtime_process_paper_intents(signal, client)
    _sync_legacy_from_runtime()


async def _nba_scanner_loop(client: KalshiClient) -> None:
    _sync_runtime_from_legacy()
    try:
        await _runtime_nba_scanner_loop(
            client,
            signal_to_response=_signal_to_response,
            signal_response_model=SignalResponse,
            top_signals_snapshot_model=TopSignalsSnapshot,
        )
    finally:
        _sync_legacy_from_runtime()


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
        "intended_quote_yes_cents": signal.intended_quote_yes_cents,
        "intended_quote_no_cents": signal.intended_quote_no_cents,
        "capturable_edge_to_cross_yes_cents": signal.capturable_edge_to_cross_yes_cents,
        "capturable_edge_to_cross_no_cents": signal.capturable_edge_to_cross_no_cents,
        "net_edge_after_entry_fee_yes_cents": signal.net_edge_after_entry_fee_yes_cents,
        "net_edge_after_entry_fee_no_cents": signal.net_edge_after_entry_fee_no_cents,
        "net_edge_after_round_trip_fee_yes_cents": signal.net_edge_after_round_trip_fee_yes_cents,
        "net_edge_after_round_trip_fee_no_cents": signal.net_edge_after_round_trip_fee_no_cents,
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
    snapshot: TopSignalsSnapshot | None
    if _top_signals_snapshot is not None:
        snapshot = _top_signals_snapshot
    elif _scanner_state.last_scan_ms is not None:
        snapshot = runtime_state.top_signals_snapshot
    else:
        snapshot = None
    if snapshot is None:
        raise HTTPException(
            status_code=503,
            detail="Signal snapshot not available yet (scanner starting up)",
        )

    # Limit to requested count
    signals = snapshot.signals[:n]

    return {
        "timestamp_ms": snapshot.timestamp_ms,
        "signals": [s.model_dump() for s in signals],
        "market_count": snapshot.market_count,
    }


async def start_scanner(request: Request) -> dict[str, Any]:
    """Start the NBA background scanner.

    If scanner is not running, creates a background task and starts it.
    If already running, returns a status indicating it's already running.

    Returns:
        Response indicating whether scanner was started or if it was already running.
    """
    _sync_runtime_from_legacy()
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
    _sync_runtime_from_legacy()

    logger.info("NBA scanner started via POST /scanner/start")
    return {"started": True}


async def stop_scanner(request: Request) -> dict[str, Any]:
    """Stop the NBA background scanner.

    Cancels the scanner task cleanly and updates state.

    Returns:
        Response indicating whether scanner was stopped or if it wasn't running.
    """
    _sync_runtime_from_legacy()
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
    runtime_state.top_signals_snapshot = None
    _sync_legacy_from_runtime()

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
    _sync_runtime_from_legacy()

    if not _ENABLE_PAPER_TRADING or _paper_store is None:
        logger.info("Paper trading not available (not enabled or no store)")
        return {"started": False, "reason": "not_available"}

    if _paper_enabled:
        logger.info("Paper trading already running, ignoring start request")
        return {"started": False, "reason": "already_running"}

    _paper_enabled = True
    _sync_runtime_from_legacy()
    _sync_legacy_from_runtime()
    logger.info("Paper trading started via POST /paper/start")
    return {"started": True}


async def stop_paper_trading() -> dict[str, Any]:
    """Stop paper trading and cancel pending intent simulations.

    Disables the paper trading flag and cancels any pending simulation tasks.

    Returns:
        Response indicating whether paper trading was stopped.
    """
    global _paper_enabled
    _sync_runtime_from_legacy()

    if not _paper_enabled:
        logger.info("Paper trading not running, ignoring stop request")
        return {"stopped": False, "reason": "not_running"}

    _paper_enabled = False
    _sync_runtime_from_legacy()

    # Cancel all pending simulation tasks
    for task in list(_paper_simulation_tasks):
        if not task.done():
            task.cancel()

    if _paper_simulation_tasks:
        # Gather with return_exceptions to avoid raising on cancelled tasks
        await asyncio.gather(*_paper_simulation_tasks, return_exceptions=True)
        logger.info("paper_trading_tasks_cancelled", extra={"count": len(_paper_simulation_tasks)})
        _paper_simulation_tasks.clear()

    _sync_runtime_from_legacy()
    _sync_legacy_from_runtime()

    logger.info("Paper trading stopped via POST /paper/stop")
    return {"stopped": True}


# Paper Trading Endpoints
async def get_paper_intents(
    status: str = PAPER_INTENT_STATUS_OPEN, limit: int = 50
) -> dict[str, Any]:
    """Get paper trading intents by status.

    Args:
        status: Filter by status.
            Active statuses: OPEN, FILLED, EXPIRED, ERROR.
            Legacy read-only statuses: TAKE_PROFIT, MAX_HOLD_REACHED.
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
        "total_fills": _paper_store.count_fills_total(),
        "total_positions": metrics["open_positions_count"],
        "realized_pnl_cents": metrics["realized_pnl_cents"],
        "unrealized_pnl_cents": metrics.get("unrealized_pnl_cents", 0.0),
        "total_pnl_cents": metrics["total_pnl_cents"],
        "intent_fill_rate": metrics.get("intent_fill_rate", 0.0),
        "raw_fills_per_intent_ratio": metrics.get("raw_fills_per_intent_ratio", 0.0),
        "intents_with_at_least_one_fill": metrics.get("intents_with_at_least_one_fill", 0),
        "fully_filled_intents": metrics.get("fully_filled_intents", 0),
        "partial_fill_intents": metrics.get("partial_fill_intents", 0),
        "partial_fill_rate": metrics.get("partial_fill_rate", 0.0),
        "average_filled_fraction": metrics.get("average_filled_fraction", 0.0),
        "average_time_to_first_fill_seconds": metrics.get("average_time_to_first_fill_seconds"),
        "median_time_to_first_fill_seconds": metrics.get("median_time_to_first_fill_seconds"),
        "average_time_to_full_fill_seconds": metrics.get("average_time_to_full_fill_seconds"),
        "median_time_to_full_fill_seconds": metrics.get("median_time_to_full_fill_seconds"),
        "unrealized_marks_available": metrics.get("unrealized_marks_available", False),
        "unrealized_pnl_status": metrics.get("unrealized_pnl_status", "marks_unavailable"),
        "unrealized_marked_positions_count": metrics.get("unrealized_marked_positions_count", 0),
        "unrealized_unmarked_positions_count": metrics.get(
            "unrealized_unmarked_positions_count", 0
        ),
        "latest_snapshot_ts_ms": snapshot["ts_ms"] if snapshot else None,
    }


async def get_paper_metrics() -> dict[str, Any]:
    """Get lightweight paper activity metrics for dashboard observability."""
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    metrics = _paper_store.get_activity_metrics(window_seconds=3600)
    return {
        "fill_rate": metrics["fill_rate"],
        "raw_fills_per_intent_ratio": metrics.get("raw_fills_per_intent_ratio", 0.0),
        "intents_with_at_least_one_fill": metrics.get("intents_with_at_least_one_fill", 0),
        "avg_time_to_fill_seconds": metrics["avg_time_to_fill_seconds"],
        "fills_last_1h": metrics["fills_last_1h"],
        "intents_last_1h": metrics["intents_last_1h"],
    }


async def get_paper_outcome_metrics(window_seconds: int = 86_400) -> dict[str, Any]:
    """Get short-horizon edge-vs-outcome telemetry aggregates."""
    if not _paper_store:
        raise HTTPException(status_code=503, detail="Paper trading not enabled")

    return _paper_store.get_short_horizon_outcome_metrics(window_seconds=window_seconds)


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
        # Backward-compatible key kept for API stability.
        "take_profit_cents": _PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT,
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
        max_hold_seconds=int(intent["max_hold_seconds"] or _PAPER_MAX_HOLD_SECONDS),
        take_profit_cents_per_contract=float(
            intent.get("take_profit_cents_per_contract")
            or intent.get("take_profit_cents")
            or _PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT
        ),
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
    fill_id = f"debug:{intent_id}:{fill_ts_ms}"
    fill_insert_result = _paper_store.insert_fill_result(
        fill_id=fill_id,
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

    if not fill_insert_result.inserted_new_fill:
        logger.warning(
            "paper_debug_force_fill_insert_rejected",
            extra={
                "intent_id": intent_id,
                "fill_id": fill_id,
                "ticker": intent["ticker"],
                "side": intent["side"],
                "fill_size": actual_fill_size,
                "insert_status": fill_insert_result.status,
            },
        )
        return {
            "intent_id": intent_id,
            "filled_size": 0,
            "remaining_size": remaining_before,
            "status": intent.get("status", PAPER_INTENT_STATUS_OPEN),
        }

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
    snapshot: TopSignalsSnapshot | None
    if _top_signals_snapshot is not None:
        snapshot = _top_signals_snapshot
    elif _scanner_state.last_scan_ms is not None:
        snapshot = runtime_state.top_signals_snapshot
    else:
        snapshot = None
    now_ms = int(time.time() * 1000)
    snapshot_age_ms: int | None = None

    if snapshot is not None:
        snapshot_age_ms = now_ms - snapshot.timestamp_ms

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
        "snapshot_timestamp_ms": snapshot.timestamp_ms if snapshot else None,
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
        _sync_runtime_from_legacy()
        client = KalshiClient()
        app.state.kalshi_client = client
        app.state.scanner_task = None  # Initialize scanner_task; may be updated below
        logger.info("KalshiClient initialized and stored on app.state")

        # Initialize paper trading store if enabled
        if _ENABLE_PAPER_TRADING:
            _init_paper_store()

        # Check if NBA scanner is enabled via environment variable
        enable_scanner = runtime_config.is_nba_scanner_enabled_from_env()

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
        _sync_runtime_from_legacy()
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
        _sync_legacy_from_runtime()
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
    app_instance.get("/paper/metrics/outcomes")(get_paper_outcome_metrics)
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
