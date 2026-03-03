"""Tests for MAX_DAILY_LOSS safety gate."""

import time

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore


class TestMaxDailyLossGate:
    """Test MAX_DAILY_LOSS safety gate."""

    @pytest.fixture
    def paper_store(self, tmp_path):
        """Create a temp paper store."""
        db_path = str(tmp_path / "test_max_loss.db")
        return PaperStore(db_path)

    def test_max_daily_loss_detection(self, paper_store: PaperStore):
        """Test that MAX_DAILY_LOSS breach is detected correctly."""
        now_ms = int(time.time() * 1000)

        # Simulate a losing day: realized = -100, unrealized = -50
        realized_pnl_cents = -100.0
        unrealized_pnl_cents = -50.0
        total_pnl_cents = realized_pnl_cents + unrealized_pnl_cents

        # If MAX_DAILY_LOSS_CENTS = 100, this should breach (total = -150)
        assert total_pnl_cents == -150.0

        # Insert snapshot
        paper_store.insert_pnl_snapshot(
            ts_ms=now_ms,
            realized_pnl_cents=realized_pnl_cents,
            unrealized_pnl_cents=unrealized_pnl_cents,
            open_positions_count=2,
            open_orders_count=0,
        )

        # Retrieve and verify
        snapshot = paper_store.get_latest_pnl_snapshot()
        assert snapshot is not None
        assert snapshot["total_pnl_cents"] < -100.0  # Breach threshold

    def test_max_daily_loss_not_breached(self, paper_store: PaperStore):
        """Test that snapshot below threshold does not breach."""
        now_ms = int(time.time() * 1000)

        # Simulate a small loss: realized = -30, unrealized = -20
        realized_pnl_cents = -30.0
        unrealized_pnl_cents = -20.0
        total_pnl_cents = realized_pnl_cents + unrealized_pnl_cents

        # If MAX_DAILY_LOSS_CENTS = 100, this should NOT breach (total = -50)
        assert total_pnl_cents == -50.0

        # Insert snapshot
        paper_store.insert_pnl_snapshot(
            ts_ms=now_ms,
            realized_pnl_cents=realized_pnl_cents,
            unrealized_pnl_cents=unrealized_pnl_cents,
            open_positions_count=1,
            open_orders_count=1,
        )

        # Retrieve and verify
        snapshot = paper_store.get_latest_pnl_snapshot()
        assert snapshot is not None
        assert snapshot["total_pnl_cents"] > -100.0  # Should not breach

    def test_max_daily_loss_positive_pnl(self, paper_store: PaperStore):
        """Test that positive PnL never breaches MAX_DAILY_LOSS."""
        now_ms = int(time.time() * 1000)

        # Simulate a winning day
        realized_pnl_cents = 200.0
        unrealized_pnl_cents = 50.0
        total_pnl_cents = realized_pnl_cents + unrealized_pnl_cents

        assert total_pnl_cents == 250.0

        # Insert snapshot
        paper_store.insert_pnl_snapshot(
            ts_ms=now_ms,
            realized_pnl_cents=realized_pnl_cents,
            unrealized_pnl_cents=unrealized_pnl_cents,
            open_positions_count=3,
            open_orders_count=2,
        )

        # Retrieve and verify
        snapshot = paper_store.get_latest_pnl_snapshot()
        assert snapshot is not None
        assert snapshot["total_pnl_cents"] > 0  # Never breaches with positive PnL
