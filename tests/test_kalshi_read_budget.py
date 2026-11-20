import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request

from kalshi_edge_engine.api import main
from kalshi_edge_engine.db.paper_store import PaperStore
from kalshi_edge_engine.engine.paper import QuoteIntent
from kalshi_edge_engine.kalshi.client import KalshiClient, KalshiReadBudgetError
from kalshi_edge_engine.kalshi.read_budget import ReadBudget, ReadBudgetTimeout, read_priority


class AutoClock:
    now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        await asyncio.sleep(0)


class ManualClock:
    def __init__(self):
        self.now = 0.0
        self.sleepers = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + delay, future))
        await future

    async def advance(self, seconds):
        self.now += seconds
        ready = [(at, future) for at, future in self.sleepers if at <= self.now]
        self.sleepers = [(at, future) for at, future in self.sleepers if at > self.now]
        for _, future in ready:
            if not future.done():
                future.set_result(None)
        await asyncio.sleep(0)


def _client(budget, handler):
    client = object.__new__(KalshiClient)
    client.base_url = "https://example.test/trade-api/v2"
    client.api_key_id = "test-id"
    client.read_budget = budget
    client._sign_request = Mock(return_value="test-signature")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_weighted_fifo_serves_inventory_and_outcomes_ahead_of_discovery():
    clock = ManualClock()
    budget = ReadBudget(10, 10, clock=clock, sleep=clock.sleep, queue_timeout=20)
    await budget.acquire("/markets")
    admitted = []

    async def read(lane, label):
        with read_priority(lane):
            await budget.acquire("/markets/A/orderbook")
        admitted.append(label)

    tasks = [
        asyncio.create_task(read(lane, label))
        for lane, label in (
            ("discovery", "scan"),
            ("outcome", "five-second"),
            ("inventory", "settlement"),
            ("outcome", "sixty-second"),
            ("inventory", "mark"),
            ("entry", "entry"),
        )
    ]
    await asyncio.sleep(0)
    for _ in range(6):
        await clock.advance(1)
    await asyncio.gather(*tasks)
    assert admitted == ["settlement", "five-second", "mark", "entry", "sixty-second", "scan"]
    assert budget.snapshot()["max_queue_delay_ms"] == 6_000


@pytest.mark.asyncio
async def test_endpoint_costs_share_one_bucket_across_client_reads():
    clock = AutoClock()
    budget = ReadBudget(
        10,
        20,
        clock=clock,
        sleep=clock.sleep,
        costs={"/markets/{ticker}/orderbook": 15, "/markets/{ticker}": 10},
    )
    requests = []

    def serve(request):
        requests.append((clock.now, request.url.path))
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook": {"yes": [], "no": []}})
        return httpx.Response(200, json={"market": {"ticker": "A"}})

    async with _client(budget, serve) as client:
        await client.get_orderbook("A")
        with read_priority("inventory"):
            await client.get_market("A")
    assert requests[0] == (0.0, "/trade-api/v2/markets/A/orderbook")
    assert requests[1][0] == pytest.approx(0.5)
    stats = budget.snapshot()
    assert stats["tokens_charged"] == 25
    assert stats["requests_by_lane"]["inventory"] == 1


@pytest.mark.asyncio
async def test_429_adapts_budget_retries_once_and_blocks_other_reads():
    clock = AutoClock()
    budget = ReadBudget(100, 30, clock=clock, sleep=clock.sleep, jitter=lambda: 0.5)
    attempts = []

    def serve(request):
        attempts.append((clock.now, request.url.path))
        if request.url.path.endswith("/orderbook"):
            return httpx.Response(429, headers={"X-RateLimit-Cost": "20"}, json={})
        return httpx.Response(200, json={"market": {"ticker": "A"}})

    async with _client(budget, serve) as client:
        with pytest.raises(KalshiReadBudgetError, match="rate limited"):
            await client.get_orderbook("A")
        assert len(attempts) == 2
        assert attempts[1][0] >= 0.28125
        with read_priority("inventory"):
            await client.get_market("A")
        assert attempts[2][0] - attempts[1][0] >= 0.5625

    stats = budget.snapshot()
    assert stats["responses_429"] == 2
    assert stats["retries"] == 1
    assert stats["tokens_charged"] == 50
    assert stats["effective_tokens_per_second"] == pytest.approx(49)
    assert stats["backoff_seconds_total"] == pytest.approx(0.84375)
    assert stats["queued_by_lane"]["inventory"] == 0


@pytest.mark.asyncio
async def test_exhausted_queue_times_out_and_canceled_waiters_are_removed():
    clock = ManualClock()
    budget = ReadBudget(10, 10, queue_timeout=0.5, clock=clock, sleep=clock.sleep)
    await budget.acquire("/markets")
    canceled = asyncio.create_task(budget.acquire("/markets"))
    waiting = asyncio.create_task(budget.acquire("/markets"))
    await asyncio.sleep(0)
    canceled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceled
    await clock.advance(0.5)
    with pytest.raises(ReadBudgetTimeout):
        await waiting
    assert budget.snapshot()["queue_timeouts"] == 1
    assert budget.snapshot()["queued_by_lane"]["discovery"] == 0


def test_budget_from_env_accepts_current_tiers_and_cost_overrides(monkeypatch):
    monkeypatch.setenv("KALSHI_READ_TIER", "advanced")
    monkeypatch.setenv("KALSHI_READ_ENDPOINT_COSTS_JSON", '{"/markets/{ticker}": 20}')
    budget = ReadBudget.from_env()
    assert budget.snapshot()["tokens_per_second"] == 300
    assert budget.snapshot()["bucket_capacity"] == 900
    assert budget.discovery_slice_size(2, 100) == 15


@pytest.mark.asyncio
async def test_read_budget_endpoint_reports_missed_horizons(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1_000)
    store = PaperStore(str(tmp_path / "paper.db"), clock_ms=lambda: clock.now)
    store.insert_intent(QuoteIntent("entry", "GAME", "YES", 41, 1, 1_000, 5, 8, 8, "maker", True))
    assert store.insert_fill(
        fill_id="fill",
        intent_id="entry",
        fill_ts_ms=1_000,
        fill_price_cents=41,
        fill_size=1,
        remaining_size=0,
        note="entry_moved_through",
    )
    clock.now = 32_000
    app = FastAPI()
    app.state.kalshi_client = SimpleNamespace(read_budget=ReadBudget(100, 100))
    monkeypatch.setattr(main, "_paper_store", store)
    result = await main.get_kalshi_read_budget(Request({"type": "http", "app": app}))
    assert result["missed_horizons_last_hour"] == {"5s": 1, "15s": 1, "30s": 0, "60s": 0}
