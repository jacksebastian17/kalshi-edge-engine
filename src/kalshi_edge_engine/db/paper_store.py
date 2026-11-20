"""SQLite persistence for paper trading."""

import json
import logging
import sqlite3
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kalshi_edge_engine.engine.fees import estimate_taker_fee_cents
from kalshi_edge_engine.engine.orderbook import LiquidationQuote
from kalshi_edge_engine.engine.paper import PaperFill, PaperOutcome, QuoteIntent
from kalshi_edge_engine.engine.position_exit import compute_realized_exit_pnl_cents

logger = logging.getLogger(__name__)

# Default database path
DEFAULT_DB_PATH = "paper_trading.db"

FILL_INSERTED_NEW = "inserted_new_fill"
FILL_ALREADY_EXISTS = "already_exists"
FILL_REJECTED_OVERFILL = "rejected_overfill"
FILL_REJECTED_MISSING_INTENT = "rejected_missing_intent"
FILL_REJECTED_INACTIVE_INTENT = "rejected_inactive_intent"
FILL_ERROR = "error"
UTC_DAY_MS = 86_400_000
MARK_MAX_AGE_MS = 10_000
OUTCOME_ON_TIME_MS = 2_000
OUTCOME_MAX_DELAY_MS = 15_000


@dataclass(frozen=True)
class FillInsertResult:
    """Structured result for insert_fill operations."""

    status: str

    @property
    def inserted_new_fill(self) -> bool:
        return self.status == FILL_INSERTED_NEW

    @property
    def already_exists(self) -> bool:
        return self.status == FILL_ALREADY_EXISTS

    @property
    def rejected_overfill(self) -> bool:
        return self.status == FILL_REJECTED_OVERFILL

    @property
    def rejected_missing_intent(self) -> bool:
        return self.status == FILL_REJECTED_MISSING_INTENT

    @property
    def error(self) -> bool:
        return self.status == FILL_ERROR


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
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.cursor()

                # quote_intents table - order lifecycle
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quote_intents (
                        id TEXT PRIMARY KEY,
                        ticker TEXT NOT NULL,
                        universe TEXT,
                        side TEXT NOT NULL,
                        quote_price_cents REAL NOT NULL,
                        size_contracts INTEGER NOT NULL,
                        created_ts_ms INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'OPEN',
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
                        max_hold_seconds INTEGER,
                        best_bid_at_submit_cents REAL,
                        cross_at_submit_cents REAL
                    )
                    """
                )

                # paper_fills table - individual fill events
                # Intent status: OPEN (partially filled or unfilled) -> FILLED (when remaining_size reaches 0)
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS paper_fills (
                        id TEXT PRIMARY KEY,
                        intent_id TEXT NOT NULL,
                        universe TEXT,
                        fill_ts_ms INTEGER NOT NULL,
                        fill_price_cents REAL NOT NULL,
                        fill_size INTEGER NOT NULL,
                        remaining_size INTEGER NOT NULL,
                        queue_ahead_at_fill INTEGER,
                        fill_fee_cents REAL,
                        note TEXT,
                        best_bid_at_fill_cents REAL,
                        cross_at_fill_cents REAL,
                        mark_5s_after_fill_cents REAL,
                        pnl_5s_after_fill_cents REAL,
                        mark_15s_after_fill_cents REAL,
                        pnl_15s_after_fill_cents REAL,
                        mark_30s_after_fill_cents REAL,
                        pnl_30s_after_fill_cents REAL,
                        mark_60s_after_fill_cents REAL,
                        pnl_60s_after_fill_cents REAL,
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
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS paper_daily_risk (
                        day_start_ts_ms INTEGER PRIMARY KEY,
                        breach_ts_ms INTEGER NOT NULL,
                        breach_pnl_cents REAL NOT NULL
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

                # Migrate existing tables by adding new columns if they don't exist
                # Safe to call multiple times: we PRAGMA-check columns before ALTER TABLE ADD COLUMN
                self._migrate_schema(cursor)

                conn.commit()
                logger.info("paper_store_schema_initialized", extra={"db_path": self.db_path})
                logger.info("paper_store_ready", extra={"db_path": self.db_path})
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_schema_error",
                extra={"db_path": self.db_path, "error": str(e)},
            )
            raise

    def _migrate_schema(self, cursor: sqlite3.Cursor) -> None:
        """Add new columns to existing tables if they don't exist.

        Safe to call multiple times - checks columns via PRAGMA before running ALTER TABLE.
        """
        migrations = [
            (
                "paper_positions",
                "open_entry_fees_cents",
                "ALTER TABLE paper_positions ADD COLUMN open_entry_fees_cents REAL",
            ),
            (
                "paper_positions",
                "opening_intent_id",
                "ALTER TABLE paper_positions ADD COLUMN opening_intent_id TEXT",
            ),
            (
                "paper_fills",
                "execution_source",
                "ALTER TABLE paper_fills ADD COLUMN execution_source TEXT",
            ),
            ("paper_fills", "bid_levels", "ALTER TABLE paper_fills ADD COLUMN bid_levels TEXT"),
            (
                "paper_fills",
                "realized_pnl_delta_cents",
                "ALTER TABLE paper_fills ADD COLUMN realized_pnl_delta_cents REAL",
            ),
            (
                "pnl_snapshots",
                "valuation_details",
                "ALTER TABLE pnl_snapshots ADD COLUMN valuation_details TEXT",
            ),
            # Add telemetry columns to quote_intents
            (
                "quote_intents",
                "best_bid_at_submit_cents",
                "ALTER TABLE quote_intents ADD COLUMN best_bid_at_submit_cents REAL",
            ),
            (
                "quote_intents",
                "cross_at_submit_cents",
                "ALTER TABLE quote_intents ADD COLUMN cross_at_submit_cents REAL",
            ),
            (
                "quote_intents",
                "universe",
                "ALTER TABLE quote_intents ADD COLUMN universe TEXT",
            ),
            # Add telemetry columns to paper_fills
            (
                "paper_fills",
                "universe",
                "ALTER TABLE paper_fills ADD COLUMN universe TEXT",
            ),
            (
                "paper_fills",
                "best_bid_at_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN best_bid_at_fill_cents REAL",
            ),
            (
                "paper_fills",
                "cross_at_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN cross_at_fill_cents REAL",
            ),
            (
                "paper_fills",
                "mark_5s_after_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN mark_5s_after_fill_cents REAL",
            ),
            (
                "paper_fills",
                "pnl_5s_after_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN pnl_5s_after_fill_cents REAL",
            ),
            (
                "paper_fills",
                "mark_15s_after_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN mark_15s_after_fill_cents REAL",
            ),
            (
                "paper_fills",
                "pnl_15s_after_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN pnl_15s_after_fill_cents REAL",
            ),
            (
                "paper_fills",
                "mark_30s_after_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN mark_30s_after_fill_cents REAL",
            ),
            (
                "paper_fills",
                "pnl_30s_after_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN pnl_30s_after_fill_cents REAL",
            ),
            (
                "paper_fills",
                "mark_60s_after_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN mark_60s_after_fill_cents REAL",
            ),
            (
                "paper_fills",
                "pnl_60s_after_fill_cents",
                "ALTER TABLE paper_fills ADD COLUMN pnl_60s_after_fill_cents REAL",
            ),
        ]
        migrations.extend(
            ("paper_fills", column, f"ALTER TABLE paper_fills ADD COLUMN {column} INTEGER")
            for horizon in (5, 15, 30, 60)
            for column in (self._observation_column_for_horizon(horizon),)
        )

        for table_name, column_name, alter_sql in migrations:
            try:
                # Check if column exists
                cursor.execute(f"PRAGMA table_info({table_name})")
                columns = [row[1] for row in cursor.fetchall()]
                if column_name not in columns:
                    cursor.execute(alter_sql)
                    logger.info(
                        "paper_store_migration_applied",
                        extra={"table": table_name, "column": column_name},
                    )
            except sqlite3.Error as e:
                logger.warning(
                    "paper_store_migration_failed",
                    extra={
                        "table": table_name,
                        "column": column_name,
                        "error": str(e),
                    },
                )
                raise

    def insert_intent(
        self,
        intent: QuoteIntent,
        universe: str | None = None,
        entry_fee_cents: float | None = None,
        take_profit_cents_per_contract: float | None = None,
        # Backward-compat alias: interpreted as cents PER CONTRACT.
        take_profit_cents: float | None = None,
        max_hold_seconds: int | None = None,
        best_bid_at_submit_cents: float | None = None,
        cross_at_submit_cents: float | None = None,
    ) -> None:
        """Insert a quote intent with optional runtime parameters.

        Args:
            intent: QuoteIntent to insert.
            entry_fee_cents: Entry fee in cents (optional).
            take_profit_cents_per_contract: Take-profit threshold in cents per contract.
            max_hold_seconds: Max hold time in seconds (optional).
            best_bid_at_submit_cents: Best bid at submission time (optional, for telemetry).
            cross_at_submit_cents: Cross price at submission time (optional, for telemetry).
        """
        effective_take_profit_cents_per_contract = (
            take_profit_cents_per_contract
            if take_profit_cents_per_contract is not None
            else take_profit_cents
        )
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO quote_intents
                    (id, ticker, universe, side, quote_price_cents, size_contracts, created_ts_ms,
                     status, net_edge, inside_width, imbalance_gap, fee_mode, round_trip,
                     entry_fee_cents, take_profit_cents, max_hold_seconds,
                     best_bid_at_submit_cents, cross_at_submit_cents)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.id,
                        intent.ticker,
                        universe if universe is not None else getattr(intent, "universe", None),
                        intent.side,
                        intent.quote_price_cents,
                        intent.size_contracts,
                        intent.created_ts_ms,
                        "OPEN",
                        intent.net_edge,
                        intent.inside_width,
                        intent.imbalance_gap,
                        intent.fee_mode,
                        intent.round_trip,
                        entry_fee_cents,
                        effective_take_profit_cents_per_contract,
                        max_hold_seconds,
                        best_bid_at_submit_cents,
                        cross_at_submit_cents,
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
                    SELECT id, ticker, universe, side, quote_price_cents, size_contracts, created_ts_ms,
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
                        universe=row[2] or "NBA",
                        side=row[3],
                        quote_price_cents=row[4],
                        size_contracts=row[5],
                        created_ts_ms=row[6],
                        net_edge=row[7],
                        inside_width=row[8],
                        imbalance_gap=row[9],
                        fee_mode=row[10],
                        round_trip=bool(row[11]),
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
                    SELECT id, ticker, universe, side, quote_price_cents, size_contracts, created_ts_ms,
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
                            universe=row[2] or "NBA",
                            side=row[3],
                            quote_price_cents=row[4],
                            size_contracts=row[5],
                            created_ts_ms=row[6],
                            net_edge=row[7],
                            inside_width=row[8],
                            imbalance_gap=row[9],
                            fee_mode=row[10],
                            round_trip=bool(row[11]),
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

    def get_intent_by_id(self, intent_id: str) -> QuoteIntent | None:
        """Get a single intent by ID.

        Args:
            intent_id: The intent ID.

        Returns:
            QuoteIntent object or None if not found.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, ticker, universe, side, quote_price_cents, size_contracts, created_ts_ms,
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
                        universe=row[2] or "NBA",
                        side=row[3],
                        quote_price_cents=row[4],
                        size_contracts=row[5],
                        created_ts_ms=row[6],
                        net_edge=row[7],
                        inside_width=row[8],
                        imbalance_gap=row[9],
                        fee_mode=row[10],
                        round_trip=bool(row[11]),
                    )
                return None
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_intent_by_id_error",
                extra={"intent_id": intent_id, "error": str(e)},
            )
            return None

    def update_intent_status(
        self,
        intent_id: str,
        status: str,
        last_seen_best_bid: float | None = None,
        queue_ahead: int | None = None,
        *,
        only_if_open: bool = False,
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
                now_ms = int(time.time() * 1000)
                conn.execute(
                    """
                    UPDATE quote_intents
                    SET status = ?, last_seen_best_bid = ?, queue_ahead_contracts = ?,
                        last_update_ts_ms = ?
                    WHERE id = ? AND (? = 0 OR status = 'OPEN')
                    """,
                    (status, last_seen_best_bid, queue_ahead, now_ms, intent_id, only_if_open),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_update_intent_status_error",
                extra={"intent_id": intent_id, "error": str(e)},
            )

    def _normalize_fill_insert_args(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        """Normalize fill insert arguments to a canonical dict.

        Supports three modes:
        1. Legacy: insert_fill(PaperFill) - single positional arg of type PaperFill
        2. Positional: insert_fill(fill_id, intent_id, fill_ts_ms, fill_price_cents, fill_size, ...)
        3. Keyword: insert_fill(fill_id=..., intent_id=..., ...)

        Returns a dict with canonical keys:
        {fill_id, intent_id, fill_ts_ms, fill_price_cents, fill_size, remaining_size,
         queue_ahead_at_fill, fill_fee_cents, best_bid_at_fill_cents, cross_at_fill_cents,
         mark_30s_after_fill_cents, pnl_30s_after_fill_cents, note, universe}

        Raises ValueError if required fields are missing or invalid.
        Returns None if mode cannot be determined (edge case).
        """
        # Mode 1: Legacy PaperFill API
        if len(args) == 1 and isinstance(args[0], PaperFill) and len(kwargs) == 0:
            fill_obj = args[0]
            # Generate deterministic fill_id: f"legacy:{intent_id}:{fill_ts_ms}"
            # This ensures idempotency and proper duplicate detection
            deterministic_fill_id = f"legacy:{fill_obj.intent_id}:{fill_obj.fill_ts_ms}"
            return {
                "fill_id": deterministic_fill_id,
                "intent_id": fill_obj.intent_id,
                "fill_ts_ms": fill_obj.fill_ts_ms,
                "fill_price_cents": fill_obj.fill_price_cents
                if fill_obj.fill_price_cents is not None
                else 0.0,
                "fill_size": 1 if fill_obj.filled else 0,
                "remaining_size": 0 if fill_obj.filled else 1,
                "queue_ahead_at_fill": None,
                "fill_fee_cents": None,
                "best_bid_at_fill_cents": None,
                "cross_at_fill_cents": None,
                "mark_30s_after_fill_cents": None,
                "pnl_30s_after_fill_cents": None,
                "note": "legacy_fill",
                "universe": None,
            }

        # Mode 2: Positional API (at least 4 required args: fill_id, intent_id, fill_ts_ms, fill_price_cents)
        if len(args) >= 4 and len(kwargs) == 0:
            fill_id = args[0]
            intent_id = args[1]
            fill_ts_ms = args[2]
            fill_price_cents = args[3]
            fill_size = args[4] if len(args) > 4 else 1
            remaining_size = args[5] if len(args) > 5 else 0
            queue_ahead_at_fill = args[6] if len(args) > 6 else None
            fill_fee_cents = args[7] if len(args) > 7 else None
            best_bid_at_fill_cents = args[8] if len(args) > 8 else None
            cross_at_fill_cents = args[9] if len(args) > 9 else None
            mark_30s_after_fill_cents = args[10] if len(args) > 10 else None
            pnl_30s_after_fill_cents = args[11] if len(args) > 11 else None
            note = args[12] if len(args) > 12 else None
            universe = args[13] if len(args) > 13 else None

            return {
                "fill_id": fill_id,
                "intent_id": intent_id,
                "fill_ts_ms": fill_ts_ms,
                "fill_price_cents": fill_price_cents,
                "fill_size": fill_size,
                "remaining_size": remaining_size,
                "queue_ahead_at_fill": queue_ahead_at_fill,
                "fill_fee_cents": fill_fee_cents,
                "best_bid_at_fill_cents": best_bid_at_fill_cents,
                "cross_at_fill_cents": cross_at_fill_cents,
                "mark_30s_after_fill_cents": mark_30s_after_fill_cents,
                "pnl_30s_after_fill_cents": pnl_30s_after_fill_cents,
                "note": note,
                "universe": universe,
            }

        # Mode 3: Keyword API (can mix kwargs + positional, kwargs take precedence)
        # Determine values from kwargs first, fall back to positional args
        fill_id = kwargs.get("fill_id", args[0] if len(args) > 0 else None)
        intent_id = kwargs.get("intent_id", args[1] if len(args) > 1 else None)
        fill_ts_ms = kwargs.get("fill_ts_ms", args[2] if len(args) > 2 else None)
        fill_price_cents = kwargs.get("fill_price_cents", args[3] if len(args) > 3 else None)
        fill_size = kwargs.get("fill_size", args[4] if len(args) > 4 else 1)
        remaining_size = kwargs.get("remaining_size", args[5] if len(args) > 5 else 0)
        queue_ahead_at_fill = kwargs.get("queue_ahead_at_fill", args[6] if len(args) > 6 else None)
        fill_fee_cents = kwargs.get("fill_fee_cents", args[7] if len(args) > 7 else None)
        best_bid_at_fill_cents = kwargs.get(
            "best_bid_at_fill_cents", args[8] if len(args) > 8 else None
        )
        cross_at_fill_cents = kwargs.get("cross_at_fill_cents", args[9] if len(args) > 9 else None)
        mark_30s_after_fill_cents = kwargs.get(
            "mark_30s_after_fill_cents", args[10] if len(args) > 10 else None
        )
        pnl_30s_after_fill_cents = kwargs.get(
            "pnl_30s_after_fill_cents", args[11] if len(args) > 11 else None
        )
        note = kwargs.get("note")
        universe = kwargs.get("universe", args[13] if len(args) > 13 else None)

        # We have at least one kwarg or a mix; return the canonical dict
        if (
            fill_id is not None
            or intent_id is not None
            or fill_ts_ms is not None
            or fill_price_cents is not None
        ):
            return {
                "fill_id": fill_id,
                "intent_id": intent_id,
                "fill_ts_ms": fill_ts_ms,
                "fill_price_cents": fill_price_cents,
                "fill_size": fill_size,
                "remaining_size": remaining_size,
                "queue_ahead_at_fill": queue_ahead_at_fill,
                "fill_fee_cents": fill_fee_cents,
                "best_bid_at_fill_cents": best_bid_at_fill_cents,
                "cross_at_fill_cents": cross_at_fill_cents,
                "mark_30s_after_fill_cents": mark_30s_after_fill_cents,
                "pnl_30s_after_fill_cents": pnl_30s_after_fill_cents,
                "note": note,
                "universe": universe,
            }

        return None

    def _validate_fill_dict(self, fill_dict: dict[str, Any]) -> None:
        """Validate a normalized fill dict for required fields and types.

        Raises ValueError if validation fails.
        """
        fill_id = fill_dict.get("fill_id")
        intent_id = fill_dict.get("intent_id")
        fill_ts_ms = fill_dict.get("fill_ts_ms")
        fill_price_cents = fill_dict.get("fill_price_cents")
        fill_size = fill_dict.get("fill_size")
        remaining_size = fill_dict.get("remaining_size")

        if not fill_id or not isinstance(fill_id, str):
            raise ValueError("fill_id must be a non-empty string")
        if not intent_id or not isinstance(intent_id, str):
            raise ValueError("intent_id must be a non-empty string")
        if fill_ts_ms is None or not isinstance(fill_ts_ms, int):
            raise ValueError("fill_ts_ms must be an integer")
        if fill_price_cents is None or not isinstance(fill_price_cents, (int, float)):
            raise ValueError("fill_price_cents must be a number")
        if not isinstance(fill_size, int) or fill_size < 0:
            raise ValueError("fill_size must be an integer >= 0")
        if not isinstance(remaining_size, int) or remaining_size < 0:
            raise ValueError("remaining_size must be an integer >= 0")

    def insert_fill_result(
        self,
        *args: Any,
        apply_to_position: bool = False,
        execution_source: str | None = None,
        observed_30s_ts_ms: int | None = None,
        **kwargs: Any,
    ) -> FillInsertResult:
        """Insert a paper fill event and return a structured result.

        Implements atomic concurrency control: all checks (idempotency, filled_so_far,
        overfill prevention) happen within a single BEGIN IMMEDIATE transaction to prevent
        race conditions when multiple fill events for the same intent arrive concurrently.

        Implements overfill prevention: prevents inserting a fill that would exceed
        the intent's size_contracts.

        Implements idempotency: if fill_id already exists, returns already_exists (no-op).

        Updates intent status atomically:
        - If remaining_size == 0 after fill -> set status to FILLED
        - Else -> keep status OPEN

        Args:
            *args: Positional arguments (see overloads)
            **kwargs: Keyword arguments (see overloads)

        Returns:
            FillInsertResult status:
            - inserted_new_fill
            - already_exists
            - rejected_overfill
            - rejected_missing_intent
            - error

        Raises:
            ValueError if arguments cannot be parsed or are invalid.
        """
        # Normalize and parse arguments (outside transaction, just for parsing)
        fill_dict = self._normalize_fill_insert_args(*args, **kwargs)
        if fill_dict is None:
            raise ValueError("insert_fill requires either a PaperFill object or fill parameters")

        # Validate the normalized dict (outside transaction, just validation)
        self._validate_fill_dict(fill_dict)

        fill_id = fill_dict["fill_id"]
        intent_id = fill_dict["intent_id"]
        fill_size = fill_dict["fill_size"]

        # ALL ATOMICITY CHECKS INSIDE ONE TRANSACTION WITH BEGIN IMMEDIATE
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Use BEGIN IMMEDIATE to acquire immediate write lock
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cursor = conn.cursor()

                    # Check if fill_id already exists (idempotent)
                    cursor.execute(
                        "SELECT 1 FROM paper_fills WHERE id = ? LIMIT 1",
                        (fill_id,),
                    )
                    if cursor.fetchone() is not None:
                        conn.commit()
                        logger.info(
                            "paper_store_insert_fill_idempotent",
                            extra={
                                "fill_id": fill_id,
                                "intent_id": intent_id,
                            },
                        )
                        return FillInsertResult(status=FILL_ALREADY_EXISTS)

                    # Load intent to get size_contracts
                    cursor.execute(
                        "SELECT size_contracts, universe, ticker, side, status "
                        "FROM quote_intents WHERE id = ?",
                        (intent_id,),
                    )
                    intent_row = cursor.fetchone()
                    if intent_row is None:
                        conn.rollback()
                        logger.warning(
                            "paper_store_insert_fill_intent_not_found",
                            extra={"intent_id": intent_id},
                        )
                        return FillInsertResult(status=FILL_REJECTED_MISSING_INTENT)

                    size_contracts = intent_row[0]
                    intent_universe = intent_row[1]
                    fill_universe = fill_dict.get("universe") or intent_universe

                    # Query current total filled size (inside transaction for consistency)
                    cursor.execute(
                        "SELECT COALESCE(SUM(fill_size), 0) FROM paper_fills WHERE intent_id = ?",
                        (intent_id,),
                    )
                    filled_so_far = cursor.fetchone()[0]

                    # Check if already fully filled (no-op if already at target)
                    if filled_so_far >= size_contracts:
                        conn.rollback()
                        logger.warning(
                            "paper_store_insert_fill_already_filled",
                            extra={
                                "intent_id": intent_id,
                                "filled_so_far": filled_so_far,
                                "size_contracts": size_contracts,
                                "attempted_fill_size": fill_size,
                            },
                        )
                        return FillInsertResult(status=FILL_REJECTED_OVERFILL)

                    # Check if this fill would cause overfill
                    if filled_so_far + fill_size > size_contracts:
                        conn.rollback()
                        logger.warning(
                            "paper_store_insert_fill_would_overfill",
                            extra={
                                "intent_id": intent_id,
                                "filled_so_far": filled_so_far,
                                "fill_size": fill_size,
                                "size_contracts": size_contracts,
                            },
                        )
                        return FillInsertResult(status=FILL_REJECTED_OVERFILL)

                    if apply_to_position and (
                        intent_row[4] != "OPEN"
                        or str(intent_id).startswith("exit_intent:")
                        or str(fill_dict["note"]).startswith("exit")
                    ):
                        return FillInsertResult(status=FILL_REJECTED_INACTIVE_INTENT)

                    # Compute corrected remaining_size for consistency
                    # remaining_size should be (size_contracts - (filled_so_far + fill_size))
                    corrected_remaining_size = max(0, size_contracts - (filled_so_far + fill_size))
                    fill_dict["remaining_size"] = corrected_remaining_size

                    # Determine new intent status based on corrected remaining size
                    new_status = "FILLED" if corrected_remaining_size == 0 else "OPEN"

                    # Insert the fill
                    conn.execute(
                        """
                        INSERT INTO paper_fills
                        (id, intent_id, fill_ts_ms, fill_price_cents, fill_size,
                         remaining_size, queue_ahead_at_fill, fill_fee_cents, note, universe,
                         best_bid_at_fill_cents, cross_at_fill_cents,
                         mark_30s_after_fill_cents, pnl_30s_after_fill_cents,
                         execution_source, observed_30s_ts_ms)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fill_dict["fill_id"],
                            fill_dict["intent_id"],
                            fill_dict["fill_ts_ms"],
                            fill_dict["fill_price_cents"],
                            fill_dict["fill_size"],
                            fill_dict["remaining_size"],
                            fill_dict["queue_ahead_at_fill"],
                            fill_dict["fill_fee_cents"],
                            fill_dict["note"],
                            fill_universe,
                            fill_dict["best_bid_at_fill_cents"],
                            fill_dict["cross_at_fill_cents"],
                            fill_dict["mark_30s_after_fill_cents"],
                            fill_dict["pnl_30s_after_fill_cents"],
                            execution_source,
                            observed_30s_ts_ms,
                        ),
                    )

                    # Update intent status atomically
                    now_ms = int(time.time() * 1000)
                    conn.execute(
                        """
                        UPDATE quote_intents
                        SET status = ?, last_update_ts_ms = ?
                        WHERE id = ?
                        """,
                        (new_status, now_ms, intent_id),
                    )
                    if apply_to_position:
                        self._apply_entry_fill(
                            conn, intent_row[2], intent_row[3], fill_dict, now_ms
                        )

                    # Commit the transaction
                    conn.commit()

                    logger.info(
                        "paper_store_insert_fill_success",
                        extra={
                            "fill_id": fill_dict["fill_id"],
                            "intent_id": fill_dict["intent_id"],
                            "fill_size": fill_dict["fill_size"],
                            "remaining_size": fill_dict["remaining_size"],
                            "new_intent_status": new_status,
                        },
                    )
                    return FillInsertResult(status=FILL_INSERTED_NEW)

                except sqlite3.Error as e:
                    conn.rollback()
                    logger.exception(
                        "paper_store_insert_fill_error",
                        extra={"fill_id": fill_dict["fill_id"], "error": str(e)},
                    )
                    return FillInsertResult(status=FILL_ERROR)

        except sqlite3.Error as e:
            logger.exception(
                "paper_store_insert_fill_connection_error",
                extra={"fill_id": fill_id, "error": str(e)},
            )
            return FillInsertResult(status=FILL_ERROR)

    def insert_fill(self, *args: Any, **kwargs: Any) -> bool:
        """Insert a fill with backward-compatible bool semantics.

        Returns:
            True when a new fill is inserted OR the fill already exists (idempotent replay).
            False when rejected (missing intent, overfill) or on error.
        """
        result = self.insert_fill_result(*args, **kwargs)
        return result.inserted_new_fill or result.already_exists

    def _apply_entry_fill(
        self, conn: sqlite3.Connection, ticker: str, side: str, fill: dict[str, Any], now_ms: int
    ) -> None:
        row = conn.execute(
            """
            SELECT net_contracts, first_open_ts_ms, open_entry_fees_cents
            FROM paper_positions WHERE ticker = ? AND side = ?
            """,
            (ticker, side),
        ).fetchone()
        fee = fill["fill_fee_cents"] or 0.0
        if row and row[0] > 0 and row[2] is None:
            fee += self._lifecycle_fees(conn, ticker, side, row[1], exclude_fill_id=fill["fill_id"])
        conn.execute(
            """
            INSERT INTO paper_positions
                (id, ticker, side, net_contracts, avg_entry_price_cents, realized_pnl_cents,
                 first_open_ts_ms, last_update_ts_ms, open_entry_fees_cents, opening_intent_id)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            ON CONFLICT(ticker, side) DO UPDATE SET
                avg_entry_price_cents =
                    (net_contracts * avg_entry_price_cents
                     + excluded.net_contracts * excluded.avg_entry_price_cents)
                    / (net_contracts + excluded.net_contracts),
                first_open_ts_ms = CASE WHEN net_contracts = 0 THEN excluded.first_open_ts_ms
                    ELSE first_open_ts_ms END,
                opening_intent_id = CASE WHEN net_contracts = 0 THEN excluded.opening_intent_id
                    ELSE opening_intent_id END,
                open_entry_fees_cents = CASE WHEN net_contracts = 0 THEN excluded.open_entry_fees_cents
                    ELSE COALESCE(open_entry_fees_cents, 0) + excluded.open_entry_fees_cents END,
                net_contracts = net_contracts + excluded.net_contracts,
                last_update_ts_ms = excluded.last_update_ts_ms
            """,
            (
                str(uuid.uuid4()),
                ticker,
                side,
                fill["fill_size"],
                fill["fill_price_cents"],
                fill["fill_ts_ms"],
                now_ms,
                fee,
                fill["intent_id"],
            ),
        )

    def close_position(
        self,
        position: dict[str, Any],
        quote: LiquidationQuote,
        *,
        now_ms: int,
        reason: str,
        execution_source: str = "simulated_displayed_bid_sweep",
    ) -> bool:
        """Commit the simulated sale, realized increment and lifecycle transition together."""
        ticker, side = position["ticker"], position["side"]
        size = quote.size_contracts
        if (
            size <= 0
            or quote.observed_ts_ms < position["first_open_ts_ms"]
            or not quote.is_fresh(now_ms, MARK_MAX_AGE_MS)
        ):
            return False
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT net_contracts, avg_entry_price_cents, first_open_ts_ms,
                       open_entry_fees_cents, opening_intent_id, last_update_ts_ms
                FROM paper_positions WHERE ticker = ? AND side = ?
                """,
                (ticker, side),
            ).fetchone()
            expected = (size, position["avg_entry_price_cents"], position["first_open_ts_ms"])
            if (
                row is None
                or row[:3] != expected
                or row[5] != position.get("last_update_ts_ms", row[5])
            ):
                return False
            _, entry_price, first_open_ts, entry_fees, opening_intent, _ = row
            if entry_fees is None:
                entry_fees = self._lifecycle_fees(conn, ticker, side, first_open_ts)
            realized_delta = (
                compute_realized_exit_pnl_cents(
                    side=side,
                    avg_entry_price_cents=entry_price,
                    mark_price_cents=quote.price_cents,
                    net_contracts=size,
                    exit_fee_cents=quote.exit_fee_cents,
                )
                - entry_fees
            )
            universe_row = conn.execute(
                "SELECT universe FROM quote_intents WHERE id = ?", (opening_intent,)
            ).fetchone()
            universe = universe_row[0] if universe_row else None
            event_id = str(uuid.uuid4())
            intent_id = f"exit_intent:{event_id}"
            conn.execute(
                """
                INSERT INTO quote_intents
                    (id, ticker, side, universe, quote_price_cents, size_contracts, created_ts_ms,
                     status, net_edge, inside_width, fee_mode, round_trip, last_update_ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'FILLED', 0, 0, 'taker', 0, ?)
                """,
                (intent_id, ticker, side, universe, quote.price_cents, size, now_ms, now_ms),
            )
            conn.execute(
                """
                INSERT INTO paper_fills
                    (id, intent_id, universe, fill_ts_ms, fill_price_cents, fill_size,
                     remaining_size, queue_ahead_at_fill, fill_fee_cents, note,
                     best_bid_at_fill_cents, cross_at_fill_cents, execution_source,
                     bid_levels, realized_pnl_delta_cents)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"exit:{event_id}",
                    intent_id,
                    universe,
                    now_ms,
                    quote.price_cents,
                    size,
                    quote.exit_fee_cents,
                    f"exit_{reason}",
                    quote.best_bid_cents,
                    quote.implied_ask_cents,
                    execution_source,
                    json.dumps(quote.bid_levels),
                    realized_delta,
                ),
            )
            conn.execute(
                """
                UPDATE paper_positions SET net_contracts = 0, open_entry_fees_cents = 0,
                    realized_pnl_cents = COALESCE(realized_pnl_cents, 0) + ?, last_update_ts_ms = ?
                WHERE ticker = ? AND side = ?
                """,
                (realized_delta, now_ms, ticker, side),
            )
            conn.execute(
                """
                UPDATE quote_intents SET exit_ts_ms = ?, mark_price_cents = ?
                WHERE ticker = ? AND side = ? AND exit_ts_ms IS NULL
                  AND id NOT LIKE 'exit_intent:%'
                  AND EXISTS (
                      SELECT 1 FROM paper_fills f
                      WHERE f.intent_id = quote_intents.id AND f.fill_ts_ms >= ?
                  )
                """,
                (now_ms, quote.price_cents, ticker, side, first_open_ts),
            )
            conn.execute(
                """
                UPDATE quote_intents SET status = 'CANCELED', last_update_ts_ms = ?
                WHERE ticker = ? AND side = ? AND status = 'OPEN'
                """,
                (now_ms, ticker, side),
            )
        return True

    def recover_interrupted_intents(self) -> int:
        """Expire orphaned remainders after the single paper worker restarts."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE quote_intents
                SET status = CASE WHEN (
                    SELECT COALESCE(SUM(fill_size), 0) FROM paper_fills
                    WHERE intent_id = quote_intents.id
                ) >= size_contracts THEN 'FILLED' ELSE 'CANCELED' END,
                last_update_ts_ms = ?
                WHERE status IN ('OPEN', 'NEW')
                """,
                (int(time.time() * 1000),),
            )
            return cursor.rowcount

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
            side = side.strip().upper()
            pos_id = f"{ticker}_{side}_{int(time.time())}"
            now_ms = int(time.time() * 1000)

            with sqlite3.connect(self.db_path) as conn:
                # Try update first
                conn.execute(
                    """
                    UPDATE paper_positions
                    SET net_contracts = ?, avg_entry_price_cents = ?,
                        realized_pnl_cents = CASE WHEN net_contracts = 0 AND ? > 0
                            THEN realized_pnl_cents ELSE ? END,
                        first_open_ts_ms = CASE WHEN net_contracts = 0 AND ? > 0
                            THEN ? ELSE first_open_ts_ms END,
                        open_entry_fees_cents = CASE WHEN ? = 0 THEN 0
                            WHEN net_contracts = 0 THEN NULL ELSE open_entry_fees_cents END,
                        last_update_ts_ms = ?
                    WHERE ticker = ? AND side = ?
                    """,
                    (
                        net_contracts,
                        avg_entry_price_cents,
                        net_contracts,
                        realized_pnl_cents,
                        net_contracts,
                        first_open_ts_ms,
                        net_contracts,
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
                    SELECT p.ticker, p.side, p.net_contracts, p.avg_entry_price_cents,
                           COALESCE(p.realized_pnl_cents, 0), p.first_open_ts_ms,
                           p.last_update_ts_ms, p.open_entry_fees_cents,
                           i.take_profit_cents, i.max_hold_seconds
                    FROM paper_positions p
                    LEFT JOIN quote_intents i ON i.id = p.opening_intent_id
                    WHERE p.net_contracts > 0
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
                            "open_entry_fees_cents": row[7],
                            "take_profit_cents": row[8],
                            "max_hold_seconds": row[9],
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
        unrealized_pnl_cents: float | None,
        open_positions_count: int,
        open_orders_count: int,
        *,
        valuation_details: dict[str, Any] | None = None,
    ) -> None:
        """Insert a PnL snapshot."""
        try:
            snapshot_id = str(uuid.uuid4())
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO pnl_snapshots
                    (id, ts_ms, realized_pnl_cents, unrealized_pnl_cents, total_pnl_cents,
                     open_positions_count, open_orders_count, valuation_details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        ts_ms,
                        realized_pnl_cents,
                        unrealized_pnl_cents or 0.0,
                        realized_pnl_cents + (unrealized_pnl_cents or 0.0),
                        open_positions_count,
                        open_orders_count,
                        json.dumps(valuation_details) if valuation_details is not None else None,
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
                           open_positions_count, open_orders_count, valuation_details
                    FROM pnl_snapshots
                    ORDER BY ts_ms DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if row:
                    details = (
                        json.loads(row[6])
                        if row[6]
                        else {
                            "unrealized_pnl_status": "legacy_unverified",
                        }
                    )
                    incomplete = bool(details.get("unrealized_unmarked_positions_count"))
                    return {
                        **details,
                        "ts_ms": row[0],
                        "realized_pnl_cents": row[1],
                        "unrealized_pnl_cents": None if incomplete else row[2],
                        "total_pnl_cents": None if incomplete else row[3],
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
                              universe,
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
                            "universe": row[5],
                            "created_ts_ms": row[6],
                            "status": row[7],
                            "last_seen_best_bid": row[8],
                            "queue_ahead_contracts": row[9],
                            "last_update_ts_ms": row[10],
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
                              universe,
                           created_ts_ms, status, last_seen_best_bid,
                           queue_ahead_contracts, last_update_ts_ms,
                           net_edge, inside_width, imbalance_gap,
                           fee_mode, round_trip, entry_fee_cents,
                              take_profit_cents, max_hold_seconds,
                              best_bid_at_submit_cents, cross_at_submit_cents
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
                    "universe": row[5],
                    "created_ts_ms": row[6],
                    "status": row[7],
                    "last_seen_best_bid": row[8],
                    "queue_ahead_contracts": row[9],
                    "last_update_ts_ms": row[10],
                    "net_edge": row[11],
                    "inside_width": row[12],
                    "imbalance_gap": row[13],
                    "fee_mode": row[14],
                    "round_trip": bool(row[15]),
                    "entry_fee_cents": row[16],
                    # Stored in quote_intents.take_profit_cents for backward compatibility,
                    # but semantics are cents PER CONTRACT.
                    "take_profit_cents": row[17],
                    "take_profit_cents_per_contract": row[17],
                    "max_hold_seconds": row[18],
                    "best_bid_at_submit_cents": row[19],
                    "cross_at_submit_cents": row[20],
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

    def count_intents_total(self) -> int:
        """Count all intents in quote_intents table."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM quote_intents")
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_count_intents_total_error",
                extra={"error": str(e)},
            )
            return 0

    def count_fills_total(self) -> int:
        """Count all fills in paper_fills table."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM paper_fills")
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_count_fills_total_error",
                extra={"error": str(e)},
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

    def get_latest_entry_created_ts_ms_for_ticker_side(self, ticker: str, side: str) -> int | None:
        """Return latest created_ts_ms for ENTRY intents on ticker+side."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT created_ts_ms
                    FROM quote_intents
                    WHERE ticker = ?
                      AND side = ?
                      AND id NOT LIKE 'exit_intent:%'
                    ORDER BY created_ts_ms DESC
                    LIMIT 1
                    """,
                    (ticker, side),
                )
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else None
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_latest_entry_created_ts_ms_error",
                extra={"ticker": ticker, "side": side, "error": str(e)},
            )
            return None

    def count_recent_entry_samples_for_ticker(
        self,
        *,
        ticker: str,
        within_seconds: int = 3600,
    ) -> int:
        """Count recent entry-only fill samples for a ticker in a rolling window."""
        cutoff_ms = int(time.time() * 1000) - max(0, int(within_seconds)) * 1000
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.ticker = ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    """,
                    (cutoff_ms, ticker),
                )
                return int(cursor.fetchone()[0] or 0)
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_count_recent_entry_samples_for_ticker_error",
                extra={"ticker": ticker, "within_seconds": within_seconds, "error": str(e)},
            )
            return 0

    def count_recent_entry_samples_for_ticker_side(
        self,
        *,
        ticker: str,
        side: str,
        within_seconds: int = 3600,
    ) -> int:
        """Count recent entry-only fill samples for ticker+side in a rolling window."""
        cutoff_ms = int(time.time() * 1000) - max(0, int(within_seconds)) * 1000
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.ticker = ?
                      AND i.side = ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    """,
                    (cutoff_ms, ticker, side),
                )
                return int(cursor.fetchone()[0] or 0)
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_count_recent_entry_samples_for_ticker_side_error",
                extra={
                    "ticker": ticker,
                    "side": side,
                    "within_seconds": within_seconds,
                    "error": str(e),
                },
            )
            return 0

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

    def get_total_realized_pnl_cents(self) -> float:
        """Get total realized PnL across all position rows (open and closed)."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COALESCE(SUM(realized_pnl_cents), 0.0) FROM paper_positions")
                row = cursor.fetchone()
                return float(row[0]) if row and row[0] is not None else 0.0
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_total_realized_pnl_error",
                extra={"error": str(e)},
            )
            return 0.0

    @staticmethod
    def _valuation_positions(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
        return conn.execute(
            """
            SELECT ticker, side, net_contracts, avg_entry_price_cents, realized_pnl_cents,
                   first_open_ts_ms, open_entry_fees_cents
            FROM paper_positions
            """
        ).fetchall()

    def _value_positions(
        self,
        conn: sqlite3.Connection,
        positions: list[tuple[Any, ...]],
        marks: dict[tuple[str, str], LiquidationQuote] | None,
        now_ms: int,
        supplied_prices: dict[str, dict[str, float]] | None = None,
    ) -> dict[str, Any]:
        marked = unmarked = unmarked_contracts = 0
        marked_pnl = downside = unmarked_cost = 0.0
        oldest_mark_ts_ms: int | None = None
        for ticker, side, size, entry, _, first_open, entry_fees in positions:
            if size <= 0:
                continue
            if entry_fees is None:
                entry_fees = self._lifecycle_fees(conn, ticker, side, first_open)
            quote = marks.get((ticker, side)) if marks else None
            supplied_price = supplied_prices.get(ticker, {}).get(side) if supplied_prices else None
            if (
                quote is not None
                and quote.size_contracts == size
                and quote.observed_ts_ms >= first_open
                and quote.is_fresh(now_ms, MARK_MAX_AGE_MS)
            ):
                price, exit_fee = quote.price_cents, quote.exit_fee_cents
                timestamp = quote.observed_ts_ms
                oldest_mark_ts_ms = (
                    timestamp if oldest_mark_ts_ms is None else min(oldest_mark_ts_ms, timestamp)
                )
            elif supplied_price is not None and 0 < supplied_price < 100:
                price = supplied_price
                exit_fee = estimate_taker_fee_cents(price, size)
            else:
                unmarked += 1
                unmarked_contracts += size
                unmarked_cost += entry * size + entry_fees
                continue
            marked += 1
            pnl = (
                compute_realized_exit_pnl_cents(
                    side=side,
                    avg_entry_price_cents=entry,
                    mark_price_cents=price,
                    net_contracts=size,
                    exit_fee_cents=exit_fee,
                )
                - entry_fees
            )
            marked_pnl += pnl
            downside += min(pnl, 0.0)
        if not marked and not unmarked:
            status = "no_open_positions"
        elif not marked:
            status = "marks_unavailable"
        else:
            status = "partial_marks" if unmarked else "marked"
        return {
            "open_positions_count": marked + unmarked,
            "unrealized_pnl_cents": None if unmarked else marked_pnl,
            "marked_unrealized_pnl_cents": marked_pnl if marked or not unmarked else None,
            "unrealized_marks_available": marked > 0,
            "unrealized_marked_positions_count": marked,
            "unrealized_unmarked_positions_count": unmarked,
            "unmarked_contracts": unmarked_contracts,
            "unmarked_cost_basis_cents": unmarked_cost,
            "oldest_mark_ts_ms": oldest_mark_ts_ms,
            "unrealized_pnl_status": status,
            "conservative_open_loss_cents": downside - unmarked_cost,
        }

    def evaluate_daily_loss(
        self,
        *,
        now_ms: int,
        max_loss_cents: float,
        liquidation_marks: dict[tuple[str, str], LiquidationQuote],
    ) -> dict[str, Any]:
        """UTC session realized exits plus current downside, including zero recovery for missing bids."""
        day_start = now_ms - now_ms % UTC_DAY_MS
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            realized = conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl_delta_cents), 0)
                FROM paper_fills WHERE fill_ts_ms >= ? AND fill_ts_ms <= ?
                """,
                (day_start, now_ms),
            ).fetchone()[0]
            valuation = self._value_positions(
                conn, self._valuation_positions(conn), liquidation_marks, now_ms
            )
            risk_pnl = realized + valuation["conservative_open_loss_cents"]
            if max_loss_cents > 0 and risk_pnl <= -max_loss_cents:
                conn.execute(
                    "INSERT OR IGNORE INTO paper_daily_risk VALUES (?, ?, ?)",
                    (day_start, now_ms, risk_pnl),
                )
            breach = conn.execute(
                "SELECT breach_ts_ms FROM paper_daily_risk WHERE day_start_ts_ms = ?",
                (day_start,),
            ).fetchone()
            if max_loss_cents > 0 and breach is not None:
                conn.execute(
                    """
                    UPDATE quote_intents SET status = 'CANCELED', last_update_ts_ms = ?
                    WHERE status = 'OPEN' AND id NOT LIKE 'exit_intent:%'
                    """,
                    (now_ms,),
                )
        return {
            "daily_session_start_ts_ms": day_start,
            "daily_realized_pnl_cents": realized,
            "daily_risk_pnl_cents": risk_pnl,
            "daily_loss_breached": max_loss_cents > 0 and breach is not None,
            "daily_loss_breach_ts_ms": breach[0] if breach else None,
        }

    def compute_paper_metrics(
        self,
        mark_price_dict: dict[str, dict[str, float]] | None = None,
        *,
        liquidation_marks: dict[tuple[str, str], LiquidationQuote] | None = None,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Compute metrics with fresh, whole-position liquidation marks.

        The compatibility mapping accepts caller-supplied executable prices for the
        entire position. Runtime callers supply size- and time-qualified quotes.
        """
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                conn.execute("BEGIN")
                positions = self._valuation_positions(conn)
                realized_pnl_cents = sum(float(row[4] or 0) for row in positions)
                valuation = self._value_positions(
                    conn, positions, liquidation_marks, now_ms, mark_price_dict
                )
                audited_realized = cursor.execute(
                    "SELECT COALESCE(SUM(realized_pnl_delta_cents), 0) FROM paper_fills"
                ).fetchone()[0]

                # Count OPEN intents
                cursor.execute("SELECT COUNT(*) FROM quote_intents WHERE status = 'OPEN'")
                open_orders_count = cursor.fetchone()[0] or 0

                cursor.execute("SELECT COUNT(*) FROM quote_intents")
                total_intents = int(cursor.fetchone()[0] or 0)

                cursor.execute("SELECT COUNT(*) FROM paper_fills")
                total_fills = int(cursor.fetchone()[0] or 0)

                cursor.execute("SELECT COUNT(DISTINCT intent_id) FROM paper_fills")
                intents_with_at_least_one_fill = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT i.id, i.created_ts_ms, i.size_contracts,
                           COALESCE(SUM(f.fill_size), 0) AS filled_size,
                           MIN(f.fill_ts_ms) AS first_fill_ts_ms,
                           MAX(f.fill_ts_ms) AS last_fill_ts_ms
                    FROM quote_intents i
                    LEFT JOIN paper_fills f ON f.intent_id = i.id
                    GROUP BY i.id, i.created_ts_ms, i.size_contracts
                    """
                )
                intent_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM quote_intents
                    WHERE id NOT LIKE 'exit_intent:%'
                    """
                )
                entry_intents_created = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM quote_intents
                    WHERE id NOT LIKE 'exit_intent:%'
                      AND side = 'YES'
                    """
                )
                entry_yes_intents_created = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM quote_intents
                    WHERE id NOT LIKE 'exit_intent:%'
                      AND side = 'NO'
                    """
                )
                entry_no_intents_created = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COALESCE(universe, 'UNKNOWN') AS universe, COUNT(*)
                    FROM quote_intents
                    WHERE id NOT LIKE 'exit_intent:%'
                    GROUP BY COALESCE(universe, 'UNKNOWN')
                    """
                )
                entry_intents_created_by_universe = {
                    str(row[0]).upper(): int(row[1]) for row in cursor.fetchall()
                }

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                    """
                )
                entry_fills = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                      AND i.side = 'YES'
                    """
                )
                entry_yes_fills = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                      AND i.side = 'NO'
                    """
                )
                entry_no_fills = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COALESCE(i.universe, 'UNKNOWN') AS universe, COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                    GROUP BY COALESCE(i.universe, 'UNKNOWN')
                    """
                )
                entry_fills_by_universe = {
                    str(row[0]).upper(): int(row[1]) for row in cursor.fetchall()
                }

                cursor.execute(
                    """
                    SELECT COUNT(DISTINCT f.intent_id)
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                    """
                )
                entry_intents_with_at_least_one_fill = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(DISTINCT ticker)
                    FROM quote_intents
                    WHERE id NOT LIKE 'exit_intent:%'
                    """
                )
                unique_entry_tickers_count = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(DISTINCT ticker || '|' || side)
                    FROM quote_intents
                    WHERE id NOT LIKE 'exit_intent:%'
                    """
                )
                unique_entry_ticker_side_pairs_count = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(DISTINCT i.ticker)
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    """
                )
                unique_filled_entry_tickers_count = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(DISTINCT i.ticker || '|' || i.side)
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    """
                )
                unique_filled_entry_ticker_side_pairs_count = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT AVG(i.net_edge),
                           AVG(
                               CASE
                                   WHEN i.cross_at_submit_cents IS NULL THEN NULL
                                   WHEN i.side = 'YES' THEN i.cross_at_submit_cents - i.quote_price_cents
                                   WHEN i.side = 'NO' THEN i.cross_at_submit_cents - i.quote_price_cents
                                   ELSE NULL
                               END
                           ),
                           AVG(ABS(i.quote_price_cents - 50.0))
                    FROM quote_intents i
                    WHERE i.id NOT LIKE 'exit_intent:%'
                    """
                )
                entry_avg_row = cursor.fetchone()
                entry_avg_net_edge_at_entry = (
                    float(entry_avg_row[0])
                    if entry_avg_row and entry_avg_row[0] is not None
                    else None
                )
                entry_avg_capturable_edge_to_cross_at_entry = (
                    float(entry_avg_row[1])
                    if entry_avg_row and entry_avg_row[1] is not None
                    else None
                )
                entry_avg_abs_distance_from_50_at_entry = (
                    float(entry_avg_row[2])
                    if entry_avg_row and entry_avg_row[2] is not None
                    else None
                )

                cursor.execute(
                    """
                    SELECT i.id, i.created_ts_ms, i.size_contracts,
                           COALESCE(SUM(f.fill_size), 0) AS filled_size,
                           MIN(f.fill_ts_ms) AS first_fill_ts_ms,
                           MAX(f.fill_ts_ms) AS last_fill_ts_ms
                    FROM quote_intents i
                    LEFT JOIN paper_fills f ON f.intent_id = i.id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                    GROUP BY i.id, i.created_ts_ms, i.size_contracts
                    """
                )
                entry_intent_rows = cursor.fetchall()

                fully_filled_intents = 0
                partial_fill_intents = 0
                fill_fractions: list[float] = []
                time_to_first_fill_seconds: list[float] = []
                time_to_full_fill_seconds: list[float] = []
                for (
                    _intent_id,
                    created_ts_ms,
                    size_contracts,
                    filled_size,
                    first_fill_ts_ms,
                    last_fill_ts_ms,
                ) in intent_rows:
                    intent_size = max(0, int(size_contracts or 0))
                    total_filled_for_intent = max(0, int(filled_size or 0))
                    if intent_size <= 0:
                        fill_fractions.append(0.0)
                        continue

                    fill_fraction = min(1.0, total_filled_for_intent / float(intent_size))
                    fill_fractions.append(fill_fraction)

                    has_fill = total_filled_for_intent > 0
                    is_fully_filled = total_filled_for_intent >= intent_size

                    if has_fill and first_fill_ts_ms is not None:
                        ttf_seconds = (int(first_fill_ts_ms) - int(created_ts_ms)) / 1000.0
                        time_to_first_fill_seconds.append(max(0.0, float(ttf_seconds)))

                    if has_fill and not is_fully_filled:
                        partial_fill_intents += 1

                    if is_fully_filled:
                        fully_filled_intents += 1
                        if last_fill_ts_ms is not None:
                            ttf_full_seconds = (int(last_fill_ts_ms) - int(created_ts_ms)) / 1000.0
                            time_to_full_fill_seconds.append(max(0.0, float(ttf_full_seconds)))

                entry_fully_filled_intents = 0
                entry_partial_fill_intents = 0
                entry_time_to_first_fill_seconds: list[float] = []
                entry_time_to_full_fill_seconds: list[float] = []
                for (
                    _intent_id,
                    created_ts_ms,
                    size_contracts,
                    filled_size,
                    first_fill_ts_ms,
                    last_fill_ts_ms,
                ) in entry_intent_rows:
                    intent_size = max(0, int(size_contracts or 0))
                    total_filled_for_intent = max(0, int(filled_size or 0))
                    if intent_size <= 0:
                        continue

                    has_fill = total_filled_for_intent > 0
                    is_fully_filled = total_filled_for_intent >= intent_size

                    if has_fill and first_fill_ts_ms is not None:
                        ttf_seconds = (int(first_fill_ts_ms) - int(created_ts_ms)) / 1000.0
                        entry_time_to_first_fill_seconds.append(max(0.0, float(ttf_seconds)))

                    if has_fill and not is_fully_filled:
                        entry_partial_fill_intents += 1

                    if is_fully_filled:
                        entry_fully_filled_intents += 1
                        if last_fill_ts_ms is not None:
                            ttf_full_seconds = (int(last_fill_ts_ms) - int(created_ts_ms)) / 1000.0
                            entry_time_to_full_fill_seconds.append(
                                max(0.0, float(ttf_full_seconds))
                            )

                avg_time_to_first_fill_seconds = (
                    float(sum(time_to_first_fill_seconds) / len(time_to_first_fill_seconds))
                    if time_to_first_fill_seconds
                    else None
                )
                median_time_to_first_fill_seconds = (
                    float(statistics.median(time_to_first_fill_seconds))
                    if time_to_first_fill_seconds
                    else None
                )
                avg_time_to_full_fill_seconds = (
                    float(sum(time_to_full_fill_seconds) / len(time_to_full_fill_seconds))
                    if time_to_full_fill_seconds
                    else None
                )
                median_time_to_full_fill_seconds = (
                    float(statistics.median(time_to_full_fill_seconds))
                    if time_to_full_fill_seconds
                    else None
                )

                intents_without_fill = max(0, total_intents - intents_with_at_least_one_fill)
                true_intent_fill_rate = (
                    float(intents_with_at_least_one_fill / total_intents)
                    if total_intents > 0
                    else 0.0
                )
                full_fill_rate = (
                    float(fully_filled_intents / total_intents) if total_intents > 0 else 0.0
                )
                partial_fill_rate = (
                    float(partial_fill_intents / total_intents) if total_intents > 0 else 0.0
                )
                avg_filled_fraction = (
                    float(sum(fill_fractions) / len(fill_fractions)) if fill_fractions else 0.0
                )
                raw_fills_per_intent_ratio = (
                    float(total_fills / total_intents) if total_intents > 0 else 0.0
                )
                entry_intent_fill_rate = (
                    float(entry_intents_with_at_least_one_fill / entry_intents_created)
                    if entry_intents_created > 0
                    else 0.0
                )
                entry_average_time_to_first_fill_seconds = (
                    float(
                        sum(entry_time_to_first_fill_seconds)
                        / len(entry_time_to_first_fill_seconds)
                    )
                    if entry_time_to_first_fill_seconds
                    else None
                )
                entry_median_time_to_first_fill_seconds = (
                    float(statistics.median(entry_time_to_first_fill_seconds))
                    if entry_time_to_first_fill_seconds
                    else None
                )
                entry_average_time_to_full_fill_seconds = (
                    float(
                        sum(entry_time_to_full_fill_seconds) / len(entry_time_to_full_fill_seconds)
                    )
                    if entry_time_to_full_fill_seconds
                    else None
                )
                entry_median_time_to_full_fill_seconds = (
                    float(statistics.median(entry_time_to_full_fill_seconds))
                    if entry_time_to_full_fill_seconds
                    else None
                )

                # Compute unrealized PnL if marks provided
                unrealized = valuation["unrealized_pnl_cents"]
                return {
                    **valuation,
                    "realized_pnl_cents": float(realized_pnl_cents),
                    "bid_sweep_realized_pnl_cents": audited_realized,
                    "legacy_realized_balance_cents": realized_pnl_cents - audited_realized,
                    "total_pnl_cents": (
                        realized_pnl_cents + unrealized if unrealized is not None else None
                    ),
                    "open_orders_count": int(open_orders_count),
                    "total_intents": int(total_intents),
                    "total_fills": int(total_fills),
                    "raw_fills_per_intent_ratio": float(raw_fills_per_intent_ratio),
                    "intents_with_at_least_one_fill": int(intents_with_at_least_one_fill),
                    "intents_without_fill": int(intents_without_fill),
                    "fully_filled_intents": int(fully_filled_intents),
                    "partial_fill_intents": int(partial_fill_intents),
                    "intent_fill_rate": float(true_intent_fill_rate),
                    "full_fill_rate": float(full_fill_rate),
                    "partial_fill_rate": float(partial_fill_rate),
                    "average_filled_fraction": float(avg_filled_fraction),
                    "average_time_to_first_fill_seconds": avg_time_to_first_fill_seconds,
                    "median_time_to_first_fill_seconds": median_time_to_first_fill_seconds,
                    "average_time_to_full_fill_seconds": avg_time_to_full_fill_seconds,
                    "median_time_to_full_fill_seconds": median_time_to_full_fill_seconds,
                    "entry_intents_created": int(entry_intents_created),
                    "entry_yes_intents_created": int(entry_yes_intents_created),
                    "entry_no_intents_created": int(entry_no_intents_created),
                    "entry_intents_created_by_universe": entry_intents_created_by_universe,
                    "entry_fills": int(entry_fills),
                    "entry_yes_fills": int(entry_yes_fills),
                    "entry_no_fills": int(entry_no_fills),
                    "entry_fills_by_universe": entry_fills_by_universe,
                    "entry_intents_with_at_least_one_fill": int(
                        entry_intents_with_at_least_one_fill
                    ),
                    "entry_intent_fill_rate": float(entry_intent_fill_rate),
                    "entry_fully_filled_intents": int(entry_fully_filled_intents),
                    "entry_partial_fill_intents": int(entry_partial_fill_intents),
                    "entry_average_time_to_first_fill_seconds": entry_average_time_to_first_fill_seconds,
                    "entry_median_time_to_first_fill_seconds": entry_median_time_to_first_fill_seconds,
                    "entry_average_time_to_full_fill_seconds": entry_average_time_to_full_fill_seconds,
                    "entry_median_time_to_full_fill_seconds": entry_median_time_to_full_fill_seconds,
                    "entry_avg_net_edge_at_entry": entry_avg_net_edge_at_entry,
                    "entry_avg_capturable_edge_to_cross_at_entry": entry_avg_capturable_edge_to_cross_at_entry,
                    "entry_avg_abs_distance_from_50_at_entry": entry_avg_abs_distance_from_50_at_entry,
                    "unique_entry_tickers_count": int(unique_entry_tickers_count),
                    "unique_entry_ticker_side_pairs_count": int(
                        unique_entry_ticker_side_pairs_count
                    ),
                    "unique_filled_entry_tickers_count": int(unique_filled_entry_tickers_count),
                    "unique_filled_entry_ticker_side_pairs_count": int(
                        unique_filled_entry_ticker_side_pairs_count
                    ),
                }
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_compute_metrics_error",
                extra={"error": str(e)},
            )
            raise

    def get_latest_intent_id_for_ticker_side(self, ticker: str, side: str) -> str | None:
        """Get latest intent id for a ticker+side pair."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id
                    FROM quote_intents
                    WHERE ticker = ? AND side = ?
                    ORDER BY created_ts_ms DESC
                    LIMIT 1
                    """,
                    (ticker, side),
                )
                row = cursor.fetchone()
                return str(row[0]) if row and row[0] is not None else None
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_latest_intent_id_error",
                extra={"ticker": ticker, "side": side, "error": str(e)},
            )
            return None

    @staticmethod
    def _lifecycle_fees(
        conn: sqlite3.Connection,
        ticker: str,
        side: str,
        first_open_ts_ms: int,
        *,
        exclude_fill_id: str = "",
    ) -> float:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(f.fill_fee_cents), 0.0)
            FROM paper_fills f
            JOIN quote_intents i ON f.intent_id = i.id
            WHERE i.ticker = ? AND i.side = ?
              AND i.id NOT LIKE 'exit_intent:%'
              AND COALESCE(f.note, '') NOT LIKE 'exit%'
              AND i.exit_ts_ms IS NULL
              AND f.fill_ts_ms >= ? AND f.id != ?
            """,
            (ticker, side.upper(), first_open_ts_ms, exclude_fill_id),
        ).fetchone()
        return float(row[0])

    def get_open_lifecycle_entry_fees_cents(
        self, *, ticker: str, side: str, first_open_ts_ms: int
    ) -> float:
        """Return entry fees for current inventory, including pre-migration positions."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT open_entry_fees_cents FROM paper_positions
                WHERE ticker = ? AND side = ? AND net_contracts > 0 AND first_open_ts_ms = ?
                """,
                (ticker, side.upper(), first_open_ts_ms),
            ).fetchone()
            if row is not None and row[0] is not None:
                return float(row[0])
            return self._lifecycle_fees(conn, ticker, side, first_open_ts_ms)

    def mark_exited_intents_for_ticker_side(
        self,
        ticker: str,
        side: str,
        exit_ts_ms: int,
        mark_price_cents: float,
    ) -> None:
        """Set exit metadata for filled intents on ticker+side that are not yet exited."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE quote_intents
                    SET exit_ts_ms = ?, mark_price_cents = ?
                    WHERE ticker = ?
                      AND side = ?
                      AND status = 'FILLED'
                      AND id NOT LIKE 'exit_intent:%'
                      AND exit_ts_ms IS NULL
                    """,
                    (exit_ts_ms, mark_price_cents, ticker, side),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_mark_exited_intents_error",
                extra={
                    "ticker": ticker,
                    "side": side,
                    "exit_ts_ms": exit_ts_ms,
                    "error": str(e),
                },
            )

    def fill_exists(self, fill_id: str) -> bool:
        """Check if a fill with the given fill_id already exists."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM paper_fills WHERE id = ? LIMIT 1",
                    (fill_id,),
                )
                return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_fill_exists_error",
                extra={"fill_id": fill_id, "error": str(e)},
            )
            return False

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
                           f.fill_fee_cents, f.note, f.best_bid_at_fill_cents,
                                     f.cross_at_fill_cents,
                                     f.mark_5s_after_fill_cents, f.pnl_5s_after_fill_cents,
                                     f.mark_15s_after_fill_cents, f.pnl_15s_after_fill_cents,
                                     f.mark_30s_after_fill_cents, f.pnl_30s_after_fill_cents,
                                     f.mark_60s_after_fill_cents, f.pnl_60s_after_fill_cents,
                                     i.ticker, i.side,
                                        COALESCE(f.universe, i.universe),
                                    f.execution_source, f.bid_levels, f.realized_pnl_delta_cents,
                                    f.observed_5s_ts_ms, f.observed_15s_ts_ms,
                                    f.observed_30s_ts_ms, f.observed_60s_ts_ms
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
                            "note": row[8],
                            "best_bid_at_fill_cents": row[9],
                            "cross_at_fill_cents": row[10],
                            "mark_5s_after_fill_cents": row[11],
                            "pnl_5s_after_fill_cents": row[12],
                            "mark_15s_after_fill_cents": row[13],
                            "pnl_15s_after_fill_cents": row[14],
                            "mark_30s_after_fill_cents": row[15],
                            "pnl_30s_after_fill_cents": row[16],
                            "mark_60s_after_fill_cents": row[17],
                            "pnl_60s_after_fill_cents": row[18],
                            "ticker": row[19],
                            "side": row[20],
                            "universe": row[21],
                            "execution_source": row[22] or "legacy_unverified",
                            "bid_levels": json.loads(row[23]) if row[23] else None,
                            "realized_pnl_delta_cents": row[24],
                            **{
                                f"observed_{horizon}s_ts_ms": observed
                                for horizon, observed in zip(
                                    (5, 15, 30, 60), row[25:29], strict=True
                                )
                            },
                        }
                    )
                return fills
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_recent_fills_error",
                extra={"error": str(e)},
            )
            return []

    @staticmethod
    def compute_short_horizon_fill_pnl_cents(
        *,
        side: str,
        fill_price_cents: float,
        fill_size: int,
        mark_price_cents: float,
        fill_fee_cents: float | None,
        exit_fee_cents: float | None = None,
    ) -> float:
        """Compute counterfactual net liquidation P&L for an entry fill."""
        return compute_realized_exit_pnl_cents(
            side=side,
            avg_entry_price_cents=fill_price_cents,
            mark_price_cents=mark_price_cents,
            net_contracts=fill_size,
            exit_fee_cents=(
                estimate_taker_fee_cents(mark_price_cents, fill_size)
                if exit_fee_cents is None
                else exit_fee_cents
            ),
        ) - (fill_fee_cents or 0.0)

    def get_pending_fill_30s_outcome_updates(
        self,
        cutoff_ts_ms: int,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Get fills eligible for 30s outcome update with joined intent metadata."""
        return self.get_pending_fill_outcome_updates(
            horizon_seconds=30,
            cutoff_ts_ms=cutoff_ts_ms,
            limit=limit,
        )

    @staticmethod
    def _mark_column_for_horizon(horizon_seconds: int) -> str:
        return f"mark_{int(horizon_seconds)}s_after_fill_cents"

    @staticmethod
    def _pnl_column_for_horizon(horizon_seconds: int) -> str:
        return f"pnl_{int(horizon_seconds)}s_after_fill_cents"

    @staticmethod
    def _observation_column_for_horizon(horizon_seconds: int) -> str:
        return f"observed_{int(horizon_seconds)}s_ts_ms"

    @classmethod
    def _on_time_outcome_sql(cls, horizon_seconds: int) -> str:
        observed = cls._observation_column_for_horizon(horizon_seconds)
        due = f"f.fill_ts_ms + {int(horizon_seconds) * 1000}"
        return f"f.{observed} BETWEEN {due} AND {due} + {OUTCOME_ON_TIME_MS}"

    def get_pending_fill_outcome_updates(
        self,
        *,
        horizon_seconds: int,
        cutoff_ts_ms: int,
        limit: int = 200,
        earliest_ts_ms: int = 0,
    ) -> list[dict[str, Any]]:
        """Get fills eligible for horizon outcome update with joined intent metadata."""
        mark_col = self._mark_column_for_horizon(horizon_seconds)
        pnl_col = self._pnl_column_for_horizon(horizon_seconds)
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    SELECT f.id, f.intent_id, f.fill_ts_ms, f.fill_price_cents,
                           f.fill_size, f.fill_fee_cents, f.note,
                           i.ticker, i.side, i.net_edge, i.quote_price_cents
                              , COALESCE(f.universe, i.universe)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms BETWEEN ? AND ?
                      AND f.{mark_col} IS NULL
                      AND f.{pnl_col} IS NULL
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                      AND i.id NOT LIKE 'exit_intent:%'
                    ORDER BY f.fill_ts_ms ASC
                    LIMIT ?
                    """,
                    (earliest_ts_ms, cutoff_ts_ms, limit),
                )
                rows = cursor.fetchall()
                return [
                    {
                        "fill_id": row[0],
                        "intent_id": row[1],
                        "fill_ts_ms": row[2],
                        "fill_price_cents": row[3],
                        "fill_size": row[4],
                        "fill_fee_cents": row[5],
                        "note": row[6],
                        "ticker": row[7],
                        "side": row[8],
                        "net_edge": row[9],
                        "quote_price_cents": row[10],
                        "universe": row[11],
                    }
                    for row in rows
                ]
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_pending_fill_outcome_updates_error",
                extra={
                    "horizon_seconds": horizon_seconds,
                    "cutoff_ts_ms": cutoff_ts_ms,
                    "limit": limit,
                    "error": str(e),
                },
            )
            return []

    def update_fill_30s_outcome(
        self,
        *,
        fill_id: str,
        mark_30s_after_fill_cents: float,
        pnl_30s_after_fill_cents: float,
        observed_ts_ms: int | None = None,
    ) -> bool:
        """Persist 30s follow-up mark and short-horizon PnL for a fill."""
        return self.update_fill_outcome(
            fill_id=fill_id,
            horizon_seconds=30,
            mark_after_fill_cents=mark_30s_after_fill_cents,
            pnl_after_fill_cents=pnl_30s_after_fill_cents,
            observed_ts_ms=observed_ts_ms,
        )

    def update_fill_outcome(
        self,
        *,
        fill_id: str,
        horizon_seconds: int,
        mark_after_fill_cents: float,
        pnl_after_fill_cents: float,
        observed_ts_ms: int | None = None,
    ) -> bool:
        """Persist follow-up mark and short-horizon PnL for a fill/horizon."""
        mark_col = self._mark_column_for_horizon(horizon_seconds)
        pnl_col = self._pnl_column_for_horizon(horizon_seconds)
        observed_col = self._observation_column_for_horizon(horizon_seconds)
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    UPDATE paper_fills
                    SET {mark_col} = ?,
                        {pnl_col} = ?,
                        {observed_col} = ?
                    WHERE id = ?
                      AND {mark_col} IS NULL
                      AND {pnl_col} IS NULL
                      AND (? IS NULL OR ? BETWEEN fill_ts_ms + ?
                          AND fill_ts_ms + ? + {OUTCOME_MAX_DELAY_MS})
                      AND intent_id NOT LIKE 'exit_intent:%'
                      AND COALESCE(note, '') NOT LIKE 'exit%'
                    """,
                    (
                        mark_after_fill_cents,
                        pnl_after_fill_cents,
                        observed_ts_ms,
                        fill_id,
                        observed_ts_ms,
                        observed_ts_ms,
                        horizon_seconds * 1000,
                        horizon_seconds * 1000,
                    ),
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_update_fill_outcome_error",
                extra={
                    "fill_id": fill_id,
                    "horizon_seconds": horizon_seconds,
                    "error": str(e),
                },
            )
            return False

    def get_recent_fills_with_intent_metadata(self, limit: int = 200) -> list[dict[str, Any]]:
        """Get recent fills joined with intent metadata for analysis."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT f.id, f.intent_id, f.fill_ts_ms, f.fill_price_cents,
                           f.fill_size, f.fill_fee_cents, f.note,
                              f.mark_5s_after_fill_cents, f.pnl_5s_after_fill_cents,
                              f.mark_15s_after_fill_cents, f.pnl_15s_after_fill_cents,
                              f.mark_30s_after_fill_cents, f.pnl_30s_after_fill_cents,
                              f.mark_60s_after_fill_cents, f.pnl_60s_after_fill_cents,
                              i.ticker, i.side, i.net_edge, i.quote_price_cents,
                              COALESCE(f.universe, i.universe)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    ORDER BY f.fill_ts_ms DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
                return [
                    {
                        "fill_id": row[0],
                        "intent_id": row[1],
                        "fill_ts_ms": row[2],
                        "fill_price_cents": row[3],
                        "fill_size": row[4],
                        "fill_fee_cents": row[5],
                        "note": row[6],
                        "mark_5s_after_fill_cents": row[7],
                        "pnl_5s_after_fill_cents": row[8],
                        "mark_15s_after_fill_cents": row[9],
                        "pnl_15s_after_fill_cents": row[10],
                        "mark_30s_after_fill_cents": row[11],
                        "pnl_30s_after_fill_cents": row[12],
                        "mark_60s_after_fill_cents": row[13],
                        "pnl_60s_after_fill_cents": row[14],
                        "ticker": row[15],
                        "side": row[16],
                        "net_edge": row[17],
                        "quote_price_cents": row[18],
                        "universe": row[19],
                    }
                    for row in rows
                ]
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_recent_fills_with_intent_metadata_error",
                extra={"limit": limit, "error": str(e)},
            )
            return []

    def get_short_horizon_outcome_metrics(
        self,
        window_seconds: int = 86_400,
        edge_bucket_size_cents: float = 1.0,
        horizon_seconds: int = 30,
    ) -> dict[str, Any]:
        """Aggregate post-fill outcomes for edge-vs-outcome research."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - max(0, window_seconds) * 1000
        horizon = int(horizon_seconds)
        pnl_col = self._pnl_column_for_horizon(horizon)
        observed_col = self._observation_column_for_horizon(horizon)
        on_time = self._on_time_outcome_sql(horizon)
        avg_key = f"avg_pnl_{horizon}s_cents"
        win_key = f"win_rate_{horizon}s"

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                timing_rows = cursor.execute(
                    f"""
                    SELECT CASE
                        WHEN {on_time} THEN 'on_time'
                        WHEN f.{observed_col} IS NOT NULL THEN 'delayed'
                        WHEN f.{pnl_col} IS NOT NULL THEN 'legacy_unverified'
                        WHEN f.fill_ts_ms + ? < ? THEN 'missed'
                        ELSE 'pending'
                    END AS timing, COUNT(*)
                    FROM paper_fills f
                    WHERE f.fill_ts_ms >= ? AND f.intent_id NOT LIKE 'exit_intent:%'
                      AND COALESCE(f.note, '') NOT LIKE 'exit%'
                    GROUP BY timing
                    """,
                    (horizon * 1000 + OUTCOME_MAX_DELAY_MS, now_ms, cutoff_ms),
                ).fetchall()
                timing_counts = dict(timing_rows)
                cursor.execute(
                    f"""
                    SELECT f.fill_ts_ms, f.note, f.{pnl_col},
                              i.side, i.net_edge, COALESCE(f.universe, i.universe)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND f.{pnl_col} IS NOT NULL
                      AND {on_time}
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND COALESCE(f.note, '') NOT LIKE 'exit%'
                    ORDER BY f.fill_ts_ms DESC
                    """,
                    (cutoff_ms,),
                )
                rows = cursor.fetchall()

            total_samples = len(rows)
            if total_samples == 0:
                empty_result: dict[str, Any] = {
                    "window_seconds": window_seconds,
                    "horizon_seconds": horizon,
                    "observation_timing_counts": timing_counts,
                    "total_samples": 0,
                    avg_key: None,
                    win_key: None,
                    "entry_only_sample_count": 0,
                    "entry_only_by_side": {},
                    "entry_only_by_fill_path": {},
                    "entry_only_by_universe": {},
                    "by_side": {},
                    "by_fill_path": {},
                    "by_universe": {},
                    "by_edge_bucket": [],
                }
                if horizon == 30:
                    empty_result["avg_pnl_30s_cents"] = None
                    empty_result["win_rate_30s"] = None
                return empty_result

            def _compute_group_stats(values: list[float]) -> dict[str, Any]:
                sample_count = len(values)
                avg_pnl = sum(values) / sample_count if sample_count > 0 else None
                win_rate = (
                    sum(1 for value in values if value > 0) / sample_count
                    if sample_count > 0
                    else None
                )
                return {
                    "sample_count": sample_count,
                    avg_key: avg_pnl,
                    win_key: win_rate,
                }

            all_pnls: list[float] = []
            by_side_values: dict[str, list[float]] = {"YES": [], "NO": []}
            by_fill_path_values: dict[str, list[float]] = {}
            entry_only_by_side_values: dict[str, list[float]] = {"YES": [], "NO": []}
            entry_only_by_fill_path_values: dict[str, list[float]] = {}
            entry_only_by_universe_values: dict[str, list[float]] = {}
            by_universe_values: dict[str, list[float]] = {}
            by_edge_bucket_values: dict[tuple[float, float], list[float]] = {}

            safe_bucket_size = max(0.1, edge_bucket_size_cents)
            for _, note, pnl_30s_after_fill_cents, side, net_edge, universe in rows:
                pnl_value = float(pnl_30s_after_fill_cents)
                all_pnls.append(pnl_value)

                side_key = str(side).upper()
                if side_key not in by_side_values:
                    by_side_values[side_key] = []
                by_side_values[side_key].append(pnl_value)

                fill_path = "unknown"
                if isinstance(note, str) and note.startswith("entry_"):
                    fill_path = note.replace("entry_", "", 1)
                by_fill_path_values.setdefault(fill_path, []).append(pnl_value)

                universe_key = str(universe or "UNKNOWN").upper()
                by_universe_values.setdefault(universe_key, []).append(pnl_value)

                if isinstance(note, str) and note.startswith("entry_"):
                    if side_key not in entry_only_by_side_values:
                        entry_only_by_side_values[side_key] = []
                    entry_only_by_side_values[side_key].append(pnl_value)
                    entry_only_by_fill_path_values.setdefault(fill_path, []).append(pnl_value)
                    entry_only_by_universe_values.setdefault(universe_key, []).append(pnl_value)

                edge_value = float(net_edge or 0.0)
                bucket_floor = (edge_value // safe_bucket_size) * safe_bucket_size
                bucket_key = (bucket_floor, bucket_floor + safe_bucket_size)
                by_edge_bucket_values.setdefault(bucket_key, []).append(pnl_value)

            by_side = {
                side_key: _compute_group_stats(values)
                for side_key, values in by_side_values.items()
                if values
            }
            by_fill_path = {
                path_key: _compute_group_stats(values)
                for path_key, values in by_fill_path_values.items()
            }
            entry_only_by_side = {
                side_key: _compute_group_stats(values)
                for side_key, values in entry_only_by_side_values.items()
                if values
            }
            entry_only_by_fill_path = {
                path_key: _compute_group_stats(values)
                for path_key, values in entry_only_by_fill_path_values.items()
            }
            by_universe = {
                universe_key: _compute_group_stats(values)
                for universe_key, values in by_universe_values.items()
            }
            entry_only_by_universe = {
                universe_key: _compute_group_stats(values)
                for universe_key, values in entry_only_by_universe_values.items()
            }

            by_edge_bucket: list[dict[str, Any]] = []
            for (edge_low, edge_high), values in sorted(by_edge_bucket_values.items()):
                stats = _compute_group_stats(values)
                by_edge_bucket.append(
                    {
                        "edge_bucket_low_cents": edge_low,
                        "edge_bucket_high_cents": edge_high,
                        **stats,
                    }
                )

            overall = _compute_group_stats(all_pnls)
            result: dict[str, Any] = {
                "window_seconds": window_seconds,
                "horizon_seconds": horizon,
                "observation_timing_counts": timing_counts,
                "total_samples": total_samples,
                avg_key: overall[avg_key],
                win_key: overall[win_key],
                "entry_only_sample_count": int(
                    sum(len(values) for values in entry_only_by_fill_path_values.values())
                ),
                "entry_only_by_side": entry_only_by_side,
                "entry_only_by_fill_path": entry_only_by_fill_path,
                "entry_only_by_universe": entry_only_by_universe,
                "by_side": by_side,
                "by_fill_path": by_fill_path,
                "by_universe": by_universe,
                "by_edge_bucket": by_edge_bucket,
            }
            if horizon == 30:
                # Preserve historical keys used by existing clients/tests.
                result["avg_pnl_30s_cents"] = overall[avg_key]
                result["win_rate_30s"] = overall[win_key]
            return result
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_short_horizon_outcome_metrics_error",
                extra={
                    "window_seconds": window_seconds,
                    "edge_bucket_size_cents": edge_bucket_size_cents,
                    "horizon_seconds": horizon,
                    "error": str(e),
                },
            )
            error_result: dict[str, Any] = {
                "window_seconds": window_seconds,
                "horizon_seconds": horizon,
                "total_samples": 0,
                avg_key: None,
                win_key: None,
                "entry_only_sample_count": 0,
                "entry_only_by_side": {},
                "entry_only_by_fill_path": {},
                "entry_only_by_universe": {},
                "by_side": {},
                "by_fill_path": {},
                "by_universe": {},
                "by_edge_bucket": [],
            }
            if horizon == 30:
                error_result["avg_pnl_30s_cents"] = None
                error_result["win_rate_30s"] = None
            return error_result

    def get_activity_metrics(self, window_seconds: int = 3600) -> dict[str, Any]:
        """Get lightweight activity metrics for paper trading telemetry.

        Args:
            window_seconds: Rolling window size for recent activity metrics.

        Returns:
            Dict with fill/intents rates and timing aggregates.
        """
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - max(0, window_seconds) * 1000

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute("SELECT COUNT(*) FROM quote_intents")
                total_intents = int(cursor.fetchone()[0] or 0)

                cursor.execute("SELECT COUNT(*) FROM paper_fills")
                total_fills = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    "SELECT COUNT(*) FROM quote_intents WHERE created_ts_ms >= ?",
                    (cutoff_ms,),
                )
                intents_last_window = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    "SELECT COUNT(*) FROM paper_fills WHERE fill_ts_ms >= ?",
                    (cutoff_ms,),
                )
                fills_last_window = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT AVG((first_fill_ts_ms - i.created_ts_ms) / 1000.0)
                    FROM quote_intents i
                    JOIN (
                        SELECT intent_id, MIN(fill_ts_ms) AS first_fill_ts_ms
                        FROM paper_fills
                        GROUP BY intent_id
                    ) ff ON i.id = ff.intent_id
                    """
                )
                avg_time_to_fill_seconds_raw = cursor.fetchone()[0]
                avg_time_to_fill_seconds = (
                    float(avg_time_to_fill_seconds_raw)
                    if avg_time_to_fill_seconds_raw is not None
                    else None
                )

                cursor.execute("SELECT COUNT(DISTINCT intent_id) FROM paper_fills")
                intents_with_at_least_one_fill = int(cursor.fetchone()[0] or 0)

                fill_rate = (
                    float(intents_with_at_least_one_fill / total_intents)
                    if total_intents > 0
                    else 0.0
                )
                raw_fills_per_intent_ratio = (
                    float(total_fills / total_intents) if total_intents > 0 else 0.0
                )

                return {
                    "fill_rate": fill_rate,
                    "raw_fills_per_intent_ratio": raw_fills_per_intent_ratio,
                    "intents_with_at_least_one_fill": intents_with_at_least_one_fill,
                    "avg_time_to_fill_seconds": avg_time_to_fill_seconds,
                    "fills_last_1h": fills_last_window,
                    "intents_last_1h": intents_last_window,
                }
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_activity_metrics_error",
                extra={"window_seconds": window_seconds, "error": str(e)},
            )
            return {
                "fill_rate": 0.0,
                "raw_fills_per_intent_ratio": 0.0,
                "intents_with_at_least_one_fill": 0,
                "avg_time_to_fill_seconds": None,
                "fills_last_1h": 0,
                "intents_last_1h": 0,
            }

    def get_recent_entry_outcome_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent ENTRY fills with side/outcome fields for manual outcome audits."""
        safe_limit = max(1, min(500, int(limit)))
        on_time = self._on_time_outcome_sql(30)
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                    SELECT f.intent_id, i.ticker, i.side,
                              COALESCE(f.universe, i.universe),
                           i.created_ts_ms, f.fill_ts_ms,
                           f.fill_price_cents, f.best_bid_at_fill_cents,
                           f.cross_at_fill_cents, f.mark_30s_after_fill_cents,
                           f.pnl_30s_after_fill_cents, f.note,
                           i.net_edge, i.quote_price_cents, i.cross_at_submit_cents,
                           LAG(f.fill_ts_ms) OVER (
                               PARTITION BY i.ticker, i.side
                               ORDER BY f.fill_ts_ms, f.id
                           ) AS prev_fill_ts_ms,
                           LAG(CASE WHEN {on_time} THEN f.pnl_30s_after_fill_cents END) OVER (
                               PARTITION BY i.ticker, i.side
                               ORDER BY f.fill_ts_ms, f.id
                           ) AS prev_pnl_30s_after_fill_cents,
                           LAG(i.net_edge) OVER (
                               PARTITION BY i.ticker, i.side
                               ORDER BY f.fill_ts_ms, f.id
                           ) AS prev_net_edge,
                           f.observed_30s_ts_ms, f.execution_source, {on_time}
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    ORDER BY f.fill_ts_ms DESC, f.id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                )
                rows = cursor.fetchall()

            audit_rows: list[dict[str, Any]] = []
            for row in rows:
                created_ts_ms = int(row[4]) if row[4] is not None else None
                fill_ts_ms = int(row[5]) if row[5] is not None else None
                quote_price = float(row[13]) if row[13] is not None else None
                submit_cross = float(row[14]) if row[14] is not None else None
                previous_fill_ts_ms = int(row[15]) if row[15] is not None else None
                previous_entry_same_ticker_side_pnl_30s_cents = (
                    float(row[16]) if row[16] is not None else None
                )
                previous_entry_same_ticker_side_net_edge = (
                    float(row[17]) if row[17] is not None else None
                )
                minutes_since_previous_entry_same_ticker_side: float | None = None
                if fill_ts_ms is not None and previous_fill_ts_ms is not None:
                    minutes_since_previous_entry_same_ticker_side = (
                        float(fill_ts_ms - previous_fill_ts_ms) / 60_000.0
                    )
                abs_distance_from_50_cents = (
                    abs(quote_price - 50.0) if quote_price is not None else None
                )
                capturable_edge_to_cross_at_entry: float | None = None
                if quote_price is not None and submit_cross is not None:
                    capturable_edge_to_cross_at_entry = submit_cross - quote_price

                audit_rows.append(
                    {
                        "intent_id": row[0],
                        "ticker": row[1],
                        "side": row[2],
                        "universe": row[3],
                        "created_ts_ms": created_ts_ms,
                        "fill_ts_ms": fill_ts_ms,
                        "fill_price_cents": row[6],
                        "best_bid_at_fill_cents": row[7],
                        "cross_at_fill_cents": row[8],
                        "mark_30s_after_fill_cents": row[9] if row[20] else None,
                        "pnl_30s_after_fill_cents": row[10] if row[20] else None,
                        "observed_30s_ts_ms": row[18],
                        "execution_source": row[19] or "legacy_unverified",
                        "observation_30s_mark_cents": row[9],
                        "observation_30s_pnl_cents": row[10],
                        "note": row[11],
                        "net_edge_at_entry": row[12],
                        "intended_quote_cents": quote_price,
                        "abs_distance_from_50_cents": abs_distance_from_50_cents,
                        "capturable_edge_to_cross_at_entry": capturable_edge_to_cross_at_entry,
                        "minutes_since_previous_entry_same_ticker_side": (
                            minutes_since_previous_entry_same_ticker_side
                        ),
                        "previous_entry_same_ticker_side_pnl_30s_cents": (
                            previous_entry_same_ticker_side_pnl_30s_cents
                        ),
                        "previous_entry_same_ticker_side_net_edge": (
                            previous_entry_same_ticker_side_net_edge
                        ),
                    }
                )
            return audit_rows
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_recent_entry_outcome_audit_error",
                extra={"limit": safe_limit, "error": str(e)},
            )
            return []

    def get_entry_outcome_concentration_metrics(
        self,
        window_seconds: int = 3600,
        top_n: int = 10,
    ) -> dict[str, Any]:
        """Get ticker concentration metrics for entry-only fills in a rolling window."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - max(0, int(window_seconds)) * 1000
        safe_top_n = max(1, min(50, int(top_n)))
        on_time_pnl = (
            f"CASE WHEN {self._on_time_outcome_sql(30)} THEN f.pnl_30s_after_fill_cents END"
        )

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    """,
                    (cutoff_ms,),
                )
                total_entry_samples = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(DISTINCT i.ticker)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    """,
                    (cutoff_ms,),
                )
                unique_tickers = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT COUNT(DISTINCT i.ticker || '|' || i.side)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    """,
                    (cutoff_ms,),
                )
                unique_ticker_side_pairs = int(cursor.fetchone()[0] or 0)

                cursor.execute(
                    """
                    SELECT i.universe, COUNT(*)
                                        FROM paper_fills f
                                        JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    GROUP BY i.universe
                    """,
                    (cutoff_ms,),
                )
                by_universe_counts = {
                    str(row[0] or "UNKNOWN").upper(): int(row[1]) for row in cursor.fetchall()
                }

                cursor.execute(
                    """
                    SELECT i.side, COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    GROUP BY i.side
                    """,
                    (cutoff_ms,),
                )
                side_counts = {str(row[0]).upper(): int(row[1]) for row in cursor.fetchall()}

                cursor.execute(
                    """
                    SELECT CASE
                               WHEN f.note LIKE 'entry_%' THEN REPLACE(f.note, 'entry_', '')
                               ELSE 'unknown'
                           END AS fill_path,
                           COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    GROUP BY fill_path
                    ORDER BY COUNT(*) DESC, fill_path ASC
                    """,
                    (cutoff_ms,),
                )
                fill_path_counts = {str(row[0]): int(row[1]) for row in cursor.fetchall()}

                cursor.execute(
                    f"""
                    SELECT i.ticker,
                           COUNT(*) AS sample_count,
                           AVG({on_time_pnl}) AS avg_pnl_30s_cents,
                           CASE
                               WHEN SUM(CASE WHEN {on_time_pnl} IS NOT NULL THEN 1 ELSE 0 END) = 0
                                   THEN NULL
                               ELSE
                                   CAST(SUM(CASE WHEN {on_time_pnl} > 0 THEN 1 ELSE 0 END) AS REAL)
                                   / SUM(CASE WHEN {on_time_pnl} IS NOT NULL THEN 1 ELSE 0 END)
                           END AS win_rate_30s
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    GROUP BY i.ticker
                    ORDER BY sample_count DESC, i.ticker ASC
                    LIMIT ?
                    """,
                    (cutoff_ms, safe_top_n),
                )
                top_tickers_by_sample_count = [
                    {
                        "ticker": row[0],
                        "sample_count": int(row[1]),
                        "avg_pnl_30s_cents": float(row[2]) if row[2] is not None else None,
                        "win_rate_30s": float(row[3]) if row[3] is not None else None,
                    }
                    for row in cursor.fetchall()
                ]

                cursor.execute(
                    f"""
                          SELECT i.ticker, i.side, i.universe,
                           COUNT(*) AS sample_count,
                           AVG({on_time_pnl}) AS avg_pnl_30s_cents,
                           CASE
                               WHEN SUM(CASE WHEN {on_time_pnl} IS NOT NULL THEN 1 ELSE 0 END) = 0
                                   THEN NULL
                               ELSE
                                   CAST(SUM(CASE WHEN {on_time_pnl} > 0 THEN 1 ELSE 0 END) AS REAL)
                                   / SUM(CASE WHEN {on_time_pnl} IS NOT NULL THEN 1 ELSE 0 END)
                           END AS win_rate_30s
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    GROUP BY i.ticker, i.side, i.universe
                    ORDER BY sample_count DESC, i.ticker ASC, i.side ASC, i.universe ASC
                    LIMIT ?
                    """,
                    (cutoff_ms, safe_top_n),
                )
                top_ticker_sides_by_sample_count = [
                    {
                        "ticker": row[0],
                        "side": row[1],
                        "universe": row[2],
                        "sample_count": int(row[3]),
                        "avg_pnl_30s_cents": float(row[4]) if row[4] is not None else None,
                        "win_rate_30s": float(row[5]) if row[5] is not None else None,
                    }
                    for row in cursor.fetchall()
                ]

                cursor.execute(
                    """
                    SELECT i.ticker, i.side, COALESCE(i.universe, 'UNKNOWN') AS universe, COUNT(*)
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                    GROUP BY i.ticker, i.side, COALESCE(i.universe, 'UNKNOWN')
                    ORDER BY COUNT(*) DESC, i.ticker ASC, i.side ASC, universe ASC
                    """,
                    (cutoff_ms,),
                )
                entry_samples_by_ticker_side = {
                    f"{str(row[0])}|{str(row[1]).upper()}|{str(row[2]).upper()}": int(row[3])
                    for row in cursor.fetchall()
                }

                by_universe_share = {
                    key: (float(count / total_entry_samples) if total_entry_samples > 0 else 0.0)
                    for key, count in by_universe_counts.items()
                }
                side_share_of_entry_samples = {
                    key: (float(count / total_entry_samples) if total_entry_samples > 0 else 0.0)
                    for key, count in side_counts.items()
                }
                fill_path_share_of_entry_samples = {
                    key: (float(count / total_entry_samples) if total_entry_samples > 0 else 0.0)
                    for key, count in fill_path_counts.items()
                }

                top_ticker_side_concentration_share = (
                    float(top_ticker_sides_by_sample_count[0]["sample_count"] / total_entry_samples)
                    if total_entry_samples > 0 and top_ticker_sides_by_sample_count
                    else 0.0
                )
                top_ticker_concentration_share = (
                    float(top_tickers_by_sample_count[0]["sample_count"] / total_entry_samples)
                    if total_entry_samples > 0 and top_tickers_by_sample_count
                    else 0.0
                )

            return {
                "window_seconds": int(window_seconds),
                "total_entry_samples": total_entry_samples,
                "unique_tickers": unique_tickers,
                "unique_ticker_side_pairs": unique_ticker_side_pairs,
                "distinct_ticker_count": unique_tickers,
                "distinct_ticker_side_count": unique_ticker_side_pairs,
                "by_universe_counts": by_universe_counts,
                "by_universe_share_of_entry_samples": by_universe_share,
                "by_side_counts": side_counts,
                "side_share_of_entry_samples": side_share_of_entry_samples,
                "entry_samples_by_fill_path": fill_path_counts,
                "fill_path_share_of_entry_samples": fill_path_share_of_entry_samples,
                "entry_samples_by_ticker_side": entry_samples_by_ticker_side,
                "top_ticker_concentration_share": top_ticker_concentration_share,
                "top_ticker_side_concentration_share": top_ticker_side_concentration_share,
                "top_tickers_by_sample_count": top_tickers_by_sample_count,
                "top_ticker_sides_by_sample_count": top_ticker_sides_by_sample_count,
            }
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_entry_outcome_concentration_metrics_error",
                extra={"window_seconds": window_seconds, "top_n": safe_top_n, "error": str(e)},
            )
            return {
                "window_seconds": int(window_seconds),
                "total_entry_samples": 0,
                "unique_tickers": 0,
                "unique_ticker_side_pairs": 0,
                "distinct_ticker_count": 0,
                "distinct_ticker_side_count": 0,
                "by_universe_counts": {},
                "by_universe_share_of_entry_samples": {},
                "by_side_counts": {},
                "side_share_of_entry_samples": {},
                "entry_samples_by_fill_path": {},
                "fill_path_share_of_entry_samples": {},
                "entry_samples_by_ticker_side": {},
                "top_ticker_concentration_share": 0.0,
                "top_ticker_side_concentration_share": 0.0,
                "top_tickers_by_sample_count": [],
                "top_ticker_sides_by_sample_count": [],
            }

    @staticmethod
    def _normalize_fill_path_filters(
        fill_paths: list[str] | tuple[str, ...] | set[str] | None,
    ) -> set[str] | None:
        if fill_paths is None:
            return None
        normalized = {str(path).strip().lower() for path in fill_paths if str(path).strip() != ""}
        if not normalized:
            return None
        return normalized

    @staticmethod
    def _entry_fill_path_from_note(note: Any) -> str:
        if isinstance(note, str) and note.startswith("entry_"):
            return note.replace("entry_", "", 1)
        return "unknown"

    @staticmethod
    def _compute_confidence_eligibility_flags(
        *,
        distinct_ticker_count: int,
        top_ticker_concentration_share: float,
        min_distinct_tickers: int | None,
        max_top_ticker_share: float | None,
    ) -> dict[str, bool | None]:
        meets_distinct = (
            bool(distinct_ticker_count >= int(min_distinct_tickers))
            if min_distinct_tickers is not None
            else None
        )
        meets_concentration = (
            bool(top_ticker_concentration_share <= float(max_top_ticker_share))
            if max_top_ticker_share is not None
            else None
        )
        configured_flags = [
            flag for flag in (meets_distinct, meets_concentration) if flag is not None
        ]
        is_confidence_eligible = all(configured_flags) if configured_flags else None
        return {
            "meets_distinct_ticker_threshold": meets_distinct,
            "meets_concentration_threshold": meets_concentration,
            "is_confidence_eligible": is_confidence_eligible,
        }

    def get_entry_only_metrics_filtered_by_fill_path(
        self,
        *,
        window_seconds: int = 86_400,
        horizon_seconds: int = 30,
        include_fill_paths: list[str] | tuple[str, ...] | set[str] | None = None,
        exclude_fill_paths: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> dict[str, Any]:
        """Compute entry-only summary metrics with optional fill-path filtering."""
        return self.get_entry_outcome_summary(
            window_seconds=window_seconds,
            horizon_seconds=horizon_seconds,
            include_fill_paths=include_fill_paths,
            exclude_fill_paths=exclude_fill_paths,
        )

    def get_headline_metrics_filtered_by_fill_path(
        self,
        *,
        window_seconds: int = 86_400,
        horizon_seconds: int = 30,
        include_fill_paths: list[str] | tuple[str, ...] | set[str] | None = None,
        exclude_fill_paths: list[str] | tuple[str, ...] | set[str] | None = None,
        min_distinct_tickers: int | None = None,
        max_top_ticker_share: float | None = None,
    ) -> dict[str, Any]:
        """Compute headline metrics and confidence eligibility for filtered entry paths."""
        metrics = self.get_entry_outcome_summary(
            window_seconds=window_seconds,
            horizon_seconds=horizon_seconds,
            include_fill_paths=include_fill_paths,
            exclude_fill_paths=exclude_fill_paths,
        )
        metrics.update(
            self._compute_confidence_eligibility_flags(
                distinct_ticker_count=int(metrics.get("distinct_ticker_count", 0)),
                top_ticker_concentration_share=float(
                    metrics.get("top_ticker_concentration_share", 0.0)
                ),
                min_distinct_tickers=min_distinct_tickers,
                max_top_ticker_share=max_top_ticker_share,
            )
        )
        return metrics

    def get_entry_outcome_summary(
        self,
        *,
        window_seconds: int = 86_400,
        horizon_seconds: int = 30,
        edge_bucket_size_cents: float = 1.0,
        include_fill_paths: list[str] | tuple[str, ...] | set[str] | None = None,
        exclude_fill_paths: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> dict[str, Any]:
        """Summarize entry-only post-fill outcomes by universe/side/path/edge bucket."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - max(0, int(window_seconds)) * 1000
        horizon = int(horizon_seconds)
        pnl_col = self._pnl_column_for_horizon(horizon)
        on_time = self._on_time_outcome_sql(horizon)
        avg_key = f"avg_pnl_{horizon}s_cents"
        win_key = f"win_rate_{horizon}s"
        safe_bucket_size = max(0.1, float(edge_bucket_size_cents))
        include_paths = self._normalize_fill_path_filters(include_fill_paths)
        exclude_paths = self._normalize_fill_path_filters(exclude_fill_paths)

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"""
                      SELECT i.side,
                          i.ticker,
                           COALESCE(f.universe, i.universe, 'UNKNOWN') AS universe,
                           f.note,
                           i.net_edge,
                           f.{pnl_col}
                    FROM paper_fills f
                    JOIN quote_intents i ON i.id = f.intent_id
                    WHERE f.fill_ts_ms >= ?
                      AND i.id NOT LIKE 'exit_intent:%'
                      AND (f.note IS NULL OR f.note NOT LIKE 'exit%')
                      AND f.{pnl_col} IS NOT NULL
                      AND {on_time}
                    ORDER BY f.fill_ts_ms DESC
                    """,
                    (cutoff_ms,),
                )
                rows = cursor.fetchall()

            def _stats(values: list[float]) -> dict[str, Any]:
                if not values:
                    return {"sample_count": 0, avg_key: None, win_key: None}
                count = len(values)
                return {
                    "sample_count": count,
                    avg_key: float(sum(values) / count),
                    win_key: float(sum(1 for value in values if value > 0) / count),
                }

            by_universe_values: dict[str, list[float]] = {}
            by_side_values: dict[str, list[float]] = {}
            by_fill_path_values: dict[str, list[float]] = {}
            by_edge_bucket_values: dict[tuple[float, float], list[float]] = {}
            by_ticker_counts: dict[str, int] = {}
            by_ticker_side_counts: dict[str, int] = {}
            filtered_sample_count = 0

            for side, ticker, universe, note, net_edge, pnl_after in rows:
                fill_path = self._entry_fill_path_from_note(note)
                if include_paths is not None and fill_path not in include_paths:
                    continue
                if exclude_paths is not None and fill_path in exclude_paths:
                    continue

                pnl_value = float(pnl_after)
                filtered_sample_count += 1
                ticker_key = str(ticker)
                universe_key = str(universe or "UNKNOWN").upper()
                side_key = str(side).upper()
                ticker_side_key = f"{ticker_key}|{side_key}|{universe_key}"

                by_universe_values.setdefault(universe_key, []).append(pnl_value)
                by_side_values.setdefault(side_key, []).append(pnl_value)
                by_fill_path_values.setdefault(fill_path, []).append(pnl_value)
                by_ticker_counts[ticker_key] = by_ticker_counts.get(ticker_key, 0) + 1
                by_ticker_side_counts[ticker_side_key] = (
                    by_ticker_side_counts.get(ticker_side_key, 0) + 1
                )

                edge_value = float(net_edge or 0.0)
                bucket_floor = (edge_value // safe_bucket_size) * safe_bucket_size
                bucket_key = (bucket_floor, bucket_floor + safe_bucket_size)
                by_edge_bucket_values.setdefault(bucket_key, []).append(pnl_value)

            by_edge_bucket: list[dict[str, Any]] = []
            for (edge_low, edge_high), values in sorted(by_edge_bucket_values.items()):
                by_edge_bucket.append(
                    {
                        "edge_bucket_low_cents": edge_low,
                        "edge_bucket_high_cents": edge_high,
                        **_stats(values),
                    }
                )

            result: dict[str, Any] = {
                "window_seconds": int(window_seconds),
                "horizon_seconds": horizon,
                "total_entry_samples": filtered_sample_count,
                "distinct_ticker_count": len(by_ticker_counts),
                "distinct_ticker_side_count": len(by_ticker_side_counts),
                "by_universe": {
                    key: _stats(values) for key, values in sorted(by_universe_values.items())
                },
                "by_side": {key: _stats(values) for key, values in sorted(by_side_values.items())},
                "by_fill_path": {
                    key: _stats(values) for key, values in sorted(by_fill_path_values.items())
                },
                "entry_samples_by_ticker_side": {
                    key: int(count) for key, count in sorted(by_ticker_side_counts.items())
                },
                "by_edge_bucket": by_edge_bucket,
            }
            if result["total_entry_samples"] > 0 and by_ticker_counts and by_ticker_side_counts:
                top_ticker_count = max(by_ticker_counts.values())
                top_ticker_side_count = max(by_ticker_side_counts.values())
                result["top_ticker_concentration_share"] = float(
                    top_ticker_count / result["total_entry_samples"]
                )
                result["top_ticker_side_concentration_share"] = float(
                    top_ticker_side_count / result["total_entry_samples"]
                )
            else:
                result["top_ticker_concentration_share"] = 0.0
                result["top_ticker_side_concentration_share"] = 0.0

            overall_stats = _stats(
                [value for values in by_fill_path_values.values() for value in values]
            )
            result[avg_key] = overall_stats[avg_key]
            result[win_key] = overall_stats[win_key]
            if horizon == 30:
                # Backward-compatible aliases for legacy tooling.
                result["avg_pnl_30s_cents"] = result[avg_key]
                result["win_rate_30s"] = result[win_key]
                for group in ("by_universe", "by_side", "by_fill_path"):
                    for stats in result[group].values():
                        stats["avg_pnl_30s_cents"] = stats[avg_key]
                        stats["win_rate_30s"] = stats[win_key]
                for stats in result["by_edge_bucket"]:
                    stats["avg_pnl_30s_cents"] = stats[avg_key]
                    stats["win_rate_30s"] = stats[win_key]
            return result
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_entry_outcome_summary_error",
                extra={
                    "window_seconds": window_seconds,
                    "horizon_seconds": horizon,
                    "error": str(e),
                },
            )
            return {
                "window_seconds": int(window_seconds),
                "horizon_seconds": horizon,
                "total_entry_samples": 0,
                "distinct_ticker_count": 0,
                "distinct_ticker_side_count": 0,
                "by_universe": {},
                "by_side": {},
                "by_fill_path": {},
                "entry_samples_by_ticker_side": {},
                "top_ticker_concentration_share": 0.0,
                "top_ticker_side_concentration_share": 0.0,
                avg_key: None,
                win_key: None,
                "by_edge_bucket": [],
            }

    def get_fills_for_position(self, ticker: str, side: str) -> list[dict[str, Any]]:
        """Get all fills for a specific ticker+side position.

        Args:
            ticker: Market ticker.
            side: Position side ("YES" or "NO").

        Returns:
            List of fill dicts with fill_fee_cents.
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT f.id, f.intent_id, f.fill_ts_ms, f.fill_price_cents,
                           f.fill_size, f.remaining_size, f.queue_ahead_at_fill,
                              f.fill_fee_cents, f.note, f.best_bid_at_fill_cents,
                                        f.cross_at_fill_cents, COALESCE(f.universe, i.universe)
                    FROM paper_fills f
                    JOIN quote_intents i ON f.intent_id = i.id
                    WHERE i.ticker = ? AND i.side = ?
                    ORDER BY f.fill_ts_ms ASC
                    """,
                    (ticker, side),
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
                            "note": row[8],
                            "best_bid_at_fill_cents": row[9],
                            "cross_at_fill_cents": row[10],
                            "universe": row[11],
                        }
                    )
                return fills
        except sqlite3.Error as e:
            logger.exception(
                "paper_store_get_fills_for_position_error",
                extra={"ticker": ticker, "side": side, "error": str(e)},
            )
            return []
