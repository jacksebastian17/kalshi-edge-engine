# Phase B1 execution diagnostics and passive recording

## Reading the fill funnel

The instrumented cohort starts with entries inserted by B1. Each entry keeps its side-specific submission book, displayed sizes, modeled fees, effective execution settings and subsequent simulator polls. A missing submission book has an error field. The per-intent endpoint returns stored history and ledger fills. The older debug endpoint evaluates a **current** book and labels its response accordingly.

```powershell
$api = 'http://127.0.0.1:8000'
Invoke-RestMethod "$api/paper/diagnostics/fill-funnel" | ConvertTo-Json -Depth 5
$expired = (Invoke-RestMethod "$api/paper/intents?status=EXPIRED&limit=1").intents
if ($expired) {
    $id = [uri]::EscapeDataString($expired[0].id)
    Invoke-RestMethod "$api/paper/diagnostics/intents/$id" | ConvertTo-Json -Depth 12
}
Invoke-RestMethod "$api/market-data/recorder/status"
```

- `intents_by_status` partitions instrumented entries, including open entries. Each terminal entry contributes once to `terminal_reasons`. Partial fills can belong to an expired or canceled entry. Legacy entries and synthetic exits have separate treatment. The legacy count covers entries whose poll history is unavailable, and synthetic exits are excluded.
- `polls_by_class` partitions attempts by price-improving, at displayed best, moved-through, other ineligible, and unprocessed. An error or interruption before classification produces an unprocessed poll. A guard that stops the simulator before a book request produces no attempt. `quote_ever_observed` covers positive displayed size at the quote anywhere in a submission or polled bid ladder. Null means insufficient evidence. A false value refers only to sampled books.
- Queue evidence includes the real top of book, modeled effective bid, size at the actual quote, queue before and after decrement, and the source of each volume estimate. At the displayed best, the queue model needs a positive size decrease beyond the estimated queue ahead before drawing for a fill. Size decreases can include cancellations.
- `simulated_candidates` counts generated fills before market and path checks. `policy_accepted_candidates` counts candidates that passed the runtime checks and reached ledger insertion. `persisted_candidate_fills` matches candidate IDs to committed ledger fills. The independent ledger count also includes entries booked through other routes. Candidate stages overlap. A validation rejection can occur on a poll that generated no candidate. The no-candidate and rejection reason maps describe different stages of those polls.
- A crash between candidate creation and the decision can leave a candidate without a recorded decision. A committed matching fill proves acceptance across this boundary. Diagnostic write failures are logged and counted. The worker retries persisting a history-gap marker when SQLite becomes writable. An abrupt exit while the ledger is unwritable can leave that gap visible only in process logs.

## Passive recorder

Recording is independent of paper mode. The default is off. For a **later recording session**, set `ENABLE_MARKET_DATA_RECORDER=true` in the backend environment before launch. `MARKET_RECORDER_DB` selects a dedicated file (default `market_data_recorder.db`), `MARKET_RECORDER_RETENTION_HOURS` defaults to 168, and `MARKET_RECORDER_MAX_MIB` defaults to 256. The cap applies to the SQLite main file. Allow extra disk space for its temporary transaction journal. To shrink below the size of an existing file, select a new recorder file and retain the previous one as an archive.

The client captures successful single-market metadata and orderbook GETs, including a successful historical market lookup. Each row contains the HTTP response body after transport decoding, base URL, actual request path, caller, read-budget lane, ticker, universe (or `UNKNOWN`), schema version, local receipt time in Unix nanoseconds and a source timestamp field/value when recognized. Entry submission and simulator reads carry the intent ID so replay can distinguish concurrent orders on the same ticker. The raw body retains additional fields, fixed-point strings and formatting. Discovery list pages and event/series fee responses are outside this capture. The current client has no public trade feed.

One background writer drains batches of up to 64. The waiting queue holds at most 1,024 responses and 8 MiB of bodies. An individual body may occupy at most 1 MiB or one sixteenth of the file limit, whichever is smaller. Oversize responses, overflow and failed batches increment `dropped`. The status endpoint reports readiness, retained rows, session receipts, writes, drops, evictions and the latest error. A startup error disables capture for that run. Later batch errors allow subsequent batches to proceed. Committed rows survive restarts. Graceful shutdown drains buffered rows, while an abrupt stop can lose the buffered tail.

Age pruning runs on writes, on startup and about once a minute while idle. At capacity, the writer evicts the oldest IDs in the same transaction as the replacement batch. SQLite reuses freed pages. Diagnostic rows in the paper ledger follow the ledger's own lifetime.

For replay, read `market_events` in increasing `id` order from a **single-writer** capture. IDs survive pruning and restarts. Within a session, `sequence` counts observed responses before queue drops. A session change, sequence gap, recorder error or clock reversal calls for an explicit replay boundary. Make an observation available only at local receipt and use ID order to resolve ties. Venue update timestamps can precede local availability. A first B2 milestone is to reproduce B1 poll classifications and queue transitions for a fixed capture, with a fixed seed for any probability draws. Compare results against stored evidence before evaluating alternative execution models.

## Windows deployment at a planned maintenance window

These commands assume this checkout, the existing virtual environment, and a local Uvicorn backend on port 8000. Keep the existing backend environment and working directory. Use the first PowerShell window for inspection and the backup.

```powershell
$ErrorActionPreference = 'Stop'
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$api = 'http://127.0.0.1:8000'
$scannerWasEnabled = (Invoke-RestMethod "$api/scanner/status").enabled
$paperWasEnabled = (Invoke-RestMethod "$api/paper/config").enabled
```

In the **original backend console**, press **Ctrl+C once** and wait for Uvicorn to finish shutting down and return to the prompt. If started through a service manager, use its normal graceful stop and wait for the worker to exit. Leave the database, WAL, SHM and worker-lock files in place. In the inspection window, create a consistent SQLite backup outside the checkout. The read-only source connection includes committed WAL contents.

```powershell
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw 'The backend port still has a listener. Wait for graceful shutdown.'
}
$backupDir = Join-Path $env:LOCALAPPDATA 'KalshiEdgeEngine\backups'
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$backup = Join-Path $backupDir ("paper-before-B1-{0}.db" -f [guid]::NewGuid())
@'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

source = Path("paper_trading.db").resolve(strict=True)
target = Path(sys.argv[1])
target.touch(exist_ok=False)
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
    with closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise SystemExit("Backup integrity check failed")
print(target)
'@ | & .\.venv\Scripts\python.exe - $backup
if ($LASTEXITCODE -ne 0) { throw 'Backup failed. Keep the backend stopped.' }
```

In the original backend console, start a single worker from the same directory with the existing experiment settings. This launch explicitly keeps passive recording disabled.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$env:ENABLE_MARKET_DATA_RECORDER = 'false'
& .\.venv\Scripts\python.exe -m uvicorn kalshi_edge_engine.api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

When paper trading is configured, startup adds two nullable columns to `quote_intents`, creates `entry_polls` and its index, and commits in the existing schema transaction. Repeated startup is safe. Existing entries retain the ledger history they already had. The existing worker recovery still reconciles interrupted open remainders against committed fills. Open positions continue under the existing maintenance worker. A failed migration rolls back and paper startup fails. Inspect the startup error and keep the verified backup before attempting a repair.

After Uvicorn reports application startup complete, verify the backend and restore the previous runtime switches in the inspection window.

```powershell
Invoke-RestMethod "$api/health"
Invoke-RestMethod "$api/market-data/recorder/status"
Invoke-RestMethod "$api/paper/diagnostics/fill-funnel" | ConvertTo-Json -Depth 5
$scannerNowEnabled = (Invoke-RestMethod "$api/scanner/status").enabled
if ($scannerWasEnabled -and -not $scannerNowEnabled) { Invoke-RestMethod -Method Post "$api/scanner/start" }
if (-not $scannerWasEnabled -and $scannerNowEnabled) { Invoke-RestMethod -Method Post "$api/scanner/stop" }
if ($paperWasEnabled) { Invoke-RestMethod -Method Post "$api/paper/start" }
Invoke-RestMethod "$api/paper/config" | ConvertTo-Json -Depth 6
```
