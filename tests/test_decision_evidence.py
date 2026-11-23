"""End-to-end decision evidence against a concurrent, in-process HTTP transport."""

import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from kalshi_edge_engine import overnight
from kalshi_edge_engine.db.market_recorder import MarketRecorder, capture_origin
from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine import paper_sim
from kalshi_edge_engine.evidence import receipt_scope
from kalshi_edge_engine.kalshi.read_budget import ReadBudget
from kalshi_edge_engine.replay.archive import open_archive
from kalshi_edge_engine.replay.evidence import evidence_audit
from kalshi_edge_engine.runtime import (
    config,
    paper_exit_runtime,
    scanner_runtime,
    state,
)
from kalshi_edge_engine.runtime.market_fees import MarketFees
from tests.test_kalshi_read_budget import _client


async def test_same_ticker_receipts_follow_the_request_and_commit_boundary(tmp_path):
    rec = MarketRecorder(str(tmp_path / "rec.db"), 24, 4, lambda _: "NBA", append_only=True)
    writer = asyncio.create_task(rec.run())
    await rec.initialized.wait()
    release = asyncio.Event()

    async def respond(req):
        if req.url.path.endswith("/orderbook"):
            await release.wait()
        else:
            release.set()
        return httpx.Response(
            200,
            content=b'{"market":{"ticker":"GAME"},"orderbook_fp":{"yes_dollars":[[0.4100,1.50]],"no_dollars":[]}}',
        )

    async with _client(ReadBudget(1000, 1000), respond) as client:
        client.recorder = rec

        async def get(label, intent, book):
            with capture_origin(label, "NBA", intent), receipt_scope() as refs:
                body = await (client.get_orderbook("GAME") if book else client.get_market("GAME"))
            return body, refs[0]

        (book, a), (_, b), (_, c) = await asyncio.gather(
            get("scanner", None, True),
            get("entry_simulator", "one", True),
            get("entry_submission", "two", False),
        )
    assert book["orderbook_fp"]["yes_dollars"][0] == ["0.4100", "1.50"]
    assert len({a, b, c}) == 3 and {a.sequence, b.sequence, c.sequence} == {1, 2, 3}
    assert [r.intent_id for r in (a, b, c)] == [None, "one", "two"]
    assert a.capture_state == b.capture_state == c.capture_state == "queued"
    rec.stop.set()
    await writer
    with closing(sqlite3.connect(rec.path)) as db:
        for r in (a, b, c):
            assert db.execute(
                "SELECT source,intent_id,received_ns FROM market_events WHERE session_id=? AND sequence=?",
                (r.session_id, r.sequence),
            ).fetchone() == (r.source, r.intent_id, r.received_ns)
    lost = rec.capture("/markets/GAME", "entry", 123, {}, b"{}", "test")
    assert lost.capture_state == "dropped" and rec.dropped == 1


@pytest.fixture
async def linked_archive(tmp_path, monkeypatch, request):
    aligned = getattr(request, "param", None) == "aligned"
    bridged = getattr(request, "param", None) == "stage2b3" or aligned
    ticker, event, series = (
        ("KXNBAGAME-TEST-ATL", "KXNBAGAME-TEST", "KXNBAGAME")
        if bridged
        else ("GAME", "EVENT", "SERIES")
    )
    ledger = PaperStore(
        str(tmp_path / "paper_trading.db"), experiment_id=overnight.LINKED_EXPERIMENT
    )
    rec = MarketRecorder(
        str(tmp_path / "market_data_recorder.db"), 24, 8, lambda _: "NBA", append_only=True
    )
    rec.session = "fixture-session"
    task = asyncio.create_task(rec.run())
    await rec.initialized.wait()
    books = iter([(40, 500), (40, 500), (41, 2), (41, 500), (41, 3), (50, 500), (50, 500)])

    def serve(req):
        path = req.url.path
        if path.endswith("/orderbook"):
            price, size = next(books)
            return httpx.Response(
                200, json={"orderbook": {"yes": [[price, size]], "no": [[45, 500]]}}
            )
        if "/markets/" in path:
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": ticker,
                        "event_ticker": event,
                        "status": "active",
                        "close_time": "2099-01-01T00:00:00Z",
                    }
                },
            )
        if "/events/" in path:
            return httpx.Response(
                200, json={"event": {"event_ticker": event, "series_ticker": series}}
            )
        return httpx.Response(
            200,
            json={
                "series": {
                    "ticker": series,
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": 1,
                }
            },
        )

    effective = replace(
        config.get_effective_paper_config("NBA"),
        allow_yes_entries=True,
        allow_no_entries=False,
        min_top_liquidity=0,
        min_edge_cents=0,
        min_capturable_edge_to_cross_cents=0,
        min_abs_distance_from_50_cents=0,
        min_reentry_delay_seconds_same_ticker_side=0,
        allow_moved_through=False,
        allow_price_improves=False,
        poll_interval_seconds=0.01,
        fill_horizon_seconds=2,
        fill_probability_at_best=0.25,
    )
    monkeypatch.setattr(config, "get_effective_paper_config", lambda _: effective)
    monkeypatch.setattr(config, "ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(config, "PAPER_MAX_DAILY_LOSS_CENTS", 0)
    for name, value in {
        "paper_enabled": True,
        "paper_store": ledger,
        "paper_simulation_tasks": set(),
        "paper_ticker_cooldown": {},
        "paper_intent_timestamps": [],
    }.items():
        monkeypatch.setattr(state, name, value)
    draws = Mock(side_effect=[0.125])
    monkeypatch.setattr(paper_sim.random, "random", draws)
    try:
        async with _client(ReadBudget(10000, 10000), serve) as client:
            client.recorder = rec
            fees = MarketFees(client)
            monkeypatch.setattr(state, "market_fees", fees)
            with ExitStack() as hooks:
                if bridged:
                    from kalshi_edge_engine.runtime import paper_runtime
                    from tools.paper_bridge import Sidecar, observe

                    sink = Sidecar(tmp_path, {"paper_session": "paper-fixture"})
                    hooks.callback(sink.close)
                    # The client has a MockTransport for every request, including this origin.
                    client.base_url = client.DEFAULT_BASE_URL
                    monkeypatch.setattr(paper_runtime.logger, "disabled", True)
                    hooks.enter_context(
                        observe(sink, ledger, rec, fees, {ticker: event}, track_scanner=aligned)
                    )
                signal = await scanner_runtime._score_market(client, ticker, "NBA")
                with receipt_scope() as cached:
                    await fees.resolve(ticker, for_entry=True)
                assert [r.sequence for r in cached] == [1, 2, 3] and rec.sequence == 4
                await scanner_runtime._process_paper_intents(signal, client)
                await asyncio.wait_for(asyncio.gather(*state.paper_simulation_tasks), 4)
            assert ledger.count_fills_total() == 1
            key = ledger.get_recent_fills()[0]["intent_id"]
            seconds, nanos = time.time, time.time_ns
            monkeypatch.setattr(time, "time", lambda: seconds() + 6)
            monkeypatch.setattr(time, "time_ns", lambda: nanos() + 6_000_000_000)
            await paper_exit_runtime._observe_due_outcomes(ledger, client, int(time.time() * 1000))
            await paper_exit_runtime._refresh_positions(ledger, client)
    finally:
        rec.stop.set()
        await task
    draws.assert_called_once_with()
    assert ledger.count_fills_total() == 2
    journal = ledger.fill_funnel.intent(key)
    assert [r["sequence"] for r in journal["submission_evidence"]["signal_receipts"]] == [
        1,
        2,
        3,
        4,
    ]
    assert journal["submission_evidence"]["book_receipts"][0]["sequence"] == 5
    assert journal["submission_evidence"]["market_receipts"][0]["sequence"] == 6
    for n, poll in enumerate(journal["polls"]):
        ev = poll["evidence"]
        assert ev["book_receipts"][0]["sequence"] == 7 + n * 2
        assert ev["validation"]["receipts"][0]["sequence"] == 8 + n * 2
    assert [p["evidence"]["random_draw"] for p in journal["polls"]] == [None, None, 0.125]
    assert journal["polls"][0]["evidence"]["no_draw_reason"] == "insufficient_volume_beyond_queue"
    assert journal["polls"][-1]["ledger_result"] == "persisted"
    assert (
        journal["persisted_fills"][0]["outcome_fee_audit"]["5s"]["evidence"]["book_receipts"][0][
            "sequence"
        ]
        == 13
    )
    assert journal["lifecycle_exits"][0]["evidence"]["book_receipts"][0]["sequence"] == 15
    dest = tmp_path / "backup"
    dest.mkdir()
    for src in (ledger.db_path, rec.path):
        with (
            closing(sqlite3.connect(src)) as origin,
            closing(sqlite3.connect(dest / Path(src).name)) as target,
        ):
            origin.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
    return dest


def audit(path):
    with open_archive(path) as snapshot:
        return evidence_audit(snapshot)


async def test_actual_draw_and_exact_book_resolve_in_read_only_backup(linked_archive):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in linked_archive.iterdir()}
    report = audit(linked_archive)
    assert report == audit(linked_archive)
    assert report["archive_sha256"] == before
    assert report["counts"]["evidence_complete_decisions"] == 3, report
    assert (
        report["counts"]["successful_probability_draws"]
        == report["counts"]["persisted_simulated_fills"]
        == 1
    )
    assert report["counts"]["exit_fills"] == report["counts"]["outcome_marks"] == 1
    assert report["counts"]["incomplete_exits"] == report["counts"]["incomplete_outcomes"] == 0, (
        report
    )
    [example] = report["resolved_fills"]
    assert (example["sequence"], example["raw_row"], example["draw"], example["probability"]) == (
        11,
        11,
        0.125,
        0.25,
    )
    assert {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in linked_archive.iterdir()
    } == before


@pytest.mark.parametrize(
    ("problem", "field"),
    [
        ("missing", "missing_references"),
        ("wrong", "inconsistent_references"),
        ("duplicate", "duplicate_references"),
        ("deleted", "unresolved_references"),
        ("future", "invalid_causal_order"),
    ],
)
async def test_incomplete_audit_does_not_substitute_nearby_receipts(linked_archive, problem, field):
    with closing(sqlite3.connect(linked_archive / "paper_trading.db")) as db, db:
        row = db.execute("SELECT evidence FROM entry_polls WHERE id=3").fetchone()
        e = json.loads(row[0])
        if problem == "missing":
            e["book_receipts"] = []
        elif problem == "wrong":
            e["book_receipts"][0]["ticker"] = "OTHER"
        elif problem == "duplicate":
            e["book_receipts"] *= 2
        elif problem == "deleted":
            e["book_receipts"][0]["sequence"] = 1000
        else:
            e["decision_ns"] = e["book_receipts"][0]["received_ns"] - 1
        db.execute("UPDATE entry_polls SET evidence=? WHERE id=3", (json.dumps(e),))
    result = audit(linked_archive)
    assert result["counts"][field] >= 1
    assert result["counts"]["evidence_incomplete_decisions"] == 1 and result["resolved_fills"] == []


def test_prepared_b3a_identity_refused_before_creating_any_lock(tmp_path, monkeypatch):
    for name, value in {
        "PAPER_EXPERIMENT_ID": overnight.LINKED_EXPERIMENT,
        "PAPER_DB_PATH": str(tmp_path / "paper_trading.db"),
        "MARKET_RECORDER_DB": str(tmp_path / "market_data_recorder.db"),
    }.items():
        monkeypatch.setenv(name, value)
    manifest = tmp_path / overnight.MANIFEST
    original = json.dumps({"experiment_id": overnight.EXPERIMENT})
    manifest.write_text(original)
    with pytest.raises(ValueError, match="another experiment"):
        overnight.Run.configured()
    assert list(tmp_path.iterdir()) == [manifest] and manifest.read_text() == original


async def test_interrupted_poll_preserves_received_book_without_sampling(tmp_path, monkeypatch):
    rec = MarketRecorder(str(tmp_path / "rec.db"), 24, 4, lambda _: "NBA", append_only=True)
    writer = asyncio.create_task(rec.run())
    await rec.initialized.wait()
    draws = Mock(side_effect=AssertionError("unexpected draw"))
    monkeypatch.setattr(paper_sim.random, "random", draws)
    rows = []
    active = iter([True, False])
    order = paper_sim.OrderRequest(
        "intent",
        "GAME",
        "YES",
        41,
        1,
        1000,
        5,
        0.2,
        60,
        may_poll=lambda: next(active),
        on_unprocessed_evidence=rows.append,
    )
    try:
        async with _client(
            ReadBudget(1000, 1000),
            lambda _: httpx.Response(200, json={"orderbook": {"yes": [[41, 2]], "no": []}}),
        ) as client:
            client.recorder = rec
            with capture_origin("entry_simulator", "NBA", "intent"):
                await paper_sim.RealisticOrderSimulator().simulate_order_fills(client, order, 1)
    finally:
        rec.stop.set()
        await writer
    draws.assert_not_called()
    [interruption] = rows
    assert interruption["no_draw_reason"] == "entry_inactive_after_read"
    assert interruption["book_receipts"][0]["sequence"] == 1


def test_version_registry_pins_code_and_refuses_old_cohort(monkeypatch):
    monkeypatch.chdir(overnight.ROOT)
    source_status = ""

    def local_git(*args):
        if args[0] == "status":
            return source_status
        return (
            str(overnight.ROOT)
            if args[0] == "rev-parse" and args[1] == "--show-toplevel"
            else "main"
            if args[0] == "branch"
            else "test-commit"
        )

    monkeypatch.setattr(overnight, "git", local_git)
    monkeypatch.setenv("PAPER_EXPERIMENT_ID", overnight.LINKED_EXPERIMENT)
    assert overnight.revision()["commit"] == "test-commit"
    source_status = "?? src/kalshi_edge_engine/evidence.py"
    with pytest.raises(ValueError, match="recorded commit"):
        overnight.revision()
    source_status = ""
    monkeypatch.setenv("PAPER_EXPERIMENT_ID", overnight.EXPERIMENT)
    with pytest.raises(ValueError, match="pinned"):
        overnight.revision()
    monkeypatch.setenv("PAPER_EXPERIMENT_ID", overnight.LINKED_EXPERIMENT)
    original = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda p: original(p) + (b"# changed" if p.name == "paper_sim.py" else b""),
    )
    with pytest.raises(ValueError, match="pinned"):
        overnight.revision()


async def test_duplicate_keys_in_damaged_snapshot_are_ambiguous(linked_archive):
    with closing(sqlite3.connect(linked_archive / "market_data_recorder.db")) as db, db:
        db.execute("DROP INDEX market_events_identity")
        fields = ",".join(
            r[1] for r in db.execute("PRAGMA table_info(market_events)") if r[1] != "id"
        )
        db.execute(
            f"INSERT INTO market_events ({fields}) SELECT {fields} FROM market_events WHERE sequence=11"
        )
    report = audit(linked_archive)
    assert report["counts"]["duplicate_references"] > 0
    assert report["classification"] == "incomplete" and not report["resolved_fills"]


@pytest.mark.parametrize("linked_archive", ["stage2b3", "aligned"], indirect=True)
async def test_real_paper_runtime_keeps_draw_rejections_receipts_and_booking(
    linked_archive, tmp_path, request
):
    from tools.paper_bridge import EVENTS
    from tools.paper_link_report import decisions, paper_records

    events = [json.loads(x) for x in (tmp_path / EVENTS).read_bytes().splitlines()]
    if request.node.callspec.params["linked_archive"] == "aligned":
        visits = [e for e in events if e["kind"] == "scanner_visit"]
        scores = [e for e in events if e["kind"] == "scanner_score"]
        assert len(visits) == len(scores) == 1
        assert visits[0]["ticker"] == scores[0]["ticker"] and scores[0]["reason"] == "SIGNAL_VALID"
    A = "KXNBAGAME-TEST-ATL"
    game = A.rpartition("-")[0]
    rows = decisions(events, {"markets": {A: game}})
    assert any(r["admission_reason"] == "side_disabled" for r in rows.values())
    assert all(r["admission"] != "unknown" for r in rows.values())
    assert len([r for r in events if r["kind"] == "paper_booking"]) == 1
    with open_archive(linked_archive) as archive:
        fees, details = paper_records(archive, events, rows, "fixture-session", "digest")
    bought = next(r for r in rows.values() if r["admission"] == "admitted")
    assert bought["paper_ledger"]["status"] in ("FILLED", "EXITED")
    assert bought["signal_receipt_audit"] == {"references": 4, "unresolved": []}
    assert (
        len(bought["paper_ledger"]["polls"]) == 3
        and len(bought["paper_ledger"]["entry_fills"]) == 1
    )
    assert bought["paper_ledger"]["entry_fills"][0]["booking_mono_ns"] is not None
    assert len(details["booked_lifecycle_exits"]) == 1 and not details["ledger_only_intents"]
    assert fees.resolve("digest", A, game, bought["decision_mono_ns"]) is not None
    assert fees.resolve("other", A, game, bought["decision_mono_ns"]) is None
    # A later public snapshot cannot backfill an earlier missing profile.
    assert fees.resolve("digest", A, game, events[0]["mono_ns"] - 1) is None
