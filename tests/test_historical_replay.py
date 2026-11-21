import hashlib
import json
import socket
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_edge_engine.db.market_recorder import MarketRecorder
from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.fees import SERIES_FEE_VERSION, FeeProfile
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.kalshi.market import MarketObservation
from kalshi_edge_engine.replay.__main__ import main
from kalshi_edge_engine.replay.archive import Selection, open_archive, validate
from kalshi_edge_engine.replay.execution import draw
from kalshi_edge_engine.replay.forensics import investigate
from kalshi_edge_engine.replay.research import analyze

T = 1700000000000
KEY = "entry"
FEE = FeeProfile(
    SERIES_FEE_VERSION, "SER", "E", "quadratic_with_maker_fees", Decimal(1), "fixture", T - 100
)
CFG = {
    "fill_horizon_seconds": 60,
    "simulated_volume_per_poll": 50,
    "fill_probability_at_best": 1,
    "fill_probability_price_improves": 1,
    "fill_probability_price_moved": 1,
    "allow_price_improves": False,
    "allow_moved_through": False,
}


def book(bid, size):
    return {"orderbook": {"yes": [[bid, size]], "no": [[45, 20]]}}


def market(status="active"):
    return {
        "market": {
            "ticker": "A",
            "status": status,
            "close_time": "2030-01-01T00:00:00Z",
            "updated_time": "2020-01-01T00:00:00Z",
        }
    }


def add(
    path,
    seq,
    ms,
    body,
    *,
    kind="orderbook",
    source="entry_simulator",
    key=KEY,
    session="first",
    ticker="A",
    event_id=None,
):
    with sqlite3.connect(path / "market_data_recorder.db") as conn:
        conn.execute(
            """INSERT INTO market_events VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                session,
                seq,
                (T + ms) * 10**6,
                kind,
                "/markets/" + ticker + ("/orderbook" if kind == "orderbook" else ""),
                "fixture",
                "entry",
                ticker,
                "NBA",
                source,
                key,
                "market.updated_time" if kind == "market" else None,
                "2020-01-01T00:00:00Z" if kind == "market" else None,
                body if isinstance(body, bytes) else json.dumps(body).encode(),
            ),
        )


@pytest.fixture
def archive_dir(tmp_path):
    path = tmp_path / "sealed"
    path.mkdir()
    MarketRecorder(str(path / "market_data_recorder.db"), 1, 1, lambda _ticker: "NBA")._initialize()
    store = PaperStore(str(tmp_path / "disposable-worker.db"), clock_ms=lambda: T)
    order = QuoteIntent(KEY, "A", "YES", 41, 1, T, 5, 8, 8, "maker", True, universe="NBA")
    store.insert_intent(
        order,
        universe="NBA",
        entry_fee_cents=FEE.fee_cents(41, 1, liquidity="maker"),
        market_observation=MarketObservation("A", "active", T - 2, T - 1, 1893456000000),
        fee_profile=FEE,
        best_bid_at_submit_cents=40,
        cross_at_submit_cents=55,
        submission_evidence={
            "execution_config": CFG,
            "signal_ts_ms": T - 5,
            "book_observed_ts_ms": T - 3,
        },
    )
    add(path, 1, -5, book(40, 10), source="scanner", key=None)
    add(path, 2, -3, book(40, 10), source="entry_submission")
    add(path, 3, -1, market(), kind="market", source="entry_submission")
    for i, (ms, bid, n) in enumerate(
        ((10, 40, 8), (30, 44, 9), (50, 41, 4), (70, 41, 9), (90, 41, 3))
    ):
        add(path, 4 + 2 * i, ms, book(bid, n))
        add(path, 5 + 2 * i, ms + 3, market(), kind="market")
    add(path, 14, 5300, book(44, 20), source="scanner", key=None)
    with (
        closing(sqlite3.connect(store.db_path)) as worker,
        closing(sqlite3.connect(path / "paper_trading.db")) as snapshot,
    ):
        worker.backup(snapshot)
        snapshot.execute("PRAGMA journal_mode=DELETE")
    return path


def run(path, **kwargs):
    with open_archive(path) as archive:
        return analyze(archive, kwargs.pop("selection", Selection()), **kwargs)


def test_causal_queue_policy_comparison_fees_and_provenance(archive_dir):
    report = run(archive_dir)
    assert report["coverage"]["segments"][0]["coverage"] == "complete_relative_to_capture_contract"
    assert "missing_persisted_polls" in report["coverage"]["intent_evidence_gaps"][KEY]
    assert report["submission_agreements"] == {
        "best_bid_at_submit_cents": 1,
        "cross_at_submit_cents": 1,
        "quote_price_cents": 1,
    }
    assert report["reconciliation"]["reconstructed_poll_classes"] == {
        "price_improving": 1,
        "moved_through": 1,
        "at_displayed_quote": 3,
    }
    for name, path, book_id in (
        ("baseline", "queue_cleared", 12),
        ("moved_through_research", "moved_through", 6),
        ("price_improving_research", "price_improves", 4),
    ):
        model = report["models"][name]
        [fill] = model["fill_details"]
        assert (fill["path"], fill["book_id"], fill["validation_id"]) == (
            path,
            book_id,
            book_id + 1,
        )
        assert fill["entry_fee_cents"] == 0.43
        assert fill["mark_5s"]["net_cents"] == pytest.approx(
            3 - 0.43 - FEE.fee_cents(44, 1, liquidity="taker", sale=True)
        )
        assert model["filled_contracts_by_ticker"] == {"A": 1}
    assert set(report["sha256"]) == {"market_data_recorder.db", "paper_trading.db"}
    assert report["effective_configurations"][0] == CFG


def test_identical_seed_outputs_and_no_credentials_network_or_mutation(archive_dir, monkeypatch):
    from kalshi_edge_engine.kalshi.client import KalshiClient

    def forbidden(*_args, **_kwargs):
        pytest.fail("Live dependency used during replay")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(KalshiClient, "__init__", forbidden)
    monkeypatch.setenv("KALSHI_API_KEY_FILE", str(archive_dir / "does-not-exist"))
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in archive_dir.iterdir()}
    a, b = run(archive_dir, seed=42, scale=0.5), run(archive_dir, seed=42, scale=0.5)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert draw(42, "first", 4, KEY) != draw(43, "first", 4, KEY)
    actual = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in archive_dir.iterdir()}
    assert before == actual == a["sha256"]
    with (
        open_archive(archive_dir) as archive,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        archive.recorder.execute("DELETE FROM market_events")


def test_time_window_and_future_validation_cannot_fill_earlier(archive_dir):
    seen = []
    selected = Selection(("A",), "first", None, (T + 93) * 10**6)
    r = run(archive_dir, selection=selected, emit=seen.append)
    assert r["models"]["baseline"]["hypothetical_fills"] == 0
    assert r["models"]["baseline"]["reasons"]["missing_validation_response"] == 1
    assert seen[-1]["id"] == 12
    assert seen[2]["source_time"] == "2020-01-01T00:00:00Z"
    before = run(archive_dir)["models"]
    add(archive_dir, 15, 6000, market("closed"), kind="market")
    assert run(archive_dir)["models"] == before
    late = run(archive_dir, selection=Selection(start_ns=(T + 9) * 10**6))
    assert all(m["hypothetical_fills"] == 0 for m in late["models"].values())
    assert (
        run(archive_dir, selection=Selection(("other",)))["coverage"]["coverage"]
        == "insufficient_evidence"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("closed", "entry_inactive_at_validation"),
        ("horizon", "horizon_elapsed_at_validation"),
        ("stale", "market_status_stale_or_incomplete"),
    ],
)
def test_market_and_horizon_guards(archive_dir, change, reason):
    with sqlite3.connect(archive_dir / "market_data_recorder.db") as conn:
        if change == "closed":
            conn.execute(
                "UPDATE market_events SET body=? WHERE id=13",
                (json.dumps(market("closed")).encode(),),
            )
        if change == "stale":
            conn.execute(
                "UPDATE market_events SET received_ns=? WHERE id=13", ((T + 2500) * 10**6,)
            )
    if change == "horizon":
        with sqlite3.connect(archive_dir / "paper_trading.db") as conn:
            conn.execute(
                "UPDATE quote_intents SET submission_evidence=json_set(submission_evidence,'$.execution_config.fill_horizon_seconds',0.093)"
            )
    m = run(archive_dir)["models"]["baseline"]
    assert not m["fill_details"]
    assert m["reasons"][reason] >= 1


def test_invalid_data_gaps_clock_restart_and_equal_time_order(archive_dir):
    with sqlite3.connect(archive_dir / "market_data_recorder.db") as conn:
        conn.execute("DELETE FROM market_events WHERE id<=2 OR id=5")
    add(archive_dir, 15, 5300, b"{")
    add(archive_dir, 14, 5200, book(41, 1))
    add(archive_dir, 1, 5200, market(), session="second", kind="market")
    events = []
    r = run(archive_dir, emit=events.append)
    c = r["coverage"]
    assert c["coverage"] == "partially_observed"
    assert {
        "sequence_gap",
        "missing_prefix",
        "invalid_payload",
        "session_boundary",
        "clock_reversal",
        "out_of_order_sequence",
        "missing_stored_ids",
    } <= {i["reason"] for i in c["issues"]}
    assert [e["id"] for e in events][-3:] == [14, 16, 17]
    assert events[-2]["received_ns"] == events[-1]["received_ns"]
    assert not any(m["fill_details"] for m in r["models"].values())
    with open_archive(archive_dir) as archive:
        scoped = validate(archive, Selection(session="second"))
        assert scoped["selected_events"] == 1
        assert scoped["missing_kinds_by_ticker"] == {"A": ["orderbook"]}


def test_missing_raw_history_and_evidence_discrepancy(archive_dir):
    with sqlite3.connect(archive_dir / "paper_trading.db") as conn:
        for ts, kind in [(T - 100, "moved_through"), (T + 90, "ineligible")]:
            conn.execute(
                "INSERT INTO entry_polls(intent_id,ts_ms,poll_class,evidence) VALUES (?,?,?,?)",
                (KEY, ts, kind, json.dumps({"poll_class": kind, "market_best_bid": 88})),
            )
    rec = run(archive_dir)["reconciliation"]
    assert rec["counts"]["unmatched_polls"] == 1 and rec["counts"]["aligned_polls"] == 1
    diff = next(d for d in rec["discrepancies"] if d["metric"] == "market_best_bid")
    assert diff["difference"] == -47 and diff["replay_received_ns"] == (T + 90) * 10**6
    with sqlite3.connect(archive_dir / "market_data_recorder.db") as conn:
        conn.execute("DELETE FROM market_events WHERE source='entry_simulator'")
    report = run(archive_dir)
    assert report["coverage"]["intents_without_simulator_books"] == [KEY]
    with open_archive(archive_dir) as archive:
        assert len(investigate(archive, KEY)["polls"]) == 2


def test_fractional_depth_sensitivity(archive_dir):
    with sqlite3.connect(archive_dir / "market_data_recorder.db") as conn:
        for seq, size in ((8, 4), (10, 9), (12, 3)):
            body = {
                "orderbook_fp": {
                    "yes_dollars": [["0.41", str(size)], ["0.44", "55.75"]],
                    "no_dollars": [["0.45", "20"]],
                }
            }
            conn.execute(
                "UPDATE market_events SET body=? WHERE id=?", (json.dumps(body).encode(), seq)
            )
    assert run(archive_dir)["models"]["baseline"]["fills_with_raw_top_disagreement"] == 1
    strict = run(archive_dir, require_raw_consistency=True)["models"]["baseline"]
    assert strict["hypothetical_fills"] == 0
    assert strict["reasons"]["raw_top_disagrees_with_simulator"] == 1


def test_cli_outputs_offline_and_refuses_unsafe_inputs(archive_dir, tmp_path):
    out = tmp_path / "results"
    assert main(["replay", "--archive", str(archive_dir), "--events", "--out", str(out)]) == 0
    data = json.loads((out / "replay.json").read_text())
    assert (
        data["engine_version"] and len((out / "observations.jsonl").read_text().splitlines()) == 14
    )
    decisions = [
        json.loads(line) for line in (out / "reconstructed.jsonl").read_text().splitlines()
    ]
    assert len(decisions) == 5
    assert decisions[-1]["reconstructed"]["eligibility"]["queue_cleared"]
    assert decisions[-1]["validation_id"] == 13 and decisions[-1]["research_guard"] is None
    assert main(["replay", "--archive", str(archive_dir), "--out", str(archive_dir)]) == 2
    assert main(["inspect", "--archive", str(Path(__file__).resolve().parents[1])]) == 2
    (archive_dir / "paper_trading.db-wal").touch()
    with pytest.raises(ValueError, match="sidecars"), open_archive(archive_dir):
        pass


def test_same_session_ties_and_missing_validation_do_not_borrow_later_metadata(archive_dir):
    with sqlite3.connect(archive_dir / "market_data_recorder.db") as conn:
        conn.execute("UPDATE market_events SET received_ns=? WHERE id=5", ((T + 10) * 10**6,))
        # Keep the recorder sequence continuous. This validation belongs to another order.
        conn.execute("UPDATE market_events SET intent_id='unrelated' WHERE id=13")
    events = []
    report = run(archive_dir, emit=events.append)
    assert events[3]["received_ns"] == events[4]["received_ns"]
    assert [e["id"] for e in events[3:5]] == [4, 5]
    assert report["models"]["baseline"]["hypothetical_fills"] == 0
    assert report["models"]["baseline"]["reasons"]["missing_validation_response"] == 1
    assert report["coverage"]["recorded_intent_ids_missing_in_ledger"] == ["unrelated"]


def test_archive_specific_intent_forensics_survive_pruned_raw_data(archive_dir):
    with sqlite3.connect(archive_dir / "paper_trading.db") as conn:
        conn.executemany(
            """INSERT INTO paper_fills
            (id,intent_id,fill_ts_ms,fill_price_cents,fill_size,remaining_size,fill_fee_cents,
             position_lifecycle_id,note,bid_levels,execution_source)
             VALUES (?,?,?,?,1,0,?,?,?,?,?)""",
            [
                (
                    "sim",
                    KEY,
                    T + 93,
                    41,
                    0.43,
                    KEY,
                    "entry_queue_cleared",
                    None,
                    "simulated_displayed_size_decrease",
                ),
                (
                    "exit",
                    "exit_intent:test",
                    T + 390,
                    44,
                    1.73,
                    KEY,
                    "exit_take_profit",
                    "[[44,1]]",
                    "simulated_displayed_bid_sweep",
                ),
            ],
        )
        conn.execute(
            "UPDATE paper_fills SET entry_market_evidence=? WHERE id='sim'",
            (json.dumps({"book_observed_ts_ms": T + 90}),),
        )
        conn.execute(
            """INSERT INTO entry_polls(intent_id,ts_ms,poll_class,evidence,candidate_id,candidate)
            VALUES (?,?,'at_displayed_quote',?,'sim',?)""",
            (
                KEY,
                T + 90,
                json.dumps(
                    {
                        "queue_ahead_before_decrement": 4,
                        "traded_volume_estimate": 6,
                        "traded_volume_source": "observed_size_delta",
                    }
                ),
                json.dumps({"fill_path_type": "queue_cleared"}),
            ),
        )
    with sqlite3.connect(archive_dir / "market_data_recorder.db") as conn:
        conn.execute(
            "UPDATE market_events SET received_ns=?,source='client',read_lane='inventory' WHERE id=14",
            ((T + 389) * 10**6,),
        )
    with open_archive(archive_dir) as archive:
        [trade] = investigate(archive, KEY)["trades"]
        assert trade["raw_entry_book_ids"] == [12]
        assert trade["inventory_book_ids_supporting_exit"] == [14]
        assert trade["recorded_queue_inference"]["excess_decrease"] == 2
        assert trade["duration_ms"] == 297 and trade["net_cents"] == 0.84
    with sqlite3.connect(archive_dir / "market_data_recorder.db") as conn:
        conn.execute("DELETE FROM market_events")
    with open_archive(archive_dir) as archive:
        [limited] = investigate(archive, KEY)["trades"]
        assert (
            limited["coverage"] == "limited_raw_evidence" and limited["nearby_observations"] == []
        )
        assert limited["net_cents"] == 0.84


def test_missing_ledger_and_corrupt_body_are_reported(archive_dir, tmp_path):
    import shutil

    rec_only = tmp_path / "recorder-only"
    rec_only.mkdir()
    shutil.copyfile(archive_dir / "market_data_recorder.db", rec_only / "market_data_recorder.db")
    add(rec_only, 15, 6000, b'{"orderbook":{"yes":[["0.4","Infinity"]]}}')
    report = run(rec_only)
    assert not report["coverage"]["ledger_available"]
    assert report["coverage"]["invalid_event_ids"] == [15]
    assert not report["models"]["baseline"]["fill_details"]
