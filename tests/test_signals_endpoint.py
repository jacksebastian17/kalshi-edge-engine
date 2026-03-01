"""Tests for /signals/live endpoint."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from kalshi_edge_engine.api.main import app
from kalshi_edge_engine.kalshi.client import KalshiAPIError, KalshiAuthError


class TestSignalsLiveEndpoint(unittest.TestCase):
    """Test /signals/live endpoint."""

    def setUp(self) -> None:
        """Set up test client."""
        self.client = TestClient(app)

    def test_signals_live_invalid_ticker(self) -> None:
        """Test endpoint with invalid (empty) ticker."""
        response = self.client.get("/signals/live?ticker=")
        assert response.status_code == 400
        assert "cannot be empty" in response.json()["detail"].lower()

    def test_signals_live_missing_ticker(self) -> None:
        """Test endpoint with missing ticker parameter."""
        response = self.client.get("/signals/live")
        assert response.status_code == 422  # FastAPI validation error

    @patch("kalshi_edge_engine.api.main.KalshiClient")
    def test_signals_live_success_valid_signal(self, mock_client_class: MagicMock) -> None:
        """Test endpoint with valid orderbook returning SIGNAL_VALID."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
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

    @patch("kalshi_edge_engine.api.main.KalshiClient")
    def test_signals_live_success_ineligible(self, mock_client_class: MagicMock) -> None:
        """Test endpoint with orderbook returning MARKET_INELIGIBLE."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.get_orderbook.return_value = {}  # Empty orderbook

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 200
        data = response.json()
        assert data["reason_code"] == "MARKET_INELIGIBLE"

    @patch("kalshi_edge_engine.api.main.KalshiClient")
    def test_signals_live_uppercase_conversion(self, mock_client_class: MagicMock) -> None:
        """Test that ticker is normalized to uppercase."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.get_orderbook.return_value = {
            "yes": [[96, 250]],
            "no": [[5, 100]],
        }

        response = self.client.get("/signals/live?ticker=test-ticker")

        assert response.status_code == 200
        # Verify the client was called with uppercase ticker
        mock_client.get_orderbook.assert_called_once_with("TEST-TICKER")

    @patch("kalshi_edge_engine.api.main.KalshiClient")
    def test_signals_live_auth_error(self, mock_client_class: MagicMock) -> None:
        """Test endpoint when auth fails."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.get_orderbook.side_effect = KalshiAuthError("Invalid credentials")

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 502
        assert "Auth error" in response.json()["detail"]

    @patch("kalshi_edge_engine.api.main.KalshiClient")
    def test_signals_live_api_error(self, mock_client_class: MagicMock) -> None:
        """Test endpoint when API returns error."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.get_orderbook.side_effect = KalshiAPIError("API error")

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 502
        assert "Upstream API error" in response.json()["detail"]

    @patch("kalshi_edge_engine.api.main.KalshiClient")
    def test_signals_live_response_shape(self, mock_client_class: MagicMock) -> None:
        """Test that response includes all required fields."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
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
            "top_liquidity",
            "timestamp_ms",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    @patch("kalshi_edge_engine.api.main.KalshiClient")
    def test_signals_live_both_sides_invalid(self, mock_client_class: MagicMock) -> None:
        """Test endpoint with both sides missing (market ineligible)."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        mock_client.get_orderbook.return_value = {"yes": [], "no": []}

        response = self.client.get("/signals/live?ticker=TEST-TICKER")

        assert response.status_code == 200
        data = response.json()
        assert data["reason_code"] == "MARKET_INELIGIBLE"
        assert data["bid_yes"] is None
        assert data["bid_no"] is None
