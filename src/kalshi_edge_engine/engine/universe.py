"""Compatibility shim for legacy NBA universe imports.

Prefer importing from `kalshi_edge_engine.universes.nba`.
"""

from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.universes import nba as nba_universe
from kalshi_edge_engine.universes.nba import (
    CACHE_TTL_SECONDS,
    CachedNBAUniverse,
    NBAUniverse,
    _find_nba_markets,
)

# Keep a legacy module-level cache binding so existing tests and callers that
# reset `engine.universe._nba_universe_cache` continue to work.
_nba_universe_cache = nba_universe._nba_universe_cache


async def discover_nba_universe(client: KalshiClient) -> NBAUniverse:
    """Backward-compatible wrapper for NBA universe discovery."""
    global _nba_universe_cache

    nba_universe._nba_universe_cache = _nba_universe_cache
    universe = await nba_universe.discover_nba_universe(client)
    _nba_universe_cache = nba_universe._nba_universe_cache
    return universe


__all__ = [
    "CACHE_TTL_SECONDS",
    "NBAUniverse",
    "CachedNBAUniverse",
    "discover_nba_universe",
    "_find_nba_markets",
    "_nba_universe_cache",
]
