# XAUUSD Provider-Faithful Replay V5.3

Status: execution-integrity candidate. V5.2/V4.1 provider-language semantics remain frozen.

## Changes from V5.2

1. **Downside-only stop risk**
   - BUY: `max(0, fill - SL)`
   - SELL: `max(0, SL - fill)`
   - BE/profit-locked positions consume zero remaining stop-loss risk.

2. **Pending risk reservation**
   - Open-position downside risk + pending-order reserved downside risk must remain <= 10% of equity at order acceptance.

3. **Concurrency**
   - Maximum 3 executable 0.01 tickets **per round**.
   - No artificial account-wide 3-ticket cap.
   - Portfolio risk and broker margin are the global constraints.

4. **Provider entries**
   - Structured/explicit provider entry prices take priority.
   - If more than 3 are explicit, frozen policy uses the deepest/better 3.
   - Synthetic 0/50/100 zone levels are used only when discrete entries are unavailable.

5. **Amendments**
   - Numeric SL amendments apply to OPEN and PENDING tickets.
   - Entry-zone amendments preserve filled tickets and replace only unfilled tickets.
   - Recreated orders receive revision-safe ticket IDs.

6. **Reentry prohibition**
   - Blocks future Round 2+ arming.
   - Cancels existing pending reentry rounds.
   - Clears stored deferred reentry triggers.

7. **Scope**
   - Close/cancel resolves ENTRY -> ROUND -> SETUP -> explicit ALL/INSTRUMENT.
   - Unresolved generic actions fail closed.

8. **TP validation**
   - Invalid TP geometry is rejected instead of producing immediate fake target exits.

9. **Pending age**
   - Primary has no arbitrary TTL.
   - Reports fills older than 6h / 12h / 24h / 48h.
   - Optional sensitivity: `V53_SAFETY_TTL_HOURS=24`.

10. **Partial-close sensitivity**
    - Primary: `CEIL_HALF` (3 x .01 -> close .02).
    - Sensitivity: `V53_PARTIAL_POLICY=FLOOR_HALF` (3 x .01 -> close .01).

11. **Gap execution**
    - BUY LIMIT fill = min(limit, Ask)
    - SELL LIMIT fill = max(limit, Bid)
    - gap-through-stop cases are separately counted.

12. **Tie diagnostics**
    - Same-timestamp Telegram/price ties are recorded.
    - Frozen primary tie rule is instruction-first.

13. **Overnight diagnostics**
    - Counts overnight closed tickets and ticket-nights.
    - Historical swap is still not invented without verified historical Blueberry terms.

## Unchanged
- S$1,000 starting balance.
- 0.01 minimum/executable ticket.
- V4.1 Setup -> Round -> Entry language/state hierarchy.
- Blueberry bid/ask ticks are P&L truth.
- Ambiguous mandatory Telegram actions fail closed.
- Historical swap uncertainty remains disclosed.
