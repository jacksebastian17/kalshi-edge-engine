"""Verified, local-receipt projection of a sealed Stage 1.1 backup."""

import base64
import hashlib
import json
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from operator import attrgetter
from pathlib import Path

from kalshi_edge_engine.engine import orderbook
from tools.capture_builds import approved_capture, verify_replay_modules
from tools.microstructure import journal
from tools.microstructure.feed import TRADE, Feed, cents, fixed

REPLAY_ORDERBOOK = "b881e5236da91e72f71af1b36faf9b76a18617f32b15e72487d1849214a44d55"
FILES = (journal.MANIFEST, journal.SEAL, journal.LOG, journal.HEALTH, journal.BACKUP_HASHES)
SIDES = ("yes", "no")
Level = tuple[Decimal, Decimal]
Key = tuple[str, str, Decimal]
ZERO = Decimal(0)


def source_hashes(archive: Path) -> dict[str, str]:
    return {name: journal.digest(archive / name) for name in FILES}


@dataclass(frozen=True, slots=True)
class Quote:
    ready: bool
    yes: Level
    no: Level

    def bid(self, side: str) -> Level:
        return self.yes if side == "yes" else self.no

    def passive(self) -> bool:
        return self.yes[1] > 0 and self.no[1] > 0 and self.yes[0] + self.no[0] < 100


EMPTY = Quote(False, (ZERO, ZERO), (ZERO, ZERO))


@dataclass(frozen=True, slots=True)
class BookAt:
    record: int
    quote: Quote
    snapshot_record: int | None = None


@dataclass(frozen=True, slots=True)
class DepthReceipt:
    record: int
    sid: int
    seq: int
    yes_whole_bid: Decimal | None
    no_whole_bid: Decimal | None

    def whole_bid(self, side: str) -> Decimal | None:
        return self.yes_whole_bid if side == "yes" else self.no_whole_bid


@dataclass(frozen=True, slots=True)
class Flow:
    record: int
    quantity: Decimal  # Signed change for depth, positive contracts for a public print.
    trade_id: str = ""


@dataclass
class Market:
    history: list[BookAt] = field(default_factory=lambda: [BookAt(0, EMPTY)])
    breaks: list[int] = field(default_factory=list)
    depth: list[DepthReceipt] = field(default_factory=list)
    # Optional Stage 2B3 projection, only for prices actually proposed in this session.
    queues: dict[tuple[str, Decimal], list[tuple[int, Decimal, Decimal]]] = field(
        default_factory=dict
    )

    def depth_at(self, record: int) -> DepthReceipt | None:
        i = bisect_right(self.depth, record, key=attrgetter("record"))
        return self.depth[i - 1] if i else None

    def at(self, record: int) -> BookAt:
        return self.history[bisect_right(self.history, record, key=attrgetter("record")) - 1]

    def update(self, feed: Feed, ticker: str, n: int) -> None:
        levels = []
        for side in SIDES:
            book = feed.books.get(ticker, {}).get(side, {})
            price = max(book, default=ZERO)
            levels.append((price, book.get(price, ZERO)))
        quote = Quote(ticker in feed.books and feed.trade_contiguous, *levels)
        snapshot = feed.snapshot_record.get(ticker) if ticker in feed.books else None
        if quote != self.history[-1].quote or snapshot != self.history[-1].snapshot_record:
            self.history.append(BookAt(n, quote, snapshot))


class Tape:
    def __init__(self, tickers: list[str]):
        self.markets = {t: Market() for t in tickers}
        self.mono = [0]  # Journal record number indexes the original receipt clock.
        self.start: int | None = None
        self.start_wall_ns: int | None = None
        self.end: int | None = None
        self.end_record = 0
        self.flows: dict[Key, list[Flow]] = defaultdict(list)
        self.peers: dict[int, int] = {}
        self.ambiguous: dict[int, int] = {}
        self.suspect_ranges: list[tuple[int, int]] = []

    def cutoff(self, mono: int) -> int:
        return min(self.end_record, bisect_right(self.mono, mono) - 1)


def project(
    path: Path, report: dict, *, with_depth: bool = False, proposal_prices: set[Key] | None = None
) -> Tape:
    tape = Tape(report["subscription_markets"])
    with_depth = with_depth or bool(proposal_prices)
    for ticker, side, price in proposal_prices or ():
        if ticker in tape.markets:
            tape.markets[ticker].queues[side, price] = []
    feed = Feed(list(tape.markets))
    replay_hash = hashlib.sha256()
    with path.open("rb") as source:
        for line in source:
            replay_hash.update(line)
            n, wall, mono, kind, value = json.loads(line)
            tape.mono.append(mono)
            if kind == "started":
                tape.start = mono
                tape.start_wall_ns = wall
            if kind == "collection_ended":
                tape.end, tape.end_record = mono, n
            old_epoch = feed.epoch
            old_trade = feed.trade_contiguous
            packet = {}
            if kind == "raw":
                value = base64.b64decode(value, validate=True)
                try:
                    decoded = json.loads(value, parse_float=Decimal)
                    packet = decoded if isinstance(decoded, dict) else {}
                except (ValueError, UnicodeError):
                    pass  # The canonical feed will record the malformed receipt and cut continuity.
            msg = packet.get("msg")
            msg = msg if isinstance(msg, dict) else {}
            symbol_field = msg.get("market_ticker")
            ticker = symbol_field if isinstance(symbol_field, str) else ""
            market = tape.markets.get(ticker)
            stats = feed.by_market[ticker] if market else {}
            deltas = stats.get("deltas", 0)
            prints = stats.get("on_book_trades", 0)
            uncertain = stats.get("unknown_block_status", 0) + stats.get(
                "unclassified_trade_direction", 0
            )
            snapshot = feed.snapshot_record.get(ticker) if market else None
            feed.event(kind, value, wall, mono)
            if (
                with_depth
                and market
                and (stats["deltas"] > deltas or feed.snapshot_record.get(ticker) == n)
            ):
                # Same whole-contract capacity per price as canonical Book.whole_bids.
                ladder = feed.books[ticker]
                whole = [
                    max((p for p, q in ladder[s].items() if q >= 1), default=None) for s in SIDES
                ]
                market.depth.append(
                    DepthReceipt(n, packet["sid"], packet["seq"], whole[0], whole[1])
                )
                if feed.snapshot_record.get(ticker) == n:
                    for (side, price), history in market.queues.items():
                        levels = ladder[side]
                        better = sum((q for p, q in levels.items() if p > price), ZERO)
                        history.append((n, levels.get(price, ZERO), better))
            if market and stats["deltas"] > deltas:
                key = (ticker, msg["side"], cents(msg["price_dollars"]))
                delta = fixed(msg["delta_fp"], 8)
                tape.flows[key].append(Flow(n, delta))
                for (side, price), history in market.queues.items():
                    if side == key[1] and key[2] >= price:
                        _, at, better = history[-1]
                        if key[2] == price:
                            at += delta
                        else:
                            better += delta
                        history.append((n, at, better))
            new_uncertain = stats.get("unknown_block_status", 0) + stats.get(
                "unclassified_trade_direction", 0
            )
            if market and stats["on_book_trades"] > prints and new_uncertain == uncertain:
                resting = "no" if msg["taker_side"] == "yes" else "yes"
                key = (ticker, resting, fixed(msg[resting + "_price_dollars"], 4) * 100)
                tape.flows[key].append(Flow(n, fixed(msg["count_fp"], 8), msg["trade_id"]))
            if old_epoch != feed.epoch or old_trade != feed.trade_contiguous:
                for symbol, item in tape.markets.items():
                    item.breaks.append(n)
                    item.update(feed, symbol, n)
            elif market:
                if snapshot is not None and snapshot != feed.snapshot_record.get(ticker):
                    market.breaks.append(n)
                if uncertain < new_uncertain:
                    market.breaks.append(n)
                if packet.get("type") != TRADE:
                    market.update(feed, ticker, n)

    if replay_hash.hexdigest() != report["journal_sha256"]:
        raise ValueError("Replayed journal differs from verified bytes")
    if tape.start is None or tape.end is None or tape.end < tape.start:
        raise ValueError("Stage 2A requires explicit monotonic start and collection end")
    for match in report["exact_coincidence_evidence"]:
        trade = match["trade"]["record"]
        delta = match["decrease"]["record"]
        tape.peers[trade], tape.peers[delta] = delta, trade
    for group in feed.evidence.groups.values():
        records = [ref["record"] for _, ref in group.decreases]
        records.extend(ref["record"] for _, _, ref in group.prints)
        if group.decreases and group.prints and not any(r in tape.peers for r in records):
            group_record = min(records)
            tape.ambiguous.update(dict.fromkeys(records, group_record))
    if feed.evidence.conflicting_ids:
        for key, flows in tape.flows.items():
            tape.flows[key] = [f for f in flows if f.trade_id not in feed.evidence.conflicting_ids]
    for segment in feed.segments:
        if (
            segment["connection"] in feed.evidence.conflicting_connections
            or feed.evidence.conflicting_ids
        ):
            # Conflicts can discredit previously accepted receipts. Keep cohort membership prospective.
            tape.suspect_ranges.append(
                (segment["opened_record"], segment.get("ended_record", tape.end_record))
            )
    return tape


def verified_source(archive: Path) -> tuple[dict, dict, dict]:
    if archive.resolve() != archive or not (archive / journal.BACKUP_HASHES).is_file():
        raise ValueError("Expected an unlinked sealed backup with a hash inventory")
    before = source_hashes(archive)
    meta = journal.manifest(archive)
    build = meta.get("build", {})
    approved_capture(build)
    verify_replay_modules(build)
    parser_bytes = Path(orderbook.__file__).read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(parser_bytes).hexdigest() != REPLAY_ORDERBOOK:
        raise ValueError("Canonical orderbook parser differs from the Stage 2A replay baseline")
    report = journal.inspect(archive)
    if not isinstance(report["seal"], dict):
        raise ValueError("Missing journal seal contents")
    if report["journal_sha256"] != before[journal.LOG]:
        raise ValueError("Journal changed during verification")
    return meta, report, before
