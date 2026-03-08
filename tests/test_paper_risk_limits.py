"""Tests for paper intent risk-limit enforcement."""

import asyncio
import contextlib
import gc
import os
import sqlite3
import tempfile
import time
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.paper_sim import Fill
from kalshi_edge_engine.engine.signal import SIGNAL_VALID, SPREAD_TOO_SMALL, Signal


@pytest.mark.asyncio
async def test_process_paper_intents_respects_max_open_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No new intents are created when open-intent cap is reached."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    store = PaperStore(db_path=db_path)

    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-26MAR01SASNYK-SAS",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=2.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=100,
        liq_no=100,
        top_liquidity=100,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=51.0,
        capturable_edge_to_cross_yes_cents=1.0,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 0)
    monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
    monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)

    class _ClientStub:
        async def get_orderbook(self, _ticker: str) -> dict[str, list[list[float]]]:
            return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

    mock_client = _ClientStub()

    await main_module._process_paper_intents(signal, mock_client)

    open_intents = store.get_intents_by_status("OPEN", limit=100)
    assert len(open_intents) == 0

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_respects_max_open_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No new intents are created when global open-position cap is reached."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    store = PaperStore(db_path=db_path)

    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-26MAR01SASNYK-SAS",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=2.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=100,
        liq_no=100,
        top_liquidity=100,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=51.0,
        capturable_edge_to_cross_yes_cents=1.0,
    )

    # Seed one open position so cap=1 blocks new intents.
    store.upsert_position(
        ticker="OTHER-TICKER",
        side="YES",
        net_contracts=1,
        avg_entry_price_cents=50.0,
        realized_pnl_cents=0.0,
        first_open_ts_ms=now_ms,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
    monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
    monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)
    monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 1)

    class _ClientStub:
        async def get_orderbook(self, _ticker: str) -> dict[str, list[list[float]]]:
            return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

    mock_client = _ClientStub()

    await main_module._process_paper_intents(signal, mock_client)

    open_intents = store.get_intents_by_status("OPEN", limit=100)
    assert len(open_intents) == 0

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_skips_below_min_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Intents below PAPER_MIN_EDGE_CENTS are skipped before persistence gates."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-26MAR01SASNYK-SAS",
        reason_code=SIGNAL_VALID,
        net_edge_yes=0.2,
        net_edge_no=0.2,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=100,
        liq_no=100,
        top_liquidity=100,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=51.0,
        capturable_edge_to_cross_yes_cents=1.0,
    )

    low_edge_intent = QuoteIntent(
        id="low-edge-intent-1",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=51.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=0.2,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 1.0)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [low_edge_intent])

    mock_client = AsyncMock()

    await main_module._process_paper_intents(signal, mock_client)

    assert store.get_recent_intents(limit=10) == []
    assert store.get_intents_by_status("OPEN", limit=10) == []

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_task_callback_removes_done_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed simulation task should be removed from tracking set via done callback."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-26MAR01SASNYK-SAS",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=0.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=100,
        liq_no=100,
        top_liquidity=100,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=51.0,
        capturable_edge_to_cross_yes_cents=1.0,
    )
    intent = QuoteIntent(
        id="callback-intent-1",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=51.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    class _ImmediateSimulator:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def simulate_order_fills(self, *_args, **_kwargs):
            return (
                [],
                {"exit_reason": "timeout", "last_seen_best_bid": 50.0, "queue_ahead_contracts": 0},
            )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 1.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
    monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 100)
    monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 100)
    monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 100)
    monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 100)
    monkeypatch.setattr(main_module, "_PAPER_MAX_INTENTS_PER_MINUTE", 1000)
    monkeypatch.setattr(main_module, "_PAPER_TICKER_COOLDOWN_SECONDS", 0)
    main_module._paper_daily_loss_breached = False
    main_module._paper_ticker_cooldown = {}
    main_module._paper_intent_timestamps = []
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])
    monkeypatch.setattr(main_module, "RealisticOrderSimulator", _ImmediateSimulator)
    main_module._paper_simulation_tasks.clear()

    mock_client = AsyncMock()
    mock_client.get_orderbook = AsyncMock(return_value={"yes": [[50.0, 10]], "no": [[45.0, 10]]})

    await main_module._process_paper_intents(signal, mock_client)
    assert len(store.get_recent_intents(limit=10)) == 1

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(main_module._paper_simulation_tasks) == 0

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_insert_fill_failure_skips_position_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected fill inserts should not mutate positions in simulation task."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-26MAR01SASNYK-SAS",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=0.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=100,
        liq_no=100,
        top_liquidity=100,
        timestamp_ms=now_ms,
    )
    intent = QuoteIntent(
        id="insert-fail-intent-1",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=51.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    class _SingleFillSimulator:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def simulate_order_fills(self, *_args, **_kwargs):
            fill_ts_ms = int(time.time() * 1000)
            return (
                [
                    Fill(
                        id="sim-fill-insert-fail-1",
                        order_id=intent.id,
                        fill_ts_ms=fill_ts_ms,
                        fill_price_cents=51.0,
                        fill_size=1,
                        remaining_size=0,
                        queue_ahead_at_fill=0,
                        fill_fee_cents=0.1,
                        best_bid_at_fill_cents=51.0,
                        cross_at_fill_cents=52.0,
                        fill_path_type="queue_cleared",
                    )
                ],
                {
                    "exit_reason": "filled",
                    "last_seen_best_bid": 51.0,
                    "queue_ahead_contracts": 0,
                },
            )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 1.0)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])
    monkeypatch.setattr(main_module, "RealisticOrderSimulator", _SingleFillSimulator)

    from kalshi_edge_engine.db.paper_store import FillInsertResult

    def _reject_insert_fill_result(*_args, **_kwargs) -> FillInsertResult:
        return FillInsertResult(status="error")

    monkeypatch.setattr(store, "insert_fill_result", _reject_insert_fill_result)
    main_module._paper_simulation_tasks.clear()

    mock_client = AsyncMock()

    await main_module._process_paper_intents(signal, mock_client)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    positions = store.get_positions()
    assert positions == []

    assert store.count_fills_total() == 0

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_side_gate_disables_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YES intents are skipped when PAPER_ALLOW_YES_ENTRIES is disabled."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-GATE-YES-OFF",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=2.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=200,
        liq_no=200,
        top_liquidity=200,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=51.0,
        capturable_edge_to_cross_yes_cents=1.0,
    )
    yes_intent = QuoteIntent(
        id="gate-yes-off-yes",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=51.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )
    no_intent = QuoteIntent(
        id="gate-yes-off-no",
        ticker=signal.ticker,
        side="NO",
        quote_price_cents=46.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    class _NoFillSimulator:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def simulate_order_fills(self, *_args, **_kwargs):
            return ([], {"exit_reason": "timeout", "last_seen_best_bid": 50.0})

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_YES_ENTRIES", False)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_NO_ENTRIES", True)
    monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
    monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
    monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [yes_intent, no_intent])
    monkeypatch.setattr(main_module, "RealisticOrderSimulator", _NoFillSimulator)

    mock_client = AsyncMock()
    mock_client.get_orderbook = AsyncMock(return_value={"yes": [[50.0, 10]], "no": [[45.0, 10]]})

    await main_module._process_paper_intents(signal, mock_client)
    intents = store.get_recent_intents(limit=10)
    assert len(intents) == 1
    assert intents[0].side == "NO"

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_side_gate_disables_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NO intents are skipped when PAPER_ALLOW_NO_ENTRIES is disabled."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-GATE-NO-OFF",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=2.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=200,
        liq_no=200,
        top_liquidity=200,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=51.0,
        capturable_edge_to_cross_yes_cents=1.0,
    )
    yes_intent = QuoteIntent(
        id="gate-no-off-yes",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=51.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )
    no_intent = QuoteIntent(
        id="gate-no-off-no",
        ticker=signal.ticker,
        side="NO",
        quote_price_cents=46.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_YES_ENTRIES", True)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_NO_ENTRIES", False)
    monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
    monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
    monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [yes_intent, no_intent])

    mock_client = AsyncMock()
    mock_client.get_orderbook = AsyncMock(return_value={"yes": [[50.0, 10]], "no": [[45.0, 10]]})

    await main_module._process_paper_intents(signal, mock_client)
    intents = store.get_recent_intents(limit=10)
    assert len(intents) == 1
    assert intents[0].side == "YES"

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_respects_min_top_liquidity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Intents are skipped when signal.top_liquidity is below configured floor."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-LIQ-FLOOR",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=2.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=100,
        liq_no=100,
        top_liquidity=100,
        timestamp_ms=now_ms,
    )
    intent = QuoteIntent(
        id="liq-floor-intent",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=51.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_YES_ENTRIES", True)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_NO_ENTRIES", True)
    monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 500)
    monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

    mock_client = AsyncMock()
    await main_module._process_paper_intents(signal, mock_client)

    assert store.get_recent_intents(limit=10) == []

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_signal_valid_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When valid-only gate is enabled, non-SIGNAL_VALID signals do not create intents."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-SIGNAL-GATE",
        reason_code=SPREAD_TOO_SMALL,
        net_edge_yes=2.0,
        net_edge_no=2.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=50.0,
        cross_yes=55.0,
        bid_no=45.0,
        cross_no=50.0,
        bid_sum=95.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=200,
        liq_no=200,
        top_liquidity=200,
        timestamp_ms=now_ms,
    )
    intent = QuoteIntent(
        id="signal-gate-intent",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=51.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

    mock_client = AsyncMock()
    await main_module._process_paper_intents(signal, mock_client)

    assert store.get_recent_intents(limit=10) == []

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_respects_min_capturable_edge_to_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YES intent is skipped when capturable edge-to-cross is below configured minimum."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-CAP-GATE",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=1.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=49.0,
        cross_yes=52.0,
        bid_no=48.0,
        cross_no=51.0,
        bid_sum=97.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=200,
        liq_no=200,
        top_liquidity=200,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=50.8,
        capturable_edge_to_cross_yes_cents=0.25,
    )
    intent = QuoteIntent(
        id="cap-gate-intent",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=50.8,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=3.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_YES_ENTRIES", True)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_NO_ENTRIES", False)
    monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
    monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
    monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.5)
    monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

    mock_client = AsyncMock()
    await main_module._process_paper_intents(signal, mock_client)
    assert store.get_recent_intents(limit=10) == []

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_respects_min_abs_distance_from_50(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YES intent is skipped when intended quote is too close to 50c."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)
    signal = Signal(
        ticker="KXNBAGAME-DIST50-GATE",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=1.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=49.0,
        cross_yes=52.0,
        bid_no=48.0,
        cross_no=51.0,
        bid_sum=97.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=200,
        liq_no=200,
        top_liquidity=200,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=50.2,
        capturable_edge_to_cross_yes_cents=1.0,
    )
    intent = QuoteIntent(
        id="dist50-gate-intent",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=50.2,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=3.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_YES_ENTRIES", True)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_NO_ENTRIES", False)
    monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
    monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
    monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.5)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

    mock_client = AsyncMock()
    await main_module._process_paper_intents(signal, mock_client)
    assert store.get_recent_intents(limit=10) == []

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_respects_reentry_delay_same_ticker_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry intent is skipped when same ticker+side was entered too recently."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quote_intents
            (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
             net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prior-entry-intent",
                "KXNBAGAME-REENTRY-GATE",
                "YES",
                50.0,
                1,
                now_ms - 2_000,
                1.0,
                2.0,
                0.0,
                "maker",
                True,
                "FILLED",
            ),
        )
        conn.commit()

    signal = Signal(
        ticker="KXNBAGAME-REENTRY-GATE",
        reason_code=SIGNAL_VALID,
        net_edge_yes=2.0,
        net_edge_no=1.0,
        inside_width_yes=3.0,
        inside_width_no=3.0,
        bid_yes=49.0,
        cross_yes=52.0,
        bid_no=48.0,
        cross_no=51.0,
        bid_sum=97.0,
        imbalance_gap=0.0,
        fee_yes=0.1,
        fee_no=0.1,
        fee_exit_yes=0.1,
        fee_exit_no=0.1,
        total_fee_yes=0.2,
        total_fee_no=0.2,
        fee_mode="maker",
        round_trip=True,
        liq_yes=200,
        liq_no=200,
        top_liquidity=200,
        timestamp_ms=now_ms,
        intended_quote_yes_cents=50.8,
        capturable_edge_to_cross_yes_cents=1.0,
    )
    intent = QuoteIntent(
        id="reentry-gated-intent",
        ticker=signal.ticker,
        side="YES",
        quote_price_cents=50.8,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=2.0,
        inside_width=3.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=True,
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_enabled", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_YES_ENTRIES", True)
    monkeypatch.setattr(main_module, "_PAPER_ALLOW_NO_ENTRIES", False)
    monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
    monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
    monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
    monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 5)
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

    mock_client = AsyncMock()
    await main_module._process_paper_intents(signal, mock_client)

    intents = store.get_recent_intents(limit=10)
    assert len(intents) == 1
    assert intents[0].id == "prior-entry-intent"

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)


@pytest.mark.asyncio
async def test_process_paper_intents_reentry_delay_excludes_synthetic_exit_intents() -> None:
    """Synthetic exit_intent rows do not trigger same ticker+side reentry suppression."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = PaperStore(db_path=db_path)
    now_ms = int(time.time() * 1000)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quote_intents
            (id, ticker, side, quote_price_cents, size_contracts, created_ts_ms,
             net_edge, inside_width, imbalance_gap, fee_mode, round_trip, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "exit_intent:take_profit:KXNBAGAME-REENTRY-EXIT:YES:1",
                "KXNBAGAME-REENTRY-EXIT",
                "YES",
                55.0,
                1,
                now_ms - 2_000,
                0.0,
                0.0,
                None,
                "taker",
                False,
                "FILLED",
            ),
        )
        conn.commit()

    latest_entry_ts = store.get_latest_entry_created_ts_ms_for_ticker_side(
        "KXNBAGAME-REENTRY-EXIT",
        "YES",
    )
    assert latest_entry_ts is None

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)
