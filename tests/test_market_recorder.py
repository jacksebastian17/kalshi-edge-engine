import asyncio
import sqlite3
import time

import httpx
from fastapi import Request

from kalshi_edge_engine.api import main
from kalshi_edge_engine.db.market_recorder import MIB, MarketRecorder, capture_origin
from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.kalshi.read_budget import ReadBudget, read_priority
from tests.test_kalshi_read_budget import _client


async def _start(recorder):
    task = asyncio.create_task(recorder.run())
    async with asyncio.timeout(3):
        while not recorder.ready and not task.done():
            await asyncio.sleep(0.001)
    return task


async def test_backend_can_record_with_paper_and_scanner_off(tmp_path, monkeypatch):
    rec_path = tmp_path / "capture.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENABLE_MARKET_DATA_RECORDER", "true")
    monkeypatch.setenv("MARKET_RECORDER_DB", str(rec_path))
    monkeypatch.setenv("ENABLE_NBA_SCANNER", "false")
    monkeypatch.setattr(main, "_ENABLE_PAPER_TRADING", False)
    client = _client(ReadBudget(200, 100), lambda _: httpx.Response(200, json={"yes": []}))
    monkeypatch.setattr(main, "KalshiClient", lambda: client)
    app = main.create_app()
    request = Request({"type": "http", "app": app})
    async with main.lifespan_production(app):
        async with asyncio.timeout(3):
            while not (await main.get_recorder_status(request))["ready"]:
                await asyncio.sleep(0.001)
        assert app.state.scanner_task is app.state.paper_maintenance_task is None
        await client.get_orderbook("GAME")
    assert not (tmp_path / "paper_trading.db").exists()
    with sqlite3.connect(rec_path) as conn:
        assert conn.execute("SELECT kind, ticker FROM market_events").fetchall() == [
            ("orderbook", "GAME")
        ]


async def test_record_actual_http_responses_without_additional_requests(tmp_path, monkeypatch):
    monkeypatch.delenv("ENABLE_MARKET_DATA_RECORDER", raising=False)
    assert MarketRecorder.from_env(lambda _ticker: "NBA") is None
    path = str(tmp_path / "capture.db")
    recorder = MarketRecorder(path, 24, 1, lambda _ticker: "NBA")
    task = await _start(recorder)
    book = b'{ "orderbook_fp": {"yes_dollars":[["0.6780","5.00"]],"no_dollars":[]}, "unknown":1 }'
    market = b'{"market":{"ticker":"A","updated_time":"2026-01-01T00:00:00Z","status":"closed"}}'
    paths = []

    def serve(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, content=book)
        if "/historical/" in request.url.path:
            return httpx.Response(200, content=market)
        return httpx.Response(404)

    budget = ReadBudget(200, 100)
    async with _client(budget, serve) as client:
        client.recorder = recorder
        await client.get_orderbook("A")
        with read_priority("entry"), capture_origin("entry_simulator", "NBA", "sample-entry"):
            assert (await client.get_market("A"))["status"] == "closed"
    recorder.stop.set()
    await task
    assert len(paths) == 3
    assert budget.snapshot()["requests_by_lane"]["entry"] == 2
    assert recorder.status()["written"] == recorder.status()["retained"] == 2
    restarted = MarketRecorder(path, 24, 1, lambda _ticker: "NBA")
    task = await _start(restarted)
    restarted.capture("/markets/A/orderbook", "entry", time.time_ns(), {}, b"{}", "test")
    restarted.stop.set()
    await task
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM market_events ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert [r["sequence"] for r in rows] == [1, 2, 1]
    assert rows[0]["session_id"] != rows[2]["session_id"]
    assert [r["kind"] for r in rows] == ["orderbook", "market", "orderbook"]
    assert [r["read_lane"] for r in rows] == ["discovery", "entry", "entry"]
    assert rows[0]["body"] == book and rows[1]["body"] == market
    assert rows[0]["source_time"] is None
    assert rows[1]["source_time_field"] == "market.updated_time"
    assert rows[1]["source_time"] == "2026-01-01T00:00:00Z"
    assert rows[1]["request_path"] == "/historical/markets/A"
    assert (rows[1]["source"], rows[1]["intent_id"]) == ("entry_simulator", "sample-entry")
    assert rows[0]["received_ns"] <= rows[1]["received_ns"]
    assert all(r["universe"] == "NBA" and r["schema_version"] == 1 for r in rows)


async def test_storage_cap_queue_bounds_and_idle_age_pruning(tmp_path):
    path = tmp_path / "capture.db"
    rec = MarketRecorder(str(path), 1, 1, lambda _ticker: "NBA")
    task = await _start(rec)
    now = time.time_ns()
    body = b"x" * 32000
    for _ in range(320):
        rec.capture("/markets/A/orderbook", "entry", now, {}, body, "test")
    rec.capture("/markets/A/orderbook", "entry", now, {}, bytes(MIB), "test")
    assert rec.queue_bytes <= 8 * MIB
    assert rec.dropped > 0
    rec.stop.set()
    await task
    assert rec.errors == 0, rec.last_error
    assert rec.sequence == rec.written + rec.dropped
    assert rec.pruned > 0
    assert 0 < rec.retained == rec.written - rec.pruned
    assert path.stat().st_size <= MIB
    with sqlite3.connect(path) as conn:
        oldest, newest = conn.execute(
            "SELECT MIN(sequence), MAX(sequence) FROM market_events"
        ).fetchone()
        assert oldest > 1 and newest == rec.written
        conn.execute("UPDATE market_events SET received_ns=?", (now - 2 * 3600 * 10**9,))
    reopened = MarketRecorder(str(path), 1, 1, lambda _ticker: "NBA")
    task = await _start(reopened)
    reopened.stop.set()
    await task
    assert reopened.retained == 0
    assert reopened.pruned == rec.retained


async def test_recorder_errors_leave_ledger_usable(tmp_path):
    ledger = PaperStore(str(tmp_path / "paper.db"))
    rec = MarketRecorder(ledger.db_path, 1, 1, lambda _ticker: "NBA")
    await rec.run()
    assert not rec.ready
    assert rec.errors == 1 and "not a market recorder database" in rec.last_error
    with sqlite3.connect(ledger.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='market_events'"
            ).fetchone()[0]
            == 0
        )
    assert ledger.count_intents_total() == 0
    assert ledger.fill_funnel.aggregate()["instrumented_intents"] == 0


async def test_capture_write_failure_is_reported_and_writer_recovers(tmp_path, monkeypatch):
    rec = MarketRecorder(str(tmp_path / "capture.db"), 1, 1, lambda _ticker: "NBA")
    original = rec._write

    def fail_once(batch):
        if batch and not rec.errors:
            raise sqlite3.OperationalError("simulated I/O failure")
        return original(batch)

    monkeypatch.setattr(rec, "_write", fail_once)
    task = await _start(rec)
    rec.capture("/markets/A", "entry", time.time_ns(), {}, b"{}", "test")
    async with asyncio.timeout(3):
        while not rec.errors:
            await asyncio.sleep(0.01)
    rec.capture("/markets/A", "entry", time.time_ns(), {}, b"{}", "test")
    rec.stop.set()
    await task
    assert rec.written == rec.dropped == rec.errors == 1
    assert "simulated I/O failure" in rec.last_error
    with sqlite3.connect(rec.path) as conn:
        assert conn.execute("SELECT sequence FROM market_events").fetchall() == [(2,)]
