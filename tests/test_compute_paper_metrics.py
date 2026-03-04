"""Tests for compute_paper_metrics consistency across all paper trading metrics."""

import sqlite3
import time

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import estimate_taker_fee_cents


class TestComputePaperMetrics:
    """Test compute_paper_metrics() consistency across all metrics."""

    @pytest.fixture
    def paper_store(self, tmp_path):
        """Create a temp paper store."""
        db_path = str(tmp_path / "test_metrics.db")
        return PaperStore(db_path)

    def test_compute_metrics_realized_pnl_consistency(self, paper_store: PaperStore):
        """Test that compute_paper_metrics returns correct realized_pnl_cents.

        Scenario:
        - Open and close a single position
        - Assert realized_pnl_cents matches expected value
        """
        now_ms = int(time.time() * 1000)
        ticker = "TEST"
        side = "YES"
        entry_price = 50.0
        exit_price = 55.0
        contracts = 2

        # Insert intents
        entry_intent_id = "entry-1"
        exit_intent_id = "exit-1"
        with sqlite3.connect(paper_store.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                 net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_intent_id,
                    ticker,
                    side,
                    entry_price,
                    contracts,
                    now_ms,
                    1.0,
                    0.5,
                    None,
                    "taker",
                    False,
                    "FILLED",
                ),
            )
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                 net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exit_intent_id,
                    ticker,
                    side,
                    exit_price,
                    contracts,
                    now_ms,
                    1.0,
                    0.5,
                    None,
                    "taker",
                    False,
                    "FILLED",
                ),
            )
            conn.commit()

        # Entry fill and position
        entry_fee = estimate_taker_fee_cents(entry_price, contracts)
        paper_store.insert_fill(
            fill_id="fill-entry",
            intent_id=entry_intent_id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=entry_price,
            fill_size=contracts,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=entry_fee,
        )

        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=contracts,
            avg_entry_price_cents=entry_price,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms + 1000,
        )

        # Exit fill and position
        exit_fee = estimate_taker_fee_cents(exit_price, contracts)
        price_pnl = (exit_price - entry_price) * contracts
        expected_realized = price_pnl - entry_fee - exit_fee

        paper_store.insert_fill(
            fill_id="fill-exit",
            intent_id=exit_intent_id,
            fill_ts_ms=now_ms + 2000,
            fill_price_cents=exit_price,
            fill_size=contracts,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=exit_fee,
        )

        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=0,
            avg_entry_price_cents=entry_price,
            realized_pnl_cents=expected_realized,
            first_open_ts_ms=now_ms + 1000,
        )

        # Test compute_paper_metrics
        metrics = paper_store.compute_paper_metrics()
        assert metrics["realized_pnl_cents"] == pytest.approx(expected_realized)
        assert metrics["total_pnl_cents"] == pytest.approx(expected_realized)
        assert metrics["open_positions_count"] == 0
        assert metrics["open_orders_count"] == 0

    def test_compute_metrics_open_positions_count(self, paper_store: PaperStore):
        """Test that open_positions_count matches nonzero net_contracts positions.

        Scenario:
        - Create 3 positions: 2 open, 1 closed
        - Assert open_positions_count == 2
        """
        now_ms = int(time.time() * 1000)

        # Create 3 ticker+side combinations: 2 open, 1 closed
        open_positions = [("NBA1", "YES"), ("NBA2", "NO")]
        closed_positions = [("NBA3", "YES")]

        # Insert open positions
        for ticker, side in open_positions:
            paper_store.upsert_position(
                ticker=ticker,
                side=side,
                net_contracts=1,
                avg_entry_price_cents=50.0,
                realized_pnl_cents=0.0,
                first_open_ts_ms=now_ms,
            )

        # Insert closed position
        for ticker, side in closed_positions:
            paper_store.upsert_position(
                ticker=ticker,
                side=side,
                net_contracts=0,
                avg_entry_price_cents=50.0,
                realized_pnl_cents=5.0,
                first_open_ts_ms=now_ms,
            )

        metrics = paper_store.compute_paper_metrics()
        assert metrics["open_positions_count"] == 2

    def test_compute_metrics_open_orders_count(self, paper_store: PaperStore):
        """Test that open_orders_count matches OPEN intents count.

        Scenario:
        - Create 5 intents: 3 OPEN, 2 FILLED
        - Assert open_orders_count == 3
        """
        now_ms = int(time.time() * 1000)

        # Insert 3 OPEN intents
        for i in range(3):
            with sqlite3.connect(paper_store.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO quote_intents
                    (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                     net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"open-{i}",
                        f"NBA{i}",
                        "YES",
                        50.0,
                        1,
                        now_ms,
                        1.0,
                        0.5,
                        None,
                        "taker",
                        False,
                        "OPEN",
                    ),
                )
                conn.commit()

        # Insert 2 FILLED intents
        for i in range(2):
            with sqlite3.connect(paper_store.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO quote_intents
                    (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                     net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"filled-{i}",
                        f"NBA_FILLED{i}",
                        "NO",
                        50.0,
                        1,
                        now_ms,
                        1.0,
                        0.5,
                        None,
                        "taker",
                        False,
                        "FILLED",
                    ),
                )
                conn.commit()

        metrics = paper_store.compute_paper_metrics()
        assert metrics["open_orders_count"] == 3

    def test_compute_metrics_total_equals_realized_plus_unrealized(self, paper_store: PaperStore):
        """Test that total_pnl_cents == realized_pnl_cents + unrealized_pnl_cents.

        Scenario:
        - Create open position with positive unrealized PnL
        - Provide mark prices for unrealized computation
        - Assert total = realized + unrealized
        """
        now_ms = int(time.time() * 1000)
        ticker = "NBA_TOTAL"
        side = "YES"
        entry_price = 50.0
        mark_price = 55.0  # 5 cents profit per contract
        contracts = 3

        # Create open position
        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=contracts,
            avg_entry_price_cents=entry_price,
            realized_pnl_cents=10.0,  # 10 cents from previous trades
            first_open_ts_ms=now_ms,
        )

        # Add entry fee to position via fills
        entry_fee = 2.0
        entry_intent_id = "entry-total"
        with sqlite3.connect(paper_store.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                 net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_intent_id,
                    ticker,
                    side,
                    entry_price,
                    contracts,
                    now_ms,
                    1.0,
                    0.5,
                    None,
                    "taker",
                    False,
                    "FILLED",
                ),
            )
            conn.commit()

        paper_store.insert_fill(
            fill_id="fill-fees",
            intent_id=entry_intent_id,
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=entry_price,
            fill_size=contracts,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=entry_fee,
        )

        # Build mark_price_dict
        mark_price_dict = {
            ticker: {
                side: mark_price,
            }
        }

        # Expected unrealized PnL
        # Price component: (mark - entry) * contracts = (55 - 50) * 3 = 15
        # Fee component: -2
        expected_unrealized = (mark_price - entry_price) * contracts - entry_fee
        expected_realized = 10.0
        expected_total = expected_realized + expected_unrealized

        metrics = paper_store.compute_paper_metrics(mark_price_dict)

        assert metrics["realized_pnl_cents"] == pytest.approx(expected_realized)
        assert metrics["unrealized_pnl_cents"] == pytest.approx(expected_unrealized)
        assert metrics["total_pnl_cents"] == pytest.approx(expected_total)
        # Verify the invariant
        assert metrics["total_pnl_cents"] == pytest.approx(
            metrics["realized_pnl_cents"] + metrics["unrealized_pnl_cents"]
        )

    def test_compute_metrics_without_mark_prices_returns_zero_unrealized(
        self, paper_store: PaperStore
    ):
        """Test that compute_paper_metrics returns 0 unrealized when no marks provided."""
        now_ms = int(time.time() * 1000)
        ticker = "NBA_NO_MARKS"
        side = "YES"

        # Create open position
        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=5,
            avg_entry_price_cents=50.0,
            realized_pnl_cents=25.0,
            first_open_ts_ms=now_ms,
        )

        # Call without mark prices
        metrics = paper_store.compute_paper_metrics()

        assert metrics["realized_pnl_cents"] == 25.0
        assert metrics["unrealized_pnl_cents"] == 0.0
        assert metrics["total_pnl_cents"] == 25.0

    def test_compute_metrics_multiple_positions_both_sides(self, paper_store: PaperStore):
        """Test compute_paper_metrics with multiple positions on both YES and NO sides.

        Scenario:
        - NBA1: 2 YES (open), 1 NO (closed)
        - NBA2: 1 YES (open), 2 NO (open)
        - Assert open_positions_count == 3 (2 NBA1-YES, 1 NBA2-NO, 1 NBA2-YES)
        """
        now_ms = int(time.time() * 1000)

        positions_data = [
            ("NBA1", "YES", 2, 0.0),  # open
            ("NBA1", "NO", 0, 15.0),  # closed with realized pnl
            ("NBA2", "YES", 1, 5.0),  # open
            ("NBA2", "NO", 2, 0.0),  # open
        ]

        for ticker, side, net_contracts, realized_pnl in positions_data:
            paper_store.upsert_position(
                ticker=ticker,
                side=side,
                net_contracts=net_contracts,
                avg_entry_price_cents=50.0,
                realized_pnl_cents=realized_pnl,
                first_open_ts_ms=now_ms,
            )

        metrics = paper_store.compute_paper_metrics()

        # Open positions: NBA1-YES (2), NBA2-YES (1), NBA2-NO (2) = 3 nonzero
        assert metrics["open_positions_count"] == 3
        # Total realized: 0 + 15 + 5 + 0 = 20
        assert metrics["realized_pnl_cents"] == pytest.approx(20.0)

    def test_compute_metrics_invariant_with_no_positions(self, paper_store: PaperStore):
        """Test that compute_paper_metrics returns all zeros with no data."""
        metrics = paper_store.compute_paper_metrics()

        assert metrics["realized_pnl_cents"] == 0.0
        assert metrics["unrealized_pnl_cents"] == 0.0
        assert metrics["total_pnl_cents"] == 0.0
        assert metrics["open_positions_count"] == 0
        assert metrics["open_orders_count"] == 0
        # Verify invariant
        assert metrics["total_pnl_cents"] == pytest.approx(
            metrics["realized_pnl_cents"] + metrics["unrealized_pnl_cents"]
        )

    def test_compute_metrics_unrealized_excludes_exit_fill_fees(self, paper_store: PaperStore):
        """Unrealized fee subtraction must include entry fills only, not exit fills."""
        now_ms = int(time.time() * 1000)
        ticker = "NBA_EXIT_FEE_FILTER"
        side = "YES"

        entry_intent_id = "entry-fee-filter"
        exit_intent_id = "exit-fee-filter"

        with sqlite3.connect(paper_store.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                 net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_intent_id,
                    ticker,
                    side,
                    50.0,
                    2,
                    now_ms,
                    1.0,
                    0.5,
                    None,
                    "taker",
                    False,
                    "FILLED",
                ),
            )
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                 net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exit_intent_id,
                    ticker,
                    side,
                    54.0,
                    2,
                    now_ms + 1000,
                    1.0,
                    0.5,
                    None,
                    "taker",
                    False,
                    "FILLED",
                ),
            )
            conn.commit()

        entry_fee = 1.5
        exit_fee = 4.0

        paper_store.insert_fill(
            fill_id="entry-fee-fill",
            intent_id=entry_intent_id,
            fill_ts_ms=now_ms + 2000,
            fill_price_cents=50.0,
            fill_size=2,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=entry_fee,
            note="fill",
        )
        paper_store.insert_fill(
            fill_id="exit-fee-fill",
            intent_id=exit_intent_id,
            fill_ts_ms=now_ms + 3000,
            fill_price_cents=54.0,
            fill_size=2,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=exit_fee,
            note="exit_time_stop",
        )

        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=2,
            avg_entry_price_cents=50.0,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms,
        )

        metrics = paper_store.compute_paper_metrics({ticker: {side: 55.0}})
        expected_unrealized = ((55.0 - 50.0) * 2) - entry_fee

        assert metrics["unrealized_pnl_cents"] == pytest.approx(expected_unrealized)

    def test_compute_metrics_mark_price_dict_uses_yes_no_side_keys(self, paper_store: PaperStore):
        """Unrealized PnL should map marks by exact YES/NO side keys."""
        now_ms = int(time.time() * 1000)

        paper_store.upsert_position(
            ticker="NBA_SIDE_YES",
            side="yes",
            net_contracts=2,
            avg_entry_price_cents=50.0,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms,
        )
        paper_store.upsert_position(
            ticker="NBA_SIDE_NO",
            side="No",
            net_contracts=3,
            avg_entry_price_cents=60.0,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms,
        )

        metrics = paper_store.compute_paper_metrics(
            {
                "NBA_SIDE_YES": {"YES": 55.0},
                "NBA_SIDE_NO": {"NO": 58.0},
            }
        )

        expected_yes = (55.0 - 50.0) * 2
        expected_no = (60.0 - 58.0) * 3
        assert metrics["unrealized_pnl_cents"] == pytest.approx(expected_yes + expected_no)

    def test_compute_metrics_unrealized_ignores_exit_take_profit_fees(
        self, paper_store: PaperStore
    ):
        """Fees from exit_take_profit fills should not be subtracted from unrealized PnL."""
        now_ms = int(time.time() * 1000)
        ticker = "NBA_EXIT_TP_FILTER"
        side = "YES"

        with sqlite3.connect(paper_store.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quote_intents
                (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                 net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "entry-tp-filter",
                    ticker,
                    side,
                    51.0,
                    1,
                    now_ms,
                    1.0,
                    0.5,
                    None,
                    "taker",
                    False,
                    "FILLED",
                ),
            )
            conn.commit()

        paper_store.insert_fill(
            fill_id="entry-tp-fee",
            intent_id="entry-tp-filter",
            fill_ts_ms=now_ms + 1000,
            fill_price_cents=51.0,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=1.25,
            note=None,
        )
        paper_store.insert_fill(
            fill_id="exit-tp-fee",
            intent_id="entry-tp-filter",
            fill_ts_ms=now_ms + 2000,
            fill_price_cents=55.0,
            fill_size=0,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=9.99,
            note="exit_take_profit",
        )
        paper_store.upsert_position(
            ticker=ticker,
            side=side,
            net_contracts=1,
            avg_entry_price_cents=51.0,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms,
        )

        metrics = paper_store.compute_paper_metrics({ticker: {side: 53.0}})
        assert metrics["unrealized_pnl_cents"] == pytest.approx((53.0 - 51.0) - 1.25)
