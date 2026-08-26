#!/usr/bin/env python3
"""Blueberry XAUUSD Telegram Layered Replay V3.

Requires replay_blueberry_telegram_v2_forensic.py beside this file (or in Downloads).
READ ONLY: sends no MT5 orders.

Three executable 0.01 tickets are tested per zone setup. The three tested layer bands are:
  0.00 / 0.25 / 0.50
  0.25 / 0.50 / 0.75
  0.50 / 0.75 / 1.00

Management is frozen as:
- If only 1 ticket filled before TP1: TP1 closes the whole 0.01.
- If 2 filled: TP1 closes one 0.01; remaining ticket SL -> its own entry; TP2 closes it.
- If 3 filled: TP1 closes shallowest 0.01; remaining two SLs -> their own entries;
  TP2 closes next 0.01; TP3 closes final 0.01.
- All still-pending layers are cancelled at TP1.
- One-target signals close all at TP1; two-target signals close all remaining at TP2.
- No 0.005 lots are ever invented.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

V2_NAME = "replay_blueberry_telegram_v2_forensic.py"
OUT_NAME = "XAUUSD_BLUEBERRY_LAYERED_V3_RESULTS"
LAYER_CENTERS = [0.25, 0.50, 0.75]
OFFSET = 0.25


def _load_v2():
    here = Path(__file__).resolve().parent
    for p in (here / V2_NAME, Path.home() / "Downloads" / V2_NAME, Path.home() / "Desktop" / V2_NAME):
        if p.exists():
            s = importlib.util.spec_from_file_location("xau_v2", p)
            if s and s.loader:
                m = importlib.util.module_from_spec(s)
                sys.modules[s.name] = m
                s.loader.exec_module(m)
                return m, p
    raise FileNotFoundError(f"Put {V2_NAME} beside this V3 script.")


v2, V2_PATH = _load_v2()
ORIG_CREATE_ZIP = v2.create_zip


def depths(center: float) -> List[float]:
    return [round(max(0.0, min(1.0, center + x)), 6) for x in (-OFFSET, 0.0, OFFSET)]


def targets_for(signal: Dict[str, Any], spec) -> List[float]:
    vals = sorted(
        set(v2.round_price(float(x), spec) for x in signal.get("targets", []) if x is not None),
        reverse=(signal["side"] == "SELL"),
    )
    return vals[:3]


def pnl_usd(side: str, entry: float, px, spec):
    return (px - entry) * spec.ounces if side == "BUY" else (entry - px) * spec.ounces


def open_tix(tickets):
    return [t for t in tickets if t["state"] == "OPEN"]


def pending_tix(tickets):
    return [t for t in tickets if t["state"] == "PENDING"]


def account_state(side, opens, bid, ask, cash, rate, spec):
    if not opens:
        return cash, math.inf
    q = bid if side == "BUY" else ask
    floating = sum(float(pnl_usd(side, t["fill_price"], q, spec)) for t in opens) * rate
    equity = cash + floating
    market = (bid + ask) / 2.0
    margin = max(market * spec.ounces * len(opens) / spec.leverage * rate, 1e-12)
    return equity, equity / margin * 100.0


def scan_market(ticks, side, tickets, target, start_ms, end_ms, cash, fx, spec, peak_in):
    out = {
        "hit": False, "time_ms": None, "bid": None, "ask": None,
        "fill_layers": [], "min_equity_sgd": cash, "max_equity_sgd": cash,
        "margin_call_seen": False, "first_margin_call_ms": None,
        "equity_peak_out": peak_in, "max_drawdown_sgd": max(0.0, peak_in - cash),
    }
    if end_ms <= start_ms:
        return out
    d = v2.ms_to_dt(start_ms).date()
    end_d = v2.ms_to_dt(end_ms - 1).date()
    cursor = start_ms
    peak = peak_in
    max_dd = out["max_drawdown_sgd"]
    while d <= end_d:
        td = ticks.load_day(d)
        if len(td.times):
            lo = int(np.searchsorted(td.times, cursor, side="left"))
            hi = int(np.searchsorted(td.times, end_ms, side="left"))
            if hi > lo:
                times, bid, ask = td.times[lo:hi], td.bid[lo:hi], td.ask[lo:hi]
                entry_q = ask if side == "BUY" else bid
                exit_q = bid if side == "BUY" else ask
                pending, opens = pending_tix(tickets), open_tix(tickets)
                any_mask = np.zeros(len(times), dtype=bool)
                for t in pending:
                    any_mask |= (entry_q <= t["entry"]) if side == "BUY" else (entry_q >= t["entry"])
                for t in opens:
                    any_mask |= (exit_q <= t["sl"]) if side == "BUY" else (exit_q >= t["sl"])
                if opens and target is not None:
                    any_mask |= (exit_q >= target) if side == "BUY" else (exit_q <= target)

                rate = fx.rate_for_date(d)
                if opens:
                    floating = np.zeros(len(times), dtype=float)
                    for t in opens:
                        floating += pnl_usd(side, t["fill_price"], exit_q, spec)
                    equity = cash + floating * rate
                    market = (bid + ask) / 2.0
                    margin = np.maximum(market * spec.ounces * len(opens) / spec.leverage * rate, 1e-12)
                    ml = equity / margin * 100.0
                    any_mask |= ml <= spec.stopout_pct
                else:
                    equity = np.full(len(times), cash, dtype=float)
                    ml = np.full(len(times), np.inf, dtype=float)

                hits = np.flatnonzero(any_mask)
                rel_end = int(hits[0]) if len(hits) else len(times) - 1
                ew = equity[: rel_end + 1]
                if len(ew):
                    local_min, local_max = float(np.min(ew)), float(np.max(ew))
                    out["min_equity_sgd"] = min(out["min_equity_sgd"], local_min)
                    out["max_equity_sgd"] = max(out["max_equity_sgd"], local_max)
                    rp = np.maximum.accumulate(np.concatenate(([peak], ew)))[1:]
                    max_dd = max(max_dd, float(np.max(rp - ew)))
                    peak = max(peak, local_max)
                call = np.flatnonzero(ml[: rel_end + 1] <= spec.margin_call_pct)
                if len(call):
                    out["margin_call_seen"] = True
                    out["first_margin_call_ms"] = int(times[int(call[0])])

                if len(hits):
                    r = int(hits[0]); b = float(bid[r]); a = float(ask[r])
                    out.update(hit=True, time_ms=int(times[r]), bid=b, ask=a)
                    for t in pending:
                        if (a <= t["entry"]) if side == "BUY" else (b >= t["entry"]):
                            out["fill_layers"].append(t["layer"])
                    out["equity_peak_out"] = peak
                    out["max_drawdown_sgd"] = max_dd
                    return out
        d += timedelta(days=1)
        cursor = v2.dt_to_ms(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
    out["equity_peak_out"] = peak
    out["max_drawdown_sgd"] = max_dd
    return out


def close_one(t, when_ms, px, reason, cash, fx, spec, audit):
    rate = fx.rate_for_ms(when_ms)
    gross = float(v2.gross_pnl_usd(t["side"], t["fill_price"], px, spec))
    comm = spec.commission_usd_per_side * rate
    cash += gross * rate - comm
    t.update(state="CLOSED", exit_ms=when_ms, exit_price=float(px), exit_reason=reason,
             gross_pnl_usd=gross, exit_commission_sgd=comm)
    audit.append({"time_ms": when_ms, "event": "LAYER_CLOSED", "layer": t["layer"],
                  "depth": t["depth"], "price": float(px), "reason": reason})
    return cash, gross, comm


def simulate_layered(signal, telegram, cfg, ticks, fx, spec, start_balance_sgd, equity_peak_in, last_data_ms):
    activation = v2.effective_time_ms(signal, cfg)
    if activation is None:
        return {"status": "EXCLUDED_EDITED_SIGNAL_STRICT", "resolution_ms": signal["time_ms"], "filled": False}
    if activation > last_data_ms:
        return {"status": "OUTSIDE_DATA", "resolution_ms": activation, "filled": False}
    side = signal["side"]
    tps = targets_for(signal, spec)
    if not tps:
        return {"status": "NO_TARGET", "resolution_ms": activation, "filled": False}
    original_sl = v2.round_price(float(signal["sl"]), spec)
    q0 = ticks.quote_at_or_after(activation, v2.INITIAL_QUOTE_MAX_GAP_SECONDS * 1000)
    if q0 is None:
        return {"status": "UNSCORABLE_NO_INITIAL_QUOTE", "resolution_ms": activation, "filled": False}
    q0_ms, bid0, ask0 = q0
    ds = depths(cfg.depth)
    market_signal = signal.get("order_type") == "MARKET"
    if market_signal:
        ds = [cfg.depth]  # market instructions are never tripled blindly

    audit = [{"time_ms": activation, "event": "LAYERED_SIGNAL_EFFECTIVE", "side": side,
              "center": cfg.depth, "depths": ds, "targets": tps, "sl": original_sl}]
    tickets = []
    for i, dep in enumerate(ds, 1):
        entry = v2.round_price(ask0 if side == "BUY" else bid0, spec) if market_signal else v2.entry_for_depth(signal, dep, spec)
        geometry_ok = (original_sl < entry < tps[0]) if side == "BUY" else (tps[0] < entry < original_sl)
        if not geometry_ok:
            audit.append({"time_ms": q0_ms, "event": "LAYER_REJECT_BAD_GEOMETRY", "layer": i, "entry": entry})
            continue
        state = "OPEN" if market_signal else "PENDING"
        if state == "PENDING":
            ok, reason = v2.valid_pending(side, entry, bid0, ask0, spec)
            if not ok:
                audit.append({"time_ms": q0_ms, "event": "LAYER_REJECT_PENDING", "layer": i, "reason": reason})
                continue
        tickets.append({"layer": i, "depth": dep, "side": side, "entry": entry, "state": state,
                        "sl": original_sl, "fill_ms": q0_ms if state == "OPEN" else None,
                        "fill_price": entry if state == "OPEN" else None})
    if not tickets:
        return {"status": "NO_EXECUTABLE_LAYERS", "resolution_ms": q0_ms, "filled": False, "audit": audit}

    cash = start_balance_sgd
    entry_comm = exit_comm = gross_total = 0.0
    min_eq = max_eq = start_balance_sgd
    peak = equity_peak_in
    max_dd = max(0.0, peak - cash)
    margin_call = False; first_call = None
    ever_filled = set(); max_open = 0; first_fill = None; last_exit = None
    stage = 0; cursor = activation; expiry = activation + cfg.pending_ttl_minutes * 60 * 1000

    def cancel_pending(reason, when):
        for t in pending_tix(tickets):
            t["state"] = "CANCELLED"
            audit.append({"time_ms": when, "event": "LAYER_PENDING_CANCELLED", "layer": t["layer"], "reason": reason})

    def fill(ids, when, b, a):
        nonlocal cash, entry_comm, min_eq, max_dd, max_open, first_fill
        for lid in ids:
            t = next((x for x in tickets if x["layer"] == lid and x["state"] == "PENDING"), None)
            if not t:
                continue
            t["state"] = "OPEN"; t["fill_ms"] = when; t["fill_price"] = t["entry"]
            c = spec.commission_usd_per_side * fx.rate_for_ms(when)
            cash -= c; entry_comm += c; ever_filled.add(lid)
            first_fill = when if first_fill is None else min(first_fill, when)
            min_eq = min(min_eq, cash); max_dd = max(max_dd, peak - cash)
            audit.append({"time_ms": when, "event": "LAYER_FILLED", "layer": lid, "depth": t["depth"], "price": t["fill_price"]})
        max_open = max(max_open, len(open_tix(tickets)))

    if market_signal:
        for t in tickets:
            c = spec.commission_usd_per_side * fx.rate_for_ms(q0_ms)
            cash -= c; entry_comm += c; ever_filled.add(t["layer"]); first_fill = q0_ms
            audit.append({"time_ms": q0_ms, "event": "LAYER_FILLED_MARKET", "layer": t["layer"], "price": t["fill_price"]})
        max_open = len(open_tix(tickets))

    events = [e for e in v2.merge_events_for_signal(signal, telegram, cfg) if e["effective_ms"] >= activation]
    ei = 0

    def final(status, when, stopout=False, fatal=False):
        opens = open_tix(tickets)
        overnight = bool(first_fill is not None and last_exit is not None and v2.ms_to_dt(first_fill).date() != v2.ms_to_dt(last_exit).date())
        return {"status": status, "resolution_ms": when, "filled": bool(ever_filled),
                "fill_ms": first_fill, "fill_price": None, "exit_ms": last_exit, "exit_price": None,
                "exit_reason": status, "entry_commission_sgd": entry_comm, "exit_commission_sgd": exit_comm,
                "gross_pnl_usd": gross_total,
                "net_pnl_sgd": (cash - start_balance_sgd) if ever_filled and not opens else None,
                "balance_after_sgd": cash if not opens else start_balance_sgd,
                "min_equity_sgd": min_eq, "max_equity_sgd": max_eq,
                "mae_usd": 0.0, "mfe_usd": 0.0, "margin_call_seen": margin_call,
                "first_margin_call_ms": first_call, "stopout": stopout,
                "overnight_exposure": overnight, "peak_equity_out": peak,
                "max_drawdown_sgd": max_dd, "layers_planned": len(tickets),
                "layers_filled": len(ever_filled), "max_open_layers": max_open,
                "layer_depths": json.dumps([t["depth"] for t in tickets]),
                "layer_details": json.dumps(tickets, default=v2.json_default),
                "fatal": fatal or bool(opens), "audit": audit}

    while True:
        opens, pending = open_tix(tickets), pending_tix(tickets)
        if not opens and not pending:
            return final("LAYERED_COMPLETED", last_exit or cursor)
        next_mgmt = events[ei] if ei < len(events) else None
        next_mgmt_ms = next_mgmt["effective_ms"] if next_mgmt else None
        bounds = [last_data_ms + 1]
        if next_mgmt_ms is not None:
            bounds.append(next_mgmt_ms)
        if pending:
            bounds.append(expiry)
        boundary = min(bounds)
        target = tps[min(stage, len(tps) - 1)] if opens else None
        s = scan_market(ticks, side, tickets, target, cursor, boundary, cash, fx, spec, peak)
        min_eq = min(min_eq, s["min_equity_sgd"]); max_eq = max(max_eq, s["max_equity_sgd"])
        peak = max(peak, s["equity_peak_out"]); max_dd = max(max_dd, s["max_drawdown_sgd"])
        if s["margin_call_seen"]:
            margin_call = True
            if first_call is None: first_call = s["first_margin_call_ms"]

        if s["hit"]:
            tm, b, a = int(s["time_ms"]), float(s["bid"]), float(s["ask"])
            fill(s["fill_layers"], tm, b, a)
            opens = open_tix(tickets)
            if opens:
                eq, ml = account_state(side, opens, b, a, cash, fx.rate_for_ms(tm), spec)
                min_eq = min(min_eq, eq); peak = max(peak, eq); max_dd = max(max_dd, peak - eq)
                if ml <= spec.margin_call_pct:
                    margin_call = True
                    if first_call is None: first_call = tm
                if ml <= spec.stopout_pct:
                    cancel_pending("ACCOUNT_STOP_OUT", tm)
                    qx = b if side == "BUY" else a
                    for t in list(open_tix(tickets)):
                        cash, g, c = close_one(t, tm, qx, "STOP_OUT", cash, fx, spec, audit); gross_total += g; exit_comm += c
                    last_exit = tm
                    return final("STOP_OUT", tm, stopout=True)

                hit_sl = [t for t in opens if (b <= t["sl"] if side == "BUY" else a >= t["sl"])]
                if hit_sl:
                    if stage == 0:
                        cancel_pending("ORIGINAL_SL_HIT", tm); hit_sl = list(open_tix(tickets))
                    qx = b if side == "BUY" else a
                    for t in hit_sl:
                        cash, g, c = close_one(t, tm, qx, "SL" if stage == 0 else "BREAK_EVEN_SL", cash, fx, spec, audit); gross_total += g; exit_comm += c
                    last_exit = tm; cursor = tm + 1
                    if not open_tix(tickets) and not pending_tix(tickets):
                        return final("SL_EXIT" if stage == 0 else "BE_EXIT", tm)
                    continue

                target = tps[min(stage, len(tps) - 1)]
                target_hit = (b >= target) if side == "BUY" else (a <= target)
                if target_hit:
                    cancel_pending("TP_REACHED_CANCEL_UNFILLED", tm)
                    opens = sorted(open_tix(tickets), key=lambda x: x["depth"])
                    final_stage = stage >= min(2, len(tps) - 1)
                    to_close = opens if (len(opens) == 1 or final_stage) else opens[:1]
                    for t in to_close:
                        cash, g, c = close_one(t, tm, target, f"TP{stage+1}", cash, fx, spec, audit); gross_total += g; exit_comm += c
                    last_exit = tm
                    rem = open_tix(tickets)
                    if not rem:
                        return final(f"TP{stage+1}_FINAL", tm)
                    if stage == 0:
                        for t in rem:
                            t["sl"] = v2.round_price(float(t["fill_price"]), spec)
                            audit.append({"time_ms": tm, "event": "LAYER_SL_MOVED_TO_OWN_BE", "layer": t["layer"], "sl": t["sl"]})
                    stage += 1
                    if stage >= len(tps):
                        for t in list(open_tix(tickets)):
                            cash, g, c = close_one(t, tm, target, "FINAL_AVAILABLE_TP", cash, fx, spec, audit); gross_total += g; exit_comm += c
                        last_exit = tm
                        return final("FINAL_AVAILABLE_TP", tm)
                    cursor = tm + 1; continue
            cursor = tm + 1; continue

        if boundary >= last_data_ms + 1:
            if open_tix(tickets):
                return final("OPEN_AT_DATA_END_UNCERTIFIED", last_data_ms, fatal=True)
            return final("UNFILLED_DATA_END", last_data_ms)
        if pending and boundary == expiry and (next_mgmt_ms is None or expiry <= next_mgmt_ms):
            cancel_pending("PENDING_TTL_EXPIRED", expiry); cursor = expiry
            if not open_tix(tickets): return final("UNFILLED_TTL", expiry)
            continue

        e = next_mgmt; ei += 1; cursor = int(e["effective_ms"]); actions = e["actions"]
        audit.append({"time_ms": cursor, "event": "TELEGRAM_LAYER_MANAGEMENT", "msg_id": e["msg_id"], "actions": actions, "text": e["text"]})
        opens, pending = open_tix(tickets), pending_tix(tickets)
        if not opens:
            if ("CANCEL" in actions or "CLOSE_FULL" in actions or "CLOSE_PARTIAL" in actions or "RESULT_NOTICE" in actions or
                    (cfg.running_notice_cancels_unfilled and "RUNNING_NOTICE" in actions)):
                cancel_pending("PRE_FILL_TELEGRAM_MANAGEMENT", cursor)
                return final("CANCELLED_PENDING_BY_MANAGEMENT", cursor)
        else:
            if "CANCEL" in actions: cancel_pending("TELEGRAM_CANCEL", cursor)
            if "CLOSE_FULL" in actions:
                cancel_pending("TELEGRAM_CLOSE_FULL", cursor)
                q = ticks.quote_at_or_after(cursor, v2.CLOSE_QUOTE_MAX_GAP_SECONDS * 1000)
                if q is None: return final("UNSCORABLE_OPEN_CLOSE_NO_QUOTE", cursor, fatal=True)
                xm, b, a = q; qx = b if side == "BUY" else a
                for t in list(open_tix(tickets)):
                    cash, g, c = close_one(t, xm, qx, "TELEGRAM_CLOSE_FULL", cash, fx, spec, audit); gross_total += g; exit_comm += c
                last_exit = xm; return final("TELEGRAM_CLOSE_FULL", xm)
            if "CLOSE_PARTIAL" in actions:
                audit.append({"time_ms": cursor, "event": "TELEGRAM_PARTIAL_IGNORED_AUTO_LADDER"})

        q = ticks.quote_at_or_after(cursor, v2.CLOSE_QUOTE_MAX_GAP_SECONDS * 1000)
        if q:
            _, b, a = q
            if "MOVE_BE" in actions:
                for t in open_tix(tickets):
                    candidate = v2.round_price(float(t["fill_price"]), spec)
                    ok, _ = v2.valid_sl(side, candidate, b, a, spec)
                    if ok: t["sl"] = candidate
            if "SET_SL" in actions and e.get("new_sl") is not None:
                ns = v2.round_price(float(e["new_sl"]), spec); original_sl = ns
                for t in tickets:
                    if t["state"] == "OPEN":
                        ok, _ = v2.valid_sl(side, ns, b, a, spec)
                        if ok: t["sl"] = ns
                    elif t["state"] == "PENDING": t["sl"] = ns
            if "SET_TPS" in actions and e.get("new_targets"):
                tmp = dict(signal); tmp["targets"] = e["new_targets"]
                nt = targets_for(tmp, spec)
                if nt: tps = nt; stage = min(stage, len(tps)-1)


def _zip_override(source_dir: Path, _ignored: Path):
    policy = {
        "ticket_size_lots": 0.01,
        "max_layers": 3,
        "max_total_lots": 0.03,
        "tested_layer_bands": {str(c): depths(c) for c in LAYER_CENTERS},
        "management": {
            "1_filled": "TP1 close full 0.01",
            "2_filled": "TP1 close 0.01; remaining own BE; TP2 close remaining",
            "3_filled": "TP1 close shallowest 0.01; remaining own BE; TP2 close next; TP3 close final",
            "pending_at_TP1": "cancel",
            "fractional_0.005": "never used",
        },
    }
    (source_dir / "LAYERED_V3_POLICY.json").write_text(json.dumps(policy, indent=2), encoding="utf-8")
    (source_dir / "READ_LAYERED_V3_FIRST.txt").write_text(
        "Layered V3 uses three independent 0.01 tickets. One fill closes fully at TP1; two fills use TP1/BE/TP2; three fills use TP1/BE/TP2/TP3. Unfilled layers cancel at TP1.\n",
        encoding="utf-8",
    )
    return ORIG_CREATE_ZIP(source_dir, source_dir.parent / f"{OUT_NAME}.zip")


# Patch V2 only in memory; the V2 file on disk is unchanged.
v2.simulate_setup = simulate_layered
v2.COARSE_DEPTHS = LAYER_CENTERS
v2.TP_POLICIES = ["LAYERED_TP123_BE"]
v2.create_zip = _zip_override

# Force a separate V3 output folder unless the user explicitly supplied --output.
if "--output" not in sys.argv:
    sys.argv += ["--output", str(Path.home() / "Desktop" / OUT_NAME)]

print("Loaded V2 engine:", V2_PATH)
print("Layer bands:", {c: depths(c) for c in LAYER_CENTERS})
print("Rule: 1 fill->TP1 all; 2 fills->TP1/BE/TP2; 3 fills->TP1/BE/TP2/TP3")
print("Maximum simultaneous exposure: 0.03 lots (3 x 0.01)")

v2.main()
