"""Tests for PnL consistency across endpoints and fill audit trail."""

import sqlite3
import time

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import estimate_taker_fee_cents


class TestPnLEndpointConsistency:
    """Test realized PnL consistency across /paper/positions and /paper/pnl-snapshot."""

    @pytest.fixture
    def paper_store(self, tmp_path):
        """Create a temp paper store."""
        db_path = str(tmp_path / "test_pnl_consistency.db")
        return PaperStore(db_path)

    def test_realized_pnl_consistent_across_endpoints(self, paper_store: PaperStore):
        """Verify realized PnL is consistent source across positions and pnl-snapshot.

        This test validates the fix for:
        - /paper/positions endpoint realized_pnl_cents
        - /paper/pnl-snapshot endpoint realized_pnl_cents

        Both endpoints must use the same source: get_total_realized_pnl_cents()
        which includes closed positions (net_contracts=0).
        """
        now_ms = int(time.time() * 1000)
        ticker = "TEST_NBA"
        side = "YES"
        entry_price = 50.0  # cents
        exit_price = 53.0  # cents, 3 cent profit per contract
        contracts = 2

        # Create entry intent ID (UUID string)
        entry_intent_id = "intent-1-entry"
        exit_intent_id = "intent-1-exit"

        # Insert entry intent into database
        with sqlite3.connect(paper_store.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms, net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_intent_id,
                    ticker,
                    side,
                    entry_price,
                    contracts,
                    now_ms,
                    0.5,  # net_edge
                    1.0,  # inside_width
                    None,  # imbalance_gap
                    "taker",  # fee_mode
                    False,  # round_trip
                    "filled",
                ),
            )
            # Insert exit intent for the reverse (closing) order
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms, net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exit_intent_id,
                    ticker,
                    side,
                    exit_price,
                    contracts,
                    now_ms,
                    0.5,  # net_edge
                    1.0,  # inside_width
                    None,  # imbalance_gap
                    "taker",  # fee_mode
                    False,  # round_trip
                    "filled",
                ),
            )
            conn.commit()

        # Insert entry fill
        entry_fee = estimate_taker_fee_cents(entry_price, contracts)
        paper_store.insert_fill(
            fill_id="fill-entry-1",
            intent_id=entry_intent_id,
            fill_ts_ms=now_ms + 1_000,
            fill_price_cents=entry_price,
            fill_size=contracts,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=entry_fee,
            best_bid_at_fill_cents=entry_price,
            cross_at_fill_cents=entry_price,
            note="fill",
        )

        # Create position
        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=contracts,
            avg_entry_price_cents=entry_price,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms + 1_000,
        )

        # Verify open position shows in get_positions()
        positions = paper_store.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == ticker
        assert positions[0]["side"] == side
        assert positions[0]["net_contracts"] == contracts
        assert positions[0]["realized_pnl_cents"] == 0.0

        # Verify total_realized after entry (should be 0)
        total_realized_before_exit = paper_store.get_total_realized_pnl_cents()
        assert total_realized_before_exit == 0.0

        # Insert exit fill at higher price (using exit_intent_id)
        exit_fee = estimate_taker_fee_cents(exit_price, contracts)
        paper_store.insert_fill(
            fill_id="fill-exit-1",
            intent_id=exit_intent_id,
            fill_ts_ms=now_ms + 2_000,
            fill_price_cents=exit_price,
            fill_size=contracts,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=exit_fee,
            best_bid_at_fill_cents=exit_price,
            cross_at_fill_cents=exit_price,
            note="exit",
        )

        # Calculate expected profit
        price_delta = exit_price - entry_price
        profit_before_fees = price_delta * contracts
        total_fees = entry_fee + exit_fee
        expected_realized = profit_before_fees - total_fees

        # Update position to closed (net_contracts=0) with realized PnL increment
        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=0,
            avg_entry_price_cents=entry_price,
            realized_pnl_cents=expected_realized,
            first_open_ts_ms=now_ms + 1_000,
        )

        # Verify get_positions() no longer shows this position (net_contracts=0 filtered)
        positions_after_close = paper_store.get_positions()
        assert len(positions_after_close) == 0, (
            "Closed positions (net_contracts=0) should not appear in get_positions()"
        )

        # Verify total_realized using authoritative source includes closed position
        total_realized_after_exit = paper_store.get_total_realized_pnl_cents()
        assert total_realized_after_exit == pytest.approx(expected_realized)

        # Verify exit fill exists with note="exit"
        with sqlite3.connect(paper_store.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, note, fill_price_cents, fill_size FROM paper_fills WHERE note = 'exit'",
            )
            exit_fills = cursor.fetchall()
            assert len(exit_fills) == 1
            assert exit_fills[0][1] == "exit"
            assert exit_fills[0][2] == exit_price
            assert exit_fills[0][3] == contracts

    def test_endpoint_consistency_positions_uses_correct_realized_source(
        self, paper_store: PaperStore
    ):
        """Test that get_paper_positions() uses total_realized_pnl_cents as source.

        This is the critical fix: /paper/positions endpoint should use
        paper_store.get_total_realized_pnl_cents() not sum of current positions.
        """
        now_ms = int(time.time() * 1000)

        # Setup: create and close a position
        ticker = "TEST_NBA2"
        side = "NO"
        entry_price = 40.0
        exit_price = 42.0
        contracts = 1

        # Insert entry and exit intents (must be separate due to overfill prevention)
        entry_intent_id = "intent-2-entry"
        exit_intent_id = "intent-2-exit"
        with sqlite3.connect(paper_store.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms, net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_intent_id,
                    ticker,
                    side,
                    entry_price,
                    contracts,
                    now_ms,
                    0.5,  # net_edge
                    1.0,  # inside_width
                    None,  # imbalance_gap
                    "taker",  # fee_mode
                    False,  # round_trip
                    "filled",
                ),
            )
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms, net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exit_intent_id,
                    ticker,
                    side,
                    exit_price,
                    contracts,
                    now_ms,
                    0.5,  # net_edge
                    1.0,  # inside_width
                    None,  # imbalance_gap
                    "taker",  # fee_mode
                    False,  # round_trip
                    "filled",
                ),
            )
            conn.commit()

        # Entry
        entry_fee = estimate_taker_fee_cents(entry_price, contracts)
        paper_store.insert_fill(
            fill_id="fill-entry-2",
            intent_id=entry_intent_id,
            fill_ts_ms=now_ms + 1_000,
            fill_price_cents=entry_price,
            fill_size=contracts,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=entry_fee,
            best_bid_at_fill_cents=entry_price,
            cross_at_fill_cents=entry_price,
        )

        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=contracts,
            avg_entry_price_cents=entry_price,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms + 1_000,
        )

        # Exit
        exit_fee = estimate_taker_fee_cents(exit_price, contracts)
        price_delta = entry_price - exit_price  # NO side: profit if price goes down
        realized_pnl = (price_delta * contracts) - entry_fee - exit_fee

        paper_store.insert_fill(
            fill_id="fill-exit-2",
            intent_id=exit_intent_id,
            fill_ts_ms=now_ms + 2_000,
            fill_price_cents=exit_price,
            fill_size=contracts,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=exit_fee,
            best_bid_at_fill_cents=exit_price,
            cross_at_fill_cents=exit_price,
            note="exit",
        )

        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=0,
            avg_entry_price_cents=entry_price,
            realized_pnl_cents=realized_pnl,
            first_open_ts_ms=now_ms + 1_000,
        )

        # TEST: get_total_realized_pnl_cents() should return the realized PnL
        total_realized = paper_store.get_total_realized_pnl_cents()
        assert total_realized == pytest.approx(realized_pnl)

        # TEST: get_positions() should NOT include closed positions
