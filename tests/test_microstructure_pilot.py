import asyncio
import hashlib
import json
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
        clock=0,
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
