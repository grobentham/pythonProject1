# V5.6 Blueberry-Date-Aligned Public M1 Proxy — Sealed Result

Protocol: `V56_BLUEBERRY_COVERAGE_ALIGNMENT_PROTOCOL.md`
Boundary frozen before P&L: `2024-12-17T06:39:39.055Z`
Classification: `BLUEBERRY_DATE_ALIGNED_PUBLIC_M1_PROXY_NOT_BLUEBERRY_TICK_CERTIFICATION`

Workflow head: `20a8cea186aac56b05f7bd08e96a58de1ff5d991`
Workflow run: `33113405571`
Artifact ID: `9663482512`
Artifact ZIP SHA-256: `804f595c6805888d0b470320e7ae77fdc06ea756b5fdcf02bf952cf66aad160e`
Combined-summary SHA-256: `f5aadb46e88a06ace20be4a3443e4849b64fde2cc030d62b5e79da9f3628d7da`

## Frozen overlap population

- full canonical compact signals: 5,275
- signals at/after exact first Blueberry-tick boundary: **3,316**
- first included signal: `2024-12-17T07:05:12Z`
- last included signal: `2026-08-25T14:14:45Z`
- compact management rows linked to included setups and at/after boundary: 2,342
- management rows that also map inside the available public-M1 endpoint: **2,313**
- public M1 price endpoint: `2026-08-20T23:58:00Z`
- 32 aligned signals occur after that public-M1 endpoint.

Each lane resets to **S$1,000** at the boundary. No pre-boundary P&L, positions, pending orders, or losses are carried forward.

## Lane A — raw canonical provider

Mechanics are unchanged from `V5.6_PUBLIC_M1_CAUSAL_ACCOUNT_SURVIVAL_V2`; only the historical population is boundary-filtered.

Result:

- starting cash: **S$1,000**
- ending cash proxy: **S$6.8165079**
- loss: **S$993.1834921 / 99.32%**
- minimum equity proxy: **S$6.8165079**
- max drawdown proxy: **S$1,042.1473016**
- first 80% margin warning: `2025-05-09T13:18:00Z`
- 50% stopout observed: **NO**
- technical account-survival flag: **YES**
- max open tickets: **6**
- canonical tickets armed: **6,568**
- filled tickets: **863**
- insufficient-free-margin fill rejections: **3,584**
- positive closed tickets: **340**
- negative closed tickets: **514**
- ambiguous fail-closed tickets: **55**

Interpretation: this is **not meaningful economic survival**. The account avoids a formal 50% forced-liquidation event in this overlap-period M1 proxy, but loses about 99.32% of starting capital and becomes unable to finance thousands of otherwise marketable provider entries. A `survived=true` flag must not be represented as a successful outcome.

## Lane B — frozen 10% reserved-stop-risk overlay

Mechanics and 10% threshold are unchanged from `V5.6_PUBLIC_M1_RISK10_SURVIVAL_V1`.

Result:

- starting cash: **S$1,000**
- ending cash proxy: **S$806.8177778**
- loss: **S$193.1822222 / 19.32%**
- minimum equity proxy: **S$787.8920635**
- max drawdown proxy: **S$261.0717460**
- 80% margin warning: **NO**
- 50% stopout: **NO**
- survived available public-M1 overlap period: **YES**
- max open tickets: **5**
- candidate canonical tickets: **6,568**
- accepted at arm: **369 (~5.62%)**
- rejected by frozen 10% risk gate: **6,199 (~94.38%)**
- filled tickets: **219**
- positive closed tickets: **98**
- negative closed tickets: **121**
- ambiguous fail-closed tickets: **6**

Interpretation: the preregistered risk overlay materially improves survival and reduces the loss, but it still does **not** create positive historical expectancy in this date-aligned proxy. It does so by refusing the overwhelming majority of candidate ticket exposure.

## What this adds

The full-history public-M1 test and the Blueberry-date-aligned public-M1 test now agree directionally:

- raw provider-following is economically destructive;
- a strict risk gate can prevent catastrophic broker-margin failure;
- the tested risk gate still loses capital rather than demonstrating edge.

This makes the result less dependent on the early 2023–mid-2024 period. However, it still does not substitute for the exact Blueberry tick replay.

## Certification boundary

This run uses public M1 Bid/Ask, not Blueberry historical ticks. It cannot certify broker-exact fills, spread path, gap sequence, commission conversion, swap, or historical margin calculation. The exact Blueberry overlap-period V5.6 replay remains `BLOCKED_RUNTIME_DATA_ACCESS` until the compact result ZIPs or six raw tick parts can be executed in a healthy runtime.

- `REAL_ORDERS`: **DISABLED**
- `LIVE_READY`: **NO**
- `PROFITABILITY`: **NOT ESTABLISHED; NEGATIVE IN BOTH DATE-ALIGNED TEST LANES**
