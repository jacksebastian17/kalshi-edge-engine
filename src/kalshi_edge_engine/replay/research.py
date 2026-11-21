"""Replay the receipt stream, then reconcile its reconstructions with the B1 journal."""

import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from kalshi_edge_engine.engine.orderbook import canonical_book, parse_orderbook
from kalshi_edge_engine.engine.signal import compute_intended_inside_quote_price_cents
from kalshi_edge_engine.kalshi.market import MarketObservation, read_market_observation
from kalshi_edge_engine.replay import ENGINE_VERSION, MATCH_MS, Mode, b1_orderbook
from kalshi_edge_engine.replay.archive import Archive, Selection, validate
from kalshi_edge_engine.replay.execution import POLICIES, Intent, Model, Queue, draw, guard

METRICS = (
    "poll_class",
    "market_best_bid",
    "cross_for_side",
    "best_size_at_bid",
    "displayed_quote_size",
    "traded_volume_source",
    "traded_volume_estimate",
    "queue_ahead_before_decrement",
    "queue_ahead_after_poll",
    "eligible_path",
)


def discrepancy(
    intent: Intent,
    original: int | None,
    replay_ns: int | None,
    metric: str,
    expected: Any,
    actual: Any,
    explanation: str,
) -> dict[str, Any]:
    delta = (
        actual - expected
        if type(expected) in (float, int) and type(actual) in (float, int)
        else None
    )
    return {
        "intent_id": intent.order.id,
        "ticker": intent.order.ticker,
        "original_ms": original,
        "replay_received_ns": replay_ns,
        "metric": metric,
        "expected": expected,
        "replayed": actual,
        "difference": delta,
        "explanation": explanation,
    }


def reconcile(
    intents: dict[str, Intent],
    polls: dict[str, list[dict[str, Any]]],
    observed: dict[str, list[dict[str, Any]]],
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    original_classes: Counter[str] = Counter()
    raw_classes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    agreements: Counter[str] = Counter()
    for key, rows in polls.items():
        intent = intents[key]
        books = observed.get(key, [])
        # Match retrospectively, independently of the reconstructed values or RNG. Receipt ID
        # order remains authoritative in the simulation even when the wall clock reverses.
        ordered = sorted(books, key=lambda b: b["ms"])
        times = [b["ms"] for b in ordered]
        matches = []
        used: Counter[int] = Counter()
        for poll in rows:
            ts = poll["ts_ms"]
            choices = ordered[bisect_left(times, ts - MATCH_MS) : bisect_right(times, ts)]
            choices = [
                b
                for b in choices
                if b.get("validation_ns") is None or ts <= b["validation_ns"] // 10**6
            ]
            matches.append(choices)
            used.update(b["id"] for b in choices)
        matched = set()
        for poll, choices in zip(rows, matches, strict=True):
            counts["recorded_polls"] += 1
            kind = poll["poll_class"]
            original_classes[kind] += 1
            candidate = json.loads(poll["candidate"] or "null")
            counts["recorded_candidates"] += int(candidate is not None)
            counts["recorded_policy_accepted"] += int(
                bool(poll["policy_accepted"] or poll["persisted"])
            )
            counts["persisted_candidate_fills"] += int(bool(poll["persisted"]))
            if poll["rejection_reason"]:
                reasons[poll["rejection_reason"]] += 1
            ts = poll["ts_ms"]
            if len(choices) != 1 or used[choices[0]["id"]] != 1 or kind == "unprocessed":
                counts["unmatched_polls"] += 1
                issues.append(
                    discrepancy(
                        intent,
                        ts,
                        None,
                        "poll_evidence",
                        kind,
                        None,
                        "No unique causal intent-specific receipt within alignment tolerance, or poll unprocessed.",
                    )
                )
                continue
            b = choices[0]
            matched.add(b["id"])
            counts["aligned_polls"] += 1
            evidence = json.loads(poll["evidence"])
            expected_path = candidate["fill_path_type"] if candidate else None
            if not candidate and evidence.get("no_candidate_reason") == "probability_draw_missed":
                expected_path = {
                    "at_displayed_quote": "queue_cleared",
                    "price_improving": "price_improves",
                    "moved_through": "moved_through",
                }.get(kind)
            expected = {**evidence, "poll_class": kind, "eligible_path": expected_path}
            for metric in METRICS:
                actual = b["diagnostics"].get(metric)
                if metric not in expected:
                    issues.append(
                        discrepancy(
                            intent,
                            ts,
                            b["ns"],
                            metric,
                            None,
                            actual,
                            "Metric absent from B1 decision evidence.",
                        )
                    )
                elif (
                    not b["diagnostics"]["queue_before_known"]
                    and metric in ("queue_ahead_before_decrement", "queue_ahead_after_poll")
                ) or (
                    not b["diagnostics"]["queue_state_known"]
                    and metric
                    in ("traded_volume_source", "traded_volume_estimate", "eligible_path")
                ):
                    issues.append(
                        discrepancy(
                            intent,
                            ts,
                            b["ns"],
                            metric,
                            expected.get(metric),
                            None,
                            "Missing queue initialization history.",
                        )
                    )
                elif expected.get(metric) != actual:
                    issues.append(
                        discrepancy(
                            intent,
                            ts,
                            b["ns"],
                            metric,
                            expected.get(metric),
                            actual,
                            "Reconstruction differs using the intent-linked book.",
                        )
                    )
                else:
                    agreements[metric] += 1
            # Test the recorded candidate through deterministic gates independently of new draws.
            expected_reason = poll["rejection_reason"]
            actual_reason = b.get("guard", "missing_validation_response")
            if candidate and not actual_reason:
                path = candidate["fill_path_type"]
                if path in ("price_improves", "moved_through"):
                    if not intent.config.get("allow_" + path):
                        actual_reason = "fill_path_" + path + "_disabled"
                    elif (
                        intent.config.get("min_edge_cents_" + path) is not None
                        and intent.order.net_edge < intent.config["min_edge_cents_" + path]
                    ):
                        actual_reason = "fill_path_" + path + "_min_edge"
            if actual_reason != expected_reason:
                issues.append(
                    discrepancy(
                        intent,
                        ts,
                        b.get("validation_ns"),
                        "validation_or_policy_reason",
                        expected_reason,
                        actual_reason,
                        "HTTP receipt bounds omit local scheduling, request start and concurrent ledger transactions.",
                    )
                )
            else:
                agreements["validation_or_policy_reason"] += 1
            if candidate:
                counts["aligned_recorded_candidates"] += 1
                accepted = bool(poll["policy_accepted"] or poll["persisted"])
                counts["aligned_recorded_accepted"] += int(accepted)
                diagnosed = b["diagnostics"]
                if (
                    diagnosed["queue_state_known"] or candidate["fill_path_type"] != "queue_cleared"
                ) and (accepted or expected_reason):
                    replay_accepted = (
                        actual_reason is None
                        and diagnosed["eligible_path"] == candidate["fill_path_type"]
                    )
                    counts["accepted_using_recorded_candidates"] += int(replay_accepted)
                    if replay_accepted != accepted:
                        issues.append(
                            discrepancy(
                                intent,
                                ts,
                                b.get("validation_ns"),
                                "candidate_acceptance",
                                accepted,
                                replay_accepted,
                                "Conditional on B1 generating this candidate. Local validation timing may differ.",
                            )
                        )
                    else:
                        agreements["candidate_acceptance"] += 1
        for b in books:
            if b["id"] not in matched:
                counts["unmatched_books"] += 1
                issues.append(
                    discrepancy(
                        intent,
                        None,
                        b["ns"],
                        "paper_poll",
                        None,
                        b["id"],
                        "No unique recorded decision for this receipt.",
                    )
                )
    for books in observed.values():
        raw_classes.update(b["diagnostics"]["poll_class"] for b in books)
    return {
        "counts": counts,
        "recorded_poll_classes": original_classes,
        "reconstructed_poll_classes": raw_classes,
        "recorded_rejection_reasons": reasons,
        "matched_metric_agreements": agreements,
        "discrepancy_count": len(issues),
        "discrepancies": issues,
        "alignment_tolerance_ms": MATCH_MS,
    }


def _load_history(
    archive: Archive, selection: Selection, coverage: dict[str, Any]
) -> tuple[dict[str, Intent], dict[str, list[dict[str, Any]]]]:
    intents = {}
    ledger = archive.ledger
    coverage["legacy_intents_without_submission_evidence_archive_wide"] = (
        ledger.execute(
            "SELECT COUNT(*) FROM quote_intents WHERE submission_evidence IS NULL AND id NOT LIKE 'exit_intent:%'"
        ).fetchone()[0]
        if ledger
        else None
    )
    if ledger:
        for r in ledger.execute(
            "SELECT * FROM quote_intents WHERE submission_evidence IS NOT NULL AND id NOT LIKE 'exit_intent:%' ORDER BY id"
        ):
            if not selection.tickers or r["ticker"] in selection.tickers:
                intents[r["id"]] = Intent.from_row(dict(r))
    counts_by_id: Counter[str] = Counter()
    validations: Counter[str] = Counter()
    submission_sources: dict[str, set[str]] = defaultdict(set)
    session_ids = set()
    invalid_ids = set(coverage["invalid_event_ids"])
    for row in archive.recorder.execute(
        "SELECT id, ticker, session_id, received_ns, intent_id, kind, source FROM market_events WHERE intent_id IS NOT NULL"
    ):
        if selection.includes(row):
            session_ids.add(row["intent_id"])
            if row["id"] in invalid_ids:
                continue
            if row["source"] == "entry_simulator" and row["kind"] == "orderbook":
                counts_by_id[row["intent_id"]] += 1
            elif row["source"] == "entry_simulator" and row["kind"] == "market":
                validations[row["intent_id"]] += 1
            elif row["source"] == "entry_submission":
                submission_sources[row["intent_id"]].add(row["kind"])
    # The ledger has no recorder session ID. Never assign an absent intent to a selected session.
    if selection.session:
        intents = {k: v for k, v in intents.items() if k in session_ids}
    if selection.start_ns is not None or selection.end_ns is not None:
        intents = {
            k: v
            for k, v in intents.items()
            if k in session_ids
            or (
                (selection.end_ns is None or v.order.created_ts_ms * 10**6 < selection.end_ns)
                and (
                    selection.start_ns is None
                    or (v.row["last_update_ts_ms"] or v.order.created_ts_ms) * 10**6
                    >= selection.start_ns
                )
            )
        }
    coverage["instrumented_intents_in_scope"] = len(intents)
    missing = sorted(k for k in intents if not counts_by_id[k])
    coverage["intents_without_simulator_books"] = missing
    coverage["recorded_intent_ids_missing_in_ledger"] = sorted(session_ids - intents.keys())
    if (missing or coverage["recorded_intent_ids_missing_in_ledger"]) and coverage[
        "coverage"
    ].startswith("complete"):
        coverage["coverage"] = "partially_observed"
    polls: dict[str, list[dict[str, Any]]] = {k: [] for k in intents}
    session_windows = [
        (s["first_received_ns"] // 10**6, s["last_received_ns"] // 10**6)
        for s in coverage["segments"]
        if s["session"] == selection.session
    ]
    if ledger:
        for row in ledger.execute("""SELECT p.*, f.id AS persisted FROM entry_polls p
            LEFT JOIN paper_fills f ON f.id=p.candidate_id AND f.intent_id=p.intent_id ORDER BY p.id"""):
            if row["intent_id"] not in polls:
                continue
            ns = row["ts_ms"] * 10**6
            if selection.session and not any(
                lo <= row["ts_ms"] <= hi for lo, hi in session_windows
            ):
                continue
            if (selection.start_ns is None or ns >= selection.start_ns) and (
                selection.end_ns is None or ns < selection.end_ns
            ):
                polls[row["intent_id"]].append(dict(row))
    evidence_gaps = {}
    for k, admitted in intents.items():
        submission = json.loads(admitted.row["submission_evidence"])
        reasons = []
        if not admitted.has_execution_config:
            reasons.append("missing_execution_configuration")
        if submission_sources[k] != {"market", "orderbook"}:
            reasons.append("missing_submission_pair")
        if not polls[k]:
            reasons.append("missing_persisted_polls")
        if not validations[k] or validations[k] != counts_by_id[k]:
            reasons.append("missing_intent_market_validation")
        if len(polls[k]) != counts_by_id[k]:
            reasons.append("poll_count_differs_from_book_count")
        if submission.get("incomplete_poll_history"):
            reasons.append("marked_journal_gap")
        if reasons:
            evidence_gaps[k] = reasons
    coverage["intent_evidence_gaps"] = evidence_gaps
    if coverage["intent_evidence_gaps"] and coverage["coverage"].startswith("complete"):
        coverage["coverage"] = "partially_observed"
    return intents, polls


def _compare_submission(
    intent: Intent,
    e: dict[str, Any],
    body: dict[str, Any],
    src: tuple[int, dict[str, Any]] | None,
    submit_agreements: Counter[str],
    issues: list[dict[str, Any]],
    unavailable: Counter[str],
    mode: Mode,
) -> None:
    parse = parse_orderbook if mode == Mode.CANONICAL else b1_orderbook.parse_orderbook
    parsed = parse(body)
    bid, ask = (
        (parsed.bid_yes, parsed.cross_yes)
        if intent.order.side == "YES"
        else (parsed.bid_no, parsed.cross_no)
    )
    metrics = {
        "best_bid_at_submit_cents": bid.price if bid else None,
        "cross_at_submit_cents": ask.price if ask else None,
    }
    submission = json.loads(intent.row["submission_evidence"])
    if (
        src
        and submission.get("signal_ts_ms") is not None
        and 0 <= submission["signal_ts_ms"] - src[0] // 10**6 <= MATCH_MS
    ):
        signal = parse(src[1])
        sb, sa = (
            (signal.bid_yes, signal.cross_yes)
            if intent.order.side == "YES"
            else (signal.bid_no, signal.cross_no)
        )
        metrics["quote_price_cents"] = compute_intended_inside_quote_price_cents(
            bid_price_cents=sb.price if sb else None,
            cross_price_cents=sa.price if sa else None,
        )
    else:
        unavailable["signal_book_unaligned"] += 1
    for metric, value in metrics.items():
        if value == intent.row[metric]:
            submit_agreements[metric] += 1
        else:
            issues.append(
                discrepancy(
                    intent,
                    submission.get("signal_ts_ms")
                    if metric == "quote_price_cents"
                    else submission.get("book_observed_ts_ms"),
                    src[0] if metric == "quote_price_cents" and src else e["received_ns"],
                    metric,
                    intent.row[metric],
                    value,
                    "Submit or signal reconstruction.",
                )
            )


def analyze(
    archive: Archive,
    selection: Selection,
    *,
    seed: int = 1,
    scale: float = 1,
    models: tuple[str, ...] = tuple(POLICIES),
    inspect_only: bool = False,
    require_raw_consistency: bool = False,
    mode: Mode = Mode.B2,
    strict: bool = False,
    emit: Callable[[dict[str, Any]], None] | None = None,
    emit_decision: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not 0 <= scale <= 10 or any(name not in POLICIES for name in models):
        raise ValueError("Unknown model or probability scale outside [0,10]")
    if strict and mode == Mode.B2:
        raise ValueError("The execution-consistency gate requires canonical or b1-queue semantics")
    coverage = validate(archive, selection, mode=mode)
    report: dict[str, Any] = {
        "dataset_id": archive.identifier,
        "sha256": archive.hashes,
        "engine_version": ENGINE_VERSION if mode == Mode.B2 else "b3a-receipt-replay-v1",
        **(
            {"semantics": mode.value, "strict_execution_consistency": strict}
            if mode != Mode.B2
            else {}
        ),
        "selection": asdict(selection),
        "seed": seed,
        "probability_scale": scale,
        "require_raw_consistency": require_raw_consistency,
        "coverage": coverage,
    }
    intents, polls = _load_history(archive, selection, coverage)
    if mode != Mode.B2:
        report["parser_version"] = "fixed-point-v1" if mode == Mode.CANONICAL else "historical-b1"
        report["queue_version"] = "witnessed-initialization-v1"
        report["execution_consistency_gate"] = {
            "version": "entry-depth-v1",
            "enabled": strict,
            "requires": [
                "complete_unambiguous_two_sided_book",
                "uncrossed_prices",
                "best_and_implied_ask_agree",
                "top_and_quote_quantity_agree",
                "reported_quantity_at_each_price_agrees",
                "whole_capacity_at_each_price_agrees",
                "whole_contract_on_both_tops",
                "queue_relevant_liquidity_agrees",
            ],
            "reference": "Canonical interpretation of the same receipt. Agreement is internal consistency, not independent evidence of execution.",
        }
    if inspect_only:
        return report
    report["effective_configurations"] = {}
    report["intent_configuration"] = {}
    for key, admitted in intents.items():
        cfg = json.dumps(admitted.config, sort_keys=True)
        index = report["effective_configurations"].setdefault(
            cfg, len(report["effective_configurations"])
        )
        report["intent_configuration"][key] = index
    report["effective_configurations"] = {
        v: json.loads(k) for k, v in report["effective_configurations"].items()
    }
    experiments = [Model(k, require_raw_consistency, mode, strict) for k in models]
    observed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pending: dict[str, dict[str, Any]] = {}
    queues: dict[str, Queue] = {}
    submitted_books: set[str] = set()
    anchored: set[str] = set()
    latest_market: dict[str, MarketObservation] = {}
    close_times: dict[str, dict[str, int]] = defaultdict(dict)
    canceled: set[str] = set()
    signal_books: dict[str, tuple[int, dict[str, Any]]] = {}
    issues: list[dict[str, Any]] = []
    submit_agreements: Counter[str] = Counter()
    unavailable: Counter[str] = Counter()
    resets = {
        k: sorted(p["ts_ms"] for p in group if p["poll_class"] == "unprocessed")
        for k, group in polls.items()
    }
    witnessed_resets = {
        k: {
            p["ts_ms"]
            for p in group
            if p["poll_class"] == "unprocessed"
            and str(json.loads(p["evidence"] or "{}").get("no_candidate_reason", "")).startswith(
                "poll_error:"
            )
        }
        for k, group in polls.items()
    }
    transitions: Counter[str] = Counter()
    consistency_counts: dict[str, Counter[str]] = defaultdict(Counter)
    last_book_ms: dict[str, int] = {}
    boundaries = {s["first_id"] for s in coverage["segments"]}
    invalid = set(coverage["invalid_event_ids"])

    def decide(k: str, book: dict[str, Any], event: dict[str, Any], veto: str | None) -> None:
        model_guard = veto if k in anchored else "missing_submission_history"
        book.update(
            guard=veto, validation_ns=event["received_ns"] if event["kind"] == "market" else None
        )
        if emit_decision:
            source = book["event"]
            emit_decision(
                {
                    "intent_id": k,
                    "ticker": source["ticker"],
                    "universe": source["universe"],
                    "session": source["session_id"],
                    "book_id": book["id"],
                    "book_received_ns": book["ns"],
                    "validation_id": event["id"] if book["validation_ns"] is not None else None,
                    "validation_received_ns": book["validation_ns"],
                    "conditional_guard": veto,
                    "research_guard": model_guard,
                    "reconstructed": book["diagnostics"],
                    "hypothetical_uniform_draw": book["draw"],
                }
            )
        for model in experiments:
            model.evaluate(
                intents[k],
                book["diagnostics"],
                event,
                book["id"],
                model_guard,
                book["draw"],
                scale,
            )

    def abandon(k: str) -> None:
        book = pending.pop(k)
        decide(k, book, book["event"], "missing_validation_response")

    for row in archive.rows():
        if row["id"] in boundaries:
            for key in list(pending):
                abandon(key)
            queues.clear()
            submitted_books.clear()
            anchored.clear()
            latest_market.clear()
            close_times.clear()
            canceled.clear()
            signal_books.clear()
            last_book_ms.clear()
            for model in experiments:
                model.pending_marks.clear()
        if not selection.includes(row) or row["id"] in invalid:
            continue
        e = dict(row)
        raw_body = e.pop("body")
        body = (
            json.loads(raw_body, parse_float=str)
            if mode == Mode.CANONICAL and e["kind"] == "orderbook"
            else json.loads(raw_body)
        )
        if emit:
            # Preserve the spelling of unquoted decimal tokens in B3A book exports.
            exported = (
                json.loads(raw_body, parse_float=str)
                if mode != Mode.B2 and e["kind"] == "orderbook"
                else body
            )
            emit({**e, "body": exported})
        key, ticker = e["intent_id"], e["ticker"]
        ms = e["received_ns"] // 10**6
        intent = intents.get(key)
        if intent and (ticker != intent.order.ticker or e["universe"] != intent.row["universe"]):
            unavailable["intent_provenance_mismatch"] += 1
            continue
        if e["kind"] == "orderbook":
            for model in experiments:
                model.mark(e, body)
            if e["source"] == "scanner":
                signal_books[ticker] = (e["received_ns"], body)
            if intent and e["source"] == "entry_submission":
                submitted_books.add(key)
                _compare_submission(
                    intent,
                    e,
                    body,
                    signal_books.get(ticker),
                    submit_agreements,
                    issues,
                    unavailable,
                    mode,
                )
            if not intent or e["source"] != "entry_simulator":
                continue
            if key in pending:
                abandon(key)
                anchored.discard(key)
                if mode != Mode.B2:
                    queues.pop(key, None)
            if not intent.has_execution_config or ms < intent.order.created_ts_ms:
                unavailable["missing_config_or_pre_creation_book"] += 1
                continue
            queue = queues.setdefault(
                key, Queue(known=key in anchored, missing_history=key not in anchored)
            )
            reset_end = bisect_left(resets[key], ms)
            if reset_end > bisect_right(resets[key], last_book_ms.get(key, -1)):
                witnessed = resets[key][reset_end - 1] in witnessed_resets[key]
                queue = queues[key] = Queue(missing_history=not witnessed)
            last_book_ms[key] = ms
            reference = (
                canonical_book(json.loads(raw_body, parse_float=str))
                if mode == Mode.B1_QUEUE
                else None
            )
            d = queue.observe(intent, body, mode, reference=reference)
            if mode != Mode.B2:
                transitions[d["queue_transition"]] += 1
                for dimension in (
                    "best_price_agrees",
                    "implied_ask_agrees",
                    "displayed_size_agrees",
                    "best_capacity_agrees",
                    "depth_capacity_agrees",
                    "depth_quantity_agrees",
                    "queue_liquidity_agrees",
                    "complete_two_sided_depth",
                ):
                    value = d["consistency"][dimension]
                    consistency_counts[dimension][
                        "unknown" if value is None else "yes" if value else "no"
                    ] += 1
            book = {
                "id": e["id"],
                "event": e,
                "ns": e["received_ns"],
                "ms": ms,
                "diagnostics": d,
                "draw": draw(seed, e["session_id"], e["sequence"], key),
            }
            pending[key] = book
            observed[key].append(book)
        else:
            previous = latest_market.get(ticker)
            validating = pending.get(key) if e["source"] == "entry_simulator" else None
            if (
                mode != Mode.B2
                and intent
                and e["source"] == "entry_simulator"
                and validating is None
            ):
                # A validation without its book breaks the chain of consecutive size deltas.
                queues[key] = Queue(missing_history=True)
            # The book bounds the validation request start from below. Using this lower
            # bound makes the freshness check conservative when request start is unavailable.
            market = read_market_observation(
                body["market"],
                ticker,
                requested_ts_ms=validating["ms"] if validating else ms,
                observed_ts_ms=ms,
            )
            usable = (
                market is not None
                and (market.status != "active" or market.close_ts_ms is not None)
                and (not previous or market.can_replace(previous))
            )
            if market and usable:
                latest_market[ticker] = market
                for active_id, cutoff in list(close_times[ticker].items()):
                    if market.close_ts_ms is not None:
                        cutoff = close_times[ticker][active_id] = min(cutoff, market.close_ts_ms)
                    if market.status != "active" or ms >= cutoff:
                        canceled.add(active_id)
                    active = intents[active_id]
                    if (
                        active_id in canceled
                        or ms
                        >= active.order.created_ts_ms + 1000 * active.config["fill_horizon_seconds"]
                    ):
                        del close_times[ticker][active_id]
            if (
                intent
                and e["source"] == "entry_submission"
                and key in submitted_books
                and market
                and market.tradable_at(ms)
                and usable
                and intent.has_execution_config
                and market.close_ts_ms is not None
                and ms <= intent.order.created_ts_ms
            ):
                anchored.add(key)
                close_times[ticker][key] = market.close_ts_ms
            if intent and validating is not None:
                veto = guard(intent, validating["ms"], ms, market, previous)
                if key in canceled and usable:
                    veto = "entry_inactive_at_validation"
                decide(key, validating, e, veto)
                del pending[key]
    for key in list(pending):
        abandon(key)
    report["reconciliation"] = reconcile(intents, polls, observed, issues)
    report["submission_agreements"] = submit_agreements
    report["unavailable"] = unavailable
    report["models"] = {m.name: m.report() for m in experiments}
    if mode != Mode.B2:
        report["queue_transitions"] = transitions
        report["consistency_poll_counts"] = consistency_counts
    report["interpretation"] = [
        "Each model conditions on the original admitted intents and observed polling schedule. Portfolio feedback and new signals are outside this comparison.",
        "A new SHA-256 keyed draw is shared across policies for each observation. B1 random draws are unknown.",
        "Validation occurs at the later intent-linked metadata receipt. Scheduler delays, request start and concurrent ledger state are only partly observable.",
        "Research fills assume access to displayed liquidity or hypothetical seller arrivals. Five-second full-size bid marks include fees and are not realized exits.",
    ]
    return report
