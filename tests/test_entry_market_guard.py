import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kalshi_edge_engine.db.paper_store import CLEAN_EXPERIMENT_ID, FillInsertResult, PaperStore
from kalshi_edge_engine.engine.fees import series_fee_profile
from kalshi_edge_engine.engine.orderbook import quote_liquidation
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.paper_sim import OrderRequest, RealisticOrderSimulator
from kalshi_edge_engine.engine.signal import score_signal
from kalshi_edge_engine.kalshi.market import MarketObservation
from kalshi_edge_engine.runtime import config, paper_runtime, state


@pytest.fixture
def profile():
    profile = series_fee_profile(
        {"ticker": "SERIES", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1},
        {"series_ticker": "SERIES", "event_ticker": "EVENT"},
        observed_ts_ms=1_000,
    )
    assert profile is not None
    return profile


def _observation(status, request, receipt, close, *, updated=None, ticker="GAME"):
    return MarketObservation(ticker, status, request, receipt, close, updated)


def _submit(store, profile, now, observation, intent_id="order", size=2):
    return store.insert_intent(
        QuoteIntent(intent_id, "GAME", "YES", 41, size, now, 4, 8, 8, "maker", True),
        fee_profile=profile,
        market_observation=observation,
    )


def _fill(store, profile, book_ts, observation, *, fill_id="fill", intent_id="order"):
    return store.insert_fill_result(
        fill_id=fill_id,
        intent_id=intent_id,
        fill_ts_ms=book_ts,
        fill_price_cents=41,
        fill_size=1,
        remaining_size=1,
        fill_fee_cents=profile.fee_cents(41, 1, liquidity="maker"),
        execution_source="simulated_higher_bid_heuristic",
        note="entry_moved_through",
        apply_to_position=True,
        fee_profile=profile,
        market_observation=observation,
        book_observed_ts_ms=book_ts,
    )


@pytest.mark.parametrize("status", ["closed", "inactive", "finalized", "determined", "disputed"])
def test_market_transition_cancels_remainder_and_preserves_earlier_inventory(
    tmp_path, profile, status
):
    clock = SimpleNamespace(now=1_000)
    store = PaperStore(str(tmp_path / "paper.db"), clock_ms=lambda: clock.now)
    assert _submit(store, profile, clock.now, _observation("active", 990, 999, 5_000))
    clock.now = 1_100
    result = _fill(store, profile, 1_080, _observation("active", 1_085, 1_095, 5_000))
    assert result.inserted_new_fill
    clock.now = 1_210
    observation = _observation(status, 1_200, 1_210, 1_200)
    for _ in range(2):
        assert not _fill(store, profile, 1_195, observation, fill_id="late").inserted_new_fill
    record = store.get_intent_record("order")
    assert record["status"] == "CANCELED"
    assert record["market_cancel_reason"] == status
    assert record["market_at_submit"]["close_ts_ms"] == 5_000
    assert record["market_close_ts_ms"] == 1_200
    assert store.get_positions()[0]["net_contracts"] == 1
    [fill] = store.get_recent_fills()
    assert fill["experiment_id"] == CLEAN_EXPERIMENT_ID
    assert fill["fill_ts_ms"] == 1_100
    assert fill["entry_market_evidence"]["book_observed_ts_ms"] == 1_080


def test_known_close_and_restart_prevent_delayed_active_from_reopening_old_intent(
    tmp_path, profile
):
    clock = SimpleNamespace(now=1_000)
    db = str(tmp_path / "paper.db")
    store = PaperStore(db, clock_ms=lambda: clock.now)
    assert _submit(store, profile, clock.now, _observation("active", 990, 999, 1_100, updated=940))
    clock.now = 1_100
    # A quote observed before close cannot be booked at an earlier timestamp after close.
    delayed = _observation("active", 1_080, 1_099, 1_100, updated=940)
    assert not _fill(store, profile, 1_070, delayed).inserted_new_fill
    assert store.get_intent_record("order")["market_cancel_reason"] == "close_time"
    clock.now = 1_210
    assert store.record_market_observation(
        _observation("closed", 1_190, 1_200, 1_100, updated=1_100)
    )

    restarted = PaperStore(db, clock_ms=lambda: clock.now)
    assert restarted.recover_interrupted_intents() == 0
    assert not restarted.record_market_observation(
        _observation("active", 1_180, 1_205, 5_000, updated=940)
    )
    assert not _submit(restarted, profile, clock.now, delayed, intent_id="stale")
    reopened = _observation("active", 1_201, 1_210, 5_000, updated=1_205)
    assert _submit(restarted, profile, clock.now, reopened, intent_id="new")
    restarted.update_intent_status("order", "OPEN")
    assert not restarted.entry_can_poll("order")
    assert restarted.get_intent_record("new")["market_close_ts_ms"] == 5_000
    assert not _fill(restarted, profile, 1_190, reopened, intent_id="order").inserted_new_fill


def test_stale_status_or_missing_evidence_cannot_book_a_guarded_fill(tmp_path, profile):
    clock = SimpleNamespace(now=1_000)
    store = PaperStore(str(tmp_path / "paper.db"), clock_ms=lambda: clock.now)
    assert not _submit(store, profile, clock.now, None)
    assert _submit(store, profile, clock.now, _observation("active", 995, 999, 8_000))
    clock.now = 3_800
    delayed = _observation("active", 1_200, 3_799, 8_000)
    assert not _fill(store, profile, 1_150, delayed).inserted_new_fill
    assert not _fill(store, profile, 3_700, None).inserted_new_fill
    assert not store.insert_fill(
        fill_id="raw",
        intent_id="order",
        fill_ts_ms=3_700,
        fill_price_cents=41,
        fill_size=1,
        remaining_size=1,
    )
    assert store.count_fills_total() == 0


def test_restart_cancels_guarded_partial_before_delayed_active_poll_finishes(tmp_path, profile):
    clock = SimpleNamespace(now=1_000)
    db_path = str(tmp_path / "paper.db")
    store = PaperStore(db_path, clock_ms=lambda: clock.now)
    assert _submit(store, profile, clock.now, _observation("active", 995, 999, 8_000))
    clock.now = 1_100
    assert _fill(
        store, profile, 1_060, _observation("active", 1_080, 1_099, 8_000)
    ).inserted_new_fill

    restarted = PaperStore(db_path, clock_ms=lambda: clock.now)
    assert restarted.recover_interrupted_intents() == 1
    clock.now = 1_200
    delayed = _observation("active", 1_150, 1_199, 8_000)
    assert not _fill(store, profile, 1_130, delayed, fill_id="late").inserted_new_fill
    assert restarted.get_intent_record("order")["market_cancel_reason"] == "restart"
    assert restarted.get_positions()[0]["net_contracts"] == 1
    assert restarted.count_fills_total() == 1


def test_authoritative_early_close_invalidates_already_booked_clean_lifecycle(tmp_path, profile):
    clock = SimpleNamespace(now=1_000)
    store = PaperStore(str(tmp_path / "paper.db"), clock_ms=lambda: clock.now)
    assert _submit(store, profile, clock.now, _observation("active", 990, 999, 9_000))
    clock.now = 1_100
    assert _fill(
        store, profile, 1_090, _observation("active", 1_095, 1_097, 9_000, updated=950)
    ).inserted_new_fill
    liquidation = quote_liquidation({"yes": [[55, 10]]}, "YES", 1, 1_150, fee_profile=profile)
    assert liquidation is not None
    assert store.close_position(store.get_positions()[0], liquidation, now_ms=1_150, reason="test")
    pnl = store.compute_paper_metrics()["clean_realized_pnl_cents"]
    assert pnl > 0
    store.insert_pnl_snapshot(
        1_150,
        pnl,
        0,
        0,
        0,
        valuation_details={"experiment_id": CLEAN_EXPERIMENT_ID},
    )
    clock.now = 1_250
    # The earlier-started request reports a newer exchange update and an earlier close.
    assert store.record_market_observation(
        _observation("closed", 1_050, 1_240, 1_075, updated=1_075)
    )
    assert store.compute_paper_metrics()["clean_realized_pnl_cents"] == 0
    assert store.get_total_realized_pnl_cents() == pytest.approx(pnl)
    assert store.get_latest_pnl_snapshot(clean_only=True) is None
    assert all(fill["market_timing_invalidated"] for fill in store.get_recent_fills())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["inactive", "active", "disabled_inside", "disabled_moved", "ledger_rejected"]
)
async def test_guarded_simulator_rechecks_market_before_candidate(
    tmp_path, profile, monkeypatch, status
):
    store = PaperStore(str(tmp_path / "paper.db"))
    if status == "ledger_rejected":
        monkeypatch.setattr(
            store, "insert_fill_result", lambda **_kwargs: FillInsertResult("rejected_market_state")
        )
    client = AsyncMock()
    book = {"yes": [[40, 500]], "no": [[45, 500]]}
    moved = {"yes": [[44, 500]], "no": [[45, 500]]}
    first_poll = book if status == "disabled_inside" else moved
    client.get_orderbook.side_effect = [book, first_poll]
    resolver = AsyncMock()

    async def observe(*_args):
        now_ms = int(time.time() * 1000)
        first = resolver.observe_entry_market.await_count == 1
        return _observation(
            "inactive" if not first and status == "inactive" else "active",
            now_ms,
            now_ms,
            now_ms + 60_000,
        )

    resolver.observe_entry_market.side_effect = observe
    tasks = set()
    monkeypatch.setattr(state, "market_fees", resolver)
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
        allow_moved_through=status != "disabled_moved",
        allow_price_improves=False,
        fill_probability_price_improves=1,
        min_edge_cents_moved_through=None,
        fill_probability_price_moved=1,
        poll_interval_seconds=0.01 if status in ("active", "inactive") else 1,
        fill_horizon_seconds=2 if status in ("active", "inactive") else 1,
    )
    monkeypatch.setattr(config, "get_effective_paper_config", lambda _universe: effective)
    await paper_runtime._process_paper_intents(
        score_signal("GAME", book, fee_profile=profile), client
    )
    assert len(tasks) == 1
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
    assert client.get_orderbook.await_count == 2
    assert resolver.observe_entry_market.await_count == 2
    aggregate = store.fill_funnel.aggregate()
    assert aggregate["instrumented_intents"] == aggregate["simulated_candidates"] == 1
    assert aggregate["polls"] == sum(aggregate["intents_by_status"].values()) == 1
    [terminal] = aggregate["terminal_reasons"]
    [intent] = store.get_intents_by_status(next(iter(aggregate["intents_by_status"])))
    history = store.fill_funnel.intent(intent["id"])
    assert history["terminal_reason"] == terminal
    assert history["submission_evidence"]["best_bid_size"] == 500
    assert history["submission_evidence"]["execution_config"]["allow_price_improves"] is False
    assert aggregate["persisted_candidate_fills"] == (status == "active")
    assert aggregate["policy_accepted_candidates"] == (status in ("active", "ledger_rejected"))
    if status == "ledger_rejected":
        assert aggregate["polls_by_rejection_reason"] == {"rejected_market_state": 1}
        assert history["polls"][0]["policy_accepted"]
        assert history["persisted_fills"] == []
        return
    if status.startswith("disabled"):
        path = "price_improves" if status == "disabled_inside" else "moved_through"
        assert aggregate["polls_by_rejection_reason"] == {f"fill_path_{path}_disabled": 1}
        assert history["polls"][0]["candidate"]["fill_path_type"] == path
        assert terminal == "timeout"
        assert history["quote_ever_observed"] is False
        assert store.get_positions() == []
        return
    if status == "inactive":
        assert terminal == "inactive"
        assert history["polls"][0]["evidence"]["market_at_validation"]["status"] == "inactive"
        assert aggregate["polls_by_rejection_reason"] == {"entry_inactive_at_validation": 1}
        assert len(store.get_intents_by_status("CANCELED")) == 1
        assert store.count_fills_total() == 0
        assert store.get_positions() == []
    else:
        assert terminal == "filled"
        [fill] = store.get_recent_fills()
        assert fill["entry_market_evidence"]["market"]["status"] == "active"
        assert fill["experiment_id"] == CLEAN_EXPERIMENT_ID
        assert store.get_positions()[0]["net_contracts"] == 1


@pytest.mark.asyncio
async def test_simulator_drops_book_returned_after_persisted_close():
    open_now = True
    client = AsyncMock()

    async def fetch(_ticker):
        nonlocal open_now
        open_now = False
        return {"yes": [[50, 100]], "no": [[45, 100]]}

    client.get_orderbook.side_effect = fetch
    order = OrderRequest(
        "order",
        "GAME",
        "YES",
        41,
        1,
        1_000,
        5,
        0.44,
        60,
        may_poll=lambda: open_now,
    )
    callback = AsyncMock()
    fills, result = await RealisticOrderSimulator(
        fill_probability_price_moved=1,
        poll_interval_seconds=0.01,
    ).simulate_order_fills(client, order, horizon_seconds=1, on_poll=callback)
    assert fills == []
    assert result["exit_reason"] == "entry_canceled"
    callback.assert_not_called()
