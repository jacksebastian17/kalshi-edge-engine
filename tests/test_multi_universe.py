"""Tests for configurable multi-universe scanner infrastructure."""

from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.runtime import config as runtime_config
from kalshi_edge_engine.runtime import scanner_runtime
from kalshi_edge_engine.universes.base import MarketUniverse
from kalshi_edge_engine.universes.cbb import (
    CBB_SERIES_TICKER,
    _find_markets_for_series,
    discover_cbb_universe,
    is_cbb_full_game_moneyline_market,
)


def test_get_enabled_universes_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled universe helper should return deterministic normalized order."""
    monkeypatch.setattr(runtime_config, "ENABLE_UNIVERSE_NBA", True)
    monkeypatch.setattr(runtime_config, "ENABLE_UNIVERSE_CBB", True)
    assert runtime_config.get_enabled_universes() == ["NBA", "CBB"]

    monkeypatch.setattr(runtime_config, "ENABLE_UNIVERSE_NBA", False)
    monkeypatch.setattr(runtime_config, "ENABLE_UNIVERSE_CBB", True)
    assert runtime_config.get_enabled_universes() == ["CBB"]

    monkeypatch.setattr(runtime_config, "ENABLE_UNIVERSE_NBA", False)
    monkeypatch.setattr(runtime_config, "ENABLE_UNIVERSE_CBB", False)
    assert runtime_config.get_enabled_universes() == []


@pytest.mark.asyncio
async def test_discover_enabled_universe_markets_merges_and_tracks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery should merge universes, dedupe tickers, and track first-owner mapping."""

    monkeypatch.setattr(runtime_config, "get_enabled_universes", lambda: ["NBA", "CBB"])

    async def _fake_nba(_client: object) -> MarketUniverse:
        return MarketUniverse(
            series_ticker="KXNBA",
            event_tickers=["KXNBA-E1"],
            market_tickers=["TICKER-A", "TICKER-B"],
        )

    async def _fake_cbb(_client: object) -> MarketUniverse:
        return MarketUniverse(
            series_ticker="KXCBB",
            event_tickers=["KXCBB-E1"],
            market_tickers=["TICKER-B", "TICKER-C"],
        )

    monkeypatch.setattr(scanner_runtime, "discover_nba_universe", _fake_nba)
    monkeypatch.setattr(scanner_runtime, "discover_cbb_universe", _fake_cbb)

    (
        merged,
        ticker_universe_map,
        discovered_counts,
        enabled,
    ) = await scanner_runtime._discover_enabled_universe_markets(AsyncMock())

    assert enabled == ["NBA", "CBB"]
    assert merged == ["TICKER-A", "TICKER-B", "TICKER-C"]
    assert discovered_counts == {"NBA": 2, "CBB": 2}
    # Duplicate ticker stays mapped to first discovered universe owner.
    assert ticker_universe_map == {
        "TICKER-A": "NBA",
        "TICKER-B": "NBA",
        "TICKER-C": "CBB",
    }


def test_cbb_series_ticker_is_single_game_series() -> None:
    """CBB discovery should target a single game-level series ticker."""
    assert CBB_SERIES_TICKER == "KXNCAAMBGAME"


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("KXNCAAMBGAME-26MAR07WASHORE", True),
        ("KXNCAAMBGAME-26MAR07WASHORE-WASH", True),
        ("KXNCAAMB1HWINNER-26MAR07WASHORE", False),
        ("KXNCAAMBGAME-26MAR07WASHORE-TIE", False),
        ("KXNCAAMBBIG12-26-HOU", False),
        ("KXNCAAMBASUN-26-CARK", False),
    ],
)
def test_is_cbb_full_game_moneyline_market_filtering(ticker: str, expected: bool) -> None:
    """CBB filter should include only full-game game-level moneyline-like tickers."""
    assert is_cbb_full_game_moneyline_market(ticker) is expected


@pytest.mark.asyncio
async def test_find_markets_for_series_filters_non_moneyline_rows() -> None:
    """Series market fetch should keep only full-game moneyline-like game rows."""
    mock_client = AsyncMock()
    mock_client.get_markets = AsyncMock(
        return_value={
            "markets": [
                {"ticker": "KXNCAAMBGAME-26MAR07WASHORE"},
                {"ticker": "KXNCAAMB1HWINNER-26MAR07WASHORE"},
                {"ticker": "KXNCAAMBGAME-26MAR07WASHORE-TIE"},
                {"ticker": "KXNCAAMBBIG12-26-HOU"},
                {"ticker": "KXNCAAMBASUN-26-CARK"},
            ],
            "cursor": None,
        }
    )

    filtered = await _find_markets_for_series(mock_client, "KXNCAAMBGAME", status="open")
    assert filtered == ["KXNCAAMBGAME-26MAR07WASHORE"]


@pytest.mark.asyncio
async def test_discover_cbb_universe_queries_single_series_only() -> None:
    """CBB universe discovery should query only CBB_SERIES_TICKER via get_markets."""
    import kalshi_edge_engine.universes.cbb as cbb_module

    cbb_module._cbb_universe_cache = None
    mock_client = AsyncMock()
    mock_client.get_series = AsyncMock()
    mock_client.get_markets = AsyncMock(
        return_value={
            "markets": [
                {"ticker": "KXNCAAMBGAME-26MAR07WASHORE"},
                {"ticker": "KXNCAAMBGAME-26MAR07WASHORE-TIE"},
                {"ticker": "KXNCAAMB1HWINNER-26MAR07WASHORE"},
                {"ticker": "KXNCAAMBBIG12-26-HOU"},
            ],
            "cursor": None,
        }
    )

    universe = await discover_cbb_universe(mock_client)

    assert universe.series_ticker == CBB_SERIES_TICKER
    assert universe.event_tickers == []
    assert universe.market_tickers == ["KXNCAAMBGAME-26MAR07WASHORE"]
    mock_client.get_markets.assert_called_once_with(
        series_ticker=CBB_SERIES_TICKER,
        status="open",
        limit=200,
        cursor=None,
    )
    mock_client.get_series.assert_not_called()
