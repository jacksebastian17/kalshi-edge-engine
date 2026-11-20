"""Shared token budget for authenticated, paper-only Kalshi reads."""

import asyncio
import json
import math
import os
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

ReadLane = Literal["inventory", "outcome", "entry", "discovery"]
_lane: ContextVar[ReadLane] = ContextVar("kalshi_read_lane", default="discovery")
_tiers = {
    "basic": (200, 600),
    "advanced": (300, 900),
    "expert": (600, 600),
    "premier": (1200, 1200),
    "paragon": (2400, 2400),
    "prime": (4800, 4800),
    "prestige": (12000, 12000),
}
_schedule: tuple[ReadLane, ...] = (
    "inventory",
    "outcome",
    "inventory",
    "entry",
    "outcome",
    "inventory",
    "discovery",
)


@contextmanager
def read_priority(lane: ReadLane) -> Iterator[None]:
    token = _lane.set(lane)
    try:
        yield
    finally:
        _lane.reset(token)


class ReadBudgetTimeout(RuntimeError):
    """The read could not fit within its local queue deadline."""


@dataclass(eq=False)
class _Read:
    lane: ReadLane
    cost: int


class ReadBudget:
    """One token bucket with weighted FIFO lanes and shared congestion feedback."""

    def __init__(
        self,
        refill_rate: float,
        capacity: int,
        *,
        costs: Mapping[str, int] | None = None,
        queue_timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if (
            not math.isfinite(refill_rate)
            or refill_rate <= 0
            or capacity < 10
            or not math.isfinite(queue_timeout)
            or queue_timeout <= 0
        ):
            raise ValueError("Invalid Kalshi read budget limits")
        self._costs = dict(costs or {})
        if any(
            not path.startswith("/") or type(value) is not int or value <= 0 or value > capacity
            for path, value in self._costs.items()
        ):
            raise ValueError("Kalshi endpoint costs must be positive integers that fit the bucket")
        self._rate = refill_rate
        self._effective_rate = refill_rate
        self._capacity = capacity
        self._queue_timeout = queue_timeout
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._tokens = float(capacity)
        self._refilled_at = clock()
        self._backoff_until = 0.0
        self._streak = 0
        self._last_429 = float("-inf")
        self._cursor = 0
        self._queues: dict[ReadLane, deque[_Read]] = {lane: deque() for lane in _schedule}
        self._sent: deque[tuple[float, ReadLane]] = deque()
        self._counts = dict.fromkeys(_schedule, 0)
        self._queue_total = 0.0
        self._queue_max = 0.0
        self._queue_timeouts = 0
        self._rate_limited = 0
        self._retries = 0
        self._backoff_seconds = 0.0
        self._charged = 0

    @classmethod
    def from_env(cls) -> "ReadBudget":
        tier = os.getenv("KALSHI_READ_TIER", "basic").strip().lower()
        if tier not in _tiers:
            raise ValueError(f"Unknown Kalshi read tier: {tier}")
        default_rate, default_capacity = _tiers[tier]
        costs = json.loads(os.getenv("KALSHI_READ_ENDPOINT_COSTS_JSON", "{}"))
        if not isinstance(costs, dict):
            raise ValueError("KALSHI_READ_ENDPOINT_COSTS_JSON must be an object")
        return cls(
            float(os.getenv("KALSHI_READ_TOKENS_PER_SECOND", str(default_rate))),
            int(os.getenv("KALSHI_READ_BUCKET_CAPACITY", str(default_capacity))),
            costs=costs,
            queue_timeout=float(os.getenv("KALSHI_READ_QUEUE_TIMEOUT_SECONDS", "5")),
        )

    @staticmethod
    def endpoint_key(path: str) -> str:
        return re.sub(r"(/(?:markets|events|series)/)[^/]+", r"\1{ticker}", path)

    def _cost(self, path: str) -> int:
        return self._costs.get(path, self._costs.get(self.endpoint_key(path), 10))

    def discovery_slice_size(self, interval_seconds: float, maximum: int) -> int:
        # Cold scans usually need an event and market read as well as the book.
        per_market = sum(
            self._cost(path)
            for path in (
                "/markets/{ticker}",
                "/events/{ticker}",
                "/markets/{ticker}/orderbook",
            )
        )
        return max(1, min(maximum, int(self._effective_rate * interval_seconds / per_market)))

    def _refill(self, now: float) -> None:
        self._tokens = min(
            self._capacity, self._tokens + (now - self._refilled_at) * self._effective_rate
        )
        self._refilled_at = now
        while self._sent and self._sent[0][0] <= now - 60:
            self._sent.popleft()

    def _head(self) -> tuple[int, _Read]:
        for shift in range(len(_schedule)):
            slot = (self._cursor + shift) % len(_schedule)
            queue = self._queues[_schedule[slot]]
            if queue:
                return slot, queue[0]
        raise RuntimeError("Read queue is empty")

    async def acquire(self, path: str) -> int:
        cost = self._cost(path)
        if cost > self._capacity:
            self._queue_timeouts += 1
            raise ReadBudgetTimeout(f"Kalshi read cost for {path} exceeds bucket capacity")
        read = _Read(_lane.get(), cost)
        queue = self._queues[read.lane]
        started = self._clock()
        queue.append(read)
        try:
            while True:
                now = self._clock()
                self._refill(now)
                slot, head = self._head()
                elapsed = now - started
                if elapsed >= self._queue_timeout:
                    self._queue_timeouts += 1
                    raise ReadBudgetTimeout(f"Kalshi {read.lane} read budget queue timed out")
                if head is read and now >= self._backoff_until and self._tokens + 1e-8 >= cost:
                    self._tokens -= cost
                    self._cursor = (slot + 1) % len(_schedule)
                    self._sent.append((now, read.lane))
                    self._counts[read.lane] += 1
                    self._queue_total += elapsed
                    self._queue_max = max(self._queue_max, elapsed)
                    self._charged += cost
                    return cost
                # Cap the recheck interval so a newly queued higher-priority read can proceed.
                ahead = max(
                    0.005,
                    self._backoff_until - now,
                    (head.cost - self._tokens) / self._effective_rate,
                )
                await self._sleep(min(0.05, ahead, self._queue_timeout - elapsed))
        finally:
            queue.remove(read)

    def observe(self, path: str, cost: int, status: int, headers: Mapping[str, str]) -> None:
        now = self._clock()
        self._refill(now)
        try:
            actual = int(headers.get("X-RateLimit-Cost", ""))
        except (ValueError, TypeError):
            actual = cost
        if cost < actual:
            self._tokens -= actual - cost
            self._charged += actual - cost
            key = path if path in self._costs else self.endpoint_key(path)
            self._costs[key] = max(actual, self._costs.get(key, 0))
        if status == 429:
            self._rate_limited += 1
            self._streak = min(6, self._streak + 1)
            self._last_429 = now
            self._effective_rate = max(self._rate * 0.25, self._effective_rate * 0.7)
            delay = min(8.0, 0.25 * 2 ** (self._streak - 1) * (1 + 0.25 * self._jitter()))
            previous = max(now, self._backoff_until)
            self._backoff_until = max(previous, now + delay)
            self._backoff_seconds += self._backoff_until - previous
            self._tokens = min(self._tokens, 0.0)
        elif 200 <= status < 400 and now - self._last_429 >= 5:
            self._streak = 0
            self._effective_rate = min(self._rate, self._effective_rate + self._rate * 0.01)

    def record_retry(self) -> None:
        self._retries += 1

    def snapshot(self) -> dict[str, object]:
        now = self._clock()
        self._refill(now)
        recent = dict.fromkeys(_schedule, 0)
        for _, lane in self._sent:
            recent[lane] += 1
        return {
            "tokens_per_second": self._rate,
            "effective_tokens_per_second": self._effective_rate,
            "bucket_capacity": self._capacity,
            "tokens_charged": self._charged,
            "requests_by_lane": dict(self._counts),
            "requests_last_60s_by_lane": recent,
            "requests_per_second_last_60s": len(self._sent) / 60,
            "queued_by_lane": {lane: len(queue) for lane, queue in self._queues.items()},
            "queue_timeouts": self._queue_timeouts,
            "mean_queue_delay_ms": self._queue_total * 1000 / max(1, sum(self._counts.values())),
            "max_queue_delay_ms": self._queue_max * 1000,
            "responses_429": self._rate_limited,
            "retries": self._retries,
            "backoff_remaining_seconds": max(0.0, self._backoff_until - now),
            "backoff_seconds_total": self._backoff_seconds,
        }
