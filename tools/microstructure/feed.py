"""Deterministic projection of the two public subscriptions from received message bytes."""

import hashlib
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_edge_engine.engine.orderbook import canonical_book

from .coincidences import Bucket, Coincidences
from .timing import Timing

DEPTH = "orderbook_delta"
TRADE = "trade"
CHANNELS = (DEPTH, TRADE)
KINDS = {"orderbook_snapshot": DEPTH, DEPTH: DEPTH, TRADE: TRADE}


def fixed(value: Any, places: int) -> Decimal:
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError("invalid fixed-point type")
    number = Decimal(str(value))
    if (
        not number.is_finite()
        or number.copy_abs() > 2**63 - 1
        or number != number.quantize(Decimal(1).scaleb(-places))
    ):
        raise ValueError("invalid fixed-point precision or range")
    return number


def cents(value: Any) -> Decimal:
    price = fixed(value, 4) * 100
    if not 0 < price < 100:
        raise ValueError("invalid book price")
    return price


class Feed:
    def __init__(self, markets: list[str]):
        self.markets = markets
        self.count: Counter[str] = Counter()
        self.by_market = {t: Counter[str]() for t in markets}
        self.problems: list[dict] = []
        self.connection = 0
        self.epoch = 0
        self.sids: dict[int, str] = {}
        self.last: dict[int, int] = {}
        self.seen: dict[tuple[int, int], bytes] = {}
        self.books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}
        self.trades: set[str] = set()
        self.observed_trade_ids: dict[str, bytes] = {}
        self.trade_contiguous = False
        self.evidence = Coincidences()
        self.timing = Timing()
        self.record = 0
        self.snapshot_record: dict[str, int] = {}
        self.market_clock: dict[str, tuple[int, int]] = {}
        self.clock_boundaries: list[dict] = []
        self.acks: Counter[str] = Counter()
        self.segments: list[dict[str, Any]] = []

    def disconnect(self) -> None:
        self.epoch += 1
        self.sids.clear()
        self.last.clear()
        self.seen.clear()
        self.books.clear()
        self.trade_contiguous = False

    def problem(
        self,
        reason: str,
        channel: str | None,
        ns: int,
        sid: Any = None,
        seq: Any = None,
        expected: int | None = None,
        market: str | None = None,
    ) -> None:
        self.count[reason] += 1
        self.problems.append(
            {
                "connection": self.connection,
                "channel": channel,
                "wall_ns": ns,
                "reason": reason,
                "sid": sid,
                "seq": seq,
                "expected": expected,
                "record": self.record,
                "market": market,
            }
        )
        self.epoch += 1
        if market is not None and channel == DEPTH:
            self.books.pop(market, None)
        elif channel != TRADE:
            self.books.clear()
        if channel != DEPTH:
            self.trade_contiguous = False

    def event(self, kind: str, value: Any, ns: int, mono: int | None = None) -> bool:
        """True requests a fresh connection. Each connection gets two fresh subscriptions."""
        if kind == "raw":
            return self.message(value, ns, mono)
        self.record += 1
        self.timing.receipt(ns, mono)
        if kind == "connected":
            if self.segments and self.segments[-1]["ended_ns"] is None:
                self.segments[-1].update(ended_ns=ns, ended_record=self.record)
            self.disconnect()
            self.connection += 1
            self.segments.append(
                {
                    "connection": self.connection,
                    "opened_ns": ns,
                    "opened_record": self.record,
                    "opened_mono_ns": mono,
                    "acks": {},
                    "ack_records": {},
                    "snapshot_ns": {},
                    "snapshot_records": [],
                    "first_sequence_by_sid": {},
                    "last_sequence_by_sid": {},
                    "first_trade_by_market": {},
                    "ended_ns": None,
                }
            )
        elif kind == "fault":
            self.problem(value["reason"], value.get("channel"), ns)
            self.problems[-1]["detail"] = value
            if self.segments and self.segments[-1]["ended_ns"] is None:
                self.segments[-1]["ended_ns"] = ns
                self.segments[-1]["ended_record"] = self.record
            self.disconnect()
        elif (
            kind in ("collection_ended", "closed")
            and self.segments
            and self.segments[-1]["ended_ns"] is None
        ):
            self.segments[-1]["ended_ns"] = ns
            self.segments[-1]["ended_record"] = self.record
        elif kind == "clock_status":
            self.timing.diagnostics.append(value)
        return False

    def observe_clock(
        self, ticker: str, clock: int, snapshot: bool, ns: int, sid: int, seq: int | None
    ) -> bool:
        stats = self.by_market[ticker]
        prior = self.market_clock.get(ticker)
        self.market_clock[ticker] = clock, self.record
        if prior is None:
            return True
        previous, record = prior
        continuous = ticker in self.books
        if clock < previous:
            stats["clock_regressions"] += 1
            self.clock_boundaries.append(
                {
                    "market": ticker,
                    "connection": self.connection,
                    "previous_record": record,
                    "record": self.record,
                    "previous_clock": previous,
                    "clock": clock,
                    "classification": "unexplained_regression"
                    if continuous
                    else "lower_after_interruption",
                    "maintenance_evidence": "unavailable",
                }
            )
            if continuous:
                self.problem("market_clock_regression", DEPTH, ns, sid, seq, market=ticker)
                # A replacement snapshot itself supplies a fresh baseline.
                return snapshot
        elif continuous and not snapshot:
            if clock == previous and record == self.snapshot_record[ticker]:
                self.problem("delta_at_snapshot_clock", DEPTH, ns, sid, seq, market=ticker)
                return False
            if clock == previous:
                stats["repeated_delta_clocks"] += 1
            elif clock > previous + 1:
                stats["skipped_clock_transitions"] += 1
        return True

    def message(self, raw: bytes, ns: int, mono: int | None = None) -> bool:
        self.record += 1
        self.timing.receipt(ns, mono)
        self.count["messages"] += 1
        channel: str | None = None
        sid = seq = None
        try:
            packet = json.loads(raw, parse_float=Decimal)
            if not isinstance(packet, dict):
                raise ValueError("invalid envelope")
            kind = packet["type"]
            sid, seq = packet.get("sid"), packet.get("seq")
            msg = packet.get("msg", {})
            channel = self.sids.get(sid) if type(sid) is int else None
            if kind == "error":
                command_id = packet.get("id")
                self.problem(
                    "channel_error",
                    CHANNELS[command_id - 1] if command_id in (1, 2) else channel,
                    ns,
                    sid,
                    seq,
                )
                self.problems[-1]["exchange_error"] = packet.get("msg")
                return True
            if kind == "subscribed":
                sid, channel = msg["sid"], msg["channel"]
                if (
                    not self.segments
                    or self.segments[-1]["ended_ns"] is not None
                    or type(sid) is not int
                    or sid < 1
                    or channel not in CHANNELS
                    or packet.get("id") != CHANNELS.index(channel) + 1
                    or sid in self.sids
                    or channel in self.sids.values()
                ):
                    raise ValueError("unexpected acknowledgement")
                self.sids[sid] = channel
                self.acks[channel] += 1
                self.segments[-1]["acks"][channel] = sid
                self.segments[-1]["ack_records"][channel] = self.record
                if channel == TRADE:
                    self.trade_contiguous = True
                return False
            if (
                type(sid) is not int
                or channel is None
                or kind not in KINDS
                or channel != KINDS[kind]
            ):
                raise ValueError("unrecognized subscription or message")
            ticker = msg["market_ticker"]
            if ticker not in self.by_market:
                raise ValueError("ticker outside subscription")
            stats = self.by_market[ticker]
            stats[kind + "_received"] += 1
            self.timing.observe(kind, packet, ns, mono)
            if channel == TRADE and isinstance(msg.get("trade_id"), str):
                # Even an unusable or gapped receipt can contradict an earlier trade identity.
                identity = msg["trade_id"]
                signature = hashlib.sha256(
                    json.dumps(msg, sort_keys=True, default=str).encode()
                ).digest()
                if self.observed_trade_ids.setdefault(identity, signature) != signature:
                    self.evidence.conflicting_ids.add(identity)
            if type(seq) is not int or seq < 1:
                # Preserve trades and access evidence on deployments without a trade sequence.
                if channel != TRADE or seq is not None:
                    raise ValueError("invalid sequence")
                self.count["unsequenced_trades"] += 1
                if self.trade_contiguous:
                    self.problem("trade_sequence_unavailable", TRADE, ns, sid)
            else:
                fingerprint = hashlib.sha256(
                    json.dumps([kind, msg], sort_keys=True, default=str).encode()
                ).digest()
                prior = self.seen.get((sid, seq))
                if prior is not None:
                    if fingerprint == prior:
                        self.count["duplicate_messages"] += 1
                        return False
                    self.evidence.conflicting_connections.add(self.connection)
                    if channel == TRADE and isinstance(msg.get("trade_id"), str):
                        self.evidence.conflicting_ids.add(msg["trade_id"])
                    self.problem("conflicting_sequence", channel, ns, sid, seq)
                    return True
                self.seen[sid, seq] = fingerprint
                last = self.last.get(sid)
                self.last[sid] = seq
                self.segments[-1]["first_sequence_by_sid"].setdefault(str(sid), seq)
                self.segments[-1]["last_sequence_by_sid"][str(sid)] = seq
                if last is not None and seq != last + 1:
                    self.problem("sequence_gaps", channel, ns, sid, seq, last + 1)
                    return True
            event_ms = msg.get("ts_ms")
            if "ts_ms" in msg and (type(event_ms) is not int or not 0 <= event_ms <= 2**63 - 1):
                stats["malformed_event_timestamp"] += 1
                event_ms = None
            ref = {"record": self.record, "sid": sid, "seq": seq}
            if channel == DEPTH:
                clock = msg.get("clock")
                if type(clock) is not int or not 0 <= clock <= 2**63 - 1:
                    raise ValueError("missing or invalid market clock")
                if not self.observe_clock(
                    ticker, clock, kind == "orderbook_snapshot", ns, sid, seq
                ):
                    return True
            for field in (
                ("clock",)
                if kind == "orderbook_snapshot"
                else ("clock", "ts_ms")
                if kind == DEPTH
                else ("ts_ms", "taker_outcome_side", "taker_book_side", "is_block_trade")
            ):
                if msg.get(field) is None:
                    stats["missing_" + kind + "_" + field] += 1
            if kind == "orderbook_snapshot":
                # Kalshi omits a side when there are no levels on it.
                levels = {s + "_dollars": msg.get(s + "_dollars_fp", []) for s in ("yes", "no")}
                if any(k in msg for k in ("yes", "no", "yes_dollars", "no_dollars")):
                    raise ValueError("unsupported snapshot ladder")
                book = canonical_book({"orderbook_fp": levels})
                if book.issues:
                    raise ValueError("invalid canonical snapshot")
                self.books[ticker] = {
                    s: {x.price: x.quantity for x in book.levels(s.upper())} for s in ("yes", "no")
                }
                stats["snapshots"] += 1
                self.snapshot_record[ticker] = self.record
                self.segments[-1]["snapshot_ns"][ticker] = ns
                self.segments[-1]["snapshot_records"].append(
                    {"ticker": ticker, **ref, "clock": clock}
                )
            elif kind == DEPTH:
                if ticker not in self.books:
                    raise ValueError("delta before snapshot")
                side, price, change = (
                    msg["side"],
                    cents(msg["price_dollars"]),
                    fixed(msg["delta_fp"], 8),
                )
                ladder = self.books[ticker][side]
                total = fixed(ladder.get(price, Decimal(0)) + change, 8)
                if total < 0:
                    raise ValueError("negative reconstructed level")
                if total:
                    ladder[price] = total
                else:
                    ladder.pop(price, None)
                stats["deltas"] += 1
                if ticker not in self.segments[-1]["first_trade_by_market"]:
                    stats["deltas_before_first_trade"] += 1
                if not self.trade_contiguous:
                    stats["deltas_without_trade_continuity"] += 1
                if change < 0:
                    stats["decreases"] += 1
                    if self.trade_contiguous and event_ms is not None:
                        key = Bucket(
                            self.connection,
                            self.epoch,
                            self.snapshot_record[ticker],
                            ticker,
                            event_ms,
                            side,
                            price,
                        )
                        self.evidence.groups[key].decreases.append(
                            (-change, {**ref, "clock": clock})
                        )
                    elif self.trade_contiguous:
                        self.evidence.untimed.add((self.connection, self.epoch, ticker))
            else:
                trade_id, taker = msg["trade_id"], msg.get("taker_side")
                direction_known = (
                    taker in ("yes", "no")
                    and msg.get("taker_outcome_side") == taker
                    and msg.get("taker_book_side") == ("bid" if taker == "yes" else "ask")
                )
                block = msg.get("is_block_trade")
                quantity = fixed(msg["count_fp"], 8)
                if not isinstance(trade_id, str) or not trade_id or quantity <= 0:
                    raise ValueError("invalid public trade")
                prices = {s: fixed(msg[s + "_price_dollars"], 4) * 100 for s in ("yes", "no")}
                if min(prices.values()) < 0 or sum(prices.values()) != 100:
                    raise ValueError("invalid trade prices")
                if trade_id in self.evidence.conflicting_ids:
                    if event_ms is not None:
                        self.evidence.uncertain_prints.add((self.connection, ticker, event_ms))
                    self.problem("conflicting_trade_id", TRADE, ns, sid, seq)
                    return True
                if trade_id in self.trades:
                    stats["duplicate_trade_ids"] += 1
                    return False
                self.trades.add(trade_id)
                stats["unique_trades"] += 1
                self.segments[-1]["first_trade_by_market"].setdefault(
                    ticker, {**ref, "mono_ns": mono}
                )
                if block is False:
                    stats["on_book_trades"] += 1
                elif block is True:
                    stats["block_trades"] += 1
                else:
                    stats["unknown_block_status"] += 1
                if not direction_known:
                    stats["unclassified_trade_direction"] += 1
                if event_ms is None and block is not True:
                    self.evidence.untimed.add((self.connection, self.epoch, ticker))
                if (
                    (not direction_known or type(block) is not bool)
                    and block is not True
                    and event_ms is not None
                ):
                    self.evidence.uncertain_prints.add((self.connection, ticker, event_ms))
                if (
                    self.trade_contiguous
                    and ticker in self.books
                    and block is False
                    and direction_known
                    and event_ms is not None
                ):
                    resting = "no" if taker == "yes" else "yes"
                    key = Bucket(
                        self.connection,
                        self.epoch,
                        self.snapshot_record[ticker],
                        ticker,
                        event_ms,
                        resting,
                        prices[resting],
                    )
                    self.evidence.groups[key].prints.append(
                        (
                            quantity,
                            trade_id,
                            {
                                **ref,
                                "taker_side": taker,
                                "taker_outcome_side": msg["taker_outcome_side"],
                                "taker_book_side": msg["taker_book_side"],
                                "is_block_trade": block,
                            },
                        )
                    )
            return False
        except (
            KeyError,
            ValueError,
            InvalidOperation,
            TypeError,
            OverflowError,
            UnicodeError,
        ) as exc:
            self.problem("malformed_message", channel, ns, sid, seq)
            self.problems[-1]["detail"] = str(exc)[:200]
            return True

    def health(self) -> dict:
        return {
            "connections": self.connection,
            "reconnects": max(0, self.connection - 1),
            "current_subscriptions": dict(self.sids),
            "initialized_books": sorted(self.books),
            "trade_contiguous": self.trade_contiguous,
            "acks": dict(self.acks),
            "counts": dict(self.count),
            "markets": {t: dict(c) for t, c in self.by_market.items()},
            "latest_problem": self.problems[-1] if self.problems else None,
        }

    def classifications(self, sealed: bool, matches: int) -> dict:
        def grade(status: str, reason: str) -> dict:
            return {"status": status, "reason": reason}

        def continuity(channel: str) -> dict:
            faults = sorted({p["reason"] for p in self.problems if p["channel"] in (channel, None)})
            if faults or self.connection > 1:
                return grade("interrupted", ", ".join(faults) or "Connection boundary in capture")
            missing = []
            for ticker, stats in self.by_market.items():
                if channel == DEPTH:
                    observed = stats["snapshots"] and stats["deltas"]
                else:
                    observed = stats["unique_trades"]
                if not observed:
                    missing.append(ticker)
            if missing:
                return grade(
                    "indeterminate", "Insufficient channel activity for " + ", ".join(missing)
                )
            if channel == TRADE and any(
                c["deltas_without_trade_continuity"] for c in self.by_market.values()
            ):
                return grade(
                    "partial_overlap",
                    "Depth updates arrived outside acknowledged continuous trade coverage",
                )
            return grade(
                "observed_contiguous",
                "Adjacent observed sequences within this subscription, with "
                + (
                    "valid snapshot and deltas per market"
                    if channel == DEPTH
                    else "distinct prints per market. Silent intervals have no independent delivery check"
                ),
            )

        available = bool(self.segments) and all(
            set(s["acks"]) == set(CHANNELS) for s in self.segments
        )
        return {
            "subscription_availability": grade(
                "acknowledged" if available else "incomplete",
                "Both channels acknowledged on each connection"
                if available
                else "Missing connection or channel acknowledgement",
            ),
            "durable_raw_capture": grade(
                "verified" if sealed else "unsealed_prefix",
                "Journal hash, record count, close and final checkpoint verified"
                if sealed
                else "No verified final seal",
            ),
            "orderbook_continuity": continuity(DEPTH),
            "trade_feed_integrity": continuity(TRADE),
            "clock_alignment_and_latency": grade(
                "indeterminate",
                "No independent contemporaneous bound on the relative host and exchange clock offset",
            ),
            "trade_depth_attribution": grade(
                "exact_coincidences_only" if matches else "indeterminate",
                f"{matches} unique one-to-one field coincidences. Public messages supply no common execution identifier",
            ),
        }

    def report(self, sealed: bool, elapsed_seconds: float) -> dict:
        table, ambiguous = self.evidence.report()
        matches = len(table)
        decreases = sum(c["decreases"] for c in self.by_market.values())
        reasons = []
        if not sealed:
            reasons.append("capture has no clean, verified seal")
        if elapsed_seconds < 600:
            reasons.append("less than ten minutes captured")
        if self.problems:
            reasons.append("one or more invalid segments")
        for ticker, c in self.by_market.items():
            if not all(c[k] for k in ("snapshots", "deltas", "on_book_trades")):
                reasons.append(f"{ticker}: insufficient observations on one or both channels")
            if any(k.startswith("missing_") for k in c):
                reasons.append(f"{ticker}: missing timing, clock or trade classification fields")
            if (
                c["malformed_event_timestamp"]
                or c["unknown_block_status"]
                or c["unclassified_trade_direction"]
            ):
                reasons.append(f"{ticker}: unusable exchange time or print classification")
        classes = self.classifications(sealed, matches)
        if classes["trade_feed_integrity"]["status"] != "observed_contiguous":
            reasons.append(classes["trade_feed_integrity"]["reason"])
        timing = self.timing.report()
        return {
            **self.health(),
            "snapshots_received": sum(
                c["orderbook_snapshot_received"] for c in self.by_market.values()
            ),
            "deltas_received": sum(c["orderbook_delta_received"] for c in self.by_market.values()),
            "public_trades_received": sum(c["trade_received"] for c in self.by_market.values()),
            "unique_public_trades": len(self.trades),
            "sequence_gaps": self.count["sequence_gaps"],
            "subscription_markets": self.markets,
            "elapsed_seconds": elapsed_seconds,
            "clean_seal_verified": sealed,
            "coverage": "incomplete" if reasons else "observed_contiguous",
            "feed_suitability": "insufficient_evidence" if reasons else "short_collection_only",
            "coverage_classifications": classes,
            "clock_alignment_status": "indeterminate",
            "one_way_latency_status": "indeterminate",
            "cross_clock_causal_order_status": "indeterminate",
            "reasons": reasons,
            "segment_faults": self.problems,
            "segments": self.segments,
            "market_clock_boundaries": self.clock_boundaries,
            "last_observed_market_clock": {
                t: {"clock": c, "record": r} for t, (c, r) in self.market_clock.items()
            },
            "valid_book_decreases": decreases,
            "unique_exact_public_print_coincidences": matches,
            "unmatched_decreases": decreases - matches,
            "unmatched_non_block_prints": sum(c["on_book_trades"] for c in self.by_market.values())
            - matches,
            "ambiguous_candidate_groups": ambiguous,
            "conflicting_trade_ids": sorted(self.evidence.conflicting_ids),
            "exact_coincidence_evidence": table,
            "server_send_to_local_receive_ms": timing,
        }
