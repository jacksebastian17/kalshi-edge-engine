# B3B2 Stage 1 — public market-data capture pilot

Use this pilot to test authenticated access, reconstruction and continuity over 10–15 minutes. It records the two public subscriptions in a new directory under `%USERPROFILE%\kalshi-microstructure-pilots`. It runs independently of the backend and paper simulator. [Research protocol](b3b2-run-forensics.md#proposed-b3b2-protocol)

## Windows operation

Use the project's installed pilot environment. Keep one PowerShell window open for the job and its run variables. Each preflight below creates a fresh name in the isolated research root.

**1. Preflight.** Review the two open NBA game tickers, current status and close times. For an explicit set, replace `--count 2` with `--tickers` followed by two to four active NBA game tickers. If market validation fails, the command records the failure in that run directory.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$repo = (Get-Location).Path
$py = Join-Path $repo '.venv\Scripts\python.exe'
$run = 'b3b2-stage11-' + [guid]::NewGuid().ToString('N')
& $py -m tools.microstructure preflight --run $run --count 2
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed' }
```

**2. Start the 12-minute capture** after reviewing the selection. Capture rechecks the same markets on every connection. It journals a read-only Windows Time status query before connecting and after collection, including a diagnostic if the query is unavailable.

```powershell
$pilotJob = Start-Job -ArgumentList $repo,$py,$run -ScriptBlock {
    param($dir,$python,$name)
    Set-Location -LiteralPath $dir
    & $python -m tools.microstructure capture --run $name --minutes 12
    if ($LASTEXITCODE -ne 0) { throw 'Capture ended with a fault' }
}
```

**3. Inspect live health.** Check both acknowledgements, initialized books, per-market counts, durable bytes and the latest fault.

```powershell
& $py -m tools.microstructure status --run $run
```

**4. Gracefully stop early if needed** while the status is `recording`. For the full 12-minute pilot, let the duration expire and continue to step 5.

```powershell
& $py -m tools.microstructure stop --run $run
```

**5. Wait for closure and create the verified backup.** The writer seals at shutdown. Backup checks the seal and final checkpoint, copies the files, and verifies their hashes.

```powershell
Receive-Job -Job $pilotJob -Wait -ErrorAction Stop
& $py -m tools.microstructure backup --run $run
if ($LASTEXITCODE -ne 0) { throw 'Backup verification failed' }
```

**6. Report offline.** The report reads the sealed backup. For an interrupted run, `report --run $run --unsealed-prefix` reads the original durable prefix and identifies a torn final line.

```powershell
& $py -m tools.microstructure report --run $run
```

## Evidence contract

The first production pilot ran on November 21, 2026 (Pacific), using run `b3b2-stage1-1795332429457`, session `6f01815e38d14c1cb119edfe048efe64`. The recorder ran 600.442 s with a 599.684 s connected window. Both authenticated subscriptions succeeded. The sealed backup contains 326 raw messages, including two subscription acknowledgements, in 333 journal records.

| Selected active market | Snapshots | Deltas | Unique public trades | Negative depth updates |
|---|---:|---:|---:|---:|
| `KXNBAGAME-26NOV22DETATL-ATL` | 1 | 177 | 9 | 96 |
| `KXNBAGAME-26NOV22OKCMIA-OKC` | 1 | 122 | 14 | 72 |
| **Total** | **2** | **299** | **23** | **168** |

Depth subscription 1 covered sequences 1–301. Trade subscription 2 covered 1–23. Replay found zero sequence gaps, reconnects, malformed frames or conflicting duplicates. All 23 public prints had a unique exact coincidence under the rule below. **145 decreases remained unmatched.** Those removals have no identified cause. Raw journal SHA-256 was `2e0fd00c8b3cfd8f8ca7e36544478709908b0044add56fb55a3dc878e1eeb211`. Two executions of the original Stage 1 inspector produced identical Windows stdout, SHA-256 `dc2eb1300aac21e814ff8b3a860d337127c8c79d0495a50f574961c17a492f11`. Backup hashes were identical before and after replay.

**Decision.** This sample establishes read-only feed access and sufficient field coverage to proceed with prospective collection. Of 324 send-to-receive wall-clock differences, 323 were negative, with a median of −438.9 ms. Measure and verify clock alignment before using local arrival times for latency or cross-clock causal ordering. Order priority and hypothetical fills remain unidentified.

The first capture ran during implementation. Its manifest predates the final preflight build fingerprint. The committed inspector verified the sealed raw data. New preflights additionally bind the pilot module hashes and Python/WebSocket versions. The six public regression-fixture messages are the two acknowledgements, first two books, depth sequence 3 and trade sequence 1 from this run, stored one JSON message per line. [Recorded fixture](../tests/fixtures/b3b2_public_frames.jsonl)

## Protocol and storage

The handshake uses RSA-PSS/SHA-256 over the timestamp, `GET` and `/trade-api/ws/v2`. The production endpoint is pinned to `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. Signed handshakes refuse redirects. The client sends one ticker-filtered `subscribe` for each public channel. The WebSocket library handles protocol ping/pong. The only REST requests are market discovery and market detail GETs. [Authentication and endpoints](https://docs.kalshi.com/getting_started/quick_start_websockets) · [Subscription acknowledgements and errors](https://docs.kalshi.com/websockets/websocket-connection)

- **Orderbook.** A snapshot replaces both ladders for its ticker. The documented `yes_dollars_fp` and `no_dollars_fp` sides may be absent when empty. Price/quantity pairs pass through the project's canonical decimal parser. A delta updates the specified side and dollar price by the signed `delta_fp`; zero resulting size removes the level. Invalid precision, bad snapshots, negative resulting sizes or deltas before initialization invalidate the segment. The subscription requests default, unconflated updates. Per-market exchange clocks may repeat or skip. [Orderbook schema and recovery](https://docs.kalshi.com/websockets/orderbook-updates)
- **Trades.** Raw payloads retain trade IDs, ticker, side-specific prices, `count_fp`, taker sides, block flag, exchange timestamps and the send timestamp when present. Unique trades are deduplicated by ID across reconnects. Conflicting content for one ID raises a fault. If a feed omits the sequence, the pilot can still count distinct prints and labels continuity unverified. [Public trade schema](https://docs.kalshi.com/websockets/public-trades)
- **Continuity.** The local record number orders arrivals. Exchange sequences are checked by connection and acknowledged subscription ID across all selected tickers. A gap, conflicting repeat, malformed input, lost connection or initial snapshot timeout records a fault. Recovery opens a fresh authenticated connection and requires fresh acknowledgements and snapshots. Fresh coverage starts a new segment; earlier holes remain visible. Byte-identical payloads remain in the journal even when replay suppresses duplicate state changes. The two channels have independent sequence domains.
- **Durability.** The manifest fixes the selected market set. Each line in `messages.jsonl` contains local record number, receive wall-clock nanoseconds, monotonic nanoseconds, event kind and value. A `raw` value is the original application-message bytes encoded in Base64. It includes every supported metadata field and preserves unknown fields. Control events record attempts, subscriptions, faults and closure. The reader sees message payloads after transport decompression. TLS packets and protocol ping frames are outside the dataset.
- **Health and isolation.** The single writer creates its files exclusively, checkpoints with flush and fsync about once a second, and records the durable prefix, disk space and feed state. A hard crash can lose the last unflushed prefix tail. Stale health means the writer needs inspection. Low free space (64 MiB reserve) or the 1 GiB journal limit ends capture. A run cannot resume or share an older experiment directory. Raw records are never pruned. The seal binds the manifest, raw journal digest, record count and close reason. The backup additionally hashes health and seal files. These hashes support byte verification; keep a trusted copy externally if independent custody is required.

## Reading coverage

The report lists requested markets, acknowledgements, snapshots, deltas, unique trades, duplicate/conflicting IDs, gaps, reconnects, faults and per-connection segment boundaries. Counters under each ticker reflect observations for that market. Missing optional event timestamps are explicit limitations for matching. Server-send to local-receive lag includes local clock offset. The monotonic connected-window measurement and total session duration are reported separately.

An **unmatched decrease** is a valid negative depth update without a unique public-print coincidence under the pilot's narrow check. The comparison requires an uninterrupted common segment, the same ticker, resting side, price, exact fixed-point quantity and exchange millisecond, and an explicitly non-block trade. More than one candidate on either side leaves the decrease unmatched. Differences in timestamps, batching, partial fills, missing data and cancellations can all contribute. Even an exact coincidence supplies no causal trade-to-delta identifier.

Read the six entries under `coverage_classifications` independently. They grade connection and subscription acknowledgements, verified durable receipts, snapshot-based depth continuity, trade continuity, relative clock alignment, and print/depth attribution. The legacy `coverage` field summarizes observed feed coverage. `short_collection_only` scopes the suitability decision to another measurement run. Timing and attribution have their own limits even when every subscription sequence is contiguous.

For real-time depth, the first delta after a snapshot must have a higher clock for that market. Subsequent deltas can repeat or skip clocks. A same-market regression interrupts the reconstruction until a replacement snapshot. A lower baseline after a disconnect is recorded separately. Maintenance can reset clocks. These two subscriptions carry no authoritative maintenance interval. Inspect `market_clock_boundaries`, snapshot record numbers and each subscription's observed sequence range. Records before a fault remain auditable. A reconnect starts fresh subscriptions and book baselines. [Kalshi real-time rules](https://docs.kalshi.com/websockets/orderbook-updates)

The timing section reports signed wall-clock differences, p95 by nearest rank, two-minute windows on the local monotonic timeline, clock-query observations and wall-versus-monotonic elapsed time. Source record numbers in `exact_coincidence_evidence` index the original journal from one. Untimed candidates, unknown classification or conflicting identities can prevent a coincidence from qualifying. `capture_build_provenance` reports the fingerprint supplied by that capture's manifest. [Stage 1.1 timestamp investigation and 23-row audit](b3b2-stage1-1-forensics.md)
