import asyncio
import base64
import hashlib
import json
from collections import Counter
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import InvalidStatus

from tools.microstructure import journal, pilot
from tools.microstructure.feed import DEPTH, TRADE, Feed
from tools.microstructure.journal import Writer, backup, exclusive, health, inspect
from tools.microstructure.timing import Timing

A = "KXNBAGAME-26NOV22DETATL-ATL"
B = "KXNBAGAME-26NOV22OKCMIA-OKC"
MARKETS = [A, B]
FIXTURE = Path(__file__).parent / "fixtures" / "b3b2_public_frames.jsonl"


def packet(kind, sid, seq, **msg):
    return json.dumps({"type": kind, "sid": sid, "seq": seq, "msg": msg}).encode()


def snapshot(seq, ticker=A):
    return packet(
        "orderbook_snapshot",
        1,
        seq,
        market_ticker=ticker,
        clock=0,
        yes_dollars_fp=[["0.4625", "10.25"]],
        no_dollars_fp=[["0.5100", "7.05"]],
    )


def delta(seq, quantity="-0.24", ticker=A, **kw):
    return packet(
        DEPTH,
        1,
        seq,
        market_ticker=ticker,
        clock=seq,
        price_dollars="0.4625",
        delta_fp=quantity,
        side="yes",
        ts_ms=1000,
        **kw,
    )


def trade(seq, qty="0.24", trade_id="trade-one", block=False):
    return packet(
        TRADE,
        2,
        seq,
        market_ticker=A,
        trade_id=trade_id,
        ts_ms=1000,
        taker_side="no",
        taker_outcome_side="no",
        taker_book_side="ask",
        is_block_trade=block,
        yes_price_dollars="0.4625",
        no_price_dollars="0.5375",
        count_fp=qty,
    )


def subscribe(feed):
    feed.event("connected", 1, 1)
    for i, channel in enumerate((DEPTH, TRADE), 1):
        assert not feed.message(
            json.dumps(
                {"type": "subscribed", "id": i, "msg": {"sid": i, "channel": channel}}
            ).encode(),
            2,
        )
    return feed


def test_recorded_public_messages():
    feed = Feed(MARKETS)
    feed.event("connected", 1, 0)
    for line in FIXTURE.read_bytes().splitlines():
        assert not feed.message(line, 1)
    assert feed.last == {1: 3, 2: 1}
    assert set(feed.books) == set(MARKETS)
    assert sum(s["deltas"] for s in feed.by_market.values()) == 1
    assert feed.by_market[A]["on_book_trades"] == 1


def test_exact_reconstruction_and_independent_subscription_sequences():
    feed = subscribe(Feed(MARKETS))
    for frame in (snapshot(1), snapshot(2, B), delta(3), trade(1), delta(4, "0.10", B)):
        assert not feed.message(frame, 4)
    assert feed.books[A]["yes"][Decimal("46.25")] == Decimal("10.01")
    assert feed.books[B]["yes"][Decimal("46.25")] == Decimal("10.35")
    assert feed.message(delta(6), 5)
    assert feed.books == {} and feed.trade_contiguous
    assert feed.problems[-1]["expected"] == 5
    assert feed.report(True, 601)["coverage"] == "incomplete"
    subscribe(feed)
    assert not feed.books and feed.message(delta(1), 8)
    subscribe(feed)
    assert not feed.message(snapshot(1), 9)
    assert not feed.message(delta(2, "-10.25"), 10)
    assert not feed.books[A]["yes"]


def test_duplicates_and_conflicts_preserve_raw_semantics():
    feed = subscribe(Feed(MARKETS))
    for raw in (snapshot(1), snapshot(1), delta(2), delta(2), trade(1), trade(2)):
        assert not feed.message(raw, 4)
    assert feed.books[A]["yes"][Decimal("46.25")] == Decimal("10.01")
    assert feed.count["duplicate_messages"] == 2 and feed.by_market[A]["unique_trades"] == 1
    assert feed.by_market[A]["duplicate_trade_ids"] == 1
    assert feed.report(True, 601)["unique_exact_public_print_coincidences"] == 1
    assert feed.message(trade(3, "0.25"), 5)
    assert feed.books and not feed.trade_contiguous
    assert feed.message(delta(2, "2.00"), 6)
    assert not feed.books and feed.count["conflicting_sequence"] == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        delta(2, "-11"),
        delta(2, "-0.000000001"),
        packet("orderbook_snapshot", 1, 2, market_ticker=A, yes_dollars_fp=[["0.4", "junk"]]),
        delta(2, ticker="KXOTHER-X"),
        packet(DEPTH, 1, 2, market_ticker=A, price_dollars="0.46255", side="yes", delta_fp="1"),
    ],
)
def test_malformed_or_foreign_input_invalidates_segment(raw):
    feed = subscribe(Feed(MARKETS))
    feed.message(snapshot(1), 2)
    assert feed.message(raw, 3)
    assert not feed.books and feed.count["malformed_message"] == 1


@pytest.mark.parametrize(
    "prints,expected",
    [
        ([], 1),
        ([trade(1, block=True)], 1),
        ([trade(1, "2.00")], 1),
        ([trade(1), trade(2, trade_id="two")], 1),
        ([trade(1)], 0),
    ],
)
def test_decrease_attribution_is_conservative(prints, expected):
    feed = subscribe(Feed(MARKETS))
    for raw in [snapshot(1), delta(2), *prints]:
        feed.message(raw, 4)
    assert feed.report(True, 601)["unmatched_decreases"] == expected


@pytest.fixture
def run(tmp_path):
    path = tmp_path / "b3b2-test"
    path.mkdir()
    exclusive(
        path / journal.MANIFEST, {"format": journal.FORMAT, "session": "test", "markets": MARKETS}
    )
    return path


class Stream:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def send(self, cmd):
        self.sent.append(json.loads(cmd))

    async def recv(self, *, decode):
        assert decode is False
        item = next(self.frames)
        if isinstance(item, Exception):
            raise item
        return item


async def test_mock_socket_reconnect_stop_backup_and_replay(run, monkeypatch):
    recorded = FIXTURE.read_bytes().splitlines()
    first = Stream([*recorded, ConnectionError("gone")])
    second = Stream(recorded)
    streams = iter([first, second])

    @asynccontextmanager
    async def dial():
        stream = next(streams)
        if stream is second:
            recv = stream.recv

            async def receive(**kw):
                raw = await recv(**kw)
                if json.loads(raw)["type"] == TRADE:
                    exclusive(run / journal.STOP, {"operator_stop": True})
                return raw

            monkeypatch.setattr(stream, "recv", receive)
        yield stream

    writer = Writer(run)
    result = await pilot.collect(writer, dial, AsyncMock(return_value=[]), 60)
    writer.close(result)
    assert result == "operator_stop"
    for stream in (first, second):
        assert stream.sent == [
            {"id": i, "cmd": "subscribe", "params": {"channels": [c], "market_tickers": MARKETS}}
            for i, c in enumerate((DEPTH, TRADE), 1)
        ]
    report = inspect(run)
    assert report["counts"]["connection_lost"] == report["reconnects"] == 1
    assert report["markets"][A]["unique_trades"] == report["markets"][A]["duplicate_trade_ids"] == 1
    assert health(run)["state"] == "sealed"
    dest = backup(run)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()}
    assert inspect(dest) == inspect(run) == report
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()}
    assert backup(run) == dest
    with (dest / journal.LOG).open("ab") as f:
        f.write(b"[]\n")
    with pytest.raises(ValueError):
        inspect(dest)


def test_interrupted_writer_partial_tail_and_no_resume(run):
    writer = Writer(run)
    writer.record("connected", 1)
    writer.close(None)
    with (run / journal.LOG).open("ab") as f:
        f.write(b"[2,")
    with pytest.raises(ValueError, match="Unsealed"):
        backup(run)
    report = inspect(run, require_seal=False)
    assert report["partial_final_record"] and not report["clean_seal_verified"]
    assert report["coverage"] == "incomplete"
    with pytest.raises((FileExistsError, ValueError)):
        Writer(run)


def test_missing_trade_sequence_preserves_print_but_invalidates_trade_continuity():
    feed = subscribe(Feed(MARKETS))
    feed.message(snapshot(1), 3)
    unsequenced = json.loads(trade(1))
    del unsequenced["seq"]
    assert not feed.message(json.dumps(unsequenced).encode(), 4)
    assert not feed.trade_contiguous and A in feed.books
    assert feed.by_market[A]["unique_trades"] == 1
    assert feed.count["trade_sequence_unavailable"] == 1
    assert feed.report(True, 601)["coverage"] == "incomplete"


async def test_graceful_interrupt_seals_and_recorder_failure_stays_unsealed(run, monkeypatch):
    meta = journal.manifest(run)
    meta["build"] = pilot.build_info()
    (run / journal.MANIFEST).write_bytes(journal.encoded(meta))

    @asynccontextmanager
    async def fake_client():
        yield None

    monkeypatch.setattr(pilot, "client", fake_client)
    monkeypatch.setattr(pilot, "collect", AsyncMock(side_effect=asyncio.CancelledError))
    assert await pilot.capture(run, 600) == "operator_interrupt"
    assert inspect(run)["clean_seal_verified"]
    other = run.parent / "b3b2-full"
    other.mkdir()
    exclusive(other / journal.MANIFEST, meta)
    monkeypatch.setattr(
        pilot, "collect", AsyncMock(side_effect=journal.RecorderError("storage failed"))
    )
    with pytest.raises(journal.RecorderError):
        await pilot.capture(other, 600)
    assert health(other)["state"] == "recorder_failed" and not (other / journal.SEAL).exists()
    with pytest.raises(ValueError, match="Unsealed"):
        backup(other)


async def test_authentication_failure_and_channel_denial(run):
    @asynccontextmanager
    async def denied():
        raise InvalidStatus(SimpleNamespace(status_code=403, reason_phrase="forbidden", headers={}))
        yield

    writer = Writer(run)
    reason = await pilot.collect(writer, denied, AsyncMock(return_value=[]), 5)
    writer.close(reason)
    report = inspect(run)
    assert reason == "authentication_failed" and report["counts"][reason] == 1
    assert report["coverage"] == "incomplete"
    feed = subscribe(Feed(MARKETS))
    assert feed.message(b'{"type":"error","id":2,"msg":{"code":7,"msg":"denied"}}', 3)
    assert feed.problems[-1]["channel"] == TRADE


async def test_selection_refuses_inactive_missing_or_foreign_markets():
    def market(t, status="active"):
        return {
            "ticker": t,
            "event_ticker": t.rsplit("-", 1)[0],
            "status": status,
            "close_time": "2099-01-01T00:00:00Z",
        }

    api = SimpleNamespace(
        get_markets=AsyncMock(
            return_value={"markets": [market(A), market(B), market("BAD-NBA-TEAM")]}
        ),
        get_market=AsyncMock(side_effect=market),
    )
    assert [r["ticker"] for r in await pilot.select(api, None, 2)] == MARKETS
    with pytest.raises(ValueError, match="unavailable"):
        await pilot.select(api, [A, "KXNBAGAME-MISSING-TEAM"], 2)
    api.get_market = AsyncMock(side_effect=lambda t: market(t, "finalized"))
    with pytest.raises(ValueError, match="inactive"):
        await pilot.select(api, [A, B], 2)
    with pytest.raises(ValueError):
        journal.run_path("../b3b1-night-2026-11-21")


def edit(raw, *, msg=None, omit=(), **envelope):
    value = json.loads(raw)
    value.update(envelope)
    value["msg"].update(msg or {})
    for key in omit:
        del value["msg"][key]
    return json.dumps(value).encode()


@pytest.mark.parametrize("drift_ms", [0, 10])
def test_offset_drift_and_buffering_are_separate_observations(drift_ms):
    timing = Timing()
    for i, buffering_ms in enumerate((0, 0, 150)):
        mono = (i * 120_000 + buffering_ms) * 1_000_000
        wall = mono + (1_000_000 - 100 + i * drift_ms) * 1_000_000
        timing.receipt(wall, mono)
        timing.observe(TRADE, {"sending_ts_ms": 1_000_000 + i * 120_000}, wall, mono)
    result = timing.report()
    assert result["by_channel"][TRADE] == {
        "min": -100.0,
        "max": 50.0 + 2 * drift_ms,
        "p95": 50.0 + 2 * drift_ms,
        "median": -100.0 + drift_ms,
        "samples": 3,
        "negative_samples": 2,
    }
    assert result["wall_elapsed_minus_monotonic_elapsed_ms"]["end"] == 2 * drift_ms
    assert [w[TRADE]["median"] for w in result["two_minute_windows"]] == [
        -100,
        -100 + drift_ms,
        50 + 2 * drift_ms,
    ]


@pytest.mark.parametrize("bad", [None, "1000", True, -1, 2**63])
def test_malformed_times_excluded_without_losing_depth_or_claiming_latency(bad):
    feed = subscribe(Feed(MARKETS))
    feed.message(snapshot(1), 10)
    assert not feed.message(edit(delta(2), msg={"ts_ms": bad}, sending_ts_ms=bad), 20)
    feed.message(edit(trade(1), sending_ts_ms=0), 30)
    r = feed.report(True, 601)
    assert r["valid_book_decreases"] == r["unmatched_decreases"] == 1
    assert r["markets"][A]["malformed_event_timestamp"] == 1
    assert r["clock_alignment_status"] == r["one_way_latency_status"] == "indeterminate"
    s = r["server_send_to_local_receive_ms"]
    assert s["samples"] == 1 and s["negative_samples"] == 0
    assert s["sending_timestamp_issues"] == {
        "orderbook_snapshot": {"missing": 1},
        DEPTH: {"malformed": 1},
    }


def test_independent_market_clocks_repeats_skips_regression_and_recovery():
    feed = subscribe(Feed(MARKETS))
    for raw in (
        edit(snapshot(1), msg={"clock": 990}),
        snapshot(2, B),
        edit(delta(3), msg={"clock": 1000}),
        edit(delta(4, ticker=B), msg={"clock": 1}),
        edit(delta(5), msg={"clock": 1000}),
    ):
        assert not feed.message(raw, 100)
    assert feed.by_market[A]["skipped_clock_transitions"] == 1
    assert feed.by_market[A]["repeated_delta_clocks"] == 1
    assert feed.message(edit(delta(6), msg={"clock": 999}), 101)
    assert feed.count["sequence_gaps"] == 0 and A not in feed.books and B in feed.books
    subscribe(feed)
    assert not feed.message(edit(snapshot(1), msg={"clock": 1}), 110)
    assert not feed.message(edit(delta(2), msg={"clock": 2}), 120)
    assert [b["classification"] for b in feed.clock_boundaries] == [
        "unexplained_regression",
        "lower_after_interruption",
    ]
    assert all(
        b["market"] == A and b["maintenance_evidence"] == "unavailable"
        for b in feed.clock_boundaries
    )
    assert feed.message(delta(4), 122) and A not in feed.books
    assert feed.message(delta(5), 123) and not feed.books
    assert not feed.message(snapshot(6), 124)
    assert not feed.message(delta(7), 125) and A in feed.books
    assert feed.count["sequence_gaps"] == 1


def test_snapshot_clock_barrier_and_no_attribution_across_replacement():
    feed = subscribe(Feed(MARKETS))
    feed.message(snapshot(1), 3)
    assert feed.message(edit(delta(2), msg={"clock": 0}), 4)
    assert feed.count["delta_at_snapshot_clock"] == 1
    assert not feed.message(snapshot(3), 5)
    assert not feed.message(delta(4), 6)
    assert not feed.message(edit(snapshot(5), msg={"clock": 5}), 7)
    assert not feed.message(trade(1), 8)
    assert not feed.report(True, 601)["exact_coincidence_evidence"]


@pytest.mark.parametrize(
    "raw",
    [
        edit(trade(1), omit=["taker_side"]),
        edit(trade(1), omit=["taker_book_side"]),
        edit(trade(1), msg={"taker_outcome_side": "yes"}),
        edit(trade(1), msg={"taker_book_side": "bid"}),
        edit(trade(1), omit=["is_block_trade"]),
        edit(trade(1), msg={"is_block_trade": 0}),
        trade(1, block=True),
        edit(
            trade(1),
            msg={"taker_side": "yes", "taker_outcome_side": "yes", "taker_book_side": "bid"},
        ),
        edit(trade(1), msg={"ts_ms": 1001}),
        edit(trade(1), msg={"market_ticker": B}),
        edit(trade(1), omit=["ts_ms"]),
    ],
)
def test_ineligible_prints_are_visible_and_never_assigned(raw):
    feed = subscribe(Feed(MARKETS))
    for frame in (snapshot(1), snapshot(2, B), delta(3), raw):
        assert not feed.message(frame, 4)
    report = feed.report(True, 601)
    assert report["unique_public_trades"] == 1
    assert report["valid_book_decreases"] == report["unmatched_decreases"] == 1
    assert report["exact_coincidence_evidence"] == []


@pytest.mark.parametrize(
    "frames",
    [
        [delta(2), trade(1), trade(2, trade_id="another")],
        [trade(1), delta(2), delta(3)],
        [delta(2, "-0.48"), trade(1), trade(2, trade_id="another")],
        [delta(2), trade(1), trade(2, "0.12", "partial")],
        [delta(2), trade(1), edit(trade(2, trade_id="unknown"), omit=["taker_side"])],
        [delta(2), trade(1), edit(delta(3), omit=["ts_ms"])],
        [delta(2), trade(1), trade(2, "0.36")],
        [delta(2), trade(1), edit(delta(2), msg={"delta_fp": "-0.36"})],
    ],
)
def test_ambiguous_and_conflicting_groups_have_no_matches(frames):
    feed = subscribe(Feed(MARKETS))
    for frame in [snapshot(1), *frames]:
        feed.message(frame, 4)
    r = feed.report(True, 601)
    assert r["unique_exact_public_print_coincidences"] == 0
    assert r["ambiguous_candidate_groups"]


def test_repeated_price_size_at_distinct_times_and_duplicate_trade_id():
    feed = subscribe(Feed(MARKETS))
    for raw in (
        snapshot(1),
        trade(1),
        delta(2),
        edit(delta(3), msg={"ts_ms": 1001}),
        edit(trade(2, trade_id="second"), msg={"ts_ms": 1001}),
        trade(3),
    ):
        assert not feed.message(raw, 4)
    rows = feed.report(True, 601)["exact_coincidence_evidence"]
    assert [(r["trade_id"], r["trade"]["record"], r["decrease"]["record"]) for r in rows] == [
        ("trade-one", 5, 6),
        ("second", 8, 7),
    ]


def test_trade_subscription_missing_late_and_quiet_while_depth_continues():
    frames = FIXTURE.read_bytes().splitlines()
    feed = Feed(MARKETS)
    feed.event("connected", 1, 0, 0)
    for frame in (frames[0], snapshot(1), snapshot(2, B), delta(3), delta(4, ticker=B)):
        feed.message(frame, 1, 1)
    grades = feed.report(True, 601)["coverage_classifications"]
    assert grades["subscription_availability"]["status"] == "incomplete"
    assert grades["orderbook_continuity"]["status"] == "observed_contiguous"
    assert grades["trade_feed_integrity"]["status"] == "indeterminate"
    feed.message(frames[3], 100, 100)
    feed.message(trade(1), 200, 200)
    assert feed.classifications(True, 0)["trade_feed_integrity"]["status"] == "indeterminate"
    feed.message(edit(trade(2, trade_id="another"), msg={"market_ticker": B}), 300, 300)
    result = feed.report(True, 601)
    assert result["coverage_classifications"]["trade_feed_integrity"]["status"] == "partial_overlap"
    assert result["unique_exact_public_print_coincidences"] == 0
    assert result["segments"][0]["first_trade_by_market"][B]["mono_ns"] == 300


def test_historical_matches_independently_from_sealed_wire_messages():
    run = journal.ROOT / "b3b2-stage1-1795332429457"
    archive = run / "backup"
    if not archive.is_dir():
        pytest.skip("Historical pilot archive is external to the repository")

    def hashes():
        return {p.relative_to(run): journal.digest(p) for p in run.rglob("*") if p.is_file()}

    before = hashes()
    assert (
        journal.digest(archive / journal.LOG)
        == "2e0fd00c8b3cfd8f8ca7e36544478709908b0044add56fb55a3dc878e1eeb211"
    )
    # Independent exhaustive join of the historical raw receipts, without Feed or its key.
    records = [json.loads(line) for line in (archive / journal.LOG).read_bytes().splitlines()]
    assert [r[0] for r in records] == list(range(1, 334))
    assert [r[3] for r in records if r[3] in ("connected", "fault", "closed")] == [
        "connected",
        "closed",
    ]
    packets = [
        (n, json.loads(base64.b64decode(v, validate=True)))
        for n, _, _, k, v in records
        if k == "raw"
    ]
    assert [p["msg"] for _, p in packets if p["type"] == "subscribed"] == [
        {"sid": 1, "channel": DEPTH},
        {"sid": 2, "channel": TRADE},
    ]
    for sid, length in [(1, 301), (2, 23)]:
        assert [p["seq"] for _, p in packets if p.get("sid") == sid] == list(range(1, length + 1))
    books = {}
    decreases = []
    prints = []
    for n, p in packets:
        m = p["msg"]
        if p["type"] == "orderbook_snapshot":
            assert p["sid"] == 1 and n in (8, 9)
            books[m["market_ticker"]] = {
                (s, Decimal(price)): Decimal(q)
                for s in ("yes", "no")
                for price, q in m[s + "_dollars_fp"]
            }
        elif p["type"] == DEPTH:
            assert p["sid"] == 1 and n > 10
            book = books[m["market_ticker"]]
            key = m["side"], Decimal(m["price_dollars"])
            change = Decimal(m["delta_fp"])
            book[key] = book.get(key, 0) + change
            assert book[key] >= 0
            if change < 0:
                decreases.append((n, p))
        elif p["type"] == TRADE:
            assert p["sid"] == 2 and n > 10
            prints.append((n, p))
    assert len({p["msg"]["trade_id"] for _, p in prints}) == len(prints) == 23
    expected = []
    for n, p in prints:
        m = p["msg"]
        assert m["is_block_trade"] is False
        resting = {"bid": "no", "ask": "yes"}[m["taker_book_side"]]
        assert m["taker_side"] == m["taker_outcome_side"] != resting
        candidates = [
            (dn, d["seq"])
            for dn, d in decreases
            if d["msg"]["market_ticker"] == m["market_ticker"]
            and d["msg"]["side"] == resting
            and Decimal(d["msg"]["price_dollars"]) == Decimal(m[resting + "_price_dollars"])
            and -Decimal(d["msg"]["delta_fp"]) == Decimal(m["count_fp"])
            and d["msg"]["ts_ms"] == m["ts_ms"]
        ]
        assert len(candidates) == 1
        expected.append((m["trade_id"], n, p["seq"], *candidates[0]))
    assert len(decreases) == 168 and len(Counter(row[3] for row in expected)) == 23
    report = inspect(archive)
    assert [
        (
            m["trade_id"],
            m["trade"]["record"],
            m["trade"]["seq"],
            m["decrease"]["record"],
            m["decrease"]["seq"],
        )
        for m in report["exact_coincidence_evidence"]
    ] == expected
    assert report["unmatched_decreases"] == 145
    assert report["capture_build_provenance"]["status"] == "missing_capture_build_fingerprint"
    assert report["server_send_to_local_receive_ms"]["negative_samples"] == 323
    assert (
        not report["market_clock_boundaries"]
        and report["clock_alignment_status"] == "indeterminate"
    )
    assert journal.encoded(inspect(archive)) == journal.encoded(report)
    assert hashes() == before
