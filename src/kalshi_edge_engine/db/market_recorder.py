"""Bounded, passive capture of successful single-market HTTP responses."""

import asyncio
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from kalshi_edge_engine.evidence import Receipt

logger = logging.getLogger(__name__)
APP_ID = 0x4B454D44
MIB = 1024 * 1024
DISK_RESERVE = 2048 * MIB
_origin: ContextVar[tuple[str, str | None, str | None]] = ContextVar(
    "market_response_origin", default=("client", None, None)
)


@contextmanager
def capture_origin(
    source: str, universe: str | None = None, intent_id: str | None = None
) -> Iterator[None]:
    token = _origin.set((source, universe, intent_id))
    try:
        yield
    finally:
        _origin.reset(token)


@dataclass(frozen=True)
class RecordedResponse:
    session_id: str
    sequence: int
    received_ns: int
    kind: str
    path: str
    base_url: str
    lane: str
    ticker: str
    universe: str
    source: str
    intent_id: str | None
    source_time_field: str | None
    source_time: str | None
    body: bytes


class MarketRecorder:
    def __init__(
        self,
        path: str,
        retention_hours: int,
        max_mib: int,
        universe_for: Callable[[str], str],
        *,
        append_only: bool = False,
    ):
        if retention_hours <= 0 or max_mib < 1:
            raise ValueError("Recorder requires positive retention and at least 1 MiB")
        self.path = path
        self.append_only = append_only
        self.initialized = asyncio.Event()
        self.on_failure: Callable[[], None] | None = None
        self.failure: str | None = None
        self.disk_free_bytes: int | None = None
        self.database_bytes = 0
        self.retention_hours = retention_hours
        self.max_bytes = max_mib * MIB
        self.universe_for = universe_for
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.queue: deque[RecordedResponse] = deque()
        self.queue_bytes = 0
        self.in_flight = 0
        self.written = self.dropped = self.pruned = self.errors = self.retained = 0
        self.last_error: str | None = None
        self.ready = False
        self.stop = asyncio.Event()

    @classmethod
    def from_env(cls, universe_for: Callable[[str], str]) -> "MarketRecorder | None":
        if os.getenv("ENABLE_MARKET_DATA_RECORDER", "").lower() not in ("true", "1", "yes"):
            return None
        return cls(
            os.getenv("MARKET_RECORDER_DB", "market_data_recorder.db"),
            int(os.getenv("MARKET_RECORDER_RETENTION_HOURS", "168")),
            int(os.getenv("MARKET_RECORDER_MAX_MIB", "256")),
            universe_for,
            append_only=os.getenv("MARKET_RECORDER_APPEND_ONLY", "").lower()
            in ("true", "1", "yes"),
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "ready": self.ready,
            "append_only": self.append_only,
            "healthy": self.ready and self.failure is None and not self.errors and not self.dropped,
            "failure_reason": self.failure,
            "database_path": self.path,
            "database_bytes": self.database_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_reserve_bytes": DISK_RESERVE if self.append_only else None,
            "last_contiguous_written_sequence": self.written if self.append_only else None,
            "session_id": self.session,
            "received": self.sequence,
            "written": self.written,
            "dropped": self.dropped,
            "queued": len(self.queue),
            "queued_bytes": self.queue_bytes,
            "in_flight": self.in_flight,
            "pruned": self.pruned,
            "retained": self.retained,
            "errors": self.errors,
            "last_error": self.last_error,
            "retention_hours": self.retention_hours,
            "max_database_bytes": self.max_bytes,
        }

    def capture(
        self,
        path: str,
        lane: str,
        received_ns: int,
        data: dict[str, Any],
        body: bytes,
        base_url: str,
    ) -> Receipt | None:
        """Copy immutable HTTP body references on the event loop. No I/O or API calls."""
        segments = path.removeprefix("/historical").strip("/").split("/")
        if len(segments) == 2 and segments[0] == "markets":
            kind = "market"
        elif len(segments) == 3 and segments[0] == "markets" and segments[2] == "orderbook":
            kind = "orderbook"
        elif len(segments) == 2 and segments[0] in ("events", "series"):
            kind = "event" if segments[0] == "events" else "series"
        else:
            return None
        self.sequence += 1
        ticker = unquote(segments[1])
        source, universe, intent_id = _origin.get()
        owner = universe or "UNKNOWN"

        def reference(state: str) -> Receipt:
            return Receipt(
                self.session,
                self.sequence,
                received_ns,
                ticker,
                kind,
                source,
                owner,
                intent_id,
                state,
            )

        if (
            not self.ready
            or self.failure is not None
            or len(self.queue) >= 1024
            or self.queue_bytes + len(body) > 8 * MIB
            or len(body) > min(MIB, self.max_bytes // 16)
        ):
            self.dropped += 1
            self.invalidate(
                "Observation dropped: recorder unavailable, oversized response or full queue"
            )
            return reference("dropped")
        try:
            owner = universe or self.universe_for(ticker)
            nested_key = (
                kind
                if kind != "orderbook"
                else ("orderbook_fp" if "orderbook_fp" in data else "orderbook")
            )
            source_field = source_time = None
            for prefix, obj in (("", data), (nested_key + ".", data.get(nested_key))):
                if not isinstance(obj, dict):
                    continue
                for key in ("updated_time", "ts_ms", "timestamp_ms", "timestamp", "ts"):
                    if isinstance(obj.get(key), (int, float, str)):
                        source_field, source_time = prefix + key, str(obj[key])
                        break
                if source_field:
                    break
            self.queue.append(
                RecordedResponse(
                    session_id=self.session,
                    sequence=self.sequence,
                    received_ns=received_ns,
                    kind=kind,
                    path=path,
                    base_url=base_url,
                    lane=lane,
                    ticker=ticker,
                    universe=owner,
                    source=source,
                    intent_id=intent_id,
                    source_time_field=source_field,
                    source_time=source_time,
                    body=body,
                )
            )
            self.queue_bytes += len(body)
            return reference("queued")
        except Exception as exc:
            self.dropped += 1
            self._error(exc)
            return reference("dropped")

    def require_healthy(self) -> None:
        if not self.ready or self.failure:
            raise RuntimeError(self.failure or "Market recorder is not ready")

    def invalidate(self, reason: str) -> None:
        if self.append_only and self.failure is None:
            self.failure = reason
            self.ready = False
            logger.error("append_only_capture_incomplete: %s", reason)
            if self.on_failure:
                self.on_failure()

    def _storage(self, *, startup: bool = False) -> None:
        path = Path(self.path)
        self.disk_free_bytes = shutil.disk_usage(path.parent).free
        self.database_bytes = path.stat().st_size if path.exists() else 0
        if self.append_only:
            # Leave room for the paper WAL, SQLite rollback journal and other users.
            headroom = max(0, self.max_bytes - self.database_bytes) if startup else 16 * MIB
            if self.disk_free_bytes < DISK_RESERVE + headroom:
                raise OSError("Insufficient disk headroom for append-only capture")
            if self.database_bytes >= self.max_bytes:
                raise OSError("Append-only recorder capacity exhausted")

    def _error(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        logger.error("market_recorder_error: %s", self.last_error)
        self.invalidate(self.last_error)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=1)
        try:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            pages = self.max_bytes // page_size
            if conn.execute(f"PRAGMA max_page_count={pages}").fetchone()[0] > pages:
                raise ValueError("Existing recorder file exceeds configured size limit")
            return conn
        except BaseException:
            conn.close()
            raise

    def _initialize(self) -> int:
        path = Path(self.path).resolve()
        ledgers = {
            Path("paper_trading.db").resolve(),
            Path(os.getenv("PAPER_DB_PATH", "paper_trading.db")).resolve(),
        }
        if any(
            path == Path(f"{paper}{suffix}")
            for paper in ledgers
            for suffix in ("", "-shm", "-wal", "-journal", ".paper-worker.lock")
        ):
            raise ValueError("The recorder path is reserved for the paper ledger")
        if path.exists() and path.stat().st_size:
            # Reject foreign databases and companion files before opening them with SQLite.
            with path.open("rb") as handle:
                header = handle.read(100)
            if (
                not header.startswith(b"SQLite format 3\0")
                or int.from_bytes(header[68:72], "big") != APP_ID
            ):
                raise ValueError("Existing file is not a market recorder database")
        self._storage(startup=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if app_id == APP_ID and version == 1:
                session_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='capture_sessions'"
                ).fetchone()
                if bool(session_table) != self.append_only:
                    raise ValueError("Cannot change retention mode of an existing recorder")
                if self.append_only:
                    self._open_session(conn)
                return int(conn.execute("SELECT COUNT(*) FROM market_events").fetchone()[0])
            if app_id or version or conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise ValueError(
                    "Recorder requires its own SQLite database with a supported schema"
                )
            conn.execute("""CREATE TABLE market_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, schema_version INTEGER NOT NULL,
                session_id TEXT NOT NULL, sequence INTEGER NOT NULL, received_ns INTEGER NOT NULL,
                kind TEXT NOT NULL, request_path TEXT NOT NULL, base_url TEXT NOT NULL, read_lane TEXT NOT NULL,
                ticker TEXT NOT NULL, universe TEXT NOT NULL, source TEXT NOT NULL, intent_id TEXT,
                source_time_field TEXT, source_time TEXT, body BLOB NOT NULL
            )""")
            uniqueness = "UNIQUE " if self.append_only else ""
            conn.execute(
                f"CREATE {uniqueness}INDEX market_events_identity ON market_events(session_id, sequence)"
            )
            conn.execute("CREATE INDEX market_events_receipt ON market_events(received_ns)")
            conn.execute(f"PRAGMA application_id={APP_ID}")
            conn.execute("PRAGMA user_version=1")
            if self.append_only:
                conn.execute("""CREATE TABLE capture_sessions (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, received INTEGER, written INTEGER,
                    dropped INTEGER, errors INTEGER, reason TEXT)""")
                self._open_session(conn)
            return 0

    def _open_session(self, conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM capture_sessions WHERE state != 'clean' LIMIT 1").fetchone():
            raise ValueError("Prior capture incomplete or still open; use a fresh run directory")
        conn.execute("INSERT INTO capture_sessions(id,state) VALUES (?, 'open')", (self.session,))

    def _finish_session(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE capture_sessions SET state=?, received=?, written=?, dropped=?, errors=?, reason=? WHERE id=?",
                (
                    "incomplete" if self.failure else "clean",
                    self.sequence,
                    self.written,
                    self.dropped,
                    self.errors,
                    self.failure,
                    self.session,
                ),
            )

    @staticmethod
    def _insert(conn: sqlite3.Connection, active: list[RecordedResponse]) -> None:
        conn.executemany(
            """INSERT INTO market_events
               (schema_version, session_id, sequence, received_ns, kind, request_path,
                base_url, read_lane, ticker, universe, source, intent_id,
                source_time_field, source_time, body)
               VALUES (1, :session_id, :sequence, :received_ns, :kind, :path,
                       :base_url, :lane, :ticker, :universe, :source, :intent_id,
                       :source_time_field, :source_time, :body)""",
            (vars(event) for event in active),
        )

    def _write(self, batch: list[RecordedResponse]) -> tuple[int, int]:
        self._storage()
        if self.append_only:
            if batch:
                with closing(self._connect()) as conn, conn:
                    self._insert(conn, batch)
                    pages = conn.execute("PRAGMA page_count").fetchone()[0]
                    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                self.database_bytes = pages * page_size
            return len(batch), 0
        cutoff = time.time_ns() - self.retention_hours * 3600 * 10**9
        active = [e for e in batch if e.received_ns >= cutoff]
        with closing(self._connect()) as conn:
            evict_count = 0
            while True:
                removed = 0
                try:
                    with conn:
                        pruned = conn.execute(
                            "DELETE FROM market_events WHERE received_ns < ?", (cutoff,)
                        ).rowcount
                        if evict_count:
                            removed = conn.execute(
                                """DELETE FROM market_events WHERE id IN
                                   (SELECT id FROM market_events ORDER BY id LIMIT ?)""",
                                (evict_count,),
                            ).rowcount
                            pruned += removed
                        self._insert(conn, active)
                    return len(active), pruned
                except sqlite3.OperationalError as exc:
                    if not active or exc.sqlite_errorcode != sqlite3.SQLITE_FULL:
                        raise
                    # A device-full error can have the same code. Only evict at the configured cap.
                    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
                    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
                    if (
                        page_count * page_size + sum(len(e.body) + page_size for e in active)
                        < self.max_bytes
                    ):
                        raise
                    if evict_count and removed < evict_count:
                        raise
                    # Retry the entire transaction. Eviction only commits with its replacement.
                    evict_count = max(len(active), 2 * evict_count)

    async def run(self) -> None:
        """One writer flushes in receipt order and drains on graceful shutdown."""
        try:
            self.retained = await asyncio.to_thread(self._initialize)
        except Exception as exc:
            self._error(exc)
            self.initialized.set()
            return
        self.ready = True
        self.initialized.set()
        next_prune = 0.0
        try:
            while True:
                batch: list[RecordedResponse] = []
                size = 0
                while self.queue and len(batch) < 64 and size < self.max_bytes // 16:
                    event = self.queue.popleft()
                    size += len(event.body)
                    batch.append(event)
                self.queue_bytes -= size
                self.in_flight = len(batch)
                if batch or (not self.failure and time.monotonic() >= next_prune):
                    try:
                        written, pruned = await asyncio.to_thread(self._write, batch)
                        self.written += written
                        self.pruned += pruned
                        self.retained += written - pruned
                        self.dropped += len(batch) - written
                        if self.append_only and self.database_bytes >= self.max_bytes:
                            self.invalidate("Append-only recorder capacity exhausted")
                    except Exception as exc:
                        self.dropped += len(batch)
                        self._error(exc)
                        if self.append_only:
                            self.dropped += len(self.queue)
                            self.queue.clear()
                            self.queue_bytes = 0
                    next_prune = time.monotonic() + 60
                    self.in_flight = 0
                if self.queue:
                    continue
                if self.stop.is_set():
                    break
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), timeout=0.5)
        except BaseException as exc:
            self.invalidate(f"Recorder writer interrupted: {type(exc).__name__}")
            raise
        finally:
            self.ready = False
            if self.append_only:
                try:
                    await asyncio.to_thread(self._finish_session)
                except Exception as exc:
                    self._error(exc)
