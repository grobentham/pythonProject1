from __future__ import annotations

import csv
import json
import math
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from v53_policy import (
    MAX_PER_ROUND,
    PARTIAL_POLICY,
    RISK_PCT,
    SAFETY_TTL_HOURS,
    as_float,
    as_int,
    downside_stop_distance,
    effective_ms,
    explicit_provider_entries,
    getv,
    instruction_kind,
    instruction_text,
    limit_fill_price,
    list_floats,
    partial_close_count,
    round_no,
    select_explicit_entries,
    setup_uid,
    side_of,
    synthetic_zone_entries,
)


class CallableRows(list):
    def __call__(self):
        return self


@dataclass
class Ticket:
    ticket_id: str
    setup_uid: str
    round_no: int
    layer_no: int
    revision: int
    side: str
    requested_entry: float
    sl: float
    targets: List[float]
    state: str = "PENDING"
    created_ms: int = 0
    fill_ms: Optional[int] = None
    fill_price: Optional[float] = None
    exit_ms: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    gross_pnl_usd: float = 0.0
    entry_commission_sgd: float = 0.0
    exit_commission_sgd: float = 0.0
    price_improved_on_gap: bool = False
    gap_through_stop: bool = False
    provider_explicit_entry: bool = False

    def row(self):
        data = asdict(self)
        data["targets"] = json.dumps(self.targets)
        return data


class V53PortfolioEngine:
    """Drop-in V5.2 execution-engine replacement with V5.3 integrity semantics."""

    def __init__(self, v2, ticks, fx, spec, policy, *args, **kwargs):
        self.v2, self.ticks, self.fx, self.spec, self.policy = v2, ticks, fx, spec, policy
        self.cash = float(getv(policy, "starting_balance_sgd", "start_balance_sgd", default=1000.0) or 1000.0)
        self.starting_balance_sgd = self.cash
        self.tickets: List[Ticket] = []
        self.audit: List[Dict[str, Any]] = []
        self.rejections = Counter()
        self.counters = Counter()
        self.round_state: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self.setup_reentry_disabled = set()
        self.deferred_triggers = defaultdict(list)
        self.revisions = Counter()
        self.peak_equity = self.cash
        self.max_drawdown_sgd = 0.0
        self.min_equity_sgd = self.cash
        self.max_equity_sgd = self.cash
        self.margin_call_seen = False
        self.first_margin_call_ms = None
        self.stopout = False
        self.commission_sgd = 0.0
        self.realized_pnl_sgd = 0.0
        self._last_heartbeat = time.monotonic()
        self._total_instructions = 0
        self._done_instructions = 0
        self.peak = self.peak_equity
        self.max_dd = 0.0
        self.max_drawdown = 0.0
        self.first_call = None
        self.fatal = False

    @property
    def ticket_rows(self):
        return CallableRows([t.row() for t in self.tickets])

    def __getattr__(self, name):
        # Preserve compatibility with V5.2 reporter-only diagnostic attributes.
        if name.startswith("gap_") or name.endswith("_count") or name.endswith("_seen"):
            return 0
        if name.startswith("max_") or name.startswith("min_"):
            return 0.0
        raise AttributeError(name)

    def _open(self):
        return [t for t in self.tickets if t.state == "OPEN"]

    def _pending(self):
        return [t for t in self.tickets if t.state == "PENDING"]

    def _rate(self, ms):
        try:
            return float(self.fx.rate_for_ms(int(ms)))
        except Exception:
            return float(self.fx.rate_for_date(self.v2.ms_to_dt(int(ms)).date()))

    def _round_price(self, price):
        return float(self.v2.round_price(float(price), self.spec))

    def _equity_margin(self, bid, ask, ms):
        rate = self._rate(ms)
        floating = 0.0
        for ticket in self._open():
            quote = bid if ticket.side == "BUY" else ask
            floating += (
                (quote - ticket.fill_price) * self.spec.ounces
                if ticket.side == "BUY"
                else (ticket.fill_price - quote) * self.spec.ounces
            )
        equity = self.cash + floating * rate
        market = max((bid + ask) / 2.0, 1e-9)
        margin = market * self.spec.ounces * len(self._open()) / max(float(self.spec.leverage), 1.0) * rate
        level = math.inf if margin <= 0 else equity / margin * 100.0
        return equity, margin, level

    def _ticket_risk_sgd(self, ticket, ms):
        entry = ticket.fill_price if ticket.state == "OPEN" and ticket.fill_price is not None else ticket.requested_entry
        return downside_stop_distance(ticket.side, entry, ticket.sl) * self.spec.ounces * self._rate(ms)

    def _reserved_risk_sgd(self, ms):
        return sum(self._ticket_risk_sgd(t, ms) for t in self.tickets if t.state in {"OPEN", "PENDING"})

    def _risk_accepts(self, side, entry, sl, ms, bid, ask):
        equity, _, _ = self._equity_margin(bid, ask, ms)
        candidate = downside_stop_distance(side, entry, sl) * self.spec.ounces * self._rate(ms)
        cap = max(0.0, equity * RISK_PCT / 100.0)
        return self._reserved_risk_sgd(ms) + candidate <= cap + 1e-9, candidate, cap

    def _margin_accepts_fill(self, bid, ask, ms):
        equity, current_margin, _ = self._equity_margin(bid, ask, ms)
        market = max((bid + ask) / 2.0, 1e-9)
        extra = market * self.spec.ounces / max(float(self.spec.leverage), 1.0) * self._rate(ms)
        return equity > current_margin + extra

    def _scope_tickets(self, ins, states=("OPEN", "PENDING")):
        tickets = [t for t in self.tickets if t.state in states]
        setup = setup_uid(ins)
        round_value = getv(ins, "round_no", "round", "round_number", default=None)
        round_id = as_int(round_value, None)
        entry = as_float(getv(ins, "entry_price", "scope_entry_price", default=None))
        scope = str(getv(ins, "scope", "scope_type", "scope_level", default="") or "").upper()
        if setup:
            tickets = [t for t in tickets if t.setup_uid == setup]
        if round_id is not None:
            tickets = [t for t in tickets if t.round_no == round_id]
        if entry is not None:
            tolerance = max(float(self.spec.tick_size), 0.01) * 1.5
            tickets = [t for t in tickets if abs(t.requested_entry - entry) <= tolerance]
        if not setup and round_id is None and entry is None and "ALL" not in scope and "INSTRUMENT" not in scope:
            return []
        return tickets

    def _extract_plan(self, ins):
        side = side_of(ins)
        lo = as_float(getv(ins, "zone_low", "entry_low", "low", default=None))
        hi = as_float(getv(ins, "zone_high", "entry_high", "high", default=None))
        sl = as_float(getv(ins, "sl", "stop", "stoploss", "new_sl", default=None))
        targets = list_floats(getv(ins, "targets", "tps", "take_profits", "new_targets", default=None))
        return side, lo, hi, sl, targets

    def _geometry_ok(self, side, entry, sl, targets):
        if side == "BUY":
            return sl < entry and all(tp > entry for tp in targets)
        return sl > entry and all(tp < entry for tp in targets)

    def _round_key(self, ins):
        return setup_uid(ins), round_no(ins)

    def _next_revision(self, key):
        self.revisions[key] += 1
        return self.revisions[key]

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
            self.round_state[(setup, round_id)] = {"side": side, "zone_low": lo, "zone_high": hi, "sl": sl, "targets": targets, "stage": old.get("stage", 0)}
            return

        explicit = explicit_provider_entries(ins, side, lo, hi)
        entries = select_explicit_entries(explicit, side) if explicit else synthetic_zone_entries(side, lo, hi, MAX_PER_ROUND)
        self.counters["rounds_using_explicit_provider_entries" if explicit else "rounds_using_synthetic_zone_entries"] += 1

        existing = [t.requested_entry for t in open_same + pending_same]
        tolerance = max(float(self.spec.tick_size), 0.01) * 0.5
        entries = [self._round_price(x) for x in entries if all(abs(x - y) > tolerance for y in existing)]
        entries = sorted(entries, reverse=(side == "BUY"))[:slots] if side == "BUY" else sorted(entries)[:slots]
        revision = self._next_revision((setup, round_id))
        order_type = str(getv(ins, "order_type", default="ZONE_LIMIT") or "ZONE_LIMIT").upper()
        market_now = "MARKET" in order_type or bool(__import__("re").search(r"(?i)\b(?:BUY|SELL)\s+NOW\b", instruction_text(ins)))

        for layer, entry in enumerate(entries, 1):
            if not self._geometry_ok(side, entry, sl, targets):
                self.rejections["BAD_SL_TP_GEOMETRY"] += 1
                continue
            accepted, candidate_risk, cap = self._risk_accepts(side, entry, sl, ms, bid, ask)
            if not accepted:
                self.rejections["MAX_RESERVED_STOP_RISK_PCT"] += 1
                self.audit.append({"time_ms": ms, "event": "REJECT_RESERVED_RISK", "setup_uid": setup, "round_no": round_id, "entry": entry, "candidate_risk_sgd": candidate_risk, "cap_sgd": cap})
                continue
            ticket = Ticket(
                ticket_id=f"{setup}__R{round_id}__L{layer}__REV{revision}",
                setup_uid=setup,
                round_no=round_id,
                layer_no=layer,
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

        self.round_state[(setup, round_id)] = {"side": side, "zone_low": lo, "zone_high": hi, "sl": sl, "targets": targets, "stage": old.get("stage", 0)}

    def _fill_ticket(self, ticket, ms, actual, bid, ask, market=False):
        if not self._margin_accepts_fill(bid, ask, ms):
            ticket.state = "REJECTED"
            ticket.exit_ms = ms
            ticket.exit_reason = "INSUFFICIENT_MARGIN_AT_ACTIVATION"
            self.rejections["INSUFFICIENT_MARGIN_AT_ACTIVATION"] += 1
            return
        actual = self._round_price(actual)
        ticket.state = "OPEN"
        ticket.fill_ms = ms
        ticket.fill_price = actual
        improved = (ticket.side == "BUY" and actual < ticket.requested_entry) or (ticket.side == "SELL" and actual > ticket.requested_entry)
        ticket.price_improved_on_gap = bool(improved and not market)
        if ticket.price_improved_on_gap:
            self.counters["gap_improved_limit_fills"] += 1
        commission = float(self.spec.commission_usd_per_side) * self._rate(ms)
        ticket.entry_commission_sgd = commission
        self.commission_sgd += commission
        self.cash -= commission
        age_hours = max(0.0, (ms - ticket.created_ms) / 3_600_000.0)
        for threshold in (6, 12, 24, 48):
            if age_hours > threshold:
                self.counters[f"pending_fill_age_gt_{threshold}h"] += 1
        self.audit.append({"time_ms": ms, "event": "FILL", "ticket_id": ticket.ticket_id, "requested_entry": ticket.requested_entry, "fill_price": actual, "price_improved_on_gap": ticket.price_improved_on_gap})

    def _close_ticket(self, ticket, ms, bid, ask, reason, forced_price=None):
        if ticket.state != "OPEN":
            return
        price = float(forced_price) if forced_price is not None else (float(bid) if ticket.side == "BUY" else float(ask))
        gross = (price - ticket.fill_price) * self.spec.ounces if ticket.side == "BUY" else (ticket.fill_price - price) * self.spec.ounces
        rate = self._rate(ms)
        commission = float(self.spec.commission_usd_per_side) * rate
        net = gross * rate - commission
        self.cash += net
        self.realized_pnl_sgd += net
        self.commission_sgd += commission
        ticket.state = "CLOSED"
        ticket.exit_ms, ticket.exit_price, ticket.exit_reason = ms, price, reason
        ticket.gross_pnl_usd, ticket.exit_commission_sgd = gross, commission
        if ticket.fill_ms is not None:
            start = self.v2.ms_to_dt(ticket.fill_ms).date()
            end = self.v2.ms_to_dt(ms).date()
            nights = max(0, (end - start).days)
            if nights:
                self.counters["overnight_closed_tickets"] += 1
                self.counters["ticket_nights"] += nights
        self.audit.append({"time_ms": ms, "event": "CLOSE", "ticket_id": ticket.ticket_id, "reason": reason, "price": price, "gross_pnl_usd": gross, "cash_sgd": self.cash})

    def _cancel_ticket(self, ticket, ms, reason):
        if ticket.state == "PENDING":
            ticket.state, ticket.exit_ms, ticket.exit_reason = "CANCELLED", ms, reason
            self.audit.append({"time_ms": ms, "event": "CANCEL", "ticket_id": ticket.ticket_id, "reason": reason})

    def _worst(self, tickets):
        if not tickets:
            return None
        return max(tickets, key=lambda x: x.fill_price) if tickets[0].side == "BUY" else min(tickets, key=lambda x: x.fill_price)

    def _better_price(self, tickets):
        return min(t.fill_price for t in tickets) if tickets[0].side == "BUY" else max(t.fill_price for t in tickets)

    def _set_sl(self, ins, ms, mode):
        tickets = self._scope_tickets(ins, states=("OPEN", "PENDING"))
        if not tickets:
            self.rejections["SL_AMEND_UNRESOLVED_SCOPE"] += 1
            return
        numeric = as_float(getv(ins, "new_sl", "sl", "stop", "stoploss", default=None))
        opens = [t for t in tickets if t.state == "OPEN"]
        better = self._better_price(opens) if opens else None
        for ticket in tickets:
            new_sl = ticket.fill_price if mode == "BE" and ticket.fill_price is not None else ticket.requested_entry if mode == "BE" else better if mode == "BETTER" and better is not None else numeric
            if new_sl is None:
                continue
            new_sl = self._round_price(new_sl)
            if ticket.state == "PENDING":
                valid = new_sl < ticket.requested_entry if ticket.side == "BUY" else new_sl > ticket.requested_entry
                if not valid:
                    self._cancel_ticket(ticket, ms, "INVALID_PENDING_SL_AMENDMENT")
                    self.rejections["INVALID_PENDING_SL_AMENDMENT"] += 1
                    continue
            ticket.sl = new_sl
            self.audit.append({"time_ms": ms, "event": "SL_AMEND", "ticket_id": ticket.ticket_id, "new_sl": new_sl, "mode": mode})

    def _apply_instruction(self, ins, bid, ask):
        ms = effective_ms(ins)
        kind = instruction_kind(ins)
        if kind in {"RUNNING_STATUS", "TP_STATUS", "SL_STATUS", "MISSED_STATUS", "RESULT_NOTICE", "PROFIT_STATUS"}:
            return
        side, lo, hi, sl, _ = self._extract_plan(ins)
        complete = side is not None and lo is not None and hi is not None and sl is not None
        if ("ARM" in kind and ("ROUND" in kind or "SETUP" in kind)) or kind in {"CREATE_ROUND", "ROUND_CREATE", "ARM_ROUND"} or (complete and not any(x in kind for x in ("RESULT", "STATUS", "ANALYSIS"))):
            self._arm_round(ins, ms, bid, ask)
            return
        if any(x in kind for x in ("ENTRY_ZONE_AMEND", "SET_ENTRY_ZONE", "EDIT_ENTRY", "ZONE_REPLACE")):
            self._arm_round(ins, ms, bid, ask, replace_pending=True)
            return
        if any(x in kind for x in ("MOVE_SL_TO_BETTER", "SL_TO_BETTER")):
            self._set_sl(ins, ms, "BETTER"); return
        if any(x in kind for x in ("MOVE_SL_TO_ENTRY", "MOVE_BE", "BREAKEVEN", "BREAK_EVEN")):
            self._set_sl(ins, ms, "BE"); return
        if any(x in kind for x in ("SET_SL", "SL_AMEND", "MOVE_SL_PRICE", "STOPLOSS_MOVE")):
            self._set_sl(ins, ms, "NUMERIC"); return
        if "NO_REENTRY" in kind or "REENTRY_PROHIBIT" in kind:
            setup = setup_uid(ins)
            if setup:
                self.setup_reentry_disabled.add(setup)
                self.deferred_triggers.pop(setup, None)
                for ticket in list(self._pending()):
                    if ticket.setup_uid == setup and ticket.round_no > 1:
                        self._cancel_ticket(ticket, ms, "NO_REENTRY")
            return
        if "CANCEL" in kind:
            tickets = self._scope_tickets(ins, states=("PENDING",))
            if not tickets:
                self.rejections["CANCEL_UNRESOLVED_SCOPE"] += 1
            for ticket in tickets:
                self._cancel_ticket(ticket, ms, kind)
            return
        if "CLOSE_WORST" in kind or "CLOSE_BAD_ENTRY" in kind:
            ticket = self._worst(self._scope_tickets(ins, states=("OPEN",)))
            if ticket:
                self._close_ticket(ticket, ms, bid, ask, "PROVIDER_CLOSE_WORST")
            else:
                self.rejections["CLOSE_WORST_UNRESOLVED_SCOPE"] += 1
            return
        if "CLOSE_PARTIAL" in kind or "CLOSE_HALF" in kind:
            tickets = self._scope_tickets(ins, states=("OPEN",))
            if not tickets:
                self.rejections["CLOSE_PARTIAL_UNRESOLVED_SCOPE"] += 1
                return
            count = partial_close_count(len(tickets))
            while count > 0 and tickets:
                ticket = self._worst(tickets)
                self._close_ticket(ticket, ms, bid, ask, f"PROVIDER_PARTIAL_{PARTIAL_POLICY}")
                tickets.remove(ticket)
                count -= 1
            return
        if "CLOSE" in kind or "EXIT" in kind:
            tickets = self._scope_tickets(ins, states=("OPEN",))
            if not tickets:
                self.rejections["CLOSE_UNRESOLVED_SCOPE"] += 1
            for ticket in tickets:
                self._close_ticket(ticket, ms, bid, ask, "PROVIDER_CLOSE")

    def _round_open(self, key):
        return [t for t in self._open() if (t.setup_uid, t.round_no) == key]

    def _round_pending(self, key):
        return [t for t in self._pending() if (t.setup_uid, t.round_no) == key]

    def _active_target(self, key):
        state = self.round_state.get(key, {})
        targets, stage = state.get("targets", []), int(state.get("stage", 0) or 0)
        return targets[stage] if stage < len(targets) else None

    def _handle_target(self, key, ms, bid, ask):
        tickets = self._round_open(key)
        if not tickets:
            return
        state = self.round_state.setdefault(key, {"stage": 0})
        stage = int(state.get("stage", 0) or 0)
        targets = state.get("targets", tickets[0].targets)
        if stage >= len(targets):
            return
        target = targets[stage]
        if stage == 0:
            for pending in self._round_pending(key):
                self._cancel_ticket(pending, ms, "TP1_CANCEL_PENDING")
            if len(tickets) == 1:
                self._close_ticket(tickets[0], ms, bid, ask, "TP1_SINGLE", forced_price=target)
            else:
                self._close_ticket(self._worst(tickets), ms, bid, ask, "TP1_SCALE", forced_price=target)
                for survivor in self._round_open(key):
                    survivor.sl = max(survivor.sl, survivor.fill_price) if survivor.side == "BUY" else min(survivor.sl, survivor.fill_price)
        else:
            tickets = self._round_open(key)
            final = stage >= len(targets) - 1 or len(tickets) <= 1
            if final:
                for ticket in list(tickets):
                    self._close_ticket(ticket, ms, bid, ask, f"TP{stage+1}_FINAL", forced_price=target)
            elif tickets:
                self._close_ticket(self._worst(tickets), ms, bid, ask, f"TP{stage+1}_SCALE", forced_price=target)
        state["stage"] = stage + 1

    def _process_quote(self, ms, bid, ask, instruction_tie=False):
        if instruction_tie:
            self.counters["instruction_price_same_tick"] += 1
        if SAFETY_TTL_HOURS is not None:
            ttl_ms = int(SAFETY_TTL_HOURS * 3_600_000)
            for ticket in list(self._pending()):
                if ms - ticket.created_ms >= ttl_ms:
                    self._cancel_ticket(ticket, ms, f"SAFETY_TTL_{SAFETY_TTL_HOURS:g}H")
                    self.counters["safety_ttl_cancellations"] += 1
        for ticket in list(self._pending()):
            triggered = ask <= ticket.requested_entry if ticket.side == "BUY" else bid >= ticket.requested_entry
            if triggered:
                self._fill_ticket(ticket, ms, limit_fill_price(ticket.side, ticket.requested_entry, bid, ask), bid, ask)
        for ticket in list(self._open()):
            hit = bid <= ticket.sl if ticket.side == "BUY" else ask >= ticket.sl
            if hit:
                if ticket.fill_ms == ms:
                    ticket.gap_through_stop = True
                    self.counters["gap_through_stop_fills"] += 1
                self._close_ticket(ticket, ms, bid, ask, "SL")
        for key in list({(t.setup_uid, t.round_no) for t in self._open()}):
            target = self._active_target(key)
            tickets = self._round_open(key)
            if target is not None and tickets:
                hit = bid >= target if tickets[0].side == "BUY" else ask <= target
                if hit:
                    self._handle_target(key, ms, bid, ask)
        if self._open():
            _, _, level = self._equity_margin(bid, ask, ms)
            if level <= float(self.spec.margin_call_pct) and not self.margin_call_seen:
                self.margin_call_seen, self.first_margin_call_ms, self.first_call = True, ms, ms
            if level <= float(self.spec.stopout_pct):
                self.stopout = True
                for ticket in list(self._open()):
                    self._close_ticket(ticket, ms, bid, ask, "BROKER_STOP_OUT")

    def _update_equity(self, times, bid, ask, rate):
        if not self._open() or len(times) == 0:
            return
        floating = np.zeros(len(times), dtype=float)
        for ticket in self._open():
            quote = bid if ticket.side == "BUY" else ask
            floating += (quote - ticket.fill_price) * self.spec.ounces if ticket.side == "BUY" else (ticket.fill_price - quote) * self.spec.ounces
        equity = self.cash + floating * rate
        self.min_equity_sgd = min(self.min_equity_sgd, float(np.min(equity)))
        self.max_equity_sgd = max(self.max_equity_sgd, float(np.max(equity)))
        peaks = np.maximum.accumulate(np.maximum(equity, self.peak_equity))
        self.max_drawdown_sgd = max(self.max_drawdown_sgd, float(np.max(peaks - equity)))
        self.peak_equity = max(self.peak_equity, float(np.max(equity)))
        self.peak, self.max_dd, self.max_drawdown = self.peak_equity, self.max_drawdown_sgd, self.max_drawdown_sgd

    def _first_event(self, start_ms, end_ms):
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
                    times, bid, ask = tick_day.times[lo:hi], tick_day.bid[lo:hi], tick_day.ask[lo:hi]
                    mask = np.zeros(len(times), dtype=bool)
                    for ticket in self._pending():
                        mask |= ask <= ticket.requested_entry if ticket.side == "BUY" else bid >= ticket.requested_entry
                    for ticket in self._open():
                        mask |= bid <= ticket.sl if ticket.side == "BUY" else ask >= ticket.sl
                    for key in {(t.setup_uid, t.round_no) for t in self._open()}:
                        target, tickets = self._active_target(key), self._round_open(key)
                        if target is not None and tickets:
                            mask |= bid >= target if tickets[0].side == "BUY" else ask <= target
                    indices = np.flatnonzero(mask)
                    cut = int(indices[0]) + 1 if len(indices) else len(times)
                    self._update_equity(times[:cut], bid[:cut], ask[:cut], self._rate(int(times[0])))
                    if len(indices):
                        index = int(indices[0])
                        return int(times[index]), float(bid[index]), float(ask[index])
            day += timedelta(days=1)
            cursor = self.v2.dt_to_ms(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
        return None

    def _heartbeat(self, ms=None, force=False):
        now = time.monotonic()
        if not force and now - self._last_heartbeat < 15:
            return
        self._last_heartbeat = now
        pct = 100.0 * self._done_instructions / self._total_instructions if self._total_instructions else 0.0
        stamp = self.v2.ms_to_iso(ms) if ms else "n/a"
        print(f"[V5.3] {pct:6.2f}% | {self._done_instructions:,}/{self._total_instructions:,} | {stamp} | open={len(self._open())} pending={len(self._pending())} | cash=S${self.cash:,.2f}", flush=True)

    def replay(self, instructions, last_data_ms):
        instructions = sorted(list(instructions), key=lambda x: (effective_ms(x), as_int(getv(x, "source_msg_id", "msg_id", default=0), 0)))
        if not instructions:
            return
        self._total_instructions = len(instructions)
        cursor, index = effective_ms(instructions[0]), 0
        self._heartbeat(cursor, force=True)
        while index < len(instructions):
            next_ms = effective_ms(instructions[index])
            while cursor < next_ms:
                event = self._first_event(cursor, next_ms)
                if event is None:
                    cursor = next_ms
                    break
                event_ms, bid, ask = event
                self._process_quote(event_ms, bid, ask)
                cursor = event_ms + 1
            group = []
            while index < len(instructions) and effective_ms(instructions[index]) == next_ms:
                group.append(instructions[index]); index += 1
            quote = self.ticks.quote_at_or_after(next_ms, 120_000)
            if quote is None:
                self.rejections["NO_QUOTE_FOR_INSTRUCTION"] += len(group)
                cursor = next_ms + 1
                self._done_instructions = index
                continue
            quote_ms, bid, ask = int(quote[0]), float(quote[1]), float(quote[2])
            for ins in group:
                self._apply_instruction(ins, bid, ask)
            self._done_instructions = index
            self._process_quote(quote_ms, bid, ask, instruction_tie=(quote_ms == next_ms))
            cursor = max(cursor, quote_ms + 1)
            self._heartbeat(quote_ms)
        while cursor <= last_data_ms and (self._open() or self._pending()):
            event = self._first_event(cursor, last_data_ms + 1)
            if event is None:
                break
            event_ms, bid, ask = event
            self._process_quote(event_ms, bid, ask)
            cursor = event_ms + 1
            self._heartbeat(event_ms)
        quote = self.ticks.quote_at_or_after(last_data_ms, 120_000)
        if quote:
            quote_ms, bid, ask = int(quote[0]), float(quote[1]), float(quote[2])
            for ticket in list(self._pending()):
                self._cancel_ticket(ticket, quote_ms, "DATA_END_PENDING_CANCEL")
            for ticket in list(self._open()):
                self._close_ticket(ticket, quote_ms, bid, ask, "FORCED_DATA_END")
        self._done_instructions = self._total_instructions
        self.gap_improved_limit_fills = int(self.counters["gap_improved_limit_fills"])
        self.gap_through_stop_fills = int(self.counters["gap_through_stop_fills"])
        self._heartbeat(last_data_ms, force=True)

    def integrity_summary(self):
        return {
            "version": "V5.3_PROVIDER_FAITHFUL_EXECUTION_INTEGRITY",
            "starting_balance_sgd": self.starting_balance_sgd,
            "cash_sgd": self.cash,
            "realized_pnl_sgd": self.realized_pnl_sgd,
            "commission_sgd": self.commission_sgd,
            "max_drawdown_sgd": self.max_drawdown_sgd,
            "min_equity_sgd": self.min_equity_sgd,
            "max_equity_sgd": self.max_equity_sgd,
            "margin_call_seen": self.margin_call_seen,
            "stopout": self.stopout,
            "tickets_total": len(self.tickets),
            "tickets_filled": sum(t.fill_ms is not None for t in self.tickets),
            "max_tickets_per_round": MAX_PER_ROUND,
            "account_wide_ticket_cap": None,
            "max_reserved_stop_risk_pct": RISK_PCT,
            "partial_policy": PARTIAL_POLICY,
            "safety_ttl_hours": SAFETY_TTL_HOURS,
            "counters": dict(self.counters),
            "rejections": dict(self.rejections),
        }
