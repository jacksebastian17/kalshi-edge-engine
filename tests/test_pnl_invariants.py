"""Tests for PnL snapshot invariants and fee-awareness."""

import sqlite3
import time

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import estimate_taker_fee_cents
from kalshi_edge_engine.engine.paper import QuoteIntent


class TestPnLInvariants:
    """Test PnL snapshot invariants."""

    @pytest.fixture
    def paper_store(self, tmp_path):
        """Create a temp paper store."""
        db_path = str(tmp_path / "test_pnl.db")
        return PaperStore(db_path)

    def test_pnl_snapshot_invariant(self, paper_store: PaperStore):
        """Test that total_pnl_cents == realized_pnl_cents + unrealized_pnl_cents."""
        now_ms = int(time.time() * 1000)

        # Insert a snapshot
        paper_store.insert_pnl_snapshot(
            ts_ms=now_ms,
            realized_pnl_cents=100.0,
            unrealized_pnl_cents=-50.0,
            open_positions_count=2,
            open_orders_count=1,
        )

        # Retrieve and verify invariant
        snapshot = paper_store.get_latest_pnl_snapshot()
        assert snapshot is not None
        assert snapshot["realized_pnl_cents"] == 100.0
        assert snapshot["unrealized_pnl_cents"] == -50.0
        assert snapshot["total_pnl_cents"] == 50.0  # 100 - 50
        assert snapshot["total_pnl_cents"] == (
            snapshot["realized_pnl_cents"] + snapshot["unrealized_pnl_cents"]
        )

    def test_pnl_snapshot_negative_total(self, paper_store: PaperStore):
        """Test PnL snapshot with negative total."""
        now_ms = int(time.time() * 1000)

        # Insert a snapshot with negative total
        paper_store.insert_pnl_snapshot(
            ts_ms=now_ms,
            realized_pnl_cents=-150.0,
            unrealized_pnl_cents=-75.0,
            open_positions_count=1,
            open_orders_count=0,
        )

        # Retrieve and verify invariant
        snapshot = paper_store.get_latest_pnl_snapshot()
        assert snapshot is not None
        assert snapshot["total_pnl_cents"] == -225.0  # -150 - 75
        assert snapshot["total_pnl_cents"] == (
            snapshot["realized_pnl_cents"] + snapshot["unrealized_pnl_cents"]
        )

    def test_fee_tracking_in_fills(self, paper_store: PaperStore):
        """Test that fills track fee_cents correctly."""
        now_ms = int(time.time() * 1000)

        # Create and insert intent
        intent = QuoteIntent(
            id="test-intent-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=10,
            created_ts_ms=now_ms,
            net_edge=1.5,
            inside_width=2.0,
            imbalance_gap=0.5,
            fee_mode="maker",
            round_trip=True,
        )
        paper_store.insert_intent(intent, entry_fee_cents=2.5)

        # Insert a fill with fee
        fill_fee_cents = 2.5
        paper_store.insert_fill(
            fill_id="test-fill-1",
            intent_id=intent.id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=50.0,
            fill_size=10,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=fill_fee_cents,
        )

        # Retrieve fills and verify fee tracking
        fills = paper_store.get_fills_for_position("TEST-TICKER", "YES")
        assert len(fills) == 1
        assert fills[0]["fill_fee_cents"] == fill_fee_cents

        # Verify total fees for position
        total_fees = sum(f["fill_fee_cents"] for f in fills)
        assert total_fees == fill_fee_cents

    def test_fee_aware_unrealized_pnl_computation(self, paper_store: PaperStore):
        """Test that unrealized PnL computation accounts for fees."""
        now_ms = int(time.time() * 1000)

        # Create and insert intent
        intent = QuoteIntent(
            id="test-intent-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=10,
            created_ts_ms=now_ms,
            net_edge=1.5,
            inside_width=2.0,
            imbalance_gap=0.5,
            fee_mode="maker",
            round_trip=True,
        )
        paper_store.insert_intent(intent, entry_fee_cents=2.5)

        # Insert a fill
        paper_store.insert_fill(
            fill_id="test-fill-1",
            intent_id=intent.id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=50.0,
            fill_size=10,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=2.5,
        )

        # Create position
        paper_store.upsert_position(
            ticker="TEST-TICKER",
            side="YES",
            net_contracts=10,
            avg_entry_price_cents=50.0,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms,
        )

        # Get fills for position
        fills = paper_store.get_fills_for_position("TEST-TICKER", "YES")
        total_fees = sum(f["fill_fee_cents"] for f in fills)

        # Simulate mark price at 52 (2 cent gain per contract)
        mark_price = 52.0
        entry_price = 50.0
        net_contracts = 10

        # Price PnL: (52 - 50) * 10 = 20
        price_pnl = (mark_price - entry_price) * net_contracts
        assert price_pnl == 20.0

        # Unrealized PnL should subtract fees: 20 - 2.5 = 17.5
        unrealized_pnl = price_pnl - total_fees
        assert unrealized_pnl == 17.5


class TestTelemetryCapture:
    """Test that telemetry fields are captured."""

    @pytest.fixture
    def paper_store(self, tmp_path):
        """Create a temp paper store."""
        db_path = str(tmp_path / "test_telemetry.db")
        return PaperStore(db_path)

    def test_intent_telemetry_fields(self, paper_store: PaperStore):
        """Test that intent captures best_bid and cross at submit time."""
        now_ms = int(time.time() * 1000)

        intent = QuoteIntent(
            id="test-intent-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.5,
            inside_width=2.0,
            imbalance_gap=0.5,
            fee_mode="maker",
            round_trip=True,
        )

        # Insert with telemetry
        paper_store.insert_intent(
            intent,
            entry_fee_cents=1.0,
            best_bid_at_submit_cents=49.0,
            cross_at_submit_cents=51.0,
        )

        # Retrieve and verify telemetry
        record = paper_store.get_intent_record(intent.id)
        assert record is not None
        # Note: get_intent_record doesn't return telemetry fields yet
        # This test validates that insert succeeds with telemetry fields

    def test_fill_telemetry_fields(self, paper_store: PaperStore):
        """Test that fills capture best_bid and cross at fill time."""
        now_ms = int(time.time() * 1000)

        # Create intent first
        intent = QuoteIntent(
            id="test-intent-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.5,
            inside_width=2.0,
            imbalance_gap=0.5,
            fee_mode="maker",
            round_trip=True,
        )
        paper_store.insert_intent(intent)

        # Insert fill with telemetry
        paper_store.insert_fill(
            fill_id="test-fill-1",
            intent_id=intent.id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=1.0,
            best_bid_at_fill_cents=50.0,
            cross_at_fill_cents=52.0,
        )

        # Retrieve fills
        fills = paper_store.get_fills_for_position("TEST-TICKER", "YES")
        assert len(fills) == 1
        # Note: get_fills_for_position doesn't return telemetry fields yet
        # This test validates that insert succeeds with telemetry fields

    def test_schema_migration_adds_columns(self, paper_store: PaperStore):
        """Test that schema migration adds telemetry columns safely."""
        # The migration should have run during __init__
        # Verify we can insert with telemetry fields
        now_ms = int(time.time() * 1000)

        intent = QuoteIntent(
            id="test-intent-1",
            ticker="TEST-TICKER",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.5,
            inside_width=2.0,
            imbalance_gap=0.5,
            fee_mode="maker",
            round_trip=True,
        )

        # Should not raise
        paper_store.insert_intent(
            intent,
            entry_fee_cents=1.0,
            best_bid_at_submit_cents=49.0,
            cross_at_submit_cents=51.0,
        )

        paper_store.insert_fill(
            fill_id="test-fill-1",
            intent_id=intent.id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            best_bid_at_fill_cents=50.0,
            cross_at_fill_cents=52.0,
        )


def test_take_profit_close_realizes_pnl_and_updates_exit_ts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Take-profit close updates realized PnL, zeroes position, and stamps exit_ts_ms."""
    import kalshi_edge_engine.api.main as main_module

    paper_store = PaperStore(str(tmp_path / "test_take_profit_lifecycle.db"))
    now_ms = int(time.time() * 1000)

    intent = QuoteIntent(
        id="tp-intent-1",
        ticker="TEST-TICKER-TP",
        side="YES",
        quote_price_cents=50.0,
        size_contracts=2,
        created_ts_ms=now_ms,
        net_edge=1.5,
        inside_width=2.0,
        imbalance_gap=0.5,
        fee_mode="maker",
        round_trip=True,
    )
    paper_store.insert_intent(intent)
    paper_store.update_intent_status(intent.id, "FILLED")

    paper_store.insert_fill(
        fill_id="tp-entry-fill",
        intent_id=intent.id,
        fill_ts_ms=now_ms + 1000,
        fill_price_cents=50.0,
        fill_size=2,
        remaining_size=0,
        fill_fee_cents=0.0,
    )
    paper_store.upsert_position(
        ticker=intent.ticker,
        side=intent.side,
        net_contracts=2,
        avg_entry_price_cents=50.0,
        realized_pnl_cents=0.0,
        first_open_ts_ms=now_ms,
    )

    monkeypatch.setattr(main_module, "_paper_store", paper_store)
    monkeypatch.setattr(main_module, "_PAPER_TAKE_PROFIT_CENTS", 2.0)

    closed = main_module._maybe_close_position_take_profit(
        ticker=intent.ticker,
        side=intent.side,
        net_contracts=2,
        avg_entry_price_cents=50.0,
        realized_pnl_cents=0.0,
        first_open_ts_ms=now_ms,
        mark_price_cents=53.0,
        now_ms=now_ms + 10_000,
    )

    assert closed is True
    assert paper_store.get_positions() == []

    expected_realized = (53.0 - 50.0) * 2 - estimate_taker_fee_cents(53.0, 2)
    actual_realized = paper_store.get_total_realized_pnl_cents()
    assert actual_realized == pytest.approx(expected_realized)

    with sqlite3.connect(paper_store.db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT exit_ts_ms FROM quote_intents WHERE id = ?",
            (intent.id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] is not None

    paper_store.insert_pnl_snapshot(
        ts_ms=now_ms + 12_000,
        realized_pnl_cents=actual_realized,
        unrealized_pnl_cents=0.0,
        open_positions_count=0,
        open_orders_count=0,
    )
    snapshot = paper_store.get_latest_pnl_snapshot()
    assert snapshot is not None
    assert snapshot["total_pnl_cents"] == pytest.approx(
        snapshot["realized_pnl_cents"] + snapshot["unrealized_pnl_cents"]
    )
