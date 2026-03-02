"""Tests for universe discovery module."""

import time
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.engine.universe import (
    _find_nba_markets,
    discover_nba_universe,
)


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

        result = await _find_nba_markets(mock_client, "KXNBAGAME", status="open")
        assert result == ["KXNBAGAME-26MAR01-T1", "KXNBAGAME-26MAR01-T2"]
        mock_client.get_markets.assert_called_once_with(
            series_ticker="KXNBAGAME",
            status="open",
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

        result = await _find_nba_markets(mock_client, "KXNBAGAME", status="open")
        assert result == ["KXNBAGAME-26MAR01-T1", "KXNBAGAME-26MAR01-T2"]
        assert mock_client.get_markets.call_count == 2

    @pytest.mark.asyncio
    async def test_find_nba_markets_full_pagination(self) -> None:
        """Test finding markets with full pagination (no page limit)."""
        mock_client = AsyncMock()

        # Mock 5 pages of results
        def get_markets_side_effect(**kwargs: any) -> dict:
            cursor = kwargs.get("cursor")
            if cursor is None:
                return {"markets": [{"ticker": "MARKET1"}], "cursor": "page2"}
            elif cursor == "page2":
                return {"markets": [{"ticker": "MARKET2"}], "cursor": "page3"}
            elif cursor == "page3":
                return {"markets": [{"ticker": "MARKET3"}], "cursor": "page4"}
            elif cursor == "page4":
                return {"markets": [{"ticker": "MARKET4"}], "cursor": "page5"}
            elif cursor == "page5":
                return {"markets": [{"ticker": "MARKET5"}], "cursor": None}
            else:
                return {"markets": [], "cursor": None}

        mock_client.get_markets = AsyncMock(side_effect=get_markets_side_effect)

        result = await _find_nba_markets(mock_client, "KXNBAGAME", status="open")
        assert result == ["MARKET1", "MARKET2", "MARKET3", "MARKET4", "MARKET5"]
        assert mock_client.get_markets.call_count == 5

    @pytest.mark.asyncio
    async def test_find_nba_markets_preserves_ticker_casing(self) -> None:
        """Test that market tickers are used exactly as returned."""
        mock_client = AsyncMock()
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "KXNBAGAME-26MAR01-T1"},
                    {"ticker": "KXNBAGame-26MAR01-T2"},  # Mixed case
                ],
                "cursor": None,
            }
        )

        result = await _find_nba_markets(mock_client, "KXNBAGAME", status="open")
        # Tickers should be preserved exactly as returned
        assert "KXNBAGAME-26MAR01-T1" in result
        assert "KXNBAGame-26MAR01-T2" in result


class TestDiscoverNBAUniverse:
    """Test discover_nba_universe function with caching."""

    @pytest.mark.asyncio
    async def test_discover_nba_universe_success(self) -> None:
        """Test successful NBA universe discovery with hardcoded KXNBAGAME series."""
        # Reset global cache
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

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

        result = await discover_nba_universe(mock_client)

        assert result.series_ticker == "KXNBAGAME"
        assert result.event_tickers == []  # Not used in new approach
        assert result.market_tickers == ["KXNBAGAME-26MAR01-T1", "KXNBAGAME-26MAR01-T2"]
        mock_client.get_markets.assert_called_once()

    @pytest.mark.asyncio
    async def test_discover_nba_universe_caching(self) -> None:
        """Test that universe is cached properly."""
        # Reset global cache
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
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
        assert mock_client.get_markets.call_count == 1

        # Second call - should use cache
        result2 = await discover_nba_universe(mock_client)
        assert result2.series_ticker == result1.series_ticker
        assert result2.event_tickers == result1.event_tickers
        assert result2.market_tickers == result1.market_tickers

        # API should not be called again
        assert mock_client.get_markets.call_count == 1

    @pytest.mark.asyncio
    async def test_discover_nba_universe_cache_expiry(self) -> None:
        """Test that cache expires after TTL."""
        # Reset global cache
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
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
        assert mock_client.get_markets.call_count == 1

        # Manually expire cache by setting old timestamp
        if universe_module._nba_universe_cache is not None:
            universe_module._nba_universe_cache.timestamp = time.time() - 61  # 61 seconds ago

        # Second call - should refresh
        await discover_nba_universe(mock_client)
        assert mock_client.get_markets.call_count == 2

    @pytest.mark.asyncio
    async def test_discover_nba_universe_full_pagination(self) -> None:
        """Test that discover_nba_universe performs full pagination."""
        # Reset global cache
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()

        # Mock 3 pages of results
        def get_markets_side_effect(**kwargs: any) -> dict:
            cursor = kwargs.get("cursor")
            if cursor is None:
                return {"markets": [{"ticker": "MARKET1"}], "cursor": "page2"}
            elif cursor == "page2":
                return {"markets": [{"ticker": "MARKET2"}], "cursor": "page3"}
            elif cursor == "page3":
                return {"markets": [{"ticker": "MARKET3"}], "cursor": None}
            else:
                return {"markets": [], "cursor": None}

        mock_client.get_markets = AsyncMock(side_effect=get_markets_side_effect)

        result = await discover_nba_universe(mock_client)

        assert result.series_ticker == "KXNBAGAME"
        assert result.market_tickers == ["MARKET1", "MARKET2", "MARKET3"]
        assert mock_client.get_markets.call_count == 3
