# V5.7 — Causal Signal-Selection Research Protocol

Status: PREREGISTERED BEFORE V5.7 FEATURE/SELECTOR OUTCOMES
Parent: `xauusd-v5.6-provider-canonical` @ `088613d84328afe8c1ff7c96d3e3b51e92a96f0c`
Purpose: determine whether a causally selectable subset of Telegram XAUUSD signals has better historical expectancy than raw provider-following.

## Hard boundary

This is retrospective research, not live-readiness or profitability certification. Real orders remain disabled. The exact Blueberry tick replay remains a separate broker-certification gate.

No post-signal management message, future bar, provider-reported result, TP-hit announcement, realized trade outcome, future signal, or later edit may be used as a selector feature.

Provider management is allowed only in the outcome simulator because it determines what the provider instructed after entry.

## Frozen data sources

1. Canonical compact provider signals reconstructed under V5.6:
   - compressed SHA-256 `f472ccad02aaef9c3a18f511263b35e3e0d8d1b0c9dc43a637f535df4ea705ad`
   - 5,275 signals
2. Compact reply-linked management:
   - compressed SHA-256 `0b05c33cf080c6ee532658f408a069a5bced789e8a61403c8f88410835404090`
   - 3,688 decoded rows
3. Public separate Bid/Ask M1 market data used by the sealed V5.6 proxy, May-2023 through Aug-2026.

## Frozen canonical trade interpretation

Inherited unchanged from V5.6:
- normal two-price zone -> two boundary 0.01 tickets; no synthetic midpoint;
- one-price zone -> one ticket;
- single published TP -> single final TP plus causal provider management;
- no invented pending-order TTL;
- compact reply-linked close/cancel/partial/BE management only in the public-M1 research lane;
- M1 intrabar ambiguity resolved adversely/fail-closed;
- current commission/FX proxy inherited from V5.6;
- no martingale, lot escalation, averaging beyond provider-authorized entries, or hindsight target reassignment.

## Research outcome unit

Primary discovery outcome is **independent setup net P&L**: every provider setup is simulated independently with canonical entries and management, so account insolvency in one historical path does not truncate later observations. Net P&L includes the inherited spread-side execution and provisional commission proxy.

Account-level tests are performed only after a selector is frozen.

## Ex-ante feature families

Only features known at or immediately before the signal timestamp are allowed.

### Provider-card geometry
- BUY/SELL side
- zone width
- shallow-entry stop distance
- deep-entry stop distance
- shallow-entry target distance
- deep-entry target distance
- shallow R:R
- deep R:R
- mean R:R
- target/zone-width ratio
- stop/zone-width ratio

### Calendar/session
- UTC hour
- weekday
- fixed session bucket: Asia 00:00–06:59 UTC; London 07:00–12:59; New York 13:00–20:59; Late 21:00–23:59

### Signal cadence known at signal time
- minutes since prior provider signal
- count of prior signals in trailing 30 minutes
- count of prior signals in trailing 60 minutes
- count of same-side prior signals in trailing 60 minutes
- previous-signal side / same-side streak length

### Market context known at signal time
Using only M1 bars ending before the signal timestamp:
- executable Bid/Ask spread at prior completed M1 close
- prior completed M1 midpoint
- distance from prior midpoint to nearest zone boundary
- distance from prior midpoint to farthest zone boundary
- 15-minute midpoint return
- 60-minute midpoint return
- trailing 30-minute high-low range
- trailing 60-minute high-low range
- zone width / 60-minute range
- stop distance / 60-minute range
- target distance / 60-minute range

No news labels, later outcomes, discretionary chart pattern labels, or future-derived volatility are permitted in V5.7.

## Time splits frozen before scoring

Primary split:
- discovery/train: first available signal through `2024-12-31T23:59:59Z`
- validation/model-selection: calendar 2025
- final retrospective holdout: `2026-01-01T00:00:00Z` through the public-M1 endpoint (`2026-08-20T23:58:00Z`)

The 2026 period is a **retrospective holdout**, not pristine untouched evidence, because aggregate project-level 2026 outcomes have been viewed previously. It remains valid as a no-refit test for V5.7 selectors.

Secondary robustness: expanding-quarter walk-forward tests across available quarters, with each quarter scored only from a model fit on strictly earlier signals.

Blueberry-date-aligned robustness: report selector behavior separately for signals at/after `2024-12-17T06:39:39.055Z`; this remains public-M1 proxy evidence, not Blueberry tick certification.

## Frozen candidate selectors

### A. Transparent univariate screens
For each continuous feature, candidate thresholds are discovery-set quantiles only: 10%, 20%, 30%, 40%, 50%, 60%, 70%, 80%, 90%, tested as both `<=` and `>=` where meaningful. Categorical screens use individual side/session/weekday categories.

A univariate screen is promotable to validation only if discovery has >=150 setups and >=25 nonzero-result setups.

### B. Transparent two-condition screens
Only pairs formed from the best discovery univariate screen in distinct feature families are considered, capped at 50 candidate pairs by discovery ranking. Minimum discovery sample: 150 setups.

### C. Regularized linear expected-P&L model
- one-hot categorical features;
- median-imputed continuous features;
- standardized continuous features;
- Ridge regression alpha grid frozen to `[0.1, 1, 10, 100]`;
- selector keeps the top predicted fraction from frozen grid `[10%, 20%, 30%, 40%, 50%]`.

### D. Shallow nonlinear expected-P&L model
HistGradientBoostingRegressor only, frozen complexity grid:
- `max_leaf_nodes`: `[7, 15]`
- `learning_rate`: `[0.03, 0.07]`
- `max_iter`: `[100, 200]`
- `l2_regularization`: `[1, 10]`
- top predicted fraction `[10%, 20%, 30%, 40%, 50%]`

No additional model family may be added after V5.7 outcomes are observed.

## Model selection objective

Discovery ranks candidates by net P&L per setup subject to minimum sample. Validation chooses one candidate from each family using this lexicographic gate:
1. validation net P&L > 0;
2. validation profit factor > 1.05;
3. validation >= 100 selected setups (unless the whole validation period contains fewer eligible setups);
4. lower max cumulative setup-P&L drawdown;
5. higher validation net P&L.

If no candidate in a family passes all gates, that family is rejected and cannot be rescued using 2026 outcomes.

At most **one** final selector may be promoted from validation to 2026 holdout. Family choice is made solely from 2025 validation using the same lexicographic gate and may not be changed after 2026 is opened.

## Holdout success definition

A V5.7 selector is only considered a promising retrospective candidate if the frozen selector, with no refit, has on 2026 holdout:
- positive net P&L;
- profit factor >= 1.10;
- >= 75 selected setups;
- max cumulative setup-P&L drawdown less than the unfiltered holdout baseline;
- positive result in at least 4 distinct holdout months;
- not more than 50% of total positive P&L coming from the single best setup.

Failure of any criterion means `NO_RETROSPECTIVE_SELECTOR_PROMOTION`.

## Account-level verification

Only if a selector passes the holdout gate:
1. run S$1,000 selected-signal account replay with canonical 0.01 sizing and broker-margin proxy;
2. run the same selected stream with the already-frozen 10% reserved-stop-risk overlay;
3. report both; no choosing the better one after outcomes.

If no selector passes holdout, account-level optimization stops. No risk increase or threshold rescue is permitted.

## Multiple-testing / interpretation

All V5.7 findings are hypothesis-generation evidence. The holdout is retrospective and public-M1-based. Positive results require future prospective shadow evidence and exact Blueberry execution certification before any real-money claim.

`REAL_ORDERS=false`
`LIVE_READY=false`
