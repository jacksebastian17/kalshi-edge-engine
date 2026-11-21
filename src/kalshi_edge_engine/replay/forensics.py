"""Retrospective evidence for one recorded entry and any linked inventory lifecycle."""

import json
from typing import Any

from kalshi_edge_engine.engine.orderbook import (
    canonical_book,
    displayed_quote_size,
    parse_orderbook,
    quote_liquidation,
)
from kalshi_edge_engine.replay import MATCH_MS, Mode, b1_orderbook
from kalshi_edge_engine.replay.archive import Archive, payload
from kalshi_edge_engine.replay.execution import Intent, raw_best_level

RAPID_INTENTS = ("5877fe52-337d-4c78-bc9b-bd9540a1727e", "b094c81f-445b-4b80-9c4f-89ac3dd64441")


def investigate(archive: Archive, key: str, mode: Mode = Mode.B2) -> dict[str, Any]:
    ledger = archive.ledger
    if ledger is None:
        return {
            "intent_id": key,
            "coverage": "insufficient_evidence",
            "reason": "No archived ledger",
        }
    row = ledger.execute(
        "SELECT * FROM quote_intents WHERE id=? AND id NOT LIKE 'exit_intent:%'", (key,)
    ).fetchone()
    if row is None:
        return {"intent_id": key, "coverage": "insufficient_evidence", "reason": "Intent absent"}
    intent = Intent.from_row(dict(row))
    polls = []
    for p in ledger.execute("SELECT * FROM entry_polls WHERE intent_id=? ORDER BY id", (key,)):
        polls.append(
            {
                **dict(p),
                "evidence": json.loads(p["evidence"]),
                "candidate": json.loads(p["candidate"] or "null"),
            }
        )
    takes = [
        dict(f)
        for f in ledger.execute(
            "SELECT * FROM paper_fills WHERE intent_id=? ORDER BY fill_ts_ms,id", (key,)
        )
    ]
    trades = []
    for fill in takes:
        validation = json.loads(fill["entry_market_evidence"] or "{}")
        ts = validation.get("book_observed_ts_ms", fill["fill_ts_ms"])
        preceding = [p for p in polls if p["ts_ms"] <= ts][-3:]
        exits = [
            dict(f)
            for f in ledger.execute(
                """SELECT * FROM paper_fills WHERE position_lifecycle_id=?
                 AND note LIKE 'exit_%' AND fill_ts_ms >= ? ORDER BY fill_ts_ms,id""",
                (fill["position_lifecycle_id"], fill["fill_ts_ms"]),
            )
        ]
        end = exits[0]["fill_ts_ms"] if exits else fill["fill_ts_ms"]
        observations = []
        for r in archive.recorder.execute(
            """SELECT * FROM market_events WHERE ticker=? AND
                received_ns >= ? AND received_ns < ? ORDER BY id""",
            (intent.order.ticker, (ts - 10000) * 10**6, (end + 1) * 10**6),
        ):
            try:
                raw = payload(r, mode)
            except (ValueError, TypeError, OverflowError):
                continue
            e = {
                k: r[k]
                for k in (
                    "id",
                    "session_id",
                    "sequence",
                    "ticker",
                    "universe",
                    "received_ns",
                    "source_time",
                    "source_time_field",
                    "source",
                    "read_lane",
                    "kind",
                    "intent_id",
                )
            }
            if r["kind"] == "orderbook":
                corrected = mode == Mode.CANONICAL
                size_at = displayed_quote_size if corrected else b1_orderbook.displayed_quote_size
                liquidation = quote_liquidation if corrected else b1_orderbook.quote_liquidation
                if corrected:
                    canonical = canonical_book(raw)
                    level = canonical.top(intent.order.side)
                    top: tuple[float, int | float | str] | None = (
                        (float(level.price), level.json_quantity()) if level else None
                    )
                    b1 = b1_orderbook.parse_orderbook(json.loads(r["body"]))
                    historical_bid = b1.bid_yes if intent.order.side == "YES" else b1.bid_no
                    e.update(
                        historical_simulator_bid_cents=historical_bid.price
                        if historical_bid
                        else None,
                        reported_quantity_exact=str(level.quantity) if level else None,
                        whole_capacity=level.capacity if level else None,
                    )
                    parsed = parse_orderbook(canonical)
                else:
                    top = raw_best_level(raw, intent.order.side)
                    parsed = b1_orderbook.parse_orderbook(raw)
                bid = parsed.bid_yes if intent.order.side == "YES" else parsed.bid_no
                e.update(
                    raw_best_bid_cents=top[0] if top else None,
                    raw_best_size=top[1] if top else None,
                    simulator_best_bid_cents=bid.price if bid else None,
                    displayed_size_at_entry=size_at(
                        raw, intent.order.side, intent.order.quote_price_cents
                    ),
                )
                if (
                    exits
                    and r["source"] == "client"
                    and r["read_lane"] == "inventory"
                    and intent.fee
                ):
                    exit_fill = exits[0]
                    depth = liquidation(
                        raw,
                        intent.order.side,
                        exit_fill["fill_size"],
                        r["received_ns"] // 10**6,
                        fee_profile=intent.fee,
                    )
                    e.update(
                        exit_size_at_recorded_price=size_at(
                            raw, intent.order.side, exit_fill["fill_price_cents"]
                        ),
                        sufficient_depth_at_recorded_vwap=bool(
                            depth and depth.price_cents == exit_fill["fill_price_cents"]
                        ),
                        age_at_exit_ms=end - r["received_ns"] // 10**6,
                    )
            else:
                e.update(
                    market_status=raw["market"].get("status"),
                    metadata_yes_bid_dollars=raw["market"].get("yes_bid_dollars"),
                )
            observations.append(e)
        historical = next((p for p in preceding if p["candidate_id"] == fill["id"]), None)
        # Close the book's alignment window at its validation response, as in reconciliation.
        # Multiple books with unresolved provenance remain ambiguous.
        pending_books: dict[str, dict[str, Any]] = {}
        validation_ms: dict[int, int] = {}
        for e in observations:
            if (
                e["intent_id"] != key
                or e["source"] != "entry_simulator"
                or e["universe"] != row["universe"]
            ):
                continue
            session = e["session_id"]
            if e["kind"] == "orderbook":
                pending_books[session] = e
            elif session in pending_books:
                validation_ms[pending_books.pop(session)["id"]] = e["received_ns"] // 10**6
        raw_books = [
            e
            for e in observations
            if e["kind"] == "orderbook"
            and e["intent_id"] == key
            and e["source"] == "entry_simulator"
            and e["universe"] == row["universe"]
            and 0 <= ts - e["received_ns"] // 10**6 <= MATCH_MS
            and ts <= validation_ms.get(e["id"], ts)
        ]
        inventory = [
            e
            for e in observations
            if "sufficient_depth_at_recorded_vwap" in e
            and e["received_ns"] // 10**6 >= fill["fill_ts_ms"]
            and 0 <= e["age_at_exit_ms"] <= 10_000
            and (len(raw_books) != 1 or raw_books[0]["session_id"] == e["session_id"])
        ]
        result: dict[str, Any] = {
            "entry_fill_id": fill["id"],
            "entry_source": fill["execution_source"],
            "book_observed_ms": ts,
            "entry_booked_ms": fill["fill_ts_ms"],
            "entry_price_cents": fill["fill_price_cents"],
            "entry_size": fill["fill_size"],
            "entry_fee_cents": fill["fill_fee_cents"],
            "validation": validation,
            "preceding_recorded_polls": preceding,
            "causal_candidate_poll_id": historical["id"] if historical else None,
            "raw_entry_book_ids": [e["id"] for e in raw_books],
            "inventory_book_ids_supporting_exit": [
                e["id"] for e in inventory if e["sufficient_depth_at_recorded_vwap"]
            ],
            "nearby_observations": observations,
            "coverage": "paired_raw_and_ledger"
            if len(raw_books) == 1 and inventory
            else "limited_raw_evidence",
            "linked_exits": [
                {
                    k: f[k]
                    for k in (
                        "id",
                        "fill_ts_ms",
                        "fill_size",
                        "fill_price_cents",
                        "fill_fee_cents",
                        "bid_levels",
                        "execution_source",
                        "realized_pnl_delta_cents",
                    )
                }
                for f in exits
            ],
        }
        if historical:
            d = historical["evidence"]
            decrease, ahead = d.get("traded_volume_estimate"), d.get("queue_ahead_before_decrement")
            result["recorded_queue_inference"] = {
                "volume_source": d.get("traded_volume_source"),
                "size_decrease": decrease,
                "estimated_ahead_before": ahead,
                "excess_decrease": decrease - ahead
                if decrease is not None and ahead is not None
                else None,
                "confirmed_trade_volume": False,
            }
        if len(takes) == 1 and len(exits) == 1 and fill["fill_size"] == exits[0]["fill_size"]:
            f = exits[0]
            gross = (f["fill_price_cents"] - fill["fill_price_cents"]) * fill["fill_size"]
            fees = fill["fill_fee_cents"] + f["fill_fee_cents"]
            result.update(
                duration_ms=end - fill["fill_ts_ms"],
                gross_cents=gross,
                total_fees_cents=round(fees, 6),
                net_cents=round(gross - fees, 6),
            )
        trades.append(result)
    return {
        "intent_id": key,
        "ticker": intent.order.ticker,
        "side": intent.order.side,
        "quote_cents": intent.order.quote_price_cents,
        "submission_evidence": json.loads(row["submission_evidence"] or "null"),
        "status": row["status"],
        "polls": polls,
        "intent_linked_receipts": [
            dict(r)
            for r in archive.recorder.execute(
                """SELECT id, received_ns, source_time_field, source_time, session_id, sequence, ticker,
                      universe, source, kind, read_lane FROM market_events WHERE intent_id=? ORDER BY id""",
                (key,),
            )
        ],
        "trades": trades,
        **(
            {
                "observation_semantics": mode.value,
                "trade_values": "Original B1 simulated ledger entries and exits.",
            }
            if mode != Mode.B2
            else {}
        ),
        "limitations": [
            "Ledger bid_levels records consumed size. Only a retained depth snapshot can independently check it.",
            "A matching inventory receipt is circumstantial because B1 did not persist its event ID.",
            "Cancellation, asynchronous sampling and queue priority are unresolved. Public trades and sequenced depth updates would improve attribution.",
        ],
    }
