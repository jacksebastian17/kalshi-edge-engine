"""Deterministic projection of the two public subscriptions from received message bytes."""

import hashlib
import json
import statistics
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_edge_engine.engine.orderbook import canonical_book

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
        self.trades: dict[str, bytes] = {}
        self.trade_contiguous = False
        self.decreases: Counter[tuple] = Counter()
        self.prints: Counter[tuple] = Counter()
        self.send_lag: list[float] = []
        self.first_connect_ns: int | None = None
        self.last_wall_ns = 0
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
            }
        )
        self.epoch += 1
        if channel != TRADE:
            self.books.clear()
        if channel != DEPTH:
            self.trade_contiguous = False

    def event(self, kind: str, value: Any, ns: int) -> bool:
        """True requests a fresh connection. Each connection gets two fresh subscriptions."""
        self.last_wall_ns = ns
        if kind == "connected":
            self.disconnect()
            self.connection += 1
            self.segments.append(
                {
                    "connection": self.connection,
                    "opened_ns": ns,
                    "acks": {},
                    "snapshot_ns": {},
                    "last_sequence_by_sid": {},
                    "ended_ns": None,
                }
            )
            if self.first_connect_ns is None:
                self.first_connect_ns = ns
        elif kind == "fault":
            self.problem(value["reason"], value.get("channel"), ns)
            self.problems[-1]["detail"] = value
            if self.segments and self.segments[-1]["ended_ns"] is None:
                self.segments[-1]["ended_ns"] = ns
            self.disconnect()
        elif kind == "closed" and self.segments and self.segments[-1]["ended_ns"] is None:
            self.segments[-1]["ended_ns"] = ns
        elif kind == "raw":
            return self.message(value, ns)
        return False

    def message(self, raw: bytes, ns: int) -> bool:
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
                    type(sid) is not int
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
                    self.problem("conflicting_sequence", channel, ns, sid, seq)
                    return True
                self.seen[sid, seq] = fingerprint
                last = self.last.get(sid)
                self.last[sid] = seq
                self.segments[-1]["last_sequence_by_sid"][str(sid)] = seq
                if last is not None and seq != last + 1:
                    self.problem("sequence_gaps", channel, ns, sid, seq, last + 1)
                    return True
            sent = packet.get("sending_ts_ms")
            if type(sent) is int:
                self.send_lag.append((ns - sent * 1_000_000) / 1e6)
            elif sent is not None:
                raise ValueError("invalid sending timestamp")
            if any(k in msg and type(msg[k]) is not int for k in ("clock", "ts_ms")):
                raise ValueError("invalid exchange clock or timestamp")
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
                self.segments[-1]["snapshot_ns"][ticker] = ns
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
                if change < 0:
                    stats["decreases"] += 1
                    if self.trade_contiguous and type(msg.get("ts_ms")) is int:
                        self.decreases[self.epoch, ticker, side, price, -change, msg["ts_ms"]] += 1
            else:
                trade_id, taker = msg["trade_id"], msg["taker_side"]
                if (
                    "is_block_trade" in msg
                    and type(msg["is_block_trade"]) is not bool
                    or msg.get("taker_outcome_side", taker) != taker
                    or msg.get("taker_book_side", "bid" if taker == "yes" else "ask")
                    != ("bid" if taker == "yes" else "ask")
                ):
                    raise ValueError("invalid trade classification")
                quantity = fixed(msg["count_fp"], 8)
                if (
                    not isinstance(trade_id, str)
                    or not trade_id
                    or taker not in ("yes", "no")
                    or quantity <= 0
                ):
                    raise ValueError("invalid public trade")
                prices = {s: fixed(msg[s + "_price_dollars"], 4) * 100 for s in ("yes", "no")}
                if min(prices.values()) < 0 or sum(prices.values()) != 100:
                    raise ValueError("invalid trade prices")
                signature = hashlib.sha256(
                    json.dumps(msg, sort_keys=True, default=str).encode()
                ).digest()
                if trade_id in self.trades:
                    if self.trades[trade_id] != signature:
                        self.problem("conflicting_trade_id", TRADE, ns, sid, seq)
                        return True
                    stats["duplicate_trade_ids"] += 1
                    return False
                self.trades[trade_id] = signature
                stats["unique_trades"] += 1
                if msg.get("is_block_trade") is False:
                    stats["on_book_trades"] += 1
                if (
                    self.trade_contiguous
                    and ticker in self.books
                    and msg.get("is_block_trade") is False
                    and type(msg.get("ts_ms")) is int
                ):
                    resting = "no" if taker == "yes" else "yes"
                    self.prints[
                        self.epoch, ticker, resting, prices[resting], quantity, msg["ts_ms"]
                    ] += 1
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

    def report(self, sealed: bool, elapsed_seconds: float) -> dict:
        matches = sum(n == 1 and self.prints[key] == 1 for key, n in self.decreases.items())
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
        lag = sorted(self.send_lag)
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
            "feed_suitability": "insufficient_evidence"
            if reasons
            else "suitable_for_next_pilot_stage",
            "reasons": reasons,
            "segment_faults": self.problems,
            "segments": self.segments,
            "valid_book_decreases": decreases,
            "unique_exact_public_print_coincidences": matches,
            "unmatched_decreases": decreases - matches,
            "server_send_to_local_receive_ms": {
                "samples": len(lag),
                "negative_samples": sum(v < 0 for v in lag),
                "interpretation": "clock_alignment_required"
                if lag and lag[0] < 0
                else "uncalibrated_wall_clock_difference",
                "min": min(lag, default=None),
                "median": statistics.median(lag) if lag else None,
                "max": max(lag, default=None),
            },
        }
