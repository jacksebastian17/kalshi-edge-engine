"""Offline Stage 2A receipt-time feasibility study. Run with python -m tools.shadow_feasibility."""

import argparse
import json
import tempfile
from bisect import bisect_right
from collections import Counter
from decimal import Decimal
from operator import attrgetter
from pathlib import Path

from tools.microstructure import journal
from tools.shadow_tape import (
    CAPTURE_BUILD,
    CAPTURE_COMMIT,
    REPLAY_ORDERBOOK,
    SIDES,
    ZERO,
    Key,
    Tape,
    project,
    source_hashes,
    verified_source,
)

MINUTE = 60_000_000_000
DELAYS = (0, 100, 250, 500)
PROTOCOL = "b3b2-stage2a-best-bid-v1"


def after(records: list, n: int) -> int:
    return bisect_right(records, n, key=attrgetter("record"))


def observations(tape: Tape, key: Key, start: int, end: int) -> dict:
    flows = tape.flows.get(key, [])
    prints, adds, decreases, exact, split = [], [], [], [], []
    ambiguous: set[int] = set()
    added = removed = traded = ZERO
    for f in flows[after(flows, start) : after(flows, end)]:
        if f.trade_id:
            traded += f.quantity
            prints.append({"record": f.record, "id": f.trade_id, "quantity": f.quantity})
        elif f.quantity < 0:
            removed -= f.quantity
            decreases.append(f.record)
        else:
            added += f.quantity
            adds.append(f.record)
        peer = tape.peers.get(f.record)
        if peer is not None:
            if start < peer <= end:
                if f.trade_id:
                    exact.append({"print": f.record, "decrease": peer})
            else:
                split.append({"inside": f.record, "outside": peer})
        if f.record in tape.ambiguous:
            ambiguous.add(tape.ambiguous[f.record])
    return {
        "print_contracts": traded,
        "addition_contracts": added,
        "removal_contracts": removed,
        "print_receipts": prints,
        "addition_records": adds,
        "decrease_records": decreases,
        "exact_pairs": exact,
        "boundary_split_pairs": split,
        "ambiguous_group_records": sorted(ambiguous),
        "decreases_without_exact_in_window": len(decreases) - len(exact),
    }


def scenario(tape: Tape, key: Key, decision_ns: int, delay: int) -> dict:
    ticker, side, price = key
    market = tape.markets[ticker]
    assert tape.end is not None
    decision = tape.cutoff(decision_ns)
    arrival_ns = decision_ns + delay * 1_000_000
    arrival = tape.cutoff(arrival_ns)
    horizon_ns = decision_ns + MINUTE
    limit = tape.cutoff(horizon_ns)
    break_ix = bisect_right(market.breaks, decision)
    boundary = market.breaks[break_ix] if break_ix < len(market.breaks) else None
    book = market.at(arrival)
    bid, ahead = book.quote.bid(side)
    result = {
        "delay_ms": delay,
        "assumed_arrival_mono_ns": arrival_ns,
        "horizon_mono_ns": horizon_ns,
        "archive_reaches_horizon": horizon_ns <= tape.end,
        "arrival_asof_record": arrival if arrival_ns <= tape.end else None,
        "arrival_book_record": book.record if arrival_ns <= tape.end else None,
        "arrival_best_cents": bid if arrival_ns <= tape.end else None,
        "arrival_ahead_contracts": None,
        "public_execution_support": None,
        "queue_comparison": "UNEVALUATED",
        "classification": "DATA_INCOMPLETE",
    }
    if (
        arrival_ns >= tape.end
        or (boundary is not None and boundary <= arrival)
        or not book.quote.ready
    ):
        return {
            **result,
            "exit": "interruption_before_arrival"
            if boundary is not None and boundary <= arrival
            else "arrival_unobserved",
            "exit_record": boundary
            if boundary is not None and boundary <= arrival
            else tape.end_record,
        }
    if any(first <= arrival and last >= decision for first, last in tape.suspect_ranges):
        return {**result, "exit": "conflicting_source_records", "exit_record": arrival}
    if bid != price or not book.quote.passive():
        return {
            **result,
            "exit": "quote_ineligible_at_arrival",
            "exit_record": arrival,
            "classification": "QUOTE_INELIGIBLE_AT_ARRIVAL",
        }

    end = limit
    reason = "expired" if horizon_ns <= tape.end else "capture_ended_early"
    if boundary is not None and boundary <= end:
        end, reason = boundary, "feed_interruption"
    for state in market.history[after(market.history, arrival) : after(market.history, end)]:
        if state.record == boundary or not state.quote.ready:
            end, reason = state.record, "feed_interruption"
            break
        if state.quote.bid(side)[0] != price or not state.quote.passive():
            end, reason = state.record, "quote_moved"
            break
    # The fault record cannot supply usable flow. The price-changing update can.
    inclusive_end = end - 1 if reason == "feed_interruption" else end
    evidence = observations(tape, key, arrival, inclusive_end)
    volume = evidence["print_contracts"]
    threshold = ahead + 1
    classification = "PUBLIC_EXECUTION_AT_PRICE" if volume else "NO_OBSERVED_EXECUTION_SUPPORT"
    if reason in ("feed_interruption", "capture_ended_early"):
        classification = "DATA_INCOMPLETE"
    comparison = "NO_COMPATIBLE_PRINTS"
    if volume:
        comparison = (
            "PRINTS_REACH_DISPLAYED_AHEAD_PLUS_ONE"
            if volume >= threshold
            else "PRINTS_BELOW_DISPLAYED_AHEAD_PLUS_ONE"
        )
    return {
        **result,
        **evidence,
        "exit": reason,
        "exit_record": end,
        "exit_mono_ns": horizon_ns if reason == "expired" else tape.mono[end],
        "exit_best_cents": market.at(end).quote.bid(side)[0],
        "arrival_ahead_contracts": ahead,
        "public_execution_support": bool(volume),
        "classification": classification,
        "queue_comparison": comparison,
        "removals_reach_displayed_ahead_plus_one": evidence["removal_contracts"] >= threshold,
    }


def cohort(tape: Tape, events: dict[str, str]) -> tuple[list[dict], list[dict]]:
    assert tape.start is not None and tape.end is not None
    episodes, excluded = [], []
    for tick in range(tape.start + MINUTE, tape.end, MINUTE):
        record = tape.cutoff(tick)
        for ticker, market in tape.markets.items():
            state = market.at(record)
            for side in SIDES:
                decision = {
                    "event_ticker": events[ticker],
                    "market": ticker,
                    "side": side,
                    "decision_mono_ns": tick,
                    "decision_asof_record": record,
                    "receipt_mono_ns": tape.mono[record],
                    "book_record": state.record,
                    "snapshot_record": state.snapshot_record,
                }
                if not state.quote.ready or not state.quote.passive():
                    excluded.append(
                        {
                            **decision,
                            "reason": "missing_feed_baseline"
                            if not state.quote.ready
                            else "no_passive_two_sided_quote",
                        }
                    )
                    continue
                price, quantity = state.quote.bid(side)
                episodes.append(
                    {
                        **decision,
                        "price_cents": price,
                        "displayed_contracts": quantity,
                        "size_contracts": 1,
                        "scenarios": [
                            scenario(tape, (ticker, side, price), tick, d) for d in DELAYS
                        ],
                    }
                )
    return episodes, excluded


def sensitivity(episodes: list[dict]) -> dict:
    result = {}
    for i, delay in enumerate(DELAYS):
        rows = [e["scenarios"][i] for e in episodes]
        observed = [r for r in rows if r["public_execution_support"] is not None]
        removal_only = [
            r
            for r in observed
            if r["removals_reach_displayed_ahead_plus_one"]
            and r["queue_comparison"] != "PRINTS_REACH_DISPLAYED_AHEAD_PLUS_ONE"
        ]
        result[str(delay)] = {
            "episodes": len(rows),
            "eligible_at_assumed_arrival": len(observed),
            "classifications": dict(Counter(r["classification"] for r in rows)),
            "exits": dict(Counter(r["exit"] for r in rows)),
            "queue_comparison": dict(Counter(r["queue_comparison"] for r in rows)),
            "episodes_with_public_prints": sum(bool(r["public_execution_support"]) for r in rows),
            "episodes_with_exact_pairs": sum(bool(r["exact_pairs"]) for r in observed),
            "episodes_with_ambiguous_groups": sum(
                bool(r["ambiguous_group_records"]) for r in observed
            ),
            "episodes_with_boundary_split_pairs": sum(
                bool(r["boundary_split_pairs"]) for r in observed
            ),
            "removal_only_threshold": len(removal_only),
            "removal_only_threshold_complete": sum(
                r["classification"] != "DATA_INCOMPLETE" for r in removal_only
            ),
            "classification_changed_from_zero_delay": sum(
                r["classification"] != e["scenarios"][0]["classification"]
                for r, e in zip(rows, episodes, strict=True)
            ),
            "print_contracts_in_exposures": sum((r["print_contracts"] for r in observed), ZERO),
            "removal_contracts_in_exposures": sum((r["removal_contracts"] for r in observed), ZERO),
        }
    return result


def flow_totals(tape: Tape) -> dict:
    counts: Counter[str] = Counter()
    removed = exact = traded = traded_at_decrease_levels = ZERO
    for flows in tape.flows.values():
        has_decrease = any(f.quantity < 0 for f in flows)
        for f in flows:
            if f.trade_id:
                counts["classified_non_block_prints"] += 1
                traded += f.quantity
                if has_decrease:
                    traded_at_decrease_levels += f.quantity
            elif f.quantity < 0:
                counts["displayed_decreases"] += 1
                removed -= f.quantity
                if f.record in tape.peers:
                    exact -= f.quantity
                else:
                    counts["decreases_without_exact_pair"] += 1
    return {
        **counts,
        "classified_non_block_contracts": traded,
        "removed_contracts": removed,
        "exact_coincident_contracts": exact,
        "removed_contracts_without_exact_pair": removed - exact,
        "print_contracts_at_levels_with_a_decrease": traded_at_decrease_levels,
    }


def analyze(archive: Path) -> tuple[dict, list[dict]]:
    meta, verified, before = verified_source(archive)
    tape = project(archive / journal.LOG, verified)
    events = {m["ticker"]: m["event_ticker"] for m in meta["market_checks"]}
    if set(events) != set(tape.markets) or not all(
        isinstance(t, str) and t for t in events.values()
    ):
        raise ValueError("Missing capture event identity")
    episodes, excluded = cohort(tape, events)
    after_hashes = source_hashes(archive)
    if before != after_hashes:
        raise ValueError("Source changed during analysis")
    return {
        "protocol": PROTOCOL,
        "parameters": {
            "grid_seconds": 60,
            "horizon_from_decision_seconds": 60,
            "size_contracts": 1,
            "delay_ms": DELAYS,
            "time_domain": "local_monotonic_receipts",
            "exposure": "until_first_quote_departure_or_expiry",
        },
        "session": meta["session"],
        "event_by_market": events,
        "underlying_events": len(set(events.values())),
        "decision_times": len({e["decision_mono_ns"] for e in episodes}),
        "eligible_episodes": len(episodes),
        "excluded_opportunities": excluded,
        "scheduled_opportunities": len(episodes) + len(excluded),
        "source": {
            "capture_commit": CAPTURE_COMMIT,
            "capture_build_sha256": CAPTURE_BUILD,
            "capture_build": meta["build"],
            "replay_orderbook_sha256": REPLAY_ORDERBOOK,
            "before_sha256": before,
            "after_sha256": after_hashes,
            "durable_records": verified["durable_records"],
            "raw_messages": verified["counts"]["messages"],
            "deltas": verified["deltas_received"],
            "unique_public_trades": verified["unique_public_trades"],
            "segments": verified["segments"],
            "segment_faults": verified["segment_faults"],
            "clock_regressions": verified["market_clock_boundaries"],
            "sequence_gaps": verified["sequence_gaps"],
            "reconnects": verified["reconnects"],
            "clock_alignment_status": verified["clock_alignment_status"],
            "median_send_to_receive_ms": verified["server_send_to_local_receive_ms"]["median"],
        },
        "full_capture_flow": {
            **flow_totals(tape),
            "exact_pairs": len(tape.peers) // 2,
            "ambiguous_groups": verified["ambiguous_candidate_groups"],
            "ambiguous_receipts": len(tape.ambiguous),
            "unmatched_non_block_prints": verified["unmatched_non_block_prints"],
        },
        "sensitivity_by_delay_ms": sensitivity(episodes),
        "interpretation": "Local receipt-time scenarios. Queue position, causal attribution, venue arrival and fill probability are unidentified.",
    }, episodes


def encode(value: object) -> str:
    def decimal_string(v: object) -> str:
        if isinstance(v, Decimal):
            return format(v, "f")
        raise TypeError(type(v).__name__)

    return json.dumps(
        value, default=decimal_string, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def publish(output: Path, summary: dict, episodes: list[dict], detail: bool) -> None:
    if output.exists():
        raise FileExistsError("Choose a fresh output directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as staging:
        bundle = Path(staging) / "results"
        bundle.mkdir()
        (bundle / "summary.json").write_bytes((encode(summary) + "\n").encode())
        if detail:
            with (bundle / "episodes.jsonl").open("xb") as detail_file:
                for row in episodes:
                    detail_file.write((encode(row) + "\n").encode())
        bundle.rename(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Fresh result directory outside the source run")
    parser.add_argument("--detail", action="store_true", help="Include per-episode source records")
    args = parser.parse_args()
    archive = args.archive.absolute()
    output = args.output.resolve() if args.output else None
    if output and (
        output.exists()
        or output.is_relative_to(archive if archive.name != "backup" else archive.parent)
    ):
        parser.error("Output must be a fresh directory outside the source run")
    if args.detail and not output:
        parser.error("--detail requires --output")
    summary, episodes = analyze(archive)
    if output:
        publish(output, summary, episodes, args.detail)
    print(encode(summary))


if __name__ == "__main__":
    main()
