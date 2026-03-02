"""SQLite persistence for paper trading."""

import logging
import sqlite3
import time
import uuid
from typing import Any, overload

from kalshi_edge_engine.engine.paper import PaperFill, PaperOutcome, QuoteIntent

logger = logging.getLogger(__name__)

# Default database path
DEFAULT_DB_PATH = "paper_trading.db"


class PaperStore:
    """SQLite-based persistence for paper trading data."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        """Initialize the store and create schema if needed.

        Args:
            db_path: Path to SQLite database file.
        """
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                cursor = conn.cursor()

                # quote_intents table - order lifecycle
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quote_intents (
                        id TEXT PRIMARY KEY,
                        ticker TEXT NOT NULL,
                        side TEXT NOT NULL,
                        quote_price_cents REAL NOT NULL,
                        size_contracts INTEGER NOT NULL,
                        created_ts_ms INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'NEW',
                        last_seen_best_bid REAL,
                        last_update_ts_ms INTEGER,
                        queue_ahead_contracts INTEGER,
                        net_edge REAL NOT NULL,
                        inside_width REAL NOT NULL,
                        imbalance_gap REAL,
                        fee_mode TEXT NOT NULL,
                        round_trip BOOLEAN NOT NULL,
                        entry_fee_cents REAL,
                        exit_ts_ms INTEGER,
                        mark_price_cents REAL,
                        take_profit_cents REAL,
                        max_hold_seconds INTEGER
                    )
                    """
                )

                # paper_fills table - individual fill events (can be partial)
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS paper_fills (
                        id TEXT PRIMARY KEY,
                        intent_id TEXT NOT NULL,
                        fill_ts_ms INTEGER NOT NULL,
                        fill_price_cents REAL NOT NULL,
                        fill_size INTEGER NOT NULL,
                        remaining_size INTEGER NOT NULL,
                        queue_ahead_at_fill INTEGER,
                        fill_fee_cents REAL,
                        note TEXT,
                        FOREIGN KEY (intent_id) REFERENCES quote_intents(id)
                    )
                    """
                )

                # paper_outcomes table - legacy, kept for compatibility
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS paper_outcomes (
                        intent_id TEXT PRIMARY KEY,
                        exit_ts_ms INTEGER NOT NULL,
                        exit_price_cents REAL,
                        total_fees_cents REAL NOT NULL,
                        pnl_cents REAL NOT NULL,
                        pnl_dollars REAL NOT NULL,
                        note TEXT NOT NULL,
                        FOREIGN KEY (intent_id) REFERENCES quote_intents(id)
                    )
                    """
                )

                # paper_positions - tracking open positions by ticker+side
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS paper_positions (
                        id TEXT PRIMARY KEY,
                        ticker TEXT NOT NULL,
                        side TEXT NOT NULL,
                        net_contracts INTEGER NOT NULL,
                        avg_entry_price_cents REAL NOT NULL,
                        realized_pnl_cents REAL DEFAULT 0.0,
                        first_open_ts_ms INTEGER NOT NULL,
                        last_update_ts_ms INTEGER NOT NULL,
                        UNIQUE(ticker, side)
                    )
                    """
                )

                # pnl_snapshots - periodic snapshot for analysis
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pnl_snapshots (
                        id TEXT PRIMARY KEY,
                        ts_ms INTEGER NOT NULL,
                        realized_pnl_cents REAL NOT NULL,
                        unrealized_pnl_cents REAL NOT NULL,
                        total_pnl_cents REAL NOT NULL,
                        open_positions_count INTEGER NOT NULL,
                        open_orders_count INTEGER NOT NULL
                    )
                    """
                )

                # Create indices for performance
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_quote_intents_ticker_status ON quote_intents(ticker, status)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_quote_intents_created_ts ON quote_intents(created_ts_ms)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_paper_fills_intent_id ON paper_fills(intent_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_paper_fills_fill_ts ON paper_fills(fill_ts_ms)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_paper_positions_ticker_side ON paper_positions(ticker, side)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pnl_snapshots_ts ON pnl_snapshots(ts_ms DESC)"
                )

                conn.commit()
                logger.info("paper_store_schema_initialized", extra={"db_path": self.db_path})
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_schema_error",
                extra={"db_path": self.db_path, "error": str(e)},
            )
            raise

    def insert_intent(
        self,
        intent: QuoteIntent,
        entry_fee_cents: float | None = None,
        take_profit_cents: float | None = None,
        max_hold_seconds: int | None = None,
    ) -> None:
        """Insert a quote intent with optional runtime parameters.

        Args:
            intent: QuoteIntent to insert.
            entry_fee_cents: Entry fee in cents (optional).
            take_profit_cents: Take profit threshold in cents (optional).
            max_hold_seconds: Max hold time in seconds (optional).
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO quote_intents
                    (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                     net_edge, inside_width, imbalance_gap, fee_mode, round_trip,
                     entry_fee_cents, take_profit_cents, max_hold_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.id,
                        intent.ticker,
                        intent.side,
                        intent.quote_price_cents,
                        intent.size_contracts,
                        intent.created_ts_ms,
                        intent.net_edge,
                        intent.inside_width,
                        intent.imbalance_gap,
                        intent.fee_mode,
                        intent.round_trip,
                        entry_fee_cents,
                        take_profit_cents,
                        max_hold_seconds,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_insert_intent_error",
                extra={"intent_id": intent.id, "error": str(e)},
            )
            raise

    def insert_outcome(self, outcome: PaperOutcome) -> None:
        """Insert a paper outcome.

        Args:
            outcome: PaperOutcome to insert.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO paper_outcomes
                    (intent_id, exit_ts_ms, exit_price_cents, total_fees_cents,
                     pnl_cents, pnl_dollars, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        outcome.intent_id,
                        outcome.exit_ts_ms,
                        outcome.exit_price_cents,
                        outcome.total_fees_cents,
                        outcome.pnl_cents,
                        outcome.pnl_dollars,
                        outcome.note,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_insert_outcome_error",
                extra={"intent_id": outcome.intent_id, "error": str(e)},
            )
            raise

    def get_intent(self, intent_id: str) -> QuoteIntent | None:
        """Retrieve a quote intent by ID.

        Args:
            intent_id: The intent ID.

        Returns:
            QuoteIntent or None if not found.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                           net_edge, inside_width, imbalance_gap, fee_mode, round_trip
                    FROM quote_intents WHERE id = ?
                    """,
                    (intent_id,),
                )
                row = cursor.fetchone()
                if row:
                    return QuoteIntent(
                        id=row[0],
                        ticker=row[1],
                        side=row[2],
                        quote_price_cents=row[3],
                        size_contracts=row[4],
                        created_ts_ms=row[5],
                        net_edge=row[6],
                        inside_width=row[7],
                        imbalance_gap=row[8],
                        fee_mode=row[9],
                        round_trip=bool(row[10]),
                    )
                return None
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_intent_error",
                extra={"intent_id": intent_id, "error": str(e)},
            )
            return None

    def get_recent_intents(self, limit: int = 100) -> list[QuoteIntent]:
        """Get recent quote intents.

        Args:
            limit: Maximum number of intents to return.

        Returns:
            List of QuoteIntent objects.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
                           net_edge, inside_width, imbalance_gap, fee_mode, round_trip
                    FROM quote_intents ORDER BY created_ts_ms DESC LIMIT ?
                    """,
                    (limit,),
                )
                intents: list[QuoteIntent] = []
                for row in cursor.fetchall():
                    intents.append(
                        QuoteIntent(
                            id=row[0],
                            ticker=row[1],
                            side=row[2],
                            quote_price_cents=row[3],
                            size_contracts=row[4],
                            created_ts_ms=row[5],
                            net_edge=row[6],
                            inside_width=row[7],
                            imbalance_gap=row[8],
                            fee_mode=row[9],
                            round_trip=bool(row[10]),
                        )
                    )
                return intents
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_recent_intents_error",
                extra={"limit": limit, "error": str(e)},
            )
            return []

    def get_outcome_stats(self, limit: int = 100) -> dict[str, Any]:
        """Get summary stats from recent outcomes.

        Args:
            limit: Number of recent outcomes to include.

        Returns:
            Dict with summary statistics.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute(
                    """
                    SELECT COUNT(*), SUM(pnl_cents), AVG(pnl_cents),
                           MIN(pnl_cents), MAX(pnl_cents), SUM(total_fees_cents)
                    FROM (
                        SELECT pnl_cents, total_fees_cents
                        FROM paper_outcomes
                        ORDER BY exit_ts_ms DESC
                        LIMIT ?
                    )
                    """,
                    (limit,),
                )

                row = cursor.fetchone()
                if row:
                    count = row[0] or 0
                    total_pnl = row[1] or 0.0
                    avg_pnl = row[2] or 0.0
                    min_pnl = row[3] or 0.0
                    max_pnl = row[4] or 0.0
                    total_fees = row[5] or 0.0

                    return {
                        "count": count,
                        "total_pnl_cents": total_pnl,
                        "avg_pnl_cents": avg_pnl,
                        "min_pnl_cents": min_pnl,
                        "max_pnl_cents": max_pnl,
                        "total_fees_cents": total_fees,
                    }

                return {
                    "count": 0,
                    "total_pnl_cents": 0.0,
                    "avg_pnl_cents": 0.0,
                    "min_pnl_cents": 0.0,
                    "max_pnl_cents": 0.0,
                    "total_fees_cents": 0.0,
                }
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_outcome_stats_error",
                extra={"limit": limit, "error": str(e)},
            )
            return {}

    def update_intent_status(
        self,
        intent_id: str,
        status: str,
        last_seen_best_bid: float | None = None,
        queue_ahead: int | None = None,
    ) -> None:
        """Update intent status and tracking fields.

        Args:
            intent_id: The intent ID.
            status: New status (NEW, OPEN, FILLED, CANCELED, EXPIRED).
            last_seen_best_bid: Latest best bid observed.
            queue_ahead: Estimated queue ahead at best level.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                now_ms = int(__import__("time").time() * 1000)
                conn.execute(
                    """
                    UPDATE quote_intents
                    SET status = ?, last_seen_best_bid = ?, queue_ahead_contracts = ?,
                        last_update_ts_ms = ?
                    WHERE id = ?
                    """,
                    (status, last_seen_best_bid, queue_ahead, now_ms, intent_id),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_update_intent_status_error",
                extra={"intent_id": intent_id, "error": str(e)},
            )

    @overload
    def insert_fill(self, fill: PaperFill) -> None:
        """Insert a paper fill using old PaperFill API (Phase 1 compatibility)."""
        ...

    @overload
    def insert_fill(
        self,
        fill_id: str,
        intent_id: str,
        fill_ts_ms: int,
        fill_price_cents: float,
        fill_size: int,
        remaining_size: int,
        queue_ahead_at_fill: int | None = None,
        fill_fee_cents: float | None = None,
    ) -> None:
        """Insert a paper fill event (Phase 2 API)."""
        ...

    def insert_fill(self, *args: Any, **kwargs: Any) -> None:
        """Insert a paper fill event. Supports both old and new API."""
        # Check if using old PaperFill API (single positional arg)
        if len(args) == 1 and isinstance(args[0], PaperFill) and len(kwargs) == 0:
            fill_obj = args[0]
            # Convert legacy PaperFill to new insert_fill parameters
            # Legacy fills are assumed to be full fills (size=1) with no queue tracking
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO paper_fills
                        (id, intent_id, fill_ts_ms, fill_price_cents, fill_size,
                         remaining_size, queue_ahead_at_fill, fill_fee_cents, note)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            fill_obj.intent_id,
                            fill_obj.fill_ts_ms,
                            fill_obj.fill_price_cents
                            if fill_obj.fill_price_cents is not None
                            else 0.0,
                            1 if fill_obj.filled else 0,
                            0 if fill_obj.filled else 1,
                            None,
                            None,
                            "legacy_fill",
                        ),
                    )
                    conn.commit()
            except sqlite3.Error as e:
                logger.exception(
                    "paper_store_insert_fill_legacy_error",
                    extra={"intent_id": fill_obj.intent_id, "error": str(e)},
                )
        # Check if using new API
        elif len(args) >= 4 and len(kwargs) == 0:
            # New positional API
            fill_id = args[0]
            intent_id = args[1]
            fill_ts_ms = args[2]
            fill_price_cents = args[3]
            fill_size = args[4] if len(args) > 4 else kwargs.get("fill_size")
            remaining_size = args[5] if len(args) > 5 else kwargs.get("remaining_size")
            queue_ahead_at_fill = args[6] if len(args) > 6 else kwargs.get("queue_ahead_at_fill")
            fill_fee_cents = args[7] if len(args) > 7 else kwargs.get("fill_fee_cents")

            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO paper_fills
                        (id, intent_id, fill_ts_ms, fill_price_cents, fill_size,
                         remaining_size, queue_ahead_at_fill, fill_fee_cents, note)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fill_id,
                            intent_id,
                            fill_ts_ms,
                            fill_price_cents,
                            fill_size,
                            remaining_size,
                            queue_ahead_at_fill,
                            fill_fee_cents,
                            "fill",
                        ),
                    )
                    conn.commit()
            except sqlite3.Error as e:
                logger.exception(
                    "paper_store_insert_fill_error",
                    extra={"fill_id": fill_id, "error": str(e)},
                )
        else:
            # keyword API
            fill_id = kwargs.get("fill_id", args[0] if len(args) > 0 else None)
            intent_id = kwargs.get("intent_id", args[1] if len(args) > 1 else None)
            fill_ts_ms = kwargs.get("fill_ts_ms", args[2] if len(args) > 2 else None)
            fill_price_cents = kwargs.get("fill_price_cents", args[3] if len(args) > 3 else None)
            fill_size = kwargs.get("fill_size", args[4] if len(args) > 4 else None)
            remaining_size = kwargs.get("remaining_size", args[5] if len(args) > 5 else None)
            queue_ahead_at_fill = kwargs.get(
                "queue_ahead_at_fill", args[6] if len(args) > 6 else None
            )
            fill_fee_cents = kwargs.get("fill_fee_cents", args[7] if len(args) > 7 else None)

            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO paper_fills
                        (id, intent_id, fill_ts_ms, fill_price_cents, fill_size,
                         remaining_size, queue_ahead_at_fill, fill_fee_cents, note)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fill_id,
                            intent_id,
                            fill_ts_ms,
                            fill_price_cents,
                            fill_size,
                            remaining_size,
                            queue_ahead_at_fill,
                            fill_fee_cents,
                            "fill",
                        ),
                    )
                    conn.commit()
            except sqlite3.Error as e:
                logger.exception(
                    "paper_store_insert_fill_error",
                    extra={"fill_id": fill_id, "error": str(e)},
                )

    def upsert_position(
        self,
        ticker: str,
        side: str,
        net_contracts: int,
        avg_entry_price_cents: float,
        realized_pnl_cents: float,
        first_open_ts_ms: int,
    ) -> None:
        """Insert or update a position."""
        try:
            pos_id = f"{ticker}_{side}_{int(time.time())}"
            now_ms = int(time.time() * 1000)

            with sqlite3.connect(self.db_path) as conn:
                # Try update first
                conn.execute(
                    """
                    UPDATE paper_positions
                    SET net_contracts = ?, avg_entry_price_cents = ?,
                        realized_pnl_cents = ?, last_update_ts_ms = ?
                    WHERE ticker = ? AND side = ?
                    """,
                    (
                        net_contracts,
                        avg_entry_price_cents,
                        realized_pnl_cents,
                        now_ms,
                        ticker,
                        side,
                    ),
                )

                if conn.total_changes == 0:
                    # No row updated, insert new
                    conn.execute(
                        """
                        INSERT INTO paper_positions
                        (id, ticker, side, net_contracts, avg_entry_price_cents,
                         realized_pnl_cents, first_open_ts_ms, last_update_ts_ms)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            pos_id,
                            ticker,
                            side,
                            net_contracts,
                            avg_entry_price_cents,
                            realized_pnl_cents,
                            first_open_ts_ms,
                            now_ms,
                        ),
                    )

                conn.commit()
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_upsert_position_error",
                extra={"ticker": ticker, "side": side, "error": str(e)},
            )

    def get_positions(self) -> list[dict[str, Any]]:
        """Get all open positions."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT ticker, side, net_contracts, avg_entry_price_cents,
                           realized_pnl_cents, first_open_ts_ms, last_update_ts_ms
                    FROM paper_positions
                    WHERE net_contracts != 0
                    """
                )
                positions = []
                for row in cursor.fetchall():
                    positions.append(
                        {
                            "ticker": row[0],
                            "side": row[1],
                            "net_contracts": row[2],
                            "avg_entry_price_cents": row[3],
                            "realized_pnl_cents": row[4],
                            "first_open_ts_ms": row[5],
                            "last_update_ts_ms": row[6],
                        }
                    )
                return positions
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_positions_error",
                extra={"error": str(e)},
            )
            return []

    def insert_pnl_snapshot(
        self,
        ts_ms: int,
        realized_pnl_cents: float,
        unrealized_pnl_cents: float,
        open_positions_count: int,
        open_orders_count: int,
    ) -> None:
        """Insert a PnL snapshot."""
        try:
            snapshot_id = str(uuid.uuid4())
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO pnl_snapshots
                    (id, ts_ms, realized_pnl_cents, unrealized_pnl_cents, total_pnl_cents,
                     open_positions_count, open_orders_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        ts_ms,
                        realized_pnl_cents,
                        unrealized_pnl_cents,
                        realized_pnl_cents + unrealized_pnl_cents,
                        open_positions_count,
                        open_orders_count,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_insert_pnl_snapshot_error",
                extra={"error": str(e)},
            )

    def get_latest_pnl_snapshot(self) -> dict[str, Any] | None:
        """Get the most recent PnL snapshot."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT ts_ms, realized_pnl_cents, unrealized_pnl_cents, total_pnl_cents,
                           open_positions_count, open_orders_count
                    FROM pnl_snapshots
                    ORDER BY ts_ms DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "ts_ms": row[0],
                        "realized_pnl_cents": row[1],
                        "unrealized_pnl_cents": row[2],
                        "total_pnl_cents": row[3],
                        "open_positions_count": row[4],
                        "open_orders_count": row[5],
                    }
                return None
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_latest_pnl_snapshot_error",
                extra={"error": str(e)},
            )
            return None

    def get_intents_by_status(self, status: str, limit: int = 50) -> list[dict[str, Any]]:
        """Get intents with a specific status."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, ticker, side, quote_price_cents, size_contracts,
                           created_ts_ms, status, last_seen_best_bid,
                           queue_ahead_contracts, last_update_ts_ms
                    FROM quote_intents
                    WHERE status = ?
                    ORDER BY last_update_ts_ms DESC
                    LIMIT ?
                    """,
                    (status, limit),
                )
                intents = []
                for row in cursor.fetchall():
                    intents.append(
                        {
                            "id": row[0],
                            "ticker": row[1],
                            "side": row[2],
                            "quote_price_cents": row[3],
                            "size_contracts": row[4],
                            "created_ts_ms": row[5],
                            "status": row[6],
                            "last_seen_best_bid": row[7],
                            "queue_ahead_contracts": row[8],
                            "last_update_ts_ms": row[9],
                        }
                    )
                return intents
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_intents_by_status_error",
                extra={"status": status, "error": str(e)},
            )
            return []

    def get_intent_record(self, intent_id: str) -> dict[str, Any] | None:
        """Get a full intent record as dict for debugging/inspection."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, ticker, side, quote_price_cents, size_contracts,
                           created_ts_ms, status, last_seen_best_bid,
                           queue_ahead_contracts, last_update_ts_ms,
                           net_edge, inside_width, imbalance_gap,
                           fee_mode, round_trip, entry_fee_cents,
                           take_profit_cents, max_hold_seconds
                    FROM quote_intents
                    WHERE id = ?
                    LIMIT 1
                    """,
                    (intent_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None

                return {
                    "id": row[0],
                    "ticker": row[1],
                    "side": row[2],
                    "quote_price_cents": row[3],
                    "size_contracts": row[4],
                    "created_ts_ms": row[5],
                    "status": row[6],
                    "last_seen_best_bid": row[7],
                    "queue_ahead_contracts": row[8],
                    "last_update_ts_ms": row[9],
                    "net_edge": row[10],
                    "inside_width": row[11],
                    "imbalance_gap": row[12],
                    "fee_mode": row[13],
                    "round_trip": bool(row[14]),
                    "entry_fee_cents": row[15],
                    "take_profit_cents": row[16],
                    "max_hold_seconds": row[17],
                }
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_intent_record_error",
                extra={"intent_id": intent_id, "error": str(e)},
            )
            return None

    def count_intents_by_status(self, status: str) -> int:
        """Count intents by lifecycle status."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM quote_intents WHERE status = ?",
                    (status,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_count_intents_by_status_error",
                extra={"status": status, "error": str(e)},
            )
            return 0

    def has_open_intent_for_ticker_side(self, ticker: str, side: str) -> bool:
        """Check whether an OPEN intent already exists for ticker+side."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT 1
                    FROM quote_intents
                    WHERE ticker = ? AND side = ? AND status = 'OPEN'
                    LIMIT 1
                    """,
                    (ticker, side),
                )
                return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_has_open_intent_error",
                extra={"ticker": ticker, "side": side, "error": str(e)},
            )
            return False

    def count_open_positions_for_ticker(self, ticker: str) -> int:
        """Count open positions for a ticker (across sides)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM paper_positions
                    WHERE ticker = ? AND net_contracts != 0
                    """,
                    (ticker,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_count_open_positions_for_ticker_error",
                extra={"ticker": ticker, "error": str(e)},
            )
            return 0

    def get_filled_size_for_intent(self, intent_id: str) -> int:
        """Get cumulative filled size for an intent."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COALESCE(SUM(fill_size), 0) FROM paper_fills WHERE intent_id = ?",
                    (intent_id,),
                )
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_filled_size_for_intent_error",
                extra={"intent_id": intent_id, "error": str(e)},
            )
            return 0

    def get_recent_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent fills across all intents."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT f.id, f.intent_id, f.fill_ts_ms, f.fill_price_cents,
                           f.fill_size, f.remaining_size, f.queue_ahead_at_fill,
                           f.fill_fee_cents, i.ticker, i.side
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    ORDER BY f.fill_ts_ms DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                fills = []
                for row in cursor.fetchall():
                    fills.append(
                        {
                            "id": row[0],
                            "intent_id": row[1],
                            "fill_ts_ms": row[2],
                            "fill_price_cents": row[3],
                            "fill_size": row[4],
                            "remaining_size": row[5],
                            "queue_ahead_at_fill": row[6],
                            "fill_fee_cents": row[7],
                            "ticker": row[8],
                            "side": row[9],
                        }
                    )
                return fills
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_recent_fills_error",
                extra={"error": str(e)},
            )
            return []
