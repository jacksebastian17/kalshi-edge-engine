"""Manually enroll, observe or inspect the separate Stage 2C NBA study."""

import argparse
import base64
import hashlib
import json
import os
import re
import time
from collections import Counter
from contextlib import suppress
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from kalshi_edge_engine.kalshi.market import timestamp_ms
from tools.microstructure.journal import encoded, exclusive
from tools.paper_activity import game_window
from tools.tipoff_metrics import BASE, measurements, source_hash, verify_scorer

PROTOCOL = "nba-tipoff-books-v1"
ROOT = Path.home() / "kalshi-tipoff-books-research" / PROTOCOL
ORIGIN = "https://external-api.kalshi.com/trade-api/v2"
MINUTE = 60_000_000_000
WINDOWS = {"advance": -120, "near_start": -10, "post_start": 30}
SERIES = "KXNBAGAME"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, record: dict) -> None:
    # Each parent is exclusively claimed by one writer. Publish only complete files.
    part = path.with_suffix(".part")
    if path.exists():
        raise FileExistsError(path)
    exclusive(part, {"record": record, "sha256": sha(encoded(record))})
    part.replace(path)


def load(path: Path) -> dict:
    envelope = json.loads(path.read_bytes())
    record: dict = envelope["record"]
    if sha(encoded(record)) != envelope["sha256"]:
        raise ValueError(f"Study evidence checksum mismatch: {path}")
    return record


def get(
    directory: Path, label: str, endpoint: str, params: dict, client: httpx.Client | None
) -> dict:
    path = directory / (label + ".json")
    if client is not None:
        started, mono = time.time_ns(), time.monotonic_ns()
        wire = b""
        status, error = None, None
        try:
            response = client.get(ORIGIN + endpoint, params=params)
            status, wire = response.status_code, response.content
        except httpx.RequestError as exc:
            error = type(exc).__name__
        completed = time.time_ns()
        record: dict = {
            "endpoint": endpoint,
            "query": params,
            "requested_ns": started,
            "received_ns": completed if status is not None else None,
            "completed_ns": completed,
            "elapsed_ns": time.monotonic_ns() - mono,
            "http_status": status,
            "error": error,
            "body_sha256": sha(wire),
            "body_b64": base64.b64encode(wire).decode("ascii"),
        }
        save(path, record)
    record = load(path)
    wire = base64.b64decode(record.pop("body_b64"), validate=True)
    if (record["endpoint"], record["query"], record["body_sha256"]) != (
        endpoint,
        params,
        sha(wire),
    ):
        raise ValueError(f"Study response identity mismatch: {path}")
    data = {}
    if record["completed_ns"] < record["requested_ns"]:
        record["error"] = "wall_clock_reversed"
    elif record["http_status"] == 200:
        try:
            data = json.loads(wire, parse_float=str)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            record["error"], data = "invalid_json_object", {}
    elif record["http_status"] is not None:
        record["error"] = "http_" + str(record["http_status"])
    return {**record, "file": path.name, "data": data}


def bounds(start_ms: int, window: str) -> tuple[int, int]:
    target = start_ms * 1_000_000 + WINDOWS[window] * MINUTE
    return target - 5 * MINUTE, target + 5 * MINUTE


def iso(ns: int) -> str:
    seconds, fraction = divmod(ns, 10**9)
    return (
        datetime.fromtimestamp(seconds, UTC)
        .replace(microsecond=fraction // 1000)
        .isoformat(timespec="milliseconds")
    )


def prefix(day: date) -> str:
    month = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")[
        day.month - 1
    ]
    return f"{SERIES}-{day.year % 100:02}{month}{day.day:02}"


def nominations(
    directory: Path, day: date, client: httpx.Client | None
) -> tuple[list[str], str | None]:
    events: set[str] = set()
    cursors: set[str] = set()
    query = {"series_ticker": SERIES, "status": "open", "limit": "200"}
    while True:
        receipt = get(directory, f"listing-{len(cursors)}", "/markets", query, client)
        page = receipt["data"]
        rows, cursor = page.get("markets"), page.get("cursor")
        if receipt["error"] or not isinstance(rows, list) or not isinstance(cursor, str):
            return sorted(events), receipt["error"] or "invalid_listing"
        for row in rows:
            event = row.get("event_ticker") if isinstance(row, dict) else None
            if not isinstance(event, str):
                return sorted(events), "invalid_listing_identity"
            if re.fullmatch(prefix(day) + "[A-Z0-9]+", event):
                events.add(event)
        if not cursor:
            return sorted(events), None
        if cursor in cursors:
            return sorted(events), "repeated_cursor"
        cursors.add(cursor)
        query = {**query, "cursor": cursor}


def pair(event: dict, game: str, at_ms: int) -> list[str]:
    markets = event.get("markets")
    if (
        event.get("event_ticker") != game
        or event.get("series_ticker") != SERIES
        or event.get("mutually_exclusive") is not True
        or not isinstance(markets, list)
        or len(markets) != 2
    ):
        return []
    tickers = set()
    teams = set()
    for m in markets:
        if not isinstance(m, dict):
            return []
        ticker, strike = m.get("ticker"), m.get("custom_strike")
        team = strike.get("basketball_team") if isinstance(strike, dict) else None
        if (
            not isinstance(ticker, str)
            or not re.fullmatch(game + "-[A-Z0-9]+", ticker)
            or m.get("event_ticker") != game
            or m.get("market_type") != "binary"
            or m.get("status") != "active"
            or (timestamp_ms(m.get("close_time")) or 0) <= at_ms
            or not isinstance(team, str)
            or not team
        ):
            return []
        teams.add(team)
        tickers.add(ticker)
    return sorted(tickers) if len(tickers) == len(teams) == 2 else []


def milestone_query(game: str) -> dict:
    return {"related_event_ticker": game, "type": "basketball_game", "limit": "100"}


def primary_milestone(data: dict, game: str, milestone_id: str | None) -> dict:
    if not milestone_id or data.get("cursor") != "" or not isinstance(data.get("milestones"), list):
        return {}
    matches = []
    for m in data["milestones"]:
        if (
            not isinstance(m, dict)
            or m.get("category") != "Sports"
            or m.get("type") != "basketball_game"
        ):
            continue
        primary, details = m.get("primary_event_tickers"), m.get("details", {})
        if (
            m.get("id") == milestone_id
            and isinstance(primary, list)
            and game in primary
            and isinstance(details, dict)
            and details.get("main_game_event_ticker", game) == game
        ):
            matches.append(m)
    return matches[0] if len(matches) == 1 else {}


def nominees(directory: Path, day: date, client: httpx.Client | None) -> dict:
    listed, error = nominations(directory, day, client)
    games: dict = {}
    for game in listed[:2] if not error else ():
        sched = get(
            directory,
            game + "-schedule",
            "/milestones",
            milestone_query(game),
            client,
        )
        metadata = get(
            directory, game + "-event", "/events/" + game, {"with_nested_markets": "true"}, client
        )
        schedule = game_window(sched["data"], game, sched["completed_ns"] // 10**9)
        event = metadata["data"].get("event")
        contracts = (
            pair(event, game, metadata["completed_ns"] // 10**6) if isinstance(event, dict) else []
        )
        reason = sched["error"] or metadata["error"]
        if not reason and "scheduled_start_ms" not in schedule:
            reason = schedule["status"]
        if not reason and not primary_milestone(sched["data"], game, schedule.get("milestone_id")):
            reason = "unverified_primary_milestone"
        if not reason and not contracts:
            reason = "unverified_contract_pair"
        games[game] = {
            "contracts": contracts,
            "milestone_id": schedule.get("milestone_id"),
            "start_ms": schedule.get("scheduled_start_ms"),
            "exclusion": reason,
        }
    return {"listed_events": listed, "discovery_error": error, "nominees": games}


def finalize(selection: dict, frozen_ns: int) -> None:
    games = selection["nominees"]
    ids = Counter(g["milestone_id"] for g in games.values() if g["milestone_id"])
    for g in games.values():
        if not g["exclusion"]:
            if ids[g["milestone_id"]] > 1:
                g["exclusion"] = "duplicate_milestone"
            elif frozen_ns >= bounds(g["start_ms"], "advance")[0]:
                g["exclusion"] = "advance_window_already_opened"


def identities(directory: Path, selection: dict, *, claim: bool) -> None:
    registry = directory.parent / "milestones"
    if claim:
        registry.mkdir(exist_ok=True)
    for game, g in selection["nominees"].items():
        if g["exclusion"]:
            continue
        # A venue reschedule or renamed event must not enroll the same milestone twice.
        key = registry / (sha(g["milestone_id"].encode()) + ".json")
        owner = {"game": game, "slate": directory.name, "milestone_id": g["milestone_id"]}
        if claim:
            published = directory / (game + ".owner")
            exclusive(published, owner)
            with suppress(FileExistsError):
                os.link(published, key)
        if json.loads(key.read_bytes()) != owner:
            g["exclusion"] = "milestone_already_enrolled"


def receipt_hashes(directory: Path) -> dict:
    return {
        p.name: sha(p.read_bytes())
        for p in sorted(directory.glob("*.json"))
        if p.name not in ("plan.json", "started.json", "attempt.json", "done.json")
    }


def study_build() -> list[str]:
    verify_scorer()
    return [
        source_hash(Path(__file__).with_name(name))
        for name in ("tipoff_study.py", "tipoff_metrics.py", "paper_activity.py")
    ]


def enrollment_clock(directory: Path, selection: dict, checked: int, frozen: int) -> None:
    marker = load(directory / "started.json")
    start = marker["started_ns"]
    if marker["protocol"] != PROTOCOL or marker["slate"] != directory.name:
        raise ValueError("Unexpected enrollment start identity")
    clocks_valid = start <= checked <= frozen
    for filename in receipt_hashes(directory):
        r = load(directory / filename)
        clocks_valid &= start <= r["requested_ns"] <= r["completed_ns"] <= checked
    if not clocks_valid:
        selection["discovery_error"] = "enrollment_clock_unverified"
        for g in selection["nominees"].values():
            g["exclusion"] = "enrollment_clock_unverified"


def enroll(directory: Path, day: date, client: httpx.Client) -> dict:
    build = study_build()
    directory.mkdir(parents=True)
    save(
        directory / "started.json",
        {"protocol": PROTOCOL, "slate": day.isoformat(), "started_ns": time.time_ns()},
    )
    selection = nominees(directory, day, client)
    checked = time.time_ns()
    enrollment_clock(directory, selection, checked, checked)
    finalize(selection, checked)
    identities(directory, selection, claim=True)
    frozen = time.time_ns()
    if frozen < checked:
        enrollment_clock(directory, selection, checked, frozen)
    finalize(selection, frozen)
    plan = {
        "protocol": PROTOCOL,
        "base": BASE,
        "build": build,
        "slate": day.isoformat(),
        "checked_ns": checked,
        "frozen_ns": frozen,
        "receipt_sha256": receipt_hashes(directory),
        "window_targets_minutes": WINDOWS,
        **selection,
    }
    save(directory / "plan.json", plan)
    return plan


def verified_plan(directory: Path) -> dict:
    plan = load(directory / "plan.json")
    if (
        plan["protocol"],
        plan["base"],
        plan["build"],
        plan["window_targets_minutes"],
        plan["slate"],
    ) != (PROTOCOL, BASE, study_build(), WINDOWS, directory.name):
        raise ValueError("Unexpected Stage 2C enrollment or source")
    selection = nominees(directory, date.fromisoformat(plan["slate"]), None)
    enrollment_clock(directory, selection, plan["checked_ns"], plan["frozen_ns"])
    finalize(selection, plan["checked_ns"])
    identities(directory, selection, claim=False)
    finalize(selection, plan["frozen_ns"])
    if receipt_hashes(directory) != plan["receipt_sha256"]:
        raise ValueError("Enrollment receipt digest mismatch")
    if any(plan[k] != v for k, v in selection.items()):
        raise ValueError("Enrollment does not reproduce from receipts")
    return plan


def slots(directory: Path, plan: dict):
    for game, g in plan["nominees"].items():
        if not g["exclusion"]:
            for window in WINDOWS:
                begin, end = bounds(g["start_ms"], window)
                yield game, g, window, begin, end, directory / (game + "_" + window)


def requests(game: str, contracts: list[str]) -> list[tuple[str, str, dict]]:
    return [
        ("event", "/events/" + game, {"with_nested_markets": "true"}),
        ("series", "/series/" + SERIES, {}),
        *((ticker, "/markets/" + ticker + "/orderbook", {}) for ticker in contracts),
        ("milestone", "/milestones", milestone_query(game)),
    ]


def observe(directory: Path, client: httpx.Client) -> list[str]:
    plan = verified_plan(directory)
    invoked = time.time_ns()
    claimed = []
    for game, g, _window, begin, end, path in slots(directory, plan):
        now = time.time_ns()
        if not begin <= invoked <= now < end:
            continue
        try:
            path.mkdir()
        except FileExistsError:
            continue
        save(
            path / "attempt.json",
            {"plan_sha256": sha(encoded(plan)), "slot": path.name, "claimed_ns": now},
        )
        claimed.append(path.name)
        for label, endpoint, params in requests(game, g["contracts"]):
            if not begin <= time.time_ns() < end:
                break
            get(path, label, endpoint, params, client)
        save(
            path / "done.json",
            {"finished_ns": time.time_ns(), "receipt_sha256": receipt_hashes(path)},
        )
    return claimed


def window_report(path: Path, plan: dict, game: str, window: str, as_of: int) -> dict:
    g = plan["nominees"][game]
    begin, end = bounds(g["start_ms"], window)
    state = "pending" if as_of < begin else "open" if as_of < end else "missed"
    sources: dict = {}
    refs = {}
    attempt = path / "attempt.json"
    claimed_ns = None
    if attempt.is_file():
        claim = load(attempt)
        if (
            claim["slot"] != path.name
            or claim["plan_sha256"] != sha(encoded(plan))
            or not begin <= claim["claimed_ns"] < end
        ):
            raise ValueError("Attempt does not match its frozen game/window")
        if claim["claimed_ns"] <= as_of:
            claimed_ns = claim["claimed_ns"]
            done = path / "done.json"
            seal = load(done) if done.is_file() else None
            finished = seal["finished_ns"] if seal else None
            if seal and receipt_hashes(path) != seal["receipt_sha256"]:
                raise ValueError("Observation receipt digest mismatch")
            state = "attempt_incomplete" if finished is None or finished > as_of else "attempted"
            for label, endpoint, params in requests(game, g["contracts"]):
                if not (path / (label + ".json")).is_file():
                    continue
                rec = get(path, label, endpoint, params, None)
                if rec["completed_ns"] > as_of:
                    continue
                refs[label] = {k: v for k, v in rec.items() if k != "data"}
                if not claimed_ns <= rec["requested_ns"] <= rec["completed_ns"] < end:
                    refs[label]["timing"] = "outside_window_or_clock_reversal"
                elif not rec["error"]:
                    sources[label] = rec["data"]
    elif path.exists() and as_of >= end:
        state = "attempt_incomplete"
    rows = {}
    spreads = []
    for ticker in g["contracts"]:
        r = refs.get(ticker)
        row: dict = {"status": state, "book_receipt_ns": None, "offset_from_start_ns": None}
        if r:
            at = r["received_ns"]
            if at is not None and not r["error"]:
                row.update(book_receipt_ns=at, offset_from_start_ns=at - g["start_ms"] * 10**6)
            if ticker not in sources:
                row["status"] = r.get("timing") or r["error"]
            else:
                event = sources.get("event", {}).get("event")
                series = sources.get("series", {}).get("series")
                # Source inputs must have arrived before this particular book.
                m = measurements(
                    ticker,
                    sources[ticker],
                    event if isinstance(event, dict) and refs["event"]["received_ns"] <= at else {},
                    series
                    if isinstance(series, dict) and refs["series"]["received_ns"] <= at
                    else {},
                    at // 10**6,
                    fee_observed_ms=min(refs[label]["received_ns"] for label in ("event", "series"))
                    // 10**6
                    if "event" in sources and "series" in sources
                    else None,
                )
                row.update(m)
                if m["book_issues"]:
                    row["status"] = "invalid_book"
                elif m["spread_cents"] is None:
                    row["status"] = "missing_best_bid"
                elif Decimal(m["spread_cents"]) < 0:
                    row["status"] = "crossed_book"
                else:
                    row["status"] = "observed"
                    spreads.append(Decimal(m["spread_cents"]))
        elif state == "attempted":
            row["status"] = "window_closed_before_read"
        rows[ticker] = row
    coverage = {2: "complete", 1: "partial", 0: "missing"}[len(spreads)]
    if not spreads and state in ("pending", "open"):
        coverage = state
    milestone = primary_milestone(sources.get("milestone", {}), game, g["milestone_id"])
    reported_state = milestone.get("details", {}).get("status")
    paired_depth = {}
    if len(spreads) == 2:
        for side in ("YES", "NO"):
            paired_depth[side] = str(
                sum(Decimal(row["sides"][side]["same_side_top_contracts"]) for row in rows.values())
                / 2
            )
    return {
        "opens_utc": iso(begin),
        "closes_utc": iso(end),
        "attempt": state,
        "claimed_ns": claimed_ns,
        "coverage": coverage,
        "contracts": rows,
        "receipt_directory": path.name,
        "receipts": refs,
        "milestone_reported_state": reported_state if isinstance(reported_state, str) else None,
        "milestone_start_as_reported": milestone.get("start_date"),
        "milestone_last_updated": milestone.get("last_updated_ts"),
        "paired_mean_top_contracts": paired_depth or None,
        "paired_mean_spread_cents": str(sum(spreads) / 2) if len(spreads) == 2 else None,
    }


def report(directory: Path, as_of: int) -> dict:
    marker = load(directory / "started.json")
    if marker["started_ns"] > as_of:
        raise ValueError("As-of time precedes the enrollment attempt")
    if not (directory / "plan.json").exists():
        return {"protocol": PROTOCOL, "enrollment": "incomplete", "started": marker, "games": {}}
    plan = verified_plan(directory)
    if as_of < plan["frozen_ns"]:
        raise ValueError("As-of time precedes frozen enrollment")
    games: dict = {}
    for game, g, window, _, _, path in slots(directory, plan):
        if game not in games:
            games[game] = {
                "scheduled_start_utc": iso(g["start_ms"] * 10**6),
                "actual_game_state": None,
                "windows": {},
            }
        games[game]["windows"][window] = window_report(path, plan, game, window, as_of)
    return {
        "protocol": PROTOCOL,
        "slate": plan["slate"],
        "as_of_ns": as_of,
        "original_sample_games_contributed": 0,
        "distinct_games": len(games),
        "enrollment": plan,
        "games": games,
        "by_window": summarize(games),
    }


def summarize(games: dict) -> dict:
    summaries = {}
    for window in WINDOWS:
        coverage: Counter[str] = Counter()
        branches: Counter[str] = Counter()
        paired = []
        depths: dict[str, list[Decimal]] = {"YES": [], "NO": []}
        for g in games.values():
            w = g["windows"][window]
            coverage[w["coverage"]] += 1
            branches.update(
                {
                    r["signal_branch"]
                    for r in w["contracts"].values()
                    if r.get("signal_branch") and not r["book_issues"]
                }
            )
            if w["paired_mean_spread_cents"] is not None:
                paired.append(Decimal(w["paired_mean_spread_cents"]))
                for side in depths:
                    depths[side].append(Decimal(w["paired_mean_top_contracts"][side]))
        summaries[window] = {
            "distinct_games": len(games),
            "games_by_coverage": dict(coverage),
            "games_with_observed_branch": dict(branches),
            "paired_games": len(paired),
            "mean_of_paired_game_top_contracts": {
                side: str(sum(values) / len(values)) if values else None
                for side, values in depths.items()
            },
            "mean_of_paired_game_spreads_cents": str(sum(paired) / len(paired)) if paired else None,
        }
    return summaries


def study_report(root: Path, as_of: int) -> dict:
    games: dict = {}
    slates = {}
    for directory in sorted(root.glob("????-??-??")):
        marker = directory / "started.json"
        if not marker.is_file():
            slates[directory.name] = "incomplete_start"
            continue
        if load(marker)["started_ns"] > as_of:
            continue
        if (
            not (directory / "plan.json").is_file()
            or load(directory / "plan.json")["frozen_ns"] > as_of
        ):
            slates[directory.name] = "enrollment_incomplete_as_of"
            continue
        slate = report(directory, as_of)
        if games.keys() & slate["games"].keys():
            raise ValueError("Duplicate enrolled event in study archive")
        games.update(slate["games"])
        slates[directory.name] = slate["enrollment"]
    return {
        "protocol": PROTOCOL,
        "as_of_ns": as_of,
        "slates": slates,
        "games": games,
        "by_window": summarize(games),
        "distinct_games": len(games),
        "original_sample_games_contributed": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("enroll", "observe", "report"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--slate", type=date.fromisoformat, required=name != "report")
        if name == "report":
            cmd.add_argument(
                "--as-of", help="UTC or offset-qualified ISO time for repeatable inspection"
            )
    args = parser.parse_args()
    directory = ROOT / args.slate.isoformat() if args.slate else ROOT
    if directory.resolve() != directory or directory.is_relative_to(
        Path(__file__).resolve().parents[1]
    ):
        parser.error("Expected the dedicated external study directory")
    if args.command == "report":
        as_of = timestamp_ms(args.as_of) if args.as_of else None
        if args.as_of and as_of is None:
            parser.error("--as-of requires a timezone-qualified timestamp")
        cutoff = as_of * 10**6 if as_of is not None else time.time_ns()
        result = report(directory, cutoff) if args.slate else study_report(ROOT, cutoff)
    else:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            if args.command == "enroll":
                enroll(directory, args.slate, client)
                result = report(directory, time.time_ns())
            else:
                result = {"attempted_windows": observe(directory, client)}
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
