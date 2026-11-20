"""Short-lived fee metadata shared by the scanner and paper workers."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kalshi_edge_engine.engine.fees import FeeProfile, series_fee_profile
from kalshi_edge_engine.kalshi.client import KalshiClient
from kalshi_edge_engine.kalshi.market import timestamp_ms

logger = logging.getLogger(__name__)
ObservedMetadata = tuple[dict[str, Any], int]


class MarketFees:
    """Resolve market → event → series without guessing the ticker format."""

    def __init__(self, client: KalshiClient) -> None:
        self.client = client
        self._cache: dict[tuple[str, str], tuple[float, ObservedMetadata | None]] = {}
        self._locks = [asyncio.Lock() for _ in range(16)]

    async def _record(
        self, kind: str, ticker: str, fetch: Callable[[str], Awaitable[dict[str, Any]]]
    ) -> ObservedMetadata | None:
        key = (kind, ticker)
        async with self._locks[hash(key) % len(self._locks)]:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached is not None and now < cached[0]:
                return cached[1]
            if len(self._cache) >= 1024:
                self._cache = {key: value for key, value in self._cache.items() if value[0] > now}
                if len(self._cache) >= 1024:
                    self._cache.pop(next(iter(self._cache)))
            try:
                record = (await fetch(ticker), int(time.time() * 1000))
            except Exception:
                logger.debug(
                    "paper_fee_metadata_unavailable", extra={"kind": kind, "ticker": ticker}
                )
                record = None
            self._cache[key] = (time.monotonic() + (60 if record is not None else 10), record)
            return record

    async def resolve(self, ticker: str, *, for_entry: bool = False) -> FeeProfile | None:
        market_record = await self._record("market", ticker, self.client.get_market)
        if market_record is None:
            return None
        market, market_ts = market_record
        event_ticker = market.get("event_ticker")
        if not isinstance(event_ticker, str) or not event_ticker:
            return None
        now_ms = int(time.time() * 1000)
        close_ts = timestamp_ms(market.get("close_time"))
        if for_entry and (
            market.get("status") != "active" or (close_ts is not None and close_ts <= now_ms)
        ):
            return None
        waiver_value = market.get("fee_waiver_expiration_time")
        waiver_end = timestamp_ms(waiver_value)
        if waiver_value and (waiver_end is None or waiver_end > now_ms):
            return None  # Promotional terms require a verified fee model.
        event_record = await self._record("event", event_ticker, self.client.get_event)
        if event_record is None:
            return None
        event, event_ts = event_record
        series_ticker = event.get("series_ticker")
        if not isinstance(series_ticker, str) or not series_ticker:
            return None
        series_record = await self._record("series", series_ticker, self.client.get_series_details)
        if series_record is None:
            return None
        series, series_ts = series_record
        return series_fee_profile(series, event, observed_ts_ms=min(market_ts, event_ts, series_ts))
