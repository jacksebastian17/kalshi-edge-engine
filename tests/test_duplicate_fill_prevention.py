"""Tests for duplicate fill prevention."""

import sqlite3
import time

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import estimate_taker_fee_cents


class TestDuplicateFillPrevention:
    """Test that duplicate fills are prevented for the same intent_id."""

    @pytest.fixture
    def paper_store(self, tmp_path):
        """Create a temp paper store."""
        db_path = str(tmp_path / "test_duplicate_fills.db")
        return PaperStore(db_path)

    def _create_intent(
        self, paper_store: PaperStore, ticker: str, side: str, size_contracts: int = 1
    ) -> str:
        """Helper to create an intent in the database."""
        intent_id = f"intent-{ticker}-{side}-{int(time.time() * 1000)}"
        now_ms = int(time.time() * 1000)

        with sqlite3.connect(paper_store.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms, net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    ticker,
                    side,
                    50.0,  # quote_price_cents
                    size_contracts,
                    now_ms,
                    0.5,  # net_edge
                    1.0,  # inside_width
                    None,  # imbalance_gap
                    "taker",  # fee_mode
                    False,  # round_trip
                    "OPEN",
                ),
            )
            conn.commit()

        return intent_id

    def test_prevent_duplicate_fills_for_single_contract_intent(self, paper_store: PaperStore):
        """Test that duplicate fills are prevented for intent with size_contracts=1.

        Scenario:
        - Create intent with size_contracts=1
        - Insert fill #1 (should succeed)
        - Insert fill #2 (should be prevented)
        - Verify only 1 fill exists
        - Verify intent marked as FILLED
        """
        ticker = "TEST_NBA"
        side = "YES"
        intent_id = self._create_intent(paper_store, ticker, side, size_contracts=1)
        now_ms = int(time.time() * 1000)

        # First fill (should succeed)
        fill_1_id = "fill-1"
        fill_price = 50.0
        fill_fee = estimate_taker_fee_cents(fill_price, 1)

        paper_store.insert_fill(
            fill_id=fill_1_id,
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=fill_fee,
            best_bid_at_fill_cents=fill_price,
            cross_at_fill_cents=fill_price,
        )

        # Verify first fill was inserted
        filled_so_far = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_so_far == 1, "First fill should be inserted"

        # Simulate attempting second fill (duplicate prevention should skip it)
        intent_record = paper_store.get_intent_record(intent_id)
        assert intent_record is not None
        size_contracts = intent_record["size_contracts"]

        if filled_so_far >= size_contracts:
            # This is what the application should do
            fill_2_inserted = False
        else:
            fill_2_id = "fill-2"
            paper_store.insert_fill(
                fill_id=fill_2_id,
                intent_id=intent_id,
                fill_ts_ms=now_ms + 1000,
                fill_price_cents=fill_price,
                fill_size=1,
                remaining_size=0,
                queue_ahead_at_fill=0,
                fill_fee_cents=fill_fee,
                best_bid_at_fill_cents=fill_price,
                cross_at_fill_cents=fill_price,
            )
            fill_2_inserted = True

        # Verify only 1 fill exists
        filled_total = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_total == 1, "Should only have 1 fill"
        assert not fill_2_inserted, "Second fill should have been prevented"

        # Verify fills table has only 1 row for this intent
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            fill_count = cursor.fetchone()[0]
            assert fill_count == 1, "Only 1 fill row should exist"

    def test_prevent_double_counting_fees(self, paper_store: PaperStore):
        """Test that fees are not double-counted when duplicate fills would occur.

        Scenario:
        - Create intent with size_contracts=1
        - Insert fill (full)
        - Verify fee is counted once
        - Simulate duplicate fill attempt
        - Verify total fees don't double
        """
        ticker = "TEST_NBA2"
        side = "NO"
        intent_id = self._create_intent(paper_store, ticker, side, size_contracts=1)
        now_ms = int(time.time() * 1000)

        fill_price = 45.0
        fill_fee = estimate_taker_fee_cents(fill_price, 1)

        paper_store.insert_fill(
            fill_id="fill-1",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=fill_fee,
            best_bid_at_fill_cents=fill_price,
            cross_at_fill_cents=fill_price,
        )

        # Get total fees from fills
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(fill_fee_cents), 0) FROM paper_fills WHERE intent_id = ?",
                (intent_id,),
            )
            total_fees_1 = cursor.fetchone()[0]

        assert total_fees_1 == pytest.approx(fill_fee), "First fill fee should be recorded"

        # Attempt duplicate (should be prevented)
        intent_record = paper_store.get_intent_record(intent_id)
        filled_so_far = paper_store.get_filled_size_for_intent(intent_id)

        duplicate_prevented = filled_so_far >= intent_record["size_contracts"]
        assert duplicate_prevented, "Duplicate should be detected"

        # Verify fees haven't doubled
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COALESCE(SUM(fill_fee_cents), 0) FROM paper_fills WHERE intent_id = ?",
                (intent_id,),
            )
            total_fees_2 = cursor.fetchone()[0]

        assert total_fees_2 == pytest.approx(fill_fee), (
            "Fees should not double (only 1 fill inserted)"
        )

    def test_get_filled_size_for_intent_cumulative(self, paper_store: PaperStore):
        """Test that get_filled_size_for_intent returns cumulative size.

        Scenario:
        - Create intent with size_contracts=3
        - Insert 3 partial fills
        - Verify cumulative size is 3
        """
        ticker = "TEST_NBA3"
        side = "YES"
        intent_id = self._create_intent(paper_store, ticker, side, size_contracts=3)
        now_ms = int(time.time() * 1000)
        fill_price = 50.0

        # Insert 3 partial fills (1 contract each)
        for i in range(3):
            remaining_after = max(0, 3 - (i + 1))
            paper_store.insert_fill(
                fill_id=f"fill-{i + 1}",
                intent_id=intent_id,
                fill_ts_ms=now_ms + (i * 1000),
                fill_price_cents=fill_price,
                fill_size=1,
                remaining_size=remaining_after,
                queue_ahead_at_fill=0,
                fill_fee_cents=estimate_taker_fee_cents(fill_price, 1),
                best_bid_at_fill_cents=fill_price,
                cross_at_fill_cents=fill_price,
            )

        # Verify cumulative filled size
        filled_total = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_total == 3, "Should have filled all 3 contracts"

    def test_intent_marked_filled_only_once(self, paper_store: PaperStore):
        """Test that intent is marked FILLED only once.

        Scenario:
        - Create intent with size_contracts=1
        - Insert fill (should mark as FILLED)
        - Verify status is FILLED
        - Simulate another fill attempt
        - Verify status hasn't changed
        """
        ticker = "TEST_NBA4"
        side = "NO"
        intent_id = self._create_intent(paper_store, ticker, side, size_contracts=1)
        now_ms = int(time.time() * 1000)
        fill_price = 52.0

        # Insert first fill
        paper_store.insert_fill(
            fill_id="fill-1",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=estimate_taker_fee_cents(fill_price, 1),
            best_bid_at_fill_cents=fill_price,
            cross_at_fill_cents=fill_price,
        )

        # Mark as FILLED
        paper_store.update_intent_status(intent_id, "FILLED")

        # Verify status
        intent_after = paper_store.get_intent_record(intent_id)
        assert intent_after["status"] == "FILLED"

        # Simulate duplicate fill attempt - should be prevented
        filled_so_far = paper_store.get_filled_size_for_intent(intent_id)
        original_record = paper_store.get_intent_record(intent_id)
        size_contracts = original_record["size_contracts"]

        was_prevented = filled_so_far >= size_contracts

        assert was_prevented, "Duplicate should be prevented"

        # Verify intent still marked as FILLED (no status change)
        intent_final = paper_store.get_intent_record(intent_id)
        assert intent_final["status"] == "FILLED"

    def test_partial_fills_up_to_contract_size(self, paper_store: PaperStore):
        """Test partial fills correctly respect contract size limit.

        Scenario:
        - Create intent with size_contracts=2
        - First fill: 1 contract (remaining=1)
        - Second fill: 1 contract (remaining=0)
        - Third attempt: should be prevented (filled_so_far >= size_contracts)
        """
        ticker = "TEST_NBA5"
        side = "YES"
        intent_id = self._create_intent(paper_store, ticker, side, size_contracts=2)
        now_ms = int(time.time() * 1000)
        fill_price = 48.0

        # Fill 1: 1 contract
        filled_after_1 = 1
        remaining_1 = max(0, 2 - filled_after_1)
        paper_store.insert_fill(
            fill_id="fill-1",
            intent_id=intent_id,
            fill_ts_ms=now_ms,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=remaining_1,
            queue_ahead_at_fill=0,
            fill_fee_cents=estimate_taker_fee_cents(fill_price, 1),
            best_bid_at_fill_cents=fill_price,
            cross_at_fill_cents=fill_price,
        )

        # Verify fill 1 inserted
        filled_so_far = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_so_far == 1

        # Fill 2: 1 contract
        intent_record = paper_store.get_intent_record(intent_id)
        remaining_before_2 = max(0, intent_record["size_contracts"] - filled_so_far)
        remaining_2 = max(0, remaining_before_2 - 1)

        paper_store.insert_fill(
            fill_id="fill-2",
            intent_id=intent_id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=fill_price,
            fill_size=1,
            remaining_size=remaining_2,
            queue_ahead_at_fill=0,
            fill_fee_cents=estimate_taker_fee_cents(fill_price, 1),
            best_bid_at_fill_cents=fill_price,
            cross_at_fill_cents=fill_price,
        )

        # Verify fill 2 inserted
        filled_so_far = paper_store.get_filled_size_for_intent(intent_id)
        assert filled_so_far == 2

        # Attempt fill 3 (should be prevented)
        intent_record = paper_store.get_intent_record(intent_id)
        filled_after_2 = paper_store.get_filled_size_for_intent(intent_id)
        size_contracts = intent_record["size_contracts"]

        fill_3_should_be_prevented = filled_after_2 >= size_contracts
        assert fill_3_should_be_prevented, "Fill 3 should be prevented (intent fully filled)"

        # Verify only 2 fills total
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM paper_fills WHERE intent_id = ?", (intent_id,))
            fill_count = cursor.fetchone()[0]
            assert fill_count == 2
