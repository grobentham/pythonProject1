# V5.8 — Signal Intelligence Gate

Status: **CODE / INFORMATION-CONTRACT PHASE — NON-AUTHORIZING**

V5.8 changes the role of the Telegram provider. The provider supplies a candidate XAUUSD idea; the software independently decides whether there is enough causal information and evidence to follow any part of it.

This checkpoint does **not** change V5.6 provider semantics, does **not** reinterpret old outcomes, and does **not** authorize live orders.

## 1. Frozen decision states

The intelligence gate may return only:

- `TAKE` — all mandatory context is fresh, account risk passes, and a separately frozen/certified edge model has sufficient positive evidence.
- `TAKE_REDUCED` — advisory only; evidence is positive but below the full `TAKE` support gate. Downstream sizing must still respect broker minimum lot and risk controls.
- `WAIT` — temporary market/event/execution conditions make immediate entry undesirable (for example spread shock, imminent high-impact event, active breaking-news risk).
- `REJECT` — proposal is invalid, account risk fails, or complete certified evidence says the setup lacks positive edge.
- `INSUFFICIENT_DATA` — required context is missing, stale, invalid, from the future, or the edge model is absent/uncertified.

No default-to-TAKE behavior is allowed.

## 2. Information contract

### Provider proposal

Required from the already-frozen provider parser:

- signal UID and timestamp
- BUY/SELL
- zone low/high
- provider SL
- provider TP
- round and layer identity

Provider direction is a feature/input. It is not treated as market truth.

### Broker / XAUUSD market state

Required at decision time:

- broker Bid and Ask
- current spread
- rolling 30-minute median spread
- causal 15-minute and 60-minute XAUUSD returns
- causal 30-minute and 60-minute range
- distance from current market to near/far edge of provider zone

Freshness default: <= 90 seconds.

### Cross-market state

Required:

- DXY 15-minute and 60-minute return
- U.S. 10-year yield change in basis points over 15 and 60 minutes

Freshness default: <= 180 seconds.

These fields must be timestamped observations. A guessed, retrospectively backfilled, or future observation cannot satisfy the gate.

### Macro / news state

Required:

- whether high-impact event state is known
- minutes to next known high-impact event
- minutes since last high-impact event
- breaking-news risk flag

The initial execution policy waits inside 15 minutes before a high-impact event and for 5 minutes after one. Active breaking-news risk also produces `WAIT`.

This is an execution-safety gate, not a claim that news predicts direction.

### Account / execution state

Required:

- balance and equity
- free margin
- projected reserved stop risk after the candidate layer
- projected free-margin percentage after the candidate layer
- drawdown from high-water mark
- consecutive-loss state
- result of the frozen account risk gate

A failed risk gate always overrides a strong model and returns `REJECT`.

### Frozen edge evidence

A future `TAKE` requires a separately frozen/certified selector with:

- version/hash identity
- causal score timestamp
- estimated win probability
- estimated after-cost SGD EV
- number of comparable historical analogues
- analogue profit factor
- analogue mean after-cost SGD result

V5.8 does not train this model. The V5.7 research result is **not automatically certified** merely because its CI run succeeded.

## 3. Initial evidence thresholds

These thresholds are frozen for the V5.8 gate code and are not profitability claims:

- full `TAKE`: >= 75 analogues, analogue PF >= 1.10, EV > S$0, estimated win probability >= 0.50
- `TAKE_REDUCED`: >= 40 analogues, analogue PF >= 1.00, EV > S$0, estimated win probability >= 0.50
- otherwise complete negative evidence -> `REJECT`

A later calibrated model may justify a revised checkpoint, but V5.8 must not be mutated after outcomes are inspected to rescue performance.

## 4. Freshness and causality rules

- Every feature has `observed_at_ms` and a source label.
- Observation timestamp must be <= decision timestamp.
- Required data older than its freshness limit produces `INSUFFICIENT_DATA`.
- Unknown / non-observed / guessed quality cannot satisfy a required field.
- A model score from the future or older than five minutes cannot authorize a trade.
- Market features must come from bars completed before the decision boundary or current broker quotes available at the decision boundary.

## 5. Execution-quality rules

Initial spread gates:

- absolute XAUUSD spread > $1.00 -> `WAIT`
- spread > 3x rolling 30-minute median -> `WAIT`

These are temporary abstention rules, not signal optimizers.

## 6. Hard separation of responsibilities

Pipeline:

`Telegram provider -> V5.6 canonical parser -> V5.8 context/evidence snapshot -> V5.8 intelligence gate -> account/risk authorization -> execution engine`

V5.6 remains responsible for understanding provider instructions such as closes, BE, cancellation, re-entry and round state.

V5.8 is responsible only for deciding whether enough causal evidence exists to permit a proposed entry/layer.

## 7. Non-authorizing invariants

V5.8 must never:

- place an MT5 order
- enable real orders
- claim live readiness
- infer missing cross-market/news data
- convert `INSUFFICIENT_DATA` into `TAKE`
- use provider-reported profit as market outcome truth
- refit a selector on the same holdout period being evaluated
- alter provider semantics to improve P&L

## 8. Current development objective

The next data-engineering milestone is to populate the information contract prospectively and persist one immutable snapshot per provider proposal/layer. Once enough snapshots and outcomes exist, a separately preregistered selector can be trained/evaluated and then supplied to this gate as `ModelEvidence`.
