"""Tests for paper store (SQLite persistence)."""

import contextlib
import os
import tempfile
import time

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import PaperFill, PaperOutcome, QuoteIntent


@pytest.fixture
def temp_db_path() -> str:
    """Create a temporary database path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    # Cleanup - ensure database is closed before deleting
    try:
        # Try to close any lingering connections
        import gc

        gc.collect()
        if os.path.exists(path):
            # Try to remove, ignoring errors on Windows
            with contextlib.suppress(OSError):
                os.unlink(path)
    except Exception:
        pass


@pytest.fixture
def paper_store(temp_db_path: str) -> PaperStore:
    """Create a paper store with a temporary database."""
    return PaperStore(db_path=temp_db_path)


@pytest.fixture
def sample_intent() -> QuoteIntent:
    """Create a sample quote intent."""
    now_ms = int(time.time() * 1000)
    return QuoteIntent(
        id="intent-test-1",
        ticker="KXNBAGAME-26MAR01SASNYK-SAS",
        side="YES",
        quote_price_cents=50.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=0.5,
        inside_width=1.5,
        imbalance_gap=5.0,
        fee_mode="maker",
        round_trip=True,
    )


@pytest.fixture
def sample_fill(sample_intent: QuoteIntent) -> PaperFill:
    """Create a sample paper fill."""
    now_ms = int(time.time() * 1000)
    return PaperFill(
        intent_id=sample_intent.id,
        filled=True,
        fill_ts_ms=now_ms,
        fill_price_cents=50.5,
        time_to_fill_ms=5000,
    )


@pytest.fixture
def sample_outcome(sample_intent: QuoteIntent) -> PaperOutcome:
    """Create a sample paper outcome."""
    now_ms = int(time.time() * 1000)
    return PaperOutcome(
        intent_id=sample_intent.id,
        exit_ts_ms=now_ms,
        exit_price_cents=52.0,
        total_fees_cents=0.4,
        pnl_cents=150.0 - 0.4,  # (52 - 50) * 100 - 0.4
        pnl_dollars=1.496,
        note="filled",
    )


class TestPaperStore:
    """Tests for PaperStore class."""

    def test_init_creates_schema(self, temp_db_path: str) -> None:
        """Test that initialization creates the database schema."""
        PaperStore(db_path=temp_db_path)
        assert os.path.exists(temp_db_path)

    def test_insert_and_retrieve_intent(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """Test inserting and retrieving a quote intent."""
        paper_store.insert_intent(sample_intent)

        retrieved = paper_store.get_intent(sample_intent.id)
        assert retrieved is not None
        assert retrieved.id == sample_intent.id
        assert retrieved.ticker == sample_intent.ticker
        assert retrieved.side == sample_intent.side
        assert retrieved.quote_price_cents == sample_intent.quote_price_cents
        assert retrieved.size_contracts == sample_intent.size_contracts
        assert retrieved.fee_mode == sample_intent.fee_mode
        assert retrieved.round_trip == sample_intent.round_trip

    def test_get_nonexistent_intent_returns_none(self, paper_store: PaperStore) -> None:
        """Test that retrieving a non-existent intent returns None."""
        result = paper_store.get_intent("nonexistent-id")
        assert result is None

    def test_insert_and_retrieve_fill(
        self, paper_store: PaperStore, sample_intent: QuoteIntent, sample_fill: PaperFill
    ) -> None:
        """Test inserting a quote intent and fill."""
        paper_store.insert_intent(sample_intent)
        paper_store.insert_fill(sample_fill)

        # Verify intent was stored
        retrieved_intent = paper_store.get_intent(sample_intent.id)
        assert retrieved_intent is not None

    def test_insert_and_retrieve_outcome(
        self,
        paper_store: PaperStore,
        sample_intent: QuoteIntent,
        sample_fill: PaperFill,
        sample_outcome: PaperOutcome,
    ) -> None:
        """Test inserting and retrieving outcomes."""
        paper_store.insert_intent(sample_intent)
        paper_store.insert_fill(sample_fill)
        paper_store.insert_outcome(sample_outcome)

        # Verify all were stored (we'd need specific retrieval methods for detailed tests)
        retrieved_intent = paper_store.get_intent(sample_intent.id)
        assert retrieved_intent is not None

    def test_get_recent_intents(self, paper_store: PaperStore) -> None:
        """Test retrieving recent intents."""
        now_ms = int(time.time() * 1000)

        # Insert multiple intents
        for i in range(5):
            intent = QuoteIntent(
                id=f"intent-{i}",
                ticker="KXNBAGAME-26MAR01SASNYK-SAS",
                side="YES",
                quote_price_cents=50.0 + i,
                size_contracts=1,
                created_ts_ms=now_ms + i * 1000,
                net_edge=0.5,
                inside_width=1.5,
                imbalance_gap=5.0,
                fee_mode="maker",
                round_trip=True,
            )
            paper_store.insert_intent(intent)

        results = paper_store.get_recent_intents(limit=3)
        assert len(results) == 3
        # Most recent should be first
        assert results[0].id == "intent-4"
        assert results[1].id == "intent-3"
        assert results[2].id == "intent-2"

    def test_get_outcome_stats(
        self,
        paper_store: PaperStore,
        sample_intent: QuoteIntent,
        sample_fill: PaperFill,
        sample_outcome: PaperOutcome,
    ) -> None:
        """Test getting outcome statistics."""
        paper_store.insert_intent(sample_intent)
        paper_store.insert_fill(sample_fill)
        paper_store.insert_outcome(sample_outcome)

        stats = paper_store.get_outcome_stats(limit=10)
        assert stats["count"] == 1
        assert stats["total_pnl_cents"] == sample_outcome.pnl_cents
        assert stats["total_fees_cents"] == sample_outcome.total_fees_cents

    def test_get_outcome_stats_empty(self, paper_store: PaperStore) -> None:
        """Test getting stats when no outcomes exist."""
        stats = paper_store.get_outcome_stats(limit=10)
        assert stats["count"] == 0
        assert stats["total_pnl_cents"] == 0.0
        assert stats["avg_pnl_cents"] == 0.0

    def test_insert_multiple_outcomes_and_aggregate_stats(self, paper_store: PaperStore) -> None:
        """Test aggregating stats from multiple outcomes."""
        now_ms = int(time.time() * 1000)

        # Insert 3 outcomes
        pnls = [100.0, -50.0, 200.0]
        fees = [0.5, 0.5, 0.5]

        for i, pnl in enumerate(pnls):
            intent = QuoteIntent(
                id=f"intent-{i}",
                ticker="KXNBAGAME-26MAR01SASNYK-SAS",
                side="YES",
                quote_price_cents=50.0,
                size_contracts=1,
                created_ts_ms=now_ms,
                net_edge=0.5,
                inside_width=1.5,
                imbalance_gap=5.0,
                fee_mode="maker",
                round_trip=True,
            )
            paper_store.insert_intent(intent)

            fill = PaperFill(
                intent_id=intent.id,
                filled=True,
                fill_ts_ms=now_ms,
                fill_price_cents=50.0,
                time_to_fill_ms=1000,
            )
            paper_store.insert_fill(fill)

            outcome = PaperOutcome(
                intent_id=intent.id,
                exit_ts_ms=now_ms,
                exit_price_cents=51.0,
                total_fees_cents=fees[i],
                pnl_cents=pnl,
                pnl_dollars=pnl / 100.0,
                note="filled",
            )
            paper_store.insert_outcome(outcome)

        stats = paper_store.get_outcome_stats(limit=10)
        assert stats["count"] == 3
        assert stats["total_pnl_cents"] == sum(pnls)
        assert stats["min_pnl_cents"] == min(pnls)
        assert stats["max_pnl_cents"] == max(pnls)
        assert abs(stats["avg_pnl_cents"] - (sum(pnls) / len(pnls))) < 0.01
        assert stats["total_fees_cents"] == sum(fees)


class TestPaperStoreV2:
    """Tests for PaperStore v2 API (realistic order lifecycle)."""

    def test_insert_fill_v2_api(self, paper_store: PaperStore, sample_intent: QuoteIntent):
        """Test inserting a fill using v2 API."""
        import uuid

        paper_store.insert_intent(sample_intent)

        fill_id = str(uuid.uuid4())
        paper_store.insert_fill(
            fill_id=fill_id,
            intent_id=sample_intent.id,
            fill_ts_ms=int(time.time() * 1000),
            fill_price_cents=50.5,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.5,
        )

        fills = paper_store.get_recent_fills(limit=10)
        assert len(fills) == 1
        assert fills[0]["intent_id"] == sample_intent.id
        assert fills[0]["fill_price_cents"] == 50.5
        assert fills[0]["fill_fee_cents"] == 0.5

    def test_update_intent_status(self, paper_store: PaperStore, sample_intent: QuoteIntent):
        """Test updating intent status."""
        paper_store.insert_intent(sample_intent)
        paper_store.update_intent_status(
            sample_intent.id,
            status="OPEN",
            last_seen_best_bid=50.5,
            queue_ahead=2,
        )

        intent = paper_store.get_intent(sample_intent.id)
        assert intent is not None

    def test_upsert_position_insert(self, paper_store: PaperStore):
        """Test upserting a new position."""
        now_ms = int(time.time() * 1000)
        paper_store.upsert_position(
            ticker="TEST_TICKER",
            side="YES",
            net_contracts=5,
            avg_entry_price_cents=50.0,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms,
        )

        positions = paper_store.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "TEST_TICKER"
        assert positions[0]["side"] == "YES"
        assert positions[0]["net_contracts"] == 5
        assert positions[0]["avg_entry_price_cents"] == 50.0

    def test_upsert_position_update(self, paper_store: PaperStore):
        """Test upserting an existing position."""
        now_ms = int(time.time() * 1000)

        # Insert initial position
        paper_store.upsert_position(
            ticker="TEST_TICKER",
            side="YES",
            net_contracts=5,
            avg_entry_price_cents=50.0,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms,
        )

        # Update the same position
        paper_store.upsert_position(
            ticker="TEST_TICKER",
            side="YES",
            net_contracts=10,
            avg_entry_price_cents=51.0,
            realized_pnl_cents=100.0,
            first_open_ts_ms=now_ms,
        )

        positions = paper_store.get_positions()
        assert len(positions) == 1
        assert positions[0]["net_contracts"] == 10
        assert positions[0]["avg_entry_price_cents"] == 51.0
        assert positions[0]["realized_pnl_cents"] == 100.0

    def test_insert_pnl_snapshot(self, paper_store: PaperStore):
        """Test inserting a PnL snapshot."""
        now_ms = int(time.time() * 1000)
        paper_store.insert_pnl_snapshot(
            ts_ms=now_ms,
            realized_pnl_cents=150.0,
            unrealized_pnl_cents=50.0,
            open_positions_count=2,
            open_orders_count=3,
        )

        snapshot = paper_store.get_latest_pnl_snapshot()
        assert snapshot is not None
        assert snapshot["ts_ms"] == now_ms
        assert snapshot["realized_pnl_cents"] == 150.0
        assert snapshot["unrealized_pnl_cents"] == 50.0
        assert snapshot["total_pnl_cents"] == 200.0
        assert snapshot["open_positions_count"] == 2
        assert snapshot["open_orders_count"] == 3

    def test_get_intents_by_status(self, paper_store: PaperStore):
        """Test retrieving intents by status."""
        now_ms = int(time.time() * 1000)

        # Create intents with different statuses
        for i in range(3):
            intent = QuoteIntent(
                id=f"intent-{i}",
                ticker="KXNBAGAME-26MAR01SASNYK-SAS",
                side="YES",
                quote_price_cents=50.0,
                size_contracts=1,
                created_ts_ms=now_ms,
                net_edge=0.5,
                inside_width=1.5,
                imbalance_gap=5.0,
                fee_mode="maker",
                round_trip=True,
            )
            paper_store.insert_intent(intent)
            if i == 0:
                paper_store.update_intent_status(intent.id, "OPEN")
            elif i == 1:
                paper_store.update_intent_status(intent.id, "FILLED")
            else:
                paper_store.update_intent_status(intent.id, "EXPIRED")

        open_intents = paper_store.get_intents_by_status("OPEN", limit=10)
        filled_intents = paper_store.get_intents_by_status("FILLED", limit=10)
        expired_intents = paper_store.get_intents_by_status("EXPIRED", limit=10)

        assert len(open_intents) == 1
        assert open_intents[0]["status"] == "OPEN"

        assert len(filled_intents) == 1
        assert filled_intents[0]["status"] == "FILLED"

        assert len(expired_intents) == 1
        assert expired_intents[0]["status"] == "EXPIRED"

    def test_get_recent_fills(self, paper_store: PaperStore):
        """Test retrieving recent fills."""
        import uuid

        now_ms = int(time.time() * 1000)

        # Create intents and fills
        for i in range(3):
            intent = QuoteIntent(
                id=f"intent-{i}",
                ticker="KXNBAGAME-26MAR01SASNYK-SAS",
                side="YES",
                quote_price_cents=50.0 + i,
                size_contracts=1,
                created_ts_ms=now_ms,
                net_edge=0.5,
                inside_width=1.5,
                imbalance_gap=5.0,
                fee_mode="maker",
                round_trip=True,
            )
            paper_store.insert_intent(intent)

            fill_id = str(uuid.uuid4())
            paper_store.insert_fill(
                fill_id=fill_id,
                intent_id=intent.id,
                fill_ts_ms=now_ms + i * 1000,
                fill_price_cents=50.0 + i,
                fill_size=1,
                remaining_size=0,
                queue_ahead_at_fill=0,
                fill_fee_cents=0.5,
            )

        fills = paper_store.get_recent_fills(limit=10)
        assert len(fills) == 3
        # Most recent should be first
        assert fills[0]["intent_id"] == "intent-2"
        assert fills[1]["intent_id"] == "intent-1"
        assert fills[2]["intent_id"] == "intent-0"

    def test_get_intents_by_status_includes_runtime_fields(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """Status query includes simulator runtime tracking fields."""
        paper_store.insert_intent(sample_intent)
        paper_store.update_intent_status(
            sample_intent.id,
            status="OPEN",
            last_seen_best_bid=50.0,
            queue_ahead=3,
        )

        intents = paper_store.get_intents_by_status("OPEN", limit=10)
        assert len(intents) == 1
        assert intents[0]["last_seen_best_bid"] == 50.0
        assert intents[0]["queue_ahead_contracts"] == 3
        assert intents[0]["last_update_ts_ms"] is not None

    def test_has_open_intent_for_ticker_side(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """Open-intent lookup works for ticker+side de-dup checks."""
        paper_store.insert_intent(sample_intent)
        paper_store.update_intent_status(sample_intent.id, status="OPEN")

        assert paper_store.has_open_intent_for_ticker_side(sample_intent.ticker, sample_intent.side)
        assert not paper_store.has_open_intent_for_ticker_side(sample_intent.ticker, "NO")

    def test_count_helpers_and_intent_record(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """Count helpers and full intent record retrieval return expected data."""
        paper_store.insert_intent(sample_intent)
        paper_store.update_intent_status(sample_intent.id, status="OPEN")

        assert paper_store.count_intents_by_status("OPEN") == 1
        assert paper_store.count_open_positions_for_ticker(sample_intent.ticker) == 0

        paper_store.upsert_position(
            ticker=sample_intent.ticker,
            side=sample_intent.side,
            net_contracts=2,
            avg_entry_price_cents=49.5,
            realized_pnl_cents=0.0,
            first_open_ts_ms=sample_intent.created_ts_ms,
        )
        assert paper_store.count_open_positions_for_ticker(sample_intent.ticker) == 1

        record = paper_store.get_intent_record(sample_intent.id)
        assert record is not None
        assert record["id"] == sample_intent.id
        assert record["status"] == "OPEN"
        assert record["ticker"] == sample_intent.ticker
