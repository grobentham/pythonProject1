# XAUUSD Provider Language V4.1

Status: language/state audit checkpoint only. It does not certify profitability.

## Input model
Telegram messages are preserved in causal `(datetime, message_id)` order. Reply chains, explicit setup numbers, round numbers, side, instrument, and recent active context are used to resolve scope.

## Provider state hierarchy
`Instrument -> Setup -> Round -> Entry/Ticket`.

- Setup numbers are reused, so setup identity is composite and time/context scoped.
- Rounds belong to an existing setup when the message/reply/context proves that relationship.
- Rounds may have their own entry zone and SL.
- Old runners and new rounds may coexist.

## Message semantics
Each Telegram message is first classified, then expanded into one or more semantic actions.

Examples:
- `Running +30pips Close 1/2 Move SL to Entry` -> RUNNING_STATUS + CLOSE_PARTIAL + MOVE_SL_TO_ENTRY_BE.
- `Close bad entry and move SL to better entry` -> CLOSE_WORST_ENTRY + MOVE_SL_TO_BETTER_ENTRY.
- `Close all or move SL to 1963` -> alternative-choice bundle; no branch is auto-executed until a frozen policy selects one.

## Important provider rules learned from corpus
- `Running` is status only.
- TP/result posts are status unless an explicit management instruction is also present.
- `Buy again`, `Sell again`, `Join round N`, `re-engage`, etc. are re-entry/round authorization forms.
- Conditional re-entry is distinct from immediate authorization.
- `do not re-enter`, `wait for provider`, and `wait for new signal` are distinct states.
- `Cancel` normally affects pending scope; `Close` affects filled exposure.
- `Missed` can be entry-, round-, or setup-scoped.
- `Move SL to better entry` is distinct from each ticket's own break-even.
- Entry/SL/TP amendments are causal and supersede earlier values only from the amendment timestamp onward.
- Formal cards can complete/update a precursor setup rather than create a duplicate trade.
- Result cards can contain copied/template mistakes; parent setup state and contemporaneous instructions outrank retrospective labels.
- Non-XAU messages are not automatically mapped to Gold.

## Safety/audit policy
- Ambiguous mandatory actions fail closed.
- Optional/OR choices require a frozen replay policy.
- Analysis prose does not trade.
- Quoted/replied source text is context, not a new instruction.
- Deleted messages cannot be reconstructed.
- Final-text Telegram exports do not prove pre-edit contents.
- Provider pips/results never replace Blueberry bid/ask tick accounting.

## V4.1 full-corpus audit snapshot
- Dated messages: 40,583
- Operational candidates: 24,666
- Primary mandatory/conditional actions: 9,347
- High-confidence operational messages: 22,369
- Medium-confidence operational messages: 2,072
- Ambiguous operational messages: 225
- Primary unresolved mandatory actions fail-closed: 186
- Setup contexts: 6,368
- Expanded semantic action rows: 17,939
- Messages with multiple actions: 1,415
- Alternative-choice messages: 27

The next replay stage must consume the expanded action ledger, not the old single-intent V3 parser.
