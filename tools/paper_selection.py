"""One prospective, read-only scanner pass for the separate strategy-aligned paper cohort."""

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

from kalshi_edge_engine.evidence import Receipt, receipt_scope
from kalshi_edge_engine.kalshi.client import KalshiClient, KalshiClientError
from tools.capture_builds import approved_capture
from tools.microstructure import journal
from tools.paper_activity import (
    ACTIVE,
    SECOND,
    START_LAG_SECONDS,
    START_LEAD_SECONDS,
    TRADE_SECONDS,
    entry_check,
    qualify_market,
    verify_activity,
)

ALIGNED = "b3b2-paper-strategy-aligned-v1"
# Canonical journal encoding of the effective B3B1 policy in the first sealed Stage 2B3 pilot.
POLICY_SHA = "169928603419ad0fa3bfd0d91ceb2934a547f2a06d3b04ceb8ee74dac5190efa"


def fingerprint(data: dict) -> str:
    return hashlib.sha256(journal.encoded(data)).hexdigest()


def frozen_runtime() -> dict:
    # The CLI sets the dedicated paper profile before runtime configuration is imported.
    from kalshi_edge_engine import overnight

    policy = overnight.paper_policy()
    digest = fingerprint(policy)
    if digest != POLICY_SHA:
        raise ValueError("Effective policy differs from the frozen first paper-link pilot")
    return {
        **overnight.revision(),
        "policy_sha256": digest,
        "policy": json.loads(journal.encoded(policy)),
    }


class SelectionReceipts:
    """Retain public response bytes, including discovery pages and shared cached fee inputs."""

    def __init__(self, session: str, rows: list[dict]):
        self.session = session
        self.rows = rows

    def capture(self, path, lane, received_ns, _data, body, base_url) -> Receipt:
        parts = path.strip("/").split("/")
        number = len(self.rows) + 1
        self.rows.append(
            {
                "number": number,
                "path": path,
                "lane": lane,
                "base_url": base_url,
                "received_ns": received_ns,
                "mono_ns": time.monotonic_ns(),
                "sha256": hashlib.sha256(body).hexdigest(),
                "body_b64": base64.b64encode(body).decode("ascii"),
            }
        )
        return Receipt(
            self.session,
            number,
            received_ns,
            parts[1] if len(parts) > 1 else "",
            parts[-1],
            "preflight",
            "NBA",
            None,
            "captured",
        )


def rank(candidates: list[dict], count: int) -> dict[str, str]:
    if count not in (2, 3, 4):
        raise ValueError("Choose two to four distinct games")
    selected: dict[str, str] = {}
    ranked = sorted((r for r in candidates if r["eligible"]), key=lambda r: r["ticker"])
    for index, row in enumerate(ranked, 1):
        game = row["event_ticker"]
        row["rank"] = index
        if game in selected.values():
            row["reason"] = "event_already_selected"
        elif len(selected) == count:
            row["reason"] = "below_selection_limit"
        else:
            selected[row["ticker"]] = game
            row["reason"] = "selected"
    return selected


async def scan(api: KalshiClient, audit: dict, progress: BinaryIO | None = None) -> list[dict]:
    from kalshi_edge_engine.engine.paper import build_quote_intents
    from kalshi_edge_engine.engine.signal import SIGNAL_VALID
    from kalshi_edge_engine.runtime import config, scanner_runtime, state
    from kalshi_edge_engine.runtime.market_fees import MarketFees
    from tools.microstructure import pilot

    rows = audit["candidates"]
    active = audit["rule"] == ACTIVE
    schedules: dict = {}
    receipts = SelectionReceipts(audit["session"], audit["receipts"])
    cfg = config.get_effective_paper_config("NBA")
    with (
        patch.object(api, "recorder", receipts),
        patch.object(state, "market_fees", MarketFees(api)),
    ):
        with receipt_scope() as discovery:
            tickers, owners, _, _ = await scanner_runtime._discover_enabled_universe_markets(api)
        audit["discovery_receipts"] = [r.sequence for r in discovery]
        if active:
            audit["discovered"] = sorted(tickers)
        if not discovery:
            raise ValueError("Expected fresh scanner discovery receipts")

        async def evaluate(ticker: str) -> None:
            row = {
                "ticker": ticker,
                "eligible": False,
                "rank": None,
                "started_ns": time.time_ns(),
                "proposals": [],
                "source_receipts": [],
            }
            rows.append(row)
            if owners[ticker] != "NBA" or not ticker.startswith(pilot.SERIES + "-"):
                row["reason"] = "outside_nba_game_series"
                return
            first = len(receipts.rows)
            try:
                signal = await scanner_runtime._score_market(api, ticker, "NBA")
            except (KalshiClientError, ValueError) as exc:
                row.update(
                    reason="score_failed",
                    error_type=type(exc).__name__,
                    source_receipts=[r["number"] for r in receipts.rows[first:]],
                )
                return
            row.update(
                scored_ns=signal.scored_ns,
                signal_reason=signal.reason_code,
                source_receipts=[r.sequence for r in signal.receipts],
            )
            fee = signal.fee_profile
            if fee is None or not fee.series_aware:
                row["reason"] = "fee_unavailable"
                return
            row.update(fee=fee.audit(), event_ticker=fee.event_ticker)
            if fee.series_ticker != pilot.SERIES or fee.event_ticker != ticker.rpartition("-")[0]:
                row["reason"] = "inconsistent_event"
                return
            # Bind the exact market and book that supplied this score, including cached fee refs.
            sources = {
                receipts.rows[r.sequence - 1]["path"]: receipts.rows[r.sequence - 1]
                for r in signal.receipts
            }
            m = sources.get("/markets/" + ticker)
            if not m or "/markets/" + ticker + "/orderbook" not in sources:
                row["reason"] = "missing_source_receipts"
                return
            payload = json.loads(base64.b64decode(m["body_b64"]))
            market = payload.get("market", payload)
            if not pilot.active(market, ticker) or market.get("event_ticker") != fee.event_ticker:
                row["reason"] = "inactive_or_inconsistent_market"
                return
            if cfg.require_signal_valid_only and signal.reason_code != SIGNAL_VALID:
                row["reason"] = "signal_not_valid"
                return
            proposals = []
            for i in build_quote_intents(signal):
                proposal = {
                    "side": i.side,
                    "price_cents": i.quote_price_cents,
                    "contracts": i.size_contracts,
                    "side_enabled": cfg.allow_yes_entries
                    if i.side == "YES"
                    else cfg.allow_no_entries,
                }
                if active:
                    proposal.update(await entry_check(signal, i, api))
                proposals.append(proposal)
            row.update(
                proposals=proposals,
                eligible=any(p["side_enabled"] for p in proposals),
                reason="no_enabled_proposal" if proposals else "no_proposal",
            )
            if active:
                await qualify_market(api, row, audit["activity_cutoff_s"], schedules)

        checkpoint = 0
        for ticker in sorted(tickers):
            await evaluate(ticker)
            if progress is not None:
                progress.write(
                    journal.encoded({"candidate": rows[-1], "receipts": receipts.rows[checkpoint:]})
                )
                progress.flush()
                os.fsync(progress.fileno())
                checkpoint = len(receipts.rows)
        selected = rank(rows, audit["count"])
        audit.update(selected=selected, ranked_ns=time.time_ns())
        if len(selected) != audit["count"]:
            raise ValueError(f"Requested {audit['count']} distinct games; found {len(selected)}")
        with receipt_scope() as confirmation:
            try:
                return await pilot.confirm(api, list(selected), selected)
            finally:
                audit["confirmation_receipts"] = [r.sequence for r in confirmation]


def plan_path(campaign: Path, tape_name: str) -> Path:
    return campaign / "preflights" / (tape_name + ".json")


async def preflight(
    campaign: Path, tape: Path, count: int, code_sha: str, protocol: str = ALIGNED
) -> None:
    from tools.microstructure import pilot

    if count not in (2, 3, 4) or tape.resolve() != tape or campaign.resolve() != campaign:
        raise ValueError("Expected a fresh unlinked preflight for two to four games")
    plan = plan_path(campaign, tape.name)
    if plan.resolve() != plan or plan.exists():
        raise ValueError("Expected an unused selection audit path")
    plan.parent.mkdir(parents=True, exist_ok=True)
    tape.mkdir(parents=True)
    audit: dict = {
        "rule": protocol,
        "count": count,
        "session": uuid.uuid4().hex,
        "run": tape.name,
        "started_ns": time.time_ns(),
        "code_sha256": code_sha,
        "receipts": [],
        "candidates": [],
    }
    if protocol == ACTIVE:
        audit.update(
            activity_cutoff_s=audit["started_ns"] // SECOND,
            trade_lookback_s=TRADE_SECONDS,
            before_start_s=START_LEAD_SECONDS,
            after_start_s=START_LAG_SECONDS,
        )
        journal.exclusive(
            plan.with_suffix(".started.json"),
            {k: audit[k] for k in ("rule", "run", "session", "started_ns", "code_sha256", "count")},
        )
    try:
        audit["runtime"] = frozen_runtime()
        build = pilot.build_info()
        approved_capture(build)
        async with pilot.client() as api:
            with (
                plan.with_suffix(".progress.jsonl").open("xb")
                if protocol == ACTIVE
                else nullcontext()
            ) as progress:
                checks = await scan(api, audit, progress)
        if protocol == ACTIVE:
            verify_activity(audit)
    except (Exception, asyncio.CancelledError) as exc:
        audit.update(status="failed", failure=str(exc), finished_ns=time.time_ns())
        journal.exclusive(plan, audit)
        raise
    audit.update(status="frozen", finished_ns=time.time_ns())
    journal.exclusive(plan, audit)
    journal.exclusive(
        tape / journal.MANIFEST,
        {
            "format": journal.FORMAT,
            "session": audit["session"],
            "prepared_ns": time.time_ns(),
            "markets": list(audit["selected"]),
            "market_checks": checks,
            "endpoint": pilot.WS_URL,
            "series": pilot.SERIES,
            "channels": pilot.CHANNELS,
            "build": build,
            "paper_link_cohort": protocol,
            "selection": {
                "rule": protocol,
                "selected": audit["selected"],
                "audit_sha256": journal.digest(plan),
            },
        },
    )


def verify_plan(
    campaign: Path,
    run: str,
    session: str,
    markets: dict,
    code: str,
    digest: str,
    protocol: str = ALIGNED,
) -> dict:
    path = plan_path(campaign, run)
    raw = path.read_bytes()
    plan: dict = json.loads(raw)
    if (
        hashlib.sha256(raw).hexdigest() != digest
        or plan["rule"] != protocol
        or plan["status"] != "frozen"
        or plan["run"] != run
        or plan["session"] != session
        or plan["code_sha256"] != code
        or plan["runtime"]["policy_sha256"] != POLICY_SHA
        or fingerprint(plan["runtime"]["policy"]) != POLICY_SHA
        or plan["selected"] != markets
        or len(markets) != plan["count"]
    ):
        raise ValueError("Invalid frozen strategy selection")
    if protocol == ACTIVE:
        verify_activity(plan)
    ranked = [dict(r) for r in plan["candidates"]]
    if rank(ranked, plan["count"]) != markets or ranked != plan["candidates"]:
        raise ValueError("Frozen selection differs from the prospective ranking rule")
    return plan
