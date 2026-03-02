"""Market universe discovery."""

import logging
import time
from dataclasses import dataclass

from kalshi_edge_engine.kalshi.client import KalshiClient

logger = logging.getLogger(__name__)


CACHE_TTL_SECONDS = 60


@dataclass
class NBAUniverse:
    """NBA market universe response."""

    series_ticker: str
    event_tickers: list[str]
    market_tickers: list[str]


@dataclass
class CachedNBAUniverse:
    """Cached NBA universe with timestamp."""

    universe: NBAUniverse
    timestamp: float


# Global cache (in-memory)
_nba_universe_cache: CachedNBAUniverse | None = None


async def discover_nba_universe(client: KalshiClient) -> NBAUniverse:
    """Discover NBA markets from Kalshi API.

    Fetches all open markets for KXNBAGAME series with full pagination.
    Caches results for 60 seconds.

    Args:
        client: Authenticated Kalshi client.

    Returns:
        NBAUniverse containing series ticker and market tickers.

    Raises:
        KalshiAPIError: If API requests fail.
    """
    global _nba_universe_cache

    # Check cache
    if _nba_universe_cache is not None:
        age = time.time() - _nba_universe_cache.timestamp
        if age < CACHE_TTL_SECONDS:
            logger.info(
                "nba_universe_cache_hit",
                extra={"cache_age_seconds": age},
            )
            return _nba_universe_cache.universe

    # Cache miss or stale - refresh
    logger.info("nba_universe_cache_miss_refreshing")

    series_ticker = "KXNBAGAME"
    logger.info(
        "nba_universe_refresh_started",
        extra={"series": series_ticker},
    )

    start_time = time.time()

    # Fetch all open markets for KXNBAGAME series
    market_tickers = await _find_nba_markets(client, series_ticker, status="open")

    latency_ms = (time.time() - start_time) * 1000
    logger.info(
        "nba_universe_markets_count",
        extra={"count": len(market_tickers)},
    )
    logger.info(
        "nba_universe_refresh_complete",
        extra={
            "series_ticker": series_ticker,
            "market_count": len(market_tickers),
            "latency_ms": latency_ms,
        },
    )

    universe = NBAUniverse(
        series_ticker=series_ticker,
        event_tickers=[],  # Not used in deterministic KXNBAGAME approach
        market_tickers=market_tickers,
    )

    _nba_universe_cache = CachedNBAUniverse(
        universe=universe,
        timestamp=time.time(),
    )

    return universe


async def _find_nba_markets(
    client: KalshiClient,
    series_ticker: str,
    status: str = "open",
) -> list[str]:
    """Find markets for NBA series.

    Fetches all markets for series with full pagination.
    Market tickers are used exactly as returned by the API.

    Args:
        client: Authenticated Kalshi client.
        series_ticker: NBA series ticker.
        status: Market status filter (default: "open").

    Returns:
        List of market tickers (as returned by API, sorted).
    """
    market_tickers: set[str] = set()
    cursor: str | None = None

    while True:
        response = await client.get_markets(
            series_ticker=series_ticker,
            status=status,
            limit=200,
            cursor=cursor,
        )

        markets = response.get("markets", [])
        for market in markets:
            ticker = market.get("ticker", "")
            if ticker:
                market_tickers.add(ticker)

        # Check for cursor to continue pagination
        cursor = response.get("cursor")
        if not cursor:
            break

    return sorted(market_tickers)
