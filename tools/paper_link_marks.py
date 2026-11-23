"""Tape observations for real proposals, including passive prices inside the displayed spread."""

from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
from operator import itemgetter

from tools.markout_fees import FeeEvidence
from tools.shadow_feasibility import DELAYS, MINUTE, after, observations
from tools.shadow_markouts import SECOND, attach
from tools.shadow_tape import Market, Tape


def classified_prints(tape: Tape) -> dict[str, list[int]]:
    by_market: dict[str, list[int]] = defaultdict(list)
    for (market, _, _), records in tape.flows.items():
        by_market[market].extend(f.record for f in records if f.trade_id)
    return {m: sorted(records) for m, records in by_market.items()}


def broken(tape: Tape, market: Market, first: int, last: int) -> bool:
    i = bisect_right(market.breaks, first)
    return (i < len(market.breaks) and market.breaks[i] <= last) or any(
        a <= last and b >= first for a, b in tape.suspect_ranges
    )


def price_marks(
    tape: Tape, row: dict, at: int, record: int, digest: str, fees: FeeEvidence | None
) -> dict:
    book = tape.markets[row["ticker"]].at(record)
    sample = {
        "market": row["ticker"],
        "event_ticker": row["game"],
        "side": row["side"],
        "scenarios": [],
        "decision_mono_ns": at,
        "decision_asof_record": record,
        "price_cents": Decimal(row["price_cents"]),
    }
    attach(tape, sample, digest, fees, reference_bid=book.quote.bid(row["side"])[0])
    return {
        "anchor_mono_ns": at,
        "asof_record": record,
        "unit_contracts": 1,
        "baseline": sample["markout_baseline"],
        "horizons": sample["markouts"],
    }


def link(
    tape: Tape, row: dict, digest: str, fees: FeeEvidence | None, prints: dict[str, list[int]]
) -> None:
    assert tape.start is not None and tape.end is not None
    at = row["decision_mono_ns"]
    ticker, side, price = row["ticker"], row["side"], Decimal(row["price_cents"])
    if not tape.start <= at < tape.end:
        row["link_status"] = "outside_capture"
        return
    market = tape.markets[ticker]
    record = tape.cutoff(at)
    if tape.mono[record] == at:
        row["link_status"] = "ambiguous_equal_receipt_clock"
        return
    book = market.at(record)
    start = tape.cutoff(at - 30 * SECOND)
    trade_records = prints.get(ticker, [])
    nprints = bisect_right(trade_records, record) - bisect_right(trade_records, start)
    activity_ok = (
        at - 30 * SECOND >= tape.start
        and market.at(start).quote.ready
        and not broken(tape, market, start, record)
    )
    row["predecision_30s"] = {
        "eligible_nonblock_prints": nprints,
        "accepted_depth_receipts": after(market.depth, record) - after(market.depth, start),
        "status": ("active" if nprints else "quiet") if activity_ok else "incomplete",
    }
    if not book.quote.ready or broken(tape, market, record, record):
        row["link_status"] = "missing_or_conflicted_book"
        return
    row["link_status"] = "linked"
    row["decision_book"] = {
        "snapshot_record": book.snapshot_record,
        "top_record": book.record,
        "yes": book.quote.yes,
        "no": book.quote.no,
    }
    row["decision_marks"] = price_marks(tape, row, at, record, digest, fees)
    horizons = {}

    def mark_ref(ns: int, n: int) -> str:
        key = f"{ns}/{n}"
        if key not in horizons:
            horizons[key] = price_marks(tape, row, ns, n, digest, fees)
        return key

    key = (ticker, side, price)
    opposite = "no" if side == "yes" else "yes"
    deadline = at + MINUTE
    limit = tape.cutoff(deadline)
    for delay in DELAYS:
        ns = at + delay * 1_000_000
        arrival = tape.cutoff(ns)
        s: dict = {
            "delay_ms": delay,
            "arrival_mono_ns": ns,
            "status": "incomplete",
            "public_execution_support": None,
            "hypothetical_entry_marks": None,
            "print_supported_entry_marks": None,
        }
        row["scenarios"].append(s)
        if ns >= tape.end or broken(tape, market, record, arrival):
            s["reason"] = "arrival_unobserved"
            continue
        quote = market.at(arrival).quote
        best, _ = quote.bid(side)
        if not quote.ready or not quote.passive() or price >= 100 - quote.bid(opposite)[0]:
            s.update(status="ineligible", reason="not_observably_passive")
            continue
        queue = market.queues[side, price]
        _, ahead, better = queue[bisect_right(queue, arrival, key=itemgetter(0)) - 1]
        s.update(
            arrival_asof_record=arrival,
            displayed_at_price=ahead,
            displayed_better_priced=better,
            price_location="at_best"
            if price == best
            else "inside_spread"
            if price > best
            else "behind_best",
            hypothetical_entry_marks=mark_ref(ns, arrival),
        )
        end = limit
        reason = "expired" if deadline <= tape.end else "capture_ended"
        i = bisect_right(market.breaks, arrival)
        if i < len(market.breaks) and market.breaks[i] <= end:
            end, reason = market.breaks[i] - 1, "feed_break"
        for state in market.history[after(market.history, arrival) : after(market.history, end)]:
            if not state.quote.passive() or price >= 100 - state.quote.bid(opposite)[0]:
                end, reason = state.record - 1, "lost_passivity"
                break
        if broken(tape, market, arrival, end):
            s.update(reason="conflicted_exposure")
            continue
        ev = observations(tape, key, arrival, end)
        volume = ev["print_contracts"]
        s.update(
            status="incomplete" if reason in ("capture_ended", "feed_break") else "observed",
            reason=reason,
            last_included_record=end,
            evidence=ev,
            public_execution_support=True
            if volume
            else False
            if reason in ("expired", "lost_passivity")
            else None,
            prints_reach_ahead_plus_size=volume >= ahead + row["contracts"],
        )
        if ev["print_receipts"]:
            first = ev["print_receipts"][0]["record"]
            s["print_supported_entry_marks"] = mark_ref(tape.mono[first], first)
    row["entry_marks_by_anchor"] = horizons
