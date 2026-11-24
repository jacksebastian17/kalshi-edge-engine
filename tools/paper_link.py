"""Explicit, one-session Stage 2B3 enrollment, paper-only pilot and offline reporting."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from tools.capture_builds import approved_capture
from tools.microstructure import journal
from tools.paper_activity import ACTIVE, LAUNCH_SECONDS, SECOND
from tools.paper_bridge import ENROLLMENT, EVENTS, PROTOCOL, SEAL, Sidecar, observe
from tools.paper_selection import ALIGNED, frozen_runtime, preflight, verify_plan
from tools.shadow_feasibility import market_events, publish

ROOT = Path.home() / "kalshi-paper-link-research"
ALIGNED_ROOT = Path.home() / "kalshi-paper-aligned-research"
ACTIVE_ROOT = Path.home() / "kalshi-paper-entry-active-research"
TARGETED = (ALIGNED, ACTIVE)


def version_digest(protocol: str = PROTOCOL) -> str:
    digest = hashlib.sha256()
    for name in (
        "paper_bridge",
        "paper_link",
        "paper_link_report",
        "paper_link_marks",
        "shadow_tape",
        "shadow_markouts",
        "shadow_feasibility",
        "markout_fees",
        *(("paper_selection",) if protocol in TARGETED else ()),
        *(("paper_activity",) if protocol == ACTIVE else ()),
    ):
        body = Path(__file__).with_name(name + ".py").read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + body + b"\0")
    return digest.hexdigest()


def enroll(campaign: Path, tape_dir: Path, protocol: str = PROTOCOL) -> Path:
    if {p.name for p in tape_dir.iterdir()} != {journal.MANIFEST}:
        raise ValueError("Enroll the prepared tape before capture starts")
    meta = journal.manifest(tape_dir)
    approved_capture(meta["build"])
    events = market_events(meta["market_checks"], meta["markets"])
    selection = meta.get("selection", {})
    targeted = protocol in TARGETED
    if (targeted or selection.get("rule") in TARGETED) and selection.get("rule") != protocol:
        raise ValueError("Selection belongs to a different paper cohort")
    code = version_digest(protocol)
    if targeted:
        verify_plan(
            campaign,
            tape_dir.name,
            meta["session"],
            events,
            code,
            selection["audit_sha256"],
            protocol,
        )
    if (
        len(set(events.values())) != len(events)
        or meta.get("selection", {}).get("selected") != events
    ):
        raise ValueError("Use prospective --distinct-events preflight for 2–4 different games")
    dest = campaign / "sessions" / tape_dir.name
    if dest.resolve() != dest or tape_dir.resolve() != tape_dir:
        raise ValueError("Use unlinked research directories")
    dest.mkdir(parents=True)
    journal.exclusive(
        dest / ENROLLMENT,
        {
            "protocol": protocol,
            "code_sha256": code,
            **({"selection_sha256": selection["audit_sha256"]} if targeted else {}),
            "paper_session": uuid.uuid4().hex,
            "enrolled_wall_ns": time.time_ns(),
            "tape_run": tape_dir.name,
            "tape_session": meta["session"],
            "manifest_sha256": journal.digest(tape_dir / journal.MANIFEST),
            "markets": events,
        },
    )
    return dest


def configure_paper_profile() -> None:
    os.environ.update(
        PAPER_EXPERIMENT_ID="decision_linked_b3b1_v1",
        ENABLE_PAPER_TRADING="true",
        ENABLE_LIVE_TRADING="false",
        ENABLE_NBA_SCANNER="false",
        PAPER_ALLOW_PRICE_IMPROVES="false",
        PAPER_ALLOW_MOVED_THROUGH="false",
    )


async def collect(directory: Path, tape_dir: Path, minutes: int, protocol: str = PROTOCOL) -> None:
    if directory.resolve() != directory or tape_dir.resolve() != tape_dir:
        raise ValueError("Use unlinked research directories")
    enrollment = json.loads((directory / ENROLLMENT).read_bytes())
    if (
        enrollment["protocol"] != protocol
        or enrollment["code_sha256"] != version_digest(protocol)
        or enrollment["tape_run"] != tape_dir.name
        or enrollment["manifest_sha256"] != journal.digest(tape_dir / journal.MANIFEST)
        or {p.name for p in tape_dir.iterdir()} != {journal.MANIFEST}
    ):
        raise ValueError("Expected the enrolled, unused preflight and protocol")
    configure_paper_profile()
    if protocol in TARGETED:
        plan = verify_plan(
            directory.parent.parent,
            tape_dir.name,
            enrollment["tape_session"],
            enrollment["markets"],
            enrollment["code_sha256"],
            enrollment["selection_sha256"],
            protocol,
        )
        if (
            protocol == ACTIVE
            and not 0
            <= time.time_ns() - plan["activity_cutoff_s"] * SECOND
            <= LAUNCH_SECONDS * SECOND
        ):
            raise ValueError(
                "The active-cohort preflight launch window has elapsed or its clock is invalid"
            )
        if plan["runtime"] != frozen_runtime():
            raise ValueError("Scanner policy or source changed after selection")
    paper = directory / "paper"
    paper.mkdir()  # Refuse retries against a prior ledger.
    os.environ.update(
        PAPER_DB_PATH=str(paper / "paper_trading.db"),
        MARKET_RECORDER_DB=str(paper / "market_data_recorder.db"),
        ENABLE_MARKET_DATA_RECORDER="true",
        MARKET_RECORDER_APPEND_ONLY="true",
        MARKET_RECORDER_MAX_MIB="4096",
    )
    # Runtime config reads its environment on import. This is a dedicated CLI process.
    from starlette.requests import Request

    from kalshi_edge_engine import overnight
    from kalshi_edge_engine.api import main as api
    from kalshi_edge_engine.runtime import state
    from tools.microstructure import pilot

    sink = Sidecar(directory, enrollment)
    try:
        sink.write(
            "opened",
            enrollment_sha256=journal.digest(directory / ENROLLMENT),
            clock="time.monotonic_ns",
            implementation=time.get_clock_info("monotonic").implementation,
        )
        await overnight.preflight()
        async with api.lifespan_production(api.app):
            rec = api.app.state.recorder
            assert rec and state.paper_store and state.market_fees
            sink.write("paper_ready", rest_session=rec.session)
            with observe(
                sink,
                state.paper_store,
                rec,
                state.market_fees,
                enrollment["markets"],
                track_scanner=protocol in TARGETED,
                track_selectivity=protocol == ACTIVE,
            ):
                # No HTTP server is exposed. Only normal local paper/scanner lifecycle calls.
                began = time.monotonic_ns()
                async with asyncio.TaskGroup() as tasks:
                    ws = tasks.create_task(pilot.capture(tape_dir, minutes * 60))
                    paper_start = await api.start_paper_trading()
                    scanner_start = await api.start_scanner(
                        Request({"type": "http", "app": api.app})
                    )
                    sink.write("paper_started", paper=paper_start, scanner=scanner_start)
                    if (
                        not (
                            paper_start["started"] or paper_start.get("reason") == "already_running"
                        )
                        or not scanner_start["started"]
                    ):
                        raise RuntimeError("Paper strategy or scanner did not start")
                    reason = await ws
                    ended = time.monotonic_ns()
                    sink.write(
                        "tape_returned",
                        began=began,
                        ended=ended,
                        reason=reason,
                        messages_sha256=journal.digest(tape_dir / journal.LOG),
                        manifest_sha256=enrollment["manifest_sha256"],
                        tape_session=enrollment["tape_session"],
                    )
                    await api.stop_scanner(Request({"type": "http", "app": api.app}))
                    await api.stop_paper_trading()
                    sink.write(
                        "paper_stopped",
                        last_scan_ms=state.scanner_state.last_scan_ms,
                        scanner_error=state.scanner_state.last_error,
                        recorder_healthy=rec.status()["healthy"],
                    )
    finally:
        sink.close()
    if sink.failed:
        raise ValueError("Sidecar incomplete; retained prefix can be inspected")
    journal.backup(tape_dir)
    backup = overnight.backup()
    journal.exclusive(
        directory / SEAL,
        {
            "protocol": protocol,
            "events_sha256": journal.digest(directory / EVENTS),
            "records": sink.count,
            "enrollment_sha256": journal.digest(directory / ENROLLMENT),
            "paper_backup": backup.name,
            "experiment_sha256": journal.digest(backup / overnight.MANIFEST),
            "paper_hashes": {
                n: journal.digest(backup / n)
                for n in ("paper_trading.db", "market_data_recorder.db")
            },
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight-aligned", "preflight-active", "enroll", "collect", "report"):
        p = commands.add_parser(name)
        p.add_argument("--campaign", required=True)
        if name.startswith("preflight-"):
            p.add_argument("--count", type=int, choices=(2, 3, 4), default=2)
        else:
            cohort = p.add_mutually_exclusive_group()
            cohort.add_argument(
                "--aligned", action="store_true", help="Use only the strategy-aligned cohort"
            )
            cohort.add_argument(
                "--active", action="store_true", help="Use only the entry-selective active cohort"
            )
        if name != "report":
            p.add_argument("--run", required=True)
        if name == "collect":
            p.add_argument("--minutes", type=int, choices=range(10, 16), default=12)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,72}", args.campaign):
        parser.error("Invalid campaign name")
    if args.command == "preflight-active" or getattr(args, "active", False):
        protocol, root = ACTIVE, ACTIVE_ROOT
    elif args.command == "preflight-aligned" or getattr(args, "aligned", False):
        protocol, root = ALIGNED, ALIGNED_ROOT
    else:
        protocol, root = PROTOCOL, ROOT
    campaign = root / args.campaign
    if campaign.resolve() != campaign:
        parser.error("Expected an unlinked research directory")
    if args.command == "report":
        from tools.paper_link_report import campaign_report

        summary, rows = campaign_report(campaign, protocol=protocol)
        output = campaign / "reports" / uuid.uuid4().hex
        if output.resolve() != output:
            parser.error("Use an unlinked report directory")
        publish(output, summary, rows, True)
        print(output)
    else:
        tape = journal.run_path(args.run)
        if args.command.startswith("preflight-"):
            configure_paper_profile()
            asyncio.run(preflight(campaign, tape, args.count, version_digest(protocol), protocol))
            print(enroll(campaign, tape, protocol))
        elif args.command == "enroll":
            print(enroll(campaign, tape, protocol))
        else:
            directory = campaign / "sessions" / tape.name
            asyncio.run(collect(directory, tape, args.minutes, protocol))
            print(directory)


if __name__ == "__main__":
    main()
