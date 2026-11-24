"""Verify the enrollment, process clock witness, sealed tape and paper books before joining."""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from kalshi_edge_engine.db.paper_store import OUTCOME_ON_TIME_MS
from kalshi_edge_engine.replay.archive import Archive, open_archive
from kalshi_edge_engine.replay.evidence import FIELDS, evidence_audit
from tools.capture_builds import PAPER_REPLAY_BUILDS
from tools.markout_fees import FeeEvidence, Receipt
from tools.microstructure import journal
from tools.paper_activity import ACTIVE, STATIC_REJECTIONS
from tools.paper_bridge import ENROLLMENT, EVENTS, PROTOCOL, SEAL
from tools.paper_link_marks import classified_prints, link
from tools.paper_selection import ALIGNED, fingerprint, verify_plan
from tools.shadow_feasibility import DELAYS, market_events
from tools.shadow_markouts import HORIZONS, SECOND
from tools.shadow_tape import Tape, project, source_hashes, verified_source


def read_events(directory: Path, meta: dict, seal: dict | None) -> list[dict]:
    path = directory / EVENTS
    if seal and (
        seal["protocol"] != meta["protocol"]
        or seal["enrollment_sha256"] != journal.digest(directory / ENROLLMENT)
        or seal["events_sha256"] != journal.digest(path)
    ):
        raise ValueError("Invalid sidecar or enrollment seal")
    rows: list[dict] = []
    if path.exists():
        with path.open("rb") as f:
            for line in f:
                if not seal and not line.endswith(b"\n"):
                    break
                r = json.loads(line)
                if r["n"] != len(rows) + 1 or r["session"] != meta["paper_session"]:
                    raise ValueError("Invalid sidecar sequence or session identity")
                rows.append(r)
    if seal and len(rows) != seal["records"]:
        raise ValueError("Incomplete sealed sidecar")
    return rows


def same_clock(rows: list[dict], tape: Tape, meta: dict, digest: str, enrollment_hash: str) -> bool:
    if not rows or rows[0]["kind"] != "opened" or tape.start is None or tape.end is None:
        return False
    first = rows[0]
    ends = [r for r in rows if r["kind"] == "tape_returned"]
    if len(ends) != 1:
        return False
    end = ends[0]
    return (
        first["clock"] == "time.monotonic_ns"
        and bool(first["origin"])
        and first["enrollment_sha256"] == enrollment_hash
        and end["tape_session"] == meta["tape_session"]
        and end["manifest_sha256"] == meta["manifest_sha256"]
        and end["messages_sha256"] == digest
        and first["mono_ns"]
        <= end["began"]
        <= tape.start
        <= tape.end
        <= end["ended"]
        <= end["mono_ns"]
        and all(r["pid"] == first["pid"] and r["origin"] == first["origin"] for r in rows)
        and all(a["mono_ns"] <= b["mono_ns"] for a, b in pairwise(rows))
        and all(
            first["mono_ns"] <= r["decision_mono_ns"] <= r["mono_ns"]
            for r in rows
            if r["kind"] == "proposal"
        )
    )


def decisions(events: list[dict], meta: dict) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for e in events:
        intent = e.get("intent_id")
        if e["kind"] == "proposal":
            if not intent or intent in rows:
                raise ValueError("Duplicate or missing proposal identity")
            rows[intent] = {
                **e,
                "decision_id": f"{e['session']}/{e['n']}",
                "game": meta["markets"].get(e["ticker"]),
                "admission": "unknown",
                "admission_reason": None,
                "admission_events": [],
                "paper_ledger": None,
                "link_status": "missing_tape",
                "scenarios": [],
                "decision_marks": None,
                "entry_marks_by_anchor": {},
                "predecision_30s": None,
            }
            if meta.get("protocol") == ACTIVE:
                rows[intent].update(deterministic_eligible=None, static_rejection=None)
        elif e["kind"] == "entry_gates_passed" and meta.get("protocol") == ACTIVE:
            if not intent or intent not in rows:
                raise ValueError("Unmatched deterministic gate record")
            rows[intent]["deterministic_eligible"] = True
        elif e["kind"] in ("admission", "reject") and intent:
            r = rows[intent]
            if meta.get("protocol") == ACTIVE and e["reason"] in STATIC_REJECTIONS:
                r.update(deterministic_eligible=False, static_rejection=e["reason"])
            r["admission_events"].append(
                {k: e[k] for k in ("n", "kind", "reason", "wall_ns", "mono_ns")}
            )
            r.update(
                admission="admitted" if e.get("admitted") else "rejected",
                admission_reason=e["reason"],
                admission_record=e["n"],
                admission_mono_ns=e["mono_ns"],
                admission_wall_ns=e["wall_ns"],
            )
            if e.get("submission") is not None:
                r["submission"] = e["submission"]
    return rows


def paper_records(
    archive: Archive, events: list[dict], rows: dict[str, dict], rest_session: str, digest: str
) -> tuple[FeeEvidence, dict]:
    db = archive.ledger
    if db is None:
        raise ValueError("Missing paper ledger in sealed paper backup")
    receipts: dict[tuple[str, str], list[Receipt]] = defaultdict(list)
    source = archive.recorder
    audit = evidence_audit(archive)
    healthy = audit["sessions"].get(rest_session, {}).get("complete", False)
    missing_fees = 0
    # Record numbers in fee audits refer to the original sidecar. Raw bytes are in the REST backup.
    for e in events:
        if e["kind"] != "fee_receipt":
            continue
        ref = e["ref"]
        raw = source.execute(
            "SELECT * FROM market_events WHERE session_id=? AND sequence=?",
            (rest_session, ref["sequence"]),
        ).fetchone()
        if not raw or ref["capture_state"] != "queued":
            missing_fees += 1
            continue
        if (
            rest_session != ref["session_id"]
            or raw["request_path"] != e["path"]
            or raw["received_ns"] != ref["received_ns"]
            or raw["base_url"] != e["base_url"]
            or hashlib.sha256(raw["body"]).hexdigest() != e["body_sha256"]
            or e["receipt_mono_ns"] > e["mono_ns"]
        ):
            raise ValueError("Fee receipt provenance mismatch")
        if healthy and e["base_url"] in (
            "https://api.elections.kalshi.com/trade-api/v2",
            "https://external-api.kalshi.com/trade-api/v2",
        ):
            receipts[digest, e["path"]].append(
                Receipt(
                    e["n"],
                    e["receipt_mono_ns"],
                    raw["received_ns"],
                    json.loads(raw["body"], parse_float=Decimal),
                )
            )
    bookings = {e["fill_id"]: e for e in events if e["kind"] == "paper_booking"}
    orphan = []
    for item in db.execute("SELECT * FROM quote_intents ORDER BY rowid"):
        key = item["id"]
        if key.startswith("exit_intent:"):
            continue
        if key not in rows:
            orphan.append(
                {
                    "intent_id": key,
                    "ticker": item["ticker"],
                    "side": item["side"],
                    "status": item["status"],
                    "reason": "missing_prospective_proposal",
                }
            )
            continue
        r = rows[key]
        if (
            item["ticker"] != r["ticker"]
            or item["side"].lower() != r["side"]
            or Decimal(str(item["quote_price_cents"])) != Decimal(r["price_cents"])
            or item["size_contracts"] != r["contracts"]
        ):
            raise ValueError("Paper intent terms differ from proposal")
        fills = [
            dict(f)
            for f in db.execute(
                """SELECT id,fill_ts_ms,fill_size,fill_price_cents,fill_fee_cents,
            position_lifecycle_id,execution_source,market_timing_invalidated,observed_5s_ts_ms FROM paper_fills
            WHERE intent_id=? ORDER BY fill_ts_ms,id""",
                (key,),
            )
        ]
        for f in fills:
            b = bookings.get(f["id"])
            f["booking_mono_ns"] = b["mono_ns"] if b and b["intent_id"] == key else None
            observed = f["observed_5s_ts_ms"]
            due = f["fill_ts_ms"] + 5000
            f["paper_5s_on_time"] = bool(
                not f["market_timing_invalidated"]
                and observed is not None
                and due <= observed <= due + OUTCOME_ON_TIME_MS
            )
        polls = [
            dict(p)
            for p in db.execute(
                """SELECT id,ts_ms,poll_class,candidate_id,rejection_reason
            FROM entry_polls WHERE intent_id=? ORDER BY id""",
                (key,),
            )
        ]
        r.update(
            admission="admitted",
            paper_ledger={
                "status": item["status"],
                "terminal_detail": item["terminal_detail"],
                "cancel_reason": item["market_cancel_reason"],
                "entry_fills": fills,
                "polls": polls,
                "submission": json.loads(item["submission_evidence"] or "{}"),
            },
        )
    for r in rows.values():
        unresolved = []
        for ref in r["source_receipts"]:
            raw = source.execute(
                "SELECT session_id,sequence,received_ns,kind,ticker,intent_id,source,universe FROM market_events WHERE session_id=? AND sequence=?",
                (ref["session_id"], ref["sequence"]),
            ).fetchone()
            if (
                ref["session_id"] != rest_session
                or ref["capture_state"] != "queued"
                or not raw
                or any(raw[k] != ref[k] for k in FIELDS)
            ):
                unresolved.append(ref)
        r["signal_receipt_audit"] = {
            "references": len(r["source_receipts"]),
            "unresolved": unresolved,
        }
    exits = [
        dict(x)
        for x in db.execute("""SELECT id,intent_id,position_lifecycle_id,fill_ts_ms,
        fill_price_cents,fill_size,realized_pnl_delta_cents,market_timing_invalidated FROM paper_fills
        WHERE realized_pnl_delta_cents IS NOT NULL ORDER BY fill_ts_ms,id""")
    ]
    return FeeEvidence(archive.hashes["market_data_recorder.db"], dict(receipts)), {
        "hashes": archive.hashes,
        "rest_session": rest_session,
        "rest_complete": healthy,
        "b3b1_evidence_classification": audit["classification"],
        "b3b1_evidence_counts": audit["counts"],
        "missing_fee_receipts": missing_fees,
        "ledger_only_intents": orphan,
        "booked_lifecycle_exits": exits,
    }


def booking_support(row: dict, tape: Tape) -> None:
    if row["paper_ledger"] is None:
        return
    for f in row["paper_ledger"]["entry_fills"]:
        stamp = f["booking_mono_ns"]
        support = {}
        for s in row["scenarios"]:
            status = "unmeasured"
            if (
                stamp is not None
                and "evidence" in s
                and Decimal(str(f["fill_price_cents"])) == Decimal(row["price_cents"])
                and s["arrival_mono_ns"] <= stamp <= row["decision_mono_ns"] + 60 * SECOND
                and tape.end is not None
                and stamp <= tape.end
            ):
                last = tape.cutoff(stamp)
                if last <= s["last_included_record"]:
                    status = (
                        "print_by_booking"
                        if any(p["record"] <= last for p in s["evidence"]["print_receipts"])
                        else "no_print_by_booking"
                    )
            support[str(s["delay_ms"])] = status
        f["public_support_through_local_booking"] = support


def session_report(
    directory: Path, tape_root: Path, protocol: str = PROTOCOL
) -> tuple[dict, list[dict]]:
    from tools.paper_link import version_digest

    meta = json.loads((directory / ENROLLMENT).read_bytes())
    producer, reader = meta["code_sha256"], version_digest(protocol)
    old, compatible = PAPER_REPLAY_BUILDS.get(protocol, (set(), ""))
    if (
        meta["protocol"] != protocol
        or directory.name != meta["tape_run"]
        or (producer != reader and not (producer in old and reader == compatible))
    ):
        raise ValueError("Invalid enrollment identity")
    plan = None
    if protocol in (ALIGNED, ACTIVE):
        plan = verify_plan(
            directory.parent.parent,
            directory.name,
            meta["tape_session"],
            meta["markets"],
            meta["code_sha256"],
            meta["selection_sha256"],
            protocol,
        )
    seal_file = directory / SEAL
    seal = json.loads(seal_file.read_bytes()) if seal_file.is_file() else None
    events = read_events(directory, meta, seal)
    rows = decisions(events, meta)
    info = {
        "enrollment": meta,
        "sealed": bool(seal),
        "records": len(events),
        "paper_archive": None,
        "sidecar_sha256": seal["events_sha256"] if seal else None,
        "no_proposal_reasons": dict(
            Counter(e["reason"] for e in events if e["kind"] == "no_proposal")
        ),
        "last_scanner_status": next(
            (e for e in reversed(events) if e["kind"] == "paper_stopped"), None
        ),
    }
    tape_path = tape_root / meta["tape_run"] / "backup"
    fees = None
    tape = None
    mapped = False
    digest = ""
    issue = "unsealed_or_missing_sidecar" if not seal else "missing_tape_backup"
    if seal and tape_path.is_dir():
        tm, verified, hashes = verified_source(tape_path)
        digest = hashes[journal.LOG]
        if (
            tm["session"] != meta["tape_session"]
            or market_events(tm["market_checks"], tm["markets"]) != meta["markets"]
            or hashes[journal.MANIFEST] != meta["manifest_sha256"]
            or (plan and tm.get("selection", {}).get("audit_sha256") != meta["selection_sha256"])
        ):
            issue = "tape_session_mismatch"
        else:
            prices = {
                (r["ticker"], r["side"], Decimal(r["price_cents"]))
                for r in rows.values()
                if r["game"]
            }
            tape = project(
                tape_path / journal.LOG, verified, with_depth=True, proposal_prices=prices
            )
            mapped = same_clock(events, tape, meta, digest, journal.digest(directory / ENROLLMENT))
            if plan and mapped:
                mapped = (
                    plan["finished_ns"]
                    <= tm["prepared_ns"]
                    <= meta["enrolled_wall_ns"]
                    <= events[0]["wall_ns"]
                    <= (tape.start_wall_ns or 0)
                )
            if source_hashes(tape_path) != hashes:
                raise ValueError("Tape changed during linking")
            issue = "verified_shared_process" if mapped else "unverified_clock"
        info["tape_hashes"] = hashes
    if seal:
        name = seal["paper_backup"]
        if Path(name).name != name or not name.startswith("backup-"):
            raise ValueError("Invalid sealed paper backup name")
        archive_dir = directory / "paper" / name
        if archive_dir.is_dir():
            experiment_file = archive_dir / "experiment.json"
            if journal.digest(experiment_file) != seal["experiment_sha256"]:
                raise ValueError("Paper experiment manifest differs from seal")
            experiment = json.loads(experiment_file.read_bytes())
            if plan and (
                experiment["source_sha256"] != plan["runtime"]["source_sha256"]
                or fingerprint(experiment["policy"]) != plan["runtime"]["policy_sha256"]
            ):
                raise ValueError("Captured runtime differs from the frozen selection policy")
            info["paper_runtime"] = {
                k: experiment[k] for k in ("experiment_id", "source_sha256", "commit", "policy")
            }
            ready = [e for e in events if e["kind"] == "paper_ready"]
            if len(ready) != 1:
                raise ValueError("Missing unique paper recorder identity")
            with open_archive(archive_dir) as archive:
                if archive.hashes != seal["paper_hashes"]:
                    raise ValueError("Paper backup differs from seal")
                fees, info["paper_archive"] = paper_records(
                    archive, events, rows, ready[0]["rest_session"], digest
                )
    info["clock_mapping"] = issue
    info["clock_witness"] = [e for e in events if e["kind"] in ("opened", "tape_returned")]
    prints = classified_prints(tape) if tape and mapped else {}
    for r in rows.values():
        if not r["game"]:
            r["link_status"] = "market_outside_subscription"
        elif mapped and tape:
            link(tape, r, digest, fees, prints)
            booking_support(r, tape)
        else:
            r["link_status"] = issue
    if plan:
        from kalshi_edge_engine.engine.signal import SIGNAL_VALID

        stages = {}
        ranks = {r["ticker"]: r["rank"] for r in plan["candidates"] if r["reason"] == "selected"}
        for ticker, game in meta["markets"].items():
            observed = [e for e in events if e.get("ticker") == ticker]
            scores = [e["reason"] for e in observed if e["kind"] == "scanner_score"]
            proposals = [r for r in rows.values() if r["ticker"] == ticker]
            stages[ticker] = {
                "game": game,
                "preflight_rank": ranks[ticker],
                "sealed": bool(seal),
                "scanner_visits": sum(e["kind"] == "scanner_visit" for e in observed),
                "score_errors": sum(e["kind"] == "scanner_score_error" for e in observed),
                "signal_reasons": dict(Counter(scores)),
                "valid_signals": scores.count(SIGNAL_VALID),
                "proposals": len(proposals),
                "admitted": sum(r["admission"] == "admitted" for r in proposals),
                "linked": sum(r["link_status"] == "linked" for r in proposals),
            }
            if protocol == ACTIVE:
                stages[ticker].update(proposal_stages(proposals, info["paper_archive"] is not None))
        info["selected_market_diagnostics"] = stages
        if protocol == ACTIVE:
            info["all_proposal_stages"] = proposal_stages(
                list(rows.values()), info["paper_archive"] is not None
            )
    return info, list(rows.values())


def proposal_stages(rows: list[dict], ledger_verified: bool) -> dict:
    return {
        "generated": len(rows),
        "enabled_side": sum(r.get("side_enabled") is True for r in rows),
        "deterministic_pass": sum(r["deterministic_eligible"] is True for r in rows),
        "static_rejections": dict(
            Counter(r["static_rejection"] for r in rows if r["static_rejection"])
        ),
        "deterministic_unknown": sum(r["deterministic_eligible"] is None for r in rows),
        "paper_admissions": sum(r["admission"] == "admitted" for r in rows),
        "paper_entry_bookings": sum(
            len((r["paper_ledger"] or {}).get("entry_fills", [])) for r in rows
        )
        if ledger_verified
        else None,
    }


def campaign_report(
    campaign: Path, tape_root: Path | None = None, *, protocol: str = PROTOCOL
) -> tuple[dict, list[dict]]:
    sessions: dict[str, dict] = {}
    games: dict[str, list[str]] = defaultdict(list)
    rows = []
    tape_ids = set()
    attempts = {}
    if protocol == ACTIVE:
        plans = campaign / "preflights"
        for marker in sorted(plans.glob("*.started.json")):
            run = marker.name.removesuffix(".started.json")
            path = plans / (run + ".json")
            record = json.loads((path if path.exists() else marker).read_bytes())
            if record["rule"] != ACTIVE or record["run"] != run:
                raise ValueError("Invalid active-cohort preflight identity")
            candidates = record.get("candidates", [])
            progress = path.with_suffix(".progress.jsonl")
            if not path.exists() and progress.is_file():
                with progress.open("rb") as prefix:
                    candidates = [
                        json.loads(line)["candidate"] for line in prefix if line.endswith(b"\n")
                    ]
            attempts[run] = {
                "status": record.get("status", "interrupted"),
                "failure": record.get("failure"),
                "selected": record.get("selected", {}),
                "candidate_reasons": dict(
                    Counter(r.get("reason", "unfinished") for r in candidates)
                ),
                "contracts_evaluated": len(candidates),
                "quote_generating_contracts": sum(bool(r["proposals"]) for r in candidates),
                "enabled_side_contracts": sum(
                    any(p["side_enabled"] for p in r["proposals"]) for r in candidates
                ),
                "deterministically_eligible_contracts": sum(
                    any(p["deterministic_eligible"] for p in r["proposals"]) for r in candidates
                ),
                "recently_active_in_window_contracts": sum(
                    r["eligible"] and r.get("activity", {}).get("status") == "recent_print"
                    for r in candidates
                ),
                "enrolled": (campaign / "sessions" / run / ENROLLMENT).is_file(),
                "audit_sha256": journal.digest(path) if path.exists() else None,
            }
    session_dir = campaign / "sessions"
    for directory in sorted(session_dir.iterdir() if session_dir.exists() else ()):
        info, rr = session_report(directory, tape_root or journal.ROOT, protocol)
        meta = info["enrollment"]
        sid = meta["paper_session"]
        if sid in sessions or meta["tape_session"] in tape_ids:
            raise ValueError("Duplicate paper or tape session enrollment")
        sessions[sid] = info
        tape_ids.add(meta["tape_session"])
        for game in sorted(set(meta["markets"].values())):
            games[game].append(sid)
        rows.extend(rr)
    if not sessions and not attempts:
        raise ValueError("No prospective enrollments")
    rows.sort(
        key=lambda r: (
            sessions[r["session"]]["enrollment"]["enrolled_wall_ns"],
            r["session"],
            r["n"],
        )
    )
    primary = {}
    uncertain_games = {}
    for sid, info in sessions.items():
        markets = info["enrollment"]["markets"]
        orphans = (info["paper_archive"] or {}).get("ledger_only_intents", ())
        uncertain_games[sid] = (
            {markets[o["ticker"]] for o in orphans if o["ticker"] in markets}
            if info["sealed"]
            else set(markets.values())
        )
    for r in rows:
        game = r["game"]
        r["role"] = "outside_roster" if not game else "secondary"
        if game and game not in primary:
            ordered = sorted(
                games[game], key=lambda s: (sessions[s]["enrollment"]["enrolled_wall_ns"], s)
            )
            if all(
                game not in uncertain_games[s] for s in ordered[: ordered.index(r["session"]) + 1]
            ):
                r["role"] = "primary"
                primary[game] = r
    dates = {
        datetime.fromtimestamp(r["decision_wall_ns"] // SECOND, UTC).date().isoformat()
        for r in primary.values()
    }
    primary_fills = [
        f for r in primary.values() if r["paper_ledger"] for f in r["paper_ledger"]["entry_fills"]
    ]
    booked_support: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for fill in (r["paper_ledger"] or {}).get("entry_fills", []):
            for delay in DELAYS:
                booked_support[str(delay)][
                    fill.get("public_support_through_local_booking", {}).get(
                        str(delay), "unmeasured"
                    )
                ] += 1
    coverage = {}
    entry_coverage: dict[str, dict] = {}
    for i, delay in enumerate(DELAYS):
        by_anchor = {}
        for anchor in ("hypothetical_entry_marks", "print_supported_entry_marks"):
            counts_by_horizon: dict[str, Counter] = {str(h): Counter() for h in HORIZONS}
            for r in rows:
                ref = r["scenarios"][i][anchor] if r["scenarios"] else None
                if ref is None:
                    for counter in counts_by_horizon.values():
                        counter["no_anchor"] += 1
                else:
                    for horizon in r["entry_marks_by_anchor"][ref]["horizons"]:
                        counts_by_horizon[str(horizon["horizon_seconds"])][horizon["status"]] += 1
            by_anchor[anchor] = counts_by_horizon
        entry_coverage[str(delay)] = by_anchor
    for h in HORIZONS:
        counts: Counter[str] = Counter()
        for r in rows:
            marks = r["decision_marks"]
            counts[marks["horizons"][HORIZONS.index(h)]["status"] if marks else "unlinked"] += 1
        coverage[str(h)] = dict(counts)
    return {
        "protocol": protocol,
        **({"original_sample_games_contributed": 0} if protocol != PROTOCOL else {}),
        **({"preflight_attempts": attempts} if protocol == ACTIVE else {}),
        "sessions": sessions,
        "selected_games": len(games),
        "sessions_by_game": games,
        "repeated_games": {g: s for g, s in games.items() if len(s) > 1},
        "primary_by_game": {
            g: primary[g]["decision_id"] if g in primary else None for g in sorted(games)
        },
        "games_without_primary": sorted(set(games) - set(primary)),
        "primary_games": {
            "observed": len(primary),
            "minimum": 100 if protocol == PROTOCOL else None,
            "met": len(primary) >= 100 if protocol == PROTOCOL else None,
        },
        "primary_dates_utc": {
            "dates": sorted(dates),
            "minimum": 10 if protocol == PROTOCOL else None,
            "met": len(dates) >= 10 if protocol == PROTOCOL else None,
        },
        "proposals": len(rows),
        "linkage": dict(Counter(r["link_status"] for r in rows)),
        "admission": dict(Counter(r["admission"] for r in rows)),
        "primary_sides": dict(Counter(r["side"] for r in primary.values())),
        "all_sides": dict(Counter(r["side"] for r in rows)),
        "predecision_activity": dict(
            Counter((r["predecision_30s"] or {}).get("status", "unmeasured") for r in rows)
        ),
        "decision_mark_coverage": coverage,
        "entry_mark_coverage_by_delay_ms": entry_coverage,
        "paper_entry_5s_observations": {
            "primary_booked_fills": len(primary_fills),
            "on_time": sum(f["paper_5s_on_time"] for f in primary_fills),
            "missing": sum(f["observed_5s_ts_ms"] is None for f in primary_fills),
        },
        "booked_fill_public_support_by_delay_ms": booked_support,
        "rejection_reasons": dict(
            Counter(r["admission_reason"] for r in rows if r["admission"] == "rejected")
        ),
        "public_support_by_delay_ms": {
            str(d): dict(
                Counter(
                    "unmeasured"
                    if not r["scenarios"] or r["scenarios"][i]["public_execution_support"] is None
                    else "observed_print"
                    if r["scenarios"][i]["public_execution_support"]
                    else "no_print"
                    for r in rows
                )
            )
            for i, d in enumerate(DELAYS)
        },
        "assessment": "DESCRIPTIVE_ONLY",
    }, rows
