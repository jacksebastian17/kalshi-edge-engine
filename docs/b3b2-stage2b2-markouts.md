# Stage 2B2 offline markouts

For each eligible Stage 2A decision, this report asks what a displayed bid for its purchased outcome was worth 5, 15, 30 and 60 seconds later. A negative gross markout means the displayed one-contract bid fell below the hypothetical posting price. A public trade at that price supplies evidence about the market. The identity, queue position and fill of the hypothetical order remain unknown.

## Run the enrolled campaign in PowerShell

Use the checkout, frozen preflight roster and existing sealed backups. The command verifies the source inventories, seals, approved replay code and journal digests, and creates a fresh report directory. It needs no credentials or service process.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$campaign = Join-Path $env:USERPROFILE 'kalshi-shadow-research\nba-campaign-01'
$roster = Join-Path $campaign 'preflights'
$pilots = Join-Path $env:USERPROFILE 'kalshi-microstructure-pilots'
$archives = @(foreach ($entry in Get-ChildItem -LiteralPath $roster -File -Filter '*.json') {
    $backup = Join-Path (Join-Path $pilots $entry.BaseName) 'backup'
    if (Test-Path -LiteralPath $backup) { $backup }
})
$out = Join-Path $campaign ('stage2b2-' + [guid]::NewGuid().ToString('N'))
& $py -m tools.research_campaign --markouts --roster $roster --output $out @archives | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Offline archive verification or reporting failed' }
$summary = Get-Content -LiteralPath (Join-Path $out 'summary.json') -Raw | ConvertFrom-Json
$summary.tape_markouts.tape_anchored_5s_primary
$summary.coverage_gates.on_time_5s_marks
$summary.tape_markouts.coverage_by_role_and_horizon_seconds.primary | ConvertTo-Json -Depth 5
$summary.tape_markouts.scenario_strata | Where-Object { $_.role -eq 'primary' -and $_.delay_ms -eq 0 } | ConvertTo-Json -Depth 6
$episodes = @(Get-Content -LiteralPath (Join-Path $out 'episodes.jsonl') | ForEach-Object { $_ | ConvertFrom-Json })
$episodes | Where-Object role -eq 'primary' | Select-Object event_ticker,market,side,markouts | ConvertTo-Json -Depth 8
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $out 'summary.json'),(Join-Path $out 'episodes.jsonl')
```

Rerunning with the same inputs and protocol produces the same two UTF-8 files. For retrospective inspection of a single verified backup, pass that backup as the positional argument and omit `--roster`. The summary records that narrower denominator. A missing enrolled backup still counts against campaign coverage. A hash mismatch stops the run.

## Reading an observation

- **Clock and receipt.** Protocol `b3b2-decision-markouts-v1` uses the Stage 2A local monotonic grid. At each deadline the final eligible journal record with receipt time at or before the deadline is inclusive. Equal times follow journal order. `asof_record` is the journal cutoff. `source.record` is the last accepted depth snapshot or delta for the selected ticker. Its monotonic time, subscription ID, sequence, snapshot and last top-state record link back to the sealed journal in `sources[session].hashes`. A trade, another ticker's update or a retransmission cannot refresh that receipt.
- **Freshness.** Age is deadline minus depth receipt time in nanoseconds. Stage 2B2 uses a fixed inclusive two-second limit for fresh depth evidence. Quiet books can be older. Stale observations retain prices, values and age in the episode file. Summary medians and directional counts use fresh observations with the required price inputs. Entry depth age is reported separately and never changes eligibility. Flags identify any depth update since decision and since the prior deadline. A new depth update can leave the displayed top unchanged.
- **Continuity.** A deadline beyond collection end is missing. A Stage 2A evidence break between decision and deadline, including a replacement snapshot, or an overlapping conflicted segment makes that horizon unscorable. Later recovery can support a new decision. A shortened last episode still gets four explicit outcomes. The full-window execution class can therefore be incomplete while an early price observation is usable.
- **Executable prices.** All monetary fields are decimal cents for one contract. Within each side's fixed-point ladder, take the best bid with at least one displayed contract at that price. Fractions at different prices are not combined. `displayed_bid_cents` and its quantity retain the visible top even when the first executable bid lies deeper. One-sided depth can support a liquidation. A missing side suppresses the midpoint. Crossed quotes suppress executable values and midpoints. An uncrossed or locked two-sided book supports the purchased-side midpoint, halfway between its bid and the ask implied by the other side's bid.
- **Price movement.** `gross_liquidation_cents` is the hypothetical sale proceeds. `executable_bid_change_cents` compares the decision and deadline executable bids. `gross_markout_cents` subtracts the hypothetical Stage 2A posted price from proceeds. `midpoint_change_cents` compares the two observed midpoints. The first departure record follows Stage 2A's definition (posted side lost its best price or the passive two-sided quote ceased). The source can be at or after that departure even if the original quote returned by the deadline.
- **Coverage.** Each eligible decision contributes once per horizon to primary or secondary coverage. Requested equals fresh plus stale plus missing. Fresh-book and fresh-liquidation counts can differ. `tape_anchored_5s_primary` includes the selected-game and observed-primary denominators. The original `coverage_gates.on_time_5s_marks` measures genuine entry-linked observations and stays unmeasured here.

## Execution support and selection

`scenario_strata` compares signed price movement across the frozen 0, 100, 250 and 500 ms arrival assumptions. Each group identifies role, delay, Stage 2A execution class, observed compatible prints, queue threshold comparison and ambiguous attribution. The evidence file includes displayed-ahead quantity at arrival, printed quantity, trade IDs and source record numbers. A zero-print active window, an ineligible arrival and an interrupted window retain distinct labels. A partial window may contain known prints. Ambiguous groups or attribution pairs split by an exposure boundary remain visible. Displayed decreases alone supply no evidence of a trade.

These are retrospective groups defined over the full Stage 2A exposure, which can extend past an early mark. They describe outcomes and cannot serve as signals at decision time. The same price observation appears in four sensitivity comparisons. Pooling those groups would count it four times.

The first eligible decision per NBA game is primary. The frozen tie order uses projected decision time, session ID, ticker and then YES before NO. It preferentially selects YES when both sides qualify together. Other sides, minutes, overlapping sessions and team tickers for that game are correlated secondary observations. Exact duplicate archives contribute once. Each scheduled exclusion retains its original reason. Any side-balanced or live-game sample requires a separately versioned prospective protocol and separate evaluation. [Stage 2A protocol](b3b2-stage2a-feasibility.md) · [Campaign rules](b3b2-stage2b1-campaign.md)

## Fees and provenance

The enrolled WebSocket capture has no contemporaneous series/event fee evidence. Its net values are null. Where separately archived evidence exists, the same command accepts `--fee-receipts <bundle.json> --fee-sha256 <trusted-digest>`. Supply the digest from independent custody and original public GET responses on the capture's own local monotonic clock. The digest verifies file identity. The custodian must establish clock origin, response provenance and the association with each journal.

The offline bundle format is an object with `format` equal to `b3b2-local-get-receipts-v1` and a `receipts` array. Each element contains the sealed journal's lowercase `journal_sha256`, `path` (one of `/markets/{ticker}`, `/events/{event_ticker}` and `/series/{series_ticker}`), `receipt_mono_ns`, `receipt_wall_ns` and `response` with the original wrapped `market`, `event` or `series` object. The reader picks the latest receipt for each path at or before the pricing time and requires all three to be less than 60 seconds old. It verifies the identity chain and skips markets with a waiver indication or unsupported fee terms. Receipt indices in the results are zero-based positions in the pinned file.

The existing versioned series fee model applies event overrides and its decimal rounding rules. A hypothetical maker purchase is priced at decision time and a one-contract taker sale at the deadline. Net liquidation proceeds deduct the sale fee. The net markout also deducts the posted entry price and maker fee. An unavailable fee stays null, with the applicable fee status on the mark. [Versioned fee model](../src/kalshi_edge_engine/engine/fees.py)

## November 22 four-game campaign

One sealed campaign archive supplied 96 eligible decisions, four YES primaries and 92 secondary decisions. All 384 requested outcomes are represented. Each of the eight secondary decisions in the final minute lacked enough remaining capture time for any requested horizon.

| Horizon | Fresh / stale / missing primary | Fresh / stale / missing secondary | Fresh primary gross moves, down / flat / up |
| --- | --- | --- | --- |
| 5 s | 4 / 0 / 0 | 54 / 30 / 8 | 1 / 3 / 0 |
| 15 s | 2 / 2 / 0 | 52 / 32 / 8 | 1 / 1 / 0 |
| 30 s | 4 / 0 / 0 | 58 / 26 / 8 | 0 / 3 / 1 |
| 60 s | 2 / 2 / 0 | 54 / 30 / 8 | 1 / 1 / 0 |

At zero assumed delay, one primary had a compatible public execution. Its gross marks were −1¢, −3¢, +4¢ and −6¢. The other three primaries had zero compatible prints and flat as-of gross marks, including stale observations. No verified net values were available. Campaign assessment remains `INCONCLUSIVE`.

For this roster and archive, two offline runs reproduced the following SHA-256 digests. A separate reconstruction directly from the raw snapshot and delta receipts checked the 352 priced observations and 32 truncated horizons.

| File | SHA-256 |
| --- | --- |
| Sealed `messages.jsonl` | `bfff732cb1bde1bcdf431b29161dcee53bb8432f19621ffd85063edb3420468c` |
| Stage 2B2 `summary.json` | `d034aaf1f823f839323d896755878e5e8214d5a4d0a4c53e1b2359bed8f593c5` |
| Stage 2B2 `episodes.jsonl` | `9c52b33b5c3ec19aa026a5148104aeadd57ea0c182a170afad1c43823aefb630` |
