import asyncio
import json
import os
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Request

from kalshi_edge_engine import overnight
from kalshi_edge_engine.api import main
from kalshi_edge_engine.db import market_recorder
from kalshi_edge_engine.db.market_recorder import DISK_RESERVE, MIB, MarketRecorder
from kalshi_edge_engine.db.paper_store import CLEAN_EXPERIMENT_ID, PaperStore
from kalshi_edge_engine.engine.fees import series_fee_profile
from kalshi_edge_engine.runtime import config
from tests.test_entry_market_guard import _fill, _observation, _submit


def test_new_cohort_separate_clean_fills_cutover_and_recovery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("PAPER_EXPERIMENT_ID", "PAPER_DB_PATH"):
        monkeypatch.delenv(name, raising=False)
    old = PaperStore(clock_ms=lambda: 50)
    assert old.db_path == "paper_trading.db" and old.experiment_id == CLEAN_EXPERIMENT_ID
    monkeypatch.setenv("PAPER_EXPERIMENT_ID", overnight.EXPERIMENT)
    with pytest.raises(ValueError, match="dedicated"):
        PaperStore()
    path = tmp_path / "new.db"
    monkeypatch.setenv("PAPER_DB_PATH", str(path))
    now = 1000
    new = PaperStore(clock_ms=lambda: now)
    assert new.count_intents_total() == new.count_fills_total() == 0
    profile = series_fee_profile(
        {"ticker": "SERIES", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1},
        {"series_ticker": "SERIES", "event_ticker": "EVENT"},
        observed_ts_ms=now,
    )
    assert _submit(new, profile, now, _observation("active", 990, 999, 5000))
    now = 1100
    assert _fill(new, profile, 1080, _observation("active", 1085, 1095, 5000)).inserted_new_fill
    restarted = PaperStore(clock_ms=lambda: now)
    assert restarted.cutover_ts_ms == 1000 and restarted.recover_interrupted_intents() == 1
    assert restarted.get_recent_fills()[0]["experiment_id"] == overnight.EXPERIMENT
    assert restarted.compute_paper_metrics()["experiment_id"] == overnight.EXPERIMENT
    assert old.count_intents_total() == old.count_fills_total() == 0
    assert old.compute_paper_metrics()["experiment_id"] == CLEAN_EXPERIMENT_ID
    with pytest.raises(ValueError, match="different experiment"):
        PaperStore(str(path), experiment_id=CLEAN_EXPERIMENT_ID)


def recorder(path, *, strict=True):
    return MarketRecorder(str(path), 1, 1, lambda _: "NBA", append_only=strict)


async def start(rec):
    task = asyncio.create_task(rec.run())
    await asyncio.wait_for(rec.initialized.wait(), 4)
    rec.require_healthy()
    return task


def capture(rec, body=b"{}"):
    rec.capture("/markets/GAME/orderbook", "entry", 1, {}, body, "test")


async def finish(rec, task):
    rec.stop.set()
    await asyncio.wait_for(task, 10)


async def test_append_only_capacity_never_evicts_even_expired_events(tmp_path):
    path = tmp_path / "rec.db"
    first = recorder(path)
    task = await start(first)
    capture(first, b"old")
    await finish(first, task)
    rec = recorder(path)
    task = await start(rec)
    for _ in range(120):
        capture(rec, b"x" * 32000)
    await finish(rec, task)
    status = rec.status()
    assert status["errors"] == 1 and status["dropped"] > 0 and rec.pruned == 0
    assert status["failure_reason"] and status["last_contiguous_written_sequence"] == rec.written
    assert rec.sequence == rec.written + rec.dropped and rec.queue_bytes == rec.in_flight == 0
    assert 0 < path.stat().st_size <= MIB
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT body FROM market_events WHERE id=1").fetchone() == (b"old",)
        assert [
            r[0]
            for r in db.execute(
                "SELECT sequence FROM market_events WHERE session_id=? ORDER BY id", (rec.session,)
            )
        ] == list(range(1, rec.written + 1))
        assert db.execute("SELECT state FROM capture_sessions ORDER BY rowid").fetchall() == [
            ("clean",),
            ("incomplete",),
        ]
    retry = recorder(path)
    await retry.run()
    assert "incomplete" in retry.failure
    rolling = recorder(path, strict=False)
    await rolling.run()
    assert rolling.errors and not rolling.pruned


async def test_queue_drop_drains_committed_prefix_and_latches_failure(tmp_path):
    rec = recorder(tmp_path / "capture.db")
    task = await start(rec)
    capture(rec)
    capture(rec, bytes(MIB))
    capture(rec)
    with pytest.raises(RuntimeError, match="dropped"):
        rec.require_healthy()
    await finish(rec, task)
    assert (rec.sequence, rec.written, rec.dropped, rec.pruned) == (3, 1, 2, 0)


async def test_unclosed_session_refuses_restart_and_preserves_receipts(tmp_path):
    path = tmp_path / "rec.db"
    rec = recorder(path)
    rec._initialize()  # A process that exited after committing its open marker.
    retry = recorder(path)
    await retry.run()
    assert not retry.ready and "incomplete or still open" in retry.failure
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT id,state FROM capture_sessions").fetchall() == [
            (rec.session, "open")
        ]


@pytest.mark.parametrize("fail", [False, True])
async def test_shutdown_waits_for_inflight_batch_and_accounts_for_waiting_queue(
    tmp_path, monkeypatch, fail
):
    rec = recorder(tmp_path / "inflight.db")
    entered, release = threading.Event(), threading.Event()
    write = rec._write

    def blocking_write(batch):
        if batch and not entered.is_set():
            entered.set()
            assert release.wait(5)
            if fail:
                raise OSError("disk write failed")
        return write(batch)

    monkeypatch.setattr(rec, "_write", blocking_write)
    task = await start(rec)
    capture(rec)
    assert await asyncio.to_thread(entered.wait, 3)
    capture(rec)
    rec.stop.set()
    assert rec.in_flight == len(rec.queue) == 1
    release.set()
    await finish(rec, task)
    assert (rec.written, rec.dropped) == ((0, 2) if fail else (2, 0))
    assert rec.sequence == 2 and not rec.queue and rec.in_flight == 0


@pytest.fixture
def configured(tmp_path, monkeypatch):
    for key, value in {
        "PAPER_EXPERIMENT_ID": overnight.EXPERIMENT,
        "PAPER_DB_PATH": str(tmp_path / "paper_trading.db"),
        "MARKET_RECORDER_DB": str(tmp_path / "market_data_recorder.db"),
        "ENABLE_MARKET_DATA_RECORDER": "true",
        "MARKET_RECORDER_APPEND_ONLY": "true",
        "MARKET_RECORDER_MAX_MIB": "4096",
        "ENABLE_LIVE_TRADING": "false",
        "ENABLE_NBA_SCANNER": "false",
        "PAPER_NBA_ALLOW_PRICE_IMPROVES": "false",
        "PAPER_NBA_ALLOW_MOVED_THROUGH": "false",
    }.items():
        monkeypatch.setenv(key, value)
    for name, value in {
        "ENABLE_PAPER_TRADING": True,
        "ENABLE_UNIVERSE_NBA": True,
        "ENABLE_UNIVERSE_CBB": False,
        "PAPER_ALLOW_PRICE_IMPROVES": False,
        "PAPER_ALLOW_MOVED_THROUGH": False,
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(overnight, "revision", lambda: {"commit": "test", "source_sha256": "test"})
    monkeypatch.setattr(overnight, "KalshiClient", AsyncMock)
    monkeypatch.setattr(
        market_recorder.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024 * MIB)
    )
    baseline = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("PAPER_", "ENABLE_UNIVERSE_", "KALSHI_READ_"))
    }
    monkeypatch.setattr(overnight, "dotenv_values", lambda _: baseline)
    return tmp_path


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PAPER_EXPERIMENT_ID", CLEAN_EXPERIMENT_ID),
        ("MARKET_RECORDER_APPEND_ONLY", "false"),
        ("ENABLE_MARKET_DATA_RECORDER", "false"),
        ("MARKET_RECORDER_MAX_MIB", "256"),
        ("MARKET_RECORDER_DB", "paper_trading.db"),
        ("ENABLE_LIVE_TRADING", "true"),
        ("PAPER_NBA_ALLOW_MOVED_THROUGH", "true"),
        ("PAPER_MAX_DAILY_LOSS_CENTS", "123456789"),
    ],
)
async def test_preflight_rejects_unsafe_configuration(configured, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        await overnight.preflight()
    assert not list(configured.glob("*.db"))


async def test_preflight_refuses_prior_data_or_low_space(configured, monkeypatch):
    path = configured / "paper_trading.db"
    path.write_bytes(b"archive")
    with pytest.raises(ValueError, match="empty directory"):
        await overnight.preflight()
    assert list(configured.iterdir()) == [path] and path.read_bytes() == b"archive"
    path.unlink()
    monkeypatch.setattr(
        market_recorder.shutil, "disk_usage", lambda _: SimpleNamespace(free=DISK_RESERVE + MIB)
    )
    with pytest.raises(RuntimeError, match="headroom"):
        await overnight.preflight()
    assert not list(configured.glob("*.db"))


async def test_real_lifespan_interlock_restart_and_offline_backup(configured, monkeypatch, capsys):
    monkeypatch.setattr(main, "KalshiClient", AsyncMock)
    monkeypatch.setattr(main, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main, "_paper_enabled", False)
    monkeypatch.setattr(main, "_supervise_paper_maintenance", lambda _: asyncio.sleep(60))
    assert (await overnight.preflight())["intents"] == 0
    assert (configured / overnight.MANIFEST).is_file()
    app = main.create_app()
    async with main.lifespan_production(app):
        rec = app.state.recorder
        capture(rec, b"one")
        with pytest.raises(RuntimeError, match="already owns"):
            overnight.backup()
    assert rec.written == 1
    async with main.lifespan_production(app):
        assert app.state.recorder.retained == 1
        assert (await main.start_paper_trading())["started"]
        rec = app.state.recorder
        monkeypatch.setattr(
            market_recorder.shutil, "disk_usage", lambda _: SimpleNamespace(free=DISK_RESERVE)
        )
        capture(rec)
        async with asyncio.timeout(3):
            while not rec.failure:
                await asyncio.sleep(0.01)
        assert "headroom" in rec.failure and not (await main.get_paper_config())["enabled"]
        with pytest.raises(HTTPException):
            await main.start_paper_trading()
        with pytest.raises(HTTPException):
            await main.start_scanner(Request({"type": "http", "app": app}))
        with pytest.raises(RuntimeError, match="headroom"):
            main._paper_store.insert_fill_result()
        await asyncio.sleep(0)
        assert app.state.paper_maintenance_task.cancelled()
    monkeypatch.setattr(
        market_recorder.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * 1024 * MIB)
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        async with main.lifespan_production(app):
            pytest.fail("restarted incomplete run")
    dest = overnight.backup()
    seals = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [s["complete_recorded_session"] for s in seals] == [True, True, False]
    assert [s["receipt_rows"] for s in seals] == [0, 1, 0]
    with closing(sqlite3.connect(dest / "market_data_recorder.db")) as db:
        assert db.execute("SELECT body FROM market_events").fetchall() == [(b"one",)]
        assert db.execute(
            "SELECT state FROM capture_sessions ORDER BY rowid DESC LIMIT 1"
        ).fetchone() == ("incomplete",)
    assert not list(dest.glob("*-wal"))
    assert (
        json.loads((dest / overnight.MANIFEST).read_text())["experiment_id"] == overnight.EXPERIMENT
    )
