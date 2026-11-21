# Run from a PowerShell window in the checkout. Variables apply to this process only.
param(
    [Parameter(Mandatory)][ValidateSet('Preflight','Backend','Enable','Status','Backup')]
    [string]$Action,
    [string]$RunDirectory = (Join-Path $env:USERPROFILE 'kalshi-paper-runs\b3a-2026-11-21')
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$run = [IO.Path]::GetFullPath($RunDirectory)
$env:PAPER_DB_PATH = Join-Path $run 'paper_trading.db'
$env:MARKET_RECORDER_DB = Join-Path $run 'market_data_recorder.db'
$env:PAPER_EXPERIMENT_ID = 'canonical_fixed_point_b3a_v1'
$env:ENABLE_MARKET_DATA_RECORDER = 'true'
$env:MARKET_RECORDER_APPEND_ONLY = 'true'
$env:MARKET_RECORDER_MAX_MIB = '4096'
$env:ENABLE_PAPER_TRADING = 'true'
$env:ENABLE_LIVE_TRADING = 'false'
$env:PAPER_ALLOW_PRICE_IMPROVES = 'false'
$env:PAPER_ALLOW_MOVED_THROUGH = 'false'
$env:ENABLE_NBA_SCANNER = 'false'  # Enable explicitly after checking backend identity.
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$api = 'http://127.0.0.1:8000'

if ($Action -eq 'Preflight') {
    [void][IO.Directory]::CreateDirectory($run)
    & $python -m kalshi_edge_engine.overnight preflight
    if ($LASTEXITCODE) { throw 'Overnight preflight failed' }
    return
}
if ($Action -eq 'Backend') {
    & $python -m uvicorn kalshi_edge_engine.api.main:app --host 127.0.0.1 --port 8000 --workers 1
    if ($LASTEXITCODE) { throw 'Backend exited with an error' }
    return
}
if ($Action -eq 'Backup') {
    & $python -m kalshi_edge_engine.overnight backup
    if ($LASTEXITCODE) { throw 'Offline backup failed' }
    return
}

$paper = Invoke-RestMethod "$api/paper/config"
$rec = Invoke-RestMethod "$api/market-data/recorder/status"
if ($paper.experiment_id -ne $env:PAPER_EXPERIMENT_ID -or $paper.paper_db_path -ne $env:PAPER_DB_PATH -or
    $rec.database_path -ne $env:MARKET_RECORDER_DB -or !$rec.append_only) {
    throw 'The backend on port 8000 does not match this experiment'
}
if ($Action -eq 'Enable') {
    if (!$rec.healthy -or !$paper.configured) { throw "Recorder or paper unavailable: $($rec.failure_reason)" }
    Invoke-RestMethod -Method Post "$api/scanner/start"
    $deadline = (Get-Date).AddMinutes(3)
    do {
        Start-Sleep -Seconds 2
        $rec = Invoke-RestMethod "$api/market-data/recorder/status"
        $scanner = Invoke-RestMethod "$api/scanner/status"
        if (!$rec.healthy) { throw "Capture incomplete: $($rec.failure_reason)" }
        if ($scanner.enabled -and $scanner.last_scan_ms -and $rec.written -gt 0) { break }
        if ((Get-Date) -ge $deadline) { throw 'No completed scan with recorded receipts yet. Inspect Status before enabling paper.' }
    } while ($true)
    Invoke-RestMethod -Method Post "$api/paper/start"
    $paper = Invoke-RestMethod "$api/paper/config"
    if (!$paper.enabled) { throw 'Paper simulation did not enable' }
}
$scanner = Invoke-RestMethod "$api/scanner/status"
$funnel = Invoke-RestMethod "$api/paper/diagnostics/fill-funnel"
$summary = Invoke-RestMethod "$api/paper/summary"
[ordered]@{
    scanner = $scanner
    paper = $paper
    intents = $summary.total_intents_created
    simulated_candidates = $funnel.simulated_candidates
    accepted_candidates = $funnel.policy_accepted_candidates
    modeled_entry_fills = $funnel.ledger_entry_fills_for_instrumented_intents
    diagnostic_errors = $funnel.diagnostic_write_errors_this_process
    history_gaps = $funnel.intents_with_marked_history_gaps
    recorder = (Invoke-RestMethod "$api/market-data/recorder/status")
} | ConvertTo-Json -Depth 8
