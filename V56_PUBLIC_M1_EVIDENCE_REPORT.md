# V5.6 Canonical Provider Historical Evidence — Public M1 Seal

Seal date: 2026-08-27 UTC
Branch: `xauusd-v5.6-provider-canonical`
Scope: historical research only; no real-order or live-readiness authority.

## Executive verdict

The provider-language interpretation was frozen before the new P&L was observed. Under that frozen canonical interpretation:

1. **Raw S$1,000 provider-following proxy fails survival.** The causal public-M1 account replay reaches the 80% margin-warning proxy on 2024-06-14 13:40 UTC and the 50% stopout proxy on 2024-06-17 06:39 UTC. Conservative forced liquidation leaves approximately **S$2.7644** proxy cash.
2. **The preregistered 10% reserved-stop-risk lane survives, but remains materially loss-making.** It finishes the available public-M1 period at approximately **S$557.7943**, a loss of **S$442.2057 / 44.22%** from the S$1,000 start.
3. The risk lane survives primarily by rejecting most candidate exposure: **1,041 / 10,486 tickets accepted at arm (~9.93%)** and **9,445 / 10,486 rejected (~90.07%)**.
4. These results do **not** constitute Blueberry historical certification. The price source for these runs is public Dukascopy-derived M1 Bid/Ask, not the uploaded Blueberry tick archive.
5. The strongest supported conclusion is therefore: **current historical evidence does not support following this Telegram provider as a profitable raw strategy; the frozen safety overlay prevents the observed proxy ruin but still does not establish positive expectancy.**

## Frozen canonical interpretation

The V5.6 interpretation was frozen before scoring and includes:

- normal two-price zone -> **two boundary 0.01 tickets only**;
- **no synthetic midpoint**;
- one published TP -> one final TP plus provider management; no fabricated TP2/TP3;
- explicit multi-TP cards preserve only explicitly published targets;
- market-now -> one 0.01 ticket;
- small-lot/small-volume -> Blueberry 0.01 minimum;
- stateful re-entry / Round 2 handling;
- exact-entry, setup, round, side-wide and global scope distinctions;
- ambiguous scope -> fail closed;
- `Close all OR move SL` primary branch frozen to `CLOSE_ALL` before P&L;
- 0.01 partial projection: one open -> full close; two opens -> close one worst entry; three opens -> close two;
- `real_orders=false`, `live_ready=false`.

Regression/canonical certification before scoring: original semantic 20/20 PASS, legacy execution 9/9 PASS, canonical provider 15/15 PASS, additional hardening/profile tests 5/5 PASS.

## Frozen input reconstruction

The preserved compact signal archive had two mutually exclusive `signals_04` representations. The old glob would concatenate both and corrupt the archive. V5.6 tested both alternatives and accepted only the CRC-valid structurally clean reconstruction.

- Valid signals compressed SHA-256: `f472ccad02aaef9c3a18f511263b35e3e0d8d1b0c9dc43a637f535df4ea705ad`
- Signals: **5,275**
- BUY: **3,199**
- SELL: **2,076**
- Compact management compressed SHA-256: `0b05c33cf080c6ee532658f408a069a5bced789e8a61403c8f88410835404090`
- Decoded compact management rows: **3,688**; **3,659** map inside the available public-M1 period.

The compact management source is intentionally acknowledged as incomplete relative to the full provider-language audit: it omits richer unlinked/global management, explicit re-entry/round nuance, and some named-entry semantics.

## Public M1 market data

Source class: public Dukascopy-derived separate Bid/Ask M1 files via the preserved Market-Data-Lab source.

- merged Bid/Ask bars: **1,721,213**
- first: `2023-05-01T00:00:00Z`
- last: `2026-08-20T23:58:00Z`
- 80 monthly Bid/Ask files downloaded
- 32 provider signals lie after the public-M1 endpoint
- M1 intrabar order is unknowable, so ambiguous fill/SL/TP bars are resolved adversely/fail-closed.

## Primary raw-provider causal survival result

Version: `V5.6_PUBLIC_M1_CAUSAL_ACCOUNT_SURVIVAL_V2`
Classification: `PUBLIC_M1_CAUSAL_SURVIVAL_SMOKE_NOT_BLUEBERRY_CERTIFICATION`

Independent optimized implementation commit: `e306a27a7fff8c8a0995c759ad4f230babc60afd`
Workflow run: `33112760431`
Artifact ID: `9663217865`
Artifact ZIP SHA-256: `9299819050efd816302595afff33f430605d97f8cb935050fbc36bbc43074e5d`

Account model:

- starting balance: S$1,000
- 0.01 per accepted canonical provider ticket
- 1:500 gross CFD margin proxy
- projected free-margin check before fills
- 80% margin warning / 50% stopout proxy
- provisional current commission proxy: S$0.0444444444 per 0.01 side
- no invented pending-order TTL
- swap not applied
- constant SGD/USD proxy, not historical broker intraday conversion
- forced liquidation at adverse M1 executable side once 50% proxy is reached; replay terminates immediately afterward.

Observed result:

- first 80% warning: **2024-06-14 13:40 UTC**
- 50% stopout: **2024-06-17 06:39 UTC**
- equity immediately before forced liquidation: **S$2.8088889 proxy**
- cash after forced liquidation: **S$2.7644444 proxy**
- account survived: **NO**
- max drawdown proxy: **S$1,018.8533**
- max open tickets: **9**
- tickets armed before termination: **2,158**
- filled/closed tickets: **1,510 / 1,510**
- positive closed tickets: **517**
- negative closed tickets: **916**
- ambiguous fail-closed: **49**
- insufficient-free-margin rejections: **1**

The initial causal implementation and the optimized event-index implementation reproduced the stopout timestamp and post-liquidation balance exactly, giving an internal implementation-consistency check.

## Secondary frozen 10% reserved-risk result

Version: `V5.6_PUBLIC_M1_RISK10_SURVIVAL_V1`
Classification: `FROZEN_SECONDARY_RISK_LANE_PUBLIC_M1_NOT_BLUEBERRY_CERTIFICATION`

Workflow head: `72678a36982bd193d38769ddb418953193b0d648`
Workflow run: `33112923269`
Artifact ID: `9663288257`
Artifact ZIP SHA-256: `4df9574c42bd14dc20033d1e760ce0df20d81e1a052909678f839f030a0cd589`

Frozen risk rule:

- maximum reserved stop risk = **10% of current equity** at admission;
- reserved risk includes remaining stop downside of OPEN + PENDING accepted canonical tickets;
- rejection happens at arm time before outcome data are read;
- the provider interpretation itself is unchanged.

Observed result:

- final public-M1 time: `2026-08-20T23:58:00Z`
- ending cash: **S$557.7942857 proxy**
- absolute loss: **S$442.2057143**
- percentage loss: **44.22%**
- min equity: **S$557.7942857 proxy**
- max drawdown: **S$463.8234921 proxy**
- margin warning seen: **NO**
- stopout seen: **NO**
- survived available public-M1 period: **YES**
- candidate tickets considered: **10,486**
- accepted at arm: **1,041 (~9.93%)**
- risk-rejected at arm: **9,445 (~90.07%)**
- filled tickets: **747**
- positive closed tickets: **266**
- negative closed tickets: **432**
- ambiguous fail-closed: **27**
- maximum open tickets: **8**
- maximum observed reserved-stop-risk: **S$78.3048**

`max_observed_risk_cap_utilization` can exceed 1 after later equity/market movement; the admission invariant is the arm-time test, not a claim that subsequent marked risk can never exceed 10%.

## Blueberry evidence boundary

The real Blueberry forensic export previously established:

- symbol: `XAUUSD.i`
- SGD account
- leverage 1:500
- min/step 0.01
- Blueberry tick coverage: `2024-12-17T06:39:39.055Z` through `2026-08-25T23:58:59.832Z`
- 435 tick files in the manifest.

This creates an unavoidable historical boundary: the public-M1 raw-provider proxy stopout occurs in June 2024, **six months before the available Blueberry tick history begins**. Therefore the Blueberry archive can never directly certify or refute that June-2024 event. It can only certify a separate `2024-12-17 onward` subperiod.

Three compact Blueberry result ZIPs are preserved in the conversation, but their internal bytes are not text-indexed and the current local runtime times out when opening even those small ZIPs because the conversation also contains the six ~2.98 GB raw tick parts. No claim is made that a reusable Blueberry event ledger has been extracted.

## Remaining certification work

The next broker-specific test, once the compact result bundle or raw Blueberry tick archive is executable in a healthy runtime, is a **coverage-only V5.6 Blueberry subperiod replay from 2024-12-17 onward**. It must preserve the same canonical interpretation and report both the raw-provider and preregistered risk-constrained lanes without choosing between them after seeing outcomes.

Historical Blueberry commission, swap, exact intraday SGD conversion, and exact historical margin/P&L calculation remain explicit uncertainty gates.

## Evidence state

- `PROVIDER_LANGUAGE_CANONICAL`: **PASS**
- `CANONICAL_SOFTWARE_REGRESSION`: **PASS**
- `PUBLIC_M1_RAW_PROVIDER_SURVIVAL`: **FAIL**
- `PUBLIC_M1_RISK10_SURVIVAL`: **PASS_SURVIVAL_BUT_LOSS_MAKING**
- `PUBLIC_M1_PROFITABILITY`: **NOT_ESTABLISHED / NEGATIVE IN BOTH RELEVANT LANES**
- `BLUEBERRY_COVERAGE_ONLY_V56_REPLAY`: **BLOCKED_RUNTIME_DATA_ACCESS**
- `BLUEBERRY_FULL_2023_2026_CERTIFICATION`: **IMPOSSIBLE_WITH_CURRENT_BROKER_HISTORY_START**
- `LIVE_READY`: **NO**
- `REAL_ORDERS`: **DISABLED**
