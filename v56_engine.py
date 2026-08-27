from __future__ import annotations

import re

from v53_engine import Ticket, V53PortfolioEngine
from v53_policy import (
    MAX_PER_ROUND,
    RISK_PCT,
    as_float,
    as_int,
    explicit_provider_entries,
    getv,
    instruction_kind,
    instruction_text,
    round_no,
    setup_uid,
    side_of,
)
from v56_canonical_policy import (
    canonical_partial_close_count,
    canonical_zone_entries,
    is_close_all_or_sl_choice,
    target_mode,
)


class V56CanonicalEngine(V53PortfolioEngine):
    """Provider-canonical overlay frozen before the next historical P&L run.

    This changes interpretation, not alpha: ordinary two-price zones become two
    endpoint tickets; one-TP cards remain dynamically managed; explicit multi-TP
    cards use a ladder; market-now requests stay one ticket; ambiguous scope
    remains fail closed.
    """

    VERSION = "V5.6_PROVIDER_CANONICAL_INTERPRETATION"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._or_choice_consumed = set()

    @staticmethod
    def _explicit_global_or_side_scope(ins) -> bool:
        text = instruction_text(ins).upper()
        scope = str(getv(ins, "scope", "scope_type", "scope_level", default="") or "").upper()
        if "ALL" in scope or "INSTRUMENT" in scope or "SIDE" in scope:
            return True
        patterns = (
            r"\bCLOSE\s+ALL\s+(?:BUY|SELL)\b",
            r"\bCLOSE\s+ALL\s+(?:BUY|SELL)\s+GOLD\b",
            r"\bCLOSE\s+ALL\s+SIGNALS?\b",
            r"\bCLOSE\s+ALL\s+OPEN\s+ORDERS?\b",
            r"\bALL\s+(?:CURRENT\s+)?SIGNALS?\b",
            r"\bALL\s+(?:STOPLOSS|STOP\s+LOSS|STL)\b",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _scope_tickets(self, ins, states=("OPEN", "PENDING")):
        tickets = [t for t in self.tickets if t.state in states]
        setup = setup_uid(ins)
        round_value = getv(ins, "round_no", "round", "round_number", default=None)
        round_id = as_int(round_value, None)
        entry = as_float(getv(ins, "entry_price", "scope_entry_price", default=None))
        side = side_of(ins)
        global_or_side = self._explicit_global_or_side_scope(ins)

        if setup:
            tickets = [t for t in tickets if t.setup_uid == setup]
        if round_id is not None:
            tickets = [t for t in tickets if t.round_no == round_id]
        if entry is not None:
            tolerance = max(float(self.spec.tick_size), 0.01) * 1.5
            tickets = [t for t in tickets if abs(t.requested_entry - entry) <= tolerance]

        # Side is a valid narrowing dimension only when another resolver has
        # already identified context or the message explicitly declares global/
        # side-wide scope (e.g. "Close all BUY").
        if side and (setup or round_id is not None or entry is not None or global_or_side):
            tickets = [t for t in tickets if t.side == side]

        if not setup and round_id is None and entry is None and not global_or_side:
            return []
        return tickets

    def _arm_round(self, ins, ms, bid, ask, replace_pending=False):
        setup, round_id = self._round_key(ins)
        if not setup:
            self.rejections["ARM_NO_SETUP_SCOPE"] += 1
            return
        if round_id > 1 and setup in self.setup_reentry_disabled:
            self.rejections["REENTRY_DISABLED"] += 1
            return

        side, lo, hi, sl, targets = self._extract_plan(ins)
        old = self.round_state.get((setup, round_id), {})
        side = side or old.get("side")
        lo = lo if lo is not None else old.get("zone_low")
        hi = hi if hi is not None else old.get("zone_high")
        sl = sl if sl is not None else old.get("sl")
        targets = targets or old.get("targets", [])
        if side is None or lo is None or hi is None or sl is None:
            self.rejections["ARM_INCOMPLETE_GEOMETRY"] += 1
            return

        lo, hi, sl = self._round_price(lo), self._round_price(hi), self._round_price(sl)
        targets = [self._round_price(x) for x in targets]
        targets = sorted(set(targets), reverse=(side == "SELL"))

        if replace_pending:
            for ticket in list(self._pending()):
                if ticket.setup_uid == setup and ticket.round_no == round_id:
                    self._cancel_ticket(ticket, ms, "ENTRY_ZONE_AMENDED")

        open_same = [t for t in self._open() if t.setup_uid == setup and t.round_no == round_id]
        pending_same = [t for t in self._pending() if t.setup_uid == setup and t.round_no == round_id]
        slots = max(0, MAX_PER_ROUND - len(open_same) - len(pending_same))
        if slots <= 0:
            self.round_state[(setup, round_id)] = {
                "side": side, "zone_low": lo, "zone_high": hi, "sl": sl,
                "targets": targets, "target_mode": target_mode(targets),
                "stage": old.get("stage", 0),
            }
            return

        order_type = str(getv(ins, "order_type", default="ZONE_LIMIT") or "ZONE_LIMIT").upper()
        market_now = "MARKET" in order_type or bool(re.search(r"(?i)\b(?:BUY|SELL)\s+NOW\b", instruction_text(ins)))
        explicit = explicit_provider_entries(ins, side, lo, hi)

        if market_now:
            # Provider market-entry requests are one ticket. The original V5.3
            # synthetic-zone implementation could otherwise multiply exposure.
            entries = [float(ask if side == "BUY" else bid)]
            self.counters["rounds_using_canonical_market_single_ticket"] += 1
        else:
            entries = canonical_zone_entries(side, lo, hi, explicit if explicit else None, MAX_PER_ROUND)
            self.counters[
                "rounds_using_explicit_provider_entries" if explicit
                else "rounds_using_canonical_zone_boundaries"
            ] += 1

        existing = [t.requested_entry for t in open_same + pending_same]
        tolerance = max(float(self.spec.tick_size), 0.01) * 0.5
        entries = [self._round_price(x) for x in entries if all(abs(float(x) - y) > tolerance for y in existing)]
        entries = entries[:slots]
        revision = self._next_revision((setup, round_id))
        base_layer = len(open_same) + len(pending_same)

        for offset, entry in enumerate(entries, 1):
            if not self._geometry_ok(side, entry, sl, targets):
                self.rejections["BAD_SL_TP_GEOMETRY"] += 1
                continue
            accepted, candidate_risk, cap = self._risk_accepts(side, entry, sl, ms, bid, ask)
            if not accepted:
                self.rejections["MAX_RESERVED_STOP_RISK_PCT"] += 1
                self.audit.append({
                    "time_ms": ms, "event": "REJECT_RESERVED_RISK",
                    "setup_uid": setup, "round_no": round_id, "entry": entry,
                    "candidate_risk_sgd": candidate_risk, "cap_sgd": cap,
                })
                continue
            ticket = Ticket(
                ticket_id=f"{setup}__R{round_id}__L{base_layer+offset}__REV{revision}",
                setup_uid=setup,
                round_no=round_id,
                layer_no=base_layer + offset,
                revision=revision,
                side=side,
                requested_entry=entry,
                sl=sl,
                targets=list(targets),
                created_ms=ms,
                provider_explicit_entry=bool(explicit),
            )
            self.tickets.append(ticket)
            if market_now:
                actual = ask if side == "BUY" else bid
                if not self._margin_accepts_fill(bid, ask, ms):
                    ticket.state = "REJECTED"
                    ticket.exit_reason = "INSUFFICIENT_MARGIN"
                    self.rejections["INSUFFICIENT_MARGIN"] += 1
                else:
                    self._fill_ticket(ticket, ms, actual, bid, ask, market=True)

        self.round_state[(setup, round_id)] = {
            "side": side,
            "zone_low": lo,
            "zone_high": hi,
            "sl": sl,
            "targets": targets,
            "target_mode": target_mode(targets),
            "stage": old.get("stage", 0),
        }

    def _handle_target(self, key, ms, bid, ask):
        tickets = self._round_open(key)
        if not tickets:
            return
        state = self.round_state.setdefault(key, {"stage": 0})
        stage = int(state.get("stage", 0) or 0)
        targets = state.get("targets", tickets[0].targets)
        if stage >= len(targets):
            return

        # Single-TP cards are not retroactively turned into TP1/TP2/TP3. Their
        # scaling/BE lifecycle comes from Telegram management. The published TP
        # is the final target for all remaining exposure.
        if len(targets) == 1:
            target = targets[0]
            for pending in list(self._round_pending(key)):
                self._cancel_ticket(pending, ms, "FINAL_TP_CANCEL_PENDING")
            for ticket in list(self._round_open(key)):
                self._close_ticket(ticket, ms, bid, ask, "SINGLE_FINAL_TP", forced_price=target)
            state["stage"] = 1
            return

        # Explicit multi-TP cards retain the V5.3 ladder, but only for targets
        # actually published by the provider.
        return super()._handle_target(key, ms, bid, ask)

    def _apply_instruction(self, ins, bid, ask):
        text = instruction_text(ins)
        if is_close_all_or_sl_choice(text):
            source_key = str(getv(ins, "source_msg_id", "msg_id", "message_id", default="") or "")
            key = source_key or f"{setup_uid(ins)}:{round_no(ins)}:{getv(ins, 'effective_ms', 'time_ms', default=0)}:{text}"
            if key in self._or_choice_consumed:
                self.counters["or_choice_duplicate_action_suppressed"] += 1
                return
            tickets = self._scope_tickets(ins, states=("OPEN", "PENDING"))
            if not tickets:
                self.rejections["OR_CHOICE_UNRESOLVED_SCOPE"] += 1
                return
            ms = as_int(getv(ins, "effective_ms", "time_ms", default=0), 0) or 0
            for ticket in list(tickets):
                if ticket.state == "OPEN":
                    self._close_ticket(ticket, ms, bid, ask, "PROVIDER_OR_PRIMARY_CLOSE_ALL")
                elif ticket.state == "PENDING":
                    self._cancel_ticket(ticket, ms, "PROVIDER_OR_PRIMARY_CLOSE_ALL")
            self._or_choice_consumed.add(key)
            self.counters["or_choice_primary_close_all"] += 1
            self.audit.append({
                "time_ms": ms,
                "event": "OR_CHOICE_PRIMARY_CLOSE_ALL",
                "setup_uid": setup_uid(ins),
                "round_no": round_no(ins),
                "source_msg_id": source_key,
                "text": text,
            })
            return

        kind = instruction_kind(ins)
        if "CLOSE_PARTIAL" in kind or "CLOSE_HALF" in kind:
            ms = as_int(getv(ins, "effective_ms", "time_ms", default=0), 0) or 0
            tickets = self._scope_tickets(ins, states=("OPEN",))
            if not tickets:
                self.rejections["CLOSE_PARTIAL_UNRESOLVED_SCOPE"] += 1
                return
            count = canonical_partial_close_count(len(tickets))
            while count > 0 and tickets:
                ticket = self._worst(tickets)
                self._close_ticket(ticket, ms, bid, ask, "PROVIDER_PARTIAL_CANONICAL")
                tickets.remove(ticket)
                count -= 1
            return

        return super()._apply_instruction(ins, bid, ask)

    def integrity_summary(self):
        result = super().integrity_summary()
        result.update({
            "version": self.VERSION,
            "canonical_zone_policy": "NORMAL_ZONE_TWO_BOUNDARY_TICKETS_NO_SYNTHETIC_MIDPOINT",
            "single_tp_policy": "DYNAMIC_PROVIDER_MANAGEMENT_THEN_FINAL_TP",
            "multi_tp_policy": "EXPLICIT_TARGETS_ONLY",
            "market_entry_policy": "ONE_TICKET",
            "or_choice_primary": "CLOSE_ALL",
            "real_orders": False,
            "live_ready": False,
        })
        return result
