# B3B2 Stage 1 — public market-data capture pilot

Use this pilot to test authenticated access, reconstruction and continuity over 10–15 minutes. It records the two public subscriptions in a new directory under `%USERPROFILE%\kalshi-microstructure-pilots`. It runs independently of the backend and paper simulator. [Research protocol](b3b2-run-forensics.md#proposed-b3b2-protocol)

## Windows operation

1. **Preflight in window 1.** Use a fresh run name. The optional dependency is included in the development install. Preflight reads the existing credential configuration, discovers open markets in the NBA game series, ranks active markets by reported 24-hour volume with ticker as the tie-breaker, and checks the chosen market details. Review the printed names, status and closing times. Discovery failure leaves a diagnostic in the requested directory. A new attempt uses a fresh name.

   ```powershell
   Set-Location 'C:\Users\jacks\kalshi-edge-engine'
   .\.venv\Scripts\python.exe -m pip install -e '.[pilot]'
   $run = 'b3b2-nba-pilot-01'
   .\.venv\Scripts\python.exe -m tools.microstructure preflight --run $run --count 2
   ```

   For an explicit selection, replace `--count 2` with `--tickers` followed by two to four exact ticker arguments. Every requested ticker must appear in the open `KXNBAGAME` listing and pass a separate active-state and close-time check. Capture rechecks that same set on each connection. If a selected market has closed, prepare a new run with an explicitly reviewed selection.

2. **Start capture in window 1.** Keep the window open. This example stops automatically after 12 minutes. The CLI accepts 10 through 15 minutes.

   ```powershell
   .\.venv\Scripts\python.exe -m tools.microstructure capture --run $run --minutes 12
   ```

3. **Inspect health in window 2.** Check both acknowledged channels, the initialized books, per-market deltas and trades, growing durable byte/record counts and the latest fault. An acknowledgement alone establishes subscription access. A quiet market can have zero observed trades.

   ```powershell
   Set-Location 'C:\Users\jacks\kalshi-edge-engine'
   $run = 'b3b2-nba-pilot-01'
   .\.venv\Scripts\python.exe -m tools.microstructure status --run $run
   ```

4. **Stop gracefully if needed.** In window 2, issue the following command while capture is running, or press Ctrl+C once in window 1. Wait for the capture command to finish. A terminal status of `sealed` indicates that the writer closed and wrote its seal. An abrupt process exit leaves the run unsealed.

   ```powershell
   .\.venv\Scripts\python.exe -m tools.microstructure stop --run $run
   ```

5. **Create the verified backup** from either window. The command verifies the source, copies into a fresh partial directory, flushes each file, verifies the copy and publishes it as `backup`. Repeating the command verifies an existing copy. It refuses an unsealed capture.

   ```powershell
   .\.venv\Scripts\python.exe -m tools.microstructure backup --run $run
   ```

6. **Produce the offline coverage report.** This command reads the backup and replays the raw journal without loading credentials or contacting the exchange. Compare its file hashes independently with PowerShell. For diagnosis of a stopped, unsealed run, `report --run $run --unsealed-prefix` reads the original journal and flags a torn final line.

   ```powershell
   .\.venv\Scripts\python.exe -m tools.microstructure report --run $run
   $backup = Join-Path $env:USERPROFILE "kalshi-microstructure-pilots\$run\backup"
   Get-Content -LiteralPath (Join-Path $backup 'backup-hashes.json')
   Get-ChildItem -LiteralPath $backup -File | Get-FileHash -Algorithm SHA256
   ```

## Evidence contract

The first production pilot ran on November 21, 2026 (Pacific), using run `b3b2-stage1-1795332429457`, session `6f01815e38d14c1cb119edfe048efe64`. The recorder ran 600.442 s with a 599.684 s connected window. Both authenticated subscriptions succeeded. The sealed backup contains 326 raw messages, including two subscription acknowledgements, in 333 journal records.

| Selected active market | Snapshots | Deltas | Unique public trades | Negative depth updates |
|---|---:|---:|---:|---:|
| `KXNBAGAME-26NOV22DETATL-ATL` | 1 | 177 | 9 | 96 |
| `KXNBAGAME-26NOV22OKCMIA-OKC` | 1 | 122 | 14 | 72 |
| **Total** | **2** | **299** | **23** | **168** |

Depth subscription 1 covered sequences 1–301. Trade subscription 2 covered 1–23. Replay found zero sequence gaps, reconnects, malformed frames or conflicting duplicates. All 23 public prints had a unique exact coincidence under the rule below. **145 decreases remained unmatched.** Those removals have no identified cause. Raw journal SHA-256 was `2e0fd00c8b3cfd8f8ca7e36544478709908b0044add56fb55a3dc878e1eeb211`. Two independent offline report executions produced identical Windows stdout, SHA-256 `dc2eb1300aac21e814ff8b3a860d337127c8c79d0495a50f574961c17a492f11`. Backup hashes were identical before and after replay.

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

The pilot declares the feeds suitable for the next validation stage only after a sealed ten-minute session with continuous captured segments, a snapshot, delta and non-block trade for every selected market, and usable clock, timing and trade-classification fields. Reported latency and raw volume guide the next collection design. A quiet or interrupted interval yields insufficient evidence. Public order priority, cancellation attribution and paper-fill calibration remain research questions.
