# B3B2 Stage 1.1 forensic addendum

**GO for one more 12-minute, two-market NBA measurement pilot.** The original archive supports within-subscription ordering and 23 reproducible public-print coincidences. Its timing cannot support one-way latency, cross-clock causal ordering or fill calibration.

## Reproduce the historical result

From the project checkout, run the following offline. The report reads the existing sealed backup and prints JSON. The historical regression test separately decodes the original Base64 payloads, rebuilds both ladders, scans all negative updates for each print and checks that the same 23 distinct source pairs emerge. It checks every file hash in the historical run before and after replay. On machines without this external archive, only the historical test skips.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
.\.venv\Scripts\python.exe -m tools.microstructure report --run b3b2-stage1-1795332429457
.\.venv\Scripts\python.exe -m pytest tests/test_microstructure_pilot.py -k historical_matches -q
```

The capture ran November 22, 2026, 07:27:34–07:37:34 UTC (November 21 Pacific). Session `6f01815e38d14c1cb119edfe048efe64` contains 333 journal records, including 326 raw WebSocket receipts. The raw journal SHA-256 is `2e0fd00c8b3cfd8f8ca7e36544478709908b0044add56fb55a3dc878e1eeb211`. The archived manifest has no final build fingerprint. The report preserves that historical provenance limitation. The verification covers the stored bytes and the observed sequences. Independent copies of all exchange transmissions are unavailable.

Two independent Stage 1.1 CLI processes produced byte-identical Windows stdout with SHA-256 `6c9b03b65337a16ce8adbe2f14fc5b0b9db6daeb34e1f3d31f0bba84d8ffe7d1`. This fingerprint includes the CLI's line endings.

## Investigating the negative wall-clock differences

Kalshi's optional envelope field `sending_ts_ms` gives the Unix millisecond when it queued the message at the network layer. The nested `msg.ts_ms` supplies the exchange event time used in the coincidence check. On receipt, the recorder takes `time.time_ns()` and `time.monotonic_ns()` immediately after the awaited WebSocket receive returns, before Base64 encoding, journal writes and book parsing. TLS, decompression, buffering, event-loop scheduling and earlier recorder flushes can all precede that sample. It is an application receive time. [Kalshi depth schema](https://docs.kalshi.com/websockets/orderbook-updates) · [Kalshi trade schema](https://docs.kalshi.com/websockets/public-trades) · [Python clock semantics](https://docs.python.org/3/library/time.html#time.time_ns)

The journal serializes both local timestamps as JSON integers. The inspector calculates `(receive_wall_ns - sending_ts_ms * 1_000_000) / 1_000_000` in milliseconds, doing the subtraction in integer nanoseconds. No local timezone, string parsing or epoch-sized floating-point conversion enters that calculation. The server stamp has millisecond granularity. A timezone or rounding error here cannot explain a roughly 439 ms negative median. No offset correction was applied.

| Message population | Samples | Negative | Minimum ms | Median ms | p95 ms | Maximum ms |
|---|---:|---:|---:|---:|---:|---:|
| Depth subscription, both message types | 301 | 300 | −447.6237 | −438.9220 | −434.2458 | +48.7723 |
| ↳ Snapshots | 2 | 2 | −432.8371 | −432.5690 | −432.3008 | −432.3008 |
| ↳ Deltas | 299 | 298 | −447.6237 | −438.9395 | −434.2668 | +48.7723 |
| Trade subscription | 23 | 23 | −446.0052 | −438.8068 | −434.7947 | −434.3126 |
| All data messages | 324 | 323 | −447.6237 | −438.9081 | −434.2668 | +48.7723 |

The two acknowledgements are outside this timing population. The p95 uses nearest rank. The only positive observation was journal record 17, depth sequence 9. Its individual buffering and transit times are unknown. The signed difference combines delivery and application delay with host clock error minus exchange clock error. Delivery delay alone cannot explain a receipt stamped hundreds of milliseconds before the server queued it.

| Monotonic minutes since start | Deltas | Delta median ms | Trades | Trade median ms |
|---|---:|---:|---:|---:|
| 0–2 | 50 | −434.8654 | 3 | −434.7947 |
| 2–4 | 72 | −436.8567 | 6 | −436.6844 |
| 4–6 | 82 | −439.3436 | 3 | −438.8068 |
| 6–8 | 53 | −443.7865 | 5 | −442.4155 |
| 8–10 | 42 | −446.3951 | 6 | −444.9164 |

Across the journal, elapsed wall time minus elapsed monotonic time ranged from −0.0020 to +0.0006 ms and ended at −0.0008 ms. Both message streams show a changing relative difference. The paired local clocks show no comparable step. A common rate error relative to UTC, exchange-clock behavior and changes in delivery or scheduling can all affect that trend. The samples cannot separate their contributions.

A read-only Windows Time query after the pilot showed W32Time running, leap indicator 3 (unsynchronized), stratum 0 and a last successful sync at 16:48:41 Pacific. Its last sync error reported unavailable time data. System events 37 and 35 corroborated a successful time-source observation around 16:48, several hours before capture. The historical journal has no clock-status query at either boundary. These later diagnostics make host-clock offset plausible. They provide neither the capture-time offset nor an independent bound on the exchange clock. `clock_alignment_status`, one-way latency and cross-clock causal ordering are **indeterminate**. Future captures preserve the output or failure of read-only status queries at both boundaries. [Windows Time diagnostics](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings)

## Reconstructing the 23 coincidences

Depth subscription 1 was acknowledged at record 7. Records 8 and 9 initialized ATL and OKC. Trade subscription 2 was acknowledged at record 10. Every candidate below falls in that single uninterrupted overlap. Depth sequences were 1–301 and trade sequences 1–23. The ATL feed had 19 repeated delta clocks and 147 upward transitions larger than one. OKC had zero repeats and 109 such transitions. Neither market regressed. No subscription sequence was missing. Exchange clocks are local to a market and can reset during maintenance. An unexplained in-segment regression invalidates the affected book, and a fresh snapshot supplies a new baseline. A lower first clock after an interruption has unknown history. Neither case identifies maintenance by itself. [Real-time clock and recovery semantics](https://docs.kalshi.com/websockets/orderbook-updates)

Kalshi expresses taker book side in YES orientation. A `bid` corresponds to a YES taker and meets resting NO bids, compared at the NO price. An `ask` corresponds to a NO taker and meets resting YES bids at the YES price. The auditor requires agreement among all three direction fields, complementary side prices, and an explicit false block flag. The table uses the resting outcome and its own price. ATL denotes `KXNBAGAME-26NOV22DETATL-ATL`, OKC denotes `KXNBAGAME-26NOV22OKCMIA-OKC`. Record numbers are one-based lines in the sealed journal. The JSON includes both subscription IDs, both sequences, snapshot record and depth clock as well. [Trade fields and direction](https://docs.kalshi.com/websockets/public-trades) · [Orderbook fields](https://docs.kalshi.com/websockets/orderbook-updates)

| Trade ID | Market | Resting side | Price ¢ | Contracts | Exchange ms | Print record | Decrease record |
|---|---|---|---:|---:|---:|---:|---:|
| 072a0925-9361-9e36-252c-24499a2d7ed9 | ATL | no | 54.0000 | 4.18 | 1795332516129 | 22 | 21 |
| 072a0924-4ca1-94aa-23d8-bd70adbf0eb1 | ATL | no | 54.0000 | 0.52 | 1795332530307 | 37 | 36 |
| 072a094c-daf1-a985-11ff-000e75c2a06a | OKC | no | 56.0000 | 1.09 | 1795332562615 | 59 | 58 |
| 072a094a-edb1-b401-14d6-697ad118776e | OKC | no | 56.0000 | 0.54 | 1795332592981 | 75 | 76 |
| 072a0981-1751-99ae-740a-631263888cc7 | OKC | yes | 42.0000 | 1.67 | 1795332611634 | 87 | 88 |
| 072a0980-0131-b70a-0e15-d7ae2d4166af | OKC | yes | 42.0000 | 10.93 | 1795332628744 | 94 | 93 |
| 072a0922-e7a1-990e-8569-c5ce87f53d85 | ATL | no | 54.0000 | 0.22 | 1795332634507 | 102 | 101 |
| 072a092d-80e1-a06a-63f2-8392f1477e1d | ATL | no | 54.0000 | 10.47 | 1795332648510 | 115 | 114 |
| 072a092f-a481-99a2-2939-100c7ca25975 | ATL | no | 54.0000 | 67.03 | 1795332679718 | 132 | 131 |
| 072a0929-2de1-95ee-a32a-8c46d75e003d | ATL | no | 54.0000 | 41.89 | 1795332704931 | 146 | 145 |
| 072a0941-1051-b211-e4cc-e724bb1508f2 | OKC | no | 56.0000 | 27.00 | 1795332747437 | 171 | 172 |
| 072a095f-3b11-95d5-7746-d8ad830e5256 | OKC | no | 56.0000 | 10.08 | 1795332777320 | 211 | 212 |
| 072a095c-f4d1-964d-2cb6-3092108ca3f2 | OKC | no | 56.0000 | 21.00 | 1795332823882 | 228 | 227 |
| 072a095b-8131-9f35-a87d-fddd1a53602a | OKC | no | 56.0000 | 21.86 | 1795332836299 | 236 | 237 |
| 072a095b-c2b1-8049-7568-a7fb88e4121a | OKC | no | 56.0000 | 10.93 | 1795332840510 | 239 | 238 |
| 072a095a-9f91-8419-6f74-985d8b3f2966 | OKC | no | 56.0000 | 3.56 | 1795332853297 | 252 | 251 |
| 072a0956-3291-b39d-b796-984ba65bb64e | OKC | no | 56.0000 | 10.93 | 1795332925499 | 279 | 278 |
| 072a093f-74e1-a382-14d6-299e691029d1 | ATL | no | 54.0000 | 5.25 | 1795332936617 | 287 | 288 |
| 072a0954-94b1-b1e9-34b2-8266b3eca7ba | OKC | no | 56.0000 | 43.73 | 1795332952490 | 292 | 291 |
| 072a0953-82d1-b379-5dea-7fa587f1f0b6 | OKC | no | 56.0000 | 21.86 | 1795332967902 | 302 | 301 |
| 072a0952-ca51-a3f9-7f1b-c8cc5fd0ed7e | OKC | no | 56.0000 | 1.64 | 1795332987152 | 303 | 304 |
| 072a093a-8241-96a2-5977-521929e96925 | ATL | no | 54.0000 | 6.28 | 1795333025274 | 320 | 319 |
| 072a0945-4f21-bec2-1339-40e5e426179d | ATL | no | 54.0000 | 52.36 | 1795333038829 | 327 | 326 |

All 23 prints and all 23 paired decreases have distinct source records. Several prints arrived locally before the paired delta. The audit uses exchange-millisecond equality without a cross-channel sequence or arrival-order constraint. It groups by connection, shared continuity segment, snapshot, ticker, resting side, price and event millisecond. A group qualifies only with one eligible print, one decrease and equal fixed-point quantity. A replacement snapshot, sequence gap or reconnect breaks eligibility across that boundary. A conflicting sequence conservatively disqualifies coincidences on that connection. A conflicting trade ID disqualifies that print, including earlier occurrences.

There were 168 valid decreases and 23 eligible prints, leaving **145 unidentified decreases and zero unmatched non-block prints**. An unmatched print is still an observed execution. Unequal quantities may reflect partial execution, and multiple prints or decreases can reflect batching. Replenishment can offset consumption. A cancellation can remove displayed size. The feed supplies no decomposition of these causes for an unidentified change and no shared execution identifier for the 23 coincidences. None of the pairs establishes an individual order's priority or fill.

## Decision for the next collection

| Evidence dimension | Historical finding |
|---|---|
| Connection and subscriptions | Both channels acknowledged on one authenticated connection |
| Durable receipts | Manifest, seal, journal count, final checkpoint and backup hashes verified |
| Depth reconstruction | Both books initialized, all 299 deltas applied, zero observed gaps or regressions |
| Trade-feed integrity | 23 unique IDs across contiguous observed sequences. Silent intervals have no independent delivery check |
| Clock alignment and latency | Indeterminate. Host/venue clock offset during capture is unbounded by this evidence |
| Print/depth attribution | 23 exact field coincidences. Causality and the 145 other removals remain unidentified |

Use a fresh run and two reviewed, active NBA game markets. Seek activity on both feeds over the full 12 minutes. Review both clock-status observations, per-channel trends, sequence ranges and ambiguous or unmatched events before expanding collection. These data support another measurement run. Timing-sensitive execution modeling requires independent clock evidence, and queue calibration requires additional attribution evidence. [Exact PowerShell commands](b3b2-stage1-pilot.md#windows-operation)

**GO for the next short prospective collection.**
