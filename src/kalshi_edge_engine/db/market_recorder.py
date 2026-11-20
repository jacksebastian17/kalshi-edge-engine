"""Bounded, passive capture of successful single-market HTTP responses."""

import asyncio
import logging
import os
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

logger = logging.getLogger(__name__)
APP_ID = 0x4B454D44
MIB = 1024 * 1024
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
        self, path: str, retention_hours: int, max_mib: int, universe_for: Callable[[str], str]
    ):
        if retention_hours <= 0 or max_mib < 1:
            raise ValueError("Recorder requires positive retention and at least 1 MiB")
        self.path = path
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
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "ready": self.ready,
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
    ) -> None:
        """Copy immutable HTTP body references on the event loop. No I/O or API calls."""
        segments = path.removeprefix("/historical").strip("/").split("/")
        if len(segments) == 2 and segments[0] == "markets":
            kind = "market"
        elif len(segments) == 3 and segments[0] == "markets" and segments[2] == "orderbook":
            kind = "orderbook"
        else:
            return
        self.sequence += 1
        if (
            not self.ready
            or len(self.queue) >= 1024
            or self.queue_bytes + len(body) > 8 * MIB
            or len(body) > min(MIB, self.max_bytes // 16)
        ):
            self.dropped += 1
            return
        try:
            ticker = unquote(segments[1])
            nested_key = (
                "market"
                if kind == "market"
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
            source, universe, intent_id = _origin.get()
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
                    universe=universe or self.universe_for(ticker),
                    source=source,
                    intent_id=intent_id,
                    source_time_field=source_field,
                    source_time=source_time,
                    body=body,
                )
            )
            self.queue_bytes += len(body)
        except Exception as exc:
            self.dropped += 1
            self._error(exc)

    def _error(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        logger.error("market_recorder_error: %s", self.last_error)

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
        paper = Path("paper_trading.db").resolve()
        if path in (
            paper,
            *(
                Path(f"{paper}{suffix}")
                for suffix in ("-shm", "-wal", "-journal", ".paper-worker.lock")
            ),
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
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if app_id == APP_ID and version == 1:
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
            conn.execute("CREATE INDEX market_events_receipt ON market_events(received_ns)")
            conn.execute(f"PRAGMA application_id={APP_ID}")
            conn.execute("PRAGMA user_version=1")
            return 0

    def _write(self, batch: list[RecordedResponse]) -> tuple[int, int]:
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
            return
        self.ready = True
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
                if batch or time.monotonic() >= next_prune:
                    try:
                        written, pruned = await asyncio.to_thread(self._write, batch)
                        self.written += written
                        self.pruned += pruned
                        self.retained += written - pruned
                        self.dropped += len(batch) - written
                    except Exception as exc:
                        self.dropped += len(batch)
                        self._error(exc)
                    next_prune = time.monotonic() + 60
                    self.in_flight = 0
                if self.queue:
                    continue
                if self.stop.is_set():
                    break
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), timeout=0.5)
        finally:
            self.ready = False
