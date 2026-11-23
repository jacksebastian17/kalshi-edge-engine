"""Decision-anchored, as-of book diagnostics on a verified Stage 2A tape."""

from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal
from operator import attrgetter
from statistics import median

from tools.markout_fees import FeeEvidence
from tools.shadow_tape import Quote, Tape

PROTOCOL = "b3b2-decision-markouts-v1"
HORIZONS = (5, 15, 30, 60)
SECOND = 10**9
MAX_AGE_NS = 2 * SECOND


def midpoint(quote: Quote, side: str) -> Decimal | None:
    bid, qty = quote.bid(side)
    opposite, other_qty = quote.bid("no" if side == "yes" else "yes")
    return (bid + 100 - opposite) / 2 if qty and other_qty and bid + opposite <= 100 else None


def fee_at(
    evidence: FeeEvidence | None, digest: str, row: dict, at: int, price: Decimal, *, sale: bool
) -> dict | None:
    resolved = (
        evidence.resolve(digest, row["market"], row["event_ticker"], at) if evidence else None
    )
    if resolved is None:
        return None
    profile, refs = resolved
    amount = profile.fee_cents(float(price), 1, liquidity="taker" if sale else "maker", sale=sale)
    return {"cents": Decimal(str(amount)), "profile": profile.audit(), "receipt_indices": refs}


def liquidation(
    proceeds: Decimal | None, entry: Decimal, entry_fee: dict | None, exit_fee: dict | None
) -> dict:
    net = None
    net_markout = None
    if proceeds is None:
        status = "no_liquidation_quote"
    elif exit_fee is None:
        status = "missing_exit_profile"
    else:
        net = proceeds - exit_fee["cents"]
        status = "missing_entry_profile"
        if entry_fee is not None:
            net_markout = net - entry - entry_fee["cents"]
            status = "verified"
    return {
        "gross_liquidation_cents": proceeds,
        "gross_markout_cents": proceeds - entry if proceeds is not None else None,
        "taker_fee": exit_fee,
        "net_liquidation_cents": net,
        "net_markout_cents": net_markout,
        "fee_status": status,
    }


def attach(
    tape: Tape,
    row: dict,
    digest: str,
    fees: FeeEvidence | None,
    *,
    reference_bid: Decimal | None = None,
) -> None:
    market = tape.markets[row["market"]]
    anchor, decision, side = row["decision_mono_ns"], row["decision_asof_record"], row["side"]
    entry = row["price_cents"]
    original_bid = entry if reference_bid is None else reference_bid
    baseline = market.depth_at(decision)
    assert baseline is not None and tape.end is not None
    entry_whole = baseline.whole_bid(side)
    entry_mid = midpoint(market.at(decision).quote, side)
    entry_fee = fee_at(fees, digest, row, anchor, entry, sale=False)
    row["markout_baseline"] = {
        "depth_record": baseline.record,
        "receipt_mono_ns": tape.mono[baseline.record],
        "age_ns": anchor - tape.mono[baseline.record],
        "sid": baseline.sid,
        "seq": baseline.seq,
        "executable_bid_cents": entry_whole,
        "midpoint_cents": entry_mid,
        "maker_fee": entry_fee,
    }
    marks = []
    first_change = bisect_right(market.history, decision, key=attrgetter("record"))
    ix = bisect_right(market.breaks, decision)
    boundary = market.breaks[ix] if ix < len(market.breaks) else None
    for index, seconds in enumerate(HORIZONS):
        previous_deadline = anchor + (HORIZONS[index - 1] * SECOND if index else 0)
        target = anchor + seconds * SECOND
        mark: dict = dict.fromkeys(
            (
                "asof_record",
                "source",
                "age_ns",
                "stale",
                "updated_since_decision",
                "updated_since_previous_horizon",
                "same_top_as_decision",
                "first_departure_record",
                "after_quote_departure",
                "source_relative_to_departure",
                "original_quote_available",
                "displayed_bid_cents",
                "displayed_bid_contracts",
                "crossed_book",
                "two_sided_midpoint_available",
                "midpoint_cents",
                "midpoint_change_cents",
                "sufficient_displayed_quantity",
                "gross_liquidation_cents",
                "executable_bid_change_cents",
                "gross_markout_cents",
                "taker_fee",
                "net_liquidation_cents",
                "net_markout_cents",
                "fee_status",
            )
        )
        mark.update(horizon_seconds=seconds, target_mono_ns=target, status="capture_ended")
        marks.append(mark)
        if target > tape.end:
            continue
        cutoff = tape.cutoff(target)
        mark["asof_record"] = cutoff
        if boundary is not None and boundary <= cutoff:
            mark.update(status="interrupted", interruption_record=boundary)
            continue
        if any(a <= cutoff and b >= decision for a, b in tape.suspect_ranges):
            mark["status"] = "conflicting_source"
            continue
        state, depth = market.at(cutoff), market.depth_at(cutoff)
        if depth is None or not state.quote.ready:
            mark["status"] = "missing_book"
            continue
        quote = state.quote
        bid, qty = quote.bid(side)
        receipt = tape.mono[depth.record]
        age = target - receipt
        last = bisect_right(market.history, cutoff, key=attrgetter("record"))
        departure = next(
            (
                s.record
                for s in market.history[first_change:last]
                if s.quote.bid(side)[0] != original_bid or not s.quote.passive()
            ),
            None,
        )
        mid = midpoint(quote, side)
        crossed = quote.yes[0] + quote.no[0] > 100
        gross = None if crossed else depth.whole_bid(side)
        exit_fee = (
            fee_at(fees, digest, row, target, gross, sale=True) if gross is not None else None
        )
        relation = "no_departure"
        if departure is not None:
            relation = "at_departure" if depth.record == departure else "after_departure"
        mark.update(
            status="stale" if age > MAX_AGE_NS else "fresh",
            source={
                "record": depth.record,
                "receipt_mono_ns": receipt,
                "sid": depth.sid,
                "seq": depth.seq,
                "snapshot_record": state.snapshot_record,
                "top_state_record": state.record,
            },
            age_ns=age,
            stale=age > MAX_AGE_NS,
            updated_since_decision=depth.record > decision,
            updated_since_previous_horizon=receipt > previous_deadline,
            same_top_as_decision=(bid, qty) == market.at(decision).quote.bid(side),
            first_departure_record=departure,
            after_quote_departure=departure is not None,
            source_relative_to_departure=relation,
            original_quote_available=bid == original_bid and quote.passive(),
            displayed_bid_cents=bid if qty else None,
            displayed_bid_contracts=qty,
            crossed_book=crossed,
            two_sided_midpoint_available=mid is not None,
            midpoint_cents=mid,
            midpoint_change_cents=mid - entry_mid
            if mid is not None and entry_mid is not None
            else None,
            sufficient_displayed_quantity=depth.whole_bid(side) is not None,
            executable_bid_change_cents=gross - entry_whole
            if gross is not None and entry_whole is not None
            else None,
            **liquidation(gross, entry, entry_fee, exit_fee),
        )
    row["markouts"] = marks
    for scenario in row["scenarios"]:
        scenario["ambiguous_attribution"] = (
            bool(scenario["ambiguous_group_records"] or scenario["boundary_split_pairs"])
            if "ambiguous_group_records" in scenario
            else None
        )


def tally(marks: list[dict]) -> dict:
    fresh = [m for m in marks if m["status"] == "fresh"]
    moves = [m["gross_markout_cents"] for m in fresh if m["gross_markout_cents"] is not None]
    net = [m["net_markout_cents"] for m in fresh if m["net_markout_cents"] is not None]
    bid_changes = [
        m["executable_bid_change_cents"]
        for m in fresh
        if m["executable_bid_change_cents"] is not None
    ]
    mid_changes = [
        m["midpoint_change_cents"] for m in fresh if m["midpoint_change_cents"] is not None
    ]
    return {
        "requested": len(marks),
        "fresh_book": len(fresh),
        "stale_book": sum(m["status"] == "stale" for m in marks),
        "missing_book": sum(m["source"] is None for m in marks),
        "carried_since_decision": sum(m["updated_since_decision"] is False for m in marks),
        "fresh_midpoint": sum(m["midpoint_cents"] is not None for m in fresh),
        "fresh_liquidation": len(moves),
        "fresh_net_markout": len(net),
        "negative": sum(v < 0 for v in moves),
        "flat": sum(v == 0 for v in moves),
        "positive": sum(v > 0 for v in moves),
        "median_gross_markout_cents": median(moves) if moves else None,
        "median_net_markout_cents": median(net) if net else None,
        "median_executable_bid_change_cents": median(bid_changes) if bid_changes else None,
        "median_midpoint_change_cents": median(mid_changes) if mid_changes else None,
    }


def report(rows: list[dict], selected_games: int, fee_sha256: str | None) -> dict:
    coverage = {}
    strata = []
    for role in ("primary", "secondary"):
        group = [r for r in rows if r["role"] == role]
        by_horizon = {}
        for i, horizon in enumerate(HORIZONS):
            marks = [r["markouts"][i] for r in group]
            by_horizon[str(horizon)] = {
                **tally(marks),
                "status": dict(Counter(m["status"] for m in marks)),
            }
        coverage[role] = by_horizon
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for r in group:
            for s in r["scenarios"]:
                key = (
                    s["delay_ms"],
                    s["classification"],
                    s["public_execution_support"],
                    s["queue_comparison"],
                    s["ambiguous_attribution"],
                )
                grouped[key].append(r)
        for (delay, classification, prints, queue, ambiguous), members in grouped.items():
            strata.append(
                {
                    "role": role,
                    "delay_ms": delay,
                    "classification": classification,
                    "public_execution_support": prints,
                    "queue_comparison": queue,
                    "ambiguous_attribution": ambiguous,
                    "games": len({r["event_ticker"] for r in members}),
                    "horizons": {
                        str(h): tally([r["markouts"][i] for r in members])
                        for i, h in enumerate(HORIZONS)
                    },
                }
            )
    five = coverage["primary"]["5"]
    return {
        "protocol": PROTOCOL,
        "max_depth_receipt_age_ns": MAX_AGE_NS,
        "fee_receipts_sha256": fee_sha256,
        "coverage_by_role_and_horizon_seconds": coverage,
        "scenario_strata": strata,
        "tape_anchored_5s_primary": {
            "fresh_book": five["fresh_book"],
            "fresh_one_contract_bid": five["fresh_liquidation"],
            "primary_decisions": five["requested"],
            "selected_games": selected_games,
        },
    }
