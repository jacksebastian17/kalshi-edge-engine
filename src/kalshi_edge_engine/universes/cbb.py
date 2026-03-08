"""College basketball universe discovery and caching.

This follows the NBA-style design:
- Query a single game-level series ticker.
- Filter conservatively to full-game binary moneyline-style rows only.
"""

import logging
import time
from dataclasses import dataclass

from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.universes.base import CachedUniverse, MarketUniverse

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 60
CBB_SERIES_TICKER = "KXNCAAMBGAME"


def is_cbb_full_game_moneyline_market(ticker: str) -> bool:
    """Return True only for full-game CBB moneyline-style game tickers.

    Inclusion is intentionally strict:
    - Must start with a known full-game CBB game series prefix.
    - Must include at least one '-' separator after the series prefix.

    Explicit exclusions:
    - Partial game markets (e.g. 1H winner / half / quarter variants).
    - 3-way tie outcome rows ending in '-TIE'.
    - Non-game futures/outright markets (implicitly excluded by prefix gate).
    """
    normalized = (ticker or "").strip().upper()
    if not normalized:
        return False

    # Hard-include only the primary full-game CBB game series.
    if not normalized.startswith(f"{CBB_SERIES_TICKER}-"):
        return False

    # Require an identifier segment after PREFIX-
    if "-" not in normalized:
        return False

    # Explicitly reject partial-game variants and tie outcomes.
    if "1HWINNER" in normalized or "-1H" in normalized or "HALF" in normalized:
        return False
    if "QTR" in normalized or "QUARTER" in normalized:
        return False
    return not normalized.endswith("-TIE")


@dataclass
class CBBUniverse(MarketUniverse):
    """College basketball market universe response."""


@dataclass
class CachedCBBUniverse(CachedUniverse[CBBUniverse]):
    """Cached CBB universe with timestamp."""


_cbb_universe_cache: CachedCBBUniverse | None = None


async def _find_markets_for_series(
    client: KalshiClient,
    series_ticker: str,
    status: str = "open",
) -> list[str]:
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
            if ticker and is_cbb_full_game_moneyline_market(ticker):
                market_tickers.add(ticker)

        cursor = response.get("cursor")
        if not cursor:
            break

    return sorted(market_tickers)


async def discover_cbb_universe(client: KalshiClient) -> CBBUniverse:
    """Discover CBB markets from a single game-level series ticker."""
    global _cbb_universe_cache

    if _cbb_universe_cache is not None:
        age = time.time() - _cbb_universe_cache.timestamp
        if age < CACHE_TTL_SECONDS:
            logger.info("cbb_universe_cache_hit", extra={"cache_age_seconds": age})
            return _cbb_universe_cache.universe

    logger.info("cbb_universe_cache_miss_refreshing")
    logger.info("cbb_universe_refresh_started", extra={"series": CBB_SERIES_TICKER})
    start_time = time.time()

    sorted_tickers = await _find_markets_for_series(
        client,
        CBB_SERIES_TICKER,
        status="open",
    )
    latency_ms = (time.time() - start_time) * 1000

    if not sorted_tickers:
        logger.info(
            "cbb_universe_empty",
            extra={
                "series_ticker": CBB_SERIES_TICKER,
                "latency_ms": latency_ms,
            },
        )

    logger.info(
        "cbb_universe_refresh_complete",
        extra={
            "series_ticker": CBB_SERIES_TICKER,
            "market_count": len(sorted_tickers),
            "latency_ms": latency_ms,
        },
    )

    universe = CBBUniverse(
        series_ticker=CBB_SERIES_TICKER,
        event_tickers=[],
        market_tickers=sorted_tickers,
    )

    _cbb_universe_cache = CachedCBBUniverse(universe=universe, timestamp=time.time())
    return universe
