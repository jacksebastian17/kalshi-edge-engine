# Overnight paper capture

## Current B3B1 launch

Use a fresh directory for the first preflight. Keep window 1 open for the single-worker backend.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$run = Join-Path $env:USERPROFILE 'kalshi-paper-runs\b3b1-run01'
if (Test-Path -LiteralPath $run) { throw 'Choose a fresh directory' }
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Preflight -RunDirectory $run
if ($LASTEXITCODE) { throw 'Preflight failed' }
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Backend -RunDirectory $run
```

In window 2, enable collection after backend startup and verify receipt counts and health.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$run = Join-Path $env:USERPROFILE 'kalshi-paper-runs\b3b1-run01'
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Enable -RunDirectory $run
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Status -RunDirectory $run
```

After a graceful Ctrl+C in window 1 and completed shutdown, use the Backup command and offline evidence audit in the [B3B1 runbook](phase-b3b1.md#overnight-run-on-windows).

## Historical B3A launch

Historical procedure for checkout `d6873bf` and its B3A launcher. Run these historical commands only with that checkout and its matching manifest. Current `main` selects a separate B3B1 cohort. For fresh experiments, follow the [B3B1 Windows runbook](phase-b3b1.md#overnight-run-on-windows).

The B3A cohort is `canonical_fixed_point_b3a_v1`. Its directory is `C:\Users\jacks\kalshi-paper-runs\b3a-2026-11-21`. The ledger is `paper_trading.db` and the recorder is `market_data_recorder.db`. The saved `experiment.json` identifies the code revision and source digest, both paths, recorder budget and effective paper policy. The ledger stores the cohort cutover. Clean restarts preserve the cutover and recover interrupted intents.

## Run on Windows

1. Open PowerShell and prepare the databases offline.

   ```powershell
   Set-Location 'C:\Users\jacks\kalshi-edge-engine'
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Preflight
   ```

   The script creates the dedicated directory. Each invocation supplies temporary process environment variables. It sets `PAPER_DB_PATH`, `MARKET_RECORDER_DB` and `PAPER_EXPERIMENT_ID` as above, `MARKET_RECORDER_MAX_MIB=4096`, `MARKET_RECORDER_APPEND_ONLY=true` and `ENABLE_MARKET_DATA_RECORDER=true`. It enables paper simulation, disables live trading and scanner autostart, and sets both global heuristic switches to false. Universe overrides, risk limits, fees, probabilities and scanner read budgets come from the existing configuration and B3A implementation.

   Require `preflight = PASS`, the expected paths and `recorder_probe = ready and drained`. For a fresh run, require zero intents, fills and retained receipts. The probe takes the worker lock and exercises initialization and shutdown without exchange calls. Each preflight adds an empty, clean recorder session. On a clean restart of the same run, activity counts can be nonzero. To start another independent run, pass the same new `-RunDirectory 'C:\absolute\new-directory'` to every command.

2. In the same window start the backend and wait for Uvicorn's application-startup-complete message.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Backend
   ```

   The backend command runs `.venv\Scripts\python.exe -m uvicorn kalshi_edge_engine.api.main:app --host 127.0.0.1 --port 8000 --workers 1`. Startup verifies the saved run identity, policy and recorder health before starting maintenance. The worker lock stays held until recorder drain finishes.

3. In a second PowerShell window, enable the scanner and paper simulation.

   ```powershell
   Set-Location 'C:\Users\jacks\kalshi-edge-engine'
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Enable
   ```

   Enable verifies both live database paths, the cohort, paper configuration and recorder health. It calls `POST /scanner/start`, waits up to three minutes for a completed scan and written receipts, then calls `POST /paper/start`. Both calls accept an already-running worker. If Enable times out, inspect Status for API errors, connectivity or an empty universe before trying again.

4. Check the live status twice before leaving the backend running.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Status
   ```

   Require `scanner.enabled`, an advancing scan timestamp, `paper.enabled`, the expected paths and cohort, and a ready, healthy append-only recorder. Expect `received` and `written` to advance, with zero pruned, dropped and errors. At any instant, received equals written plus queued, in-flight and dropped. The last contiguous written sequence is the durable prefix of the current session. Retained rows span all sessions. Leave the backend window open and keep the computer awake and powered.

5. Optionally start the dashboard in a third window.

   ```powershell
   Set-Location 'C:\Users\jacks\kalshi-edge-engine\web'
   npm.cmd run dev
   ```

6. The following morning, in the second window, check results before shutdown.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Status
   ```

   Status reads `/scanner/status`, `/paper/config`, `/paper/summary`, `/paper/diagnostics/fill-funnel` and `/market-data/recorder/status`. Compare total intents, simulated candidates, policy-accepted candidates and booked modeled entry fills. Check storage usage, free bytes, diagnostic errors and marked poll-history gaps. A quiet interval may produce zero candidates or fills.

7. Press **Ctrl+C once** in the backend window. Wait for application shutdown to complete and for the PowerShell prompt to return. Shutdown stops producers, drains pending and in-flight batches, and seals the session. If you started the dashboard, stop it with Ctrl+C in its own window.

8. In the second window, create the offline replay backup.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Backup
   ```

   Backup takes the worker lock and uses SQLite's backup API for both databases, including committed WAL records. It checks each output with `quick_check`, includes the manifest and publishes a new `backup-<id>` directory after both checks pass. Failed backups can leave a directory ending in `.partial`. The printed JSON compares stored receipt order and row count with each session's final counters. For a complete recorded session, require `complete_recorded_session = true`, equal received and written counts and zero drops, errors and sequence mismatches. An open session after an abrupt stop has an unknown suffix. Preserve and label incomplete runs. Use the printed completed backup directory with the replay CLI's `--archive` option. [Replay commands](phase-b2.md)

## Storage and evidence limits

A 4 GiB recorder budget provides headroom over the previous 256 MiB rolling capture. Coverage over 8–12 hours depends on actual response size and rate. Startup requires enough free space for the remaining budget plus 2 GiB. The writer checks before each batch and at least once per idle minute. A SQLite capacity error or free space below 2 GiB plus 16 MiB of working room trips the interlock. The ledger, transient journals and backups use additional space.

Append-only recording preserves timestamps, session IDs, sequence numbers, raw payloads and request provenance. A failed batch rolls back atomically. A lost receipt, queue overflow, oversized response or recording error latches a failure, blocks paper decisions, cancels scanner and simulation workers and exposes the reason in telemetry. Committed receipts stay in place. A failed or unclosed session blocks further collection in that recorder. Use a fresh run directory after investigating.

Withhold unattended operation if preflight fails, the port is occupied, scans stall, counters stop advancing unexpectedly, recorder health fails or diagnostic gaps appear. Receipt continuity covers successful supported per-market responses. Polling cannot establish intra-poll exchange activity or actual execution. Use the offline Backup command after shutdown to preserve the SQLite snapshot.
