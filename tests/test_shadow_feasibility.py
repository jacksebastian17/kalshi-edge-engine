import base64
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tools.microstructure import journal
from tools.shadow_feasibility import analyze, encode, main
from tools.shadow_tape import FILES, source_hashes

A, B = "KXNBAGAME-TEST-ATL", "KXNBAGAME-TEST-DET"


class Case:
    """A miniature sealed tape on a millisecond local receipt clock."""

    def __init__(self, *, wall_base=10**18, tickers=(A, B)):
        self.rows = []
        self.wall_base = wall_base
        self.tickers = tickers
        self.add(0, "started", {"duration_seconds": 121})
        self.add(1, "connected", 1)
        for sid, channel in enumerate(("orderbook_delta", "trade"), 1):
            self.raw(2, {"type": "subscribed", "id": sid, "msg": {"sid": sid, "channel": channel}})
        self.snapshot(3, 1, tickers[0])
        self.snapshot(4, 2, tickers[1])

    def add(self, ms, kind, value):
        n = len(self.rows) + 1
        assert not self.rows or ms * 1_000_000 >= self.rows[-1][2]
        self.rows.append([n, self.wall_base + ms * 1_000_000, ms * 1_000_000, kind, value])
        return n

    def raw(self, ms, packet):
        return self.add(ms, "raw", base64.b64encode(journal.encoded(packet)).decode())

    def snapshot(self, ms, seq, ticker=A):
        return self.raw(
            ms,
            {
                "type": "orderbook_snapshot",
                "sid": 1,
                "seq": seq,
                "msg": {
                    "market_ticker": ticker,
                    "clock": seq,
                    "yes_dollars_fp": [["0.4425", "10.25"]],
                    "no_dollars_fp": [["0.52", "12.00"]],
                },
            },
        )

    def delta(self, ms, seq, qty, stamp=100, price="0.4425"):
        return self.raw(
            ms,
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": seq,
                "msg": {
                    "market_ticker": A,
                    "clock": seq,
                    "ts_ms": stamp,
                    "price_dollars": price,
                    "side": "yes",
                    "delta_fp": qty,
                },
            },
        )

    def trade(self, ms, seq, qty, ident="one", stamp=100, **fields):
        return self.raw(
            ms,
            {
                "type": "trade",
                "sid": 2,
                "seq": seq,
                "msg": {
                    "market_ticker": A,
                    "trade_id": ident,
                    "ts_ms": stamp,
                    "taker_side": "no",
                    "taker_outcome_side": "no",
                    "taker_book_side": "ask",
                    "is_block_trade": False,
                    "yes_price_dollars": "0.4425",
                    "no_price_dollars": "0.5575",
                    "count_fp": qty,
                    **fields,
                },
            },
        )

    def seal(self, parent, end=121_000, *, bad_build=False, session_id="fixture"):
        dest = parent / "backup"
        dest.mkdir(parents=True)
        self.add(end, "collection_ended", "duration")
        self.add(end, "closed", "duration")
        modules = {
            p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for p in Path(journal.__file__).parent.glob("*.py")
        }
        build = {"python": "3.13.14", "websockets": "16.0", "module_sha256": modules}
        journal.exclusive(
            dest / journal.MANIFEST,
            {
                "format": journal.FORMAT,
                "session": session_id,
                "prepared_ns": self.wall_base - 1,
                "build": {} if bad_build else build,
                "markets": self.tickers,
                "market_checks": [
                    {"ticker": t, "event_ticker": t.rpartition("-")[0]} for t in self.tickers
                ],
            },
        )
        data = b"".join(journal.encoded(row) for row in self.rows)
        (dest / journal.LOG).write_bytes(data)
        journal.exclusive(
            dest / journal.HEALTH, {"durable_records": len(self.rows), "durable_bytes": len(data)}
        )
        journal.exclusive(
            dest / journal.SEAL,
            {
                "format": journal.FORMAT,
                "records": len(self.rows),
                "reason": "duration",
                "messages_sha256": journal.digest(dest / journal.LOG),
                "manifest_sha256": journal.digest(dest / journal.MANIFEST),
            },
        )
        journal.exclusive(
            dest / journal.BACKUP_HASHES,
            {f: journal.digest(dest / f) for f in FILES if f != journal.BACKUP_HASHES},
        )
        return dest


def yes_episode(rows):
    return next(e for e in rows if e["market"] == A and e["side"] == "yes")


def test_cohort_is_frozen_before_future_events_and_partial_tail_is_retained(tmp_path):
    empty = Case()
    empty.delta(60_000, 3, "1.00")
    changed = Case()
    changed.delta(60_000, 3, "1.00")
    changed.delta(60_001, 4, "99", price="0.4525")
    changed.trade(61_000, 1, "10")
    first = analyze(empty.seal(tmp_path / "first"))[1]
    summary, second = analyze(changed.seal(tmp_path / "second"))
    for a, b in zip(first[:4], second[:4], strict=True):
        assert {k: v for k, v in a.items() if k != "scenarios"} == {
            k: v for k, v in b.items() if k != "scenarios"
        }
    episode = yes_episode(second)
    assert episode["displayed_contracts"] == Decimal("11.25")
    assert episode["decision_asof_record"] == 7 and episode["snapshot_record"] == 5
    assert episode["scenarios"][0]["exit"] == "quote_moved"
    assert all(
        s["classification"] == "QUOTE_INELIGIBLE_AT_ARRIVAL" for s in episode["scenarios"][1:]
    )
    assert summary["eligible_episodes"] == 8 and summary["underlying_events"] == 1
    assert all(e["scenarios"][0]["classification"] == "DATA_INCOMPLETE" for e in second[4:])


def test_removals_replenishment_and_small_unique_print_do_not_clear_displayed_ahead(tmp_path):
    case = Case()
    case.delta(61_000, 3, "5.00")
    removal = case.delta(62_000, 4, "-12.00")
    matched = case.delta(63_000, 5, "-0.25", stamp=101)
    trade = case.trade(63_001, 1, "0.25", stamp=101)
    case.trade(63_002, 2, "0.25", stamp=101)  # repeated ID on a later trade sequence
    case.trade(63_003, 3, "20", "block", is_block_trade=True)
    case.trade(
        63_004,
        4,
        "21",
        "other-side",
        taker_side="yes",
        taker_outcome_side="yes",
        taker_book_side="bid",
    )
    report, episodes = analyze(case.seal(tmp_path))
    scenario = yes_episode(episodes)["scenarios"][0]
    assert scenario["print_receipts"] == [
        {"id": "one", "quantity": Decimal("0.25"), "record": trade}
    ]
    assert scenario["decrease_records"] == [removal, matched]
    assert scenario["exact_pairs"] == [{"decrease": matched, "print": trade}]
    assert scenario["addition_contracts"] == 5 and scenario["removal_contracts"] == Decimal("12.25")
    assert scenario["decreases_without_exact_in_window"] == 1
    assert scenario["classification"] == "PUBLIC_EXECUTION_AT_PRICE"
    assert scenario["queue_comparison"] == "PRINTS_BELOW_DISPLAYED_AHEAD_PLUS_ONE"
    assert report["sensitivity_by_delay_ms"]["0"]["removal_only_threshold"] == 1


def test_delays_reprice_ahead_exclude_equal_time_and_do_not_borrow_exact_peer(tmp_path):
    case = Case()
    pair = case.delta(60_050, 3, "-1.00")
    print_ref = case.trade(60_150, 1, "1.00")
    case.trade(60_250, 2, "10.25", "later", stamp=101)
    case.delta(60_500, 4, "0.50", price="0.4525")
    _, episodes = analyze(case.seal(tmp_path))
    zero, hundred, quarter, half = yes_episode(episodes)["scenarios"]
    assert zero["print_contracts"] == Decimal("11.25")
    assert zero["queue_comparison"] == "PRINTS_REACH_DISPLAYED_AHEAD_PLUS_ONE"
    assert hundred["arrival_ahead_contracts"] == Decimal("9.25")
    assert hundred["exact_pairs"] == [] and hundred["boundary_split_pairs"] == [
        {"inside": print_ref, "outside": pair}
    ]
    assert quarter["print_contracts"] == 0 and quarter["exit"] == "quote_moved"
    assert half["classification"] == "QUOTE_INELIGIBLE_AT_ARRIVAL"


def test_ambiguous_many_to_one_evidence_keeps_prints_without_attributing_decrease(tmp_path):
    case = Case()
    decrease = case.delta(61_000, 3, "-0.50")
    case.trade(61_001, 1, "0.25", "a")
    case.trade(61_002, 2, "0.25", "b")
    summary, rows = analyze(case.seal(tmp_path))
    s = yes_episode(rows)["scenarios"][0]
    assert s["print_contracts"] == Decimal("0.50")
    assert not s["exact_pairs"] and s["ambiguous_group_records"] == [decrease]
    assert summary["full_capture_flow"]["ambiguous_receipts"] == 3


def test_gap_stops_open_window_and_snapshot_recovery_cannot_reopen_it(tmp_path):
    case = Case()
    case.trade(61_000, 1, "0.50")
    gap = case.delta(61_010, 4, "-2")
    case.snapshot(61_020, 5)
    case.trade(61_030, 2, "8", "following")
    summary, episodes = analyze(case.seal(tmp_path))
    first = yes_episode(episodes)["scenarios"][0]
    assert first["exit_record"] == gap and first["exit"] == "feed_interruption"
    assert first["classification"] == "DATA_INCOMPLETE" and first["print_contracts"] == Decimal(
        "0.50"
    )
    assert summary["source"]["sequence_gaps"] == 1
    assert summary["excluded_opportunities"]  # B lacks its own recovery snapshot.


@pytest.mark.parametrize("bad", ["build", "journal", "checkpoint", "inventory", "empty_seal"])
def test_integrity_failure_prevents_output(tmp_path, monkeypatch, bad):
    archive = Case().seal(tmp_path / "run", bad_build=bad == "build")
    if bad == "journal":
        with (archive / journal.LOG).open("ab") as f:
            f.write(b"\n")
    if bad == "checkpoint":
        (archive / journal.HEALTH).write_text('{"durable_records":0,"durable_bytes":0}\n')
        hashes = json.loads((archive / journal.BACKUP_HASHES).read_bytes())
        hashes[journal.HEALTH] = journal.digest(archive / journal.HEALTH)
        (archive / journal.BACKUP_HASHES).write_bytes(journal.encoded(hashes))
    if bad == "inventory":
        (archive / journal.BACKUP_HASHES).write_text("{}")
    if bad == "empty_seal":
        (archive / journal.SEAL).write_bytes(b"null\n")
        hashes = json.loads((archive / journal.BACKUP_HASHES).read_bytes())
        hashes[journal.SEAL] = journal.digest(archive / journal.SEAL)
        (archive / journal.BACKUP_HASHES).write_bytes(journal.encoded(hashes))
    output = tmp_path / "result"
    monkeypatch.setattr(sys, "argv", ["shadow", "--archive", str(archive), "--output", str(output)])
    with pytest.raises(ValueError):
        main()
    assert not output.exists()


def test_read_only_deterministic_cli_and_archive_output_guard(tmp_path, monkeypatch, capsys):
    case = Case()
    case.delta(61_000, 3, "-10.25")  # An unidentified removal can empty the best with no print.
    archive = case.seal(tmp_path / "run")
    before = source_hashes(archive)
    bundles = []
    for name in ("first", "second"):
        output = tmp_path / name
        monkeypatch.setattr(
            sys, "argv", ["shadow", "--archive", str(archive), "--output", str(output), "--detail"]
        )
        main()
        compact = (output / "summary.json").read_bytes()
        assert capsys.readouterr().out.strip() == compact.decode().strip()
        bundles.append((compact, (output / "episodes.jsonl").read_bytes()))
    assert bundles[0] == bundles[1]
    summary = json.loads(bundles[0][0])
    assert (
        before
        == summary["source"]["before_sha256"]
        == summary["source"]["after_sha256"]
        == source_hashes(archive)
    )
    row = yes_episode([json.loads(s) for s in bundles[0][1].splitlines()])
    assert row["scenarios"][0]["classification"] == "NO_OBSERVED_EXECUTION_SUPPORT"
    assert encode(row["price_cents"]) == '"44.2500"'
    monkeypatch.setattr(
        sys, "argv", ["shadow", "--archive", str(archive), "--output", str(archive / "new")]
    )
    with pytest.raises(SystemExit):
        main()
    assert not (archive / "new").exists()
