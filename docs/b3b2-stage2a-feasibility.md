# B3B2 Stage 2A — passive-order feasibility on the sealed NBA tape

**48 eligible decisions came from 12 sampled minutes of one Atlanta–Detroit game.** At zero assumed delay, 39 decisions saw an opposing public print at the quoted price, including two whose windows were cut short. Only one decision had enough printed volume to cover the displayed quantity ahead plus one contract. Gross displayed removals crossed that threshold without supporting print volume in 22 complete exposures. These observations identify a source of optimism in a depletion-based queue calculation. Actual execution remains unidentified in all 48 decisions.

## Frozen protocol

This is a retrospective application of a prospective decision rule to a capture whose aggregate counts were already known. Sampling, quote-exit and queue-comparison rules were fixed before examining episode outcomes.

The decision grid starts 60 seconds after the journal's `started` record and advances in 60-second increments strictly before collection ends. At every tick, evaluate both YES and NO of each preflight-selected market. Read the last journal receipt at or before the local monotonic tick. Eligibility requires an initialized book, continuous acknowledged trade coverage, positive displayed bids on both outcomes, and a strictly positive implied spread. Record every exclusion. A decision fixes one contract at the then-displayed best bid. Later trades, quote duration and availability of a complete horizon play no role in inclusion.

The observation horizon ends 60 seconds after decision. Each market and side has disjoint decision windows. Sides, team contracts, successive minutes and the four delays are correlated views of a single game. Each delay is a paired counterfactual for the same decision. None represents an additional independent opportunity.

Arrival delays are 0, 100, 250 and 500 ms on the recorder's monotonic clock. Zero is the earliest local scenario. Venue arrival time is unobserved. At each assumed arrival, use the then-visible quantity ahead only if the original bid is still best and passive. Observe later receipts through the first best-price departure, loss of a passive two-sided quote, data interruption, or horizon expiry. Include the depth receipt that first moves the quote and record its new best. A return to the original best does not restart the episode. A price that moves and returns before the assumed arrival is reconsidered at arrival. This is a time-at-best research exposure with instantaneous local withdrawal. Real routing and cancellation could give a different exposure.

Use public, deduplicated, explicitly non-block opposing-taker prints at the same ticker, outcome and price. Count exact Stage 1.1 coincidences locally only when both records lie inside exposure. Record ambiguous groups and coincidences split by a boundary separately. A positive print quantity supports public execution at the quoted price. The descriptive FIFO comparison tests cumulative prints against displayed quantity at arrival plus one contract, assuming all displayed quantity initially has priority, later additions are behind, no cancellations ahead, and identical external flow. Actual priority, hidden liquidity, cancellation allocation and order impact are unknown. Gross removals crossing the same threshold without print support are potential false signals for a depletion-based proxy.

Signed server-send/local-receive differences describe two clocks. All ordering and sensitivity scenarios below use local receipts. Public exchange events may have occurred before their receipt or before the hypothetical order reached the venue. Absolute latency, causal event ordering relative to an unsent order, actual execution and informative guaranteed fill bounds remain unidentified.

## Source and verification

Run `b3b2-stage11-66884b73738e499a979ed8cb962719bc`, session `6bee14f020bb4f3c91adba1233419928`, contains 142,154 raw messages in 142,164 journal records. The investigation verified the manifest, close reason, record count, journal digest, final durable checkpoint and backup inventory before evaluating episodes. The seven captured pilot module hashes match commit `cfba2d7773250e0f2e4e3442b1abf9a944cad4b1`. The manifest records Python 3.13.14 and websockets 16.0. The study also checks the local canonical orderbook parser against that commit. The manifest's fingerprint covers the recorded modules and versions. It does not attest to the entire historical machine environment.

| Verified item | Value |
|---|---|
| Manifest SHA-256 | `48e235847ce0bcffa42b4661252d3fc0a713a96fb98fa6f669725cf76f886318` |
| Journal SHA-256 | `a77260fd0f6f398e42d16ff8589ca399f6cb58673e85d4aabbbf0eabc9d29aed` |
| Build fingerprint SHA-256 | `90bbee4f819d93ee920b7b1c42015a102ec7c543e1d2fd9bdbfc1b9b3e3f877e` |
| Depth subscription 1 | Sequences 1–139,274, initialized by ATL and DET snapshot records 9 and 10 |
| Trade subscription 2 | Sequences 1–2,878 |
| Continuity | One connection, zero observed gaps, reconnects, malformed segments or market-clock regressions |

The second pass hashed the exact journal bytes it replayed. All five source files had identical before/after hashes. An independent pass through the raw messages reconstructed both decision books, checked quantities at decision and arrival, and reconciled print, addition and removal receipt lists for all 188 scenarios that entered an exposure. Four of the 192 paired scenarios had already lost their quote at arrival. The median signed server-send/local-receive difference was +19.5778 ms. A hypothetical outbound order might face very different scheduling and transit times. [Stage 1.1 evidence contract](b3b2-stage1-pilot.md#reading-coverage)

## Results by assumed arrival delay

`PUBLIC_EXECUTION_AT_PRICE` means a qualifying print was observed during a fully observed exposure. If cancellations had already removed orders ahead, even a small print could be compatible with a one-contract execution. The same print could have served another order entirely. `NO_OBSERVED_EXECUTION_SUPPORT` means a completed exposure had zero qualifying prints. An execution would then require a departure from the complete, unchanged public-flow assumption. `QUOTE_INELIGIBLE_AT_ARRIVAL` means the research policy would have opened no exposure at that delay. `DATA_INCOMPLETE` retains whatever prefix was observable before interruption or the capture boundary. All four categories describe evidence about an unsent order.

All 48 scheduled market-side decisions qualified at decision time. Each row reuses them. The last four decisions had less than one second of capture remaining and are included as incomplete. An observed print in an incomplete window remains evidence about that prefix. Conclusions about the rest of its horizon are unavailable.

| Delay | Eligible at arrival | Complete with prints | Complete without prints | Incomplete | Ineligible at arrival | Any observed print, including prefixes | Printed volume ≥ ahead + 1 | Removals alone cross threshold |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 ms | 48 | 37 | 7 | 4 | 0 | 39 | 1 | 22 |
| 100 ms | 48 | 37 | 7 | 4 | 0 | 38 | 1 | 22 |
| 250 ms | 46 | 37 | 5 | 4 | 2 | 38 | 0 | 22 |
| 500 ms | 46 | 35 | 7 | 4 | 2 | 35 | 0 | 21 |

At zero delay, 32 exposures ended when the quote moved and 12 reached expiry. Of the 39 with any print, 38 had less printed volume than the displayed-ahead-plus-one threshold. Nineteen complete exposures touched an ambiguous trade/depth group. Thirty-six complete exposures included an exact pair. These categories overlap. At 500 ms, two original quotes were ineligible at arrival, 30 exposures ended at a price change and 12 expired. Four episode classifications differed from zero delay. Delay changes both the starting displayed quantity and which later receipts qualify.

**Receipt-level examples**

- **ATL YES, decision record 19,588.** At 57¢, displayed quantity ahead was 3,794.00 contracts. Subsequent removals at that level totaled 4,356.78 through quote departure at record 20,363. Across all four delay scenarios the compatible public print count was zero. The cause of the removals remains unknown.
- **DET NO, decision record 122,559.** At 44¢, the 0 and 100 ms scenarios had 57.33 contracts ahead. Print records 122,670, 122,677 and 122,719 supplied 17.32, 34.64 and 8.66 contracts, totaling 60.62. This was the sole displayed-ahead-plus-one crossing. At 250 ms the displayed quantity was 137.82 and only 8.66 printed afterward. At 500 ms the quantity was 129.16 and no subsequent qualifying print preceded quote departure at 122,918. Even the zero-delay crossing depends on the receipt-time, FIFO and external-flow assumptions.

## Displayed removals and the B3B1 heuristic

| Full-capture quantity | Events | Contracts |
|---|---:|---:|
| Negative depth updates | 72,205 | 108,396,755.38 |
| Unique, direction-classified non-block public prints | 2,878 | 345,187.97 |
| One-to-one exact coincidences | 1,690 pairs | 138,016.48 |
| Decreases without an exact pair | 70,515 | 108,258,738.90 |
| Prints without an exact pair | 1,188 | 207,171.49 |

All printed volume occurred at ticker/side/price combinations that also had a decrease somewhere in the capture. The 352 ambiguous groups involved 2,380 receipts. Neither a common price level nor identical fields establish the cause of a depth update. Gross removed volume was about 314 times total classified printed volume. Repeated removals can consume replenished displayed size, and neither the amount canceled nor its position in the queue is observable.

B3B1 initialized queue ahead from the ceiling of displayed size and consumed the floored decrease between consecutive at-quote polls. A queue-cleared candidate required inferred flow beyond modeled ahead. Stage 2A measures gross WebSocket removals during each exposure. Polling can net intervening additions against removals, so these counts describe the proxy's failure mode rather than reproduce a B3B1 poll or random draw. This game establishes that treating every displayed decrease as execution can materially overstate supporting volume. The earlier November 21 Sacramento paper decision came from a different window with no paired trade tape. This archive cannot adjudicate that fill or calibrate its 10% assumption. [B3B1 run forensics and research protocol](b3b2-run-forensics.md)

## Reproduce offline

Run from the checkout using the existing project environment. Each invocation below publishes results in a fresh directory outside the source run. Omit `--detail` for only the compact JSON, or omit both output arguments for JSON on stdout.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$archive = Join-Path $env:USERPROFILE 'kalshi-microstructure-pilots\b3b2-stage11-66884b73738e499a979ed8cb962719bc\backup'
$outputDir = Join-Path $env:USERPROFILE ('kalshi-shadow-research\b3b2-stage2a-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m tools.shadow_feasibility --archive $archive --output $outputDir --detail
if ($LASTEXITCODE -ne 0) { throw 'Stage 2A analysis failed' }
Get-ChildItem -LiteralPath $outputDir -File | Get-FileHash -Algorithm SHA256
```

The saved `summary.json` and `episodes.jsonl` have SHA-256 digests `fe170474e7d36bee7fac4f1eb85b03481b2324ab727507c13714147a44b63004` and `baa1d3f5e082b19d724d579940a7a6e1df99ad987f7fbefa1612baf80b211263`. Each JSONL line contains one decision and its four scenarios. Receipt references are one-based journal lines. An ambiguous-group reference is its earliest member. A boundary-split pair identifies the inside and outside receipts. Only pairs whose two members fall in the exposure enter its exact-pair count.

## Stage 2B recommendation

**GO for prospective, game-clustered measurement. Fill-probability calibration remains inconclusive.** Freeze the protocol before collecting independent games on multiple dates, retain misses and truncated episodes, and pair each paper decision with contemporaneous public feeds. Compare receipt-order sensitivity and both sides of boundary-split pairs. Independently check print completeness and clock alignment. The published protocol calls for at least 100 non-overlapping primary episodes from 100 games across at least 10 dates. This archive contributes one game. Private queue priority, actual venue arrival and counterfactual market impact still limit what a larger public tape can establish. [Prespecified research protocol](b3b2-run-forensics.md#proposed-b3b2-protocol)
