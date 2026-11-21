"""Causal per-order queue reconstruction and independent conditional execution experiments."""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from kalshi_edge_engine.engine.fees import LEGACY_FEE_VERSION, SERIES_FEE_VERSION, FeeProfile
from kalshi_edge_engine.engine.orderbook import (
    _extract_prices_from,
    displayed_quote_size,
    quote_liquidation,
)
from kalshi_edge_engine.engine.paper_sim import (
    OrderRequest,
    _conservative_participation_fill_size,
    _estimate_moved_through_available_volume,
    _estimate_price_improving_arrival_volume,
    compute_simulator_diagnostics,
)
from kalshi_edge_engine.kalshi.market import MAX_ENTRY_OBSERVATION_AGE_MS, MarketObservation

POLICIES = {
    "baseline": ("queue_cleared",),
    "price_improving_research": ("queue_cleared", "price_improves"),
    "moved_through_research": ("queue_cleared", "moved_through"),
}
MODEL_VERSION = "b1-conditional-entry-v1"


def raw_best_level(body: dict[str, Any], side: str) -> tuple[float, float] | None:
    """Highest positive-size level, retaining fractional contract counts from the HTTP book."""
    yes, no, dollars = _extract_prices_from(body)
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


@dataclass
class Queue:
    ahead: int = 0
    last_size: int | None = None
    known: bool = False

    def observe(self, intent: Intent, body: dict[str, Any]) -> dict[str, Any]:
        order = intent.order
        volume = intent.config["simulated_volume_per_poll"]
        d = compute_simulator_diagnostics(order, body, self.ahead, self.last_size, volume)
        eligibility = d["eligibility"]
        at_quote = eligibility["at_best_price"] and not d["price_improves"]
        known_before = self.known
        path = None
        capacity = 0
        before = self.ahead
        if at_quote:
            kind = "at_displayed_quote"
            if self.last_size is None:
                before = d["best_size_at_bid"]
            self.last_size = d["best_size_at_bid"]
            self.ahead = max(0, before - d["traded_volume_estimate"])
            capacity = max(0, d["traded_volume_estimate"] - before)
            if capacity > 0 and self.known:
                path = "queue_cleared"
        else:
            self.last_size = None
            self.ahead = 0
            self.known = True  # B1 resets its queue at every non-displayed-best poll.
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
        raw = raw_best_level(body, order.side)
        opposite = raw_best_level(body, "NO" if order.side == "YES" else "YES")
        raw_bid = raw[0] if raw else None
        raw_cross = (100 - opposite[0]) if opposite else None
        after = self.ahead if at_quote else d["queue_ahead_before"]
        eligibility["queue_cleared"] = at_quote and after <= 0
        return {
            **d,
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
            "displayed_quote_size": displayed_quote_size(body, order.side, order.quote_price_cents),
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
        self.pending_marks.setdefault(order.ticker, []).append((fill, intent))

    def mark(self, event: dict[str, Any], body: dict[str, Any]) -> None:
        ticker = event["ticker"]
        pending = self.pending_marks.pop(ticker, [])
        for fill, intent in pending:
            age = (event["received_ns"] - fill["received_ns"]) / 10**9
            if age > 7:
                continue
            if age >= 5 and intent.fee:
                quote = quote_liquidation(
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
            "version": MODEL_VERSION,
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
