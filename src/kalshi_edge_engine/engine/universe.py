"""Market universe discovery."""

import logging
import time
from dataclasses import dataclass

from kalshi_edge_engine.kalshi.client import KalshiClient

logger = logging.getLogger(__name__)


MAX_MARKETS_PER_DISCOVERY = 400
MAX_EVENTS_PER_DISCOVERY = 500
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

    Uses series/events/markets discovery endpoints with pagination.
    Caches results for 60 seconds.

    Args:
        client: Authenticated Kalshi client.

    Returns:
        NBAUniverse containing series ticker, event tickers, and market tickers.

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
    start_time = time.time()

    # Step 1: Find NBA series
    nba_series_ticker = await _find_nba_series(client)
    if not nba_series_ticker:
        # Fallback: return empty universe if no NBA series found
        logger.warning("no_nba_series_found")
        empty_universe = NBAUniverse(
            series_ticker="",
            event_tickers=[],
            market_tickers=[],
        )
        _nba_universe_cache = CachedNBAUniverse(
            universe=empty_universe,
            timestamp=time.time(),
        )
        return empty_universe

    # Step 2: Find events for NBA series (status=open)
    event_tickers = await _find_nba_events(client, nba_series_ticker)

    # Step 3: Find markets (try nested first, fallback to separate call)
    market_tickers = await _find_nba_markets(client, nba_series_ticker, event_tickers)

    latency_ms = (time.time() - start_time) * 1000
    logger.info(
        "nba_universe_refreshed",
        extra={
            "series_ticker": nba_series_ticker,
            "event_count": len(event_tickers),
            "market_count": len(market_tickers),
            "latency_ms": latency_ms,
        },
    )

    universe = NBAUniverse(
        series_ticker=nba_series_ticker,
        event_tickers=event_tickers,
        market_tickers=market_tickers,
    )

    _nba_universe_cache = CachedNBAUniverse(
        universe=universe,
        timestamp=time.time(),
    )

    return universe


async def _find_nba_series(client: KalshiClient) -> str:
    """Find NBA series ticker.

    Prefers series where ticker contains "NBA" (strong match).
    Falls back to title/category heuristics if no strong match found.

    Returns:
        NBA series ticker (e.g., 'KXNBA') or empty string if not found.
    """
    cursor: str | None = None
    max_pages = 10  # Safety limit
    fallback_match: str = ""

    for _page in range(max_pages):
        response = await client.get_series(limit=200, cursor=cursor)
        series_list = response.get("series", [])

        for series in series_list:
            ticker_raw = series.get("ticker", "")
            ticker = str(ticker_raw).upper()
            category_raw = series.get("category", "")
            category = str(category_raw).lower()
            title_raw = series.get("title", "")
            title = str(title_raw).lower()

            # Strong match: ticker itself contains "NBA"
            if "NBA" in ticker:
                logger.info("nba_series_found_strong", extra={"ticker": ticker})
                return ticker

            # Weak match: category or title suggests NBA (save as fallback)
            if not fallback_match and (
                "nba" in title or (category == "sports" and "basketball" in title)
            ):
                fallback_match = ticker

        # Check for cursor to continue pagination
        cursor = response.get("cursor")
        if not cursor:
            break

    # Return fallback if found
    if fallback_match:
        logger.info("nba_series_found_fallback", extra={"ticker": fallback_match})
        return fallback_match

    return ""


async def _find_nba_events(client: KalshiClient, series_ticker: str) -> list[str]:
    """Find open events for NBA series.

    Args:
        client: Authenticated Kalshi client.
        series_ticker: NBA series ticker.

    Returns:
        List of event tickers (uppercase, deduped, sorted).
    """
    event_tickers: set[str] = set()
    cursor: str | None = None
    max_pages = 10  # Safety limit

    for _page in range(max_pages):
        response = await client.get_events(
            series_ticker=series_ticker,
            status="open",
            with_nested_markets=False,  # Don't fetch markets here
            limit=200,
            cursor=cursor,
        )

        events = response.get("events", [])
        for event in events:
            ticker = event.get("event_ticker", "").upper()
            if ticker:
                event_tickers.add(ticker)

        # Stop if we hit the limit
        if len(event_tickers) >= MAX_EVENTS_PER_DISCOVERY:
            logger.warning(
                "event_discovery_limit_reached",
                extra={"limit": MAX_EVENTS_PER_DISCOVERY},
            )
            break

        # Check for cursor to continue pagination
        cursor = response.get("cursor")
        if not cursor:
            break

    return sorted(event_tickers)


async def _find_nba_markets(
    client: KalshiClient,
    series_ticker: str,
    _event_tickers: list[str],
) -> list[str]:
    """Find markets for NBA series.

    Fetches all markets for series (Kalshi API filters applied server-side).
    Tries to fetch markets efficiently via series_ticker filter.

    Args:
        client: Authenticated Kalshi client.
        series_ticker: NBA series ticker.
        event_tickers: List of event tickers (unused but kept for future).

    Returns:
        List of market tickers (uppercase, deduped, sorted).
    """
    market_tickers: set[str] = set()
    cursor: str | None = None
    max_pages = 4  # Safety limit (200 markets/page * 4 = 800 max before hitting limit)

    for _page in range(max_pages):
        response = await client.get_markets(
            series_ticker=series_ticker,
            limit=200,
            cursor=cursor,
        )

        markets = response.get("markets", [])
        for market in markets:
            ticker = market.get("ticker", "").upper()
            if ticker:
                market_tickers.add(ticker)

        # Stop if we hit the limit
        if len(market_tickers) >= MAX_MARKETS_PER_DISCOVERY:
            logger.warning(
                "market_discovery_limit_reached",
                extra={"limit": MAX_MARKETS_PER_DISCOVERY},
            )
            break

        # Check for cursor to continue pagination
        cursor = response.get("cursor")
        if not cursor:
            break

    return sorted(market_tickers)
