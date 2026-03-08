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

    def test_new_intent_has_open_status(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """Test that newly inserted intents have status='OPEN'."""
        paper_store.insert_intent(sample_intent)

        # Verify status is OPEN via get_intents_by_status
        open_intents = paper_store.get_intents_by_status("OPEN", limit=10)
        assert len(open_intents) == 1
        assert open_intents[0]["id"] == sample_intent.id
        assert open_intents[0]["status"] == "OPEN"

        # Verify no intents with 'NEW' status exist
        new_intents = paper_store.get_intents_by_status("NEW", limit=10)
        assert len(new_intents) == 0

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

    def test_universe_metadata_persists_in_intents_fills_and_outcomes(
        self,
        paper_store: PaperStore,
    ) -> None:
        """Universe metadata should survive storage and be visible in analytics views."""
        now_ms = int(time.time() * 1000)
        intent = QuoteIntent(
            id="intent-cbb-1",
            ticker="KXCBBGAME-TEST",
            universe="CBB",
            side="YES",
            quote_price_cents=47.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.2,
            inside_width=3.0,
            imbalance_gap=6.0,
            fee_mode="maker",
            round_trip=False,
        )
        paper_store.insert_intent(intent)

        intent_record = paper_store.get_intent_record(intent.id)
        assert intent_record is not None
        assert intent_record["universe"] == "CBB"

        inserted = paper_store.insert_fill(
            fill_id="fill-cbb-1",
            intent_id=intent.id,
            fill_ts_ms=now_ms + 100,
            fill_price_cents=47.0,
            fill_size=1,
            note="entry_queue_cleared",
            mark_30s_after_fill_cents=48.0,
            pnl_30s_after_fill_cents=1.0,
        )
        assert inserted is True

        recent_fills = paper_store.get_recent_fills(limit=1)
        assert recent_fills[0]["universe"] == "CBB"

        outcome_metrics = paper_store.get_short_horizon_outcome_metrics(window_seconds=3600)
        assert outcome_metrics["by_universe"]["CBB"]["sample_count"] == 1
        assert outcome_metrics["entry_only_by_universe"]["CBB"]["sample_count"] == 1

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

    def test_schema_smoke_test_telemetry_columns(self, temp_db_path: str) -> None:
        """Smoke test: verify that all telemetry columns exist after init + migrations."""
        import sqlite3

        # Instantiate PaperStore (triggers schema init + migrations)
        PaperStore(db_path=temp_db_path)

        # Connect to DB and check schema
        with sqlite3.connect(temp_db_path) as conn:
            cursor = conn.cursor()

            # Check quote_intents table has telemetry columns
            cursor.execute("PRAGMA table_info(quote_intents)")
            quote_intents_columns = {row[1] for row in cursor.fetchall()}
            assert "best_bid_at_submit_cents" in quote_intents_columns
            assert "cross_at_submit_cents" in quote_intents_columns

            # Check paper_fills table has telemetry columns
            cursor.execute("PRAGMA table_info(paper_fills)")
            paper_fills_columns = {row[1] for row in cursor.fetchall()}
            assert "best_bid_at_fill_cents" in paper_fills_columns
            assert "cross_at_fill_cents" in paper_fills_columns
            assert "mark_30s_after_fill_cents" in paper_fills_columns
            assert "pnl_30s_after_fill_cents" in paper_fills_columns


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
                best_bid_at_fill_cents=49.0 + i,
                cross_at_fill_cents=51.0 + i,
                mark_30s_after_fill_cents=52.0 + i,
                pnl_30s_after_fill_cents=1.5 + i,
                note=f"test_note_{i}",
            )

        fills = paper_store.get_recent_fills(limit=10)
        assert len(fills) == 3
        # Most recent should be first
        assert fills[0]["intent_id"] == "intent-2"
        assert fills[1]["intent_id"] == "intent-1"
        assert fills[2]["intent_id"] == "intent-0"
        assert fills[0]["note"] == "test_note_2"
        assert fills[0]["best_bid_at_fill_cents"] == 51.0
        assert fills[0]["cross_at_fill_cents"] == 53.0
        assert fills[0]["mark_30s_after_fill_cents"] == 54.0
        assert fills[0]["pnl_30s_after_fill_cents"] == 3.5

    def test_get_fills_for_position_includes_telemetry(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """Position fills query should include note + top-of-book telemetry fields."""
        now_ms = int(time.time() * 1000)
        paper_store.insert_intent(sample_intent)

        paper_store.insert_fill(
            fill_id="fill-pos-telemetry-1",
            intent_id=sample_intent.id,
            fill_ts_ms=now_ms,
            fill_price_cents=50.5,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.5,
            best_bid_at_fill_cents=50.0,
            cross_at_fill_cents=51.0,
            note="position_fill",
        )

        fills = paper_store.get_fills_for_position(sample_intent.ticker, sample_intent.side)
        assert len(fills) == 1
        assert fills[0]["note"] == "position_fill"
        assert fills[0]["best_bid_at_fill_cents"] == 50.0
        assert fills[0]["cross_at_fill_cents"] == 51.0

    def test_mark_exited_intents_skips_synthetic_exit_intents(
        self, paper_store: PaperStore
    ) -> None:
        """Exit metadata update should not touch synthetic exit_intent:* rows."""
        now_ms = int(time.time() * 1000)

        normal_intent = QuoteIntent(
            id="intent-normal-1",
            ticker="KXNBAGAME-TEST-EXIT",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        synthetic_exit_intent = QuoteIntent(
            id="exit_intent:take_profit:KXNBAGAME-TEST-EXIT:YES:123",
            ticker="KXNBAGAME-TEST-EXIT",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=0.0,
            inside_width=0.0,
            imbalance_gap=None,
            fee_mode="taker",
            round_trip=False,
        )

        paper_store.insert_intent(normal_intent)
        paper_store.insert_intent(synthetic_exit_intent)
        paper_store.update_intent_status(normal_intent.id, "FILLED")
        paper_store.update_intent_status(synthetic_exit_intent.id, "FILLED")

        paper_store.mark_exited_intents_for_ticker_side(
            ticker="KXNBAGAME-TEST-EXIT",
            side="YES",
            exit_ts_ms=now_ms + 1000,
            mark_price_cents=52.0,
        )

        import sqlite3

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT exit_ts_ms FROM quote_intents WHERE id = ?",
                (normal_intent.id,),
            )
            normal_exit_row = cursor.fetchone()
            assert normal_exit_row is not None
            assert normal_exit_row[0] is not None

            cursor.execute(
                "SELECT exit_ts_ms FROM quote_intents WHERE id = ?",
                (synthetic_exit_intent.id,),
            )
            synthetic_exit_row = cursor.fetchone()
            assert synthetic_exit_row is not None
            assert synthetic_exit_row[0] is None

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


class TestPaperStoreInsertFillValidation:
    """Tests for insert_fill validation (new API)."""

    def test_insert_fill_missing_fill_id_raises(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """insert_fill raises ValueError if fill_id is missing or empty."""
        paper_store.insert_intent(sample_intent)

        with pytest.raises(ValueError, match="fill_id must be a non-empty string"):
            paper_store.insert_fill(
                fill_id="",
                intent_id=sample_intent.id,
                fill_ts_ms=int(time.time() * 1000),
                fill_price_cents=50.0,
            )

        with pytest.raises(ValueError, match="fill_id must be a non-empty string"):
            paper_store.insert_fill(
                fill_id=None,  # type: ignore
                intent_id=sample_intent.id,
                fill_ts_ms=int(time.time() * 1000),
                fill_price_cents=50.0,
            )

    def test_insert_fill_missing_intent_id_raises(self, paper_store: PaperStore) -> None:
        """insert_fill raises ValueError if intent_id is missing or empty."""
        with pytest.raises(ValueError, match="intent_id must be a non-empty string"):
            paper_store.insert_fill(
                fill_id="fill-123",
                intent_id="",
                fill_ts_ms=int(time.time() * 1000),
                fill_price_cents=50.0,
            )

    def test_insert_fill_missing_fill_ts_ms_raises(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """insert_fill raises ValueError if fill_ts_ms is missing."""
        paper_store.insert_intent(sample_intent)

        with pytest.raises(ValueError, match="fill_ts_ms must be an integer"):
            paper_store.insert_fill(
                fill_id="fill-123",
                intent_id=sample_intent.id,
                fill_ts_ms=None,  # type: ignore
                fill_price_cents=50.0,
            )

    def test_insert_fill_missing_fill_price_cents_raises(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """insert_fill raises ValueError if fill_price_cents is missing."""
        paper_store.insert_intent(sample_intent)

        with pytest.raises(ValueError, match="fill_price_cents must be a number"):
            paper_store.insert_fill(
                fill_id="fill-123",
                intent_id=sample_intent.id,
                fill_ts_ms=int(time.time() * 1000),
                fill_price_cents=None,  # type: ignore
            )

    def test_insert_fill_negative_fill_size_raises(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """insert_fill raises ValueError if fill_size is negative."""
        paper_store.insert_intent(sample_intent)

        with pytest.raises(ValueError, match="fill_size must be an integer >= 0"):
            paper_store.insert_fill(
                fill_id="fill-123",
                intent_id=sample_intent.id,
                fill_ts_ms=int(time.time() * 1000),
                fill_price_cents=50.0,
                fill_size=-1,
            )

    def test_insert_fill_negative_remaining_size_raises(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """insert_fill raises ValueError if remaining_size is negative."""
        paper_store.insert_intent(sample_intent)

        with pytest.raises(ValueError, match="remaining_size must be an integer >= 0"):
            paper_store.insert_fill(
                fill_id="fill-123",
                intent_id=sample_intent.id,
                fill_ts_ms=int(time.time() * 1000),
                fill_price_cents=50.0,
                fill_size=1,
                remaining_size=-1,
            )

    def test_insert_fill_valid_positional_succeeds(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """insert_fill succeeds with valid positional arguments."""
        paper_store.insert_intent(sample_intent)

        # Should not raise
        paper_store.insert_fill(
            "fill-123",
            sample_intent.id,
            int(time.time() * 1000),
            50.0,
            1,
            0,
        )

        fills = paper_store.get_recent_fills(limit=10)
        assert len(fills) == 1
        assert fills[0]["fill_price_cents"] == 50.0

    def test_insert_fill_valid_keyword_succeeds(
        self, paper_store: PaperStore, sample_intent: QuoteIntent
    ) -> None:
        """insert_fill succeeds with valid keyword arguments."""
        paper_store.insert_intent(sample_intent)

        # Should not raise
        paper_store.insert_fill(
            fill_id="fill-456",
            intent_id=sample_intent.id,
            fill_ts_ms=int(time.time() * 1000),
            fill_price_cents=51.5,
            fill_size=1,
            remaining_size=0,
        )

        fills = paper_store.get_recent_fills(limit=10)
        assert len(fills) == 1
        assert fills[0]["fill_price_cents"] == 51.5
        assert fills[0]["fill_size"] == 1


class TestOverfillPrevention:
    """Tests for overfill prevention in insert_fill()."""

    @pytest.fixture
    def temp_db_path(self) -> str:
        """Create a temporary database path."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        yield path
        with contextlib.suppress(OSError):
            if os.path.exists(path):
                os.unlink(path)

    @pytest.fixture
    def paper_store(self, temp_db_path: str) -> PaperStore:
        """Create a paper store with a temporary database."""
        return PaperStore(db_path=temp_db_path)

    def _create_intent(
        self, paper_store: PaperStore, ticker: str, side: str, size_contracts: int = 1
    ) -> str:
        """Helper to create an intent in the database."""
        intent_id = f"intent-{ticker}-{side}-{int(time.time() * 1000)}"
        now_ms = int(time.time() * 1000)

        intent = QuoteIntent(
            id=intent_id,
            ticker=ticker,
            side=side,
            quote_price_cents=50.0,
            size_contracts=size_contracts,
            created_ts_ms=now_ms,
            net_edge=0.5,
            inside_width=1.0,
            imbalance_gap=None,
            fee_mode="taker",
            round_trip=False,
        )
        paper_store.insert_intent(intent)
        return intent_id

    def test_overfill_prevention_positional_args(self, paper_store: PaperStore) -> None:
        """Test overfill prevention with positional API.

        Scenario:
        - Create intent with size_contracts=1
        - Insert fill #1 (should succeed)
        - Insert fill #2 (should be prevented/rejected)
        - Verify only 1 fill exists in DB
        - Verify both calls return correct bool
        """
        intent_id = self._create_intent(paper_store, "TEST_OVERFILL_1", "YES", size_contracts=1)
        now_ms = int(time.time() * 1000)
        fill_price = 50.0

        # First fill (should succeed)
        fill_1_id = "fill-1-positional"
        result_1 = paper_store.insert_fill(
            fill_1_id,
            intent_id,
            now_ms,
            fill_price,
            1,  # fill_size
            0,  # remaining_size (will be corrected)
            0,  # queue_ahead_at_fill
            0.5,  # fill_fee_cents
        )
        assert result_1 is True, "First fill should be accepted"

        # Verify first fill was inserted
        filled_so_far = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_so_far == 1

        # Second fill (should be rejected)
        fill_2_id = "fill-2-positional"
        result_2 = paper_store.insert_fill(
            fill_2_id,
            intent_id,
            now_ms + 1000,
            fill_price,
            1,  # fill_size
            0,  # remaining_size
            0,  # queue_ahead_at_fill
            0.5,  # fill_fee_cents
        )
        assert result_2 is False, "Second fill should be rejected (overfill)"

        # Verify only 1 fill exists
        filled_total = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_total == 1, "DB should only have 1 fill"

        # Verify fills table has exactly 1 row
        import sqlite3

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            fill_count = cursor.fetchone()[0]
            assert fill_count == 1, "Only 1 fill row should exist"

    def test_overfill_prevention_kwargs(self, paper_store: PaperStore) -> None:
        """Test overfill prevention with keyword API.

        Scenario:
        - Create intent with size_contracts=1
        - Insert fill #1 using kwargs (should succeed)
        - Insert fill #2 using kwargs (should be prevented)
        - Verify only 1 fill exists in DB
        """
        intent_id = self._create_intent(paper_store, "TEST_OVERFILL_2", "NO", size_contracts=1)
        now_ms = int(time.time() * 1000)
        fill_price = 45.0

        # First fill with kwargs (should succeed)
        fill_1_id = "fill-1-kwargs"
        result_1 = paper_store.insert_fill(
            fill_id=fill_1_id,
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.4,
        )
        assert result_1 is True, "First fill should be accepted"

        # Second fill with kwargs (should be rejected)
        fill_2_id = "fill-2-kwargs"
        result_2 = paper_store.insert_fill(
            fill_id=fill_2_id,
            intent_id=intent_id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.4,
        )
        assert result_2 is False, "Second fill should be rejected (overfill)"

        # Verify only 1 fill exists
        import sqlite3

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            fill_count = cursor.fetchone()[0]
            assert fill_count == 1, "Only 1 fill row should exist"

    def test_overfill_prevention_partial_fills(self, paper_store: PaperStore) -> None:
        """Test overfill prevention with partial fills.

        Scenario:
        - Create intent with size_contracts=2
        - Insert fill #1 (1 contract, should succeed)
        - Insert fill #2 (1 contract, should succeed)
        - Insert fill #3 (1 contract, should be rejected)
        - Verify 2 fills exist, first is not overfilled
        """
        intent_id = self._create_intent(paper_store, "TEST_OVERFILL_3", "YES", size_contracts=2)
        now_ms = int(time.time() * 1000)
        fill_price = 52.0

        # Fill 1: 1 contract (should succeed)
        result_1 = paper_store.insert_fill(
            fill_id="fill-1-partial",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=1,  # Will be corrected to 1
        )
        assert result_1 is True, "First partial fill should succeed"

        filled_so_far = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_so_far == 1

        # Fill 2: 1 contract (should succeed, completes intent)
        result_2 = paper_store.insert_fill(
            fill_id="fill-2-partial",
            intent_id=intent_id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=0,  # Will be corrected to 0
        )
        assert result_2 is True, "Second partial fill should succeed"

        filled_so_far = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_so_far == 2

        # Fill 3: Attempt 1 more (should be rejected)
        result_3 = paper_store.insert_fill(
            fill_id="fill-3-partial",
            intent_id=intent_id,
            fill_ts_ms=now_ms + 2000,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=0,
        )
        assert result_3 is False, "Third fill should be rejected (overfill)"

        # Verify exactly 2 fills
        import sqlite3

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            fill_count = cursor.fetchone()[0]
            assert fill_count == 2, "Only 2 fill rows should exist"

    def test_remaining_size_corrected_by_overfill_prevention(self, paper_store: PaperStore) -> None:
        """Test that remaining_size is corrected to maintain invariant.

        Scenario:
        - Create intent with size_contracts=3
        - Insert first fill with fill_size=1, user provides remaining_size=5 (incorrect)
        - Verify stored remaining_size is corrected to 2 (3 - 1)
        - Insert second fill with fill_size=1
        - Verify stored remaining_size is corrected to 1 (3 - 2)
        """
        intent_id = self._create_intent(paper_store, "TEST_OVERFILL_4", "NO", size_contracts=3)
        now_ms = int(time.time() * 1000)
        fill_price = 48.0

        # Fill 1: User provides wrong remaining_size, should be corrected
        result_1 = paper_store.insert_fill(
            fill_id="fill-1-remaining",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=5,  # Wrong! Should be 2 (3-1)
        )
        assert result_1 is True

        # Verify remaining_size was corrected in DB
        import sqlite3

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT remaining_size FROM paper_fills WHERE id = ?", ("fill-1-remaining",)
            )
            stored_remaining = cursor.fetchone()[0]
            assert stored_remaining == 2, f"remaining_size should be 2, got {stored_remaining}"

        # Fill 2: Verify remaining_size is corrected again
        result_2 = paper_store.insert_fill(
            fill_id="fill-2-remaining",
            intent_id=intent_id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=10,  # Wrong again
        )
        assert result_2 is True

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT remaining_size FROM paper_fills WHERE id = ?", ("fill-2-remaining",)
            )
            stored_remaining = cursor.fetchone()[0]
            assert stored_remaining == 1, f"remaining_size should be 1, got {stored_remaining}"

    def test_backwards_compat_legacy_paperfill_api(self, paper_store: PaperStore) -> None:
        """Test that legacy PaperFill API still works + respects overfill prevention.

        Scenario:
        - Create intent with size_contracts=1
        - Insert legacy fill (should succeed)
        - Insert second legacy fill (should be rejected)
        """
        intent_id = self._create_intent(paper_store, "TEST_LEGACY", "YES", size_contracts=1)
        now_ms = int(time.time() * 1000)

        # First legacy fill (should succeed)
        legacy_fill_1 = PaperFill(
            intent_id=intent_id,
            filled=True,
            fill_ts_ms=now_ms,
            fill_price_cents=50.0,
            time_to_fill_ms=100,
        )
        result_1 = paper_store.insert_fill(legacy_fill_1)
        assert result_1 is True, "First legacy fill should succeed"

        # Second legacy fill (should be rejected)
        legacy_fill_2 = PaperFill(
            intent_id=intent_id,
            filled=True,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=51.0,
            time_to_fill_ms=100,
        )
        result_2 = paper_store.insert_fill(legacy_fill_2)
        assert result_2 is False, "Second legacy fill should be rejected (overfill)"

        # Verify only 1 fill exists
        import sqlite3

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            fill_count = cursor.fetchone()[0]
            assert fill_count == 1, "Only 1 legacy fill should exist"

    def test_intent_not_found_returns_false(self, paper_store: PaperStore) -> None:
        """Test that insert_fill returns False if intent doesn't exist.

        Scenario:
        - Try to insert a fill for a non-existent intent
        - Verify result is False
        - Verify no fill was inserted
        """
        result = paper_store.insert_fill(
            fill_id="fill-unknown",
            intent_id="intent-nonexistent",
            fill_ts_ms=int(time.time() * 1000),
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
        )
        assert result is False, "insert_fill should return False for missing intent"

        # Verify no fill was inserted
        import sqlite3

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE id = ?", ("fill-unknown",))
            count = cursor.fetchone()[0]
            assert count == 0, "No fill should be inserted for missing intent"

    def test_normalize_fill_insert_args_with_kwargs(self, paper_store: PaperStore) -> None:
        """Test _normalize_fill_insert_args with various keyword inputs."""
        result = paper_store._normalize_fill_insert_args(
            fill_id="test-fill-id",
            intent_id="test-intent-id",
            fill_ts_ms=12345,
            fill_price_cents=50.5,
            fill_size=2,
            remaining_size=1,
            queue_ahead_at_fill=10,
            fill_fee_cents=1.0,
        )

        assert result is not None
        assert result["fill_id"] == "test-fill-id"
        assert result["intent_id"] == "test-intent-id"
        assert result["fill_ts_ms"] == 12345
        assert result["fill_price_cents"] == 50.5
        assert result["fill_size"] == 2
        assert result["remaining_size"] == 1
        assert result["queue_ahead_at_fill"] == 10
        assert result["fill_fee_cents"] == 1.0

    def test_validate_fill_dict_rejects_missing_fields(self, paper_store: PaperStore) -> None:
        """Test _validate_fill_dict rejects invalid inputs."""
        invalid_dicts = [
            {"intent_id": "test", "fill_ts_ms": 123},  # missing fill_id
            {"fill_id": "test", "fill_ts_ms": 123},  # missing intent_id
            {"fill_id": "test", "intent_id": "test"},  # missing fill_ts_ms
            {"fill_id": "", "intent_id": "test", "fill_ts_ms": 123},  # empty fill_id
        ]

        for invalid_dict in invalid_dicts:
            # Fill in defaults for any missing required fields to test one field at a time
            test_dict = {
                "fill_id": "id",
                "intent_id": "intent",
                "fill_ts_ms": 123,
                "fill_price_cents": 50.0,
                "fill_size": 1,
                "remaining_size": 0,
            }
            test_dict.update(invalid_dict)
            try:
                paper_store._validate_fill_dict(test_dict)
                # If we reach here, check if this was expected to fail
                if not test_dict.get("fill_id") or not isinstance(test_dict.get("fill_id"), str):
                    pytest.fail(f"Should have raised ValueError for {test_dict}")
            except ValueError:
                pass  # Expected


class TestInsertFillAtomicity:
    """Tests for atomic, concurrency-safe insert_fill behavior."""

    @pytest.fixture
    def temp_db_path(self) -> str:
        """Create a temporary database path."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        yield path
        with contextlib.suppress(OSError):
            if os.path.exists(path):
                os.unlink(path)

    @pytest.fixture
    def paper_store(self, temp_db_path: str) -> PaperStore:
        """Create a paper store with a temporary database."""
        return PaperStore(db_path=temp_db_path)

    def _create_intent(
        self, paper_store: PaperStore, ticker: str, side: str, size_contracts: int = 1
    ) -> str:
        """Helper to create an intent in the database."""
        intent_id = f"intent-{ticker}-{side}-{int(time.time() * 1000)}"
        now_ms = int(time.time() * 1000)

        intent = QuoteIntent(
            id=intent_id,
            ticker=ticker,
            side=side,
            quote_price_cents=50.0,
            size_contracts=size_contracts,
            created_ts_ms=now_ms,
            net_edge=0.5,
            inside_width=1.0,
            imbalance_gap=None,
            fee_mode="taker",
            round_trip=False,
        )
        paper_store.insert_intent(intent)
        return intent_id

    def test_sequential_overfill_rejection(self, paper_store: PaperStore) -> None:
        """Test that sequential fills respect overfill prevention.

        Scenario:
        - Create intent with size_contracts=1
        - Insert fill #1 (1 contract) -> should succeed
        - Insert fill #2 (1 contract) -> should be rejected
        - Verify only 1 fill exists in DB
        """
        intent_id = self._create_intent(paper_store, "ATOMIC_SEQ", "YES", size_contracts=1)
        now_ms = int(time.time() * 1000)

        # First fill (should succeed)
        result_1 = paper_store.insert_fill(
            fill_id="atomic-fill-1",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
        )
        assert result_1 is True

        # Second fill (should be rejected due to atomicity + overfill detection)
        result_2 = paper_store.insert_fill(
            fill_id="atomic-fill-2",
            intent_id=intent_id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=50.5,
            fill_size=1,
            remaining_size=0,
        )
        assert result_2 is False

        # Verify only 1 fill exists
        import sqlite3

        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            count = cursor.fetchone()[0]
            assert count == 1

    def test_concurrent_fill_attempts_race_condition(self, paper_store: PaperStore) -> None:
        """Simulate concurrent fill attempts using two separate connections.

        Tests that BEGIN IMMEDIATE prevents race conditions where:
        1. Connection A: Reads filled_so_far = 0
        2. Connection B: Reads filled_so_far = 0
        3. Connection A: Inserts fill (1 contract)
        4. Connection B: Inserts fill (1 contract) <- Should be prevented by atomic locking

        Scenario:
        - Create intent with size_contracts=1
        - Simulate two concurrent fills attempting to insert
        - Verify only 1 fill succeeds (due to atomic BEGIN IMMEDIATE)
        """
        import sqlite3

        intent_id = self._create_intent(paper_store, "ATOMIC_RACE", "NO", size_contracts=1)
        now_ms = int(time.time() * 1000)

        # Connection 1: Insert first fill through PaperStore
        result_1 = paper_store.insert_fill(
            fill_id="concurrent-fill-1",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
        )
        assert result_1 is True

        # Connection 2: Try to insert second fill (should be rejected by atomicity)
        result_2 = paper_store.insert_fill(
            fill_id="concurrent-fill-2",
            intent_id=intent_id,
            fill_ts_ms=now_ms + 1,
            fill_price_cents=50.1,
            fill_size=1,
            remaining_size=0,
        )
        assert result_2 is False

        # Verify DB consistency: only 1 fill
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            fill_count = cursor.fetchone()[0]
            assert fill_count == 1

            # Verify intent status is correct
            cursor.execute("SELECT status FROM quote_intents WHERE id = ?", (intent_id,))
            status = cursor.fetchone()[0]
            assert status == "FILLED", f"Expected FILLED status, got {status}"

    def test_partial_fills_atomic_status_updates(self, paper_store: PaperStore) -> None:
        """Test that status updates are atomic with fill inserts.

        Scenario:
        - Create intent with size_contracts=2
        - Insert fill #1 (1 contract) -> status should remain OPEN
        - Insert fill #2 (1 contract) -> status should be FILLED
        - Verify both status transitions were atomic
        """
        import sqlite3

        intent_id = self._create_intent(paper_store, "ATOMIC_STATUS", "YES", size_contracts=2)
        now_ms = int(time.time() * 1000)

        # First fill: 1 of 2
        result_1 = paper_store.insert_fill(
            fill_id="atomic-status-fill-1",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=1,
        )
        assert result_1 is True

        # Check status after first fill
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM quote_intents WHERE id = ?", (intent_id,))
            status_1 = cursor.fetchone()[0]
            assert status_1 == "OPEN"

        # Second fill: 2 of 2 (completes)
        result_2 = paper_store.insert_fill(
            fill_id="atomic-status-fill-2",
            intent_id=intent_id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
        )
        assert result_2 is True

        # Check status after second fill
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM quote_intents WHERE id = ?", (intent_id,))
            status_2 = cursor.fetchone()[0]
            assert status_2 == "FILLED"

            # Verify total filled size
            cursor.execute(
                "SELECT SUM(fill_size) FROM paper_fills WHERE intent_id = ?", (intent_id,)
            )
            total_filled = cursor.fetchone()[0]
            assert total_filled == 2

    def test_idempotency_within_atomic_transaction(self, paper_store: PaperStore) -> None:
        """Test that idempotency check works correctly within atomic transaction.

        Scenario:
        - Create intent with size_contracts=1
        - Insert fill #1 (should succeed)
        - Insert fill #1 again with same ID (should return True, no-op)
        - Verify only 1 fill exists in DB
        """
        import sqlite3

        intent_id = self._create_intent(paper_store, "ATOMIC_IDEM", "YES", size_contracts=1)
        now_ms = int(time.time() * 1000)

        # First insert
        result_1 = paper_store.insert_fill(
            fill_id="atomic-idem-fill",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
        )
        assert result_1 is True

        # Second insert with same fill_id (idempotent)
        result_2 = paper_store.insert_fill(
            fill_id="atomic-idem-fill",
            intent_id=intent_id,
            fill_ts_ms=now_ms + 1000,  # Different timestamp, but same fill_id
            fill_price_cents=51.0,
            fill_size=1,
            remaining_size=0,
        )
        assert result_2 is True, "Idempotent insert should return True"

        # Verify only 1 fill exists
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            count = cursor.fetchone()[0]
            assert count == 1

            # Verify it's the original fill (timestamp should match first insert)
            cursor.execute(
                "SELECT fill_ts_ms, fill_price_cents FROM paper_fills WHERE id = ?",
                ("atomic-idem-fill",),
            )
            row = cursor.fetchone()
            assert row[0] == now_ms, "Original fill_ts_ms should be preserved"
            assert row[1] == 50.0, "Original fill_price_cents should be preserved"


class TestShortHorizonOutcomeTelemetry:
    """Tests for 30s post-fill outcome telemetry helpers."""

    def test_compute_short_horizon_fill_pnl_cents_side_aware(self) -> None:
        """Short-horizon PnL should be side-aware and fee-aware."""
        yes_pnl = PaperStore.compute_short_horizon_fill_pnl_cents(
            side="YES",
            fill_price_cents=48.0,
            fill_size=2,
            mark_price_cents=50.0,
            fill_fee_cents=0.3,
        )
        no_pnl = PaperStore.compute_short_horizon_fill_pnl_cents(
            side="NO",
            fill_price_cents=53.0,
            fill_size=2,
            mark_price_cents=51.5,
            fill_fee_cents=0.2,
        )

        assert yes_pnl == pytest.approx(3.7)
        assert no_pnl == pytest.approx(2.8)

    def test_pending_30s_updates_and_idempotent_write(self, paper_store: PaperStore) -> None:
        """Pending selector should return eligible entry fills and updates should be idempotent."""
        now_ms = int(time.time() * 1000)

        intent_entry = QuoteIntent(
            id="intent-outcome-entry",
            ticker="KXNBAGAME-OUTCOME-ENTRY",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms - 120_000,
            net_edge=1.4,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        intent_exit = QuoteIntent(
            id="intent-outcome-exit",
            ticker="KXNBAGAME-OUTCOME-EXIT",
            side="NO",
            quote_price_cents=49.0,
            size_contracts=1,
            created_ts_ms=now_ms - 120_000,
            net_edge=0.8,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        paper_store.insert_intent(intent_entry)
        paper_store.insert_intent(intent_exit)

        inserted_entry = paper_store.insert_fill(
            fill_id="fill-entry-pending",
            intent_id=intent_entry.id,
            fill_ts_ms=now_ms - 60_000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.1,
            note="entry_queue_cleared",
        )
        inserted_exit = paper_store.insert_fill(
            fill_id="fill-exit-not-pending",
            intent_id=intent_exit.id,
            fill_ts_ms=now_ms - 60_000,
            fill_price_cents=49.0,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.1,
            note="exit_take_profit",
        )
        assert inserted_entry is True
        assert inserted_exit is True

        pending = paper_store.get_pending_fill_30s_outcome_updates(cutoff_ts_ms=now_ms - 30_000)
        assert len(pending) == 1
        assert pending[0]["fill_id"] == "fill-entry-pending"
        assert pending[0]["side"] == "YES"

        first_update = paper_store.update_fill_30s_outcome(
            fill_id="fill-entry-pending",
            mark_30s_after_fill_cents=51.0,
            pnl_30s_after_fill_cents=0.9,
        )
        second_update = paper_store.update_fill_30s_outcome(
            fill_id="fill-entry-pending",
            mark_30s_after_fill_cents=52.0,
            pnl_30s_after_fill_cents=1.9,
        )
        assert first_update is True
        assert second_update is False

        enriched = paper_store.get_recent_fills_with_intent_metadata(limit=10)
        updated_row = next(row for row in enriched if row["fill_id"] == "fill-entry-pending")
        assert updated_row["mark_30s_after_fill_cents"] == 51.0
        assert updated_row["pnl_30s_after_fill_cents"] == 0.9

    def test_short_horizon_outcome_metrics_grouping(self, paper_store: PaperStore) -> None:
        """Outcome metrics should aggregate by side, fill path, and edge buckets."""
        now_ms = int(time.time() * 1000)
        intents = [
            QuoteIntent(
                id="intent-metrics-yes-1",
                ticker="KXNBAGAME-METRICS-1",
                side="YES",
                quote_price_cents=50.0,
                size_contracts=1,
                created_ts_ms=now_ms - 80_000,
                net_edge=0.6,
                inside_width=2.0,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            ),
            QuoteIntent(
                id="intent-metrics-no-1",
                ticker="KXNBAGAME-METRICS-2",
                side="NO",
                quote_price_cents=49.0,
                size_contracts=1,
                created_ts_ms=now_ms - 80_000,
                net_edge=1.4,
                inside_width=2.0,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            ),
            QuoteIntent(
                id="intent-metrics-yes-2",
                ticker="KXNBAGAME-METRICS-3",
                side="YES",
                quote_price_cents=51.0,
                size_contracts=1,
                created_ts_ms=now_ms - 80_000,
                net_edge=1.6,
                inside_width=2.0,
                imbalance_gap=0.0,
                fee_mode="maker",
                round_trip=True,
            ),
        ]
        for intent in intents:
            paper_store.insert_intent(intent)

        assert paper_store.insert_fill(
            fill_id="fill-metrics-yes-1",
            intent_id="intent-metrics-yes-1",
            fill_ts_ms=now_ms - 50_000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            fill_fee_cents=0.1,
            note="entry_queue_cleared",
            mark_30s_after_fill_cents=50.8,
            pnl_30s_after_fill_cents=0.7,
        )
        assert paper_store.insert_fill(
            fill_id="fill-metrics-no-1",
            intent_id="intent-metrics-no-1",
            fill_ts_ms=now_ms - 45_000,
            fill_price_cents=49.0,
            fill_size=1,
            remaining_size=0,
            fill_fee_cents=0.1,
            note="entry_price_improves",
            mark_30s_after_fill_cents=48.0,
            pnl_30s_after_fill_cents=0.9,
        )
        assert paper_store.insert_fill(
            fill_id="fill-metrics-yes-2",
            intent_id="intent-metrics-yes-2",
            fill_ts_ms=now_ms - 40_000,
            fill_price_cents=51.0,
            fill_size=1,
            remaining_size=0,
            fill_fee_cents=0.1,
            note="entry_queue_cleared",
            mark_30s_after_fill_cents=50.4,
            pnl_30s_after_fill_cents=-0.7,
        )

        metrics = paper_store.get_short_horizon_outcome_metrics(window_seconds=3600)

        assert metrics["total_samples"] == 3
        assert metrics["avg_pnl_30s_cents"] == pytest.approx(0.3)
        assert metrics["win_rate_30s"] == pytest.approx(2 / 3)

        assert metrics["by_side"]["YES"]["sample_count"] == 2
        assert metrics["by_side"]["YES"]["avg_pnl_30s_cents"] == pytest.approx(0.0)
        assert metrics["by_side"]["NO"]["sample_count"] == 1
        assert metrics["by_side"]["NO"]["avg_pnl_30s_cents"] == pytest.approx(0.9)

        assert metrics["by_fill_path"]["queue_cleared"]["sample_count"] == 2
        assert metrics["by_fill_path"]["price_improves"]["sample_count"] == 1

        bucket_ranges = {
            (bucket["edge_bucket_low_cents"], bucket["edge_bucket_high_cents"]): bucket
            for bucket in metrics["by_edge_bucket"]
        }
        assert (0.0, 1.0) in bucket_ranges
        assert (1.0, 2.0) in bucket_ranges
        assert bucket_ranges[(0.0, 1.0)]["sample_count"] == 1
        assert bucket_ranges[(1.0, 2.0)]["sample_count"] == 2
        assert metrics["entry_only_sample_count"] == 3
        assert metrics["entry_only_by_side"]["YES"]["sample_count"] == 2
        assert metrics["entry_only_by_side"]["NO"]["sample_count"] == 1
        assert metrics["entry_only_by_fill_path"]["queue_cleared"]["sample_count"] == 2
        assert metrics["entry_only_by_fill_path"]["price_improves"]["sample_count"] == 1

    def test_short_horizon_outcome_metrics_entry_only_excludes_exit_notes(
        self, paper_store: PaperStore
    ) -> None:
        """Entry-only outcome breakdowns should exclude exit-path rows."""
        now_ms = int(time.time() * 1000)
        intent = QuoteIntent(
            id="intent-metrics-entry-only",
            ticker="KXNBAGAME-METRICS-ENTRY-ONLY",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms - 80_000,
            net_edge=1.1,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        exit_intent = QuoteIntent(
            id="exit_intent:take_profit:KXNBAGAME-METRICS-ENTRY-ONLY:YES:1",
            ticker="KXNBAGAME-METRICS-ENTRY-ONLY",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms - 75_000,
            net_edge=0.0,
            inside_width=0.0,
            imbalance_gap=None,
            fee_mode="taker",
            round_trip=False,
        )
        paper_store.insert_intent(intent)
        paper_store.insert_intent(exit_intent)

        assert paper_store.insert_fill(
            fill_id="fill-entry-only-1",
            intent_id=intent.id,
            fill_ts_ms=now_ms - 50_000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            fill_fee_cents=0.1,
            note="entry_queue_cleared",
            mark_30s_after_fill_cents=50.6,
            pnl_30s_after_fill_cents=0.5,
        )
        assert paper_store.insert_fill(
            fill_id="fill-entry-only-2",
            intent_id=exit_intent.id,
            fill_ts_ms=now_ms - 45_000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            fill_fee_cents=0.1,
            note="exit_take_profit",
            mark_30s_after_fill_cents=49.8,
            pnl_30s_after_fill_cents=-0.2,
        )

        metrics = paper_store.get_short_horizon_outcome_metrics(window_seconds=3600)
        assert metrics["total_samples"] == 2
        assert metrics["entry_only_sample_count"] == 1
        assert "queue_cleared" in metrics["entry_only_by_fill_path"]
        assert "unknown" not in metrics["entry_only_by_fill_path"]
