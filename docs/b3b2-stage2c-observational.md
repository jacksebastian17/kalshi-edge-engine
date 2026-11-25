# Stage 2C prospective NBA book observations

## Frozen protocol v1

`nba-tipoff-books-v1` studies how displayed spreads, depth and the existing signal classification vary with proximity to the **scheduled** start. The unit is a distinct NBA game. This cohort contributes zero games to the original 100-game sample.

**Enrollment.** Before viewing study books, choose a future NBA slate date. Use that date's original `KXNBAGAME-YYMONDD` event ticker prefix and exhaust the open series listing. Sort distinct event tickers lexically and nominate the first two, or all if fewer. Selection ignores prices, activity, fees and scores. For each nomination require one exact-linked basketball-game milestone with an explicit timezone and one mutually exclusive NBA game event with exactly two distinct active binary team-win contracts. Freeze the contract IDs, milestone ID and scheduled start before the first window opens. Record short slates, failed verification and late enrollments. A failed nomination leaves its place empty. One write-once enrollment per slate prevents repeated draws.

| Window | Target relative to frozen start | Admissible receipt times |
|---|---:|---|
| Advance | −120 minutes | [−125, −115) minutes |
| Near start | −10 minutes | [−15, −5) minutes |
| Post-start | +30 minutes | [+25, +35) minutes |

**Sampling and misses.** The operator starts a single pass during a window. It samples due games only, with one attempt per game/window, and requests books in lexical contract order. Local UTC receipt time determines each contract's inclusion independently. Record request and receipt clocks, original response bytes, identifiers and hashes. Keep early/late receipts, GET failures, partial pairs, missing sides and interrupted attempts distinguishable. An unattempted past window remains missing. Future windows remain pending. No backfill, replacement, polling loop or schedule adjustment. Actual tipoff and game state are unknown in v1.

**Measurements.** Reconstruct both bid ladders with the frozen canonical parser. Retain decimal prices in cents and fractional displayed contracts. Report YES and NO best bid and implied ask, both sides' top depth, and their sum and maximum with explicit units. For each side, opposite depth is the other side's displayed best bid. Record the venue's price ranges and local tick at the bid when supported. Score the book using the base commit's unmodified scanner scoring defaults and contemporaneous public market/event/series fee inputs. Record the exact executed classification branch. Preserve partial book measurements and label unavailable metadata, grid, or fees. Only an in-window usable book contributes to observed coverage.

**Reporting and limits.** Report each game's two contract records in each window, and count complete, partial and missing games per window. Paired spread and side-specific depth means require two valid, uncrossed, two-sided books from the window. Across games weight each complete pair once. After the two book requests, read the enrolled milestone once. Retain its published status, updated time, and latest scheduled start if directly verifiable. Keep the original windows even if the schedule changes. Actual tipoff and independently verified game state stay unknown. Displayed quantities are snapshots of resting interest. Complementary contracts and repeated observations are correlated. Manual timing, REST caching, original-date discovery, lexical selection, schedule changes, sequential receipts and missing coverage limit generalization.

Sources for the public fields are the [milestone listing](https://docs.kalshi.com/api-reference/milestone/get-milestones), [event detail](https://docs.kalshi.com/api-reference/events/get-event) and [orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook).

In the frozen scorer, `SPREAD_TOO_SMALL` can mean the structural gap fell below its minimum, neither side supported an inside quote, or no eligible side met the net-edge minimum. The report names the branch for each contract. Scorer depth uses whole contracts at each top level. Decimal displayed depth is retained separately. [Signal scorer](../src/kalshi_edge_engine/engine/signal.py)

## Windows operator runbook

The two online commands use unsigned public GETs to Kalshi's production REST origin. Each invocation exits after one pass. The offline report reads only local files. V1 writes once under `%USERPROFILE%\kalshi-tipoff-books-research\nba-tipoff-books-v1`. Keep the whole directory together, including its milestone registry. [Public market-data access](https://docs.kalshi.com/getting_started/quick_start_market_data)

**Enroll.** Choose the original NBA slate date before inspecting its markets. November 27 is an example for an operator working on November 24. Enrollment must finish before the advance window opens for each game. Save the date in the same PowerShell window and display the frozen schedule in local time.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$py = (Resolve-Path '.venv\Scripts\python.exe').Path
$slate = '2026-11-27'
$json = & $py -m tools.tipoff_study enroll --slate $slate
if ($LASTEXITCODE) { throw 'Inspect the existing slate and retained evidence.' }
$study = $json | ConvertFrom-Json
$study.enrollment.nominees | ConvertTo-Json -Depth 5
$study.games.PSObject.Properties | ForEach-Object {
    $game = $_.Name
    $_.Value.windows.PSObject.Properties | ForEach-Object {
        [pscustomobject]@{
            Game = $game
            Window = $_.Name
            OpensLocal = ([datetimeoffset]$_.Value.opens_utc).LocalDateTime
            ClosesLocal = ([datetimeoffset]$_.Value.closes_utc).LocalDateTime
        }
    }
} | Sort-Object OpensLocal,Game | Format-Table
```

**Observe.** On explicit operator initiation, run this command once during each displayed interval. Aim for its midpoint before viewing market conditions. One invocation samples all games currently due. An empty `attempted_windows` list means this pass claimed no windows. Reopening PowerShell requires setting the same working directory, interpreter and slate again.

```powershell
& $py -m tools.tipoff_study observe --slate $slate
```

**Inspect offline.** Use a fixed, offset-qualified cutoff after the last scheduled window for the final report. The example below compares all slates available by November 29 at noon UTC. Reuse the exact cutoff to reproduce the output. During the study omit `--as-of` to inspect current coverage.

```powershell
$cutoff = '2026-11-29T12:00:00Z'
$json = & $py -m tools.tipoff_study report --slate $slate --as-of $cutoff
if ($LASTEXITCODE) { throw 'Slate evidence verification failed.' }
$result = $json | ConvertFrom-Json
$result.by_window | ConvertTo-Json -Depth 6
$result.games | ConvertTo-Json -Depth 12
& $py -m tools.tipoff_study report --as-of $cutoff
```

Each contract row identifies its book receipt. The window's receipt map lists the source endpoint, query, request and receipt times in Unix nanoseconds, elapsed monotonic time, HTTP outcome, filename and body SHA-256. Each receipt file preserves the response body as base64. Empty ladders, crossed books and parser issues retain their scorer diagnostics and do not enter paired means. A missing fee profile leaves the real scorer free to take its earlier structural rejection branch. An incomplete attempt retains any already published receipts. Its game stays in every planned window's denominator.
