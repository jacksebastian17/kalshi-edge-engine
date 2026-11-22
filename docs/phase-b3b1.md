# Phase B3B1 — Decision-linked execution evidence

B3B1 records which exchange responses a paper decision consumed, the actual stochastic test it performed and the action that reached the SQLite ledger. The new cohort is `decision_linked_b3b1_v1`. The strict launcher still uses the existing effective strategy, fee model, read budget and risk settings, with both optional heuristic entry paths disabled.

## Receipt identity and lifecycle

A raw observation has the immutable identity **(recorder session ID, receipt sequence)**. The session changes on restart. Sequence starts at one and advances even for a rejected capture. New append-only recorder files enforce a unique SQLite index on the pair. The receipt timestamp is local HTTP arrival time in nanoseconds. Ticker, kind, source, universe and originating intent travel with the reference. Market, orderbook, event and series detail responses retain their original bytes. Discovery lists remain outside the recorder contract.

The client publishes the receipt into the consumer's request scope after capture. The response dict and fixed-point parsing stay available through the existing GET interfaces. A fee-cache hit carries the market, event and series references from the original fetches. Each concurrent scanner task and simulator poll collects its own evidence. A queued reference claims only that capture accepted the body into memory. A committed recorder row and its clean session seal establish durability in the offline verifier. A dropped reference or an empty required list is missing evidence.

| Stage | Evidence and storage |
|---|---|
| Signal | The scanner scores its canonical book with the actual fee inputs. The signal carries all four receipts and the score time. |
| Submission | `quote_intents.submission_evidence` version 2 stores the signal set, separate submission book and market-state references, decision time, model version and effective execution configuration. The ledger retains side, price, status, market observation and fee profile. |
| Poll | `entry_polls.evidence` stores the current book and prior displayed-queue book dependency, observed top and quote quantity, whole-contract capacity, queue transitions, estimated volume and its source. |
| Random test | The journal stores the evaluated path, eligibility, effective probability, exact draw, comparison result, proposed size and no-draw reason. Ineligible polls consume zero random values. Eligible polls consume the one draw used for the decision. |
| Validation and policy | The guarded runtime records its separate market response, validation time and state before booking. Rejections retain their reason. The candidate ID and intent ID join directly to `paper_fills`. That committed row is authoritative if the process stops between journal updates. |
| Outcome and exit | Each observed 5/15/30/60-second mark embeds its book and fee inputs in the existing `outcome_fee_audit` JSON. Exit intent evidence is written in the same transaction as the sale or settlement. Depth-based exits retain book, market-check and fee references. Settlements retain the market reference. Lifecycle IDs associate exits with their opening inventory. |

`GET /paper/diagnostics/intents/{intent_id}` returns the submission, individual poll evidence, acceptance/rejection, ledger result, observed markouts and lifecycle exits. The runtime can identify a missing reference immediately. Resolving a queued reference requires the paired recorder. Historical rows keep their original data. B3B1 adds JSON fields and an index for new recorder files and requires no backfill.

**Reading a decision.** A displayed size and a market status are observed data. The implied ask, displayed capacity and queue estimate are derived. Interpreting a displayed reduction as executable volume is a model assumption. The sampled probability comparison is a simulation outcome. A persisted fill is a paper accounting action. These distinctions apply equally to positive and negative markouts.

## Offline verification

Use the completed SQLite backup directory. The archive reader checks hashes, SQLite integrity and sidecars and opens both files immutable and query-only. The audit resolves composite keys, validates payload kind, ticker, attribution and temporal order, checks sequence continuity against clean session seals, and reconciles candidates to ledger rows. Missing references, unresolved keys, duplicate or inconsistent references, future inputs, missing final results and legacy polls are counted separately. Summary counts are per poll, with separate outcomes and exits. Only the first five example fills and five failing polls are printed. Archive classification and counters carry the research result. A successful CLI exit means the report was produced.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$archive = 'C:\Users\jacks\kalshi-paper-runs\b3a-smoke-2026-11-21\backup-6f3d695bff8a4d80b2830803f85af15e'
.\.venv\Scripts\python.exe -m kalshi_edge_engine.replay evidence --archive $archive
```

| Verified input | Recorder rows | Entry polls | Complete / incomplete polls | Audit result |
|---|---:|---:|---:|---|
| Mock HTTP, strict path policy | 15 | 3 | 3 / 0 | One modeled entry, one mark and one exit linked |
| Same scenario with the final poll's book reference removed | 15 | 3 | 2 / 1 | One missing reference, even though the raw row exists |
| Sealed B3A smoke backup | 7,081 | 342 | 0 / 342 | Nine legacy entry intents, zero explicit poll links, zero receipt sequence breaks |

In the mock lifecycle, signal receipts 1–4 lead to submission receipts 5–6. The third poll consumes book 11, compares **0.125 < 0.25** and uses market validation 12 before booking. Raw book 11 hashes to `e594ee5cbb80ae59a3136e7845f2d000522643fd02215d4dc6942814a0ae963f`. The first two polls consumed no draw. Book 13 supports the mark. Market 14 and book 15 support the modeled exit. The test uses a one-value draw source and asserts exactly one call.

## Overnight run on Windows

1. In PowerShell window 1, select a **new** directory and prepare the paired databases. The launcher supplies temporary environment variables and uses the repository's existing local credentials and policy settings.

   ```powershell
   Set-Location 'C:\Users\jacks\kalshi-edge-engine'
   $run = Join-Path $env:USERPROFILE 'kalshi-paper-runs\b3b1-run01'
   if (Test-Path -LiteralPath $run) { throw 'Choose a fresh run directory in both windows' }
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Preflight -RunDirectory $run
   ```

   Require PASS, the B3B1 ID, both expected paths, a healthy drained probe and zero intents, fills and retained receipts. Preflight verifies that the runtime Python files match the checked-out commit and registered source fingerprint. It also checks B3A checkpoint ancestry, policy, storage and the worker lock. The saved manifest adds the exact revision and full source digest. Restarting a clean session requires an exact manifest match. The launcher budgets 4 GiB for the append-only recorder and reserves 2 GiB of free disk, with 16 MiB of working headroom when writing. No age pruning or capacity eviction occurs.

2. Start the single-worker backend in window 1 and wait for startup to finish.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Backend -RunDirectory $run
   ```

3. In window 2, enable the scanner and paper simulator. Enable checks the live cohort and paths, then waits for a completed scan with durable receipts.

   ```powershell
   Set-Location 'C:\Users\jacks\kalshi-edge-engine'
   $run = Join-Path $env:USERPROFILE 'kalshi-paper-runs\b3b1-run01'
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Enable -RunDirectory $run
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Status -RunDirectory $run
   ```

   Before leaving, repeat Status. Require an advancing scan timestamp and receipt counts, enabled paper, healthy append-only storage and zero drops, errors, pruning and diagnostic gaps. An empty market interval can produce zero fills. Keep the computer awake. For the optional dashboard run `npm.cmd run dev` in a third window from `C:\Users\jacks\kalshi-edge-engine\web`.

4. In the morning repeat Status. Press **Ctrl+C once in window 1** and wait for the prompt. In window 2, back up the closed databases and verify the printed pair.

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File .\overnight.ps1 Backup -RunDirectory $run
   if ($LASTEXITCODE) { throw 'Backup failed' }
   $archive = (Get-ChildItem -LiteralPath $run -Directory -Filter 'backup-*' | Where-Object { $_.Extension -ne '.partial' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
   .\.venv\Scripts\python.exe -m kalshi_edge_engine.replay evidence --archive $archive
   ```

   A capture or decision-journal write failure trips the strict interlock. Preserve incomplete runs for diagnosis. A clean session with no decisions can still provide observation data. A session with unresolved evidence requires investigation before using its decisions for research.

## Documentation and compatibility audit

- The root README previously described historical queue-cleared P&L as encouraging execution evidence and foregrounded the development reload server. It now directs research runs to the isolated launcher and describes the Toronto correction. The historical entries remain in the research log with a dated reconciliation.
- The dashboard's API base and launch command remain valid. Receipt inspection uses the backend diagnostics endpoint. The historical B1, B2, B3A and overnight instructions identify their applicable versions. Use checkout `d6873bf` for the original B3A launcher and manifests.
- The example environment contains sample strategy values and enabled NBA heuristic overrides. The balanced and moved-through presets also enable a heuristic. The queue-cleared preset disables both for NBA. A preset alone does not establish global policy, isolation or durable capture. The strict launcher checks the effective configuration. The agent guide now lists the evidence and replay modules.
- Keep `replay/b1_orderbook.py` and the default B2 replay interpretation for historical reproducibility. Keep the API-to-runtime bridge used by existing callers and tests. The B2 seed-42 report reproduced SHA-256 `603af7df6e286b3d617d9518c9aad4d718a369030cfe7eb7cb8deefed9d2ba2b`.
- Future cleanup candidates include duplicate policy presentation in API responses and compatibility aliases around runtime state. Their current cost is maintenance and the risk of future drift. The confirmed historical correctness issue was inconsistent depth parsing, addressed by B3A. The production simulator uses canonical depth.

## Research limits and next milestone

The audit verifies explicit recorded dependencies and decision consistency. It does not independently rerun every strategy calculation. Receipt time measures local arrival; the exchange may change between requests. Depth changes can reflect cancellations, and historical B1/B3A draws and receipt identities are unrecoverable. This milestone journals admitted paper intents, outcomes and booked exits. Signals rejected before intent creation have no full lifecycle journal. Portfolio-level risk snapshots do not identify every contributing market receipt.

The next research milestone is to compare decision-linked queue estimates against sequenced public depth updates and trades across independent sessions, including missed and adverse outcomes. Keep execution policy fixed during that comparison.
