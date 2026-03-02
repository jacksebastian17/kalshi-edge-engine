"""Tests for universe discovery module."""

import time
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.engine.universe import (
    _find_nba_events,
    _find_nba_markets,
    _find_nba_series,
    discover_nba_universe,
)


class TestFindNBASeries:
    """Test _find_nba_series function."""

    @pytest.mark.asyncio
    async def test_find_nba_series_first_page(self) -> None:
        """Test finding NBA series on first page."""
        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "OTHER", "category": "politics", "title": "Election"},
                    {"ticker": "KXNBA", "category": "sports", "title": "NBA Games"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_series(mock_client)
        assert result == "KXNBA"
        mock_client.get_series.assert_called_once()

    @pytest.mark.asyncio
    async def test_find_nba_series_pagination(self) -> None:
        """Test finding NBA series with pagination."""
        mock_client = AsyncMock()

        # Mock paginated responses
        def get_series_side_effect(**kwargs: any) -> dict:
            cursor = kwargs.get("cursor")
            if cursor is None:
                return {
                    "series": [
                        {"ticker": "OTHER1", "category": "politics", "title": "Election"},
                    ],
                    "cursor": "next_page",
                }
            else:
                return {
                    "series": [
                        {"ticker": "KXNBA", "category": "sports", "title": "NBA Games"},
                    ],
                    "cursor": None,
                }

        mock_client.get_series = AsyncMock(side_effect=get_series_side_effect)

        result = await _find_nba_series(mock_client)
        assert result == "KXNBA"
        assert mock_client.get_series.call_count == 2

    @pytest.mark.asyncio
    async def test_find_nba_series_not_found(self) -> None:
        """Test when NBA series is not found."""
        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "OTHER", "category": "politics", "title": "Election"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_series(mock_client)
        assert result == ""

    @pytest.mark.asyncio
    async def test_find_nba_series_title_match(self) -> None:
        """Test finding NBA series by title matching."""
        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "KXNBA", "category": "other", "title": "NBA Basketball"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_series(mock_client)
        assert result == "KXNBA"

    @pytest.mark.asyncio
    async def test_find_nba_series_ticker_match_preferred(self) -> None:
        """Test that ticker-based match is preferred over title match."""
        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    # Title match comes first, but ticker match should win
                    {"ticker": "KXBB", "category": "sports", "title": "NBA Basketball"},
                    {"ticker": "KXNBA", "category": "other", "title": "Sports Event"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_series(mock_client)
        # Should return KXNBA because ticker contains "NBA"
        assert result == "KXNBA"

    @pytest.mark.asyncio
    async def test_find_nba_series_ticker_substring_match(self) -> None:
        """Test that ticker containing NBA is matched."""
        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "OTHER", "category": "politics", "title": "Election"},
                    {"ticker": "SPORTNBA", "category": "sports", "title": "Basketball"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_series(mock_client)
        assert result == "SPORTNBA"

    @pytest.mark.asyncio
    async def test_find_nba_series_fallback_when_no_ticker_match(self) -> None:
        """Test fallback to title match when no ticker match exists."""
        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "KXBB", "category": "sports", "title": "NBA Season"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_series(mock_client)
        # Should fall back to title match since no ticker contains "NBA"
        assert result == "KXBB"


class TestFindNBAEvents:
    """Test _find_nba_events function."""

    @pytest.mark.asyncio
    async def test_find_nba_events_single_page(self) -> None:
        """Test finding events on single page."""
        mock_client = AsyncMock()
        mock_client.get_events = AsyncMock(
            return_value={
                "events": [
                    {"event_ticker": "KXNBAGAME-26MAR01"},
                    {"event_ticker": "KXNBAGAME-26MAR02"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_events(mock_client, "KXNBA")
        assert result == ["KXNBAGAME-26MAR01", "KXNBAGAME-26MAR02"]
        mock_client.get_events.assert_called_once_with(
            series_ticker="KXNBA",
            status="open",
            with_nested_markets=False,
            limit=200,
            cursor=None,
        )

    @pytest.mark.asyncio
    async def test_find_nba_events_pagination(self) -> None:
        """Test finding events with pagination."""
        mock_client = AsyncMock()

        # Mock paginated responses
        def get_events_side_effect(**kwargs: any) -> dict:
            cursor = kwargs.get("cursor")
            if cursor is None:
                return {
                    "events": [
                        {"event_ticker": "KXNBAGAME-26MAR01"},
                    ],
                    "cursor": "next_page",
                }
            else:
                return {
                    "events": [
                        {"event_ticker": "KXNBAGAME-26MAR02"},
                    ],
                    "cursor": None,
                }

        mock_client.get_events = AsyncMock(side_effect=get_events_side_effect)

        result = await _find_nba_events(mock_client, "KXNBA")
        assert result == ["KXNBAGAME-26MAR01", "KXNBAGAME-26MAR02"]
        assert mock_client.get_events.call_count == 2

    @pytest.mark.asyncio
    async def test_find_nba_events_deduplication(self) -> None:
        """Test that duplicate events are deduplicated."""
        mock_client = AsyncMock()
        mock_client.get_events = AsyncMock(
            return_value={
                "events": [
                    {"event_ticker": "KXNBAGAME-26MAR01"},
                    {"event_ticker": "KXNBAGAME-26MAR01"},  # Duplicate
                    {"event_ticker": "KXNBAGAME-26MAR02"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_events(mock_client, "KXNBA")
        assert result == ["KXNBAGAME-26MAR01", "KXNBAGAME-26MAR02"]

    @pytest.mark.asyncio
    async def test_find_nba_events_uppercase(self) -> None:
        """Test that event tickers are uppercased."""
        mock_client = AsyncMock()
        mock_client.get_events = AsyncMock(
            return_value={
                "events": [
                    {"event_ticker": "kxnbagame-26mar01"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_events(mock_client, "KXNBA")
        assert result == ["KXNBAGAME-26MAR01"]


class TestFindNBAMarkets:
    """Test _find_nba_markets function."""

    @pytest.mark.asyncio
    async def test_find_nba_markets_single_page(self) -> None:
        """Test finding markets on single page."""
        mock_client = AsyncMock()
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "KXNBAGAME-26MAR01-T1"},
                    {"ticker": "KXNBAGAME-26MAR01-T2"},
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_markets(mock_client, "KXNBA", [])
        assert result == ["KXNBAGAME-26MAR01-T1", "KXNBAGAME-26MAR01-T2"]
        mock_client.get_markets.assert_called_once_with(
            series_ticker="KXNBA",
            limit=200,
            cursor=None,
        )

    @pytest.mark.asyncio
    async def test_find_nba_markets_pagination(self) -> None:
        """Test finding markets with pagination."""
        mock_client = AsyncMock()

        # Mock paginated responses
        def get_markets_side_effect(**kwargs: any) -> dict:
            cursor = kwargs.get("cursor")
            if cursor is None:
                return {
                    "markets": [
                        {"ticker": "KXNBAGAME-26MAR01-T1"},
                    ],
                    "cursor": "next_page",
                }
            else:
                return {
                    "markets": [
                        {"ticker": "KXNBAGAME-26MAR01-T2"},
                    ],
                    "cursor": None,
                }

        mock_client.get_markets = AsyncMock(side_effect=get_markets_side_effect)

        result = await _find_nba_markets(mock_client, "KXNBA", [])
        assert result == ["KXNBAGAME-26MAR01-T1", "KXNBAGAME-26MAR01-T2"]
        assert mock_client.get_markets.call_count == 2


class TestDiscoverNBAUniverse:
    """Test discover_nba_universe function with caching."""

    @pytest.mark.asyncio
    async def test_discover_nba_universe_success(self) -> None:
        """Test successful NBA universe discovery."""
        # Reset global cache
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "KXNBA", "category": "sports", "title": "NBA Games"},
                ],
                "cursor": None,
            }
        )
        mock_client.get_events = AsyncMock(
            return_value={
                "events": [
                    {"event_ticker": "KXNBAGAME-26MAR01"},
                ],
                "cursor": None,
            }
        )
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "KXNBAGAME-26MAR01-T1"},
                    {"ticker": "KXNBAGAME-26MAR01-T2"},
                ],
                "cursor": None,
            }
        )

        result = await discover_nba_universe(mock_client)

        assert result.series_ticker == "KXNBA"
        assert result.event_tickers == ["KXNBAGAME-26MAR01"]
        assert result.market_tickers == ["KXNBAGAME-26MAR01-T1", "KXNBAGAME-26MAR01-T2"]

    @pytest.mark.asyncio
    async def test_discover_nba_universe_caching(self) -> None:
        """Test that universe is cached properly."""
        # Reset global cache
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "KXNBA", "category": "sports", "title": "NBA Games"},
                ],
                "cursor": None,
            }
        )
        mock_client.get_events = AsyncMock(
            return_value={
                "events": [
                    {"event_ticker": "KXNBAGAME-26MAR01"},
                ],
                "cursor": None,
            }
        )
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "KXNBAGAME-26MAR01-T1"},
                ],
                "cursor": None,
            }
        )

        # First call - should fetch from API
        result1 = await discover_nba_universe(mock_client)
        assert mock_client.get_series.call_count == 1
        assert mock_client.get_events.call_count == 1
        assert mock_client.get_markets.call_count == 1

        # Second call - should use cache
        result2 = await discover_nba_universe(mock_client)
        assert result2.series_ticker == result1.series_ticker
        assert result2.event_tickers == result1.event_tickers
        assert result2.market_tickers == result1.market_tickers

        # API should not be called again
        assert mock_client.get_series.call_count == 1
        assert mock_client.get_events.call_count == 1
        assert mock_client.get_markets.call_count == 1

    @pytest.mark.asyncio
    async def test_discover_nba_universe_cache_expiry(self) -> None:
        """Test that cache expires after TTL."""
        # Reset global cache
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "KXNBA", "category": "sports", "title": "NBA Games"},
                ],
                "cursor": None,
            }
        )
        mock_client.get_events = AsyncMock(
            return_value={
                "events": [
                    {"event_ticker": "KXNBAGAME-26MAR01"},
                ],
                "cursor": None,
            }
        )
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "KXNBAGAME-26MAR01-T1"},
                ],
                "cursor": None,
            }
        )

        # First call
        await discover_nba_universe(mock_client)
        assert mock_client.get_series.call_count == 1

        # Manually expire cache by setting old timestamp
        if universe_module._nba_universe_cache is not None:
            universe_module._nba_universe_cache.timestamp = time.time() - 61  # 61 seconds ago

        # Second call - should refresh
        await discover_nba_universe(mock_client)
        assert mock_client.get_series.call_count == 2

    @pytest.mark.asyncio
    async def test_discover_nba_universe_no_series(self) -> None:
        """Test when no NBA series is found."""
        # Reset global cache
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            return_value={
                "series": [
                    {"ticker": "OTHER", "category": "politics", "title": "Election"},
                ],
                "cursor": None,
            }
        )

        result = await discover_nba_universe(mock_client)

        assert result.series_ticker == ""
        assert result.event_tickers == []
        assert result.market_tickers == []
