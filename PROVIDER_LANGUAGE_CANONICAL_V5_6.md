# XAUUSD Provider Language Canonical V5.6

Status: **FROZEN BEFORE NEW P&L OBSERVATION**

Purpose: convert the Telegram provider's stateful communication into one deterministic, causal replay interpretation without retrospectively selecting whichever interpretation makes more money.

This is a replay projection of the provider's instructions. It is **not** a claim that every subscriber placed exactly the same tickets, and it does not authorize live trading.

## Full-corpus evidence carried forward

The V4.1 provider-language audit processed the full dated corpus rather than only a hand-picked sample:

- 40,583 dated messages
- 24,666 operational candidates
- 22,369 high-confidence operational messages
- 2,072 medium-confidence operational messages
- 225 ambiguous operational messages
- 186 unresolved mandatory actions, permanently fail-closed
- 6,368 setup contexts
- 17,939 expanded semantic action rows
- 1,415 messages containing multiple actions
- 27 explicit alternative-choice (`OR`) messages

A separate forensic parser recorded 40,562 message objects, 4,500 reply-linked management messages, 4,258 unlinked/global management messages, and 6,284 media-only messages. Small differences in totals arise from parser/date eligibility rules and must not be hidden by pretending the counts are identical.

## Core finding: the provider communicates state, not isolated phrases

Examples in the corpus include:

- `Close entry 1974. Hold entry 1972 +20pips.` followed by `Buy small lot at 1972 again.`
- `Close entry 1981. Hold entry 1983 ... Move stl to entry.`
- `Sell again`, `Buy again. Use small lot`, `Round 2`, `One more`.
- `Wait for the price to come back and buy again` — conditional re-entry, not immediate execution.
- `Wait for new signal` — no re-entry authorization.
- `Close all BUY`, `Close All SELL GOLD`, `Close all signals before news`.
- `CLOSE ALL open orders and cancel limit orders before FOMC`.
- `Move all stoploss to 1983`, `Use stop loss at 1930 for all current signals`.
- `Close all or move stl to ...` — an alternative choice, not two simultaneous commands.

Therefore replay semantics operate on:

`Instrument -> Setup -> Round -> Ticket/Entry`

and require scope resolution before execution.

## Frozen V5.6 rules

### 1. Scope resolution precedence

Mandatory actions are resolved in this order:

1. direct reply target;
2. explicit setup + round;
3. explicit setup;
4. explicit entry price;
5. explicit instrument/side plus exactly one compatible active context;
6. exactly one recent compatible active context.

If more than one plausible mandatory target remains, the instruction **fails closed**. No P&L-aware scope selection is allowed.

### 2. Entry construction

- One explicit entry price -> one 0.01 ticket.
- A normal two-price provider zone -> **two boundary tickets only**.
  - BUY `1974-1972` -> E1 1974, E2 1972.
  - SELL `1981-1983` -> E1 1981, E2 1983.
- No synthetic midpoint or automatic third ticket is created.
- An explicit discrete list is honored exactly, subject only to the already-existing executable-ticket ceiling.
- `BUY/SELL NOW` or a market-entry request -> one market ticket, not every zone layer at market.
- `Buy again`, `Sell again`, `One more`, explicit `Round N`, or an explicit additional price authorizes separate additional state; it is not deduplicated into the parent entry.
- `Wait for price/zone ... again` arms conditional re-entry; it does not execute immediately.
- `Wait for new/next signal` prohibits further re-entry until a new signal.
- `small lot` / `small volume` maps to Blueberry's minimum 0.01 lot. No fictional sub-0.01 size is used.
- `big lot` is not converted into a larger historical size because no deterministic quantity is supplied; size escalation fails closed.
- `DCA` alone does not create unmentioned extra tickets.

### 3. Partial closes and named entries

- `Close entry PRICE` closes the exact matching filled ticket.
- `Hold entry PRICE` preserves the exact matching ticket.
- Generic `close 1/2` with two 0.01 tickets closes the worse/shallow entry:
  - BUY -> higher fill closes first;
  - SELL -> lower fill closes first.
- With only one 0.01 ticket, half-volume is impossible at Blueberry's 0.01 minimum; the frozen small-account projection closes the ticket in full and records that projection explicitly.
- More than two open tickets can exist only because the provider explicitly created additional entries/round state; generic half-close rounds up rather than leaving more exposure than the instruction implies.

### 4. Stop-loss language

- `Move SL to Entry` means each surviving filled ticket's own actual fill price unless the provider supplies a numeric stop.
- `Move SL to better entry` is distinct from own-ticket break-even and uses the explicitly resolved better-entry level.
- Numeric `Move SL to X` / `Stoploss X` is a causal amendment from that message time onward.
- `Move all stoploss to X` / `all current signals` applies to all compatible active XAUUSD state in the resolved scope.
- A later SL message never rewrites earlier historical risk before its timestamp.

### 5. Single-TP versus multi-TP setups

The provider often publishes a single TP and then manages the position dynamically. V5.6 does **not** manufacture TP2/TP3.

**Single explicit TP:**

- Telegram partial-close/BE/SL/close instructions drive the lifecycle.
- The published TP is the final target for whatever exposure remains.
- Reaching that final TP closes remaining filled exposure and cancels remaining pending tickets in that round.

**Explicit multiple TPs:**

- E1 -> TP1.
- E2 -> TP2.
- An explicit E3, if genuinely present, -> TP3.
- At TP1, the worst/E1 ticket closes, unfilled entries in the round are cancelled, and surviving filled tickets move to their own BE unless a contemporaneous provider instruction specifies a different stop.
- Only targets actually supplied by the provider can become mechanical target stages.

### 6. Close versus cancel

- `Cancel` applies to pending entries unless wording explicitly says to flatten positions.
- `Close` applies to filled exposure.
- `Close all BUY` and `Close all SELL` are side scoped.
- `Close all signals` is XAUUSD-account scoped in this replay.
- `Close all open orders and cancel limit orders before news/FOMC` both flattens filled XAUUSD exposure and cancels XAUUSD pending entries.

### 7. Status text is not broker truth

`running +Xpips`, `TP hit`, `SL hit`, daily marketing summaries, screenshots and claimed net pips are status/claims unless the same message contains an executable imperative.

Provider-reported results never replace Blueberry Bid/Ask outcome accounting.

Malformed result text cannot rewrite setup geometry. Known corpus defects include wrong-side TP text, huge typo zones, result-post SL typos and SELL result posts containing `Buy entry` labels.

### 8. Alternative-choice (`OR`) messages

The 27 identified choice messages cannot be evaluated by selecting the better historical branch.

Primary V5.6 policy is frozen to:

`CLOSE_ALL`

when the provider says `Close all OR move SL ...`.

The move-SL alternative may be reported only as a separate sensitivity. It cannot replace the primary result after P&L is known.

### 9. Timing and causality

- Instructions are ordered by provider timestamp/message order after the frozen latency/uncertainty transformation.
- No later message may alter an earlier fill, stop or target.
- Same-time market/instruction ordering must be deterministic and disclosed.
- No future provider text may determine whether an earlier ambiguous action is credited.

### 10. Ambiguity and media

- The 186 unresolved mandatory V4.1 actions remain fail-closed.
- Media-only posts cannot authorize an unseen trade action unless caption/text is actually available to the parser.
- A quoted/replied original signal is context, not a second instruction.

### 11. No profitability tuning

V5.6 may repair semantic fidelity and execution defects only. It may not add indicators, ML filters, news filters, martingale, larger lots, outcome-selected entry depths or retrospective trade selection to improve historical P&L.

## Required historical replay output

The next Blueberry replay must report, at minimum:

- starting/ending SGD balance and net P&L;
- accepted/rejected setups and exact rejection reasons;
- number of one-entry, two-entry and explicit-extra-entry rounds;
- market-now one-ticket count;
- partial closes, named-entry closes, BE moves, better-entry SL moves;
- immediate re-entries, conditional re-entries and prohibited re-entries;
- global/side-scoped close counts;
- OR-choice primary executions;
- single-TP versus explicit multi-TP performance;
- win/loss/BE counts at ticket and setup level;
- profit factor, expectancy and maximum drawdown;
- margin-call/stop-out events;
- commission, swap, FX and historical-margin uncertainty flags;
- fail-closed ambiguous action count;
- exact Blueberry coverage ledger so unavailable 2023/most-2024 history is never presented as tested.

## Authorization

`real_orders = false`

`live_ready = false`

Historical replay certification, if eventually achieved, remains retrospective research evidence only.
