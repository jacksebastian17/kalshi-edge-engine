"""Tests for paper intent risk-limit enforcement."""

import contextlib
import gc
import os
import tempfile
import time
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
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
