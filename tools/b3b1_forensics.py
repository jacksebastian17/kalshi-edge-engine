"""Offline forensics for verified B3B1 backups. Run from a checkout with the project installed."""

import argparse
import hashlib
import json
import math
import sqlite3
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_edge_engine.engine.orderbook import canonical_book, quote_liquidation
from kalshi_edge_engine.replay import Mode
from kalshi_edge_engine.replay.archive import Archive, open_archive, payload
from kalshi_edge_engine.replay.evidence import evidence_audit
from kalshi_edge_engine.replay.execution import profile_from_audit

COHORT = "decision_linked_b3b1_v1"


def rows(db: sqlite3.Connection, query: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(query, args)]


def funnel(db: sqlite3.Connection) -> dict[str, Any]:
    paths: dict[str, Counter[str]] = {}
    games: Counter[str] = Counter()
    tickers: Counter[str] = Counter()
    intents: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    noncandidate_rejections: Counter[str] = Counter()
    other = []
    eligible_queue = []
    intervals = []
    previous: dict[str, int] = {}
    for p in db.execute("""
        SELECT p.*, i.ticker, json_extract(i.fee_profile,'$.event_ticker') AS event,
               f.id AS persisted FROM entry_polls p JOIN quote_intents i ON i.id=p.intent_id
        LEFT JOIN paper_fills f ON f.intent_id=p.intent_id AND f.id=p.candidate_id ORDER BY p.id
    """):
        ev = json.loads(p["evidence"])
        key = p["intent_id"]
        path = ev["evaluated_path"] or "no_path"
        count = paths.setdefault(path, Counter())
        count["polls"] += 1
        count["draws"] += ev["draw_eligible"]
        count["passed"] += ev["probability_passed"] is True
        ref = ev["book_receipts"][0]
        if key in previous:
            intervals.append(ref["received_ns"] - previous[key])
        previous[key] = ref["received_ns"]
        if path == "queue_cleared" and ev["draw_eligible"]:
            eligible_queue.append(
                {
                    "intent": key,
                    "ticker": p["ticker"],
                    "book": ref,
                    "draw": ev["random_draw"],
                    "probability": ev["effective_probability"],
                    "beyond": ev["volume_beyond_queue"],
                }
            )
        if not p["candidate_id"]:
            if p["rejection_reason"]:
                noncandidate_rejections[p["rejection_reason"]] += 1
            continue
        count["candidates"] += 1
        reason = p["rejection_reason"]
        outcome = reason or ("persisted" if p["persisted"] else "unresolved")
        reasons[outcome] += 1
        count[outcome] += 1
        games[p["event"] or "unknown"] += 1
        tickers[p["ticker"]] += 1
        intents[key] += 1
        if reason and not reason.startswith("fill_path_"):
            other.append(
                {
                    "poll": p["id"],
                    "intent": key,
                    "ticker": p["ticker"],
                    "path": path,
                    "book": ref,
                    "validation": ev["validation"],
                    "reason": reason,
                }
            )
    intervals.sort()
    return {
        "paths": paths,
        "candidate_outcomes": reasons,
        "noncandidate_rejections": noncandidate_rejections,
        "candidates_by_event": games,
        "candidates_by_ticker": tickers,
        "candidate_intents": len(intents),
        "intents_with_multiple_candidates": sum(n > 1 for n in intents.values()),
        "candidates_on_repeat_intents": sum(n for n in intents.values() if n > 1),
        "max_candidates_one_intent": max(intents.values(), default=0),
        "queue_draws": eligible_queue,
        "other_rejections": other,
        "within_intent_intervals": len(intervals),
        "poll_gap_ms_nearest_rank": {
            str(q): intervals[math.ceil(q * len(intervals)) - 1] / 1e6 if intervals else None
            for q in (0.5, 0.95, 1.0)
        },
    }


def receipt_payload(archive: Archive, ref: dict[str, Any], kind: str) -> dict[str, Any]:
    row = archive.recorder.execute(
        "SELECT * FROM market_events WHERE session_id=? AND sequence=?",
        (ref["session_id"], ref["sequence"]),
    ).fetchone()
    if row is None or row["kind"] != kind:
        raise ValueError(f"Missing linked {kind}")
    return payload(row, Mode.CANONICAL)


def market_fields(archive: Archive, ref: dict) -> dict:
    market = receipt_payload(archive, ref, "market")["market"]
    return {
        k: market.get(k)
        for k in (
            "ticker",
            "title",
            "status",
            "yes_bid_dollars",
            "yes_ask_dollars",
            "last_price_dollars",
            "volume_fp",
            "updated_time",
        )
    }


def queue_trace(archive: Archive, db: sqlite3.Connection, intent: dict[str, Any]) -> list:
    trace = []
    previous = None
    ahead = 0
    for p in db.execute("SELECT * FROM entry_polls WHERE intent_id=? ORDER BY id", (intent["id"],)):
        e = json.loads(p["evidence"])
        ref = e["book_receipts"][0]
        book = canonical_book(receipt_payload(archive, ref, "orderbook"))
        top = book.top(intent["side"])
        displayed = top is not None and top.price == Decimal(str(intent["quote_price_cents"]))
        size = top.quantity if top else None
        before, used, after = None, None, None
        matches = None
        if displayed and size is not None:
            before = math.ceil(size) if previous is None else ahead
            used = 0 if previous is None else int(max(Decimal(0), previous - size))
            after = max(0, before - used)
            matches = (before, used, after, max(0, used - before)) == (
                e["queue_ahead_before_decrement"],
                e["traded_volume_estimate"],
                e["queue_ahead_after_poll"],
                e["volume_beyond_queue"],
            )
            ahead = after
            previous = size
        else:
            previous, ahead = None, 0
        trace.append(
            {
                "poll": p["id"],
                "book": ref,
                "validation": e["validation"],
                "market_state": market_fields(archive, e["validation"]["receipts"][0]),
                "best": top.price if top else None,
                "ask": book.ask(intent["side"]),
                "best_size": size,
                "ahead_before": before,
                "depletion": used,
                "ahead_after": after,
                "matches_journal": matches,
                "draw": e["random_draw"],
                "candidate": p["candidate_id"],
                "rejection": p["rejection_reason"],
            }
        )
    return trace


def lifecycle(archive: Archive, db: sqlite3.Connection, key: str) -> dict[str, Any]:
    entries = rows(db, "SELECT * FROM paper_fills WHERE intent_id=?", key)
    if len(entries) != 1:
        raise ValueError("Select a lifecycle with one entry fill")
    entry = entries[0]
    fills = rows(
        db,
        "SELECT * FROM paper_fills WHERE position_lifecycle_id=? ORDER BY fill_ts_ms,id",
        entry["position_lifecycle_id"],
    )
    if (
        len(fills) != 2
        or fills[0]["id"] != entry["id"]
        or fills[1]["execution_source"] != "simulated_displayed_bid_sweep"
        or fills[1]["fill_size"] != entry["fill_size"]
    ):
        raise ValueError("Lifecycle requires one entry and one full displayed-depth exit")
    exit_fill = fills[1]
    (intent,) = rows(db, "SELECT * FROM quote_intents WHERE id=?", key)
    side, n, price = intent["side"], entry["fill_size"], Decimal(str(entry["fill_price_cents"]))
    entry_profile = profile_from_audit(json.loads(entry["fee_profile"]))
    if not entry_profile or not entry_profile.series_aware:
        raise ValueError("Entry lacks a versioned series fee profile")
    entry_fee = Decimal(str(entry_profile.fee_cents(float(price), n, liquidity=intent["fee_mode"])))
    submission = json.loads(intent["submission_evidence"])
    book = canonical_book(receipt_payload(archive, submission["book_receipts"][0], "orderbook"))
    level = book.at(side, Decimal(str(intent["quote_price_cents"])))
    (exit_intent,) = rows(db, "SELECT * FROM quote_intents WHERE id=?", exit_fill["intent_id"])
    exit_ev = json.loads(exit_intent["submission_evidence"])

    def sale(
        ref: dict, fee: dict, stored_price: float, stored_fee: float, stored_pnl: float
    ) -> dict:
        profile = profile_from_audit(fee)
        if not profile or not profile.series_aware:
            raise ValueError("Sale lacks a versioned series fee profile")
        depth = canonical_book(receipt_payload(archive, ref, "orderbook"))
        quote = quote_liquidation(
            depth, side, n, ref["received_ns"] // 1_000_000, fee_profile=profile
        )
        if not quote:
            return {"book": ref, "matches_ledger": False, "issue": "insufficient executable depth"}
        gross = sum((Decimal(str(p)) - price) * qty for p, qty in quote.bid_levels)
        net = gross - entry_fee - Decimal(str(quote.exit_fee_cents))
        recorded = (stored_price, stored_fee, stored_pnl)
        matches = all(
            v is not None and math.isclose(c, v, rel_tol=0, abs_tol=1e-8)
            for c, v in zip(
                (quote.price_cents, quote.exit_fee_cents, float(net)), recorded, strict=True
            )
        )
        return {
            "book": ref,
            "levels": quote.bid_levels,
            "displayed_at_best": depth.levels(side)[0].quantity,
            "ask": quote.implied_ask_cents,
            "exit_fee": quote.exit_fee_cents,
            "gross": gross,
            "net": net,
            "recorded_price_fee_pnl": recorded,
            "matches_ledger": matches,
        }

    exit_sale = sale(
        exit_ev["book_receipts"][0],
        json.loads(exit_fill["fee_profile"]),
        exit_fill["fill_price_cents"],
        exit_fill["fill_fee_cents"],
        exit_fill["realized_pnl_delta_cents"],
    )
    exit_sale["matches_recorded_levels"] = json.loads(exit_fill["bid_levels"]) == [
        list(x) for x in exit_sale.get("levels", ())
    ]
    marks = {}
    outcomes = json.loads(entry["outcome_fee_audit"] or "{}")
    for h in (5, 15, 30, 60):
        audit = outcomes.get(f"{h}s")
        t = entry[f"observed_{h}s_ts_ms"]
        if not audit or t is None:
            marks[h] = {"issue": "missing mark or fee evidence"}
            continue
        mark = sale(
            audit["evidence"]["book_receipts"][0],
            audit["profile"],
            entry[f"mark_{h}s_after_fill_cents"],
            audit["exit_fee_cents"],
            entry[f"pnl_{h}s_after_fill_cents"],
        )
        marks[h] = {
            **mark,
            "observed_ms": t,
            "after_fill_ms": t - entry["fill_ts_ms"],
            "after_close": t > exit_fill["fill_ts_ms"],
            "fee_receipts": audit["evidence"]["fee_receipts"],
        }
    return {
        "ticker": intent["ticker"],
        "side": side,
        "universe": intent["universe"],
        "intent": key,
        "submission": submission,
        "quote_cents": intent["quote_price_cents"],
        "submission_ms": intent["created_ts_ms"],
        "submission_size_at_quote": level.quantity if level else None,
        "submission_market_state": market_fields(archive, submission["market_receipts"][0]),
        "entry_id": entry["id"],
        "entry_ms": entry["fill_ts_ms"],
        "entry_fee": entry_fee,
        "entry_fee_matches": float(entry_fee) == entry["fill_fee_cents"],
        "entry_price": price,
        "size": n,
        "entry_profile": entry_profile.audit(),
        "trace": queue_trace(archive, db, intent),
        "exit_id": exit_fill["id"],
        "exit_ms": exit_fill["fill_ts_ms"],
        "exit_reason": exit_fill["note"],
        "exit_evidence": exit_ev,
        "exit": exit_sale,
        "marks": marks,
    }


def analyze(directory: Path, intent_id: str) -> dict[str, Any]:
    manifest_bytes = (directory / "experiment.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["experiment_id"] != COHORT or not manifest["append_only"]:
        raise ValueError("Expected an append-only B3B1 backup")
    if any(
        (directory / Path(manifest[k]).name).resolve() == Path(manifest[k]).resolve()
        for k in ("paper_db", "recorder_db")
    ):
        raise ValueError("Supply an offline backup of the run")
    with open_archive(directory) as archive:
        db = archive.ledger
        if db is None or [r[0] for r in db.execute("SELECT id FROM paper_experiments")] != [COHORT]:
            raise ValueError("Expected matching B3B1 ledger")
        audit = evidence_audit(archive)
        if audit["classification"] != "verified_links":
            raise ValueError(f"Archive evidence classification: {audit['classification']}")
        return {
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "experiment": manifest["experiment_id"],
            "source_commit": manifest["commit"],
            "source_sha256": manifest["source_sha256"],
            "audit": audit,
            "recorder_kinds": rows(
                archive.recorder,
                "SELECT kind, COUNT(*) n FROM market_events GROUP BY kind ORDER BY kind",
            ),
            "capture_range_ns": list(
                archive.recorder.execute(
                    "SELECT MIN(received_ns),MAX(received_ns) FROM market_events"
                ).fetchone()
            ),
            "intent_status": rows(
                db,
                "SELECT status,COUNT(*) n FROM quote_intents WHERE id NOT LIKE 'exit_intent:%' GROUP BY status ORDER BY status",
            ),
            "funnel": funnel(db),
            "trade": lifecycle(archive, db, intent_id),
            "positions": rows(
                db,
                "SELECT ticker,side,net_contracts,realized_pnl_cents,open_entry_fees_cents FROM paper_positions ORDER BY ticker,side",
            ),
            "portfolio": rows(
                db,
                """SELECT ts_ms,realized_pnl_cents,unrealized_pnl_cents,total_pnl_cents,
                    open_positions_count,open_orders_count FROM pnl_snapshots ORDER BY ts_ms,id""",
            ),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--intent", required=True, help="Entry intent of the completed lifecycle")
    args = parser.parse_args()
    print(
        json.dumps(analyze(args.archive, args.intent), sort_keys=True, default=str, allow_nan=False)
    )


if __name__ == "__main__":
    main()
