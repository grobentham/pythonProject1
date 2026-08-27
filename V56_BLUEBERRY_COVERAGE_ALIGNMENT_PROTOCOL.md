# V5.6 Blueberry-Coverage-Aligned Proxy Protocol

Preregistered before observing the coverage-aligned P&L.

## Purpose

The uploaded Blueberry tick history begins at `2024-12-17T06:39:39.055Z`, while the full public-M1 raw-provider V5.6 proxy reaches its first stopout in June 2024. Because Blueberry has no earlier broker ticks, the June-2024 event can never be directly broker-certified from the available archive.

This experiment asks a narrower question before the Blueberry bytes become executable again:

> If a fresh S$1,000 account begins exactly at the first available Blueberry-history boundary, does the frozen V5.6 provider interpretation survive and/or make money over the date-overlap period when evaluated on the existing public Bid/Ask M1 proxy?

It is **not** Blueberry certification. It is a preregistered date-aligned comparator for the future exact Blueberry subperiod replay.

## Frozen boundary

- Exact Blueberry first tick: `2024-12-17T06:39:39.055Z`
- Include provider signal cards with Telegram UTC timestamp >= this boundary.
- Include compact management only when its `root_uid` belongs to an included signal and the management timestamp is >= the boundary.
- Market data remains the already-used public separate Bid/Ask M1 source.
- The first executable M1 observation is the first available M1 bar at or after each message timestamp.
- Account starts fresh at **S$1,000**; no P&L or open/pending state from before the boundary is carried in.

## Lane A — raw canonical provider

Use the already-frozen `V5.6_PUBLIC_M1_CAUSAL_ACCOUNT_SURVIVAL_V2` mechanics unchanged:

- two boundary 0.01 tickets per ordinary two-price zone;
- no synthetic midpoint;
- compact causal management;
- single published TP is final TP;
- no research stop-risk admission filter;
- margin/free-margin proxy and 80/50 gates unchanged;
- stop at first 50% proxy forced liquidation;
- no invented TTL;
- M1 ambiguity fails adversely;
- current commission proxy unchanged;
- swap omitted.

## Lane B — frozen 10% reserved-risk overlay

Use the already-frozen `V5.6_PUBLIC_M1_RISK10_SURVIVAL_V1` rules unchanged, including the 10% arm-time reserved-stop-risk admission gate.

## No-hindsight rule

Both lanes must be run and reported. A favorable lane may not replace an unfavorable lane. No parameter, boundary, entry rule, target rule, management rule, commission proxy, ambiguity rule, or risk threshold may be modified after observing these results.

## Classification

All outputs must be labeled:

`BLUEBERRY_DATE_ALIGNED_PUBLIC_M1_PROXY_NOT_BLUEBERRY_TICK_CERTIFICATION`

Real orders remain disabled; live readiness remains false.
