from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence


MAX_EXPLICIT_TICKETS = 3
OR_PRIMARY_POLICY = "CLOSE_ALL"
ONE_TICKET_PARTIAL_PROJECTION = "CLOSE_FULL"


def _uniq(values: Iterable[float]) -> List[float]:
    out: List[float] = []
    for value in values:
        x = float(value)
        if x not in out:
            out.append(x)
    return out


def canonical_zone_entries(
    side: str,
    zone_low: float,
    zone_high: float,
    explicit_entries: Optional[Sequence[float]] = None,
    max_explicit_tickets: int = MAX_EXPLICIT_TICKETS,
) -> List[float]:
    """Freeze the provider-faithful entry projection before P&L is observed.

    Normal two-price zones are two boundary entries, never a synthetic midpoint.
    Explicit discrete provider entries are honored as written, capped only by the
    already-existing executable-ticket ceiling. A one-price zone is one ticket.
    """
    side = str(side).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"unsupported side: {side!r}")

    if explicit_entries:
        values = _uniq(explicit_entries)
        if max_explicit_tickets is not None:
            values = values[: max(0, int(max_explicit_tickets))]
        return values

    lo, hi = sorted((float(zone_low), float(zone_high)))
    if abs(hi - lo) < 1e-12:
        return [lo]
    return [hi, lo] if side == "BUY" else [lo, hi]


def worst_entry(side: str, fills: Sequence[float]) -> Optional[float]:
    if not fills:
        return None
    side = str(side).upper()
    return max(map(float, fills)) if side == "BUY" else min(map(float, fills))


def better_entry(side: str, fills: Sequence[float]) -> Optional[float]:
    if not fills:
        return None
    side = str(side).upper()
    return min(map(float, fills)) if side == "BUY" else max(map(float, fills))


def canonical_partial_close_count(n_open: int) -> int:
    """Provider 'close 1/2' projected onto indivisible 0.01 tickets.

    One 0.01 ticket cannot be halved at Blueberry's 0.01 minimum, so the frozen
    small-account projection closes it in full. Two tickets close one. More than
    two can exist only through explicit provider entries/re-entry state; half is
    rounded up to avoid leaving more risk than the instruction implies.
    """
    n = int(n_open)
    if n <= 0:
        return 0
    if n == 1:
        return 1
    return (n + 1) // 2


def target_mode(targets: Sequence[float]) -> str:
    n = len(list(targets))
    if n <= 0:
        return "NO_TARGET"
    if n == 1:
        return "SINGLE_FINAL_TP_DYNAMIC_MANAGEMENT"
    return "EXPLICIT_MULTI_TP_LADDER"


def target_assignment(n_entries: int, targets: Sequence[float]) -> List[Optional[float]]:
    """Diagnostic ticket-target ownership; engine remains message/state driven."""
    n = max(0, int(n_entries))
    tps = [float(x) for x in targets]
    if n == 0:
        return []
    if not tps:
        return [None] * n
    if len(tps) == 1:
        return [tps[0]] * n
    out: List[Optional[float]] = []
    for i in range(n):
        out.append(tps[min(i, len(tps) - 1)])
    return out


def is_close_all_or_sl_choice(text: str) -> bool:
    low = " ".join(str(text or "").lower().split())
    close_all = bool(re.search(r"\bclose\s+(?:all|full)\b", low))
    move_sl = bool(re.search(r"\b(?:move|set|put)\b.{0,30}\b(?:sl|stl|stop\s*loss|stoploss)\b", low))
    return close_all and " or " in f" {low} " and move_sl


def primary_or_choice(text: str) -> Optional[str]:
    """No-hindsight primary for provider alternatives.

    'Close all OR move SL' is frozen to CLOSE_ALL in the primary replay. The
    protective-SL alternative is reported as sensitivity only; outcomes never
    choose the better branch after the fact.
    """
    return OR_PRIMARY_POLICY if is_close_all_or_sl_choice(text) else None


def scope_priority(
    *,
    direct_reply: bool = False,
    explicit_setup: bool = False,
    explicit_round: bool = False,
    explicit_entry_price: bool = False,
    explicit_side: bool = False,
    recent_compatible_contexts: int = 0,
) -> str:
    """Describe the deterministic scope resolver outcome; ambiguous => fail closed."""
    if direct_reply:
        return "DIRECT_REPLY"
    if explicit_setup and explicit_round:
        return "EXPLICIT_SETUP_ROUND"
    if explicit_setup:
        return "EXPLICIT_SETUP"
    if explicit_entry_price:
        return "EXPLICIT_ENTRY_PRICE"
    if explicit_side and recent_compatible_contexts == 1:
        return "SIDE_RECENT_UNIQUE_CONTEXT"
    if recent_compatible_contexts == 1:
        return "RECENT_UNIQUE_CONTEXT"
    return "FAIL_CLOSED_AMBIGUOUS"


def reentry_mode(text: str) -> str:
    low = " ".join(str(text or "").lower().split())
    if re.search(r"\b(?:wait|waiting)\b.{0,40}\b(?:new|next)\s+signal\b", low):
        return "REENTRY_PROHIBITED_UNTIL_NEW_SIGNAL"
    if re.search(r"\bwait\b.{0,50}\b(?:price|zone)\b.{0,50}\b(?:again|re-?enter|buy|sell)\b", low):
        return "CONDITIONAL_REENTRY"
    if re.search(r"\b(?:buy|sell)\s+again\b|\bone\s+more\b|\bround\s*\d+\b", low):
        return "IMMEDIATE_OR_EXPLICIT_ROUND_REENTRY"
    return "NONE"


def size_directive(text: str) -> str:
    low = str(text or "").lower()
    if "small lot" in low or "small volume" in low:
        return "BLUEBERRY_MIN_0_01"
    if "big lot" in low or "large lot" in low:
        # Historical replay does not invent larger size from qualitative wording.
        return "UNSUPPORTED_SIZE_ESCALATION_FAIL_CLOSED"
    return "DEFAULT_0_01"
