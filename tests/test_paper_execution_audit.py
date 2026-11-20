import asyncio
import sqlite3
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.db.paper_store import (
    MARK_MAX_AGE_MS,
    UTC_DAY_MS,
    PaperStore,
)
from kalshi_edge_engine.engine.orderbook import quote_liquidation
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.paper_sim import Fill, OrderRequest
from kalshi_edge_engine.engine.signal import score_signal
from kalshi_edge_engine.runtime import config, paper_runtime, state
from kalshi_edge_engine.runtime.paper_exit_runtime import _observe_due_outcomes, _refresh_positions


def _intent(store, ticker, side, price, size, now, suffix="1", **kwargs):
    intent = QuoteIntent(
        id=f"{ticker}:{side}:{suffix}",
        ticker=ticker,
        side=side,
        quote_price_cents=price,
        size_contracts=size,
        created_ts_ms=now,
        net_edge=4,
        inside_width=10,
        imbalance_gap=10,
        fee_mode="maker",
        round_trip=True,
    )
    store.insert_intent(intent, **kwargs)
    return intent


def _entry(store, intent, now, size, suffix="a", **kwargs):
    return store.insert_fill_result(
        fill_id=f"{intent.id}:{suffix}",
        intent_id=intent.id,
        fill_ts_ms=now,
        fill_price_cents=intent.quote_price_cents,
        fill_size=size,
        remaining_size=intent.size_contracts - size,
        fill_fee_cents=1,
        note="entry_queue_cleared",
        execution_source="simulated_displayed_size_decrease",
        apply_to_position=True,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_bid_side_mark_and_time_stop_outside_discovery(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    now = int(time.time() * 1000)
    intent = _intent(
        store,
        "NOT-IN-SIGNALS",
        "YES",
        46,
        1,
        now - 2_000,
        max_hold_seconds=60,
        take_profit_cents_per_contract=1,
    )
    assert _entry(store, intent, now - 1_900, 1).inserted_new_fill
    client = AsyncMock()
    client.get_orderbook.return_value = {"yes": [[45, 1]], "no": [[45, 20]]}
    marks = await _refresh_positions(store, client)
    quote = marks[intent.ticker, "YES"]
    assert (quote.best_bid_cents, quote.implied_ask_cents, quote.midpoint_cents) == (45, 55, 50)
    assert (quote.price_cents - 46) == -1
    assert store.compute_paper_metrics(liquidation_marks=marks)["unrealized_pnl_cents"] == -4
    assert store.get_total_realized_pnl_cents() == 0
    client.get_orderbook.assert_awaited_once_with(intent.ticker)

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE quote_intents SET max_hold_seconds = 1 WHERE id = ?", (intent.id,))
    client.get_orderbook.return_value = {"yes": [], "no": [[45, 20]]}
    assert await _refresh_positions(store, client) == {}
    assert store.get_positions()
    assert store.count_fills_total() == 1

    client.get_orderbook.return_value = {"yes": [[45, 1]], "no": [[45, 20]]}
    assert await _refresh_positions(store, client) == {}
    assert store.get_positions() == []
    assert store.get_total_realized_pnl_cents() == -4
    exit_fill = next(fill for fill in store.get_recent_fills() if fill["note"] == "exit_time_stop")
    assert exit_fill["fill_price_cents"] == exit_fill["best_bid_at_fill_cents"] == 45
    assert exit_fill["cross_at_fill_cents"] == 55
    assert exit_fill["bid_levels"] == [[45, 1]]
    assert exit_fill["execution_source"] == "simulated_displayed_bid_sweep"
    assert store.compute_paper_metrics()["entry_fills"] == 1


def test_depth_walk_and_partial_mark_coverage(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    for ticker in ("MARKED", "STALE", "THIN"):
        intent = _intent(store, ticker, "NO", 40, 3, 10)
        assert _entry(store, intent, 20, 3).inserted_new_fill
    book = {"orderbook_fp": {"no_dollars": [["0.45", "1.50"], ["0.42", "2"]], "yes_dollars": []}}
    quote = quote_liquidation(book, "NO", 3, 100)
    assert quote is not None
    assert quote.price_cents == 43
    assert quote.bid_levels == ((45, 1), (42, 2))
    assert quote.exit_fee_cents == 6
    assert quote_liquidation(book, "NO", 4, 100) is None
    assert quote_liquidation({"no": [[55, 5]], "yes": [[55, 5]]}, "NO", 1, 100) is None

    now = 100 + MARK_MAX_AGE_MS
    metrics = store.compute_paper_metrics(
        liquidation_marks={
            ("MARKED", "NO"): quote,
            ("STALE", "NO"): replace(quote, observed_ts_ms=99),
        },
        now_ms=now,
    )
    assert metrics["unrealized_pnl_cents"] is metrics["total_pnl_cents"] is None
    assert metrics["unrealized_pnl_status"] == "partial_marks"
    assert metrics["marked_unrealized_pnl_cents"] == 2
    assert metrics["unrealized_unmarked_positions_count"] == 2
    assert metrics["unmarked_contracts"] == 6
    assert metrics["unmarked_cost_basis_cents"] == 242
    assert metrics["oldest_mark_ts_ms"] == 100
    store.insert_pnl_snapshot(
        now,
        0,
        None,
        3,
        0,
        valuation_details={
            "unrealized_unmarked_positions_count": 2,
            "marked_unrealized_pnl_cents": 2,
        },
    )
    assert store.get_latest_pnl_snapshot()["total_pnl_cents"] is None


def test_reopen_preserves_realized_and_resets_fill_time_and_fees(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    # NO at 40 -> 45 gains 5c gross per contract. Cycle two loses 5c gross.
    accumulated = 0.0
    for cycle, (entry, bid, expected) in enumerate([(40, 45, 4), (50, 45, -16), (40, 50, 14)]):
        now = 1000 + cycle * 100
        intent = _intent(store, "CYCLES", "NO", entry, 2, now, str(cycle))
        assert _entry(store, intent, now + 10, 1).inserted_new_fill
        assert _entry(store, intent, now + 10, 1).already_exists
        assert _entry(store, intent, now + 11, 2, "over").rejected_overfill
        assert _entry(store, intent, now + 20, 1, "b").inserted_new_fill
        pos = store.get_positions()[0]
        assert pos["first_open_ts_ms"] == now + 10
        assert pos["net_contracts"] == 2
        assert pos["open_entry_fees_cents"] == 2
        assert pos["realized_pnl_cents"] == accumulated
        quote = quote_liquidation({"no": [[bid, 2]]}, "NO", 2, now + 21)
        assert quote is not None
        assert store.close_position(pos, quote, now_ms=now + 21, reason="time_stop")
        assert not store.close_position(pos, quote, now_ms=now + 21, reason="time_stop")
        accumulated += expected
        assert store.get_total_realized_pnl_cents() == accumulated
        assert store.get_positions() == []
    metrics = store.compute_paper_metrics()
    assert metrics["entry_fills"] == 6
    assert metrics["total_fills"] == 9
    assert metrics["bid_sweep_realized_pnl_cents"] == accumulated


def test_daily_loss_is_utc_scoped_and_latched_across_restart(tmp_path, monkeypatch):
    store = PaperStore(str(tmp_path / "paper.db"))
    day = int(time.time() * 1000) // UTC_DAY_MS * UTC_DAY_MS
    prior = _intent(store, "PRIOR-DAY", "NO", 80, 1, day - 30)
    assert _entry(store, prior, day - 20, 1).inserted_new_fill
    quote = quote_liquidation({"no": [[40, 1]]}, "NO", 1, day - 10)
    assert quote is not None
    assert store.close_position(
        store.get_positions()[0], quote, now_ms=day - 10, reason="time_stop"
    )
    assert store.get_total_realized_pnl_cents() < -31

    def risk(db, now, marks=None):
        return db.evaluate_daily_loss(now_ms=now, max_loss_cents=31, liquidation_marks=marks or {})

    assert risk(store, day + 1)["daily_realized_pnl_cents"] == 0
    assert not risk(store, day + 1)["daily_loss_breached"]
    current = _intent(store, "TODAY", "YES", 30, 2, day + 3)
    assert _entry(store, current, day + 4, 1).inserted_new_fill
    first_breach = risk(store, day + 5)
    assert first_breach["daily_risk_pnl_cents"] == -31
    assert first_breach["daily_loss_breached"]
    assert store.get_intent_record(current.id)["status"] == "CANCELED"

    restarted = PaperStore(store.db_path)
    recovered_quote = quote_liquidation({"yes": [[45, 1]]}, "YES", 1, day + 6)
    marks = {("TODAY", "YES"): recovered_quote}
    assert risk(restarted, day + 7, marks)["daily_risk_pnl_cents"] == 0
    assert risk(restarted, day + 7, marks)["daily_loss_breach_ts_ms"] == day + 5
    monkeypatch.setattr(state, "paper_store", restarted)
    monkeypatch.setattr(state, "paper_enabled", True)
    monkeypatch.setattr(state, "paper_liquidation_marks", marks)
    monkeypatch.setattr(config, "PAPER_MAX_DAILY_LOSS_CENTS", 31)
    assert (
        paper_runtime._get_rate_limit_or_cooldown_reason("FRESH", day + 7)
        == "max_daily_loss_breached"
    )
    next_day = day + UTC_DAY_MS
    assert not risk(
        restarted,
        next_day,
        {key: replace(recovered_quote, observed_ts_ms=next_day) for key in marks},
    )["daily_loss_breached"]


@pytest.mark.asyncio
async def test_horizons_record_observation_time_and_exclude_delayed_samples(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    now = int(time.time() * 1000)
    for name, age in (("ON-TIME", 5_100), ("DELAYED", 11_000), ("TOO-OLD", 30_100)):
        intent = _intent(store, name, "NO", 40, 1, now - age - 1)
        assert _entry(store, intent, now - age, 1).inserted_new_fill
    client = AsyncMock()
    client.get_orderbook.return_value = {"yes": [[45, 1]], "no": [[45, 1]]}
    await _observe_due_outcomes(store, client, now)
    fills = {row["ticker"]: row for row in store.get_recent_fills()}
    assert fills["ON-TIME"]["pnl_5s_after_fill_cents"] == 2
    assert fills["ON-TIME"]["observed_5s_ts_ms"] >= now
    assert fills["DELAYED"]["observed_5s_ts_ms"] - fills["DELAYED"]["fill_ts_ms"] >= 11_000
    assert fills["TOO-OLD"]["mark_5s_after_fill_cents"] is None
    assert fills["TOO-OLD"]["pnl_30s_after_fill_cents"] == 2
    metrics = store.get_short_horizon_outcome_metrics(horizon_seconds=5)
    assert metrics["total_samples"] == 1
    assert metrics["observation_timing_counts"] == {"delayed": 1, "missed": 1, "on_time": 1}


def test_recovery_keeps_partial_inventory_and_rejects_late_fills(tmp_path):
    store = PaperStore(str(tmp_path / "paper.db"))
    intent = _intent(store, "RECOVER", "YES", 40, 3, 100)
    assert _entry(store, intent, 110, 1).inserted_new_fill
    restarted = PaperStore(store.db_path)
    assert restarted.recover_interrupted_intents() == 1
    assert restarted.recover_interrupted_intents() == 0
    assert restarted.get_intent_record(intent.id)["status"] == "CANCELED"
    assert restarted.get_positions()[0]["net_contracts"] == 1
    assert _entry(restarted, intent, 120, 1).already_exists
    late = _entry(restarted, intent, 130, 1, "late")
    assert late.status == "rejected_inactive_intent"
    assert restarted.get_filled_size_for_intent(intent.id) == 1


def test_failed_inventory_write_rolls_back_fill_and_status(tmp_path, monkeypatch):
    store = PaperStore(str(tmp_path / "paper.db"))
    intent = _intent(store, "ROLLBACK", "YES", 40, 1, 100)

    def fail(*_args):
        raise sqlite3.IntegrityError("test interruption")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_apply_entry_fill", fail)
        assert _entry(store, intent, 110, 1).error
    assert store.get_filled_size_for_intent(intent.id) == 0
    assert store.get_intent_record(intent.id)["status"] == "OPEN"
    assert store.get_positions() == []
    assert _entry(store, intent, 110, 1).inserted_new_fill
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER abort_exit BEFORE UPDATE ON paper_positions
            WHEN NEW.net_contracts = 0
            BEGIN SELECT RAISE(ABORT, 'test exit interruption'); END
            """
        )
    quote = quote_liquidation({"yes": [[50, 1]]}, "YES", 1, 120)
    assert quote is not None
    with pytest.raises(sqlite3.IntegrityError):
        store.close_position(store.get_positions()[0], quote, now_ms=120, reason="time_stop")
    assert store.get_positions()[0]["net_contracts"] == 1
    assert store.count_intents_total() == 1
    assert store.count_fills_total() == 1
    assert store.get_total_realized_pnl_cents() == 0


@pytest.mark.asyncio
async def test_cancel_running_simulation_preserves_checkpointed_fill(tmp_path, monkeypatch):
    store = PaperStore(str(tmp_path / "paper.db"))
    tasks = set()
    monkeypatch.setattr(state, "paper_store", store)
    monkeypatch.setattr(state, "paper_enabled", True)
    monkeypatch.setattr(state, "paper_simulation_tasks", tasks)
    monkeypatch.setattr(state, "paper_ticker_cooldown", {})
    monkeypatch.setattr(state, "paper_intent_timestamps", [])
    monkeypatch.setattr(config, "ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(config, "PAPER_MAX_DAILY_LOSS_CENTS", 0)
    effective = replace(
        config.get_effective_paper_config("NBA"),
        allow_yes_entries=True,
        allow_no_entries=False,
        min_top_liquidity=0,
        min_edge_cents=0,
        min_capturable_edge_to_cross_cents=0,
        min_abs_distance_from_50_cents=0,
        min_reentry_delay_seconds_same_ticker_side=0,
    )
    monkeypatch.setattr(config, "get_effective_paper_config", lambda _universe: effective)
    checkpoint = asyncio.Event()

    class PartialSimulator:
        def __init__(self, **_kwargs):
            pass

        async def simulate_order_fills(self, _client, order: OrderRequest, *, on_poll, **_kwargs):
            assert on_poll(
                {
                    "fill": Fill(
                        id="partial",
                        order_id=order.id,
                        fill_ts_ms=int(time.time() * 1000),
                        fill_price_cents=order.quote_price_cents,
                        fill_size=1,
                        remaining_size=2,
                        queue_ahead_at_fill=0,
                        fill_fee_cents=1,
                        fill_path_type="queue_cleared",
                    )
                }
            )
            checkpoint.set()
            await asyncio.Future()

    monkeypatch.setattr(paper_runtime, "RealisticOrderSimulator", PartialSimulator)
    book = {"yes": [[40, 500]], "no": [[45, 500]]}
    signal = score_signal("CANCEL-ME", book, universe="NBA")
    build = paper_runtime.build_quote_intents
    monkeypatch.setattr(
        paper_runtime,
        "build_quote_intents",
        lambda signal: [replace(intent, size_contracts=3) for intent in build(signal)],
    )
    client = AsyncMock()
    client.get_orderbook.return_value = book
    await paper_runtime._process_paper_intents(signal, client)
    await asyncio.wait_for(checkpoint.wait(), timeout=1)
    task = next(iter(tasks))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_intents_by_status("OPEN") == []
    assert len(store.get_intents_by_status("CANCELED")) == 1
    assert store.get_positions()[0]["net_contracts"] == 1
