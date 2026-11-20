import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, call

import httpx
import pytest
from fastapi import Request

from kalshi_edge_engine.api import main
from kalshi_edge_engine.db.paper_store import CLEAN_EXPERIMENT_ID, PaperStore
from kalshi_edge_engine.engine.fees import LEGACY_FEE_PROFILE, series_fee_profile
from kalshi_edge_engine.engine.orderbook import quote_liquidation
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.signal import FEE_DATA_UNAVAILABLE, score_signal
from kalshi_edge_engine.kalshi.client import KalshiAPIError, KalshiClient
from kalshi_edge_engine.kalshi.market import read_settlement
from kalshi_edge_engine.runtime import config, paper_exit_runtime, state
from kalshi_edge_engine.runtime.market_fees import MarketFees


def _profile(series="KXNBAGAME", fee_type="quadratic_with_maker_fees", **overrides):
    result = series_fee_profile(
        {"ticker": series, "fee_type": fee_type, "fee_multiplier": 1},
        {"event_ticker": f"{series}-GAME", "series_ticker": series, **overrides},
        observed_ts_ms=100,
    )
    assert result is not None
    return result


def _entry(store, ticker, side, price, now, *, size=1, profile=LEGACY_FEE_PROFILE):
    intent_id = f"entry:{ticker}:{side}:{now}"
    assert store.insert_intent(
        QuoteIntent(intent_id, ticker, side, price, size, now, 5, 8, 8, "maker", True),
        max_hold_seconds=3600,
        fee_profile=profile,
    )
    assert store.insert_fill(
        fill_id=f"fill:{intent_id}",
        intent_id=intent_id,
        fill_ts_ms=now,
        fill_price_cents=price,
        fill_size=1,
        remaining_size=size - 1,
        fill_fee_cents=profile.fee_cents(price, 1, liquidity="maker"),
        note="entry_queue_cleared",
        execution_source="simulated_displayed_size_decrease",
        fee_profile=profile,
        apply_to_position=True,
    )
    return intent_id


def _market(ticker, result, payout, ts_ms, **kwargs):
    return {
        "ticker": ticker,
        "status": "finalized",
        "result": result,
        "notional_value_dollars": "1.0000",
        "settlement_value_dollars": payout,
        "settlement_ts": datetime.fromtimestamp(ts_ms / 1000, UTC).isoformat(),
        **kwargs,
    }


@pytest.mark.parametrize("series", ["KXNBAGAME", "KXNCAAMBGAME"])
def test_nba_and_cbb_moneyline_one_contract_fees(series):
    profile = _profile(series)
    assert profile.fee_cents(50, 1, liquidity="maker") == 0.44
    assert profile.fee_cents(45, 1, liquidity="taker", sale=True) == 1.74
    assert profile.fee_cents(50, 1, liquidity="taker") == 1.75
    signal = score_signal(
        "GAME", {"yes": [[49, 100]], "no": [[45, 100]]}, round_trip=True, fee_profile=profile
    )
    assert signal.fee_yes == 0.44
    assert signal.fee_exit_yes == 1.74
    assert signal.fee_profile.audit()["series_ticker"] == series


def test_fee_types_multipliers_rounding_and_unknown():
    assert _profile(fee_type="quadratic").fee_cents(50, 1, liquidity="maker") == 0
    assert (
        _profile(fee_type="quadratic_with_combo_maker_fees").fee_cents(50, 1, liquidity="maker")
        == 0.88
    )
    discounted = _profile(fee_multiplier_override=0.5)
    assert discounted.source == "kalshi_event_override"
    assert discounted.fee_cents(50, 1, liquidity="maker") == 0.22
    assert discounted.fee_cents(50, 1, liquidity="taker") == 0.88
    assert _profile(fee_multiplier_override=0).fee_cents(50, 1, liquidity="taker") == 0
    assert LEGACY_FEE_PROFILE.fee_cents(50, 1, liquidity="maker") == 1
    assert LEGACY_FEE_PROFILE.fee_cents(50, 1, liquidity="taker") == 2
    assert (
        series_fee_profile(
            {"ticker": "SERIES", "fee_type": "flat", "fee_multiplier": 1},
            {"series_ticker": "SERIES", "event_ticker": "EVENT"},
            observed_ts_ms=100,
        )
        is None
    )
    unknown = score_signal("GAME", {"yes": [[45, 100]], "no": [[45, 100]]}, fee_profile=None)
    assert unknown.reason_code == FEE_DATA_UNAVAILABLE
    assert unknown.net_edge_yes is None


@pytest.mark.asyncio
async def test_metadata_resolver_uses_event_override_and_caches_observation():
    client = AsyncMock()
    client.get_market.return_value = {"ticker": "M", "event_ticker": "E", "status": "active"}
    client.get_event.return_value = {
        "event_ticker": "E",
        "series_ticker": "S",
        "fee_type_override": "quadratic_with_maker_fees",
        "fee_multiplier_override": 0.5,
    }
    client.get_series_details.return_value = {
        "ticker": "S",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    resolver = MarketFees(client)
    first = await resolver.resolve("M", for_entry=True)
    assert first is not None
    assert first.fee_cents(50, 1, liquidity="maker") == 0.22
    assert await resolver.resolve("M", for_entry=True) == first
    client.get_market.assert_awaited_once_with("M")
    client.get_event.assert_awaited_once_with("E")
    client.get_series_details.assert_awaited_once_with("S")


@pytest.mark.asyncio
async def test_market_lookup_falls_back_to_historical_after_404(monkeypatch):
    client = object.__new__(KalshiClient)
    response = httpx.Response(
        404, request=httpx.Request("GET", "https://example.test/markets/GAME")
    )
    missing = KalshiAPIError("Market outside current partition")
    missing.__cause__ = httpx.HTTPStatusError("404", request=response.request, response=response)
    market = _market("GAME", "yes", "1", 500)
    request = AsyncMock(side_effect=[missing, {"market": market}])
    monkeypatch.setattr(client, "_authenticated_request", request)
    assert await client.get_market("GAME") == market
    assert request.await_args_list == [
        call("GET", "/markets/GAME"),
        call("GET", "/historical/markets/GAME"),
    ]


@pytest.mark.parametrize(
    ("result", "value", "yes", "no"),
    [
        ("yes", "1.0000", 100, 0),
        ("no", "0.0000", 0, 100),
        ("scalar", "0.3725", 37.25, 62.75),
    ],
)
def test_settlement_is_atomic_idempotent_and_releases_both_sides(tmp_path, result, value, yes, no):
    db_path = str(tmp_path / "paper.db")
    store = PaperStore(db_path)
    now = store.cutover_ts_ms + 100
    profile = _profile()
    yes_id = _entry(store, "GAME", "YES", 40, now, size=2, profile=profile)
    _entry(store, "GAME", "NO", 45, now, profile=profile)
    settlement = read_settlement(_market("GAME", result, value, now + 500))
    assert settlement is not None
    assert store.reconcile_market_settlement(settlement, now_ms=now + 1_000)
    restarted = PaperStore(db_path)
    assert not restarted.reconcile_market_settlement(settlement, now_ms=now + 2_000)
    assert restarted.get_intent_record(yes_id)["status"] == "CANCELED"
    assert restarted.get_positions() == []
    fills = {
        row["side"]: row
        for row in restarted.get_recent_fills()
        if row["execution_source"] == "kalshi_market_settlement"
    }
    assert set(fills) == {"YES", "NO"}
    assert fills["YES"]["fill_price_cents"] == yes
    assert fills["NO"]["fill_price_cents"] == no
    for fill in fills.values():
        assert fill["fill_fee_cents"] == 0
        assert fill["best_bid_at_fill_cents"] is None
        assert fill["settlement_ts_ms"] == now + 500
        assert fill["experiment_id"] == CLEAN_EXPERIMENT_ID
    metrics = restarted.compute_paper_metrics()
    expected = (yes - 40 - 0.42) + (no - 45 - 0.44)
    assert metrics["clean_realized_pnl_cents"] == pytest.approx(expected)
    assert metrics["bid_sweep_realized_pnl_cents"] == 0
    assert not restarted.insert_intent(
        QuoteIntent("late", "GAME", "YES", 40, 1, now + 3_000, 5, 8, 8, "maker", True),
        fee_profile=profile,
    )


def test_failed_settlement_rolls_back_claim_and_inventory(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    now = store.cutover_ts_ms
    _entry(store, "GAME", "YES", 45, now)
    settlement = read_settlement(_market("GAME", "yes", "1", now + 500))
    assert settlement is not None
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER abort_settlement BEFORE UPDATE ON paper_positions
            WHEN NEW.net_contracts = 0
            BEGIN SELECT RAISE(ABORT, 'test settlement interruption'); END
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="test settlement interruption"):
        store.reconcile_market_settlement(settlement, now_ms=now + 1_000)
    assert len(store.get_positions()) == 1
    assert store.count_fills_total() == 1
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_market_settlements").fetchone()[0] == 0
        conn.execute("DROP TRIGGER abort_settlement")
    assert store.reconcile_market_settlement(settlement, now_ms=now + 1_000)
    assert store.get_positions() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "value", "status"),
    [
        ("", None, "finalized"),
        ("scalar", None, "finalized"),
        ("yes", "0", "finalized"),
        ("no", "0", "determined"),
        ("yes", "1", "disputed"),
    ],
)
async def test_missing_or_provisional_settlement_keeps_inventory(tmp_path, result, value, status):
    store = PaperStore(str(tmp_path / "paper.db"))
    now = int(time.time() * 1000)
    _entry(store, "GAME", "YES", 45, now - 5_000)
    client = AsyncMock()
    client.get_market.return_value = _market("GAME", result, value, now, status=status)
    client.get_orderbook.return_value = {"yes": [], "no": []}
    assert await paper_exit_runtime._refresh_positions(store, client) == {}
    assert len(store.get_positions()) == 1
    assert store.get_total_realized_pnl_cents() == 0
    assert store.count_fills_total() == 1


@pytest.mark.asyncio
async def test_scanner_stop_preserves_marks_and_settlement_then_lifespan_recovers(
    tmp_path, monkeypatch
):
    db_path = str(tmp_path / "paper.db")
    store = PaperStore(db_path)
    now = int(time.time() * 1000)
    _entry(store, "FIRST", "YES", 45, now - 100)
    pending_id = _entry(store, "RESTART", "NO", 45, now - 100, size=2)
    client = AsyncMock()
    final_markets = set()

    async def get_market(ticker):
        if ticker in final_markets:
            return _market(ticker, "yes", "1", now)
        return {
            "ticker": ticker,
            "status": "active",
        }  # Legacy inventory uses its explicit fallback.

    client.get_market.side_effect = get_market
    client.get_orderbook.return_value = {"yes": [[45, 10]], "no": [[45, 10]]}
    monkeypatch.setattr(main, "KalshiClient", lambda: client)
    monkeypatch.setattr(main, "_paper_store", store)
    monkeypatch.setattr(main, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(config, "is_nba_scanner_enabled_from_env", lambda: True)
    monkeypatch.setattr(paper_exit_runtime, "SETTLEMENT_POLL_SECONDS", 0)
    scanner_started = asyncio.Event()

    async def scan(_client):
        scanner_started.set()
        await asyncio.Future()

    monkeypatch.setattr(main, "_nba_scanner_loop", scan)
    app = main.create_app()
    async with main.lifespan_production(app):
        await asyncio.wait_for(scanner_started.wait(), 2)
        worker = app.state.paper_maintenance_task
        async with asyncio.timeout(3):
            while ("FIRST", "YES") not in state.paper_liquidation_marks:
                await asyncio.sleep(0.01)
        request = Request({"type": "http", "app": app})
        assert await main.stop_scanner(request) == {"stopped": True}
        assert not worker.done()
        assert ("FIRST", "YES") in state.paper_liquidation_marks
        assert await main.start_scanner(request) == {"started": True}
        assert app.state.paper_maintenance_task is worker
        assert await main.stop_scanner(request) == {"stopped": True}
        final_markets.add("FIRST")
        async with asyncio.timeout(3):
            while any(pos["ticker"] == "FIRST" for pos in store.get_positions()):
                await asyncio.sleep(0.01)
        assert store.get_intent_record(pending_id)["status"] == "CANCELED"
    assert worker.done()

    restarted = PaperStore(db_path)
    monkeypatch.setattr(main, "_paper_store", restarted)
    monkeypatch.setattr(config, "is_nba_scanner_enabled_from_env", lambda: False)
    final_markets.add("RESTART")
    async with main.lifespan_production(app):
        assert app.state.scanner_task is None
        async with asyncio.timeout(3):
            while restarted.get_positions():
                await asyncio.sleep(0.01)
    assert restarted.get_intent_record(pending_id)["status"] == "CANCELED"
    client.aclose.assert_awaited()


@pytest.mark.asyncio
async def test_supervisor_retries_failed_worker(monkeypatch):
    recovered = asyncio.Event()
    invocations = 0

    async def worker(_client):
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            raise RuntimeError("simulated worker failure")
        recovered.set()
        await asyncio.Future()

    monkeypatch.setattr(main, "_paper_maintenance_loop", worker)
    task = asyncio.create_task(main._supervise_paper_maintenance(AsyncMock()))
    try:
        await asyncio.wait_for(recovered.wait(), 3)
        assert invocations == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_database_worker_claim_is_exclusive_and_released(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    other = PaperStore(store.db_path)
    with (
        store.claim_worker(),
        pytest.raises(RuntimeError, match="already owns"),
        other.claim_worker(),
    ):
        pytest.fail("Second worker should never enter")
    with other.claim_worker():
        assert other.cutover_ts_ms == store.cutover_ts_ms


@pytest.mark.asyncio
async def test_cutover_isolates_preexisting_balances_and_exits(tmp_path, monkeypatch):
    store = PaperStore(str(tmp_path / "paper.db"))
    now = store.cutover_ts_ms + 500
    store.upsert_position("GAME", "YES", 0, 39, 350, now - 1_000)
    store.upsert_position("GAME", "YES", 0, 39, 0, now - 500)
    store.insert_pnl_snapshot(now - 1, 350, 0, 0, 0)
    assert store.get_latest_pnl_snapshot()["realized_pnl_cents"] == 350
    assert store.get_latest_pnl_snapshot(clean_only=True) is None

    legacy_id = _entry(store, "CARRY", "NO", 45, store.cutover_ts_ms - 1)
    profile = _profile()
    intent_id = _entry(store, "GAME", "YES", 50, now, profile=profile)
    assert store.get_intent_record(intent_id)["fee_profile"]["version"] == profile.version
    assert (
        store.get_intent_record(legacy_id)["fee_profile"]["version"] == LEGACY_FEE_PROFILE.version
    )
    with pytest.raises(ValueError, match="Fill-backed"):
        store.upsert_position("GAME", "YES", 1, 50, 0, now)
    for pos in store.get_positions():
        quote = quote_liquidation(
            {pos["side"].lower(): [[55, 1]]}, pos["side"], 1, now + 100, fee_profile=profile
        )
        assert quote is not None
        assert store.close_position(pos, quote, now_ms=now + 100, reason="time_stop")

    reopened = PaperStore(store.db_path)
    monkeypatch.setattr(main, "_paper_store", reopened)
    expected_clean = 5 - 0.44 - 1.74
    expected_outside = 350 + 10 - 1 - 1.74
    summary = await main.get_paper_summary()
    positions = await main.get_paper_positions()
    snapshot = await main.get_paper_pnl_snapshot()
    assert summary["experiment_cutover_ts_ms"] == store.cutover_ts_ms
    assert summary["realized_pnl_cents"] == pytest.approx(expected_clean)
    assert summary["total_pnl_cents"] == pytest.approx(expected_clean)
    assert summary["non_experiment_realized_pnl_cents"] == pytest.approx(expected_outside)
    assert positions["total_realized_pnl_cents"] == pytest.approx(expected_clean)
    assert snapshot["realized_pnl_cents"] == pytest.approx(expected_clean)
    assert snapshot["ts_ms"] is None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pnl_snapshots").fetchone()[0] == 1
