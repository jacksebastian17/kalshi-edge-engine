"""Offline game-clustered evaluation of verified B3B2 Stage 2A decisions."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from tools import shadow_markouts
from tools.capture_builds import approved_capture
from tools.markout_fees import FeeEvidence
from tools.microstructure import journal
from tools.shadow_feasibility import PROTOCOL, analyze, encode, market_events, publish


def enrollments(roster: Path | None) -> dict[str, dict]:
    expected: dict[str, dict] = {}
    if roster is None:
        return expected
    if not roster.is_dir():
        raise ValueError("Expected a directory of frozen preflight manifest copies")
    for path in sorted(roster.glob("*.json")):
        raw = path.read_bytes()
        meta = json.loads(raw)
        sid = meta["session"]
        tickers = meta["markets"]
        if (
            meta["format"] != journal.FORMAT
            or not isinstance(sid, str)
            or not sid
            or type(meta["prepared_ns"]) is not int
            or not 2 <= len(set(tickers)) == len(tickers) <= 4
        ):
            raise ValueError("Invalid preflight enrollment")
        entry = {
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "prepared_ns": meta["prepared_ns"],
            "event_by_market": market_events(meta["market_checks"], tickers),
            **approved_capture(meta["build"]),
        }
        if expected.setdefault(sid, entry) != entry:
            raise ValueError(f"Conflicting preflight enrollments for session {sid}")
    if not expected:
        raise ValueError("Empty preflight roster")
    return expected


def decision_key(row: dict) -> tuple:
    # No outcome or future availability fields participate in primary assignment.
    return row["decision_utc_estimate_ns"], row["session"], row["market"], row["side"] != "yes"


def campaign_rows(summary: dict, episodes: list[dict]) -> list[dict]:
    context = summary["campaign_context"]
    offset = context["start_wall_ns"] - context["start_mono_ns"]
    rows = []
    for episode in [*episodes, *summary["excluded_opportunities"]]:
        utc = offset + episode["decision_mono_ns"]
        row = {k: v for k, v in episode.items() if k != "scenarios"}
        row.update(
            session=summary["session"],
            decision_utc_estimate_ns=utc,
            observation_date_utc=datetime.fromtimestamp(utc // 10**9, UTC).date().isoformat(),
        )
        if "scenarios" in episode:
            fields: tuple[str, ...] = (
                "delay_ms",
                "classification",
                "exit",
                "exit_record",
                "public_execution_support",
                "queue_comparison",
            )
            if "markouts" in row:
                fields += (
                    "ambiguous_attribution",
                    "assumed_arrival_mono_ns",
                    "arrival_ahead_contracts",
                    "print_contracts",
                    "print_receipts",
                    "ambiguous_group_records",
                    "boundary_split_pairs",
                )
            row["scenarios"] = [{k: s.get(k) for k in fields} for s in episode["scenarios"]]
        else:
            row["role"] = "excluded"
        rows.append(row)
    return rows


def classifications(rows: list[dict]) -> dict:
    counts: dict[str, dict[str, Counter[str]]] = {"primary": {}, "secondary": {}}
    for r in rows:
        if r["role"] == "excluded":
            continue
        for s in r["scenarios"]:
            counts[r["role"]].setdefault(str(s["delay_ms"]), Counter())[s["classification"]] += 1
    return counts


def usable(row: dict) -> bool:
    window = row["window_coverage"]
    return (
        window["full_horizon"]
        and window["first_interruption_record"] is None
        and not window["conflict"]
        and all(s["classification"] != "DATA_INCOMPLETE" for s in row["scenarios"])
    )


def aggregate(
    archives: list[Path],
    *,
    roster: Path | None = None,
    markouts: bool = False,
    fees: FeeEvidence | None = None,
) -> tuple[dict, list[dict]]:
    expected = enrollments(roster)
    if not archives and not expected:
        raise ValueError("Supply at least one sealed backup or a preflight roster")
    sources: dict[str, dict] = {}
    journals: dict[str, str] = {}
    rows: list[dict] = []
    for path in archives:
        summary, episodes = analyze(
            path.absolute(), campaign_context=True, markouts=markouts, fees=fees
        )
        sid, source, events = summary["session"], summary["source"], summary["event_by_market"]
        hashes = source["before_sha256"]
        if not isinstance(sid, str) or not sid:
            raise ValueError("Missing verified session identity")
        if roster is not None and (
            sid not in expected or expected[sid]["manifest_sha256"] != hashes[journal.MANIFEST]
        ):
            raise ValueError(f"Archive does not match a frozen preflight enrollment: {sid}")
        if sid in sources:
            if sources[sid]["hashes"] != hashes:
                raise ValueError(f"Conflicting verified archives for session {sid}")
            sources[sid]["input_copies"] += 1
            continue
        if journals.setdefault(hashes[journal.LOG], sid) != sid:
            raise ValueError("Identical journal claimed by different session identities")
        sources[sid] = {
            **summary["campaign_context"],
            "hashes": hashes,
            "input_copies": 1,
            "event_by_market": events,
            "capture_build_sha256": source["capture_build_sha256"],
            "scheduled_slots": summary["scheduled_opportunities"],
            "eligible_slots": len(episodes),
            "excluded_slots": len(summary["excluded_opportunities"]),
            "segment_faults": source["segment_faults"],
            "clock_alignment_status": source["clock_alignment_status"],
        }
        rows.extend(campaign_rows(summary, episodes))
        del episodes

    rows.sort(key=decision_key)
    missing = {s: e for s, e in expected.items() if s not in sources}
    sessions_by_game: dict[str, list[str]] = defaultdict(list)
    for sid, info in sorted({**missing, **sources}.items()):
        for game in set(info["event_by_market"].values()):
            sessions_by_game[game].append(sid)
    by_game: dict[str, list[dict]] = defaultdict(list)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_game[r["event_ticker"]].append(r)
        by_date[r["observation_date_utc"]].append(r)

    primary = []
    games = {}
    for game, sessions in sorted(sessions_by_game.items()):
        group = by_game[game]
        eligible = [r for r in group if "scenarios" in r]
        first = next(iter(eligible), None)
        unseen = [s for s in sessions if s in missing]
        # An earlier missing capture could contain the first eligible decision.
        unavailable = first is None or any(
            missing[s]["prepared_ns"] <= first["decision_utc_estimate_ns"] for s in unseen
        )
        for r in eligible:
            r["role"] = "primary" if r is first and not unavailable else "secondary"
        ref = None
        if first is not None and not unavailable:
            primary.append(first)
            ref = {
                k: first[k]
                for k in (
                    "session",
                    "market",
                    "side",
                    "decision_mono_ns",
                    "decision_asof_record",
                    "observation_date_utc",
                )
            }
        overlaps = []
        for a, b in combinations((s for s in sessions if s in sources), 2):
            start = max(sources[a]["start_wall_ns"], sources[b]["start_wall_ns"])
            end = min(sources[a]["end_wall_estimate_ns"], sources[b]["end_wall_estimate_ns"])
            if start < end:
                overlaps.append(
                    {"sessions": [a, b], "from_estimate_ns": start, "until_estimate_ns": end}
                )
        games[game] = {
            "sessions": sessions,
            "overlapping_capture_intervals": overlaps,
            "primary": ref,
            "primary_unavailable_reason": (
                "missing_enrolled_capture" if unseen else "no_eligible_slot"
            )
            if unavailable
            else None,
            "eligible_slots": len(eligible),
            "excluded_slots": len(group) - len(eligible),
            "classifications_by_delay_ms": classifications(group),
        }
    dates = {r["observation_date_utc"] for r in primary}
    n = len(primary)
    total = len(games)
    scored = [r for r in primary if usable(r)]
    complete = len(scored)
    interrupted = sum(
        r["window_coverage"]["first_interruption_record"] is not None
        or r["window_coverage"]["conflict"]
        for r in primary
    )
    gates: dict[str, dict] = {
        "primary_games": {"observed": n, "minimum": 100, "met": n >= 100},
        "primary_observation_dates_utc": {
            "observed": len(dates),
            "minimum": 10,
            "met": len(dates) >= 10,
        },
        "usable_primary_windows": {
            "numerator": complete,
            "denominator_selected_games": total,
            "minimum_percent": 95,
            "met": total > 0 and 100 * complete >= 95 * total,
        },
        "unresolved_continuity_in_scored_windows": {
            "observed": 0,
            "scored_windows": complete,
            "maximum": 0,
            "met": complete > 0,
        },
        "on_time_5s_marks": {
            "numerator": None,
            "denominator_scored_windows": complete,
            "minimum_percent": 95,
            "met": None,
            "reason": "Frozen Stage 2A has no entry-linked 5-second mark measurement",
        },
    }
    return {
        **(
            {"tape_markouts": shadow_markouts.report(rows, total, fees.sha256 if fees else None)}
            if markouts
            else {}
        ),
        "protocol": "b3b2-campaign-v1",
        "episode_protocol": PROTOCOL,
        "assessment": "COVERAGE_MET" if all(g["met"] for g in gates.values()) else "INCONCLUSIVE",
        "measured_sample_and_window_gates_met": all(
            g["met"] for name, g in gates.items() if name != "on_time_5s_marks"
        ),
        "fill_probability": "UNIDENTIFIED",
        "expected_trading_profit": "UNIDENTIFIED",
        "denominator_basis": "frozen_preflight_manifests"
        if roster is not None
        else "supplied_verified_sessions_only",
        "archive_inputs": len(archives),
        "verified_sessions": len(sources),
        "duplicate_inputs": len(archives) - len(sources),
        "enrolled_sessions": len(expected) if roster is not None else None,
        "missing_enrolled_sessions": missing,
        "sources": sources,
        "games": games,
        "selected_games": total,
        "primary_episodes": n,
        "primary_dates_utc": sorted(dates),
        "unique_observation_dates": len(by_date),
        "unique_primary_dates": len(dates),
        "secondary_episodes": sum(r["role"] == "secondary" for r in rows),
        "scheduled_slots_in_verified_sessions": len(rows),
        "slot_exclusions": dict(Counter(r["reason"] for r in rows if r["role"] == "excluded")),
        "games_without_observable_primary": total - n,
        "incomplete_or_unusable_primaries": n - complete,
        "truncated_windows_by_role": dict(
            Counter(
                r["role"]
                for r in rows
                if "window_coverage" in r and not r["window_coverage"]["full_horizon"]
            )
        ),
        "primaries_with_interruption_or_conflict": interrupted,
        "coverage_gates": gates,
        "classifications_by_delay_ms": classifications(rows),
        "observation_dates_utc": {
            d: {
                "games": {
                    game: classifications([r for r in rr if r["event_ticker"] == game])
                    for game in sorted({r["event_ticker"] for r in rr})
                },
                "classifications_by_delay_ms": classifications(rr),
            }
            for d, rr in by_date.items()
        },
    }, rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path, nargs="*", help="Verified sealed backup directories")
    parser.add_argument("--roster", type=Path, help="Directory of exact preflight manifest copies")
    parser.add_argument(
        "--markouts", action="store_true", help="Include Stage 2B2 decision-anchored evidence"
    )
    parser.add_argument(
        "--fee-receipts", type=Path, help="Optional trusted same-clock GET receipt bundle"
    )
    parser.add_argument(
        "--fee-sha256", help="Independently obtained SHA-256 of the fee receipt bundle"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="Fresh campaign results directory"
    )
    args = parser.parse_args()
    if bool(args.fee_receipts) != bool(args.fee_sha256) or (
        args.fee_receipts and not args.markouts
    ):
        parser.error("Fee evidence requires --markouts, --fee-receipts and --fee-sha256 together")
    fees = FeeEvidence.read(args.fee_receipts, args.fee_sha256) if args.fee_receipts else None
    output = args.output.resolve()
    protected = [(a.parent if a.name == "backup" else a).resolve() for a in args.archives]
    if args.roster:
        protected.append(args.roster.resolve())
    if output.exists() or any(output.is_relative_to(p) for p in protected):
        parser.error("Choose a fresh output directory outside all inputs")
    summary, rows = aggregate(args.archives, roster=args.roster, markouts=args.markouts, fees=fees)
    publish(output, summary, rows, detail=True)
    print(encode(summary))


if __name__ == "__main__":
    main()
