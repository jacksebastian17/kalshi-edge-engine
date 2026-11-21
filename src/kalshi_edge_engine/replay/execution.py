"""Causal per-order queue reconstruction and independent conditional execution experiments."""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from kalshi_edge_engine.engine import orderbook
from kalshi_edge_engine.engine.fees import LEGACY_FEE_VERSION, SERIES_FEE_VERSION, FeeProfile
from kalshi_edge_engine.engine.orderbook import Book, canonical_book
from kalshi_edge_engine.engine.paper_sim import (
    OrderRequest,
    _conservative_participation_fill_size,
    _estimate_moved_through_available_volume,
    _estimate_price_improving_arrival_volume,
    compute_simulator_diagnostics,
    displayed_queue_transition,
)
from kalshi_edge_engine.kalshi.market import MAX_ENTRY_OBSERVATION_AGE_MS, MarketObservation
from kalshi_edge_engine.replay import Mode, b1_orderbook

POLICIES = {
    "baseline": ("queue_cleared",),
    "price_improving_research": ("queue_cleared", "price_improves"),
    "moved_through_research": ("queue_cleared", "moved_through"),
}
MODEL_VERSION = "b1-conditional-entry-v1"


def raw_best_level(body: dict[str, Any], side: str) -> tuple[float, float] | None:
    """Highest positive-size level, retaining fractional contract counts from the HTTP book."""
    yes, no, dollars = b1_orderbook._extract_prices_from(body)
    return max(
        (
            (float(Decimal(str(p)) * (100 if dollars else 1)), float(n))
            for p, n in (yes if side == "YES" else no)
            if float(n) > 0
        ),
        default=None,
    )


PROBABILITY_KEYS = {
    "queue_cleared": "fill_probability_at_best",
    "price_improves": "fill_probability_price_improves",
    "moved_through": "fill_probability_price_moved",
}


def profile_from_audit(audit: dict[str, Any] | None) -> FeeProfile | None:
    if not audit or audit.get("version") not in (SERIES_FEE_VERSION, LEGACY_FEE_VERSION):
        return None
    return FeeProfile(**{**audit, "multiplier": Decimal(audit["multiplier"])})


@dataclass
class Intent:
    row: dict[str, Any]
    config: dict[str, Any]
    order: OrderRequest
    fee: FeeProfile | None

    @property
    def has_execution_config(self) -> bool:
        return {
            "fill_horizon_seconds",
            "simulated_volume_per_poll",
            *PROBABILITY_KEYS.values(),
        } <= self.config.keys()

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Intent":
        submission = json.loads(row["submission_evidence"] or "{}")
        order = OrderRequest(
            row["id"],
            row["ticker"],
            row["side"],
            row["quote_price_cents"],
            row["size_contracts"],
            row["created_ts_ms"],
            row["net_edge"],
            row["entry_fee_cents"] or 0,
            row["max_hold_seconds"] or 0,
        )
        return cls(
            row,
            submission.get("execution_config", {}),
            order,
            profile_from_audit(json.loads(row["fee_profile"] or "null")),
        )


def liquidity_evidence(
    book: Book,
    body: dict[str, Any],
    side: str,
    quote: float,
    mode: Mode,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    """Compare reported depth with the chosen entry parser, before integer order sizing."""
    price = Decimal(str(quote))
    raw_top = book.top(side)
    raw_at_quote = book.at(side, price)
    ask = book.ask(side)
    best = diagnostics["market_best_bid"]
    cross = diagnostics["cross_for_side"]
    raw_size = raw_top.quantity if raw_top else None
    quote_size = raw_at_quote.quantity if raw_at_quote else None
    opposite = book.top("NO" if side == "YES" else "YES")
    expected_top = diagnostics["raw_top_of_book"][side.lower()]
    modeled_capacity = expected_top["best_bid_size"]
    b1_depth = b1_orderbook.entry_depth(body) if mode == Mode.B1_QUEUE else None
    if b1_depth is None:
        model_size = (
            Decimal(diagnostics["reported_best_size"])
            if diagnostics["reported_best_size"] is not None
            else None
        )
        model_quote = (
            Decimal(diagnostics["reported_quote_size"])
            if diagnostics["reported_quote_size"] is not None
            else None
        )
    else:
        sizes = b1_depth[side]
        model_size = Decimal(sizes[max(sizes)]) if sizes else None
        model_quote = Decimal(sizes[price]) if price in sizes else None
    depth_differences = 0
    quantity_differences = 0
    for name in ("YES", "NO"):
        modeled = b1_depth[name] if b1_depth is not None else dict(book.whole_bids(name))
        for level in book.levels(name):
            if modeled.get(level.price, 0) != int(level.quantity):
                depth_differences += 1
            if b1_depth is not None and b1_depth[name].get(level.price, 0) != level.quantity:
                quantity_differences += 1
        if b1_depth is not None:
            raw_prices = {level.price for level in book.levels(name)}
            extra = sum(p not in raw_prices and n > 0 for p, n in modeled.items())
            depth_differences += extra
            quantity_differences += extra
    complete = not book.issues and raw_top is not None and opposite is not None

    def price_agreement(raw: Decimal | None, modeled: float | None) -> bool | None:
        if raw is None and modeled is None:
            return None
        return modeled is not None and raw == Decimal(str(modeled))

    return {
        "raw_best_cents": str(raw_top.price) if raw_top else None,
        "raw_ask_cents": str(ask) if ask is not None else None,
        "best_price_agrees": price_agreement(raw_top.price if raw_top else None, best),
        "implied_ask_agrees": price_agreement(ask, cross),
        "raw_best_quantity": str(raw_size) if raw_size is not None else None,
        "model_best_quantity": str(model_size) if model_size is not None else None,
        "raw_quote_quantity": str(quote_size) if quote_size is not None else None,
        "model_quote_quantity": str(model_quote) if model_quote is not None else None,
        "displayed_size_agrees": raw_size == model_size and quote_size == model_quote
        if raw_top
        else None,
        "best_capacity_contracts": raw_top.capacity if raw_top else None,
        "model_best_capacity_contracts": modeled_capacity,
        "best_capacity_agrees": raw_top.capacity == modeled_capacity if raw_top else None,
        "opposite_capacity_contracts": opposite.capacity if opposite else None,
        "depth_capacity_agrees": depth_differences == 0 if complete else None,
        "depth_quantity_agrees": quantity_differences == 0 if complete else None,
        "different_quantity_levels": quantity_differences,
        "different_capacity_levels": depth_differences,
        "complete_two_sided_depth": complete,
        "uncrossed": bool(raw_top and ask is not None and raw_top.price <= ask),
        "issues": book.issues,
    }


def strict_liquidity_rejection(evidence: dict[str, Any]) -> str | None:
    if not evidence["complete_two_sided_depth"]:
        return "unknown_or_ambiguous_depth"
    if (
        not evidence["uncrossed"]
        or not evidence["best_price_agrees"]
        or not evidence["implied_ask_agrees"]
    ):
        return "price_inconsistent"
    if not (
        evidence["displayed_size_agrees"]
        and evidence["depth_quantity_agrees"]
        and evidence["depth_capacity_agrees"]
        and evidence["best_capacity_agrees"]
    ):
        return "liquidity_inconsistent"
    if not evidence["best_capacity_contracts"] or not evidence["opposite_capacity_contracts"]:
        return "subcontract_top"
    if evidence["queue_liquidity_agrees"] is not True:
        return "unknown_or_inconsistent_queue_liquidity"
    return None


@dataclass
class Queue:
    ahead: int = 0
    last_size: Decimal | int | None = None
    known: bool = False
    missing_history: bool = False
    previous_liquidity_agrees: bool | None = None
    reference_queue: "Queue | None" = None
    owner: tuple[str, str] | None = None

    def observe(
        self,
        intent: Intent,
        body: dict[str, Any],
        mode: Mode = Mode.CANONICAL,
        *,
        reference: Book | None = None,
    ) -> dict[str, Any]:
        order = intent.order
        identity = (order.id, order.ticker)
        if self.owner is not None and identity != self.owner:
            raise ValueError("Queue state belongs to another intent or ticker")
        self.owner = identity
        volume = intent.config["simulated_volume_per_poll"]
        canonical = (reference or canonical_book(body)) if mode != Mode.B2 else None
        reference_poll = None
        if mode == Mode.B1_QUEUE:
            if self.reference_queue is None:
                self.reference_queue = Queue(known=self.known, missing_history=self.missing_history)
            reference_poll = self.reference_queue.observe(
                intent, body, Mode.CANONICAL, reference=canonical
            )
        historical = b1_orderbook.parse_orderbook(body) if mode != Mode.CANONICAL else None
        d = compute_simulator_diagnostics(
            order, canonical or body, self.ahead, self.last_size, volume, historical_view=historical
        )
        eligibility = d["eligibility"]
        at_quote = eligibility["at_best_price"] and not d["price_improves"]
        known_before = self.known
        path = None
        capacity = 0
        before, after = displayed_queue_transition(
            d, self.ahead, initialized=self.last_size is not None
        )
        prior = self.last_size
        if at_quote:
            kind = "at_displayed_quote"
            if self.last_size is None and mode != Mode.B2 and not self.missing_history:
                self.known = True
                known_before = True  # the initialized pre-decrement queue is witnessed
            self.last_size = (
                Decimal(d["reported_best_size"])
                if mode == Mode.CANONICAL
                else d["best_size_at_bid"]
            )
            self.ahead = after
            capacity = max(0, d["traded_volume_estimate"] - before)
            if capacity > 0 and self.known:
                path = "queue_cleared"
            if mode != Mode.B2 and not self.known:
                capacity = 0
                eligibility["queue_cleared"] = None
        else:
            self.last_size = None
            self.ahead = 0
            self.known = True  # B1 resets its queue at every non-displayed-best poll.
            self.missing_history = False
            if d["price_improves"]:
                kind, path = "price_improving", "price_improves"
                capacity = _conservative_participation_fill_size(
                    remaining_size=order.size_contracts,
                    participation_rate=0.25,
                    per_poll_cap_contracts=2,
                    available_volume_estimate=_estimate_price_improving_arrival_volume(
                        simulated_volume_per_poll=volume
                    ),
                )
            elif eligibility["price_moved_through"]:
                kind, path = "moved_through", "moved_through"
                capacity = _conservative_participation_fill_size(
                    remaining_size=order.size_contracts,
                    participation_rate=0.35,
                    per_poll_cap_contracts=3,
                    available_volume_estimate=_estimate_moved_through_available_volume(
                        traded_volume_estimate=d["traded_volume_estimate"],
                        simulated_volume_per_poll=volume,
                    ),
                )
            else:
                kind = "ineligible"
        extra: dict[str, Any] = {}
        if canonical is not None:
            consistency = liquidity_evidence(
                canonical, body, order.side, order.quote_price_cents, mode, d
            )
            level = canonical.top(order.side)
            ask = canonical.ask(order.side)
            raw_bid = float(level.price) if level else None
            raw_cross = float(ask) if ask is not None else None
            current = (
                consistency["displayed_size_agrees"]
                and consistency["best_price_agrees"]
                and consistency["best_capacity_agrees"]
                and consistency["depth_capacity_agrees"]
                and consistency["depth_quantity_agrees"]
                and not canonical.issues
            )
            consistency["queue_liquidity_agrees"] = (
                bool(current and self.previous_liquidity_agrees)
                if self.known and prior is not None and at_quote
                else None
                if at_quote
                else current
            )
            # A matching top on the last two books cannot repair divergence earlier in this queue.
            source = reference_poll if reference_poll is not None else d
            raw_known = (
                reference_poll["queue_state_known"] if reference_poll is not None else self.known
            )
            raw_before = source["queue_ahead_before_decrement"] if raw_known else None
            raw_after = source["queue_ahead_after_poll"] if raw_known else None
            consistency.update(
                raw_queue_before=raw_before,
                raw_queue_after=raw_after,
                model_queue_before=before if self.known else None,
                model_queue_after=after if self.known else None,
                raw_size_decrease=source["traded_volume_estimate"]
                if source["traded_volume_source"] == "observed_size_delta"
                else None,
            )
            if at_quote and consistency["queue_liquidity_agrees"] is True:
                consistency["queue_liquidity_agrees"] = (
                    raw_before == before
                    and raw_after == after
                    and source["traded_volume_estimate"] == d["traded_volume_estimate"]
                    if raw_known
                    else None
                )
            self.previous_liquidity_agrees = current if at_quote else None
            extra["consistency"] = consistency
            extra["queue_transition"] = (
                "unknown_history"
                if not self.known
                else "initialize"
                if at_quote and prior is None
                else "decrement"
                if at_quote and d["traded_volume_estimate"]
                else "hold"
                if at_quote
                else "reset"
                if prior is not None
                else "off_quote"
            )
        else:
            raw = raw_best_level(body, order.side)
            opposite = raw_best_level(body, "NO" if order.side == "YES" else "YES")
            raw_bid = raw[0] if raw else None
            raw_cross = (100 - opposite[0]) if opposite else None
        return {
            **d,
            **extra,
            "raw_bid_cents": raw_bid,
            "raw_cross_cents": raw_cross,
            "raw_top_matches_model": raw_bid == d["market_best_bid"]
            and raw_cross == d["cross_for_side"],
            "poll_class": kind,
            "queue_ahead_before_decrement": before,
            "queue_ahead_after": after,
            "queue_ahead_after_poll": after,
            "queue_before_known": known_before,
            "queue_state_known": self.known,
            "displayed_quote_size": (
                orderbook.displayed_quote_size(
                    canonical or body, order.side, order.quote_price_cents
                )
                if mode == Mode.CANONICAL
                else b1_orderbook.displayed_quote_size(body, order.side, order.quote_price_cents)
            ),
            "eligible_path": path,
            "capacity": capacity,
        }


def guard(
    intent: Intent,
    book_ms: int,
    now: int,
    market: MarketObservation | None,
    previous: MarketObservation | None,
) -> str | None:
    if market is None or market.ticker != intent.order.ticker:
        return "market_status_unavailable"
    if previous and not market.can_replace(previous):
        return "market_observation_rejected"
    if not market.tradable_at(now):
        return "market_status_stale_or_incomplete"
    if not (
        intent.order.created_ts_ms <= book_ms <= market.requested_ts_ms <= now
        and now - book_ms <= MAX_ENTRY_OBSERVATION_AGE_MS
    ):
        return "rejected_market_state"
    if now >= intent.order.created_ts_ms + 1000 * intent.config["fill_horizon_seconds"]:
        return "horizon_elapsed_at_validation"
    submitted = json.loads(intent.row["market_at_submit"] or "null")
    if not submitted or not MarketObservation(**submitted).tradable_at(intent.order.created_ts_ms):
        return "unverified_submission"
    if now >= submitted["close_ts_ms"]:
        return "close_time"
    # Only use a recorded cancellation once its ledger time has arrived.
    if (
        intent.row["status"] == "CANCELED"
        and intent.row["last_update_ts_ms"] is not None
        and now >= intent.row["last_update_ts_ms"]
    ):
        return "entry_inactive_at_validation"
    return None


def draw(seed: int, session: str, sequence: int, intent_id: str) -> float:
    key = json.dumps([seed, session, sequence, intent_id], separators=(",", ":")).encode()
    return (int.from_bytes(hashlib.sha256(key).digest()[:8], "big") >> 11) / (1 << 53)


@dataclass
class Model:
    name: str
    require_raw_consistency: bool = False
    mode: Mode = Mode.B2
    strict: bool = False
    opportunities: Counter[str] = field(default_factory=Counter)
    candidates: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)
    sizes: Counter[str] = field(default_factory=Counter)
    fills: list[dict[str, Any]] = field(default_factory=list)
    pending_marks: dict[str, list[tuple[dict[str, Any], Intent]]] = field(default_factory=dict)

    def evaluate(
        self,
        intent: Intent,
        d: dict[str, Any],
        event: dict[str, Any],
        book_id: int,
        veto: str | None,
        uniform: float,
        scale: float,
    ) -> None:
        order = intent.order
        path = d["eligible_path"]
        if self.sizes[order.id] >= order.size_contracts or path is None:
            return
        self.opportunities[path] += 1
        if uniform >= min(1, intent.config[PROBABILITY_KEYS[path]] * scale):
            self.rejected["probability_draw_missed"] += 1
            return
        self.candidates[path] += 1
        threshold = intent.config.get("min_edge_cents_" + path)
        reason = veto
        if not reason and path not in POLICIES[self.name]:
            reason = f"fill_path_{path}_disabled"
        if not reason and threshold is not None and order.net_edge < threshold:
            reason = f"fill_path_{path}_min_edge"
        if not reason and self.require_raw_consistency and not d["raw_top_matches_model"]:
            reason = "raw_top_disagrees_with_simulator"
        if not reason and self.strict:
            reason = strict_liquidity_rejection(d["consistency"])
        if reason or intent.fee is None:
            self.rejected[reason or "unknown_fees"] += 1
            return
        size = min(order.size_contracts - self.sizes[order.id], d["capacity"])
        self.sizes[order.id] += size
        fill = {
            "raw_top_matches_model": d["raw_top_matches_model"],
            "raw_bid_cents": d["raw_bid_cents"],
            "simulator_bid_cents": d["market_best_bid"],
            "intent_id": order.id,
            "ticker": order.ticker,
            "side": order.side,
            "path": path,
            "book_id": book_id,
            "validation_id": event["id"],
            "session": event["session_id"],
            "received_ns": event["received_ns"],
            "size": size,
            "price_cents": order.quote_price_cents,
            "entry_fee_cents": intent.fee.fee_cents(
                order.quote_price_cents, size, liquidity=intent.row["fee_mode"]
            ),
            "time_to_fill_ms": event["received_ns"] // 10**6 - order.created_ts_ms,
            "mark_5s": None,
        }
        self.fills.append(fill)
        if self.mode != Mode.B2:
            fill["consistency"] = d["consistency"]
        self.pending_marks.setdefault(order.ticker, []).append((fill, intent))

    def mark(self, event: dict[str, Any], body: dict[str, Any]) -> None:
        ticker = event["ticker"]
        pending = self.pending_marks.pop(ticker, [])
        for fill, intent in pending:
            age = (event["received_ns"] - fill["received_ns"]) / 10**9
            if age > 7:
                continue
            if age >= 5 and intent.fee:
                quote_fn = (
                    orderbook.quote_liquidation
                    if self.mode == Mode.CANONICAL
                    else b1_orderbook.quote_liquidation
                )
                quote = quote_fn(
                    body,
                    intent.order.side,
                    fill["size"],
                    event["received_ns"] // 10**6,
                    fee_profile=intent.fee,
                )
                if quote:
                    gross = (quote.price_cents - fill["price_cents"]) * fill["size"]
                    fill["mark_5s"] = {
                        "book_id": event["id"],
                        "age_seconds": age,
                        "bid_vwap_cents": quote.price_cents,
                        "gross_cents": gross,
                        "exit_fee_cents": quote.exit_fee_cents,
                        "net_cents": round(
                            gross - fill["entry_fee_cents"] - quote.exit_fee_cents, 6
                        ),
                    }
                    continue
            self.pending_marks.setdefault(ticker, []).append((fill, intent))

    def report(self) -> dict[str, Any]:
        marks = [f["mark_5s"] for f in self.fills if f["mark_5s"] is not None]
        exposure: Counter[str] = Counter()
        for f in self.fills:
            exposure[f["ticker"]] += f["size"]
        return {
            "version": MODEL_VERSION if self.mode == Mode.B2 else f"b3a-{self.mode.value}-v1",
            **(
                {"fills_by_path": Counter(f["path"] for f in self.fills)}
                if self.mode != Mode.B2
                else {}
            ),
            "allowed_paths": POLICIES[self.name],
            "eligible_opportunities_by_path": self.opportunities,
            "candidates_by_path": self.candidates,
            "reasons": self.rejected,
            "hypothetical_fills": len(self.fills),
            "filled_contracts_by_ticker": exposure,
            "top_ticker_contract_share": max(exposure.values()) / exposure.total()
            if exposure
            else None,
            "mean_time_to_fill_ms": sum(f["time_to_fill_ms"] for f in self.fills) / len(self.fills)
            if self.fills
            else None,
            "fills_with_raw_top_disagreement": sum(
                not f["raw_top_matches_model"] for f in self.fills
            ),
            "fill_details": self.fills,
            "markout_samples": len(marks),
            "missing_markouts": len(self.fills) - len(marks),
            "mean_net_markout_cents": sum(m["net_cents"] for m in marks) / len(marks)
            if marks
            else None,
            "adverse_gross_marks": sum(m["gross_cents"] < 0 for m in marks),
        }
