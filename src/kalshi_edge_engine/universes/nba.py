"""NBA market universe discovery and caching."""

import logging
import time
from dataclasses import dataclass

from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.universes.base import CachedUniverse, MarketUniverse

logger = logging.getLogger(__name__)


CACHE_TTL_SECONDS = 60
NBA_SERIES_TICKER = "KXNBAGAME"


@dataclass
class NBAUniverse(MarketUniverse):
    """NBA market universe response."""


@dataclass
class CachedNBAUniverse(CachedUniverse[NBAUniverse]):
    """Cached NBA universe with timestamp."""


# Global cache (in-memory)
_nba_universe_cache: CachedNBAUniverse | None = None


async def discover_nba_universe(client: KalshiClient) -> NBAUniverse:
    """Discover NBA markets from Kalshi API.

    Fetches all open markets for KXNBAGAME series with full pagination.
    Caches results for 60 seconds.
    """
    global _nba_universe_cache

    if _nba_universe_cache is not None:
        age = time.time() - _nba_universe_cache.timestamp
        if age < CACHE_TTL_SECONDS:
            logger.info(
                "nba_universe_cache_hit",
                extra={"cache_age_seconds": age},
            )
            return _nba_universe_cache.universe

    logger.info("nba_universe_cache_miss_refreshing")
    logger.info(
        "nba_universe_refresh_started",
        extra={"series": NBA_SERIES_TICKER},
    )

    start_time = time.time()
    market_tickers = await _find_nba_markets(client, NBA_SERIES_TICKER, status="open")

    latency_ms = (time.time() - start_time) * 1000
    logger.info("nba_universe_markets_count", extra={"count": len(market_tickers)})
    logger.info(
        "nba_universe_refresh_complete",
        extra={
            "series_ticker": NBA_SERIES_TICKER,
            "market_count": len(market_tickers),
            "latency_ms": latency_ms,
        },
    )

    universe = NBAUniverse(
        series_ticker=NBA_SERIES_TICKER,
        event_tickers=[],
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
    """Find markets for an NBA series using full pagination."""
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

        cursor = response.get("cursor")
        if not cursor:
            break

    return sorted(market_tickers)
