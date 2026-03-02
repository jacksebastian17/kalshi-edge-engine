"""Tests for API endpoints."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from kalshi_edge_engine.api.main import create_app
from kalshi_edge_engine.kalshi.client import KalshiAPIError, KalshiAuthError


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the FastAPI app."""
    return TestClient(create_app(testing=True))


def test_health_endpoint(client: TestClient) -> None:
    """Test that /health endpoint returns ok status."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_endpoint(client: TestClient) -> None:
    """Test that /version endpoint returns correct version info."""
    response = client.get("/version")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "kalshi-edge-engine"
    assert data["version"] == "0.1.0"


class TestDebugOrderbookEndpoint:
    """Test /debug/orderbook/{ticker} endpoint."""

    def test_debug_orderbook_empty_ticker(self, client: TestClient) -> None:
        """Test that empty ticker returns 400."""
        # Test with explicit empty (space) ticker
        response = client.get("/debug/orderbook/%20")  # space
        assert response.status_code == 400
        assert "cannot be empty" in response.json()["detail"].lower()

    def test_debug_orderbook_success(self, client: TestClient) -> None:
        """Test successful orderbook fetch."""
        # Use realistic Kalshi orderbook shape
        mock_orderbook = {
            "yes": [[80, 250], [79, 100]],
            "no": [[15, 250], [14, 100]],
        }

        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(return_value=mock_orderbook)
        client.app.state.kalshi_client = mock_client

        response = client.get("/debug/orderbook/TRUMP-ELECTION")

        assert response.status_code == 200
        data = response.json()
        assert data["ticker"] == "TRUMP-ELECTION"
        assert data["orderbook"] == mock_orderbook
        assert data["success"] is True
        assert "latency_ms" in data
        assert isinstance(data["latency_ms"], float)

    def test_debug_orderbook_uppercase_conversion(self, client: TestClient) -> None:
        """Test that ticker is converted to uppercase."""
        # Use realistic Kalshi orderbook shape
        mock_orderbook = {"yes": [[80, 250]], "no": [[15, 250]]}

        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(return_value=mock_orderbook)
        client.app.state.kalshi_client = mock_client

        response = client.get("/debug/orderbook/trump-election")

        assert response.status_code == 200
        data = response.json()
        assert data["ticker"] == "TRUMP-ELECTION"
        # Verify the client was called with uppercase ticker
        mock_client.get_orderbook.assert_called_once_with("TRUMP-ELECTION")

    def test_debug_orderbook_auth_error(self, client: TestClient) -> None:
        """Test handling of authentication errors."""
        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(side_effect=KalshiAuthError("Missing credentials"))
        client.app.state.kalshi_client = mock_client

        response = client.get("/debug/orderbook/TRUMP-ELECTION")

        assert response.status_code == 502
        assert "Auth error" in response.json()["detail"]

    def test_debug_orderbook_api_error(self, client: TestClient) -> None:
        """Test handling of API errors."""
        mock_client = AsyncMock()
        mock_client.get_orderbook = AsyncMock(
            side_effect=KalshiAPIError("API error: 404 Not Found")
        )
        client.app.state.kalshi_client = mock_client

        response = client.get("/debug/orderbook/INVALID-TICKER")

        assert response.status_code == 502
        assert "Upstream API error" in response.json()["detail"]


class TestNBAUniverseEndpoint:
    """Test /universe/nba endpoint."""

    def test_nba_universe_success(self, client: TestClient) -> None:
        """Test successful NBA universe retrieval."""
        # Reset cache before test
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
                    {"event_ticker": "kxnbagame-26mar01"},
                    {"event_ticker": "KXNBAGAME-26MAR02"},
                ],
                "cursor": None,
            }
        )
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "kxnbagame-26mar01-t1"},
                    {"ticker": "KXNBAGAME-26MAR01-T2"},
                    {"ticker": "KXNBAGAME-26MAR02-T1"},
                ],
                "cursor": None,
            }
        )
        client.app.state.kalshi_client = mock_client

        response = client.get("/universe/nba")

        assert response.status_code == 200
        data = response.json()
        assert data["series_ticker"] == "KXNBA"
        assert data["event_tickers"] == ["KXNBAGAME-26MAR01", "KXNBAGAME-26MAR02"]
        assert data["market_tickers"] == [
            "KXNBAGAME-26MAR01-T1",
            "KXNBAGAME-26MAR01-T2",
            "KXNBAGAME-26MAR02-T1",
        ]

    def test_nba_universe_uppercase_dedup(self, client: TestClient) -> None:
        """Test that tickers are uppercased and deduplicated."""
        # Reset cache before test
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
                    {"event_ticker": "kxnbagame-26mar01"},
                    {"event_ticker": "KXNBAGAME-26MAR01"},  # Duplicate different case
                ],
                "cursor": None,
            }
        )
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "kxnbagame-26mar01-t1"},
                    {"ticker": "KXNBAGAME-26MAR01-T1"},  # Duplicate different case
                ],
                "cursor": None,
            }
        )
        client.app.state.kalshi_client = mock_client

        response = client.get("/universe/nba")

        assert response.status_code == 200
        data = response.json()
        # Should be deduplicated
        assert data["event_tickers"] == ["KXNBAGAME-26MAR01"]
        assert data["market_tickers"] == ["KXNBAGAME-26MAR01-T1"]

    def test_nba_universe_stable_ordering(self, client: TestClient) -> None:
        """Test that tickers are returned in stable sorted order."""
        # Reset cache before test
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
                    {"event_ticker": "KXNBAGAME-26MAR03"},
                    {"event_ticker": "KXNBAGAME-26MAR01"},
                    {"event_ticker": "KXNBAGAME-26MAR02"},
                ],
                "cursor": None,
            }
        )
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "KXNBAGAME-26MAR01-T3"},
                    {"ticker": "KXNBAGAME-26MAR01-T1"},
                    {"ticker": "KXNBAGAME-26MAR01-T2"},
                ],
                "cursor": None,
            }
        )
        client.app.state.kalshi_client = mock_client

        response = client.get("/universe/nba")

        assert response.status_code == 200
        data = response.json()
        # Should be sorted
        assert data["event_tickers"] == [
            "KXNBAGAME-26MAR01",
            "KXNBAGAME-26MAR02",
            "KXNBAGAME-26MAR03",
        ]
        assert data["market_tickers"] == [
            "KXNBAGAME-26MAR01-T1",
            "KXNBAGAME-26MAR01-T2",
            "KXNBAGAME-26MAR01-T3",
        ]

    def test_nba_universe_auth_error(self, client: TestClient) -> None:
        """Test handling of authentication errors."""
        # Reset cache before test
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(side_effect=KalshiAuthError("Missing credentials"))
        client.app.state.kalshi_client = mock_client

        response = client.get("/universe/nba")

        assert response.status_code == 502
        assert "Auth error" in response.json()["detail"]

    def test_nba_universe_api_error(self, client: TestClient) -> None:
        """Test handling of API errors."""
        # Reset cache before test
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
        mock_client.get_series = AsyncMock(
            side_effect=KalshiAPIError("API error: 500 Server Error")
        )
        client.app.state.kalshi_client = mock_client

        response = client.get("/universe/nba")

        assert response.status_code == 502
        assert "Upstream API error" in response.json()["detail"]


class TestTopSignalsEndpoint:
    """Test /signals/top endpoint."""

    def test_signals_top_not_available_yet(self, client: TestClient) -> None:
        """Test that endpoint returns 503 when snapshot is not available."""
        # Reset global snapshot
        import kalshi_edge_engine.api.main as main_module

        main_module._top_signals_snapshot = None

        response = client.get("/signals/top")

        assert response.status_code == 503
        assert "not available yet" in response.json()["detail"].lower()

    def test_signals_top_returns_snapshot(self, client: TestClient) -> None:
        """Test that endpoint returns snapshot when available."""
        import kalshi_edge_engine.api.main as main_module
        from kalshi_edge_engine.engine.signal import Signal

        # Create mock signals
        mock_signals = [
            Signal(
                ticker=f"TICKER-{i}",
                reason_code="SIGNAL_VALID",
                net_edge_yes=float(100 - i),
                net_edge_no=float(50 - i),
                inside_width_yes=5.0,
                inside_width_no=3.0,
                bid_yes=45,
                cross_yes=50,
                bid_no=45,
                cross_no=50,
                bid_sum=90.0,
                imbalance_gap=0.0,
                liq_yes=1000,
                liq_no=1000,
                top_liquidity=1000,
                timestamp_ms=1234567890 + i,
            )
            for i in range(25)  # Create 25 signals
        ]

        # Create snapshot
        from kalshi_edge_engine.api.main import SignalResponse, TopSignalsSnapshot

        snapshot = TopSignalsSnapshot(
            timestamp_ms=9999999999,
            signals=[
                SignalResponse(
                    ticker=s.ticker,
                    reason_code=s.reason_code,
                    net_edge_yes=s.net_edge_yes,
                    net_edge_no=s.net_edge_no,
                    inside_width_yes=s.inside_width_yes,
                    inside_width_no=s.inside_width_no,
                    bid_yes=s.bid_yes,
                    cross_yes=s.cross_yes,
                    bid_no=s.bid_no,
                    cross_no=s.cross_no,
                    bid_sum=s.bid_sum,
                    imbalance_gap=s.imbalance_gap,
                    liq_yes=s.liq_yes,
                    liq_no=s.liq_no,
                    top_liquidity=s.top_liquidity,
                    timestamp_ms=s.timestamp_ms,
                )
                for s in mock_signals
            ],
            market_count=100,
        )
        main_module._top_signals_snapshot = snapshot

        response = client.get("/signals/top")

        assert response.status_code == 200
        data = response.json()
        assert data["timestamp_ms"] == 9999999999
        assert data["market_count"] == 100
        # Should return top 20 by default
        assert len(data["signals"]) == 20
        # First signal should be TICKER-0 (highest edge)
        assert data["signals"][0]["ticker"] == "TICKER-0"

    def test_signals_top_respects_limit(self, client: TestClient) -> None:
        """Test that endpoint respects n parameter."""
        import kalshi_edge_engine.api.main as main_module
        from kalshi_edge_engine.engine.signal import Signal

        # Create mock signals
        mock_signals = [
            Signal(
                ticker=f"TICKER-{i}",
                reason_code="SIGNAL_VALID",
                net_edge_yes=float(100 - i),
                net_edge_no=None,
                inside_width_yes=5.0,
                inside_width_no=None,
                bid_yes=45,
                cross_yes=50,
                bid_no=None,
                cross_no=None,
                bid_sum=None,
                imbalance_gap=None,
                liq_yes=1000,
                liq_no=0,
                top_liquidity=1000,
                timestamp_ms=1234567890,
            )
            for i in range(20)
        ]

        from kalshi_edge_engine.api.main import SignalResponse, TopSignalsSnapshot

        snapshot = TopSignalsSnapshot(
            timestamp_ms=9999999999,
            signals=[
                SignalResponse(
                    ticker=s.ticker,
                    reason_code=s.reason_code,
                    net_edge_yes=s.net_edge_yes,
                    net_edge_no=s.net_edge_no,
                    inside_width_yes=s.inside_width_yes,
                    inside_width_no=s.inside_width_no,
                    bid_yes=s.bid_yes,
                    cross_yes=s.cross_yes,
                    bid_no=s.bid_no,
                    cross_no=s.cross_no,
                    bid_sum=s.bid_sum,
                    imbalance_gap=s.imbalance_gap,
                    liq_yes=s.liq_yes,
                    liq_no=s.liq_no,
                    top_liquidity=s.top_liquidity,
                    timestamp_ms=s.timestamp_ms,
                )
                for s in mock_signals
            ],
            market_count=50,
        )
        main_module._top_signals_snapshot = snapshot

        response = client.get("/signals/top?n=5")

        assert response.status_code == 200
        data = response.json()
        # Should return only 5 signals
        assert len(data["signals"]) == 5
