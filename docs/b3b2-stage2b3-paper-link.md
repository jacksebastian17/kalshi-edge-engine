# Stage 2B3 prospective paper-decision pilot

Protocol `b3b2-paper-proposals-v1` enrolls the proposals the ordinary paper strategy generates during a manually started session. A proposal enters the sidecar when the real quote builder returns, before side, risk, cooldown and admission checks. Both sides of one builder call share its decision timestamp and retain their original order. Capture includes rejected proposals and proposals outside the subscribed markets. Signals that produce no quote are counted separately. Each run uses a fresh paper ledger and REST recorder under the strict B3B1 profile. The cohort lives in a separate research directory.

## Stage 2B3.2 entry-selective, recently active pilot

The first Stage 2B3.1 pilot linked 280 of 3,704 proposals. All 140 linked YES proposals failed the minimum capturable-edge check. All 140 linked NO proposals had a disabled side. The six paper admissions were outside the recording, and the ledger booked zero entry fills. Of the 280 linked five-second decision marks, 10 were fresh, 266 stale and four beyond capture end. The two selected games were scheduled for November 25 during a November 23 Pacific recording. The published evidence is under `nba-paper-aligned-01/reports/9eca63bfc06e482193e71a745c389977`.

### Frozen sampling rule

`b3b2-paper-entry-active-v1` has a dedicated campaign root, `%USERPROFILE%\kalshi-paper-entry-active-research`. It contributes zero games to the original 100-game sample. The earlier volume and enabled-side cohorts keep their own protocols.

1. Freeze a common cutoff at the whole UTC second at which the supervised preflight begins. Discover the ordinary scanner universe and score each NBA game contract once using the frozen B3B1 fee resolver, signal scorer and quote builder. Each candidate retains its own score time and response receipts.
2. Evaluate each generated quote through the deterministic portion of the existing entry function. Check the configured side first. On YES only, both the capturable-edge and intended-quote metrics must exist, capturable edge must meet its minimum, and the intended quote must meet the minimum absolute distance from 50 cents. Both sides must meet the top-liquidity and net-edge minima. Equality passes. The pinned NBA policy enables YES, disables NO, and sets the four numerical minima to 3 cents, 8 cents, 200 contracts and 3 cents respectively. Preflight reads the frozen configuration. Its isolated store stops evaluation at the first portfolio query, before any order, admission, random draw or booking.
3. For a game with a passing quote, request its basketball-game milestone. Require a unique Sports milestone linked to that exact event and a timezone-qualified start time. The cutoff must fall between one hour before and three hours after scheduled start, inclusive, and before the supplied end time if present. Missing, ambiguous, or paginated schedule evidence excludes the candidate. This measures proximity to scheduled play. Actual game state is unknown. [Kalshi milestone fields](https://docs.kalshi.com/api-reference/milestone/get-milestones)
4. For each qualifying contract in that window, request one page of up to 100 public non-block prints for that ticker in the preceding five minutes. Require a returned print with the exact ticker, a trade ID, explicitly false block flag, positive fixed-point quantity and exchange timestamp in `[cutoff − 300s, cutoff)`. A complete empty response, an unavailable response and a page without verifiable evidence have distinct exclusions. A qualifying print establishes presence only. It provides no estimate of fill probability or subsequent book freshness. [Kalshi public trade fields](https://docs.kalshi.com/api-reference/market/get-trades)
5. Rank qualifying tickers in ascending lexical order, keep one contract per event, and select the requested count. Reconfirm those contracts and freeze the inputs, audit and enrollment before recording. A shortfall or failed confirmation ends the attempt. Request collection within ten minutes of the cutoff. Retain expired enrollments, later quiet games and every out-of-subscription proposal. All gates, windows and ranking rules are prespecified. Subsequent prints, markouts and bookings do not participate.

The permanent preflight audit includes the frozen policy, code hashes, rankings, all candidate reasons, per-side gate results, source response bytes, schedule and trade query parameters, timestamps, and exact receipt numbers. Failed attempts remain in the campaign report. If the process terminates abruptly, the start marker and flushed candidate records identify the observed prefix. Preserve the full campaign directory with the tape.

### Supervise one 12-minute, two-game pilot

Run the public-GET preflight from the repository checkout. Review both selected rows before collection. If the requested games are unavailable, retain the attempt and report it with the same campaign name.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$campaign = 'nba-paper-entry-active-01'
$run = 'b3b2-entry-active-' + [guid]::NewGuid().ToString('N')
$root = Join-Path $env:USERPROFILE "kalshi-paper-entry-active-research\$campaign"
& $py -m tools.paper_link preflight-active --campaign $campaign --run $run --count 2
if ($LASTEXITCODE) { throw "Preflight incomplete. Retain $run and run the offline report." }
$plan = Get-Content -Raw -LiteralPath (Join-Path $root "preflights\$run.json") | ConvertFrom-Json
$plan.candidates | Select-Object ticker,rank,reason,proposals,schedule,activity | ConvertTo-Json -Depth 5
```

Once the operator authorizes the recording, run this command in the same PowerShell window. The scanner retains its normal universe, portfolio, cooldown, risk and admission checks.

```powershell
& $py -m tools.paper_link collect --active --campaign $campaign --run $run --minutes 12
if ($LASTEXITCODE) { Write-Warning "Retain $run and inspect the offline report." }
```

For status or graceful early stop, use the Stage 1 `status --run` and `stop --run` commands below with the exact run name. Report offline after collection, or after a failed preflight.

```powershell
$out = & $py -m tools.paper_link report --active --campaign $campaign
if ($LASTEXITCODE) { throw 'Offline verification failed' }
$s = Get-Content -Raw -LiteralPath (Join-Path $out 'summary.json') | ConvertFrom-Json
$s.preflight_attempts | ConvertTo-Json -Depth 6
$s.sessions.PSObject.Properties | ForEach-Object {
    $_.Value | Select-Object sealed,clock_mapping,all_proposal_stages,selected_market_diagnostics | ConvertTo-Json -Depth 6
}
$s.decision_mark_coverage.'5' | ConvertTo-Json
```

The report counts generated quotes, enabled sides, observed deterministic passes, paper admissions and verified ledger entry bookings. It gives the same counts for each selected contract, including zero-proposal games. A dynamic rejection can follow a deterministic pass. An interrupted decision with no observed gate result has unknown eligibility. Missing ledger evidence leaves bookings null. `episodes.jsonl` includes every generated proposal. The two-second depth-age limit applies at every markout horizon.

## Stage 2B3.1 strategy-aligned pilot

The first 12-minute volume-ranked pilot recorded Portland–Philadelphia (POR) and Utah–Los Angeles (UTA). The paper scanner generated 2,106 proposals on 21 other contracts across 15 games. All four admissions were on Milwaukee–Detroit, and the paper ledger booked zero fills. The shared-process clock verified and every signal receipt resolved. Neither selected game produced a proposal. The original report is under `nba-paper-link-01/reports/67948dd2f4cc498f9f5b8de13c011215`.

| | Original Stage 2B3 | Stage 2B3.1 |
| --- | --- | --- |
| Cohort | `b3b2-paper-proposals-v1` | `b3b2-paper-strategy-aligned-v1` |
| Candidate evidence | Active game listing and 24-hour volume | One fresh NBA scanner discovery pass and one fee-resolved scanner score per candidate |
| Ranking | Descending listed volume, then ascending ticker, one contract per event | Ascending ticker among contracts producing an enabled-side quote, one contract per event |
| Denominator | All games enrolled under the volume rule | All games enrolled under the strategy rule, including later scanner and proposal misses |

The targeted preflight runs the existing scorer and quote builder with the effective paper policy from the first pilot. A candidate needs verified series fees, an active market with a matching game ID, and at least one generated quote on a configured entry side. The preflight stops at quote generation. Later per-intent selectivity, portfolio limits, cooldowns and admission checks still run in the paper engine. Lexical ordering favors earlier ticker strings. A single pass describes the book at each contract's own receipt time and can select future games.

The frozen audit records the discovery pages, fee and orderbook response bytes and SHA-256 hashes, source receipt numbers (including cached fee inputs), candidate timestamps, proposed sides, selection reasons, eligible ranks, and the effective policy and implementation hashes. A fresh market GET confirms each selected contract. A shortfall or failed confirmation saves the attempted selection and ends preflight. Keep that attempt with the campaign. Once enrolled, selected games stay in the denominator even if a game closes, the scanner misses it, or recording stops early.

### Manually run one 12-minute, two-game paper pilot

Run this preflight once in PowerShell from the project checkout. It uses public GETs and writes a new enrollment under `%USERPROFILE%\kalshi-paper-aligned-research`. It exits before starting the paper engine or WebSocket.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$campaign = 'nba-paper-aligned-01'
$run = 'b3b2-aligned-' + [guid]::NewGuid().ToString('N')
$root = Join-Path $env:USERPROFILE "kalshi-paper-aligned-research\$campaign"
& $py -m tools.paper_link preflight-aligned --campaign $campaign --run $run --count 2
if ($LASTEXITCODE) { throw "Preflight failed. Inspect preflights\$run.json before any new attempt." }
$audit = Get-Content -Raw -LiteralPath (Join-Path $root "preflights\$run.json") | ConvertFrom-Json
$audit.candidates | Select-Object ticker,event_ticker,signal_reason,eligible,rank,reason | Format-Table
```

Once collection is explicitly authorized, use the same PowerShell session and run name. This command ends after 12 minutes plus shutdown and backup. The ordinary scanner retains its full universe.

```powershell
& $py -m tools.paper_link collect --aligned --campaign $campaign --run $run --minutes 12
if ($LASTEXITCODE) { Write-Warning 'Collection incomplete. Retain the enrollment and run the offline report.' }
```

For status or early stop, use the existing `tools.microstructure status --run` and `stop --run` commands below with the exact aligned run name. To report offline after collection, use the following in the original window.

```powershell
$out = & $py -m tools.paper_link report --aligned --campaign $campaign
if ($LASTEXITCODE) { throw 'Offline verification failed' }
$summary = Get-Content -Raw -LiteralPath (Join-Path $out 'summary.json') | ConvertFrom-Json
$summary | Select-Object protocol,selected_games,games_without_primary,proposals,linkage,admission | ConvertTo-Json -Depth 4
$summary.sessions.PSObject.Properties | ForEach-Object {
    $_.Value | Select-Object clock_mapping,sealed,selected_market_diagnostics | ConvertTo-Json -Depth 6
}
```

Each selected ticker has counts for scanner attempts, score errors, signal reasons, valid signals, proposals, admissions and clock-linked proposals. An unsealed session reports observed counts as lower bounds. Inspect `episodes.jsonl` for all individual proposals, including rejections and contracts outside the recording. The targeted campaign has its own reports and contributes zero games to the original 100-game sample.

## Original volume-ranked two-to-four-game pilot

Choose two distinct NBA games for the first 12-minute session. Set the count to three or four before preflight for a larger session. Use the frozen Stage 2B1 distinct-event ranking, record every selected event and review its market identity. An active contract can concern a future game. Quiet events, a second visit to a game, zero paper proposals and zero compatible prints are all reportable results. The scanner keeps its normal universe and strategy. Its proposals can miss the subscribed tickers entirely.

The following commands are for an explicitly authorized future pilot. Enrollment uses public GETs. Collection opens the authenticated public WebSocket and starts the local paper simulator for this session. The CLI exposes no HTTP server. Each collection requires a new enrollment and an explicit invocation.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$campaign = 'nba-paper-link-01'
$run = 'b3b2-paper-' + [guid]::NewGuid().ToString('N')
& $py -m tools.microstructure preflight --run $run --distinct-events --count 2
if ($LASTEXITCODE) { throw 'Public market preflight failed' }
& $py -m tools.paper_link enroll --campaign $campaign --run $run
if ($LASTEXITCODE) { throw 'Enrollment failed' }
```

Keep the campaign name for later sessions. The enrollment pins the selected markets, event IDs, WebSocket session and manifest digest, paper session ID, protocol implementation digest and registration time. Copy the enrollment to independent custody before recording if enrollment time must be independently evidenced.

```powershell
& $py -m tools.paper_link collect --campaign $campaign --run $run --minutes 12
if ($LASTEXITCODE) { Write-Warning 'Inspect the incomplete enrollment with the offline report.' }
```

The command prepares the fresh strict ledger, runs the normal scanner, paper lifecycle and public tape reader in one Python process, stops the workers, backs up both immutable inputs and seals their identities. Normal completion takes 12 minutes plus verification and backup time. For an early graceful stop, use another PowerShell window and the same run name.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$run = Read-Host 'Exact enrolled run name'
& $py -m tools.microstructure status --run $run
& $py -m tools.microstructure stop --run $run
```

## Produce the offline report

This step only reads enrolled sessions and backups. It also works for an enrollment that never recorded. It publishes fresh `summary.json` and `episodes.jsonl` files under `%USERPROFILE%\kalshi-paper-link-research\<campaign>\reports`.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$out = & $py -m tools.paper_link report --campaign 'nba-paper-link-01'
if ($LASTEXITCODE) { throw 'Verification failed' }
$report = Get-Content -Raw -LiteralPath (Join-Path $out 'summary.json') | ConvertFrom-Json
$report | Select-Object proposals,selected_games,primary_games,primary_dates_utc,linkage,admission,primary_sides,predecision_activity | ConvertTo-Json -Depth 5
$report.public_support_by_delay_ms | ConvertTo-Json -Depth 4
$report.booked_fill_public_support_by_delay_ms | ConvertTo-Json -Depth 4
$report.decision_mark_coverage | ConvertTo-Json -Depth 4
$report.entry_mark_coverage_by_delay_ms.'0' | ConvertTo-Json -Depth 5
Get-Content -LiteralPath (Join-Path $out 'episodes.jsonl') -TotalCount 1 | ConvertFrom-Json | ConvertTo-Json -Depth 12
```

**Clock evidence.** The original recorder and observer call `time.monotonic_ns` in the same process. A fresh process-run token and PID accompany each sidecar record. A witness brackets the actual recorder invocation and binds its returned tape digest and enrolled manifest. Offline verification checks the brackets, identities, monotonic ordering, clock origin and seals. The mapping is the identity with zero conversion offset. Wall time is retained for inspection and UTC date accounting. An independent backend's wall timestamps cannot establish this mapping. An exact tie between a real proposal and a tape receipt has ambiguous order and receives no mark. Interrupted sidecars retain complete prefix records with unknown coverage. An invalid sealed digest fails verification. These hashes support integrity; local custody alone does not authenticate the producer.

**The evidence file.** The decision ID is the paper session plus the append-only event number. The paper engine's intent UUID joins admissions, failed polls, candidate IDs, terminal status, fills and source receipt references. A ledger-only intent is listed in session diagnostics when its prospective observation is missing. Missing paper evidence leaves lifecycle values null. A bounded background writer keeps disk writes off the decision thread. Overflow or a writer failure prevents a complete seal. Observation and serialization still have a local timing cost.

| Measurement | Anchor and interpretation |
| --- | --- |
| Decision price movement | The real generated proposal's monotonic time. The reconstructed book, source receipts, displayed and executable bids, midpoint change, depth and 5/15/30/60-second marks are reported even for a rejected proposal with a verifiable link. |
| Hypothetical entry price movement | Assumed local arrival at the proposed price, with 0/100/250/500 ms sensitivity. The corresponding entry mark reference exists for an observably passive proposal even with zero prints. |
| Print-supported hypothetical liquidation | The first compatible public print in that scenario anchors a separate conditional one-contract calculation. The record and trade ID identify the print. Each horizon points to the as-of depth receipt. A print can support market activity at the price without establishing an individual fill. |
| Booked paper result | Entry fill ID, price, size and fee come from the verified ledger. Realized P&L is the ledger's `realized_pnl_delta_cents` on each lifecycle exit. The session lists it once and the entry fill references the lifecycle ID. An open lifecycle has no booked exit result. Multiple contributing intents can share an exit. |

At each assumed arrival the proposal must be below the implied ask in a continuous, uncrossed two-sided book. The report distinguishes at-best, inside-spread and behind-best proposals. It measures displayed size at the proposed price and separately at better prices. The comparison uses same-price printed contracts versus displayed size at that price plus proposed size. A virtual improved bid adds no liquidity to the historical tape. The fixed-price observation window ends at decision plus 60 seconds, loss of passivity, a feed break or capture end. The receipt that makes a quote unobservable or marketable closes the window before that receipt. This is a prespecified diagnostic exposure even for rejected or expired paper orders. Outbound venue latency, private priority and real execution are unidentified.

Stage 1.1 supplies unique non-block prints, signed depth changes, exact coincidences, ambiguous groups and boundary splits. Unmatched decreases remain unidentified. A positive print in an incomplete prefix is observable; an incomplete zero-print prefix has unknown full-window support. For a booked paper entry, the per-delay comparison counts only prints through the observer's local post-commit receipt at the same price, inside the evaluated exposure. A later print cannot corroborate that booking.

The Stage 2B2 two-second depth-age limit and one-contract liquidation math apply to all three tape anchors. Stale and missing marks remain in the evidence. At a decision, price change uses the actual starting executable bid while hypothetical purchase cost uses the proposed price. Fee observation reuses the paper runtime's existing public market, event and series GETs. Original response bytes live in the REST backup. The sidecar records their SHA-256, route, REST receipt identity and original wall receipt time, plus the monotonic time when the response reached the recorder callback. The report requires the linked, complete REST session, matching bytes, an approved origin and Stage 2B2's as-of identity, waiver and freshness checks. Missing fees yield null net values. [Markout and fee definitions](b3b2-stage2b2-markouts.md)

**Campaign accounting.** The sampling unit is the enrolled game. Order sessions by frozen enrollment time and paper session ID, then proposals by sidecar sequence. The first generated proposal on that game's enrolled ticker is primary, including a rejection, missing tape, stale mark or unsupported booking. An earlier enrollment with uncertain proposal coverage prevents promotion of a later observation. Repeated proposals and sessions remain visible. Both the scanner's emitted side mix and the primary side mix are reported. The original Stage 2A/2B1 YES tie preference belongs to those historical cohorts. This protocol preserves actual builder order, which also emits YES first when both qualify. No side balancing or activity-based filtering takes place.

Predecision activity uses the preceding 30 seconds of accepted depth and compatible-classified public trades for the game's subscribed ticker. A complete window with one or more prints is active. A complete window with zero is quiet. Incomplete history has a separate label. Each enrolled game remains in the denominator even when the scanner proposes nothing. Review integrity, unresolved references, rejected and unfilled episodes, per-delay print support, all four freshness counts and booked-fill diagnostics before considering any expansion. The campaign retains the 100-game and 10-UTC-date minima. Genuine ledger five-second observations are reported separately from decision- and print-anchored tape marks. A two-to-four-game pilot can assess linkage and coverage only. [Research protocol](b3b2-run-forensics.md) · [Frozen campaign](b3b2-stage2b1-campaign.md)
