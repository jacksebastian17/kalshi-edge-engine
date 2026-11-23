# Stage 2B1 — prospective NBA campaign

Use one campaign roster across collection dates. Each preflight can select two to four different NBA events. Seeing the same event in a later capture adds secondary observations. The campaign’s sampling unit is one underlying game.

## Collect the next sample in PowerShell

**1. Preflight and register.** In one PowerShell window, run the block below. Reuse the campaign directory for subsequent sessions. Choose the requested count before recording. Review the event tickers, active status and close times printed by preflight. An active contract can refer to a future game.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$repo = (Get-Location).Path
$py = Join-Path $repo '.venv\Scripts\python.exe'
$pilots = Join-Path $env:USERPROFILE 'kalshi-microstructure-pilots'
$campaign = Join-Path $env:USERPROFILE 'kalshi-shadow-research\nba-campaign-01'
$roster = Join-Path $campaign 'preflights'
[IO.Directory]::CreateDirectory($roster) | Out-Null
$run = 'b3b2-diverse-' + [guid]::NewGuid().ToString('N')
$runDir = Join-Path $pilots $run
& $py -m tools.microstructure preflight --run $run --distinct-events --count 4
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed. See preflight_failure.json in the new run.' }
[IO.File]::Copy((Join-Path $runDir 'manifest.json'), (Join-Path $roster "$run.json"), $false)
```

Discovery reads all pages of the open `KXNBAGAME` listing. It checks event identity, active status and future close time. It ranks eligible contracts by descending reported 24-hour volume, breaking ties by ascending ticker, and keeps the first representative per event. A missing volume ranks as zero. Negative, malformed and nonfinite volumes are excluded. The selected markets are confirmed by individual GETs. A shortfall fails the run. Each successful manifest records the candidate observations, ranking rule, reasons, selected identities and preflight times. A discovery failure preserves the audit collected up to failure. Existing `--count` and `--tickers` modes still work. A repeated game across sessions remains visible in the campaign report.

**2. Capture for 12 minutes and check health.** Keep the window open. Repeat the status command as needed. The recorder rechecks the selected markets before each connection.

```powershell
$job = Start-Job -ArgumentList $repo,$py,$run -ScriptBlock {
    param($dir,$python,$name)
    Set-Location -LiteralPath $dir
    & $python -m tools.microstructure capture --run $name --minutes 12
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Capture ended with a fault. Inspect its seal and health.' }
}
& $py -m tools.microstructure status --run $run
```

Capture seals automatically at the duration limit. For early graceful shutdown while recording, use:

```powershell
& $py -m tools.microstructure stop --run $run
```

**3. Verify and back up.** Wait for the job. A short or faulted session with a verified seal can still contribute coverage evidence. Leave the preflight copy in the roster if the session never sealed.

```powershell
Receive-Job -Job $job -Wait
& $py -m tools.microstructure status --run $run
& $py -m tools.microstructure backup --run $run
if ($LASTEXITCODE -ne 0) { throw 'Backup unavailable. Retain the enrolled run and its diagnostics.' }
```

## Evaluate the accumulated sessions offline

The Stage 2B2 report adds decision-anchored price observations to this same cohort. Use the offline markout runbook to produce both campaign accounting and per-episode evidence in a fresh directory. [Stage 2B2 runbook](b3b2-stage2b2-markouts.md)

Run this block from the checkout after one or more sessions. It also works in a fresh PowerShell window. It creates a fresh output directory for this analysis pass. Every roster entry participates in denominator accounting, including entries without a backup. A corrupt or mismatched supplied backup stops the analysis.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$pilots = Join-Path $env:USERPROFILE 'kalshi-microstructure-pilots'
$campaign = Join-Path $env:USERPROFILE 'kalshi-shadow-research\nba-campaign-01'
$roster = Join-Path $campaign 'preflights'
$batch = Join-Path $campaign ('analysis-' + [guid]::NewGuid().ToString('N'))
$backups = @(foreach ($entry in Get-ChildItem -LiteralPath $roster -Filter '*.json' -File) {
    $backup = Join-Path (Join-Path $pilots $entry.BaseName) 'backup'
    if (Test-Path -LiteralPath $backup) { $backup }
})
foreach ($backup in $backups) {
    $name = Split-Path (Split-Path $backup -Parent) -Leaf
    & $py -m tools.shadow_feasibility --archive $backup --output (Join-Path $batch $name) --detail
    if ($LASTEXITCODE -ne 0) { throw "Stage 2A verification failed for $name" }
}
& $py -m tools.research_campaign --roster $roster --output (Join-Path $batch 'campaign') @backups
if ($LASTEXITCODE -ne 0) { throw 'Campaign verification failed' }
Get-Content -Raw -LiteralPath (Join-Path $batch 'campaign\summary.json') | ConvertFrom-Json
```

`summary.json` gives source hashes, repeats, overlapping capture intervals, selection and exclusion counts, coverage gates, and separate primary and secondary classifications by delay, event and observation date. `episodes.jsonl` retains the decision or exclusion for each scheduled slot with its session, ticker, side and source record. Primary, secondary and excluded rows carry explicit roles. The per-session Stage 2A evidence files contain the individual print and depth references.

## Primary selection and coverage

- The frozen 60-second Stage 2A decision grid and its four receipt-time delays apply to every verified session. Within each game, the first eligible decision wins. Order by estimated UTC decision time, then session ID, market ticker, then YES before NO. The UTC key is the recorder’s starting wall time plus monotonic elapsed time. Dates are UTC **observation dates**. Clock uncertainty can affect ordering between machines or near midnight.
- An early zero-print, truncated, interrupted or adverse primary keeps its place. A verified session with no eligible slot contributes its selected games and exclusions. If a missing enrolled session could precede the first observed decision for a game, primary identity is unknown. Later eligible decisions stay secondary. Identical archive copies are verified and counted once. Conflicting session identities or conflicting claims on the same journal fail verification.
- The primary-window denominator is every selected game in the supplied roster. A usable window covers the full 60 seconds with continuous evidence and observable scenario outcomes, including an observed quote departure. Interrupted or conflicted windows remain in the report and are excluded from scoring. The gates require 100 primaries from 100 games on at least 10 primary observation dates, 95% usable windows, zero unresolved continuity errors in scored windows and 95% on-time five-second marks. Stage 2A supplies no entry-linked five-second marks. That gate is unmeasured and the full assessment remains `INCONCLUSIVE`. Fill probability and expected profit remain unidentified. [Frozen episode protocol](b3b2-stage2a-feasibility.md) · [Research gates](b3b2-run-forensics.md#proposed-b3b2-protocol)
- An offline run may omit `--roster` for historical inspection. Its denominator is limited to supplied verified sessions. Copy prospectively selected manifests before starting capture and retain every copy. Local files cannot independently prove enrollment time or identify sessions omitted by the operator. The legacy November 22 sample is a historical baseline.

## Capture build approvals

The registry approves the hash of the **entire** capture descriptor, including all seven module hashes, Python 3.13.14 and websockets 16.0. The legacy Stage 1.1 descriptor is `90bbee4f819d93ee920b7b1c42015a102ec7c543e1d2fd9bdbfc1b9b3e3f877e`. The distinct-event release `b3b2-stage2b1-v1` is `f5403fd6efa88aa2d1c1c596db7519945ceade9948eac4d9f81deed1dad9e32c`. Only the recorder CLI and acquisition module changed between those descriptors. Offline verification also compares every local replay module and the canonical orderbook parser against approved hashes. [Approval registry](../tools/capture_builds.py)

For a new build, review module and dependency changes against the prior approved release, compute the descriptor from the reviewed checkout, and add a named registry entry in a reviewed commit. Exercise synthetic capture/replay and reproduce the November 22 Stage 2A artifacts before approval. A changed replay dependency requires a compatible, verified replay implementation for each supported release. Arbitrary manifest hashes cannot enroll a new build. These fingerprints identify the recorded code and versions. They provide no independent signature or machine-wide attestation.
