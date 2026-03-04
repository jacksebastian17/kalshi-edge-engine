"""Tests for paper intent risk-limit enforcement."""

import asyncio
import contextlib
import gc
import os
import tempfile
import time
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.signal import SIGNAL_VALID, Signal


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
    )

    monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(main_module, "_paper_store", store)
    monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 0)
    monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
    monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)

    mock_client = AsyncMock()

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

    mock_client = AsyncMock()

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
    monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])
    monkeypatch.setattr(main_module, "RealisticOrderSimulator", _ImmediateSimulator)
    main_module._paper_simulation_tasks.clear()

    mock_client = AsyncMock()
    mock_client.get_orderbook = AsyncMock(return_value={"yes": [[50.0, 10]], "no": [[45.0, 10]]})

    await main_module._process_paper_intents(signal, mock_client)
    assert len(main_module._paper_simulation_tasks) == 1

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(main_module._paper_simulation_tasks) == 0

    gc.collect()
    if os.path.exists(db_path):
        with contextlib.suppress(OSError):
            os.unlink(db_path)
