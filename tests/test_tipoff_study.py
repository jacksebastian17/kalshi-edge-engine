import base64
import hashlib
import json
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tools import tipoff_study as study
from tools.tipoff_metrics import measurements, tick_cents

DAY = date(2026, 11, 25)
START = 1795662000000
GAMES = ["KXNBAGAME-26NOV25" + k for k in ("AAABBB", "CCCDDD", "EEEFFF")]


def market(game, suffix):
    return {
        "ticker": game + "-" + suffix,
        "event_ticker": game,
        "market_type": "binary",
        "custom_strike": {"basketball_team": suffix},
        "status": "active",
        "close_time": "2026-12-01T00:00:00Z",
        "price_level_structure": "linear_cent",
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }


def book(yes="0.4000", no="0.5900", quantity="237.91"):
    return {"orderbook_fp": {"yes_dollars": [[yes, "1.25"]], "no_dollars": [[no, quantity]]}}


class Fixture:
    def __init__(self, tmp_path, monkeypatch):
        self.path = tmp_path / DAY.isoformat()
        self.now = (START * 10**6) - 180 * study.MINUTE
        self.calls = []
        self.override = {}
        self.client = httpx.Client(transport=httpx.MockTransport(self.serve))
        monkeypatch.setattr(
            study, "time", SimpleNamespace(time_ns=lambda: self.now, monotonic_ns=lambda: self.now)
        )

    def serve(self, request):
        assert request.method == "GET" and request.url.host == "external-api.kalshi.com"
        assert "authorization" not in request.headers and "kalshi-access-key" not in request.headers
        path = request.url.path.removeprefix("/trade-api/v2")
        self.calls.append(path)
        if path in self.override:
            return self.override[path](request)
        if path == "/markets":
            page = request.url.params.get("cursor")
            chosen = [GAMES[1], GAMES[0]] if page else [GAMES[2], GAMES[1]]
            data = {
                "markets": [market(g, k) for g in chosen for k in ("X", "Y")],
                "cursor": "" if page else "next",
            }
        elif path == "/milestones":
            g = request.url.params["related_event_ticker"]
            data = {
                "milestones": [
                    {
                        "id": g,
                        "primary_event_tickers": [g],
                        "category": "Sports",
                        "type": "basketball_game",
                        "details": {"status": "scheduled", "main_game_event_ticker": g},
                        "start_date": study.iso(START * 10**6),
                    }
                ],
                "cursor": "",
            }
        elif path.startswith("/events/"):
            g = path.split("/")[-1]
            assert request.url.params["with_nested_markets"] == "true"
            data = {
                "event": {
                    "event_ticker": g,
                    "series_ticker": study.SERIES,
                    "mutually_exclusive": True,
                    "markets": [market(g, "Y"), market(g, "X")],
                }
            }
        elif path == "/series/KXNBAGAME":
            data = {
                "series": {
                    "ticker": study.SERIES,
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": 1,
                }
            }
        else:
            assert path.endswith("/orderbook")
            data = book() if "-X/" in path else book("0.3800")
        return httpx.Response(200, json=data)

    def enroll(self):
        return study.enroll(self.path, DAY, self.client)

    def observe(self):
        return study.observe(self.path, self.client)


@pytest.fixture
def fx(tmp_path, monkeypatch):
    fixture = Fixture(tmp_path, monkeypatch)
    yield fixture
    fixture.client.close()


def test_prospective_enrollment_and_equal_game_weight(fx):
    plan = fx.enroll()
    assert list(plan["nominees"]) == GAMES[:2] and plan["listed_events"] == GAMES
    assert all(len(g["contracts"]) == 2 for g in plan["nominees"].values())
    assert not any("/orderbook" in p for p in fx.calls)
    with pytest.raises(FileExistsError):
        fx.enroll()
    before = len(fx.calls)
    begin, end = study.bounds(START, "advance")
    fx.now = begin - 1
    assert fx.observe() == [] and len(fx.calls) == before
    fx.now = begin
    assert len(fx.observe()) == 2
    assert fx.observe() == [] and len(fx.calls) == before + 10
    fx.now = end
    assert fx.observe() == []
    result = study.report(fx.path, fx.now)
    assert study.report(fx.path, fx.now) == result
    assert study.study_report(fx.path.parent, fx.now)["by_window"] == result["by_window"]
    assert result["distinct_games"] == 2 and result["original_sample_games_contributed"] == 0
    summary = result["by_window"]["advance"]
    assert (
        summary["paired_games"] == 2 and Decimal(summary["mean_of_paired_game_spreads_cents"]) == 2
    )
    assert summary["games_with_observed_branch"]["structural_gap_below_threshold"] == 2
    g = result["games"][GAMES[0]]
    assert g["actual_game_state"] is None
    w = g["windows"]["advance"]
    assert w["milestone_reported_state"] == "scheduled"
    assert Decimal(summary["mean_of_paired_game_top_contracts"]["YES"]) == Decimal("1.25")
    row = w["contracts"][GAMES[0] + "-X"]
    assert row["offset_from_start_ns"] == -125 * study.MINUTE
    assert (
        Decimal(row["sides"]["YES"]["bid_cents"]) == 40
        and Decimal(row["sides"]["YES"]["implied_ask_cents"]) == 41
    )
    assert Decimal(row["sides"]["YES"]["same_side_top_contracts"]) == Decimal("1.25")
    assert Decimal(row["sides"]["YES"]["opposite_side_top_contracts"]) == Decimal("237.91")
    assert (
        Decimal(row["sum_top_contracts"]) == Decimal("239.16")
        and row["scorer_top_whole_contracts"] == 237
    )
    assert Decimal(row["sides"]["YES"]["bid_tick_cents"]) == 1 and row["fee_profile"] is not None
    assert w["receipts"][GAMES[0] + "-X"]["endpoint"].endswith("-X/orderbook")
    assert study.report(fx.path, begin - 1)["by_window"]["advance"]["games_by_coverage"] == {
        "pending": 2
    }
    after = study.bounds(START, "post_start")[1]
    assert study.report(fx.path, after)["by_window"]["near_start"]["games_by_coverage"] == {
        "missing": 2
    }


def test_late_receipt_partial_interrupted_and_no_backfill(fx):
    fx.enroll()
    begin, end = study.bounds(START, "near_start")
    fx.now = end - 1

    def late(_req):
        fx.now = end
        return httpx.Response(200, json=book())

    fx.override["/markets/" + GAMES[0] + "-Y/orderbook"] = late
    assert len(fx.observe()) == 1
    assert fx.observe() == []
    result = study.report(fx.path, end)
    w = result["games"][GAMES[0]]["windows"]["near_start"]
    assert w["coverage"] == "partial" and w["contracts"][GAMES[0] + "-Y"]["book_receipt_ns"] == end
    assert w["contracts"][GAMES[0] + "-Y"]["status"] == "outside_window_or_clock_reversal"
    assert result["by_window"]["near_start"]["games_by_coverage"] == {"partial": 1, "missing": 1}
    assert result["by_window"]["near_start"]["paired_games"] == 0
    fx.now = study.bounds(START, "post_start")[0]

    def interrupt(_req):
        raise KeyboardInterrupt

    fx.override["/markets/" + GAMES[0] + "-Y/orderbook"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        fx.observe()
    w = study.report(fx.path, fx.now)["games"][GAMES[0]]["windows"]["post_start"]
    assert w["attempt"] == "attempt_incomplete" and w["coverage"] == "partial"
    assert fx.observe() == [GAMES[1] + "_post_start"]


def test_bad_schedule_and_pair_never_replace_nominee(fx):
    fx.override["/events/" + GAMES[0]] = lambda _: httpx.Response(200, json={"event": {}})
    fx.override["/milestones"] = lambda _: httpx.Response(
        200, json={"milestones": [], "cursor": ""}
    )
    result = fx.enroll()
    assert set(result["nominees"]) == set(GAMES[:2])
    assert all(g["exclusion"] == "schedule_missing" for g in result["nominees"].values())
    assert result["nominees"][GAMES[0]]["contracts"] == []
    assert not study.pair(
        {
            "event_ticker": GAMES[0],
            "series_ticker": study.SERIES,
            "mutually_exclusive": True,
            "markets": [market(GAMES[0], "X")] * 2,
        },
        GAMES[0],
        START,
    )
    assert study.report(fx.path, fx.now)["distinct_games"] == 0


def test_duplicate_milestone_registry(fx):
    registry = fx.path.parent / "milestones"
    registry.mkdir()
    study.exclusive(
        registry / (study.sha(GAMES[0].encode()) + ".json"), {"slate": "prior", "game": "prior"}
    )
    assert fx.enroll()["nominees"][GAMES[0]]["exclusion"] == "milestone_already_enrolled"
    assert study.report(fx.path, fx.now)["distinct_games"] == 1


def test_enrollment_deadline(fx):
    fx.now = study.bounds(START, "advance")[0]
    assert all(
        g["exclusion"] == "advance_window_already_opened" for g in fx.enroll()["nominees"].values()
    )


def test_failed_page_preserves_shortfall(fx):
    fx.override["/markets"] = lambda _: httpx.Response(503)
    plan = fx.enroll()
    assert plan["discovery_error"] == "http_503" and not plan["nominees"]
    file = fx.path / "listing-0.json"
    obj = json.loads(file.read_bytes())
    obj["record"]["http_status"] = 200
    file.write_bytes(study.encoded(obj))
    with pytest.raises(ValueError, match="checksum"):
        study.report(fx.path, fx.now)


def test_unavailable_metadata_and_missing_side(fx):
    fx.enroll()
    fx.now = study.bounds(START, "advance")[0]
    fx.override["/series/KXNBAGAME"] = lambda _: httpx.Response(403)
    fx.override["/markets/" + GAMES[0] + "-X/orderbook"] = lambda _: httpx.Response(
        200, json={"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.5000", "0.75"]]}}
    )
    fx.observe()
    rows = study.report(fx.path, fx.now)["games"][GAMES[0]]["windows"]["advance"]["contracts"]
    assert rows[GAMES[0] + "-X"]["signal_branch"] == "missing_best_bid"
    assert rows[GAMES[0] + "-X"]["sides"]["YES"]["same_side_top_contracts"] is None
    assert rows[GAMES[0] + "-Y"]["signal_branch"] == "fee_profile_unavailable"
    assert rows[GAMES[0] + "-Y"]["fee_profile"] is None


def test_cli_public_gets_only_and_read_only_report(fx, monkeypatch, capsys):
    monkeypatch.setattr(study, "ROOT", fx.path.parent)

    def command(name, *args):
        monkeypatch.setattr(sys, "argv", ["study", name, *args])
        study.main()
        return json.loads(capsys.readouterr().out)

    transport = httpx.MockTransport(fx.serve)
    client_class = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client_class(transport=transport, **kw))
    args = ("--slate", DAY.isoformat())
    assert command("enroll", *args)["distinct_games"] == 2
    fx.now = study.bounds(START, "advance")[0]
    assert len(command("observe", *args)["attempted_windows"]) == 2
    monkeypatch.setattr(
        httpx, "Client", lambda **_kw: pytest.fail("offline report opened a client")
    )
    cutoff = ("--as-of", study.iso(fx.now))
    before = {path: path.read_bytes() for path in fx.path.parent.rglob("*") if path.is_file()}
    assert command("report", *args, *cutoff)["by_window"] == command("report", *cutoff)["by_window"]
    assert before == {
        path: path.read_bytes() for path in fx.path.parent.rglob("*") if path.is_file()
    }


def test_transport_failure_has_no_fictitious_receipt_and_sealed_deletion_detected(fx):
    fx.enroll()
    fx.now = study.bounds(START, "advance")[0]

    def fail(req):
        raise httpx.ReadTimeout("mock", request=req)

    fx.override["/markets/" + GAMES[0] + "-Y/orderbook"] = fail
    fx.observe()
    window = study.report(fx.path, fx.now)["games"][GAMES[0]]["windows"]["advance"]
    row = window["contracts"][GAMES[0] + "-Y"]
    assert row["book_receipt_ns"] is None and row["status"] == "ReadTimeout"
    assert window["coverage"] == "partial"
    (fx.path / window["receipt_directory"] / "series.json").unlink()
    with pytest.raises(ValueError, match="digest"):
        study.report(fx.path, fx.now)


def test_reschedule_retains_original_grid_and_separates_actual_state(fx):
    fx.enroll()
    fx.now = study.bounds(START, "post_start")[0]
    revised = study.iso((START + 3600000) * 10**6)

    def rescheduled(req):
        game = req.url.params["related_event_ticker"]
        return httpx.Response(
            200,
            json={
                "cursor": "",
                "milestones": [
                    {
                        "type": "basketball_game",
                        "category": "Sports",
                        "id": game,
                        "primary_event_tickers": [game],
                        "start_date": revised,
                        "details": {"status": "scheduled"},
                    }
                ],
            },
        )

    fx.override["/milestones"] = rescheduled
    assert len(fx.observe()) == 2
    result = study.report(fx.path, fx.now)["games"][GAMES[0]]
    w = result["windows"]["post_start"]
    assert result["actual_game_state"] is None and w["milestone_reported_state"] == "scheduled"
    assert result["scheduled_start_utc"] != w["milestone_start_as_reported"] == revised
    assert w["opens_utc"] == study.iso(fx.now) and w["coverage"] == "complete"


@pytest.mark.parametrize(
    ("raw", "multiplier", "branch"),
    [
        (book(), 1, "structural_gap_below_threshold"),
        (book("0.4001", "0.5400"), 1, "neither_inside_quote_constructable"),
        (book("0.40", "0.58", "0.72"), 1, "usable_quotes_below_side_liquidity"),
        (book("0.40", "0.58"), 100, "no_eligible_side_meets_net_edge"),
        (book("0.40", "0.58"), 1, "eligible_side_meets_net_edge"),
        (book("0.70", "0.45"), 1, "crossed_bid_sum"),
    ],
)
def test_exact_frozen_branch(raw, multiplier, branch):
    game = GAMES[0]
    m = market(game, "X")
    event = {"event_ticker": game, "series_ticker": study.SERIES, "markets": [m]}
    series = {
        "ticker": study.SERIES,
        "fee_type": "quadratic_with_maker_fees",
        "fee_multiplier": multiplier,
    }
    row = measurements(m["ticker"], raw, event, series, START)
    assert row["signal_branch"] == branch
    assert (
        tick_cents(
            [
                {"start": "0", "end": "0.5", "step": "0.001"},
                {"start": "0.5", "end": "1", "step": "0.01"},
            ],
            Decimal(50),
        )
        is None
    )


@pytest.mark.parametrize(
    ("run", "digest", "n", "spread"),
    [
        (
            "b803df46eb184db3a545c29d992b958a",
            "b821ccf40457ac1032f55c5dadc26a933a21380621ce605dbc630a063b57d5b8",
            64,
            12,
        ),
        (
            "5ca2f18c17e54c78b327dca9ac551514",
            "5a8dce69184a79a6a978a3f24b9f004524d9881ad27894476626907787cc180a",
            60,
            26,
        ),
    ],
)
def test_historical_active_audit_classifications(run, digest, n, spread):
    path = (
        Path.home()
        / "kalshi-paper-entry-active-research"
        / "nba-paper-entry-active-01"
        / "preflights"
        / ("b3b2-entry-active-" + run + ".json")
    )
    if not path.exists():
        pytest.skip("External Stage 2B3.2 audit unavailable")
    wire = path.read_bytes()
    assert hashlib.sha256(wire).hexdigest() == digest
    data = json.loads(wire)
    counts: Counter[str] = Counter()
    for c in data["candidates"]:
        market_body, event, series, b = [
            json.loads(base64.b64decode(data["receipts"][i - 1]["body_b64"]))
            for i in c["source_receipts"]
        ]
        row = measurements(
            c["ticker"],
            b,
            dict(event["event"], markets=[market_body["market"]]),
            series["series"],
            c["scored_ns"] // 10**6,
        )
        assert row["signal_reason"] == c["signal_reason"]
        counts[row["signal_branch"]] += 1
    assert sum(counts.values()) == n and counts["structural_gap_below_threshold"] == spread
