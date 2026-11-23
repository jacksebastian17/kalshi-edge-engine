import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from kalshi_edge_engine.engine import universe
from kalshi_edge_engine.kalshi.read_budget import ReadBudget
from kalshi_edge_engine.runtime import config
from tests.test_kalshi_read_budget import _client
from tests.test_paper_link import A, C, fixture_session
from tests.test_research_campaign import listed
from tests.test_shadow_feasibility import B
from tools import paper_selection as selection
from tools.microstructure import journal, pilot
from tools.paper_bridge import ENROLLMENT
from tools.paper_link import collect, enroll, version_digest
from tools.paper_link_report import campaign_report
from tools.paper_selection import ALIGNED
from tools.research_campaign import aggregate
from tools.shadow_feasibility import encode

QUIET = "KXNBAGAME-A-QUIET"
NO_FEE = "KXNBAGAME-B-NOFEE"
OTHER = "KXNBAGAME-Z-OTHER"


def mock_preflight(monkeypatch, *, reverse=False, all_quiet=False, late_close=False):
    # Real discovery, budgeted GET client, fee resolver, scorer and quote builder.
    # MockTransport is the only HTTP transport and accepts only the enumerated public routes.
    tickers = [A, B, C, QUIET, NO_FEE, OTHER]
    if reverse:
        tickers.reverse()
    listings = {t: listed(t, "9999" if t == QUIET else "1") for t in tickers}
    seen = []

    def serve(req):
        assert req.method == "GET"
        path = req.url.path.removeprefix("/trade-api/v2")
        seen.append(path)
        if path == "/markets":
            assert req.url.params["series_ticker"] == "KXNBAGAME"
            result = {"markets": list(listings.values()), "cursor": ""}
        elif path.endswith("/orderbook"):
            t = path.split("/")[-2]
            quantity = 1 if t == QUIET or all_quiet else 500
            result = {"orderbook": {"yes": [[40, quantity]], "no": [[45, quantity]]}}
        elif path.startswith("/markets/"):
            m = dict(listings[path.split("/")[-1]])
            if late_close and seen.count(path) > 1:
                m["status"] = "settled"
            result = {"market": m}
        elif path.startswith("/events/"):
            game = path.split("/")[-1]
            if game == NO_FEE.rpartition("-")[0]:
                return httpx.Response(503)
            result = {"event": {"event_ticker": game, "series_ticker": "KXNBAGAME"}}
        else:
            assert path == "/series/KXNBAGAME"
            result = {
                "series": {
                    "ticker": "KXNBAGAME",
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": 1,
                }
            }
        return httpx.Response(200, json=result)

    policy = {"fixture": "paper", "universes": {"NBA": {}}}
    sha = selection.fingerprint(policy)
    monkeypatch.setattr(selection, "POLICY_SHA", sha)
    monkeypatch.setattr(
        selection,
        "frozen_runtime",
        lambda: {
            "policy": policy,
            "policy_sha256": sha,
            "commit": "fixture",
            "source_sha256": "fixture",
        },
    )
    monkeypatch.setattr(config, "get_enabled_universes", lambda: ["NBA"])
    monkeypatch.setattr(universe, "_nba_universe_cache", None)
    monkeypatch.setattr(pilot, "client", lambda: _client(ReadBudget(10000, 10000), serve))
    build = dict(pilot.build_info(), python="3.13.14", websockets="16.0")
    monkeypatch.setattr(pilot, "build_info", lambda: build)
    return seen


@pytest.mark.parametrize("reverse", [False, True])
async def test_rank_sources_and_separate_cohort(tmp_path, monkeypatch, reverse):
    mock_preflight(monkeypatch, reverse=reverse)
    campaign, tape = tmp_path / "campaign", tmp_path / "tapes" / "b3b2-mock"
    await selection.preflight(campaign, tape, 2, version_digest(ALIGNED))
    plan = json.loads(selection.plan_path(campaign, tape.name).read_bytes())
    rows = {r["ticker"]: r for r in plan["candidates"]}
    assert journal.manifest(tape)["markets"] == [C, A]
    assert {t: (r["reason"], r["rank"]) for t, r in rows.items()} == {
        C: ("selected", 1),
        A: ("selected", 2),
        B: ("event_already_selected", 3),
        OTHER: ("below_selection_limit", 4),
        QUIET: ("signal_not_valid", None),
        NO_FEE: ("fee_unavailable", None),
    }
    receipts = plan["receipts"]
    assert [receipts[n - 1]["path"] for n in rows[C]["source_receipts"]] == [
        "/markets/" + C,
        "/events/" + C.rpartition("-")[0],
        "/series/KXNBAGAME",
        "/markets/" + C + "/orderbook",
    ]
    assert all(
        hashlib.sha256(base64.b64decode(r["body_b64"])).hexdigest() == r["sha256"] for r in receipts
    )
    assert plan["ranked_ns"] <= plan["finished_ns"] <= journal.manifest(tape)["prepared_ns"]
    assert len(plan["confirmation_receipts"]) == 2
    with pytest.raises(ValueError, match="different paper cohort"):
        enroll(campaign, tape)
    with pytest.raises(ValueError, match="Stage 2B1"):
        aggregate([tape])
    directory = enroll(campaign, tape, ALIGNED)
    meta = json.loads((directory / ENROLLMENT).read_bytes())
    before, episodes = campaign_report(campaign, tape.parent, protocol=ALIGNED)
    assert before["selected_games"] == 2 and not episodes
    assert all(
        stage["scanner_visits"] == 0 and not stage["sealed"]
        for stage in before["sessions"][meta["paper_session"]][
            "selected_market_diagnostics"
        ].values()
    )
    with pytest.raises(ValueError, match="Invalid enrollment"):
        campaign_report(campaign, tape.parent)
    monkeypatch.setattr("tools.paper_link.configure_paper_profile", lambda: None)
    monkeypatch.setattr("tools.paper_link.frozen_runtime", lambda: {"policy_sha256": "changed"})
    with pytest.raises(ValueError, match="changed after selection"):
        await collect(directory, tape, 12, ALIGNED)
    assert not (directory / "paper").exists()
    with selection.plan_path(campaign, tape.name).open("ab") as f:
        f.write(b" ")
    with pytest.raises(ValueError, match="Invalid frozen"):
        campaign_report(campaign, tape.parent, protocol=ALIGNED)


@pytest.mark.parametrize("late_close", [False, True])
async def test_no_substitution_on_shortfall_or_confirmation_failure(
    tmp_path, monkeypatch, late_close
):
    mock_preflight(monkeypatch, all_quiet=not late_close, late_close=late_close)
    campaign, tape = tmp_path / "campaign", tmp_path / "b3b2-miss"
    with pytest.raises(ValueError, match="inactive|found 0"):
        await selection.preflight(campaign, tape, 2, version_digest(ALIGNED))
    plan = json.loads(selection.plan_path(campaign, tape.name).read_bytes())
    assert plan["status"] == "failed" and len(plan["candidates"]) == 6
    assert set(plan["selected"]) == ({A, C} if late_close else set())
    assert not (tape / journal.MANIFEST).exists() and not (campaign / "sessions").exists()


async def test_builder_quotes_on_disabled_sides_do_not_enter_ranking(tmp_path, monkeypatch):
    mock_preflight(monkeypatch)
    cfg = replace(
        config.get_effective_paper_config("NBA"), allow_yes_entries=False, allow_no_entries=False
    )
    monkeypatch.setattr(config, "get_effective_paper_config", lambda _: cfg)
    campaign, tape = tmp_path / "campaign", tmp_path / "b3b2-disabled"
    with pytest.raises(ValueError, match="found 0"):
        await selection.preflight(campaign, tape, 2, version_digest(ALIGNED))
    plan = json.loads(selection.plan_path(campaign, tape.name).read_bytes())
    first = next(r for r in plan["candidates"] if r["ticker"] == A)
    assert first["reason"] == "no_enabled_proposal" and len(first["proposals"]) == 2
    assert first["rank"] is None


@pytest.mark.parametrize("clock", [False, True])
async def test_prospective_miss_and_clock_linkage(tmp_path, monkeypatch, clock):
    mock_preflight(monkeypatch)
    campaign, tape = tmp_path / "campaign", tmp_path / "tapes" / "b3b2-mock"
    await selection.preflight(campaign, tape, 2, version_digest(ALIGNED))
    fixture_session(tmp_path, prepared=True, clock=clock)
    summary, rows = campaign_report(campaign, tape.parent, protocol=ALIGNED)
    (info,) = summary["sessions"].values()
    assert info["clock_mapping"] == ("verified_shared_process" if clock else "unverified_clock")
    stages = info["selected_market_diagnostics"]
    assert stages[C]["scanner_visits"] == 1 and stages[C]["signal_reasons"] == {"LOW_LIQUIDITY": 1}
    assert stages[C]["proposals"] == stages[C]["valid_signals"] == 0
    assert stages[A]["proposals"] == 4 and stages[A]["admitted"] == 0
    assert stages[A]["linked"] == (4 if clock else 0)
    assert summary["selected_games"] == 2 and summary["games_without_primary"] == [
        C.rpartition("-")[0]
    ]
    assert summary["original_sample_games_contributed"] == 0
    assert summary["primary_games"] == {"observed": 1, "minimum": None, "met": None}
    assert summary["primary_dates_utc"]["minimum"] is None
    assert len(rows) == 5 and rows[-1]["link_status"] == "market_outside_subscription"
    assert all(r["admission"] == "rejected" for r in rows)


def test_first_real_volume_ranked_pilot_reproduces_published_bytes_if_available(monkeypatch):
    campaign = Path.home() / "kalshi-paper-link-research" / "nba-paper-link-01"
    run = "b3b2-paper-c69905360c00427ab7340a3e1c114934"
    source = campaign / "sessions" / run
    if not source.is_dir():
        pytest.skip("External first paper pilot unavailable")
    # Replay only this enrollment even if operators later extend the original campaign.
    iterdir = Path.iterdir
    monkeypatch.setattr(
        Path, "iterdir", lambda p: iter([source]) if p == source.parent else iterdir(p)
    )
    summary, rows = campaign_report(campaign)
    (info,) = summary["sessions"].values()
    assert info["clock_mapping"] == "verified_shared_process"
    assert len(rows) == 2106 and all(
        r["link_status"] == "market_outside_subscription" for r in rows
    )
    assert (
        hashlib.sha256("".join(encode(r) + "\n" for r in rows).encode()).hexdigest()
        == "68096bbf3594599f80df5a126858ba607a9a7d969d448154069d9a9bcd5ed890"
    )
    assert (
        hashlib.sha256((encode(summary) + "\n").encode()).hexdigest()
        == "be6f32456a60e9a05cae6193ecd67d8e0e79097d75b485d39b4ddb5d48403139"
    )
