"""Shared abstractions for Kalshi market universe discovery."""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from kalshi_edge_engine.kalshi.client import KalshiClient


@dataclass
class MarketUniverse:
    """Common shape for discovered market universes."""

    series_ticker: str
    event_tickers: list[str]
    market_tickers: list[str]


TUniverse = TypeVar("TUniverse", bound=MarketUniverse)
TUniverseCo = TypeVar("TUniverseCo", bound=MarketUniverse, covariant=True)


@dataclass
class CachedUniverse(Generic[TUniverse]):
    """Simple in-memory cache wrapper for discovered universes."""

    universe: TUniverse
    timestamp: float


class UniverseDiscoverer(Protocol[TUniverseCo]):
    """Protocol for universe-specific discovery implementations."""

    async def discover(self, client: KalshiClient) -> TUniverseCo: ...
