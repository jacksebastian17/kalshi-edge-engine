"""FastAPI application for Kalshi Edge Engine."""

import logging
import time
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from kalshi_edge_engine.engine.signal import Signal, score_signal
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

app = FastAPI(
    title="Kalshi Edge Engine",
    description="Real-time signal generation and dashboard backend",
    version="0.1.0",
)


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
    top_liquidity: int
    timestamp_ms: int


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


@app.get("/health", response_model=HealthResponse)
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/version", response_model=VersionResponse)
async def version() -> dict[str, Any]:
    """Version information endpoint."""
    return {
        "name": "kalshi-edge-engine",
        "version": "0.1.0",
    }


@app.get("/debug/orderbook/{ticker}")
async def debug_orderbook(ticker: str) -> dict[str, Any]:
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
        # Initialize client (will read env vars)
        client = KalshiClient()

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


@app.get("/signals/live", response_model=SignalResponse)
async def get_live_signal(ticker: str) -> dict[str, Any]:
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
        # Initialize client (will read env vars)
        client = KalshiClient()

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


@app.get("/signals/scan", response_model=list[SignalResponse])
async def scan_live_signals(tickers: str) -> list[dict[str, Any]]:
    """Scan multiple tickers and return sorted signal opportunities."""
    parsed_tickers = _parse_tickers(tickers)
    if not parsed_tickers:
        logger.warning("scan_invalid_tickers", extra={"tickers": tickers})
        raise HTTPException(status_code=400, detail="At least one valid ticker is required")
    if len(parsed_tickers) > 20:
        logger.warning("scan_too_many_tickers", extra={"count": len(parsed_tickers)})
        raise HTTPException(status_code=400, detail="Maximum 20 tickers per request")

    start_time = time.time()

    try:
        client = KalshiClient()
        scored_signals: list[Signal] = []
        for ticker in parsed_tickers:
            try:
                orderbook = await client.get_orderbook(ticker)
                scored_signals.append(score_signal(ticker, orderbook))
            except KalshiAPIError as e:
                logger.warning(
                    "scan_ticker_failed",
                    extra={
                        "ticker": ticker,
                        "error": str(e),
                    },
                )
                continue

        if not scored_signals:
            raise HTTPException(status_code=502, detail="Upstream API error")

        scored_signals.sort(
            key=lambda signal: max(
                signal.net_edge_yes if signal.net_edge_yes is not None else float("-inf"),
                signal.net_edge_no if signal.net_edge_no is not None else float("-inf"),
            ),
            reverse=True,
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
