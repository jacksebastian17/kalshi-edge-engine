import hashlib
import json
import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest

from tests import test_decision_evidence
from tools.b3b1_forensics import COHORT, analyze, funnel

verified_pair = test_decision_evidence.linked_archive


@pytest.fixture
def completed(verified_pair):
    directory = verified_pair
    manifest = {
        "experiment_id": COHORT,
        "commit": "fixture",
        "source_sha256": "fixture",
        "append_only": True,
        "paper_db": str(directory.parent / "paper_trading.db"),
        "recorder_db": str(directory.parent / "market_data_recorder.db"),
    }
    (directory / "experiment.json").write_text(json.dumps(manifest))
    with closing(
        sqlite3.connect(
            (directory / "paper_trading.db").as_uri() + "?immutable=1&mode=ro", uri=True
        )
    ) as db:
        (key,) = db.execute(
            "SELECT intent_id FROM paper_fills WHERE note='entry_queue_cleared'"
        ).fetchone()
    return directory, key


async def test_forensics_rebuilds_prices_fees_and_queue_without_writing(completed):
    directory, key = completed

    def hashes():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}

    before = hashes()
    result = analyze(directory, key)
    assert result == analyze(directory, key)
    assert hashes() == before
    assert result["funnel"]["candidate_outcomes"] == {"persisted": 1}
    trade = result["trade"]
    assert trade["trace"][-1]["book"]["sequence"] == 11
    assert all(row["matches_journal"] for row in trade["trace"])
    assert trade["marks"][5]["matches_ledger"] and "issue" in trade["marks"][60]
    assert trade["exit"]["matches_ledger"] and trade["entry_fee_matches"]
    assert trade["exit"]["net"] == Decimal(str(trade["exit"]["recorded_price_fee_pnl"][2]))
    assert trade["exit"]["matches_recorded_levels"]


async def test_forensics_exposes_ledger_discrepancy_and_refuses_unverified_links(completed):
    directory, key = completed
    with closing(sqlite3.connect(directory / "paper_trading.db")) as db, db:
        db.execute("UPDATE paper_fills SET realized_pnl_delta_cents=1000 WHERE note LIKE 'exit%'")
    assert not analyze(directory, key)["trade"]["exit"]["matches_ledger"]
    with closing(sqlite3.connect(directory / "paper_trading.db")) as db, db:
        db.execute(
            "UPDATE entry_polls SET evidence=json_remove(evidence,'$.book_receipts') WHERE candidate_id IS NOT NULL"
        )
    with pytest.raises(ValueError, match="incomplete"):
        analyze(directory, key)


async def test_candidate_classification_keeps_validation_and_policy_distinct(completed):
    directory, _ = completed
    with closing(sqlite3.connect(directory / "paper_trading.db")) as db, db:
        db.row_factory = sqlite3.Row
        db.execute(
            "UPDATE entry_polls SET candidate_id='x', rejection_reason='horizon_elapsed_at_validation' WHERE id=1"
        )
        db.execute(
            "UPDATE entry_polls SET candidate_id='y', rejection_reason='fill_path_price_improves_disabled' WHERE id=2"
        )
        result = funnel(db)
    assert result["candidate_outcomes"] == {
        "persisted": 1,
        "horizon_elapsed_at_validation": 1,
        "fill_path_price_improves_disabled": 1,
    }
    assert sum(result["candidates_by_event"].values()) == 3
    assert len(result["candidates_by_event"]) == result["candidate_intents"] == 1
    assert [r["poll"] for r in result["other_rejections"]] == [1]
