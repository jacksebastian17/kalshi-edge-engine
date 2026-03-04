"""Tests for position exit lifecycle helpers and semantics."""

import time

import pytest

from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.engine.position_exit import (
    compute_realized_exit_pnl_cents,
    decide_position_exit,
    should_max_hold_exit,
    should_take_profit,
)


def test_should_take_profit_yes_side_per_contract() -> None:
    """YES side take-profit uses per-contract threshold."""
    assert (
        should_take_profit(
            side="YES",
            avg_entry_price_cents=50.0,
            mark_price_cents=52.5,
            take_profit_cents_per_contract=2.0,
        )
        is True
    )
    assert (
        should_take_profit(
            side="YES",
            avg_entry_price_cents=50.0,
            mark_price_cents=51.0,
            take_profit_cents_per_contract=2.0,
        )
        is False
    )


def test_should_take_profit_no_side_per_contract() -> None:
    """NO side take-profit is side-aware and uses per-contract threshold."""
    assert (
        should_take_profit(
            side="NO",
            avg_entry_price_cents=48.0,
            mark_price_cents=45.5,
            take_profit_cents_per_contract=2.0,
        )
        is True
    )
    assert (
        should_take_profit(
            side="NO",
            avg_entry_price_cents=48.0,
            mark_price_cents=47.0,
            take_profit_cents_per_contract=2.0,
        )
        is False
    )


def test_should_max_hold_exit() -> None:
    """Max-hold decision triggers only after threshold."""
    now_ms = int(time.time() * 1000)
    first_open_ts_ms = now_ms - 15_000

    assert (
        should_max_hold_exit(
            first_open_ts_ms=first_open_ts_ms,
            now_ms=now_ms,
            max_hold_seconds=10,
        )
        is True
    )
    assert (
        should_max_hold_exit(
            first_open_ts_ms=first_open_ts_ms,
            now_ms=now_ms,
            max_hold_seconds=20,
        )
        is False
    )


def test_compute_realized_exit_pnl_includes_fees_yes_no() -> None:
    """Realized exit PnL is side-aware and fee-aware for YES and NO."""
    yes_realized = compute_realized_exit_pnl_cents(
        side="YES",
        avg_entry_price_cents=50.0,
        mark_price_cents=54.0,
        net_contracts=3,
        exit_fee_cents=1.5,
    )
    assert yes_realized == pytest.approx((54.0 - 50.0) * 3 - 1.5)

    no_realized = compute_realized_exit_pnl_cents(
        side="NO",
        avg_entry_price_cents=48.0,
        mark_price_cents=44.0,
        net_contracts=2,
        exit_fee_cents=0.75,
    )
    assert no_realized == pytest.approx((48.0 - 44.0) * 2 - 0.75)


def test_decide_position_exit_priority_take_profit_then_time_stop() -> None:
    """Decision order is deterministic: take-profit before time-stop."""
    now_ms = int(time.time() * 1000)
    first_open_ts_ms = now_ms - 1_000_000

    decision = decide_position_exit(
        side="YES",
        avg_entry_price_cents=50.0,
        mark_price_cents=53.0,
        first_open_ts_ms=first_open_ts_ms,
        now_ms=now_ms,
        max_hold_seconds=60,
        take_profit_cents_per_contract=2.0,
    )
    assert decision.should_close is True
    assert decision.reason == "take_profit"


def test_paper_store_take_profit_per_contract_alias_roundtrip(tmp_path) -> None:
    """Store preserves backward-compatible key while normalizing semantics."""
    store = PaperStore(str(tmp_path / "test_take_profit_alias.db"))
    now_ms = int(time.time() * 1000)

    intent = QuoteIntent(
        id="tp-alias-1",
        ticker="TEST-TICKER-ALIAS",
        side="YES",
        quote_price_cents=50.0,
        size_contracts=1,
        created_ts_ms=now_ms,
        net_edge=1.0,
        inside_width=2.0,
        imbalance_gap=0.0,
        fee_mode="maker",
        round_trip=False,
    )

    store.insert_intent(
        intent,
        take_profit_cents_per_contract=2.5,
        max_hold_seconds=600,
    )

    record = store.get_intent_record(intent.id)
    assert record is not None
    assert record["take_profit_cents"] == pytest.approx(2.5)
    assert record["take_profit_cents_per_contract"] == pytest.approx(2.5)
