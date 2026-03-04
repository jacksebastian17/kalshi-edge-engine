"""Tests for API endpoints."""

import contextlib
import gc
import os
import sqlite3
import tempfile
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from kalshi_edge_engine.api.main import create_app
from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import QuoteIntent
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
        """Test successful NBA universe retrieval with KXNBAGAME series."""
        # Reset cache before test
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "KXNBAGAME-26MAR01-T1"},
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
        assert data["series_ticker"] == "KXNBAGAME"
        assert data["event_tickers"] == []  # Not used in new approach
        assert data["market_tickers"] == [
            "KXNBAGAME-26MAR01-T1",
            "KXNBAGAME-26MAR01-T2",
            "KXNBAGAME-26MAR02-T1",
        ]

    def test_nba_universe_uppercase_dedup(self, client: TestClient) -> None:
        """Test that tickers are deduplicated."""
        # Reset cache before test
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
        mock_client.get_markets = AsyncMock(
            return_value={
                "markets": [
                    {"ticker": "KXNBAGAME-26MAR01-T1"},
                    {"ticker": "KXNBAGAME-26MAR01-T1"},  # Duplicate
                ],
                "cursor": None,
            }
        )
        client.app.state.kalshi_client = mock_client

        response = client.get("/universe/nba")

        assert response.status_code == 200
        data = response.json()
        # Should be deduplicated
        assert data["event_tickers"] == []  # Not used in new approach
        assert data["market_tickers"] == ["KXNBAGAME-26MAR01-T1"]

    def test_nba_universe_stable_ordering(self, client: TestClient) -> None:
        """Test that tickers are returned in stable sorted order."""
        # Reset cache before test
        import kalshi_edge_engine.engine.universe as universe_module

        universe_module._nba_universe_cache = None

        mock_client = AsyncMock()
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
        assert data["event_tickers"] == []  # Not used in new approach
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
        mock_client.get_markets = AsyncMock(side_effect=KalshiAuthError("Missing credentials"))
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
        mock_client.get_markets = AsyncMock(
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
                fee_yes=1.0,
                fee_no=1.0,
                fee_exit_yes=None,
                fee_exit_no=None,
                total_fee_yes=1.0,
                total_fee_no=1.0,
                fee_mode="maker",
                round_trip=False,
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
                    fee_yes=s.fee_yes,
                    fee_no=s.fee_no,
                    fee_exit_yes=s.fee_exit_yes,
                    fee_exit_no=s.fee_exit_no,
                    total_fee_yes=s.total_fee_yes,
                    total_fee_no=s.total_fee_no,
                    fee_mode=s.fee_mode,
                    round_trip=s.round_trip,
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


class TestPaperDebugEndpoints:
    """Test paper debug endpoints for simulator diagnostics and force-fill."""

    def test_paper_debug_intent_returns_diagnostics(self, client: TestClient) -> None:
        """GET /paper/debug/intent/{id} returns computed simulator diagnostics."""
        import kalshi_edge_engine.api.main as main_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = PaperStore(db_path=db_path)
            intent = QuoteIntent(
                id="intent-debug-1",
                ticker="KXNBAGAME-26MAR01SASNYK-SAS",
                side="YES",
                quote_price_cents=50.0,
                size_contracts=1,
                created_ts_ms=int(time.time() * 1000),
                net_edge=1.0,
                inside_width=2.0,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            )
            store.insert_intent(intent)
            store.update_intent_status(intent.id, "OPEN", last_seen_best_bid=49.0, queue_ahead=2)
            main_module._paper_store = store

            mock_client = AsyncMock()
            mock_client.get_orderbook = AsyncMock(
                return_value={"yes": [[51.0, 100]], "no": [[45.0, 80]]}
            )
            client.app.state.kalshi_client = mock_client

            response = client.get(f"/paper/debug/intent/{intent.id}")
            assert response.status_code == 200
            data = response.json()
            assert data["intent"]["id"] == intent.id
            assert data["top_of_book"]["yes"]["best_bid_price"] == 51.0
            assert data["simulator"]["eligibility"]["has_best_bid"] is True
        finally:
            main_module._paper_store = None
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)

    def test_paper_debug_force_fill_writes_fill_and_position(self, client: TestClient) -> None:
        """POST /paper/debug/force-fill inserts synthetic fill and upserts position."""
        import kalshi_edge_engine.api.main as main_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = PaperStore(db_path=db_path)
            intent = QuoteIntent(
                id="intent-force-1",
                ticker="KXNBAGAME-26MAR01SASNYK-SAS",
                side="YES",
                quote_price_cents=53.0,
                size_contracts=1,
                created_ts_ms=int(time.time() * 1000),
                net_edge=1.2,
                inside_width=2.2,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            )
            store.insert_intent(
                intent,
                best_bid_at_submit_cents=52.0,
                cross_at_submit_cents=54.0,
            )
            store.update_intent_status(intent.id, "OPEN", last_seen_best_bid=52.0, queue_ahead=1)
            main_module._paper_store = store

            response = client.post(
                "/paper/debug/force-fill",
                json={"intent_id": intent.id, "fill_size": 1},
            )
            assert response.status_code == 200
            data = response.json()
            assert data["intent_id"] == intent.id
            assert data["filled_size"] == 1
            assert data["status"] == "FILLED"

            fills = store.get_recent_fills(limit=10)
            assert len(fills) == 1
            assert fills[0]["intent_id"] == intent.id

            with sqlite3.connect(store.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT note, best_bid_at_fill_cents, cross_at_fill_cents
                    FROM paper_fills
                    WHERE intent_id = ?
                    ORDER BY fill_ts_ms DESC
                    LIMIT 1
                    """,
                    (intent.id,),
                )
                note_row = cursor.fetchone()
                assert note_row is not None
                assert note_row[0] == "debug_fill"
                assert note_row[1] == 52.0
                assert note_row[2] == 54.0

            positions = store.get_positions()
            assert len(positions) == 1
            assert positions[0]["ticker"] == intent.ticker
            assert positions[0]["side"] == intent.side
        finally:
            main_module._paper_store = None
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)

    def test_paper_debug_force_fill_rejects_non_positive_size(self, client: TestClient) -> None:
        """POST /paper/debug/force-fill returns 400 for fill_size <= 0."""
        import kalshi_edge_engine.api.main as main_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = PaperStore(db_path=db_path)
            intent = QuoteIntent(
                id="intent-force-bad-size",
                ticker="KXNBAGAME-26MAR01SASNYK-SAS",
                side="YES",
                quote_price_cents=53.0,
                size_contracts=1,
                created_ts_ms=int(time.time() * 1000),
                net_edge=1.2,
                inside_width=2.2,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            )
            store.insert_intent(intent)
            main_module._paper_store = store

            response = client.post(
                "/paper/debug/force-fill",
                json={"intent_id": intent.id, "fill_size": 0},
            )
            assert response.status_code == 400
        finally:
            main_module._paper_store = None
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)

    def test_paper_debug_force_fill_returns_404_for_unknown_intent(
        self, client: TestClient
    ) -> None:
        """POST /paper/debug/force-fill returns 404 when intent is missing."""
        import kalshi_edge_engine.api.main as main_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            main_module._paper_store = PaperStore(db_path=db_path)

            response = client.post(
                "/paper/debug/force-fill",
                json={"intent_id": "does-not-exist", "fill_size": 1},
            )
            assert response.status_code == 404
        finally:
            main_module._paper_store = None
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)

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
                fee_yes=1.0,
                fee_no=None,
                fee_exit_yes=None,
                fee_exit_no=None,
                total_fee_yes=1.0,
                total_fee_no=None,
                fee_mode="maker",
                round_trip=False,
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
                    fee_yes=s.fee_yes,
                    fee_no=s.fee_no,
                    fee_exit_yes=s.fee_exit_yes,
                    fee_exit_no=s.fee_exit_no,
                    total_fee_yes=s.total_fee_yes,
                    total_fee_no=s.total_fee_no,
                    fee_mode=s.fee_mode,
                    round_trip=s.round_trip,
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


class TestPaperTradingControl:
    """Tests for paper trading start/stop endpoints."""

    def test_paper_start_succeeds_when_enabled(self, client: TestClient) -> None:
        """POST /paper/start should succeed when paper trading is enabled."""
        import kalshi_edge_engine.api.main as main_module

        # Setup: Initialize paper store and enable trading
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = PaperStore(db_path=db_path)
            main_module._paper_store = store
            main_module._ENABLE_PAPER_TRADING = True
            main_module._paper_enabled = False

            response = client.post("/paper/start")

            assert response.status_code == 200
            data = response.json()
            assert data["started"] is True
            assert main_module._paper_enabled is True
        finally:
            main_module._paper_store = None
            main_module._ENABLE_PAPER_TRADING = False
            main_module._paper_enabled = False
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)

    def test_paper_start_fails_when_disabled(self, client: TestClient) -> None:
        """POST /paper/start should fail when paper trading is not enabled."""
        import kalshi_edge_engine.api.main as main_module

        # Ensure paper trading is disabled
        main_module._ENABLE_PAPER_TRADING = False
        main_module._paper_store = None

        response = client.post("/paper/start")

        assert response.status_code == 200
        data = response.json()
        assert data["started"] is False
        assert data["reason"] == "not_available"

    def test_paper_start_already_running(self, client: TestClient) -> None:
        """POST /paper/start should return already_running if already started."""
        import kalshi_edge_engine.api.main as main_module

        # Setup
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = PaperStore(db_path=db_path)
            main_module._paper_store = store
            main_module._ENABLE_PAPER_TRADING = True
            main_module._paper_enabled = True  # Already running

            response = client.post("/paper/start")

            assert response.status_code == 200
            data = response.json()
            assert data["started"] is False
            assert data["reason"] == "already_running"
        finally:
            main_module._paper_store = None
            main_module._ENABLE_PAPER_TRADING = False
            main_module._paper_enabled = False
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)

    def test_paper_stop_succeeds(self, client: TestClient) -> None:
        """POST /paper/stop should succeed when paper trading is running."""
        import kalshi_edge_engine.api.main as main_module

        # Setup
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = PaperStore(db_path=db_path)
            main_module._paper_store = store
            main_module._ENABLE_PAPER_TRADING = True
            main_module._paper_enabled = True

            response = client.post("/paper/stop")

            assert response.status_code == 200
            data = response.json()
            assert data["stopped"] is True
            assert main_module._paper_enabled is False
        finally:
            main_module._paper_store = None
            main_module._ENABLE_PAPER_TRADING = False
            main_module._paper_enabled = False
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)

    def test_paper_stop_not_running(self, client: TestClient) -> None:
        """POST /paper/stop should return not_running if not started."""
        import kalshi_edge_engine.api.main as main_module

        # Ensure paper trading is stopped
        main_module._paper_enabled = False

        response = client.post("/paper/stop")

        assert response.status_code == 200
        data = response.json()
        assert data["stopped"] is False
        assert data["reason"] == "not_running"


class TestPaperMetricsEndpoint:
    """Tests for paper metrics endpoint."""

    def test_paper_metrics_returns_activity_fields(self, client: TestClient) -> None:
        """GET /paper/metrics returns fill rate and timing metrics."""
        import kalshi_edge_engine.api.main as main_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = PaperStore(db_path=db_path)
            main_module._paper_store = store

            now_ms = int(time.time() * 1000)

            recent_filled_1 = QuoteIntent(
                id="intent-metrics-1",
                ticker="KXNBAGAME-26MAR01AAA-BBB",
                side="YES",
                quote_price_cents=50.0,
                size_contracts=1,
                created_ts_ms=now_ms - 20_000,
                net_edge=1.0,
                inside_width=2.0,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            )
            recent_filled_2 = QuoteIntent(
                id="intent-metrics-2",
                ticker="KXNBAGAME-26MAR01CCC-DDD",
                side="NO",
                quote_price_cents=48.0,
                size_contracts=1,
                created_ts_ms=now_ms - 50_000,
                net_edge=1.2,
                inside_width=2.5,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            )
            old_unfilled = QuoteIntent(
                id="intent-metrics-3",
                ticker="KXNBAGAME-26MAR01EEE-FFF",
                side="YES",
                quote_price_cents=52.0,
                size_contracts=1,
                created_ts_ms=now_ms - 7_200_000,
                net_edge=0.9,
                inside_width=1.8,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            )

            store.insert_intent(recent_filled_1)
            store.insert_intent(recent_filled_2)
            store.insert_intent(old_unfilled)

            inserted_1 = store.insert_fill(
                fill_id="fill-metrics-1",
                intent_id="intent-metrics-1",
                fill_ts_ms=now_ms - 10_000,
                fill_price_cents=50.0,
                fill_size=1,
                remaining_size=0,
                queue_ahead_at_fill=0,
                fill_fee_cents=0.1,
            )
            inserted_2 = store.insert_fill(
                fill_id="fill-metrics-2",
                intent_id="intent-metrics-2",
                fill_ts_ms=now_ms - 30_000,
                fill_price_cents=48.0,
                fill_size=1,
                remaining_size=0,
                queue_ahead_at_fill=1,
                fill_fee_cents=0.1,
            )

            assert inserted_1 is True
            assert inserted_2 is True

            response = client.get("/paper/metrics")
            assert response.status_code == 200

            data = response.json()
            assert data["fill_rate"] == pytest.approx(2 / 3)
            assert data["avg_time_to_fill_seconds"] == pytest.approx(15.0)
            assert data["fills_last_1h"] == 2
            assert data["intents_last_1h"] == 2
        finally:
            main_module._paper_store = None
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)


class TestPaperConfigAndSummaryEndpoints:
    """Tests for /paper/config and /paper/summary consistency."""

    def test_paper_config_active_uses_running_tasks_only(self, client: TestClient) -> None:
        """GET /paper/config should only mark active when any tracked task is not done."""
        import kalshi_edge_engine.api.main as main_module

        class _StubTask:
            def __init__(self, done_value: bool) -> None:
                self._done_value = done_value

            def done(self) -> bool:
                return self._done_value

        main_module._paper_enabled = True
        main_module._paper_simulation_tasks = {_StubTask(True), _StubTask(False)}  # type: ignore[assignment]
        response_active = client.get("/paper/config")
        assert response_active.status_code == 200
        assert response_active.json()["active"] is True

        main_module._paper_simulation_tasks = {_StubTask(True)}  # type: ignore[assignment]
        response_done_only = client.get("/paper/config")
        assert response_done_only.status_code == 200
        assert response_done_only.json()["active"] is False

        main_module._paper_enabled = False
        main_module._paper_simulation_tasks = set()

    def test_paper_summary_uses_total_intent_count(self, client: TestClient) -> None:
        """GET /paper/summary should use total COUNT(*) and not be capped by status list limits."""
        import kalshi_edge_engine.api.main as main_module

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = PaperStore(db_path=db_path)
            main_module._paper_store = store
            main_module._paper_enabled = True

            now_ms = int(time.time() * 1000)
            with sqlite3.connect(db_path) as conn:
                for idx in range(1005):
                    conn.execute(
                        """
                        INSERT INTO quote_intents
                        (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                         net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"summary-intent-{idx}",
                            f"KXNBAGAME-SUM-{idx}",
                            "YES",
                            50.0,
                            1,
                            now_ms,
                            1.0,
                            2.0,
                            None,
                            "maker",
                            True,
                            "OPEN",
                        ),
                    )
                conn.commit()

            response = client.get("/paper/summary")
            assert response.status_code == 200
            assert response.json()["total_intents_created"] == 1005
        finally:
            main_module._paper_store = None
            main_module._paper_enabled = False
            gc.collect()
            if os.path.exists(db_path):
                with contextlib.suppress(OSError):
                    os.unlink(db_path)
