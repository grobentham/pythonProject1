# V5.6 S$1,000 Historical Test Protocol

Frozen before any new V5.6 historical P&L is observed.

## Primary — CANONICAL_PROVIDER_SGD1000

Purpose: answer the user's question, **how did this provider actually perform when followed canonically on an S$1,000 SGD Blueberry account?**

- Starting balance: S$1,000.
- Ticket size: 0.01 per canonical provider-authorized entry.
- Normal two-price zone: two boundary tickets, no synthetic midpoint.
- Explicit extra entry/re-entry/round: separate provider-authorized 0.01 state.
- Market-now: one 0.01 market ticket.
- Provider management: canonical V5.6 grammar.
- Broker Bid/Ask, spread, margin call and stop-out: enforced.
- Research stop-risk filter: **disabled** in this primary lane so it cannot change the provider opportunity stream.
- No DD shutdown or consecutive-loss shutdown in this primary lane; those are risk-overlay questions, not provider-performance measurement.
- Malformed or semantically unresolved mandatory messages remain fail-closed.
- Real orders disabled.

## Secondary — CANONICAL_SURVIVAL_SGD1000

Purpose: show what happens after applying the pre-existing research safety overlay to the same canonical provider stream.

- Same S$1,000 start and 0.01 minimum lot.
- Same canonical provider interpretation.
- Maximum reserved stop risk: 10% of current equity.
- Broker margin rules still enforced.
- This lane may reject signals/layers; all rejections must be counted and reported.
- This result can never replace the primary provider result because it happens to look better.

## Required comparison

The final report must show both lanes side-by-side and explicitly attribute differences to rejected/accepted tickets. It must not compare the new V5.6 result to old V2/V5.1 numbers without labeling those older runs as non-canonical or invalidated.

## Required integrity

- Blueberry historical coverage only; 2023 and most of 2024 are not silently treated as tested.
- No use of provider-reported pip summaries as outcome truth.
- No after-the-fact choice between `close all` versus `move SL` alternatives.
- No synthetic third ticket for a normal two-price zone.
- No fabricated TP2/TP3 on a single-TP card.
- No future Telegram message can alter an earlier market event.
- Historical commission/swap/FX/margin uncertainty must be disclosed, not guessed away.

`real_orders = false`

`live_ready = false`
