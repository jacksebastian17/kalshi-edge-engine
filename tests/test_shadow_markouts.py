import hashlib
import json
import shutil
import sys
from decimal import Decimal

import pytest

from tests.test_shadow_feasibility import A, B, Case, yes_episode
from tools.markout_fees import FeeEvidence
from tools.microstructure import journal
from tools.research_campaign import aggregate, main
from tools.shadow_feasibility import analyze
from tools.shadow_markouts import SECOND
from tools.shadow_tape import source_hashes


def test_decision_horizons_asof_ties_and_return_after_departure(tmp_path):
    case = Case()
    base = case.delta(60_000, 3, "1")
    departed = case.delta(62_000, 4, "2", price="0.4525")
    restored = case.delta(65_000, 5, "-2", price="0.4525")
    # Deeper receipt at the exact same monotonic time leaves the restored top unchanged.
    last = case.delta(65_000, 6, "2", price="0.4312")
    next_book = case.delta(65_001, 7, "1", price="0.4599")
    case.trade(74_999, 1, "1")
    case.snapshot(75_000, 8, B)
    final = case.delta(88_000, 9, "1", price="0.4312")
    _, rows = analyze(case.seal(tmp_path, end=90_000), markouts=True)
    episode = yes_episode(rows)
    five, fifteen, thirty, sixty = episode["markouts"]
    assert episode["decision_asof_record"] == episode["markout_baseline"]["depth_record"] == base
    assert [m["target_mono_ns"] for m in episode["markouts"]] == [
        n * SECOND for n in (65, 75, 90, 120)
    ]
    assert five["source"] == {
        "record": last,
        "seq": 6,
        "sid": 1,
        "receipt_mono_ns": 65 * SECOND,
        "snapshot_record": 5,
        "top_state_record": restored,
    }
    assert five["asof_record"] == last and five["first_departure_record"] == departed
    assert five["status"] == "fresh" and five["age_ns"] == 0 and five["updated_since_decision"]
    assert (
        five["after_quote_departure"] and five["source_relative_to_departure"] == "after_departure"
    )
    assert five["same_top_as_decision"] and five["original_quote_available"]
    assert five["midpoint_cents"] == Decimal("46.125") and five["executable_bid_change_cents"] == 0
    assert fifteen["source"]["record"] == next_book and fifteen["age_ns"] == 9_999_000_000
    assert fifteen["status"] == "stale" and fifteen["gross_markout_cents"] == Decimal("1.74")
    assert (
        thirty["source"]["record"] == final
        and thirty["age_ns"] == 2 * SECOND
        and not thirty["stale"]
    )
    assert (
        sixty["status"] == "capture_ended"
        and sixty["source"] is sixty["gross_markout_cents"] is None
    )
    # A later snapshot for B does not modify earlier decisions, or refresh A.
    b_five = next(r for r in rows if r["market"] == B)["markouts"][0]
    assert b_five["source"]["record"] == 6 and b_five["updated_since_decision"] is False


def test_whole_contract_deeper_bid_one_sided_and_insufficient_depth(tmp_path):
    case = Case()
    case.delta(60_000, 3, "-10")  # Displayed 0.25 at 44.25 cents.
    case.delta(60_000, 4, "1.00000001", price="0.4187")
    lower = case.delta(65_000, 5, "-0.00000002", price="0.4187")
    case.delta(75_000, 6, "-0.25")
    case.delta(75_000, 7, "-0.99999999", price="0.4187")
    case.delta(90_000, 8, "1", price="0.3951")
    _, rows = analyze(case.seal(tmp_path), markouts=True)
    yes = yes_episode(rows)
    assert yes["markout_baseline"]["executable_bid_cents"] == Decimal("41.87")
    five, fifteen, thirty, sixty = yes["markouts"]
    assert five["source"]["record"] == lower and five["displayed_bid_contracts"] == Decimal("0.25")
    assert (
        five["sufficient_displayed_quantity"] is False and five["gross_liquidation_cents"] is None
    )
    assert five["midpoint_cents"] == Decimal("46.125")
    assert fifteen["displayed_bid_cents"] is fifteen["midpoint_cents"] is None
    no = next(r for r in rows if r["side"] == "no" and r["market"] == A)["markouts"][1]
    assert no["gross_liquidation_cents"] == 52 and no["two_sided_midpoint_available"] is False
    assert thirty["gross_liquidation_cents"] == Decimal("39.51")
    assert thirty["executable_bid_change_cents"] == Decimal("-2.36")
    assert thirty["gross_markout_cents"] == Decimal("-4.74") and thirty["net_markout_cents"] is None
    assert sixty["status"] == "stale" and not sixty["updated_since_previous_horizon"]


def test_exposure_grouping_leakage_duplicate_and_correlated_games(tmp_path):
    case = Case()
    case.delta(60_000, 3, "1")
    unchanged = Case()
    unchanged.delta(60_000, 3, "1")
    case.trade(60_200, 1, "12.25")
    case.trade(60_300, 2, "0.25", ident="later", stamp=101)
    case.delta(65_000, 4, "-11.25")
    case.delta(65_000, 5, "1", price="0.4311")
    archive = case.seal(tmp_path / "prints")
    reference, _ = aggregate([unchanged.seal(tmp_path / "quiet")], markouts=True)
    out, rows = aggregate([archive, archive], markouts=True)
    legacy, legacy_rows = aggregate([archive])
    game = "KXNBAGAME-TEST"
    chosen = out["games"][game]["primary"]
    assert chosen == legacy["games"][game]["primary"] == reference["games"][game]["primary"]
    assert chosen["side"] == "yes"
    assert [(r["decision_asof_record"], r["role"]) for r in rows] == [
        (r["decision_asof_record"], r["role"]) for r in legacy_rows
    ]
    assert out["duplicate_inputs"] == out["primary_episodes"] == out["selected_games"] == 1
    assert (
        out["coverage_gates"] == legacy["coverage_gates"]
        and out["coverage_gates"]["on_time_5s_marks"]["met"] is None
    )
    diag = out["tape_markouts"]
    assert diag["tape_anchored_5s_primary"] == {
        "fresh_book": 1,
        "fresh_one_contract_bid": 1,
        "primary_decisions": 1,
        "selected_games": 1,
    }
    primary = [s for s in diag["scenario_strata"] if s["role"] == "primary"]
    assert len(primary) == 4
    stratum = {s["delay_ms"]: s for s in primary}
    for delay, queue in [
        (0, "PRINTS_REACH_DISPLAYED_AHEAD_PLUS_ONE"),
        (250, "PRINTS_BELOW_DISPLAYED_AHEAD_PLUS_ONE"),
        (500, "NO_COMPATIBLE_PRINTS"),
    ]:
        s = stratum[delay]
        assert s["queue_comparison"] == queue and s["horizons"]["5"]["negative"] == 1
    assert stratum[500]["classification"] == "NO_OBSERVED_EXECUTION_SUPPORT"
    secondary = diag["coverage_by_role_and_horizon_seconds"]["secondary"]["60"]
    assert secondary["requested"] == 7 and secondary["missing_book"] == 4
    assert all(
        sum(
            s["horizons"]["5"]["requested"]
            for s in diag["scenario_strata"]
            if s["role"] == "secondary" and s["delay_ms"] == delay
        )
        == 7
        for delay in (0, 100, 250, 500)
    )


def test_gap_and_suspect_history_are_unscored(tmp_path):
    case = Case()
    case.delta(65_000, 3, "1")
    fault = case.delta(65_001, 5, "1")
    _, rows = analyze(case.seal(tmp_path), markouts=True)
    marks = yes_episode(rows)["markouts"]
    assert marks[0]["status"] == "fresh"
    assert marks[1]["status"] == "interrupted" and marks[1]["interruption_record"] == fault
    assert marks[1]["after_quote_departure"] is marks[1]["gross_markout_cents"] is None
    conflict = Case()
    conflict.trade(61_000, 1, "1")
    conflict.trade(62_000, 2, "2")
    _, rows = analyze(conflict.seal(tmp_path / "conflict"), markouts=True)
    assert yes_episode(rows)["markouts"][0]["status"] in ("conflicting_source", "interrupted")


def test_ambiguous_receipts_and_duplicate_updates(tmp_path):
    case = Case()
    decrease = case.delta(61_000, 3, "-0.50")
    case.trade(61_001, 1, "0.25", "a")
    case.trade(61_002, 2, "0.25", "b")
    case.delta(65_000, 3, "-0.50")  # An identical retransmission cannot refresh the book.
    out, rows = aggregate([case.seal(tmp_path)], markouts=True)
    episode = yes_episode(rows)
    assert episode["markouts"][0]["source"]["record"] == decrease
    assert episode["markouts"][0]["stale"] and episode["scenarios"][0][
        "ambiguous_group_records"
    ] == [decrease]
    zero = next(
        s
        for s in out["tape_markouts"]["scenario_strata"]
        if s["role"] == "primary" and s["delay_ms"] == 0
    )
    assert zero["ambiguous_attribution"] and zero["horizons"]["5"]["stale_book"] == 1


def test_fee_receipt_provenance_precision_and_expiry(tmp_path):
    case = Case()
    case.delta(65_000, 3, "1", price="0.4526")
    archive = case.seal(tmp_path)
    digest = journal.digest(archive / journal.LOG)
    event = "KXNBAGAME-TEST"
    payloads = [
        ("/markets/" + A, {"market": {"ticker": A, "event_ticker": event}}),
        (
            "/events/" + event,
            {
                "event": {
                    "event_ticker": event,
                    "series_ticker": "KXNBAGAME",
                    "fee_multiplier_override": "1.25",
                }
            },
        ),
        (
            "/series/KXNBAGAME",
            {
                "series": {
                    "ticker": "KXNBAGAME",
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": "2",
                }
            },
        ),
    ]
    receipts = [
        {
            "journal_sha256": digest,
            "path": p,
            "receipt_mono_ns": 10 * SECOND,
            "receipt_wall_ns": case.wall_base + 10 * SECOND,
            "response": body,
        }
        for p, body in payloads
    ]
    receipts.append(
        {
            **receipts[0],
            "receipt_mono_ns": 65 * SECOND + 1,
            "response": {
                "market": {**payloads[0][1]["market"], "fee_waiver_expiration_time": "unknown"}
            },
        }
    )
    path = tmp_path / "fee.json"
    path.write_text(json.dumps({"format": "b3b2-local-get-receipts-v1", "receipts": receipts}))
    with pytest.raises(ValueError, match="SHA-256"):
        FeeEvidence.read(path, "0" * 64)
    fees = FeeEvidence.read(path, journal.digest(path))
    assert fees.resolve(digest, A, event, 10 * SECOND - 1) is None
    assert fees.resolve(digest, A, event, 65 * SECOND + 1) is None
    assert fees.resolve("f" * 64, A, event, 60 * SECOND) is None
    assert fees.resolve(digest, B, event, 60 * SECOND) is None
    assert fees.resolve(digest, A, event, 70 * SECOND) is None
    out, rows = aggregate([archive], markouts=True, fees=fees)
    yes = yes_episode(rows)
    assert out["tape_markouts"]["fee_receipts_sha256"] == journal.digest(path)
    entry = yes["markout_baseline"]["maker_fee"]
    assert entry["receipt_indices"] == [0, 1, 2] and entry["cents"] == Decimal("0.54")
    five = yes["markouts"][0]
    assert five["taker_fee"]["cents"] == Decimal("2.17")
    assert five["net_liquidation_cents"] == Decimal("43.09")
    assert five["net_markout_cents"] == Decimal("-1.70") and five["fee_status"] == "verified"
    assert yes["markouts"][1]["fee_status"] == "missing_exit_profile"


def test_missing_roster_and_exclusions_are_visible(tmp_path):
    case = Case()
    case.delta(59_000, 3, "-10.25")
    archive = case.seal(tmp_path)
    roster = tmp_path / "roster"
    roster.mkdir()
    shutil.copyfile(archive / journal.MANIFEST, roster / "one.json")
    out, rows = aggregate([], roster=roster, markouts=True)
    assert rows == [] and out["tape_markouts"]["tape_anchored_5s_primary"]["selected_games"] == 1
    assert out["tape_markouts"]["tape_anchored_5s_primary"]["primary_decisions"] == 0
    out, rows = aggregate([archive], roster=roster, markouts=True)
    assert out["slot_exclusions"] == {"no_passive_two_sided_quote": 4}
    assert all("markouts" not in r for r in rows if r["role"] == "excluded")


def test_offline_cli_is_repeatable_and_preserves_inputs(tmp_path, monkeypatch, capsys):
    case = Case()
    case.delta(65_000, 3, "1", price="0.5525")  # 55.25 + 52 is crossed.
    archive = case.seal(tmp_path / "source")
    before = source_hashes(archive)
    first, second = tmp_path / "first", tmp_path / "second"
    for output in (first, second):
        monkeypatch.setattr(
            sys, "argv", ["campaign", "--markouts", "--output", str(output), str(archive)]
        )
        main()
        assert json.loads(capsys.readouterr().out) == json.loads(
            (output / "summary.json").read_bytes()
        )
    assert source_hashes(archive) == before
    for name in ("episodes.jsonl", "summary.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    rows = [json.loads(line) for line in (first / "episodes.jsonl").read_bytes().splitlines()]
    five = yes_episode(rows)["markouts"][0]
    assert five["crossed_book"] and five["sufficient_displayed_quantity"]
    assert five["midpoint_cents"] is five["gross_liquidation_cents"] is None
    assert five["source_relative_to_departure"] == "at_departure"
    with pytest.raises(SystemExit):
        main()  # Existing report cannot be overwritten.


def test_stage11_verified_report_reproduces_archived_output_if_present():
    path = journal.ROOT / "b3b2-stage11-66884b73738e499a979ed8cb962719bc"
    if not path.is_dir():
        pytest.skip("Historical November 22 archive is external")
    expected = (path / "verified-coverage.json").read_bytes()
    assert (
        hashlib.sha256(expected).hexdigest()
        == "a6d72ac5b8df444b0fb3b1f484f7cfd5e7b092045ee83a808daf94954683c4dc"
    )
    report = journal.inspect(path / "backup")
    assert (json.dumps(report, indent=2, sort_keys=True) + "\n").replace("\n", "\r\n").encode(
        "utf-8-sig"
    ) == expected
