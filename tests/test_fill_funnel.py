import sqlite3
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from kalshi_edge_engine.api import main
from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.paper_sim import OrderRequest, RealisticOrderSimulator


def _intent(key: str = "entry") -> QuoteIntent:
    return QuoteIntent(key, "GAME", "NO", 41, 1, 1000, 4, 8, 8, "maker", True)


async def test_actual_polls_classified_and_queue_evidence_survives_restart(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    store.insert_intent(_intent())
    client = AsyncMock()

    def book(*levels):
        return {"orderbook_fp": {"no_dollars": list(levels), "yes_dollars": [["0.45", "30"]]}}

    client.get_orderbook.side_effect = [
        book(["0.40", "8"]),
        book(["0.41", "8"]),
        book(["0.41", "3"]),
        book(["0.43", "9"], ["0.41", "0.50"]),
        {},
        RuntimeError("read failed"),
        book(["0.41", "1"]),
        book(["0.41", "8"]),
        book(["0.41", "2"]),
    ]

    def observe(update):
        store.fill_funnel.record_poll("entry", update)

    order = OrderRequest(
        "entry",
        "GAME",
        "NO",
        41,
        1,
        1000,
        4,
        1,
        60,
        on_unprocessed_poll=lambda reason: observe(
            {"diagnostics": {"no_candidate_reason": reason}}
        ),
    )
    sim = RealisticOrderSimulator(
        poll_interval_seconds=0,
        fill_probability_price_improves=0,
        fill_probability_price_moved=0,
        fill_probability_at_best=1,
    )
    fills, _ = await sim.simulate_order_fills(client, order, horizon_seconds=2, on_poll=observe)
    assert [fill.fill_path_type for fill in fills] == ["queue_cleared"]
    reopened = PaperStore(store.db_path)
    assert reopened.recover_interrupted_intents() == 1
    history = reopened.fill_funnel.intent("entry")
    assert history["quote_ever_observed"] is True
    assert history["terminal_reason"] == "restart"
    polls = history["polls"]
    assert polls[0]["evidence"]["displayed_quote_size"] is None
    assert polls[2]["evidence"]["queue_ahead_before_decrement"] == 8
    assert polls[2]["evidence"]["queue_ahead_after_poll"] == 3
    assert polls[2]["evidence"]["no_candidate_reason"] == "insufficient_volume_beyond_queue"
    assert polls[3]["evidence"]["displayed_quote_size"] == 0.5
    assert polls[5]["evidence"]["no_candidate_reason"] == "poll_error:RuntimeError"
    assert polls[-1]["evidence"]["volume_is_confirmed_trade_data"] is False
    funnel = reopened.fill_funnel.aggregate()
    assert funnel["polls_by_class"] == {
        "price_improving": 1,
        "at_displayed_quote": 5,
        "moved_through": 1,
        "ineligible": 1,
        "unprocessed": 1,
    }
    assert funnel["intents_with_quote_observed"] == 1
    assert funnel["simulated_candidates"] == funnel["candidates_without_recorded_decision"] == 1
    assert funnel["policy_accepted_candidates"] == funnel["persisted_candidate_fills"] == 0


def test_migration_and_historical_routes_require_no_client(tmp_path, monkeypatch):
    db = str(tmp_path / "paper.db")
    store = PaperStore(db)
    store.insert_intent(_intent("old"), best_bid_at_submit_cents=40)
    store.update_intent_status("old", "EXPIRED")
    store.insert_intent(_intent("exit_intent:old"))
    # Reproduce the previous schema on a disposable database.
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE entry_polls")
        conn.execute("ALTER TABLE quote_intents DROP COLUMN submission_evidence")
        conn.execute("ALTER TABLE quote_intents DROP COLUMN terminal_detail")
    PaperStore(db)
    store = PaperStore(db)
    monkeypatch.setattr(main, "_paper_store", store)
    client = TestClient(main.create_app(testing=True))
    history = client.get("/paper/diagnostics/intents/old").json()
    assert history["best_bid_at_submit_cents"] == 40
    assert history["historical_poll_coverage"] == "unavailable"
    assert history["quote_ever_observed"] is history["poll_count"] is None
    assert history["terminal_reason"] == "expired"
    assert client.get("/paper/diagnostics/intents/missing").status_code == 404
    assert client.get("/paper/diagnostics/intents/exit_intent:old").status_code == 404
    store.insert_intent(_intent())
    store.update_intent_status("entry", "ERROR", terminal_detail="simulation_error:ValueError")
    funnel = client.get("/paper/diagnostics/fill-funnel").json()
    assert funnel["instrumented_intents"] == funnel["legacy_intents_without_poll_history"] == 1
    assert funnel["terminal_reasons"] == {"simulation_error:ValueError": 1}
    assert client.get("/market-data/recorder/status").json()["enabled"] is False


def test_journal_failure_marks_partial_history(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    store.insert_intent(_intent())
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert store.fill_funnel.record_poll("entry", {}) is None
    store.fill_funnel.flush_gaps()
    assert (
        PaperStore(store.db_path).fill_funnel.intent("entry")["historical_poll_coverage"]
        == "incomplete"
    )
