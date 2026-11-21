"""Open sealed snapshots and inspect the unfiltered recorder sequence before selection."""

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from kalshi_edge_engine.db.market_recorder import APP_ID
from kalshi_edge_engine.engine.orderbook import canonical_book
from kalshi_edge_engine.replay import Mode

REPO = Path(__file__).resolve().parents[3]
DB_NAMES = ("market_data_recorder.db", "paper_trading.db")


@dataclass(frozen=True)
class Selection:
    tickers: tuple[str, ...] = ()
    session: str | None = None
    start_ns: int | None = None
    end_ns: int | None = None

    def __post_init__(self) -> None:
        if self.start_ns is not None and self.end_ns is not None and self.start_ns >= self.end_ns:
            raise ValueError("Time selection requires start_ns < end_ns")

    def includes(self, row: sqlite3.Row) -> bool:
        return (
            (not self.tickers or row["ticker"] in self.tickers)
            and (self.session is None or row["session_id"] == self.session)
            and (self.start_ns is None or row["received_ns"] >= self.start_ns)
            and (self.end_ns is None or row["received_ns"] < self.end_ns)
        )


@dataclass
class Archive:
    directory: Path
    recorder: sqlite3.Connection
    ledger: sqlite3.Connection | None
    hashes: dict[str, str]

    @property
    def identifier(self) -> str:
        return hashlib.sha256(json.dumps(self.hashes, sort_keys=True).encode()).hexdigest()

    def rows(self) -> Iterator[sqlite3.Row]:
        yield from self.recorder.execute("SELECT * FROM market_events ORDER BY id")


@contextmanager
def open_archive(directory: Path) -> Iterator[Archive]:
    directory = directory.resolve(strict=True)
    if directory == REPO or (directory / "paper_trading.db.paper-worker.lock").exists():
        raise ValueError("Designate a separate archived dataset or disposable fixture directory")
    with ExitStack() as stack:
        files = {}
        hashes = {}
        stats = {}
        for name in DB_NAMES:
            path = directory / name
            if name == DB_NAMES[1] and not path.exists():
                continue
            if path.resolve(strict=True).parent != directory or any(
                (REPO / live).exists() and path.samefile(REPO / live) for live in DB_NAMES
            ):
                raise ValueError(
                    "Refusing a link to a database outside the archive or a live database"
                )
            if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise ValueError(f"{name} has SQLite sidecars. Supply a standalone SQLite backup")
            stats[path] = path.stat()
            with path.open("rb") as handle:
                hashes[name] = hashlib.file_digest(handle, "sha256").hexdigest()
            conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
            stack.callback(conn.close)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            if [r[0] for r in conn.execute("PRAGMA quick_check")] != ["ok"]:
                raise ValueError(f"SQLite integrity check failed for {name}")
            files[name] = conn
        rec = files[DB_NAMES[0]]
        if (
            rec.execute("PRAGMA application_id").fetchone()[0] != APP_ID
            or rec.execute("PRAGMA user_version").fetchone()[0] != 1
        ):
            raise ValueError("Unsupported B1 recorder database")
        ledger = files.get(DB_NAMES[1])
        if ledger is not None:
            ledger.execute("SELECT submission_evidence FROM quote_intents LIMIT 0")
            ledger.execute("SELECT evidence, candidate_id FROM entry_polls LIMIT 0")
            ledger.execute("SELECT position_lifecycle_id FROM paper_fills LIMIT 0")
        yield Archive(directory, rec, ledger, hashes)
        for path, before in stats.items():
            after = path.stat()
            if (before.st_mtime_ns, before.st_size, before.st_ino) != (
                after.st_mtime_ns,
                after.st_size,
                after.st_ino,
            ):
                raise ValueError("Archive changed during analysis")


def payload(row: sqlite3.Row, mode: Mode = Mode.B2) -> dict[str, Any]:
    if row["schema_version"] != 1:
        raise ValueError("unknown_event_version")

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value}")

    data = json.loads(
        row["body"],
        parse_constant=reject_constant,
        parse_float=str if mode == Mode.CANONICAL and row["kind"] == "orderbook" else float,
    )
    if not isinstance(data, dict):
        raise ValueError("invalid_object")
    path = row["request_path"].removeprefix("/historical").split("/")
    if (
        not row["ticker"]
        or path[:2] != ["", "markets"]
        or len(path) < 3
        or unquote(path[2]) != row["ticker"]
    ):
        raise ValueError("ticker_path_mismatch")
    if row["kind"] == "market":
        market = data.get("market")
        if len(path) != 3 or not isinstance(market, dict) or market.get("ticker") != row["ticker"]:
            raise ValueError("invalid_market")
    elif row["kind"] == "orderbook":
        if mode == Mode.CANONICAL:
            normalized = canonical_book(data)
            malformed = [issue for issue in normalized.issues if issue.startswith("invalid_")]
            if (
                path[3:] != ["orderbook"]
                or malformed
                or sum(s.startswith("missing_") for s in normalized.issues) == 2
            ):
                raise ValueError(f"invalid_book: {normalized.issues}")
            return data
        fixed = "orderbook_fp" in data
        ladder = data.get("orderbook_fp", data.get("orderbook", data))
        keys = ("yes_dollars", "no_dollars") if fixed else ("yes", "no")
        if (
            path[3:] != ["orderbook"]
            or not isinstance(ladder, dict)
            or not any(k in ladder for k in keys)
        ):
            raise ValueError("invalid_book")
        for key in keys:
            levels = ladder.get(key, [])
            if not isinstance(levels, list):
                raise ValueError("invalid_ladder")
            for level in levels:
                if not isinstance(level, list) or len(level) != 2:
                    raise ValueError("invalid_level")
                if any(isinstance(value, bool) for value in level):
                    raise ValueError("invalid_level")
                price, size = map(float, level)
                if not (0 <= price <= (1 if fixed else 100) and math.isfinite(size) and size >= 0):
                    raise ValueError("invalid_level")
    else:
        raise ValueError("unknown_event_kind")
    return data


def validate(archive: Archive, selection: Selection, *, mode: Mode = Mode.B2) -> dict[str, Any]:
    sessions: dict[str, Any] = {}
    segments: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    invalid_ids = []
    ticker_kinds: dict[str, set[str]] = defaultdict(set)
    previous: sqlite3.Row | None = None
    total = selected = 0
    counts: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    universes: Counter[str] = Counter()
    for row in archive.rows():
        total += 1
        sid, seq, ns = row["session_id"], row["sequence"], row["received_ns"]
        boundary = previous is None
        reasons: list[tuple[str, Any]] = []
        s = sessions.setdefault(sid, {"first_sequence": seq, "last_sequence": None, "events": 0})
        last = s["last_sequence"]
        if last is None and seq != 1:
            reasons.append(("missing_prefix", [1, seq - 1]))
        elif last is not None and seq != last + 1:
            reasons.append(
                (
                    "sequence_gap" if seq > last else "out_of_order_sequence",
                    [last + 1, seq - 1] if seq > last else [last, seq],
                )
            )
        if previous is not None:
            if previous["session_id"] != sid:
                reasons.append(("session_boundary", previous["session_id"]))
            if ns < previous["received_ns"]:
                reasons.append(("clock_reversal", previous["received_ns"]))
            if row["id"] > previous["id"] + 1:
                reasons.append(("missing_stored_ids", [previous["id"] + 1, row["id"] - 1]))
        elif row["id"] > 1:
            reasons.append(("missing_initial_stored_ids", [1, row["id"] - 1]))
        try:
            payload(row, mode)
        except (ValueError, TypeError, OverflowError) as exc:
            invalid_ids.append(row["id"])
            reasons.append(("invalid_payload", str(exc)))
        if reasons:
            boundary = True
            issues.extend(
                {"id": row["id"], "session": sid, "reason": k, "detail": v} for k, v in reasons
            )
        if boundary or (previous is not None and invalid_ids and previous["id"] == invalid_ids[-1]):
            segments.append(
                {
                    "first_id": row["id"],
                    "session": sid,
                    "first_received_ns": ns,
                    "events": 0,
                    "selected": 0,
                    "kinds": Counter(),
                    "ticker_kinds": defaultdict(set),
                }
            )
        seg = segments[-1]
        seg.update(last_id=row["id"], last_received_ns=ns)
        seg["events"] += 1
        if selection.includes(row):
            seg["selected"] += 1
            selected += 1
            sources[row["source"]] += 1
            universes[row["universe"]] += 1
            ticker_kinds.setdefault(row["ticker"], set())
            seg["ticker_kinds"].setdefault(row["ticker"], set())
            if not invalid_ids or invalid_ids[-1] != row["id"]:
                seg["kinds"][row["kind"]] += 1
                seg["ticker_kinds"][row["ticker"]].add(row["kind"])
                ticker_kinds[row["ticker"]].add(row["kind"])
        counts[row["kind"]] += 1
        s["events"] += 1
        s["last_sequence"] = seq
        previous = row
    missing_kinds = {
        t: sorted({"market", "orderbook"} - ticker_kinds[t])
        for t in sorted(set(ticker_kinds) | set(selection.tickers))
        if ticker_kinds[t] != {"market", "orderbook"}
    }
    for seg in segments:
        kinds = seg["kinds"]
        seg["missing_kinds_by_ticker"] = {
            ticker: sorted({"market", "orderbook"} - seen)
            for ticker, seen in seg.pop("ticker_kinds").items()
            if seen != {"market", "orderbook"}
        }
        seg["coverage"] = (
            "insufficient_evidence"
            if not kinds.get("orderbook")
            else "partially_observed"
            if seg["missing_kinds_by_ticker"]
            else "complete_relative_to_capture_contract"
        )
    classification = "complete_relative_to_capture_contract"
    if not any(seg["kinds"].get("orderbook") for seg in segments):
        classification = "insufficient_evidence"
    elif (
        issues
        or missing_kinds
        or archive.ledger is None
        or any(seg["selected"] and seg["coverage"] != classification for seg in segments)
    ):
        classification = "partially_observed"
    return {
        "coverage": classification,
        "total_events": total,
        "selected_events": selected,
        "selected_tickers": sorted(ticker_kinds),
        "selected_by_source": sources,
        "selected_by_universe": universes,
        "kinds": counts,
        "sessions": sessions,
        "segments": segments,
        "issues": issues,
        "invalid_event_ids": invalid_ids,
        "missing_kinds_by_ticker": missing_kinds,
        "ledger_available": archive.ledger is not None,
        "contract": "Successful sampled single-market GET responses. Local receipt ID orders ties. "
        "A missing ID prefix is evidence of removed stored rows. Sequence gaps alone cannot distinguish "
        "drops from pruning. Session suffix, inter-poll exchange activity and actual trades are unknown.",
    }
