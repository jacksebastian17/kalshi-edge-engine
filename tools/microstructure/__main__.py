import argparse
import asyncio
import json
import sys

from .journal import STOP, backup, exclusive, health, inspect, manifest, run_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only NBA public WebSocket pilot")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "capture", "status", "stop", "backup", "report"):
        sub = commands.add_parser(name)
        sub.add_argument("--run", required=True)
        if name == "preflight":
            group = sub.add_mutually_exclusive_group()
            group.add_argument("--tickers", nargs="+")
            group.add_argument("--count", type=int, choices=(2, 3, 4), default=2)
        elif name == "capture":
            sub.add_argument("--minutes", type=int, choices=range(10, 16), default=12)
        elif name == "report":
            sub.add_argument(
                "--unsealed-prefix",
                action="store_true",
                help="Inspect the original journal prefix, including interrupted runs",
            )
    arg = parser.parse_args()
    path = run_path(arg.run)
    result: object
    code = 0
    if arg.command in ("capture", "preflight"):
        from .pilot import capture, preflight

        if arg.command == "preflight":
            result = asyncio.run(preflight(path, arg.tickers, arg.count))
        else:
            result = asyncio.run(capture(path, arg.minutes * 60))
            code = int(result not in ("duration", "operator_stop", "operator_interrupt"))
    elif arg.command == "backup":
        result = str(backup(path))
    elif arg.command == "status":
        result = health(path)
    elif arg.command == "stop":
        manifest(path)
        if not (path / "messages.jsonl").exists() or (path / "seal.json").exists():
            raise ValueError("Pilot is not recording")
        exclusive(path / STOP, {"operator_stop": True})
        result = "stop requested"
    else:
        result = inspect(
            path if arg.unsealed_prefix else path / "backup", require_seal=not arg.unsealed_prefix
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
