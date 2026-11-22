"""Resolve journaled decisions against immutable recorder rows by primary evidence identity."""

import hashlib
import json
from collections import Counter
from typing import Any

from kalshi_edge_engine.evidence import EVIDENCE_VERSION, MODEL
from kalshi_edge_engine.replay import Mode
from kalshi_edge_engine.replay.archive import Archive, payload

FIELDS = (
    "session_id",
    "sequence",
    "received_ns",
    "kind",
    "ticker",
    "intent_id",
    "source",
    "universe",
)
COUNTS = [
    "instrumented_intents",
    "total_entry_intents",
    "legacy_instrumented_intents",
    "legacy_intents",
    "legacy_polls",
    "simulator_polls",
    "explicitly_linked_polls",
    "missing_references",
    "unresolved_references",
    "duplicate_references",
    "inconsistent_references",
    "dropped_references",
    "invalid_causal_order",
    "invalid_decisions",
    "eligible_queue_cleared",
    "successful_probability_draws",
    "policy_rejections",
    "accepted_candidates",
    "persisted_simulated_fills",
    "orphan_fills",
    "evidence_complete_decisions",
    "evidence_incomplete_decisions",
    "incomplete_intents",
    "outcome_marks",
    "incomplete_outcomes",
    "exit_fills",
    "legacy_exits",
    "incomplete_exits",
]


def _receipt_cutoff(decision_ns: Any, observed_ms: Any) -> int | None:
    if type(decision_ns) is not int or type(observed_ms) is not int:
        return None
    return min(decision_ns, (observed_ms + 1) * 1_000_000 - 1)


def evidence_audit(archive: Archive) -> dict[str, Any]:
    ledger = archive.ledger
    if ledger is None:
        raise ValueError("Decision evidence audit requires the matching paper ledger")
    rec = archive.recorder
    totals = Counter(dict.fromkeys(COUNTS, 0))
    examples: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    sessions: dict[str, dict[str, Any]] = {}
    for row in rec.execute("SELECT session_id,sequence FROM market_events ORDER BY id"):
        session = sessions.setdefault(
            row["session_id"], {"rows": 0, "last": 0, "breaks": 0, "state": "unsealed"}
        )
        session["breaks"] += int(row["sequence"] != session["last"] + 1)
        session["rows"] += 1
        session["last"] = row["sequence"]
    if rec.execute("SELECT 1 FROM sqlite_master WHERE name='capture_sessions'").fetchone():
        for seal in rec.execute("SELECT * FROM capture_sessions ORDER BY rowid"):
            session = sessions.setdefault(seal["id"], {"rows": 0, "last": 0, "breaks": 0})
            session.update(
                state=seal["state"],
                dropped=seal["dropped"],
                errors=seal["errors"],
                received=seal["received"],
                written=seal["written"],
            )
    for session in sessions.values():
        session["complete"] = (
            session["state"] == "clean"
            and not session["breaks"]
            and session.get("dropped") == session.get("errors") == 0
            and session["rows"] == session.get("written") == session.get("received")
        )

    def resolve(
        refs: Any,
        expected: dict[str, str],
        cutoff: Any,
        *,
        intent: str | None = None,
        source: str | None = None,
        universe: str | None = None,
        earliest_ns: int | None = None,
    ) -> list[str]:
        errors = []
        seen: set[str] = set()
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict) or not isinstance(ref.get("kind"), str):
                errors.append("inconsistent_references")
                continue
            kind = ref["kind"]
            if kind in seen:
                errors.append("duplicate_references")
            seen.add(kind)
            if ref.get("capture_state") == "dropped":
                errors.append("dropped_references")
            if (
                ref.get("capture_state") != "queued"
                or kind not in expected
                or ref.get("ticker") != expected.get(kind)
            ):
                errors.append("inconsistent_references")
            if type(ref.get("sequence")) is not int or not isinstance(ref.get("session_id"), str):
                errors.append("missing_references")
                continue
            found = rec.execute(
                "SELECT * FROM market_events WHERE session_id=? AND sequence=? LIMIT 2",
                (ref["session_id"], ref["sequence"]),
            ).fetchall()
            if len(found) != 1:
                errors.append("duplicate_references" if found else "unresolved_references")
                continue
            row = found[0]
            if (
                any(k not in ref or row[k] != ref[k] for k in FIELDS)
                or (intent is not None and row["intent_id"] != intent)
                or (source is not None and row["source"] != source)
                or (universe is not None and row["universe"] != universe)
            ):
                errors.append("inconsistent_references")
            try:
                payload(row, Mode.CANONICAL)
            except (ValueError, TypeError, OverflowError):
                errors.append("inconsistent_references")
            if (
                type(cutoff) is not int
                or row["received_ns"] > cutoff
                or (type(earliest_ns) is int and row["received_ns"] < earliest_ns)
            ):
                errors.append("invalid_causal_order")
            if not sessions.get(row["session_id"], {}).get("complete"):
                errors.append("incomplete_capture_session")
        errors.extend("missing_references" for _ in expected.keys() - seen)
        totals.update(errors)
        return errors

    for intent_row in ledger.execute(
        "SELECT * FROM quote_intents WHERE id NOT LIKE 'exit_intent:%' ORDER BY id"
    ):
        key, ticker, universe = intent_row["id"], intent_row["ticker"], intent_row["universe"]
        initial = json.loads(intent_row["submission_evidence"] or "{}")
        modern = initial.get("schema_version") == EVIDENCE_VERSION
        totals["total_entry_intents"] += 1
        totals["legacy_instrumented_intents"] += bool(initial) and not modern
        totals["instrumented_intents" if modern else "legacy_intents"] += 1
        initial_errors: list[str] = []
        profile = json.loads(intent_row["fee_profile"] or "{}")
        fee_inputs = (
            {
                "market": ticker,
                "event": profile.get("event_ticker"),
                "series": profile.get("series_ticker"),
            }
            if profile.get("series_ticker")
            else {}
        )
        if modern:
            sig = initial.get("signal_receipts")
            if not isinstance(sig, list):
                sig = []
            books = [r for r in sig if isinstance(r, dict) and r.get("kind") == "orderbook"]
            fees = [r for r in sig if r not in books]
            scored, submitted = initial.get("signal_decision_ns"), initial.get("decision_ns")
            initial_errors += resolve(
                books, {"orderbook": ticker}, scored, source="scanner", universe=universe
            )
            initial_errors += resolve(fees, fee_inputs, scored)
            initial_errors += resolve(
                initial.get("book_receipts"),
                {"orderbook": ticker},
                submitted,
                intent=key,
                source="entry_submission",
                universe=universe,
                earliest_ns=scored,
            )
            if initial.get("validation_required"):
                initial_errors += resolve(
                    initial.get("market_receipts"),
                    {"market": ticker},
                    submitted,
                    intent=key,
                    source="entry_submission",
                    universe=universe,
                    earliest_ns=scored,
                )
            if type(scored) is not int or type(submitted) is not int or scored > submitted:
                initial_errors.append("invalid_causal_order")
                totals["invalid_causal_order"] += 1
            if initial.get("incomplete_poll_history") or initial.get("model") != MODEL:
                initial_errors.append("incomplete_submission")
            totals["incomplete_intents"] += bool(initial_errors)
        polls = ledger.execute(
            """SELECT p.*, f.id AS fill_id, f.fill_ts_ms, f.fill_size, f.fill_price_cents
                 FROM entry_polls p LEFT JOIN paper_fills f ON p.candidate_id=f.id AND f.intent_id=p.intent_id
                 WHERE p.intent_id=? ORDER BY p.id""",
            (key,),
        )
        previous: list[dict[str, Any]] = []
        previous_clean = True
        last_decision = initial.get("decision_ns")
        used: set[tuple[Any, Any]] = set()
        for p in polls:
            totals["simulator_polls"] += 1
            e = json.loads(p["evidence"])
            persisted = p["fill_id"] is not None
            accepted = bool(p["policy_accepted"] or persisted)
            totals["accepted_candidates"] += bool(p["candidate_id"] and accepted)
            totals["policy_rejections"] += bool(
                p["candidate_id"]
                and not accepted
                and (p["rejection_reason"] or "").startswith("fill_path_")
            )
            if not modern or e.get("schema_version") != EVIDENCE_VERSION:
                totals["legacy_polls"] += not modern
                totals["evidence_incomplete_decisions"] += 1
                continue
            errors = initial_errors.copy()
            decision = e.get("decision_ns")
            refs = e.get("book_receipts", [])
            book_errors = resolve(
                refs,
                {"orderbook": ticker},
                _receipt_cutoff(decision, p["ts_ms"]),
                intent=key,
                source="entry_simulator",
                universe=universe,
                earliest_ns=last_decision,
            )
            errors += book_errors
            if not book_errors:
                ident = (refs[0]["session_id"], refs[0]["sequence"])
                if ident in used:
                    errors.append("duplicate_references")
                    totals["duplicate_references"] += 1
                else:
                    totals["explicitly_linked_polls"] += 1
                used.add(ident)
            if (
                e.get("previous_book_receipts", []) != previous or (previous and not previous_clean)
            ) and not e.get("interrupted"):
                errors.append("invalid_queue_dependency")
            previous = (
                refs if p["poll_class"] == "at_displayed_quote" and not e.get("interrupted") else []
            )
            previous_clean = not errors
            validation = e.get("validation")
            if not isinstance(validation, dict):
                validation = {}
            final = validation.get("decision_ns")
            if initial.get("validation_required") and not e.get("interrupted"):
                errors += resolve(
                    validation.get("receipts"),
                    {"market": ticker},
                    final,
                    intent=key,
                    source="entry_simulator",
                    universe=universe,
                    earliest_ns=decision,
                )
            if (
                type(decision) is not int
                or type(initial.get("decision_ns")) is not int
                or decision < initial["decision_ns"]
                or (final is not None and (type(final) is not int or final < decision))
                or (persisted and final is not None and final // 1_000_000 > p["fill_ts_ms"])
            ):
                errors.append("invalid_causal_order")
                totals["invalid_causal_order"] += 1
            last_decision = final or decision
            eligible, path, draw = (
                e.get("draw_eligible"),
                e.get("evaluated_path"),
                e.get("random_draw"),
            )
            prob, passed = e.get("effective_probability"), e.get("probability_passed")
            totals["eligible_queue_cleared"] += bool(eligible and path == "queue_cleared")
            totals["successful_probability_draws"] += passed is True
            candidate = json.loads(p["candidate"] or "{}")
            policy_key = {
                "queue_cleared": "fill_probability_at_best",
                "moved_through": "fill_probability_price_moved",
                "price_improves": "fill_probability_price_improves",
            }.get(path)
            valid_draw = (
                (
                    type(draw) in (int, float)
                    and 0 <= draw < 1
                    and type(prob) in (int, float)
                    and passed is (draw < prob)
                    and e.get("no_draw_reason") is None
                )
                if eligible
                else (draw is None and passed is None and bool(e.get("no_draw_reason")))
            )
            if (
                type(eligible) is not bool
                or (eligible and policy_key is None)
                or not valid_draw
                or e.get("model") != MODEL
                or e.get("interrupted")
                or (policy_key and prob != initial.get("execution_config", {}).get(policy_key))
                or bool(candidate) != bool(e.get("proposed_fill_size"))
                or (
                    candidate
                    and (
                        passed is not True
                        or candidate.get("fill_path_type") != path
                        or candidate.get("id") != p["candidate_id"]
                        or candidate.get("fill_size") != e.get("proposed_fill_size")
                        or candidate.get("order_id") != key
                        or candidate.get("fill_price_cents") != intent_row["quote_price_cents"]
                        or not (persisted or p["rejection_reason"])
                    )
                )
                or (
                    persisted
                    and (
                        not initial.get("validation_required")
                        or not fee_inputs
                        or not initial.get("execution_config")
                        or (
                            path in ("price_improves", "moved_through")
                            and not initial["execution_config"].get("allow_" + path)
                        )
                        or validation.get("status") != "active"
                        or candidate.get("fill_size") != p["fill_size"]
                        or candidate.get("fill_price_cents") != p["fill_price_cents"]
                    )
                )
            ):
                errors.append("invalid_decisions")
                totals["invalid_decisions"] += 1
            totals[
                "evidence_incomplete_decisions" if errors else "evidence_complete_decisions"
            ] += 1
            if errors and len(failures) < 5:
                failures.append({"intent": key, "poll": p["id"], "issues": sorted(set(errors))})
            if persisted and not errors and len(examples) < 5:
                ref = refs[0]
                row = rec.execute(
                    "SELECT id,body FROM market_events WHERE session_id=? AND sequence=?",
                    (ref["session_id"], ref["sequence"]),
                ).fetchone()
                examples.append(
                    {
                        "intent": key,
                        "poll": p["id"],
                        "fill": p["fill_id"],
                        "session": ref["session_id"],
                        "sequence": ref["sequence"],
                        "raw_row": row["id"],
                        "body_sha256": hashlib.sha256(row["body"]).hexdigest(),
                        "probability": prob,
                        "draw": draw,
                        "path": path,
                    }
                )
        for fill in ledger.execute("SELECT * FROM paper_fills WHERE intent_id=?", (key,)):
            totals["persisted_simulated_fills"] += 1
            if not ledger.execute(
                "SELECT 1 FROM entry_polls WHERE intent_id=? AND candidate_id=?", (key, fill["id"])
            ).fetchone():
                totals["orphan_fills"] += 1
                totals["evidence_incomplete_decisions"] += 1
            outcomes = json.loads(fill["outcome_fee_audit"] or "{}")
            for h in (5, 15, 30, 60):
                if fill[f"mark_{h}s_after_fill_cents"] is None:
                    continue
                totals["outcome_marks"] += 1
                mark = outcomes.get(f"{h}s", {})
                evidence = mark.get("evidence", {})
                if not modern:
                    totals["incomplete_outcomes"] += 1
                    continue
                end = evidence.get("decision_ns")
                errors = resolve(
                    evidence.get("book_receipts"),
                    {"orderbook": ticker},
                    _receipt_cutoff(end, fill[f"observed_{h}s_ts_ms"]),
                    source="outcome",
                    earliest_ns=(fill["fill_ts_ms"] + h * 1000) * 1_000_000,
                )
                fee = mark.get("profile", {})
                if fee.get("series_ticker"):
                    errors += resolve(
                        evidence.get("fee_receipts"),
                        {
                            "market": ticker,
                            "event": fee["event_ticker"],
                            "series": fee["series_ticker"],
                        },
                        end,
                    )
                if type(end) is not int or end // 1_000_000 < fill["fill_ts_ms"] + h * 1000:
                    errors.append("invalid_causal_order")
                    totals["invalid_causal_order"] += 1
                totals["incomplete_outcomes"] += bool(errors)
    for row in ledger.execute("""SELECT i.ticker,i.submission_evidence,f.execution_source,f.fee_profile,f.fill_ts_ms
                    FROM paper_fills f JOIN quote_intents i ON i.id=f.intent_id
                    WHERE i.id LIKE 'exit_intent:%' ORDER BY f.id"""):
        totals["exit_fills"] += 1
        e = json.loads(row["submission_evidence"] or "{}")
        if e.get("schema_version") != EVIDENCE_VERSION:
            totals["legacy_exits"] += 1
            totals["incomplete_exits"] += 1
            continue
        ticker, cutoff = row["ticker"], _receipt_cutoff(e.get("decision_ns"), row["fill_ts_ms"])
        errors = resolve(e.get("market_receipts"), {"market": ticker}, cutoff, source="inventory")
        if row["execution_source"] != "kalshi_market_settlement":
            errors += resolve(
                e.get("book_receipts"), {"orderbook": ticker}, cutoff, source="inventory"
            )
            fee = json.loads(row["fee_profile"] or "{}")
            if fee.get("series_ticker"):
                errors += resolve(
                    e.get("fee_receipts"),
                    {
                        "market": ticker,
                        "event": fee["event_ticker"],
                        "series": fee["series_ticker"],
                    },
                    cutoff,
                )
        totals["incomplete_exits"] += bool(errors)
    if not totals["instrumented_intents"]:
        classification = "legacy_only" if totals["legacy_intents"] else "no_entry_decisions"
    elif (
        not sessions
        or any(not s["complete"] for s in sessions.values())
        or any(
            totals[k]
            for k in (
                "incomplete_intents",
                "evidence_incomplete_decisions",
                "incomplete_outcomes",
                "incomplete_exits",
                "legacy_intents",
            )
        )
    ):
        classification = "incomplete"
    else:
        classification = "verified_links"
    return {
        "classification": classification,
        "schema_version": EVIDENCE_VERSION,
        "archive_sha256": archive.hashes,
        "recorder_rows": sum(s["rows"] for s in sessions.values()),
        "sessions": sessions,
        "counts": dict(totals),
        "resolved_fills": examples,
        "incomplete_examples": failures,
    }
