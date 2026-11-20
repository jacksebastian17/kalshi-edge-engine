"""Historical entry evidence. All reads use one SQLite snapshot of polls and ledger."""

import json
import logging
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kalshi_edge_engine.engine.paper_sim import Fill
from kalshi_edge_engine.kalshi.market import MarketObservation

logger = logging.getLogger(__name__)

_TERMINAL_REASON = """CASE WHEN status IN ('OPEN', 'NEW') THEN NULL
                           WHEN status = 'FILLED' THEN 'filled'
                           ELSE COALESCE(market_cancel_reason, terminal_detail, LOWER(status)) END"""


def create_poll_table(cursor: sqlite3.Cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS entry_polls (
            id INTEGER PRIMARY KEY,
            intent_id TEXT NOT NULL REFERENCES quote_intents(id),
            ts_ms INTEGER NOT NULL,
            poll_class TEXT NOT NULL,
            evidence TEXT NOT NULL,
            candidate_id TEXT UNIQUE,
            candidate TEXT,
            policy_accepted INTEGER NOT NULL DEFAULT 0,
            rejection_reason TEXT
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS entry_polls_intent ON entry_polls(intent_id)")


class FillFunnel:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.read_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        self.write_errors = 0
        self._gaps: set[str] = set()

    def _write_error(self, intent_id: str) -> None:
        self.write_errors += 1
        self._gaps.add(intent_id)
        logger.exception("entry_poll_history_incomplete", extra={"intent_id": intent_id})

    def flush_gaps(self) -> None:
        if not self._gaps:
            return
        try:
            with sqlite3.connect(self.db_path, timeout=0.1) as conn:
                conn.executemany(
                    """UPDATE quote_intents SET submission_evidence =
                           json_set(submission_evidence, '$.incomplete_poll_history', 1)
                       WHERE id = ?""",
                    ((key,) for key in self._gaps),
                )
            self._gaps.clear()
        except sqlite3.Error:
            logger.exception("entry_poll_gap_marker_failed")

    def record_poll(self, intent_id: str, update: dict[str, Any]) -> int | None:
        self.flush_gaps()
        evidence = update.get("diagnostics", {})
        fill = update.get("fill")
        candidate = asdict(fill) if isinstance(fill, Fill) else None
        try:
            with sqlite3.connect(self.db_path, timeout=0.1) as conn:
                cursor = conn.execute(
                    """INSERT INTO entry_polls
                   (intent_id, ts_ms, poll_class, evidence, candidate_id, candidate)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        intent_id,
                        update.get("last_update_ts_ms", int(time.time() * 1000)),
                        evidence.get("poll_class", "unprocessed"),
                        json.dumps(evidence),
                        candidate["id"] if candidate else None,
                        json.dumps(candidate) if candidate else None,
                    ),
                )
                return cursor.lastrowid
        except sqlite3.Error:
            self._write_error(intent_id)
            return None

    def decision(
        self,
        intent_id: str,
        poll_id: int | None,
        *,
        accepted: bool = False,
        rejection: str | None = None,
        observation: MarketObservation | None = None,
    ) -> None:
        if poll_id is None:
            return
        try:
            with sqlite3.connect(self.db_path, timeout=0.1) as conn:
                conn.execute(
                    """UPDATE entry_polls SET policy_accepted = ?, rejection_reason = ?,
                           evidence = json_patch(evidence, ?) WHERE id = ?""",
                    (
                        accepted,
                        rejection,
                        json.dumps({"market_at_validation": asdict(observation)})
                        if observation
                        else "{}",
                        poll_id,
                    ),
                )
        except sqlite3.Error:
            self._write_error(intent_id)

    def aggregate(self) -> dict[str, Any]:
        with sqlite3.connect(self.read_uri, uri=True) as conn:
            conn.execute("BEGIN")
            outcomes = dict(
                conn.execute(
                    """SELECT status, COUNT(*) FROM quote_intents
                   WHERE submission_evidence IS NOT NULL AND id NOT LIKE 'exit_intent:%'
                   GROUP BY status"""
                )
            )
            legacy = conn.execute(
                """SELECT COUNT(*) FROM quote_intents
                   WHERE submission_evidence IS NULL AND id NOT LIKE 'exit_intent:%'"""
            ).fetchone()[0]
            terminal = dict(
                conn.execute(f"""
                SELECT {_TERMINAL_REASON}, COUNT(*) FROM quote_intents
                WHERE submission_evidence IS NOT NULL AND id NOT LIKE 'exit_intent:%'
                  AND status NOT IN ('OPEN', 'NEW') GROUP BY 1""")
            )
            incomplete = conn.execute("""SELECT COUNT(*) FROM quote_intents
                WHERE json_extract(submission_evidence, '$.incomplete_poll_history') = 1""").fetchone()[
                0
            ]
            classes = dict(conn.execute("SELECT poll_class, COUNT(*) FROM entry_polls GROUP BY 1"))
            reasons = dict(
                conn.execute(
                    """SELECT rejection_reason, COUNT(*) FROM entry_polls
                   WHERE rejection_reason IS NOT NULL GROUP BY 1"""
                )
            )
            no_candidate = dict(
                conn.execute(
                    """SELECT json_extract(evidence, '$.no_candidate_reason'), COUNT(*)
                   FROM entry_polls WHERE json_extract(evidence, '$.no_candidate_reason') IS NOT NULL
                   GROUP BY 1"""
                )
            )
            candidates, accepted, persisted, undecided = conn.execute(
                """SELECT COUNT(*), COUNT(CASE WHEN p.policy_accepted OR f.id IS NOT NULL THEN 1 END),
                          COUNT(f.id), COUNT(CASE WHEN f.id IS NULL AND p.rejection_reason IS NULL
                                                  AND NOT p.policy_accepted THEN 1 END)
                   FROM entry_polls p LEFT JOIN paper_fills f
                     ON f.id = p.candidate_id AND f.intent_id = p.intent_id
                   WHERE p.candidate_id IS NOT NULL"""
            ).fetchone()
            ledger_fills = conn.execute(
                """SELECT COUNT(*) FROM paper_fills f JOIN quote_intents i ON i.id = f.intent_id
                   WHERE i.submission_evidence IS NOT NULL AND i.id NOT LIKE 'exit_intent:%'
                     AND COALESCE(f.note, '') NOT LIKE 'exit%'"""
            ).fetchone()[0]
            seen = conn.execute(
                """SELECT COUNT(*) FROM quote_intents i WHERE i.submission_evidence IS NOT NULL
                   AND i.id NOT LIKE 'exit_intent:%'
                   AND (json_extract(i.submission_evidence, '$.displayed_quote_size') > 0 OR EXISTS (
                      SELECT 1 FROM entry_polls p WHERE p.intent_id = i.id
                      AND json_extract(p.evidence, '$.displayed_quote_size') > 0))"""
            ).fetchone()[0]
        return {
            "instrumented_intents": sum(outcomes.values()),
            "legacy_intents_without_poll_history": legacy,
            "intents_by_status": outcomes,
            "terminal_reasons": terminal,
            "diagnostic_write_errors_this_process": self.write_errors,
            "intents_with_marked_history_gaps": incomplete,
            "intents_with_quote_observed": seen,
            "polls": sum(classes.values()),
            "polls_by_class": classes,
            "polls_by_rejection_reason": reasons,
            "polls_by_no_candidate_reason": no_candidate,
            "simulated_candidates": candidates,
            "policy_accepted_candidates": accepted,
            "persisted_candidate_fills": persisted,
            "ledger_entry_fills_for_instrumented_intents": ledger_fills,
            "candidates_without_recorded_decision": undecided,
        }

    def intent(self, intent_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.read_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            row = conn.execute(
                f"""SELECT id, ticker, universe, side, quote_price_cents, size_contracts, created_ts_ms,
                          best_bid_at_submit_cents, cross_at_submit_cents, entry_fee_cents, fee_mode,
                          fee_profile, market_at_submit, status, market_cancel_reason, terminal_detail,
                          {_TERMINAL_REASON} AS terminal_reason, submission_evidence
                   FROM quote_intents WHERE id = ? AND id NOT LIKE 'exit_intent:%'""",
                (intent_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            for key in ("fee_profile", "market_at_submit", "submission_evidence"):
                result[key] = json.loads(row[key]) if row[key] else None
            result["persisted_fills"] = [
                dict(fill)
                for fill in conn.execute(
                    """SELECT id, fill_ts_ms, fill_price_cents, fill_size, fill_fee_cents,
                              execution_source, note, market_timing_invalidated
                       FROM paper_fills WHERE intent_id = ? AND COALESCE(note, '') NOT LIKE 'exit%'
                       ORDER BY fill_ts_ms, id""",
                    (intent_id,),
                )
            ]
            polls = []
            for poll in conn.execute(
                """SELECT p.*, f.id AS persisted_fill_id FROM entry_polls p
                   LEFT JOIN paper_fills f ON f.id = p.candidate_id AND f.intent_id = p.intent_id
                   WHERE p.intent_id = ? ORDER BY p.id""",
                (intent_id,),
            ):
                item = dict(poll)
                item["evidence"] = json.loads(poll["evidence"])
                item["candidate"] = json.loads(poll["candidate"]) if poll["candidate"] else None
                item["policy_accepted"] = (
                    True
                    if poll["policy_accepted"] or poll["persisted_fill_id"]
                    else False
                    if poll["candidate_id"] and poll["rejection_reason"]
                    else None
                )
                polls.append(item)
        initial = result["submission_evidence"]
        result["historical_poll_coverage"] = (
            "instrumented" if initial is not None else "unavailable"
        )
        if initial and initial.get("incomplete_poll_history"):
            result["historical_poll_coverage"] = "incomplete"
        result["poll_count"] = len(polls) if initial is not None else None
        result["polls"] = polls
        observed = [p["evidence"] for p in polls]
        if initial and initial.get("book_observed_ts_ms") is not None:
            observed.append(initial)
        result["quote_ever_observed"] = (
            any((e.get("displayed_quote_size") or 0) > 0 for e in observed)
            if any("displayed_quote_size" in e for e in observed)
            else None
        )
        if result["historical_poll_coverage"] == "incomplete" and not result["quote_ever_observed"]:
            result["quote_ever_observed"] = None
        return result
