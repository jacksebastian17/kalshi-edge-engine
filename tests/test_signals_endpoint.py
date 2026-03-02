"""Tests for /signals/live endpoint."""

import unittest
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from kalshi_edge_engine.api.main import create_app
from kalshi_edge_engine.kalshi.client import KalshiAPIError, KalshiAuthError


class TestSignalsLiveEndpoint(unittest.TestCase):
    """Test /signals/live endpoint."""

    def setUp(self) -> None:
        """Set up test client."""
        self.client = TestClient(create_app(testing=True))

    def test_signals_live_invalid_ticker(self) -> None:
        """Test endpoint with invalid (empty) ticker."""
        response = self.client.get("/signals/live?ticker=")
        assert response.status_code == 400
        assert "cannot be empty" in response.json()["detail"].lower()

    def test_signals_live_missing_ticker(self) -> None:
        """Test endpoint with missing ticker parameter."""
        response = self.client.get("/signals/live")
        assert response.status_code == 422  # FastAPI validation error

    def test_signals_live_success_valid_signal(self) -> None:
        """Test endpoint with valid orderbook returning SIGNAL_VALID."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client
        mock_client.get_orderbook.return_value = {
            "yes": [[80, 250], [79, 100]],
            "no": [[15, 250], [14, 100]],
        }

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 200
        data = response.json()
        assert data["ticker"] == "TEST-TICKER"
        assert data["reason_code"] == "SIGNAL_VALID"
        assert data["inside_width_yes"] == 5.0
        assert data["inside_width_no"] == 5.0
        assert data["bid_yes"] == 80.0
        assert data["cross_yes"] == 85.0

    def test_signals_live_success_ineligible(self) -> None:
        """Test endpoint with orderbook returning MARKET_INELIGIBLE."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client
        mock_client.get_orderbook.return_value = {}  # Empty orderbook

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 200
        data = response.json()
        assert data["reason_code"] == "MARKET_INELIGIBLE"

    def test_signals_live_uppercase_conversion(self) -> None:
        """Test that ticker is normalized to uppercase."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client
        mock_client.get_orderbook.return_value = {
            "yes": [[96, 250]],
            "no": [[5, 100]],
        }

        response = self.client.get("/signals/live?ticker=test-ticker")

        assert response.status_code == 200
        # Verify the client was called with uppercase ticker
        mock_client.get_orderbook.assert_called_once_with("TEST-TICKER")

    def test_signals_live_auth_error(self) -> None:
        """Test endpoint when auth fails."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client
        mock_client.get_orderbook.side_effect = KalshiAuthError("Invalid credentials")

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 502
        assert "Auth error" in response.json()["detail"]

    def test_signals_live_api_error(self) -> None:
        """Test endpoint when API returns error."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client
        mock_client.get_orderbook.side_effect = KalshiAPIError("API error")

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 502
        assert "Upstream API error" in response.json()["detail"]

    def test_signals_live_response_shape(self) -> None:
        """Test that response includes all required fields including new liq_yes/liq_no."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client
        mock_client.get_orderbook.return_value = {
            "yes": [[80, 250]],
            "no": [[15, 250]],
        }

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 200
        data = response.json()

        # Verify all required fields are present
        required_fields = [
            "ticker",
            "reason_code",
            "net_edge_yes",
            "net_edge_no",
            "inside_width_yes",
            "inside_width_no",
            "bid_yes",
            "cross_yes",
            "bid_no",
            "cross_no",
            "bid_sum",
            "imbalance_gap",
            "liq_yes",
            "liq_no",
            "top_liquidity",
            "timestamp_ms",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    def test_signals_live_both_sides_invalid(self) -> None:
        """Test endpoint with both sides missing (market ineligible)."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client
        mock_client.get_orderbook.return_value = {"yes": [], "no": []}

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 200
        data = response.json()
        assert data["reason_code"] == "MARKET_INELIGIBLE"
        assert data["bid_yes"] is None
        assert data["bid_no"] is None

    def test_signals_live_tracks_liq_yes_liq_no(self) -> None:
        """Test that response includes liq_yes and liq_no fields."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client
        mock_client.get_orderbook.return_value = {
            "yes": [[80, 250]],
            "no": [[15, 500]],
        }

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 200
        data = response.json()
        assert data["liq_yes"] == 250
        assert data["liq_no"] == 500
        assert data["top_liquidity"] == 500


class TestSignalsScanEndpoint(unittest.TestCase):
    """Test /signals/scan endpoint."""

    def setUp(self) -> None:
        """Set up test client."""
        self.client = TestClient(create_app(testing=True))

    def test_signals_scan_sorts_and_dedupes(self) -> None:
        """Test scan endpoint sorts by strongest edge and deduplicates tickers."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client

        books: dict[str, dict[str, list[list[int]]]] = {
            "AAA": {"yes": [[70, 100]], "no": [[27, 100]]},  # strongest net edge
            "BBB": {"yes": [[80, 100]], "no": [[18, 100]]},  # medium net edge
            "CCC": {"yes": [[50, 100]], "no": [[49, 100]]},  # spread too small
        }

        async def get_orderbook_for_ticker(ticker: str) -> dict[str, list[list[int]]]:
            return books[ticker]

        mock_client.get_orderbook.side_effect = get_orderbook_for_ticker

        response = self.client.get("/signals/scan?tickers=ccc,,bbb,AAA,bbb")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        assert [item["ticker"] for item in data] == ["AAA", "BBB", "CCC"]
        assert data[0]["reason_code"] == "SIGNAL_VALID"
        assert data[1]["reason_code"] == "SIGNAL_VALID"
        assert data[2]["reason_code"] == "SPREAD_TOO_SMALL"
        assert mock_client.get_orderbook.call_count == 3

    def test_signals_scan_exceeds_max_tickers(self) -> None:
        """Test scan endpoint rejects requests above 20 deduped tickers."""
        tickers = ",".join([f"TICKER{i}" for i in range(21)])

        response = self.client.get(f"/signals/scan?tickers={tickers}")

        assert response.status_code == 400
        assert "Maximum 20 tickers per request" in response.json()["detail"]

    def test_signals_scan_parallel_execution_deterministic(self) -> None:
        """Test that parallel scan execution produces deterministic sorted results under ties."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client

        # All tickers have identical net edges (9.4 cents) - sort should fall back to alphabetical
        books: dict[str, dict[str, list[list[int]]]] = {
            "T1": {"yes": [[60, 100]], "no": [[30, 100]]},  # net_edge = 10 - 0.5 - 0.1 = 9.4
            "T2": {"yes": [[50, 100]], "no": [[40, 100]]},  # net_edge = 10 - 0.5 - 0.1 = 9.4
            "T3": {"yes": [[70, 100]], "no": [[20, 100]]},  # net_edge = 10 - 0.5 - 0.1 = 9.4
        }

        async def get_orderbook_for_ticker(ticker: str) -> dict[str, list[list[int]]]:
            return books[ticker]

        mock_client.get_orderbook.side_effect = get_orderbook_for_ticker

        # Make two requests with same tickers in different orders
        response1 = self.client.get("/signals/scan?tickers=T1,T2,T3")
        response2 = self.client.get("/signals/scan?tickers=T3,T1,T2")

        assert response1.status_code == 200
        assert response2.status_code == 200

        data1 = response1.json()
        data2 = response2.json()

        # Results should be sorted the same way (deterministically by ticker under edge ties)
        assert len(data1) == 3
        assert len(data2) == 3
        assert [item["ticker"] for item in data1] == ["T1", "T2", "T3"]
        assert [item["ticker"] for item in data2] == ["T1", "T2", "T3"]

    def test_signals_scan_uses_persistent_client(self) -> None:
        """Test that scan endpoint uses persistent client from app.state."""
        # Create a mock that tracks if it was reused
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client

        books = {
            "T1": {"yes": [[80, 100]], "no": [[15, 100]]},
            "T2": {"yes": [[80, 100]], "no": [[15, 100]]},
        }

        async def get_orderbook_for_ticker(ticker: str) -> dict[str, list[list[int]]]:
            return books[ticker]

        mock_client.get_orderbook.side_effect = get_orderbook_for_ticker

        response = self.client.get("/signals/scan?tickers=T1,T2")

        assert response.status_code == 200
        # Should have called get_orderbook twice with same client instance
        assert mock_client.get_orderbook.call_count == 2

    def test_signals_scan_skips_failed_tickers(self) -> None:
        """Test that scan endpoint gracefully skips tickers that fail."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client

        async def get_orderbook_side_effect(ticker: str) -> dict[str, list[list[int]]]:
            if ticker == "FAIL":
                raise KalshiAPIError("API error")
            return {"yes": [[80, 100]], "no": [[15, 100]]}

        mock_client.get_orderbook.side_effect = get_orderbook_side_effect

        response = self.client.get("/signals/scan?tickers=GOOD,FAIL,GOOD2")

        assert response.status_code == 200
        data = response.json()
        # Only GOOD and GOOD2, not FAIL
        assert len(data) == 2
        tickers = [item["ticker"] for item in data]
        assert "GOOD" in tickers
        assert "GOOD2" in tickers
        assert "FAIL" not in tickers

    def test_signals_scan_response_includes_liq_fields(self) -> None:
        """Test that scan response includes liq_yes and liq_no fields."""
        mock_client = AsyncMock()
        self.client.app.state.kalshi_client = mock_client

        mock_client.get_orderbook.return_value = {
            "yes": [[80, 250]],
            "no": [[15, 500]],
        }

        response = self.client.get("/signals/scan?tickers=TEST")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        item = data[0]
        assert "liq_yes" in item
        assert "liq_no" in item
        assert item["liq_yes"] == 250
        assert item["liq_no"] == 500
