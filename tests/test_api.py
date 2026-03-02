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
