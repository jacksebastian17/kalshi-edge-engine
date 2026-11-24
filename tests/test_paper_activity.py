import hashlib
import json
import os
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from kalshi_edge_engine.engine.fees import LEGACY_FEE_PROFILE
from kalshi_edge_engine.engine.signal import SIGNAL_VALID
from kalshi_edge_engine.runtime import config, state
from tests.test_paper_link import A, C, fixture_session
from tests.test_paper_selection import OTHER, B, mock_preflight
from tools import paper_link, paper_selection
from tools.microstructure import journal
from tools.paper_activity import ACTIVE, entry_check, game_window, trade_activity, verify_activity
from tools.paper_link_report import campaign_report
from tools.shadow_feasibility import encode


def iso(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def milestone(game, start):
    return {
        "id": game,
        "type": "basketball_game",
        "category": "Sports",
        "start_date": iso(start),
        "related_event_tickers": [game],
    }


def print_row(ticker, stamp):
    return {
        "ticker": ticker,
        "trade_id": "print-1",
        "created_time": iso(stamp),
        "count_fp": "0.50",
        "is_block_trade": False,
    }


@pytest.mark.parametrize(
    "side,changes,intent_changes,reason",
    [
        ("YES", {}, {}, None),
        ("NO", {"capturable_edge_to_cross_yes_cents": None}, {}, None),
        (
            "YES",
            {"capturable_edge_to_cross_yes_cents": None},
            {},
            "missing_entry_selectivity_metric",
        ),
        ("YES", {"intended_quote_yes_cents": None}, {}, "missing_entry_selectivity_metric"),
        (
            "YES",
            {"capturable_edge_to_cross_yes_cents": 2.99, "top_liquidity": 0},
            {},
            "min_capturable_edge_to_cross",
        ),
        ("YES", {"intended_quote_yes_cents": 42.01}, {}, "min_abs_distance_from_50"),
        ("NO", {"top_liquidity": 199}, {"net_edge": 1}, "min_top_liquidity"),
        ("NO", {}, {"net_edge": 2.99}, "min_edge"),
    ],
)
async def test_exact_frozen_gate_boundary(monkeypatch, side, changes, intent_changes, reason):
    # Equality on all numeric boundaries passes; both sides share the last two gates.
    cfg = replace(
        config.get_effective_paper_config("NBA"),
        allow_yes_entries=True,
        allow_no_entries=True,
        min_capturable_edge_to_cross_cents=3,
        min_abs_distance_from_50_cents=8,
        min_top_liquidity=200,
        min_edge_cents=3,
    )
    monkeypatch.setattr(config, "ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(config, "get_effective_paper_config", lambda _: cfg)
    monkeypatch.setattr(state, "market_fees", None)
    signal = SimpleNamespace(
        **(
            {
                "universe": "NBA",
                "ticker": A,
                "fee_profile": LEGACY_FEE_PROFILE,
                "reason_code": SIGNAL_VALID,
                "top_liquidity": 200,
                "intended_quote_yes_cents": 42,
                "capturable_edge_to_cross_yes_cents": 3,
            }
            | changes
        )
    )
    intent = SimpleNamespace(
        **({"ticker": A, "side": side, "net_edge": 3, "universe": "NBA"} | intent_changes)
    )
    store_before = state.paper_store
    result = await entry_check(signal, intent, None)
    assert result["static_rejection"] == reason and result["deterministic_eligible"] == (
        reason is None
    )
    assert state.paper_store is store_before


def test_schedule_and_print_evidence_fail_closed_at_boundaries():
    cutoff = 1_800_000_000
    game = C.rpartition("-")[0]

    def scheduled(*ms):
        return game_window({"milestones": list(ms), "cursor": ""}, game, cutoff)["status"]

    assert scheduled(milestone(game, cutoff + 3600)) == "in_window"
    assert scheduled(milestone(game, cutoff - 10800)) == "in_window"
    assert scheduled(milestone(game, cutoff + 3601)) == "outside_game_window"
    assert scheduled(milestone(game, cutoff - 10801)) == "outside_game_window"
    assert scheduled(milestone(game, cutoff) | {"end_date": iso(cutoff)}) == "outside_game_window"
    assert scheduled(milestone("other", cutoff)) == "schedule_missing"
    assert scheduled(milestone(game, cutoff), milestone(game, cutoff)) == "schedule_ambiguous"
    assert scheduled(milestone(game, cutoff) | {"start_date": "unknown"}) == "invalid_schedule"

    def trades(*rows, cursor=""):
        return trade_activity({"trades": list(rows), "cursor": cursor}, C, cutoff)["status"]

    good = print_row(C, cutoff - 300)
    assert trades(good) == "recent_print"
    assert trades() == "no_recent_print" and trades(cursor="next") == "activity_unverified"
    for altered in (
        {"ticker": A},
        {"created_time": iso(cutoff)},
        {"created_time": iso(cutoff - 301)},
        {"count_fp": "NaN"},
        {"trade_id": ""},
        {"is_block_trade": True},
        {"is_block_trade": None},
    ):
        assert trades(good | altered) == "activity_unverified"


def active_preflight(monkeypatch, quiet=False):
    def activity(path, params):
        if path == "/milestones":
            game = params["related_event_ticker"]
            stamp = int(time.time()) + (2 * 86400 if game == OTHER.rpartition("-")[0] else 0)
            return httpx.Response(200, json={"milestones": [milestone(game, stamp)], "cursor": ""})
        assert params["is_block_trade"] == "false"
        return httpx.Response(
            200,
            json={
                "trades": [] if quiet else [print_row(params["ticker"], int(params["max_ts"]) - 1)],
                "cursor": "",
            },
        )

    mock_preflight(monkeypatch, activity_api=activity)
    cfg = replace(
        config.get_effective_paper_config("NBA"),
        allow_yes_entries=True,
        allow_no_entries=False,
        min_capturable_edge_to_cross_cents=3,
        min_abs_distance_from_50_cents=8,
        min_top_liquidity=200,
        min_edge_cents=3,
    )
    monkeypatch.setattr(config, "ENABLE_PAPER_TRADING", True)
    monkeypatch.setattr(config, "get_effective_paper_config", lambda _: cfg)


async def test_frozen_active_selection_misses_and_no_substitution(tmp_path, monkeypatch):
    campaign, tape = tmp_path / "campaign", tmp_path / "tapes" / "b3b2-mock"
    active_preflight(monkeypatch)
    await paper_selection.preflight(campaign, tape, 2, paper_link.version_digest(ACTIVE), ACTIVE)
    plan = json.loads(paper_selection.plan_path(campaign, tape.name).read_bytes())
    rows = {r["ticker"]: r for r in plan["candidates"]}
    assert set(plan["selected"]) == {A, C} and rows[B]["reason"] == "event_already_selected"
    assert rows[OTHER]["reason"] == "outside_game_window" and "activity" not in rows[OTHER]
    assert {p["side"]: p["static_rejection"] for p in rows[C]["proposals"]} == {
        "YES": None,
        "NO": "side_disabled",
    }
    with pytest.raises(ValueError, match="different paper cohort"):
        paper_link.enroll(campaign, tape, paper_selection.ALIGNED)
    fixture_session(tmp_path, prepared=True)
    summary, episodes = campaign_report(campaign, tape.parent, protocol=ACTIVE)
    (info,) = summary["sessions"].values()
    assert info["clock_mapping"] == "verified_shared_process" and summary["selected_games"] == 2
    assert summary["original_sample_games_contributed"] == 0
    assert info["selected_market_diagnostics"][C]["generated"] == 0
    stages = info["selected_market_diagnostics"][A]
    assert (stages["enabled_side"], stages["deterministic_pass"], stages["paper_admissions"]) == (
        3,
        2,
        1,
    )
    assert stages["paper_entry_bookings"] is None and stages["static_rejections"] == {
        "side_disabled": 1,
        "min_edge": 1,
    }
    assert len(episodes) == 5 and episodes[-1]["link_status"] == "market_outside_subscription"
    rows[A]["activity"]["query"]["max_ts"] += 1
    with pytest.raises(ValueError, match="query"):
        verify_activity(plan)


def test_cli_shortfall_remains_reportable_without_collection(tmp_path, monkeypatch):
    active_preflight(monkeypatch, quiet=True)
    monkeypatch.setattr(paper_link, "ACTIVE_ROOT", tmp_path / "active")
    monkeypatch.setattr(journal, "ROOT", tmp_path / "tapes")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "paper_link",
            "preflight-active",
            "--run",
            "b3b2-quiet",
            "--campaign",
            "c",
            "--count",
            "2",
        ],
    )
    with patch.dict(os.environ), pytest.raises(ValueError, match="found 0"):
        paper_link.main()
    campaign = paper_link.ACTIVE_ROOT / "c"
    summary, rows = campaign_report(campaign, journal.ROOT, protocol=ACTIVE)
    attempt = summary["preflight_attempts"]["b3b2-quiet"]
    assert not rows and summary["selected_games"] == 0 and attempt["status"] == "failed"
    assert attempt["candidate_reasons"]["no_recent_print"] == 3
    assert not (journal.ROOT / "b3b2-quiet" / journal.MANIFEST).exists()

    # After a hard stop only complete progress records can be counted.
    plan = paper_selection.plan_path(campaign, "b3b2-quiet")
    plan.unlink()
    with plan.with_suffix(".progress.jsonl").open("ab") as partial:
        partial.write(b'{"candidate"')
    summary, _ = campaign_report(campaign, journal.ROOT, protocol=ACTIVE)
    interrupted = summary["preflight_attempts"]["b3b2-quiet"]
    assert interrupted["status"] == "interrupted" and interrupted["contracts_evaluated"] == 6


async def test_expired_preflight_retains_both_enrolled_games(tmp_path, monkeypatch):
    active_preflight(monkeypatch)
    campaign, tape = tmp_path / "campaign", tmp_path / "tapes" / "b3b2-expired"
    await paper_selection.preflight(campaign, tape, 2, paper_link.version_digest(ACTIVE), ACTIVE)
    directory = paper_link.enroll(campaign, tape, ACTIVE)
    cutoff = json.loads(paper_selection.plan_path(campaign, tape.name).read_bytes())[
        "activity_cutoff_s"
    ]
    monkeypatch.setattr(time, "time_ns", lambda: (cutoff + 601) * 10**9)
    with patch.dict(os.environ), pytest.raises(ValueError, match="launch window"):
        await paper_link.collect(directory, tape, 12, ACTIVE)
    summary, rows = campaign_report(campaign, tape.parent, protocol=ACTIVE)
    assert summary["selected_games"] == 2 and not rows and not (directory / "paper").exists()


def test_sealed_first_aligned_pilot_reproduces_reports(monkeypatch):
    campaign = Path.home() / "kalshi-paper-aligned-research" / "nba-paper-aligned-01"
    source = campaign / "sessions" / "b3b2-aligned-2ab464ebe2f94796b6adc25d53fb8479"
    if not source.is_dir():
        pytest.skip("First aligned pilot is external")
    iterdir = Path.iterdir
    monkeypatch.setattr(
        Path, "iterdir", lambda p: iter([source]) if p == source.parent else iterdir(p)
    )
    summary, rows = campaign_report(campaign, protocol=paper_selection.ALIGNED)
    assert (
        hashlib.sha256((encode(summary) + "\n").encode()).hexdigest()
        == "8969020568f3826f3e19add958c979f44f5694f72c0c4d2c58230d4f429e5da8"
    )
    assert (
        hashlib.sha256("".join(encode(r) + "\n" for r in rows).encode()).hexdigest()
        == "2dffc44e1f5c7256861019bd483b904fff0624a52d4eb6ca5d4b155793d673f6"
    )
