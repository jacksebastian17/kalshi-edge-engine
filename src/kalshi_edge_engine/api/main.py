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

    return app_instance


# Create default app instance for production use
app = create_app()


# Development server entry point
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
