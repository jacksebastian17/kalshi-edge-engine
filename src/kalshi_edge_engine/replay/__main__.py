"""Run with python -m kalshi_edge_engine.replay."""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import IO, Any

from kalshi_edge_engine.replay import Mode
from kalshi_edge_engine.replay.archive import REPO, Selection, open_archive
from kalshi_edge_engine.replay.evidence import evidence_audit
from kalshi_edge_engine.replay.execution import POLICIES
from kalshi_edge_engine.replay.forensics import RAPID_INTENTS, investigate
from kalshi_edge_engine.replay.research import analyze


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic offline B1 capture research")
    parser.add_argument(
        "command",
        choices=("inspect", "validate", "replay", "compare", "intent", "report", "evidence"),
    )
    parser.add_argument(
        "--archive",
        required=True,
        type=Path,
        help="Directory containing standalone B1 SQLite snapshots",
    )
    parser.add_argument(
        "--out", type=Path, help="Explicit research directory. Otherwise print JSON to stdout"
    )
    parser.add_argument(
        "--events",
        action="store_true",
        help="Export raw receipts and reconstructed decisions as separate JSON Lines files",
    )
    parser.add_argument("--ticker", action="append", default=[])
    parser.add_argument("--session")
    parser.add_argument("--start-ns", type=int, help="Inclusive local receipt time")
    parser.add_argument("--end-ns", type=int, help="Exclusive local receipt time")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--mode",
        type=Mode,
        choices=list(Mode),
        default=Mode.B2,
        help="b2 reproduces the B2 checkpoint; b1-queue repairs reconstruction; canonical also uses fixed-point books",
    )
    parser.add_argument(
        "--require-execution-consistency",
        action="store_true",
        help="Require unambiguous two-sided depth, prices, quantities, whole capacity and queue liquidity to agree",
    )
    parser.add_argument(
        "--require-raw-consistency",
        action="store_true",
        help="Censor hypothetical entries when B1 and full positive-size ladder tops disagree",
    )
    parser.add_argument("--probability-scale", type=float, default=1.0)
    parser.add_argument("--intent-id")
    args = parser.parse_args(argv)
    if args.command == "evidence" and (
        args.ticker
        or args.session
        or args.start_ns is not None
        or args.end_ns is not None
        or args.events
    ):
        parser.error("Evidence audit verifies the entire paired snapshot")
    if (args.command == "intent") != bool(args.intent_id):
        parser.error("Use intent with --intent-id")
    if args.command == "intent" and (
        args.ticker or args.session or args.start_ns is not None or args.end_ns is not None
    ):
        parser.error("Intent forensics uses its complete archived lifecycle")
    if args.events and (not args.out or args.command not in ("replay", "compare", "report")):
        parser.error("--events requires a replay/compare/report and --out")
    try:
        selection = Selection(
            tuple(sorted(set(args.ticker))), args.session, args.start_ns, args.end_ns
        )
        directory = args.archive.resolve(strict=True)
        output = args.out.resolve() if args.out else None
        if (
            output
            and args.mode != Mode.B2
            and output.is_relative_to(REPO / "research" / "phase-b2")
        ):
            raise ValueError("Use a new B3A output directory to preserve the B2 baseline")
        if output and (
            output.is_relative_to(directory)
            or output == REPO
            or output.is_relative_to(REPO / "src")
        ):
            raise ValueError(
                "Choose a research output directory separate from sources and snapshots"
            )
        with ExitStack() as cleanup:
            pending = []

            def new_output(name: str) -> IO[str]:
                assert output is not None
                output.mkdir(exist_ok=True, parents=True)
                stream = cleanup.enter_context(
                    NamedTemporaryFile(
                        mode="w", encoding="utf-8", newline="\n", dir=output, delete=False
                    )
                )
                tmp = Path(stream.name)
                cleanup.callback(tmp.unlink, missing_ok=True)
                cleanup.callback(stream.close)
                pending.append((stream, tmp, output / name))
                return stream.file

            stream = new_output("observations.jsonl") if args.events else None
            decisions = new_output("reconstructed.jsonl") if args.events else None

            def emit(event: dict[str, Any]) -> None:
                assert stream is not None
                stream.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")

            def emit_decision(event: dict[str, Any]) -> None:
                assert decisions is not None
                decisions.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")

            with open_archive(directory) as archive:
                report = (
                    evidence_audit(archive)
                    if args.command == "evidence"
                    else analyze(
                        archive,
                        selection,
                        seed=args.seed,
                        mode=args.mode,
                        strict=args.require_execution_consistency,
                        require_raw_consistency=args.require_raw_consistency,
                        scale=args.probability_scale,
                        models=("baseline",) if args.command == "replay" else tuple(POLICIES),
                        inspect_only=args.command in ("inspect", "validate", "intent"),
                        emit=emit if stream else None,
                        emit_decision=emit_decision if decisions else None,
                    )
                )
                if args.command == "intent":
                    report["intent"] = investigate(archive, args.intent_id, args.mode)
                elif args.command == "report":
                    report["rapid_trade_audits_full_archive"] = [
                        investigate(archive, k, args.mode) for k in RAPID_INTENTS
                    ]
            if output:
                report_file = new_output(args.command + ".json")
                json.dump(
                    report, report_file, sort_keys=True, allow_nan=False, separators=(",", ":")
                )
                report_file.write("\n")
                for file, tmp, target in pending:
                    file.close()
                    os.replace(tmp, target)
                if args.command == "evidence":
                    print(f"Evidence audit: {output / 'evidence.json'}")
                    return 0
                c = report["coverage"]
                print(
                    f"{args.command}: {c['coverage']}, {c['selected_events']} selected of {c['total_events']} retained receipts. {output / (args.command + '.json')}"
                )
                for name, m in report.get("models", {}).items():
                    print(
                        f"{name}: {m['hypothetical_fills']} hypothetical fills, {m['markout_samples']} five-second marks."
                    )
            else:
                print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, ArithmeticError) as exc:
        print(f"Replay failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
