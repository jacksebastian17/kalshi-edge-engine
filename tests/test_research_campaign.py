import hashlib
import json
import shutil
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_shadow_feasibility import A, B, Case
from tools import capture_builds
from tools.microstructure import journal, pilot
from tools.research_campaign import aggregate
from tools.shadow_feasibility import analyze, encode

C = "KXNBAGAME-OTHER-BOS"
DAY = 86_400 * 10**9


def listed(t, volume, status="active"):
    return {
        "ticker": t,
        "event_ticker": t.rpartition("-")[0],
        "volume_24h_fp": volume,
        "status": status,
        "close_time": "2099-01-01T00:00:00Z",
    }


@pytest.mark.parametrize("reverse", [False, True])
async def test_prospective_selection_uses_only_preflight_gets(tmp_path, monkeypatch, reverse):
    last = "KXNBAGAME-Z-LAL"
    rows = [
        listed(B, "100"),
        listed(A, "100"),
        listed(C, "20.01"),
        listed(last, None),
        listed("KXNBAGAME-SETTLED-X", "999", "settled"),
        listed("KXNBAGAME-BAD-X", "NaN"),
    ]
    if reverse:
        rows.reverse()
    api = SimpleNamespace(
        get_markets=AsyncMock(
            side_effect=[{"markets": rows[:3], "cursor": "next"}, {"markets": rows[3:]}]
        ),
        get_market=AsyncMock(side_effect=lambda t: next(m for m in rows if m["ticker"] == t)),
    )

    @asynccontextmanager
    async def client():
        yield api

    monkeypatch.setattr(pilot, "client", client)
    build = pilot.build_info()
    build.update(python="3.13.14", websockets="16.0")
    monkeypatch.setattr(pilot, "build_info", lambda: build)
    path = tmp_path / "b3b2-diverse"
    result = await pilot.preflight(path, None, 2, distinct_events=True)
    assert result["markets"] == [A, C] and journal.manifest(path) == json.loads(
        journal.encoded(result)
    )
    assert {p.name for p in path.iterdir()} == {journal.MANIFEST}
    audit = result["selection"]
    assert audit["selected"] == {m["ticker"]: m["event_ticker"] for m in result["market_checks"]}
    assert {r["ticker"]: r["reason"] for r in audit["candidates"]} == {
        A: "selected",
        C: "selected",
        B: "event_already_selected",
        last: "below_selection_limit",
        "KXNBAGAME-SETTLED-X": "inactive_status",
        "KXNBAGAME-BAD-X": "invalid_reported_volume",
    }
    assert all(
        audit["started_ns"] <= r["observed_ns"] <= audit["ranked_ns"] <= result["prepared_ns"]
        for r in audit["candidates"]
    )
    assert [call.args[0] for call in api.get_market.await_args_list] == [A, C]
    assert [call.kwargs["cursor"] for call in api.get_markets.await_args_list] == [None, "next"]


async def test_shortfall_keeps_audit_and_never_substitutes_same_event(tmp_path, monkeypatch):
    api = SimpleNamespace(
        get_markets=AsyncMock(return_value={"markets": [listed(A, "8"), listed(B, "7")]}),
        get_market=AsyncMock(),
    )

    @asynccontextmanager
    async def client():
        yield api

    monkeypatch.setattr(pilot, "client", client)
    build = dict(pilot.build_info(), python="3.13.14", websockets="16.0")
    monkeypatch.setattr(pilot, "build_info", lambda: build)
    dest = tmp_path / "b3b2-short"
    with pytest.raises(ValueError, match="Requested 4 distinct active NBA events, found 1"):
        await pilot.preflight(dest, None, 4, distinct_events=True)
    failure = json.loads((dest / "preflight_failure.json").read_bytes())
    assert failure["selection"]["selected"] == {A: "KXNBAGAME-TEST"}
    assert not (dest / journal.MANIFEST).exists()
    api.get_market.assert_not_awaited()


async def test_four_event_limit_and_changed_confirmation():
    tickers = [A, C, "KXNBAGAME-THIRD-NYK", "KXNBAGAME-FOURTH-LAL"]
    markets = [listed(t, str(i)) for i, t in enumerate(tickers)]
    api = SimpleNamespace(
        get_markets=AsyncMock(return_value={"markets": markets}),
        get_market=AsyncMock(side_effect=lambda t: next(m for m in markets if m["ticker"] == t)),
    )
    selected = await pilot.select_events(api, 4, {})
    assert [m["ticker"] for m in selected] == list(reversed(tickers))
    api.get_market = AsyncMock(
        side_effect=lambda t: {**listed(t, "1"), "event_ticker": "KXNBAGAME-WRONG"}
    )
    with pytest.raises(ValueError, match="inactive|identity"):
        await pilot.select_events(api, 4, {})


def test_continuity_loss_remains_attached_to_primary_after_recovery(tmp_path):
    case = Case()
    gap = case.delta(60_050, 4, "-0.01")
    case.snapshot(60_100, 5)
    case.snapshot(60_101, 6, B)
    case.trade(121_000, 1, "1.00")
    path = case.seal(tmp_path, end=181_000)
    summary, rows = aggregate([path])
    primary = next(r for r in rows if r["role"] == "primary")
    assert primary["window_coverage"]["first_interruption_record"] == gap
    assert summary["primaries_with_interruption_or_conflict"] == 1
    assert summary["coverage_gates"]["usable_primary_windows"]["numerator"] == 0
    assert all(s["classification"] == "DATA_INCOMPLETE" for s in primary["scenarios"])


def test_first_eligible_per_game_retains_bad_windows_and_order_is_canonical(tmp_path):
    first = Case().seal(tmp_path / "first", end=61_000, session_id="s1")
    repeat = Case(wall_base=10**18 + 10**9, tickers=(A, C))
    repeat.trade(61_500, 1, "200")
    second = repeat.seal(tmp_path / "second", session_id="s2")
    third = Case(wall_base=10**18 + DAY, tickers=(A, C)).seal(tmp_path / "third", session_id="s3")
    out, rows = aggregate([second, third, first, first])
    other, reordered = aggregate([first, second, first, third])
    assert encode(out) == encode(other) and encode(rows) == encode(reordered)
    assert out["verified_sessions"] == 3 and out["duplicate_inputs"] == 1
    assert out["selected_games"] == out["primary_episodes"] == 2
    assert out["secondary_episodes"] == 18 and out["unique_observation_dates"] == 2
    assert out["unique_primary_dates"] == 1
    assert out["games"]["KXNBAGAME-TEST"]["overlapping_capture_intervals"][0]["sessions"] == [
        "s1",
        "s2",
    ]
    primaries = {r["event_ticker"]: r for r in rows if r["role"] == "primary"}
    chosen = primaries["KXNBAGAME-TEST"]
    assert (chosen["market"], chosen["side"], chosen["session"]) == (A, "yes", "s1")
    assert all(s["classification"] == "DATA_INCOMPLETE" for s in chosen["scenarios"])
    assert (
        primaries["KXNBAGAME-OTHER"]["scenarios"][0]["classification"]
        == "NO_OBSERVED_EXECUTION_SUPPORT"
    )
    assert any(
        r["role"] == "secondary"
        and r["scenarios"][0]["classification"] == "PUBLIC_EXECUTION_AT_PRICE"
        for r in rows
    )
    assert out["incomplete_or_unusable_primaries"] == 1 and out["assessment"] == "INCONCLUSIVE"
    assert out["expected_trading_profit"] == out["fill_probability"] == "UNIDENTIFIED"
    assert out["coverage_gates"]["usable_primary_windows"]["denominator_selected_games"] == 2
    assert out["coverage_gates"]["on_time_5s_marks"]["met"] is None


def test_roster_retains_unobserved_games_and_prevents_replacement(tmp_path):
    roster = tmp_path / "enrolled"
    roster.mkdir()
    early = Case(tickers=(A, C)).seal(tmp_path / "missing", session_id="missing")
    later = Case(wall_base=10**18 + DAY).seal(tmp_path / "later", session_id="later")
    for name, archive in (("first", early), ("second", later)):
        shutil.copyfile(archive / journal.MANIFEST, roster / (name + ".json"))
    report, rows = aggregate([later], roster=roster)
    assert report["selected_games"] == 2 and report["primary_episodes"] == 0
    assert report["verified_sessions"] == 1 and set(report["missing_enrolled_sessions"]) == {
        "missing"
    }
    assert report["games_without_observable_primary"] == 2 and all(
        r["role"] == "secondary" for r in rows
    )
    assert all(
        g["primary_unavailable_reason"] == "missing_enrolled_capture"
        for g in report["games"].values()
    )
    with (roster / "second.json").open("ab") as f:
        f.write(b"\n")
    with pytest.raises(ValueError, match="enrollment"):
        aggregate([later], roster=roster)


def test_no_slots_and_ineligible_slots_stay_in_denominator(tmp_path):
    case = Case()
    case.delta(10_000, 3, "-10.25")
    archive = case.seal(tmp_path / "zero_at_quote", end=121_000)
    report, rows = aggregate([archive])
    assert report["scheduled_slots_in_verified_sessions"] == 8
    assert report["slot_exclusions"] == {"no_passive_two_sided_quote": 4}
    assert report["games"]["KXNBAGAME-TEST"]["primary"]["market"] == B
    assert len([r for r in rows if r["role"] == "excluded"]) == 4
    quiet = Case().seal(tmp_path / "no_grid", end=40_000)
    report, rows = aggregate([quiet])
    assert (
        not rows
        and report["primary_episodes"] == 0
        and report["games_without_observable_primary"] == 1
    )


def test_archive_conflicts_and_tamper_fail_even_on_duplicate_input(tmp_path):
    first = Case().seal(tmp_path / "first")
    second = Case().seal(tmp_path / "second", end=130_000)
    with pytest.raises(ValueError, match="Conflicting verified"):
        aggregate([first, second])
    same_journal = Case().seal(tmp_path / "alias", session_id="different")
    with pytest.raises(ValueError, match="Identical journal"):
        aggregate([first, same_journal])
    with (second / journal.LOG).open("ab") as f:
        f.write(b"\n")
    with pytest.raises(ValueError):
        aggregate([first, second])


def test_capture_release_registry_is_exact_and_replay_dependencies_are_pinned(tmp_path):
    archive = Case().seal(tmp_path)
    meta = journal.manifest(archive)
    current = meta["build"]
    legacy = deepcopy(current)
    legacy["module_sha256"].update(
        {
            "__main__.py": "e9e4423407c8b294e367b2a273bb6fa3916791d4c4fb3eb07cbba046cfaadd91",
            "pilot.py": "48386fafc30838a27f31238cffb941dc5a25f8f2b1929174845024d360c295d4",
        }
    )
    assert (
        capture_builds.approved_capture(legacy)["capture_build_sha256"]
        == capture_builds.LEGACY_BUILD
    )
    capture_builds.verify_replay_modules(legacy)
    assert analyze(archive)[0]["source"]["capture_release"] == "b3b2-stage2b1-v1"
    for key in ("python", "websockets"):
        with pytest.raises(ValueError, match="Unapproved capture build"):
            capture_builds.approved_capture({**current, key: "unreviewed"})
    for name in ("pilot.py", "journal.py"):
        modified = deepcopy(legacy)
        modified["module_sha256"][name] = "0" * 64
        with pytest.raises(ValueError, match="Unapproved"):
            capture_builds.approved_capture(modified)
    with pytest.raises(ValueError, match="replay modules"):
        capture_builds.verify_replay_modules(modified)


def test_historical_stage2a_output_bytes_if_available():
    path = journal.ROOT / "b3b2-stage11-66884b73738e499a979ed8cb962719bc" / "backup"
    if not path.is_dir():
        pytest.skip("November 22 archive is external")
    summary, episodes = analyze(path)
    for rows, expected in (
        ([summary], "fe170474e7d36bee7fac4f1eb85b03481b2324ab727507c13714147a44b63004"),
        (episodes, "baa1d3f5e082b19d724d579940a7a6e1df99ad987f7fbefa1612baf80b211263"),
    ):
        assert (
            hashlib.sha256("".join(encode(r) + "\n" for r in rows).encode()).hexdigest() == expected
        )
