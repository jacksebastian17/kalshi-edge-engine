"""Tests for universe-specific paper config resolution and runtime usage."""

import asyncio
import contextlib
import gc
import os
import tempfile
import time

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.paper_sim import Fill
from kalshi_edge_engine.engine.signal import SIGNAL_VALID, Signal
from kalshi_edge_engine.runtime import config as runtime_config


def _sample_signal(*, universe: str) -> Signal:
    now_ms = int(time.time() * 1000)
    return Signal(
        ticker=f"KX{universe}GAME-26MAR01TEST",
        universe=universe,
        reason_code=SIGNAL_VALID,
        net_edge_yes=1.0,
        net_edge_no=1.0,
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


def _clear_universe_specific_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = [
        "PAPER_NBA_MIN_EDGE_CENTS",
        "PAPER_NBA_ALLOW_YES_ENTRIES",
        "PAPER_NBA_ALLOW_NO_ENTRIES",
        "PAPER_NBA_MIN_TOP_LIQUIDITY",
        "PAPER_NBA_REQUIRE_SIGNAL_VALID_ONLY",
        "PAPER_NBA_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS",
        "PAPER_NBA_MIN_ABS_DISTANCE_FROM_50_CENTS",
        "PAPER_NBA_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE",
        "PAPER_NBA_ALLOW_PRICE_IMPROVES",
        "PAPER_NBA_MIN_EDGE_CENTS_PRICE_IMPROVES",
        "PAPER_NBA_ALLOW_MOVED_THROUGH",
        "PAPER_NBA_MIN_EDGE_CENTS_MOVED_THROUGH",
        "PAPER_NBA_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR",
        "PAPER_NBA_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR",
        "PAPER_NBA_MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE",
        "PAPER_CBB_MIN_EDGE_CENTS",
        "PAPER_CBB_ALLOW_YES_ENTRIES",
        "PAPER_CBB_ALLOW_NO_ENTRIES",
        "PAPER_CBB_MIN_TOP_LIQUIDITY",
        "PAPER_CBB_REQUIRE_SIGNAL_VALID_ONLY",
        "PAPER_CBB_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS",
        "PAPER_CBB_MIN_ABS_DISTANCE_FROM_50_CENTS",
        "PAPER_CBB_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE",
        "PAPER_CBB_ALLOW_PRICE_IMPROVES",
        "PAPER_CBB_MIN_EDGE_CENTS_PRICE_IMPROVES",
        "PAPER_CBB_ALLOW_MOVED_THROUGH",
        "PAPER_CBB_MIN_EDGE_CENTS_MOVED_THROUGH",
        "PAPER_CBB_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR",
        "PAPER_CBB_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR",
        "PAPER_CBB_MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE",
    ]
    for key in keys:
        monkeypatch.delenv(key, raising=False)


def test_effective_paper_config_uses_global_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing universe override should fall back to global PAPER_* values."""
    monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS", 1.25)
    monkeypatch.delenv("PAPER_NBA_MIN_EDGE_CENTS", raising=False)

    effective = runtime_config.get_effective_paper_config("NBA")
    assert effective.min_edge_cents == pytest.approx(1.25)


def test_effective_paper_config_prefers_nba_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """NBA override should supersede global fallback values."""
    monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS", 1.0)
    monkeypatch.setattr(runtime_config, "PAPER_FILL_PROBABILITY", 0.8)
    monkeypatch.setenv("PAPER_NBA_MIN_EDGE_CENTS", "2.5")
    monkeypatch.setenv("PAPER_NBA_FILL_PROBABILITY", "0.2")

    effective = runtime_config.get_effective_paper_config("NBA")
    assert effective.min_edge_cents == pytest.approx(2.5)
    assert effective.fill_probability_price_moved == pytest.approx(0.2)


def test_effective_paper_config_prefers_cbb_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """CBB override should supersede global fallback values."""
    monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS", 0.9)
    monkeypatch.setattr(runtime_config, "PAPER_FILL_PROBABILITY_AT_BEST", 0.4)
    monkeypatch.setenv("PAPER_CBB_MIN_EDGE_CENTS", "1.8")
    monkeypatch.setenv("PAPER_CBB_FILL_PROBABILITY_AT_BEST", "0.05")

    effective = runtime_config.get_effective_paper_config("CBB")
    assert effective.min_edge_cents == pytest.approx(1.8)
    assert effective.fill_probability_at_best == pytest.approx(0.05)


def test_effective_paper_config_resolves_concentration_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concentration controls should support global defaults and universe-specific overrides."""
    monkeypatch.setattr(runtime_config, "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR", 5)
    monkeypatch.setattr(runtime_config, "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR", 4)
    monkeypatch.setattr(runtime_config, "PAPER_MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE", 3)
    monkeypatch.setenv("PAPER_NBA_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR", "2")
    monkeypatch.setenv("PAPER_NBA_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR", "1")

    nba_effective = runtime_config.get_effective_paper_config("NBA")
    cbb_effective = runtime_config.get_effective_paper_config("CBB")

    assert nba_effective.max_entry_samples_per_ticker_per_hour == 2
    assert nba_effective.max_entry_samples_per_ticker_side_per_hour == 1
    assert nba_effective.min_distinct_tickers_for_strategy_confidence == 3
    assert cbb_effective.max_entry_samples_per_ticker_per_hour == 5
    assert cbb_effective.max_entry_samples_per_ticker_side_per_hour == 4
    assert cbb_effective.min_distinct_tickers_for_strategy_confidence == 3


@pytest.mark.asyncio
async def test_process_paper_intents_applies_universe_specific_min_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry gating should use effective config for each signal universe."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)
        _clear_universe_specific_overrides(monkeypatch)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_PRICE_IMPROVES", True)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_MOVED_THROUGH", True)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES", None)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_MOVED_THROUGH", None)

        nba_signal = _sample_signal(universe="NBA")
        cbb_signal = _sample_signal(universe="CBB")

        nba_intent = QuoteIntent(
            id="intent-nba-edge",
            ticker=nba_signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        cbb_intent = QuoteIntent(
            id="intent-cbb-edge",
            ticker=cbb_signal.ticker,
            universe="CBB",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        class _SimulatorNoFill:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def simulate_order_fills(self, *_args: object, **_kwargs: object):
                return [], {
                    "exit_reason": "timeout",
                    "last_seen_best_bid": 50.0,
                    "queue_ahead_contracts": 0,
                }

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)

        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)

        monkeypatch.setenv("PAPER_NBA_MIN_EDGE_CENTS", "0.5")
        monkeypatch.setenv("PAPER_CBB_MIN_EDGE_CENTS", "2.0")

        monkeypatch.setattr(main_module, "RealisticOrderSimulator", _SimulatorNoFill)
        monkeypatch.setattr(
            main_module,
            "build_quote_intents",
            lambda signal: [nba_intent] if signal.universe == "NBA" else [cbb_intent],
        )

        client = _ClientStub()
        await main_module._process_paper_intents(nba_signal, client)
        await main_module._process_paper_intents(cbb_signal, client)

        recent_intents = {intent.id for intent in store.get_recent_intents(limit=10)}
        assert "intent-nba-edge" in recent_intents
        assert "intent-cbb-edge" not in recent_intents

        if main_module._paper_simulation_tasks:
            await asyncio.gather(*list(main_module._paper_simulation_tasks), return_exceptions=True)
            main_module._paper_simulation_tasks.clear()
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)


@pytest.mark.asyncio
async def test_path_policy_default_preserves_price_improves_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When new path policy vars are unset, price_improves fill remains allowed."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)
        _clear_universe_specific_overrides(monkeypatch)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_PRICE_IMPROVES", True)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_MOVED_THROUGH", True)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES", None)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_MOVED_THROUGH", None)

        signal = _sample_signal(universe="NBA")
        intent = QuoteIntent(
            id="intent-path-default-1",
            ticker=signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        class _PriceImprovesFillSimulator:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def simulate_order_fills(self, *_args: object, **_kwargs: object):
                fill = Fill(
                    id="sim-fill-path-default-1",
                    order_id=intent.id,
                    fill_ts_ms=now_ms + 1,
                    fill_price_cents=51.0,
                    fill_size=1,
                    remaining_size=0,
                    queue_ahead_at_fill=0,
                    fill_fee_cents=0.1,
                    best_bid_at_fill_cents=50.0,
                    cross_at_fill_cents=55.0,
                    fill_path_type="price_improves",
                )
                return [fill], {
                    "exit_reason": "filled",
                    "last_seen_best_bid": 50.0,
                    "queue_ahead_contracts": 0,
                }

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        monkeypatch.delenv("PAPER_ALLOW_PRICE_IMPROVES", raising=False)
        monkeypatch.delenv("PAPER_NBA_ALLOW_PRICE_IMPROVES", raising=False)
        monkeypatch.delenv("PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES", raising=False)
        monkeypatch.delenv("PAPER_NBA_MIN_EDGE_CENTS_PRICE_IMPROVES", raising=False)

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)
        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)

        monkeypatch.setattr(main_module, "RealisticOrderSimulator", _PriceImprovesFillSimulator)
        monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

        await main_module._process_paper_intents(signal, _ClientStub())
        if main_module._paper_simulation_tasks:
            await asyncio.gather(*list(main_module._paper_simulation_tasks), return_exceptions=True)

        fills = store.get_recent_fills(limit=10)
        assert any(fill["id"] == "sim-fill-path-default-1" for fill in fills)
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)


@pytest.mark.asyncio
async def test_path_policy_can_disable_price_improves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting PAPER_NBA_ALLOW_PRICE_IMPROVES=false rejects price-improves fills."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)
        _clear_universe_specific_overrides(monkeypatch)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_PRICE_IMPROVES", True)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_MOVED_THROUGH", True)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES", None)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_MOVED_THROUGH", None)

        signal = _sample_signal(universe="NBA")
        intent = QuoteIntent(
            id="intent-path-disable-1",
            ticker=signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        class _PriceImprovesFillSimulator:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def simulate_order_fills(self, *_args: object, **_kwargs: object):
                fill = Fill(
                    id="sim-fill-path-disable-1",
                    order_id=intent.id,
                    fill_ts_ms=now_ms + 1,
                    fill_price_cents=51.0,
                    fill_size=1,
                    remaining_size=0,
                    queue_ahead_at_fill=0,
                    fill_fee_cents=0.1,
                    best_bid_at_fill_cents=50.0,
                    cross_at_fill_cents=55.0,
                    fill_path_type="price_improves",
                )
                return [fill], {
                    "exit_reason": "filled",
                    "last_seen_best_bid": 50.0,
                    "queue_ahead_contracts": 0,
                }

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        monkeypatch.setenv("PAPER_NBA_ALLOW_PRICE_IMPROVES", "false")

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)
        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)

        monkeypatch.setattr(main_module, "RealisticOrderSimulator", _PriceImprovesFillSimulator)
        monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

        await main_module._process_paper_intents(signal, _ClientStub())
        if main_module._paper_simulation_tasks:
            await asyncio.gather(*list(main_module._paper_simulation_tasks), return_exceptions=True)

        fills = store.get_recent_fills(limit=10)
        assert all(fill["id"] != "sim-fill-path-disable-1" for fill in fills)
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)


@pytest.mark.asyncio
async def test_path_policy_price_improves_can_require_higher_min_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Higher min edge for price_improves rejects low-edge intents on that path."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)
        _clear_universe_specific_overrides(monkeypatch)

        signal = _sample_signal(universe="NBA")
        intent = QuoteIntent(
            id="intent-path-minedge-1",
            ticker=signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        class _PriceImprovesFillSimulator:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def simulate_order_fills(self, *_args: object, **_kwargs: object):
                fill = Fill(
                    id="sim-fill-path-minedge-1",
                    order_id=intent.id,
                    fill_ts_ms=now_ms + 1,
                    fill_price_cents=51.0,
                    fill_size=1,
                    remaining_size=0,
                    queue_ahead_at_fill=0,
                    fill_fee_cents=0.1,
                    best_bid_at_fill_cents=50.0,
                    cross_at_fill_cents=55.0,
                    fill_path_type="price_improves",
                )
                return [fill], {
                    "exit_reason": "filled",
                    "last_seen_best_bid": 50.0,
                    "queue_ahead_contracts": 0,
                }

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        monkeypatch.setenv("PAPER_NBA_MIN_EDGE_CENTS_PRICE_IMPROVES", "2.0")

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)
        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)

        monkeypatch.setattr(main_module, "RealisticOrderSimulator", _PriceImprovesFillSimulator)
        monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

        await main_module._process_paper_intents(signal, _ClientStub())
        if main_module._paper_simulation_tasks:
            await asyncio.gather(*list(main_module._paper_simulation_tasks), return_exceptions=True)

        fills = store.get_recent_fills(limit=10)
        assert all(fill["id"] != "sim-fill-path-minedge-1" for fill in fills)
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)


@pytest.mark.asyncio
async def test_concentration_gating_default_is_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With concentration limits unset, repeated ticker-side entries remain allowed."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)
        _clear_universe_specific_overrides(monkeypatch)
        monkeypatch.setattr(runtime_config, "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR", None)
        monkeypatch.setattr(
            runtime_config, "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR", None
        )

        signal = _sample_signal(universe="NBA")
        intent = QuoteIntent(
            id="intent-conc-default-1",
            ticker=signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        class _NoFillSimulator:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def simulate_order_fills(self, *_args: object, **_kwargs: object):
                return [], {
                    "exit_reason": "timeout",
                    "last_seen_best_bid": 50.0,
                    "queue_ahead_contracts": 0,
                }

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        # Seed a recent entry sample on same ticker-side; defaults should still allow new intent.
        seeded_intent = QuoteIntent(
            id="seed-conc-default-entry",
            ticker=signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms - 30_000,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        store.insert_intent(seeded_intent)
        assert store.insert_fill(
            fill_id="seed-conc-default-fill",
            intent_id=seeded_intent.id,
            fill_ts_ms=now_ms - 20_000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.1,
            note="entry_queue_cleared",
        )

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)
        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)

        monkeypatch.setattr(main_module, "RealisticOrderSimulator", _NoFillSimulator)
        monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

        await main_module._process_paper_intents(signal, _ClientStub())

        recent_intent_ids = {row.id for row in store.get_recent_intents(limit=10)}
        assert "intent-conc-default-1" in recent_intent_ids
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)


@pytest.mark.asyncio
async def test_concentration_gating_blocks_repeated_ticker_side_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured ticker-side cap should block repeated entry intent creation."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)
        _clear_universe_specific_overrides(monkeypatch)
        monkeypatch.setattr(runtime_config, "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR", 1)
        monkeypatch.setattr(runtime_config, "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR", None)

        signal = _sample_signal(universe="NBA")
        intent = QuoteIntent(
            id="intent-conc-block-1",
            ticker=signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        seeded_intent = QuoteIntent(
            id="seed-conc-block-entry",
            ticker=signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms - 30_000,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        store.insert_intent(seeded_intent)
        assert store.insert_fill(
            fill_id="seed-conc-block-fill",
            intent_id=seeded_intent.id,
            fill_ts_ms=now_ms - 20_000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.1,
            note="entry_queue_cleared",
        )

        class _NoFillSimulator:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def simulate_order_fills(self, *_args: object, **_kwargs: object):
                return [], {"exit_reason": "timeout"}

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)
        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)
        monkeypatch.setattr(main_module, "RealisticOrderSimulator", _NoFillSimulator)
        monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

        await main_module._process_paper_intents(signal, _ClientStub())

        recent_intent_ids = {row.id for row in store.get_recent_intents(limit=10)}
        assert "intent-conc-block-1" not in recent_intent_ids
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)


@pytest.mark.asyncio
async def test_concentration_gating_uses_universe_specific_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Universe override should apply ticker-side cap to NBA while leaving CBB fallback intact."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)
        _clear_universe_specific_overrides(monkeypatch)
        monkeypatch.setattr(runtime_config, "PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR", 10)
        monkeypatch.setenv("PAPER_NBA_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR", "1")

        nba_signal = _sample_signal(universe="NBA")
        cbb_signal = _sample_signal(universe="CBB")

        nba_intent = QuoteIntent(
            id="intent-conc-override-nba",
            ticker=nba_signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        cbb_intent = QuoteIntent(
            id="intent-conc-override-cbb",
            ticker=cbb_signal.ticker,
            universe="CBB",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        seeded_nba = QuoteIntent(
            id="seed-conc-override-nba-entry",
            ticker=nba_signal.ticker,
            universe="NBA",
            side="YES",
            quote_price_cents=50.0,
            size_contracts=1,
            created_ts_ms=now_ms - 30_000,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        store.insert_intent(seeded_nba)
        assert store.insert_fill(
            fill_id="seed-conc-override-nba-fill",
            intent_id=seeded_nba.id,
            fill_ts_ms=now_ms - 20_000,
            fill_price_cents=50.0,
            fill_size=1,
            remaining_size=0,
            queue_ahead_at_fill=0,
            fill_fee_cents=0.1,
            note="entry_queue_cleared",
        )

        class _NoFillSimulator:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def simulate_order_fills(self, *_args: object, **_kwargs: object):
                return [], {"exit_reason": "timeout"}

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)
        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 5)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 3)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)
        monkeypatch.setattr(main_module, "RealisticOrderSimulator", _NoFillSimulator)
        monkeypatch.setattr(
            main_module,
            "build_quote_intents",
            lambda signal: [nba_intent] if signal.universe == "NBA" else [cbb_intent],
        )

        client = _ClientStub()
        await main_module._process_paper_intents(nba_signal, client)
        await main_module._process_paper_intents(cbb_signal, client)

        recent_intent_ids = {row.id for row in store.get_recent_intents(limit=10)}
        assert "intent-conc-override-nba" not in recent_intent_ids
        assert "intent-conc-override-cbb" in recent_intent_ids
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)


@pytest.mark.asyncio
async def test_global_max_intents_per_minute_applies_across_universes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global intents-per-minute limiter should apply across all universes together."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)

        _clear_universe_specific_overrides(monkeypatch)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_PRICE_IMPROVES", True)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_MOVED_THROUGH", True)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES", None)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_MOVED_THROUGH", None)

        nba_signal = _sample_signal(universe="NBA")
        cbb_signal = _sample_signal(universe="CBB")

        nba_intent = QuoteIntent(
            id="intent-rate-global-nba",
            ticker="KXNBAGAME-RATE-GLOBAL-NBA",
            universe="NBA",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )
        cbb_intent = QuoteIntent(
            id="intent-rate-global-cbb",
            ticker="KXCBBGAME-RATE-GLOBAL-CBB",
            universe="CBB",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        class _SimulatorNoFill:
            def __init__(self, **_kwargs: object) -> None:
                pass

            async def simulate_order_fills(self, *_args: object, **_kwargs: object):
                return [], {"exit_reason": "timeout"}

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)

        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_TICKER_COOLDOWN_SECONDS", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_INTENTS_PER_MINUTE", 1)

        monkeypatch.setattr(main_module, "RealisticOrderSimulator", _SimulatorNoFill)
        monkeypatch.setattr(
            main_module,
            "build_quote_intents",
            lambda signal: [nba_intent] if signal.universe == "NBA" else [cbb_intent],
        )

        client = _ClientStub()
        await main_module._process_paper_intents(nba_signal, client)
        await main_module._process_paper_intents(cbb_signal, client)

        recent_intents = {intent.id for intent in store.get_recent_intents(limit=10)}
        assert "intent-rate-global-nba" in recent_intents
        assert "intent-rate-global-cbb" not in recent_intents
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)


@pytest.mark.asyncio
async def test_global_max_pos_per_ticker_applies_across_universes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_pos_per_ticker is intentionally global and not resolved per universe."""
    import kalshi_edge_engine.api.main as main_module

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        store = PaperStore(db_path=db_path)
        now_ms = int(time.time() * 1000)

        _clear_universe_specific_overrides(monkeypatch)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_PRICE_IMPROVES", True)
        monkeypatch.setattr(runtime_config, "PAPER_ALLOW_MOVED_THROUGH", True)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES", None)
        monkeypatch.setattr(runtime_config, "PAPER_MIN_EDGE_CENTS_MOVED_THROUGH", None)
        monkeypatch.setenv("PAPER_CBB_MIN_EDGE_CENTS", "0.5")

        shared_ticker = "KXSHARED-GLOBAL-POS-CAP"
        store.upsert_position(
            ticker=shared_ticker,
            side="YES",
            net_contracts=1,
            avg_entry_price_cents=50.0,
            realized_pnl_cents=0.0,
            first_open_ts_ms=now_ms,
        )

        signal = _sample_signal(universe="CBB")
        signal.ticker = shared_ticker
        signal.intended_quote_yes_cents = 51.0
        signal.capturable_edge_to_cross_yes_cents = 1.0
        intent = QuoteIntent(
            id="intent-global-pos-cap-cbb",
            ticker=shared_ticker,
            universe="CBB",
            side="YES",
            quote_price_cents=51.0,
            size_contracts=1,
            created_ts_ms=now_ms,
            net_edge=1.0,
            inside_width=2.0,
            imbalance_gap=0.0,
            fee_mode="maker",
            round_trip=True,
        )

        monkeypatch.setattr(main_module, "_ENABLE_PAPER_TRADING", True)
        monkeypatch.setattr(main_module, "_paper_enabled", True)
        monkeypatch.setattr(main_module, "_paper_store", store)
        monkeypatch.setattr(main_module, "_paper_simulation_tasks", set())
        monkeypatch.setattr(main_module, "_paper_ticker_cooldown", {})
        monkeypatch.setattr(main_module, "_paper_intent_timestamps", [])
        monkeypatch.setattr(main_module, "_paper_daily_loss_breached", False)

        monkeypatch.setattr(main_module, "_PAPER_MIN_EDGE_CENTS", 0.5)
        monkeypatch.setattr(main_module, "_PAPER_MIN_TOP_LIQUIDITY", 0)
        monkeypatch.setattr(main_module, "_PAPER_REQUIRE_SIGNAL_VALID_ONLY", True)
        monkeypatch.setattr(main_module, "_PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS", 0.0)
        monkeypatch.setattr(main_module, "_PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE", 0)
        monkeypatch.setattr(main_module, "_PAPER_MAX_CONCURRENT_INTENTS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_ORDERS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_OPEN_POSITIONS", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_INTENTS_PER_MINUTE", 20)
        monkeypatch.setattr(main_module, "_PAPER_MAX_POS_PER_TICKER", 1)

        monkeypatch.setattr(main_module, "build_quote_intents", lambda _signal: [intent])

        class _ClientStub:
            async def get_orderbook(self, _ticker: str):
                return {"yes": [[50.0, 10]], "no": [[45.0, 10]]}

        await main_module._process_paper_intents(signal, _ClientStub())

        recent_intents = {row.id for row in store.get_recent_intents(limit=10)}
        assert "intent-global-pos-cap-cbb" not in recent_intents
    finally:
        gc.collect()
        if os.path.exists(db_path):
            with contextlib.suppress(OSError):
                os.unlink(db_path)
