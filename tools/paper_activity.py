"""Frozen prospective sampling inputs for the entry-selective, recently active cohort."""

import base64
import hashlib
import json
from decimal import InvalidOperation
from typing import Any
from unittest.mock import patch

from kalshi_edge_engine.evidence import receipt_scope
from kalshi_edge_engine.kalshi.client import KalshiClient, KalshiClientError
from kalshi_edge_engine.kalshi.market import timestamp_ms
from tools.microstructure.feed import fixed

ACTIVE = "b3b2-paper-entry-active-v1"
SECOND = 10**9
TRADE_SECONDS = 300
START_LEAD_SECONDS = 3600
START_LAG_SECONDS = 3 * 3600
LAUNCH_SECONDS = 600
STATIC_REJECTIONS = frozenset(
    (
        "side_disabled",
        "missing_entry_selectivity_metric",
        "min_capturable_edge_to_cross",
        "min_abs_distance_from_50",
        "min_top_liquidity",
        "min_edge",
    )
)


class PortfolioBoundary(Exception):
    """Reached the first state-dependent check of the pinned B3B1 entry function."""


class PreflightStore:
    def count_intents_by_status(self, _status: str) -> int:
        raise PortfolioBoundary


async def entry_check(signal: Any, intent: Any, api: KalshiClient) -> dict:
    """Exercise the real gates on one already-built quote, in the isolated preflight process."""
    from kalshi_edge_engine.runtime import paper_runtime, state

    reasons = []

    def skip(message: str, *, extra: dict) -> None:
        if message == "paper_intent_skipped":
            reasons.append(extra["reason"])

    with (
        patch.object(state, "paper_store", PreflightStore()),
        patch.object(state, "paper_simulation_tasks", set()),
        patch.object(state, "paper_enabled", True),
        patch.object(paper_runtime, "build_quote_intents", lambda _: (intent,)),
        patch.object(paper_runtime.logger, "info", skip),
    ):
        try:
            await paper_runtime._process_paper_intents(signal, api)
        except PortfolioBoundary:
            reason = None
        else:
            if len(reasons) != 1 or reasons[0] not in STATIC_REJECTIONS:
                raise ValueError("Frozen deterministic entry boundary could not be established")
            reason = reasons[0]
    return {
        "deterministic_eligible": reason is None,
        "static_rejection": reason,
        "net_edge_cents": intent.net_edge,
        "top_liquidity": signal.top_liquidity,
        "capturable_edge_yes_cents": signal.capturable_edge_to_cross_yes_cents,
        "intended_yes_quote_cents": signal.intended_quote_yes_cents,
    }


async def public_read(api: KalshiClient, path: str, params: dict, evidence: dict) -> dict | None:
    evidence.update(path=path, query=params)
    with receipt_scope() as refs:
        try:
            data = await api._authenticated_request("GET", path, params)
        except (KalshiClientError, ValueError) as exc:
            evidence["error"] = type(exc).__name__
            return None
    if len(refs) != 1:
        evidence["error"] = "missing_public_receipt"
        return None
    evidence["receipt"] = refs[0].sequence
    return data


def game_window(data: dict | None, game: str, cutoff: int) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("milestones"), list):
        return {"status": "schedule_unavailable"}
    matches = [
        m
        for m in data["milestones"]
        if isinstance(m, dict)
        and m.get("type") == "basketball_game"
        and m.get("category") == "Sports"
        and any(
            isinstance(m.get(k), list) and game in m[k]
            for k in ("primary_event_tickers", "related_event_tickers")
        )
    ]
    if data.get("cursor") or len(matches) != 1:
        return {
            "status": "schedule_ambiguous" if matches or data.get("cursor") else "schedule_missing"
        }
    m = matches[0]
    start, end = timestamp_ms(m.get("start_date")), timestamp_ms(m.get("end_date"))
    if (
        not isinstance(m.get("id"), str)
        or not m["id"]
        or start is None
        or (m.get("end_date") is not None and (end is None or end < start))
    ):
        return {"status": "invalid_schedule"}
    now = cutoff * 1000
    near = start - START_LEAD_SECONDS * 1000 <= now <= start + START_LAG_SECONDS * 1000
    return {
        "milestone_id": m["id"],
        "scheduled_start_ms": start,
        "scheduled_end_ms": end,
        "status": "in_window" if near and (end is None or now < end) else "outside_game_window",
    }


def trade_activity(data: dict | None, ticker: str, cutoff: int) -> dict:
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("trades"), list)
        or not isinstance(data.get("cursor"), str)
    ):
        return {"status": "activity_unavailable"}
    for trade in data["trades"]:
        if not isinstance(trade, dict):
            continue
        stamp = timestamp_ms(trade.get("created_time"))
        try:
            quantity = fixed(trade.get("count_fp"), 8)
        except (ValueError, InvalidOperation):
            continue
        if (
            trade.get("ticker") == ticker
            and isinstance(trade.get("trade_id"), str)
            and trade["trade_id"]
            and trade.get("is_block_trade") is False
            and quantity > 0
            and stamp is not None
            and (cutoff - TRADE_SECONDS) * 1000 <= stamp < cutoff * 1000
        ):
            return {
                "status": "recent_print",
                "trade_id": trade["trade_id"],
                "created_time": trade["created_time"],
                "contracts": str(quantity),
            }
    # A malformed or partial page cannot establish a quiet market.
    return {
        "status": "activity_unverified"
        if data["trades"] or data.get("cursor")
        else "no_recent_print"
    }


async def qualify_market(api: KalshiClient, row: dict, cutoff: int, schedules: dict) -> None:
    row["eligible"] = any(p["deterministic_eligible"] for p in row["proposals"])
    if not row["eligible"]:
        row["reason"] = "no_deterministically_eligible_proposal"
        return
    game = row["event_ticker"]
    if game not in schedules:
        evidence: dict = {}
        data = await public_read(
            api,
            "/milestones",
            {"related_event_ticker": game, "type": "basketball_game", "limit": 100},
            evidence,
        )
        schedules[game] = {**evidence, **game_window(data, game, cutoff)}
    row["schedule"] = schedules[game]
    if row["schedule"]["status"] != "in_window":
        row.update(eligible=False, reason=row["schedule"]["status"])
        return
    query = {
        "ticker": row["ticker"],
        "min_ts": cutoff - TRADE_SECONDS,
        "max_ts": cutoff,
        "is_block_trade": "false",
        "limit": 100,
    }
    activity: dict = {}
    page = await public_read(api, "/markets/trades", query, activity)
    activity.update(trade_activity(page, row["ticker"], cutoff))
    row["activity"] = activity
    row.update(eligible=activity["status"] == "recent_print", reason=activity["status"])


def verify_activity(plan: dict) -> None:
    """Replay timing and activity eligibility from the preselection response bytes."""
    cutoff = plan["activity_cutoff_s"]
    if (
        cutoff != plan["started_ns"] // SECOND
        or plan["trade_lookback_s"] != TRADE_SECONDS
        or plan["before_start_s"] != START_LEAD_SECONDS
        or plan["after_start_s"] != START_LAG_SECONDS
    ):
        raise ValueError("Activity policy differs from frozen protocol")

    def payload(observation: dict, path: str, query: dict) -> dict | None:
        if observation["path"] != path or observation["query"] != query:
            raise ValueError("Activity query differs from frozen protocol")
        if "error" in observation:
            return None
        number = observation["receipt"]
        if type(number) is not int or not 1 <= number <= len(plan["receipts"]):
            raise ValueError("Missing activity receipt")
        receipt = plan["receipts"][number - 1]
        body = base64.b64decode(receipt["body_b64"], validate=True)
        if (
            receipt["number"] != number
            or receipt["path"] != path
            or not plan["started_ns"] <= receipt["received_ns"] <= plan["ranked_ns"]
            or hashlib.sha256(body).hexdigest() != receipt["sha256"]
        ):
            raise ValueError("Invalid preselection receipt")
        data: dict = json.loads(body)
        return data

    for row in plan["candidates"]:
        eligible = any(p["side_enabled"] and p["deterministic_eligible"] for p in row["proposals"])
        if eligible:
            game, schedule = row["event_ticker"], row["schedule"]
            query = {"related_event_ticker": game, "type": "basketball_game", "limit": 100}
            result = game_window(payload(schedule, "/milestones", query), game, cutoff)
            if any(schedule.get(k) != v for k, v in result.items()):
                raise ValueError("Game timing does not reproduce")
            eligible = result["status"] == "in_window"
        if eligible:
            ticker, activity = row["ticker"], row["activity"]
            query = {
                "ticker": ticker,
                "min_ts": cutoff - TRADE_SECONDS,
                "max_ts": cutoff,
                "is_block_trade": "false",
                "limit": 100,
            }
            result = trade_activity(payload(activity, "/markets/trades", query), ticker, cutoff)
            if any(activity.get(k) != v for k, v in result.items()):
                raise ValueError("Preselection trade evidence does not reproduce")
            eligible = result["status"] == "recent_print"
        if eligible != row["eligible"]:
            raise ValueError("Activity eligibility does not reproduce")
