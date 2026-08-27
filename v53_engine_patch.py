from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

import numpy as np

from v53_policy import as_float, getv, instruction_kind, instruction_text, list_floats, round_no, setup_uid


def apply_v53_integrity_patch(engine_cls):
    original_apply = engine_cls._apply_instruction
    original_set_sl = engine_cls._set_sl
    original_handle_target = engine_cls._handle_target
    original_replay = engine_cls.replay

    def set_targets(self, ins, ms):
        tickets = self._scope_tickets(ins, states=("OPEN", "PENDING"))
        values = list_floats(getv(ins, "new_targets", "targets", "tps", "take_profits", default=None))
        if not values:
            self.rejections["TP_AMEND_NO_TARGETS"] += 1
            return
        if not tickets:
            self.rejections["TP_AMEND_UNRESOLVED_SCOPE"] += 1
            return

        accepted_any = False
        for ticket in tickets:
            entry = ticket.fill_price if ticket.fill_price is not None else ticket.requested_entry
            rounded = [self._round_price(x) for x in values]
            valid = [x for x in rounded if x > entry] if ticket.side == "BUY" else [x for x in rounded if x < entry]
            valid = sorted(set(valid), reverse=(ticket.side == "SELL"))
            if not valid:
                self.rejections["INVALID_TP_AMENDMENT"] += 1
                self.audit.append({"time_ms": ms, "event": "TP_AMEND_REJECT_GEOMETRY", "ticket_id": ticket.ticket_id, "values": str(values)})
                continue
            ticket.targets = list(valid)
            accepted_any = True
            self.audit.append({"time_ms": ms, "event": "TP_AMEND", "ticket_id": ticket.ticket_id, "targets": str(valid)})

        setup = setup_uid(ins)
        rid = round_no(ins)
        key = (setup, rid)
        if accepted_any and setup and key in self.round_state:
            side = self.round_state[key].get("side")
            rounded = [self._round_price(x) for x in values]
            if side == "BUY":
                rounded = sorted(set(rounded))
            elif side == "SELL":
                rounded = sorted(set(rounded), reverse=True)
            self.round_state[key]["targets"] = rounded
            # Do not rewind a target stage already completed.
            self.round_state[key]["stage"] = min(int(self.round_state[key].get("stage", 0) or 0), max(0, len(rounded) - 1))

    def patched_set_sl(self, ins, ms, mode):
        before = {}
        for ticket in self._scope_tickets(ins, states=("OPEN", "PENDING")):
            before[ticket.ticket_id] = ticket.sl
        original_set_sl(self, ins, ms, mode)

        # Numeric amendments must also update round state for future replacement tickets.
        if mode == "NUMERIC":
            numeric = as_float(getv(ins, "new_sl", "sl", "stop", "stoploss", default=None))
            setup = setup_uid(ins)
            rid = round_no(ins)
            key = (setup, rid)
            if numeric is not None and setup and key in self.round_state:
                self.round_state[key]["sl"] = self._round_price(numeric)

        # Risk cap applies to an SL amendment that increases downside risk.
        scoped = self._scope_tickets(ins, states=("OPEN", "PENDING"))
        if scoped:
            quote = self.ticks.quote_at_or_after(ms, 120_000)
            if quote:
                qms, bid, ask = int(quote[0]), float(quote[1]), float(quote[2])
                equity, _, _ = self._equity_margin(bid, ask, qms)
                cap = max(0.0, equity * 0.10)
                reserved = self._reserved_risk_sgd(qms)
                if reserved > cap + 1e-9:
                    # Fail closed: roll back only the amendments from this instruction.
                    for ticket in scoped:
                        if ticket.ticket_id in before:
                            ticket.sl = before[ticket.ticket_id]
                    self.rejections["SL_AMEND_WOULD_BREACH_RESERVED_RISK"] += 1
                    self.audit.append({"time_ms": ms, "event": "SL_AMEND_ROLLBACK_RISK_CAP", "reserved_risk_sgd": reserved, "cap_sgd": cap})

    def patched_handle_target(self, key, ms, bid, ask):
        """Record the automatic TP1 lifecycle so its later Telegram confirmation is idempotent."""
        state = self.round_state.setdefault(key, {"stage": 0})
        before_stage = int(state.get("stage", 0) or 0)
        before_open = len(self._round_open(key))
        original_handle_target(self, key, ms, bid, ask)
        state = self.round_state.setdefault(key, {"stage": 0})
        after_stage = int(state.get("stage", 0) or 0)
        after_open = len(self._round_open(key))
        if before_stage == 0 and after_stage >= 1:
            state["tp1_auto_transition_ms"] = int(ms)
            state["tp1_auto_closed_count"] = max(0, before_open - after_open)
            state["tp1_confirmation_partial_consumed"] = False

    def _combined_partial_be_confirmation(ins):
        text = instruction_text(ins).lower()
        if not text:
            return False
        partial = bool(re.search(r"\bclose\s*(?:1\s*/\s*2|half|50\s*%)\b", text))
        be = bool(
            re.search(r"\b(?:break\s*even|breakeven)\b", text)
            or re.search(r"\b(?:sl|stl|stop\s*loss|stoploss)\b.{0,24}\b(?:to\s+)?entry\b", text)
            or re.search(r"\bmove\b.{0,24}\b(?:sl|stl|stop\s*loss|stoploss)\b.{0,24}\bentry\b", text)
        )
        return partial and be

    def patched_apply(self, ins, bid, ask):
        kind = instruction_kind(ins)
        if any(token in kind for token in ("SET_TP", "SET_TPS", "TP_AMEND", "TARGET_AMEND")):
            set_targets(self, ins, int(getv(ins, "effective_ms", "time_ms", default=0) or 0))
            return

        # Provider-language V4.1 expands e.g. "Running +30pips Close 1/2 Move SL to Entry"
        # into RUNNING_STATUS + CLOSE_PARTIAL + MOVE_SL_TO_ENTRY_BE. If price already hit TP1,
        # the mechanical TP1 transition has already closed the shallow/worst ticket and moved
        # survivors to their own BE. The first matching Telegram bundle after that event is a
        # confirmation of the same lifecycle transition, not permission to close a second layer.
        # A later standalone partial instruction remains executable.
        if "CLOSE_PARTIAL" in kind or "CLOSE_HALF" in kind:
            setup = setup_uid(ins)
            rid = round_no(ins)
            key = (setup, rid)
            state = self.round_state.get(key, {})
            if (
                setup
                and state.get("tp1_auto_transition_ms") is not None
                and int(state.get("stage", 0) or 0) >= 1
                and not bool(state.get("tp1_confirmation_partial_consumed", False))
                and _combined_partial_be_confirmation(ins)
            ):
                state["tp1_confirmation_partial_consumed"] = True
                self.counters["provider_partial_tp1_confirmation_suppressed"] += 1
                self.audit.append({
                    "time_ms": int(getv(ins, "effective_ms", "time_ms", default=0) or 0),
                    "event": "PROVIDER_PARTIAL_TP1_CONFIRMATION_IDEMPOTENT",
                    "setup_uid": setup,
                    "round_no": rid,
                    "text": instruction_text(ins),
                })
                return

        return original_apply(self, ins, bid, ask)

    def patched_first_event(self, start_ms, end_ms):
        """V5.3 event scan includes stop-out/margin events, not only fill/SL/TP events."""
        if end_ms <= start_ms:
            return None
        day = self.v2.ms_to_dt(start_ms).date()
        end_day = self.v2.ms_to_dt(end_ms - 1).date()
        cursor = start_ms
        while day <= end_day:
            tick_day = self.ticks.load_day(day)
            if len(tick_day.times):
                lo = int(np.searchsorted(tick_day.times, cursor, side="left"))
                hi = int(np.searchsorted(tick_day.times, end_ms, side="left"))
                if hi > lo:
                    times = tick_day.times[lo:hi]
                    bid = tick_day.bid[lo:hi]
                    ask = tick_day.ask[lo:hi]
                    mask = np.zeros(len(times), dtype=bool)

                    for ticket in self._pending():
                        mask |= (ask <= ticket.requested_entry) if ticket.side == "BUY" else (bid >= ticket.requested_entry)
                    for ticket in self._open():
                        mask |= (bid <= ticket.sl) if ticket.side == "BUY" else (ask >= ticket.sl)
                    for key in {(t.setup_uid, t.round_no) for t in self._open()}:
                        target = self._active_target(key)
                        tickets = self._round_open(key)
                        if target is not None and tickets:
                            mask |= (bid >= target) if tickets[0].side == "BUY" else (ask <= target)

                    rate = self._rate(int(times[0]))
                    stopout_idx = None
                    margin_call_idx = None
                    if self._open():
                        floating = np.zeros(len(times), dtype=float)
                        for ticket in self._open():
                            quote = bid if ticket.side == "BUY" else ask
                            floating += (quote - ticket.fill_price) * self.spec.ounces if ticket.side == "BUY" else (ticket.fill_price - quote) * self.spec.ounces
                        equity = self.cash + floating * rate
                        market = np.maximum((bid + ask) / 2.0, 1e-9)
                        margin = market * self.spec.ounces * len(self._open()) / max(float(self.spec.leverage), 1.0) * rate
                        level = np.where(margin > 0, equity / margin * 100.0, np.inf)
                        calls = np.flatnonzero(level <= float(self.spec.margin_call_pct))
                        stops = np.flatnonzero(level <= float(self.spec.stopout_pct))
                        if len(calls):
                            margin_call_idx = int(calls[0])
                            if not self.margin_call_seen:
                                self.margin_call_seen = True
                                self.first_margin_call_ms = int(times[margin_call_idx])
                                self.first_call = self.first_margin_call_ms
                        if len(stops):
                            stopout_idx = int(stops[0])

                    ordinary = np.flatnonzero(mask)
                    ordinary_idx = int(ordinary[0]) if len(ordinary) else None
                    candidates = [x for x in (ordinary_idx, stopout_idx) if x is not None]
                    first_idx = min(candidates) if candidates else None
                    cut = first_idx + 1 if first_idx is not None else len(times)
                    self._update_equity(times[:cut], bid[:cut], ask[:cut], rate)

                    if first_idx is not None:
                        return int(times[first_idx]), float(bid[first_idx]), float(ask[first_idx])

            day += timedelta(days=1)
            cursor = self.v2.dt_to_ms(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
        return None

    def patched_replay(self, instructions, last_data_ms):
        result = original_replay(self, instructions, last_data_ms)
        # If V2 offers a quote-at-or-before helper, make sure final open exposure is not silently omitted.
        if self._open() and hasattr(self.ticks, "quote_at_or_before"):
            quote = self.ticks.quote_at_or_before(last_data_ms)
            if quote:
                qms, bid, ask = int(quote[0]), float(quote[1]), float(quote[2])
                for ticket in list(self._open()):
                    self._close_ticket(ticket, qms, bid, ask, "FORCED_DATA_END")
        return result

    engine_cls._set_targets_v53 = set_targets
    engine_cls._set_sl = patched_set_sl
    engine_cls._handle_target = patched_handle_target
    engine_cls._apply_instruction = patched_apply
    engine_cls._first_event = patched_first_event
    engine_cls.replay = patched_replay
    return engine_cls
