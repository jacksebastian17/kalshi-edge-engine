"""Authenticated production market-data connection; outbound messages are subscriptions only."""

import asyncio
import hashlib
import json
import os
import platform
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from pathlib import Path

from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from kalshi_edge_engine.kalshi.client import KalshiClient, KalshiClientError
from kalshi_edge_engine.kalshi.market import read_market_observation
from tools.capture_builds import approved_capture

from .feed import CHANNELS
from .journal import FORMAT, MANIFEST, STOP, RecorderError, Writer, exclusive, manifest
from .timing import windows_clock_status

WS_PATH = "/trade-api/ws/v2"
WS_URL = "wss://external-api-ws.kalshi.com" + WS_PATH
SERIES = "KXNBAGAME"


def build_info() -> dict:
    source = Path(__file__).parent
    return {
        "python": platform.python_version(),
        "websockets": version("websockets"),
        "module_sha256": {
            p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for p in sorted(source.glob("*.py"))
        },
    }


class VenueConnection(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc  # Never forward a signed handshake to a redirect destination.


def client() -> KalshiClient:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    base = os.environ.get("KALSHI_BASE_URL", KalshiClient.DEFAULT_BASE_URL).rstrip("/")
    if base not in (KalshiClient.DEFAULT_BASE_URL, "https://external-api.kalshi.com/trade-api/v2"):
        raise ValueError("Pilot requires an official production REST origin")
    return KalshiClient(base_url=base)


def socket(api: KalshiClient) -> VenueConnection:
    t = int(time.time() * 1000)
    return VenueConnection(
        WS_URL,
        additional_headers={
            "KALSHI-ACCESS-KEY": api.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(t),
            "KALSHI-ACCESS-SIGNATURE": api._sign_request(t, "GET", WS_PATH),
        },
        proxy=None,
        open_timeout=10,
        close_timeout=3,
        ping_interval=15,
        ping_timeout=10,
        max_size=4 * 1024**2,
        max_queue=32,
    )


def active(market: dict, ticker: str, now_ms: int | None = None) -> bool:
    now = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    event = market.get("event_ticker")
    observation = read_market_observation(market, ticker, requested_ts_ms=now, observed_ts_ms=now)
    return bool(
        isinstance(event, str)
        and event.startswith(SERIES + "-")
        and ticker.startswith(event + "-")
        and observation
        and observation.tradable_at(now)
    )


async def confirm(
    api: KalshiClient, tickers: list[str], expected_events: dict[str, str] | None = None
) -> list[dict]:
    records = []
    for ticker in tickers:
        market = await api.get_market(ticker)
        if not active(market, ticker):
            raise ValueError(f"Selected NBA market is unavailable or inactive: {ticker}")
        if expected_events is not None and market["event_ticker"] != expected_events[ticker]:
            raise ValueError(f"Event identity changed during market confirmation: {ticker}")
        records.append(
            {
                k: market.get(k)
                for k in ("ticker", "event_ticker", "status", "close_time", "volume_24h_fp")
            }
        )
    return records


async def listed_markets(api: KalshiClient) -> AsyncIterator[dict]:
    cursor = None
    cursors = set()
    while True:
        page = await api.get_markets(series_ticker=SERIES, status="open", limit=200, cursor=cursor)
        for m in page["markets"]:
            yield m
        cursor = page.get("cursor")
        if not cursor:
            break
        if cursor in cursors:
            raise ValueError("Repeated discovery cursor")
        cursors.add(cursor)


async def select(api: KalshiClient, explicit: list[str] | None, count: int) -> list[dict]:
    if explicit is not None and (
        not 2 <= len(set(explicit)) == len(explicit) <= 4
        or not all(t.startswith(SERIES + "-") for t in explicit)
    ):
        raise ValueError("Specify 2–4 distinct NBA game markets")
    available: dict[str, Decimal] = {}
    async for m in listed_markets(api):
        ticker = m["ticker"]
        if active(m, ticker):
            volume = Decimal(str(m.get("volume_24h_fp") or 0))
            available[ticker] = volume if volume.is_finite() else Decimal(0)
    tickers = (
        explicit
        if explicit is not None
        else sorted(available, key=lambda t: (-available[t], t))[:count]
    )
    if len(tickers) < (len(explicit) if explicit else count) or set(tickers) - available.keys():
        raise ValueError("Requested market set unavailable in open NBA game series")
    return await confirm(api, tickers)


async def select_events(api: KalshiClient, count: int, audit: dict) -> list[dict]:
    audit.update(
        rule="distinct-nba-events-v1",
        count=count,
        started_ns=time.time_ns(),
        candidates=[],
        ranking="Active KXNBAGAME contracts with future close. Descending listed 24h volume, then ascending ticker. Missing volume ranks as zero. One per event.",
    )
    ranked: dict[str, tuple[Decimal, dict]] = {}
    async for market in listed_markets(api):
        observed = time.time_ns()
        row = {
            k: market.get(k)
            for k in ("ticker", "event_ticker", "status", "close_time", "volume_24h_fp")
        }
        row["observed_ns"] = observed
        row["reason"] = "awaiting_rank"
        audit["candidates"].append(row)
        ticker = row["ticker"]
        event = row["event_ticker"]
        if (
            not isinstance(ticker, str)
            or not isinstance(event, str)
            or not event.startswith(SERIES + "-")
            or ticker.rpartition("-")[0] != event
        ):
            row["reason"] = "outside_series_or_inconsistent_event"
            continue
        if row["status"] != "active":
            row["reason"] = "inactive_status"
            continue
        if not active(market, ticker, observed // 1_000_000):
            row["reason"] = "invalid_or_elapsed_close"
            continue
        try:
            volume = Decimal(str(row["volume_24h_fp"] if row["volume_24h_fp"] is not None else 0))
        except InvalidOperation:
            volume = Decimal("NaN")
        if not volume.is_finite() or volume < 0:
            row["reason"] = "invalid_reported_volume"
            continue
        if ticker in ranked:
            row["reason"] = "duplicate_listing"
            if ranked[ticker][0] != volume:
                raise ValueError(f"Inconsistent volume across discovery pages: {ticker}")
            continue
        ranked[ticker] = volume, row
    selected: dict[str, str] = {}
    events: set[str] = set()
    for ticker in sorted(ranked, key=lambda t: (-ranked[t][0], t)):
        _, row = ranked[ticker]
        event = row["event_ticker"]
        if event in events:
            row["reason"] = "event_already_selected"
        elif len(selected) == count:
            row["reason"] = "below_selection_limit"
        else:
            selected[ticker] = event
            events.add(event)
            row["reason"] = "selected"
    audit.update(selected=selected, ranked_ns=time.time_ns())
    if len(selected) < count:
        raise ValueError(f"Requested {count} distinct active NBA events, found {len(selected)}")
    return await confirm(api, list(selected), selected)


async def preflight(
    path: Path, explicit: list[str] | None, count: int, *, distinct_events: bool = False
) -> dict:
    path.parent.mkdir(exist_ok=True)
    path.mkdir()
    audit: dict = {}
    try:
        build = build_info()
        if distinct_events:
            if explicit is not None or count not in (2, 3, 4):
                raise ValueError("Distinct-event selection requires a count of 2–4")
            approved_capture(build)
        async with client() as api:
            records = (
                await select_events(api, count, audit)
                if distinct_events
                else await select(api, explicit, count)
            )
        meta = {
            "format": FORMAT,
            "session": uuid.uuid4().hex,
            "prepared_ns": time.time_ns(),
            "markets": [r["ticker"] for r in records],
            "market_checks": records,
            "endpoint": WS_URL,
            "series": SERIES,
            "channels": CHANNELS,
            "build": build,
        }
        if distinct_events:
            meta["selection"] = audit
        exclusive(path / MANIFEST, meta)
        return meta
    except Exception as exc:
        exclusive(
            path / "preflight_failure.json",
            {
                "error_type": type(exc).__name__,
                "reason": str(exc),
                "selection": audit,
                "failed_ns": time.time_ns(),
            },
        )
        raise


async def collect(writer: Writer, dial: Callable, verify: Callable, seconds: float) -> str:
    deadline = time.monotonic() + seconds
    attempts = 0
    while time.monotonic() < deadline and not (writer.path / STOP).exists():
        try:
            checked = await verify()
        except (KalshiClientError, ValueError) as exc:
            writer.record("fault", {"reason": "market_or_auth_check_failed", "detail": str(exc)})
            return "market_check_failed"
        writer.record("market_checks", checked)
        attempts += 1
        writer.record("attempt", attempts)
        try:
            async with dial() as ws:
                writer.record("connected", attempts)
                if writer.feed.connection == 1:
                    deadline = time.monotonic() + seconds
                for i, channel in enumerate(CHANNELS, 1):
                    command = {
                        "id": i,
                        "cmd": "subscribe",
                        "params": {"channels": [channel], "market_tickers": writer.feed.markets},
                    }
                    writer.record("subscribe", command)
                    await ws.send(json.dumps(command))
                start = time.monotonic()
                while time.monotonic() < deadline and not (writer.path / STOP).exists():
                    if time.monotonic() - start > 15 and (
                        len(writer.feed.sids) != 2
                        or len(writer.feed.books) != len(writer.feed.markets)
                    ):
                        writer.record("fault", {"reason": "subscription_or_snapshot_timeout"})
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(decode=False), timeout=0.5)
                    except TimeoutError:
                        writer.flush()
                        continue
                    if writer.record("raw", raw):
                        writer.record("fault", {"reason": "segment_ended_for_recovery"})
                        break
                    writer.flush()
                else:
                    return "operator_stop" if (writer.path / STOP).exists() else "duration"
        except InvalidStatus as exc:
            code = exc.response.status_code
            writer.record(
                "fault",
                {
                    "reason": "authentication_failed" if code in (401, 403) else "handshake_error",
                    "http_status": code,
                },
            )
            if code in (401, 403):
                return "authentication_failed"
        except (TimeoutError, WebSocketException, ConnectionError) as exc:
            writer.record("fault", {"reason": "connection_lost", "error_type": type(exc).__name__})
        except OSError as exc:
            writer.record(
                "fault",
                {"reason": "transport_error", "error_type": type(exc).__name__, "errno": exc.errno},
            )
        writer.flush(force=True)
        if attempts >= 5:
            return "retry_limit"
        await asyncio.sleep(min(2 ** (attempts - 1), 8, max(0, deadline - time.monotonic())))
    return "operator_stop" if (writer.path / STOP).exists() else "duration"


async def capture(path: Path, seconds: float) -> str:
    meta = manifest(path)
    if meta.get("build") != build_info():
        raise ValueError("Pilot build differs from preflight. Prepare a fresh run.")
    markets = meta["markets"]
    expected_events = meta.get("selection", {}).get("selected")
    writer = Writer(path)
    reason = None
    try:
        start_status = windows_clock_status()
        writer.record("started", {"duration_seconds": seconds})
        writer.record("clock_status", {"phase": "before_connection", **start_status})
        writer.flush(force=True)
        async with client() as api:
            reason = await collect(
                writer, lambda: socket(api), lambda: confirm(api, markets, expected_events), seconds
            )
    except asyncio.CancelledError:
        reason = "operator_interrupt"
    except KalshiClientError as exc:
        writer.record("fault", {"reason": "credential_error", "error_type": type(exc).__name__})
        reason = "credential_error"
    except (OSError, RecorderError) as exc:
        with suppress(OSError):
            exclusive(
                path / "recorder-failure.json",
                {"type": type(exc).__name__, "wall_ns": time.time_ns()},
            )
        raise
    finally:
        try:
            if reason is not None:
                writer.record("collection_ended", reason)
                writer.record(
                    "clock_status", {"phase": "after_collection", **windows_clock_status()}
                )
        except BaseException:
            writer.close(None)
            raise
        writer.close(reason)
    return reason
