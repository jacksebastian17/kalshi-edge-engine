"""Tests for API endpoints."""

import pytest
from fastapi.testclient import TestClient

from kalshi_edge_engine.api.main import app


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the FastAPI app."""
    return TestClient(app)


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
