import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from tests.test_shadow_feasibility import A, Case
from tools.microstructure import journal
from tools.paper_bridge import ENROLLMENT, EVENTS, PROTOCOL, SEAL
from tools.paper_link import enroll
from tools.paper_link_report import booking_support, campaign_report, decisions
from tools.research_campaign import aggregate
from tools.shadow_feasibility import encode
from tools.shadow_markouts import SECOND
from tools.shadow_tape import FILES, project, verified_source

C = "KXNBAGAME-OTHER-BOS"


def fixture_session(root, name="b3b2-mock", *, prints=True, gap=False, clock=True, enrolled=1):
    case = Case(tickers=(A, C))
    case.delta(59_000, 3, "1")
    if prints:
        case.trade(60_080, 1, "1")
    case.delta(64_999, 5 if gap else 4, "-11.25")
    if not gap:
        case.delta(65_000, 5, "2", price="0.4200")
    tape = root / "tapes" / name
    backup = case.seal(root / "wire" / name, end=150_000, session_id=name)
    meta = journal.manifest(backup)
    games = {t: t.rpartition("-")[0] for t in meta["markets"]}
    meta["selection"] = {"selected": games}
    (backup / journal.MANIFEST).write_bytes(journal.encoded(meta))
    ws_seal = json.loads((backup / journal.SEAL).read_bytes())
    ws_seal["manifest_sha256"] = journal.digest(backup / journal.MANIFEST)
    (backup / journal.SEAL).write_bytes(journal.encoded(ws_seal))
    (backup / journal.BACKUP_HASHES).write_bytes(
        journal.encoded(
            {n: journal.digest(backup / n) for n in FILES if n != journal.BACKUP_HASHES}
        )
    )
    tape.mkdir(parents=True)
    shutil.copyfile(backup / journal.MANIFEST, tape / journal.MANIFEST)
    directory = enroll(root / "campaign", tape)
    with pytest.raises(FileExistsError):
        enroll(root / "campaign", tape)
    shutil.copytree(backup, tape / "backup")
    with pytest.raises(ValueError, match="before capture"):
        enroll(root / "campaign", tape)
    meta = json.loads((directory / ENROLLMENT).read_bytes())
    meta["enrolled_wall_ns"] = enrolled
    (directory / ENROLLMENT).write_bytes(journal.encoded(meta))
    frozen = journal.digest(directory / ENROLLMENT)
    events = []

    def event(kind, mono, **items):
        events.append(
            dict(
                items,
                kind=kind,
                n=len(events) + 1,
                session=meta["paper_session"],
                pid=100,
                origin="producer",
                mono_ns=mono,
                wall_ns=10**18 + mono,
            )
        )

    event("opened", 0, enrollment_sha256=frozen, clock="time.monotonic_ns")
    event("paper_ready", 1, rest_session="fixture-session")
    for i, (ticker, side, price, at) in enumerate(
        (
            (A, "yes", "44.25", 60 * SECOND),
            (A, "no", "52", 60 * SECOND),
            (A, "yes", "45.25", 61 * SECOND),
            (A, "yes", "41.00", 61 * SECOND),
            ("UNSUBSCRIBED", "yes", "30", 61 * SECOND),
        )
    ):
        event(
            "proposal",
            at,
            intent_id=str(i),
            ticker=ticker,
            side=side,
            price_cents=price,
            contracts=1,
            direction="buy",
            decision_mono_ns=at,
            decision_wall_ns=10**18 + at,
            source_receipts=[],
        )
        event("reject", at, intent_id=str(i), reason="side_disabled")
    event(
        "tape_returned",
        151 * SECOND,
        began=0,
        ended=151 * SECOND,
        reason="duration",
        messages_sha256=journal.digest(backup / journal.LOG),
        manifest_sha256=meta["manifest_sha256"],
        tape_session=name,
    )
    if not clock:
        events[-1]["pid"] += 1
    (directory / EVENTS).write_bytes(b"".join(journal.encoded(r) for r in events))
    journal.exclusive(
        directory / SEAL,
        {
            "protocol": PROTOCOL,
            "enrollment_sha256": frozen,
            "events_sha256": journal.digest(directory / EVENTS),
            "records": len(events),
            "paper_backup": "backup-missing",
            "paper_hashes": {},
        },
    )
    return directory, events


def test_decision_arrival_and_print_anchors_with_adverse_marks_and_zero_prints(tmp_path):
    _, _ = fixture_session(tmp_path)
    summary, rows = campaign_report(tmp_path / "campaign", tmp_path / "tapes")
    assert (summary, rows) == campaign_report(tmp_path / "campaign", tmp_path / "tapes")
    first = rows[0]
    assert first["role"] == "primary" and first["admission"] == "rejected"
    assert first["decision_marks"]["horizons"][0]["gross_markout_cents"] == Decimal("-2.25")
    assert first["decision_marks"]["horizons"][0]["net_markout_cents"] is None
    assert (
        first["predecision_30s"]["status"] == "quiet"
        and rows[2]["predecision_30s"]["status"] == "active"
    )
    zero, hundred, *_ = first["scenarios"]
    assert zero["displayed_at_price"] == Decimal("11.25") and zero["public_execution_support"]
    assert zero["evidence"]["print_contracts"] == 1 and not zero["prints_reach_ahead_plus_size"]
    assert (
        first["entry_marks_by_anchor"][zero["print_supported_entry_marks"]]["anchor_mono_ns"]
        == 60_080_000_000
    )
    assert (
        first["entry_marks_by_anchor"][hundred["hypothetical_entry_marks"]]["anchor_mono_ns"]
        == 60_100_000_000
    )
    assert (
        hundred["public_execution_support"] is False
        and hundred["print_supported_entry_marks"] is None
    )
    inside = rows[2]["scenarios"][0]
    assert inside["price_location"] == "inside_spread" and inside["displayed_at_price"] == 0
    behind = rows[3]["scenarios"][0]
    assert behind["price_location"] == "behind_best" and behind[
        "displayed_better_priced"
    ] == Decimal("11.25")
    assert rows[4]["link_status"] == "market_outside_subscription"
    assert summary["decision_mark_coverage"]["5"] == {"fresh": 4, "unlinked": 1}
    supported = summary["entry_mark_coverage_by_delay_ms"]["0"]["print_supported_entry_marks"]["5"]
    assert supported == {"fresh": 1, "no_anchor": 4}
    assert (
        summary["primary_games"] == {"observed": 1, "minimum": 100, "met": False}
        and summary["primary_dates_utc"]["minimum"] == 10
    )


def test_repeated_games_no_lookahead_unverified_clock_and_interruption(tmp_path):
    first, events = fixture_session(tmp_path, "b3b2-first", prints=False, gap=True)
    fixture_session(tmp_path, "b3b2-second", clock=False, enrolled=2)
    out, rows = campaign_report(tmp_path / "campaign", tmp_path / "tapes")
    assert len(out["repeated_games"]) == 2
    assert (
        rows[0]["role"] == "primary"
        and rows[0]["decision_marks"]["horizons"][0]["status"] == "interrupted"
    )
    assert rows[0]["scenarios"][0]["public_execution_support"] is None
    assert rows[5]["role"] == "secondary" and rows[5]["link_status"] == "unverified_clock"
    with pytest.raises(ValueError, match="Duplicate"):
        decisions([*events, events[2]], json.loads((first / ENROLLMENT).read_bytes()))
    (first / SEAL).unlink()
    with (first / EVENTS).open("ab") as f:
        f.write(b'{"partial"')
    out, rows = campaign_report(tmp_path / "campaign", tmp_path / "tapes")
    assert rows[0]["link_status"] == "unsealed_or_missing_sidecar" and out["proposals"] == 10
    assert not out["sessions"][rows[0]["session"]]["sealed"]


def test_missing_tape_and_booking_before_the_first_print(tmp_path):
    fixture_session(tmp_path)
    out, rows = campaign_report(tmp_path / "campaign", tmp_path / "tapes")
    first = rows[0]
    tape_path = tmp_path / "tapes" / "b3b2-mock" / "backup"
    tape = project(tape_path / journal.LOG, verified_source(tape_path)[1], with_depth=True)
    first["paper_ledger"] = {
        "entry_fills": [
            {"booking_mono_ns": 60_050_000_000, "fill_price_cents": 44.25},
            {"booking_mono_ns": 60_100_000_000, "fill_price_cents": 44.25},
        ]
    }
    booking_support(first, tape)
    a, b = first["paper_ledger"]["entry_fills"]
    assert a["public_support_through_local_booking"]["0"] == "no_print_by_booking"
    assert b["public_support_through_local_booking"]["0"] == "print_by_booking"
    out, rows = campaign_report(tmp_path / "campaign", tmp_path / "absent")
    assert out["proposals"] == 5 and rows[0]["link_status"] == "missing_tape_backup"


@pytest.mark.parametrize(
    "marks, summary_hash, episode_hash",
    [
        (
            False,
            "7288c6ea9105585b7539ea94ae1e650c5f93a3bfb63c12b59d1d5cc8bf293a5c",
            "8c249630e0d8b553d94e86c0388724301ada546f1e1e17e8b36da93ce80b32cb",
        ),
        (
            True,
            "d034aaf1f823f839323d896755878e5e8214d5a4d0a4c53e1b2359bed8f593c5",
            "9c52b33b5c3ec19aa026a5148104aeadd57ea0c182a170afad1c43823aefb630",
        ),
    ],
)
def test_historical_campaign_and_markout_bytes_if_available(
    tmp_path, marks, summary_hash, episode_hash
):
    root = Path.home() / "kalshi-shadow-research" / "nba-campaign-01"
    manifest = root / "preflights" / "b3b2-diverse-b8197c5dc26e456abd840034d4ab3f2c.json"
    if not manifest.is_file():
        pytest.skip("Historical four-game campaign is external")
    shutil.copyfile(manifest, tmp_path / manifest.name)
    summary, rows = aggregate(
        [journal.ROOT / manifest.stem / "backup"], roster=tmp_path, markouts=marks
    )
    for data, expected in (
        ([summary], summary_hash),
        (rows, episode_hash),
    ):
        assert (
            hashlib.sha256("".join(encode(r) + "\n" for r in data).encode()).hexdigest() == expected
        )
