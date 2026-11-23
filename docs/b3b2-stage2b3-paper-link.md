# Stage 2B3 prospective paper-decision pilot

Protocol `b3b2-paper-proposals-v1` enrolls the proposals the ordinary paper strategy generates during a manually started session. A proposal enters the sidecar when the real quote builder returns, before side, risk, cooldown and admission checks. Both sides of one builder call share its decision timestamp and retain their original order. Capture includes rejected proposals and proposals outside the subscribed markets. Signals that produce no quote are counted separately. Each run uses a fresh paper ledger and REST recorder under the strict B3B1 profile. The cohort lives in a separate research directory.

## Two-to-four-game pilot

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
