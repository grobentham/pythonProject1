from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from v56_canonical_policy import canonical_partial_close_count, canonical_zone_entries
from v56_public_m1_account_survival import (
    Account,
    COMMISSION_SGD_SIDE,
    DATA_DIR,
    INPUT_DIR,
    OZ,
    SGD_PER_USD_PROXY,
    Ticket,
    first_ge,
    iso,
    load_market,
    ms,
    schedule_first_touch,
)

OUT_DIR = Path("xauusd_replay/v57_signal_selection")
TRAIN_END = pd.Timestamp("2024-12-31T23:59:59Z")
VAL_START = pd.Timestamp("2025-01-01T00:00:00Z")
VAL_END = pd.Timestamp("2025-12-31T23:59:59Z")
HOLD_START = pd.Timestamp("2026-01-01T00:00:00Z")
BLUEBERRY_BOUNDARY = pd.Timestamp("2024-12-17T06:39:39.055Z")

CONT_FEATURES = [
    "zone_width", "shallow_stop", "deep_stop", "shallow_target", "deep_target",
    "shallow_rr", "deep_rr", "mean_rr", "target_zone_ratio", "stop_zone_ratio",
    "minutes_since_prior_signal", "signals_30m", "signals_60m", "same_side_60m", "same_side_streak",
    "spread", "prior_mid", "zone_near_distance", "zone_far_distance", "directional_zone_gap",
    "ret_15m", "ret_60m", "range_30m", "range_60m", "zone_width_over_range60",
    "stop_over_range60", "target_over_range60",
]
CAT_FEATURES = ["side", "session", "weekday"]
FEATURES = CAT_FEATURES + CONT_FEATURES
FEATURE_FAMILY = {
    **{k: "geometry" for k in ["zone_width", "shallow_stop", "deep_stop", "shallow_target", "deep_target",
                                "shallow_rr", "deep_rr", "mean_rr", "target_zone_ratio", "stop_zone_ratio"]},
    **{k: "calendar" for k in ["side", "session", "weekday"]},
    **{k: "cadence" for k in ["minutes_since_prior_signal", "signals_30m", "signals_60m", "same_side_60m", "same_side_streak"]},
    **{k: "market" for k in ["spread", "prior_mid", "zone_near_distance", "zone_far_distance", "directional_zone_gap",
                              "ret_15m", "ret_60m", "range_30m", "range_60m", "zone_width_over_range60",
                              "stop_over_range60", "target_over_range60"]},
}


def session_for_hour(h: int) -> str:
    if 0 <= h <= 6:
        return "ASIA"
    if 7 <= h <= 12:
        return "LONDON"
    if 13 <= h <= 20:
        return "NEW_YORK"
    return "LATE"


def safe_div(a, b):
    if b is None or not np.isfinite(b) or abs(b) < 1e-12:
        return np.nan
    return float(a) / float(b)


class ResearchAccount(Account):
    """Same canonical execution path as V5.6 but no capital coupling.

    Every provider ticket is accepted so later historical setup outcomes remain
    observable even if an account-level path would already be insolvent.
    """

    def can_fill(self, i, fill_price):
        return True

    def broker_check(self, i):
        self.record_equity(self.equity(i, "close"))
        return False


def load_signal_rows(ts: np.ndarray):
    import csv

    submissions = defaultdict(list)
    management = defaultdict(list)
    setup_meta = {}
    rows = []
    with (INPUT_DIR / "signals.csv").open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = ms(r["time_utc"])
            row = {
                "uid": r["uid"], "time_ms": t, "time_utc": r["time_utc"], "side": r["side"],
                "lo": float(r["lo"]), "hi": float(r["hi"]), "sl": float(r["sl"]), "tp": float(r["tp"]),
            }
            rows.append(row)
            idx = first_ge(ts, t)
            setup_meta[r["uid"]] = row
            if idx < len(ts):
                submissions[idx].append(row)

    with (INPUT_DIR / "management_compact.csv").open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            idx = first_ge(ts, ms(r["time_utc"]))
            if idx < len(ts):
                management[idx].append((r["root_uid"], tuple(a for a in r["actions"].split("|") if a)))

    first_cancel = {}
    for idx, mgrows in management.items():
        for uid, actions in mgrows:
            if "cancel_pending" in actions:
                first_cancel[uid] = min(idx, first_cancel.get(uid, idx))
    return rows, submissions, management, setup_meta, first_cancel


def simulate_independent_outcomes(market: pd.DataFrame):
    A = {k: market[k].to_numpy(dtype="float64") for k in ["bo", "bh", "bl", "bc", "ao", "ah", "al", "ac"]}
    A["ts"] = market["timestamp"].to_numpy(dtype="int64")
    signal_rows, submissions, management, setup_meta, first_cancel = load_signal_rows(A["ts"])
    account = ResearchAccount(A)
    fill_events = defaultdict(list)
    first_signal_idx = min(submissions) if submissions else len(A["ts"])

    for i in range(first_signal_idx, len(A["ts"])):
        for s in submissions.get(i, ()):
            cancel_i = first_cancel.get(s["uid"], len(A["ts"]))
            for layer, entry in enumerate(canonical_zone_entries(s["side"], s["lo"], s["hi"]), 1):
                t = Ticket(
                    f"{s['uid']}__L{layer}", s["uid"], layer, s["side"], float(entry),
                    float(s["sl"]), float(s["tp"]), submit_idx=i,
                )
                account.arm(t)
                fi = schedule_first_touch(t, i, cancel_i, A)
                t.scheduled_fill_idx = fi
                if fi is not None:
                    fill_events[fi].append(t)

        for uid, actions in management.get(i, ()):
            if "cancel_pending" in actions:
                for t in list(account.setup_pending(uid)):
                    account.cancel(t, i, "PROVIDER_CANCEL_PENDING")
            opens = account.setup_open(uid)
            if "close_full" in actions:
                for t in list(opens):
                    px = A["bo"][i] if t.side == "BUY" else A["ao"][i]
                    account.close(t, i, px, "PROVIDER_CLOSE_FULL")
            elif "close_partial" in actions:
                opens = account.setup_open(uid)
                count = canonical_partial_close_count(len(opens))
                for _ in range(count):
                    t = account.worst(opens)
                    if t is None:
                        break
                    px = A["bo"][i] if t.side == "BUY" else A["ao"][i]
                    account.close(t, i, px, "PROVIDER_CLOSE_PARTIAL_CANONICAL")
                    opens.remove(t)
            if "move_sl_to_entry" in actions:
                for t in account.setup_open(uid):
                    t.sl = float(t.fill_price)

        for t in fill_events.pop(i, ()):
            if t.state != "PENDING":
                continue
            if t.side == "BUY":
                fill_price = A["ao"][i] if A["ao"][i] <= t.requested_entry else t.requested_entry
            else:
                fill_price = A["bo"][i] if A["bo"][i] >= t.requested_entry else t.requested_entry
            account.fill(t, i, fill_price)

        setup_targets = set()
        for t in list(account.open_map.values()):
            target_touch = A["bh"][i] >= t.tp if t.side == "BUY" else A["al"][i] <= t.tp
            if t.side == "BUY":
                hit_sl = A["bl"][i] <= t.sl
                adverse_px = A["bo"][i] if A["bo"][i] <= t.sl else t.sl
            else:
                hit_sl = A["ah"][i] >= t.sl
                adverse_px = A["ao"][i] if A["ao"][i] >= t.sl else t.sl
            if t.fill_idx == i and (hit_sl or target_touch):
                t.ambiguous_fail_closed = True
                account.close(t, i, adverse_px, "AMBIGUOUS_FILL_BAR_FAIL_CLOSED")
            elif hit_sl and target_touch:
                t.ambiguous_fail_closed = True
                account.close(t, i, adverse_px, "AMBIGUOUS_EXIT_BAR_FAIL_CLOSED")
            elif hit_sl:
                account.close(t, i, adverse_px, "STOP")
            elif target_touch:
                setup_targets.add(t.uid)

        for uid in setup_targets:
            opens = account.setup_open(uid)
            if opens:
                tp = setup_meta[uid]["tp"]
                for t in list(opens):
                    account.close(t, i, tp, "SINGLE_FINAL_TP")
                for t in list(account.setup_pending(uid)):
                    account.cancel(t, i, "FINAL_TP_CANCEL_PENDING")

    last_i = len(A["ts"]) - 1
    for t in list(account.pending_map.values()):
        account.cancel(t, last_i, "DATA_END_PENDING_CANCEL")
    for t in list(account.open_map.values()):
        px = A["bc"][last_i] if t.side == "BUY" else A["ac"][last_i]
        account.close(t, last_i, px, "DATA_END_MTM")

    ticket_df = pd.DataFrame([asdict(t) for t in account.tickets])
    if not ticket_df.empty:
        ticket_df["net_pnl_sgd"] = np.where(
            ticket_df["fill_idx"].notna(),
            ticket_df["gross_pnl_usd"].fillna(0.0) * SGD_PER_USD_PROXY - 2.0 * COMMISSION_SGD_SIDE,
            0.0,
        )
    return signal_rows, ticket_df


def build_features(signal_rows, market, ticket_df):
    ts = market["timestamp"].to_numpy(dtype="int64")
    bc = market["bc"].to_numpy(float); ac = market["ac"].to_numpy(float)
    bh = market["bh"].to_numpy(float); ah = market["ah"].to_numpy(float)
    bl = market["bl"].to_numpy(float); al = market["al"].to_numpy(float)
    mid = (bc + ac) / 2.0
    high_mid = (bh + ah) / 2.0
    low_mid = (bl + al) / 2.0

    grouped = {}
    if not ticket_df.empty:
        for uid, g in ticket_df.groupby("uid"):
            filled = g[g["fill_idx"].notna()]
            grouped[uid] = {
                "setup_net_sgd": float(g["net_pnl_sgd"].sum()),
                "filled_tickets": int(len(filled)),
                "positive_tickets": int((g["net_pnl_sgd"] > 0).sum()),
                "negative_tickets": int((g["net_pnl_sgd"] < 0).sum()),
                "ambiguous_tickets": int(g["ambiguous_fail_closed"].fillna(False).sum()),
            }

    ordered = sorted(signal_rows, key=lambda r: r["time_ms"])
    prior_times = []
    prior_sides = []
    out = []
    for s in ordered:
        t = int(s["time_ms"])
        dt = pd.Timestamp(t, unit="ms", tz="UTC")
        idx = int(np.searchsorted(ts, t, side="left")) - 1  # strictly prior completed bar
        if idx < 60 or idx >= len(ts):
            prior_times.append(t); prior_sides.append(s["side"])
            continue

        side = s["side"].upper(); lo, hi = sorted((float(s["lo"]), float(s["hi"])))
        entries = canonical_zone_entries(side, lo, hi)
        shallow = entries[0]; deep = entries[-1]
        if side == "BUY":
            shallow_stop = shallow - s["sl"]; deep_stop = deep - s["sl"]
            shallow_target = s["tp"] - shallow; deep_target = s["tp"] - deep
            directional_zone_gap = mid[idx] - hi
        else:
            shallow_stop = s["sl"] - shallow; deep_stop = s["sl"] - deep
            shallow_target = shallow - s["tp"]; deep_target = deep - s["tp"]
            directional_zone_gap = lo - mid[idx]

        zone_width = hi - lo
        spread = ac[idx] - bc[idx]
        r30 = float(np.nanmax(high_mid[idx-29:idx+1]) - np.nanmin(low_mid[idx-29:idx+1]))
        r60 = float(np.nanmax(high_mid[idx-59:idx+1]) - np.nanmin(low_mid[idx-59:idx+1]))
        ret15 = safe_div(mid[idx] - mid[idx-15], mid[idx-15])
        ret60 = safe_div(mid[idx] - mid[idx-60], mid[idx-60])
        zone_center = (lo + hi) / 2.0
        dists = [abs(mid[idx] - lo), abs(mid[idx] - hi)]

        p = np.array(prior_times, dtype=np.int64) if prior_times else np.array([], dtype=np.int64)
        cutoff30 = t - 30 * 60_000; cutoff60 = t - 60 * 60_000
        signals30 = int((p >= cutoff30).sum()) if len(p) else 0
        signals60 = int((p >= cutoff60).sum()) if len(p) else 0
        same60 = 0
        if prior_times:
            same60 = sum(1 for pt, ps in zip(prior_times, prior_sides) if pt >= cutoff60 and ps == side)
        streak = 0
        for ps in reversed(prior_sides):
            if ps == side: streak += 1
            else: break
        mins_prior = np.nan if not prior_times else (t - prior_times[-1]) / 60_000.0

        outcome = grouped.get(s["uid"], {"setup_net_sgd": 0.0, "filled_tickets": 0, "positive_tickets": 0,
                                         "negative_tickets": 0, "ambiguous_tickets": 0})
        row = {
            "uid": s["uid"], "time_utc": dt.isoformat(), "side": side,
            "session": session_for_hour(dt.hour), "weekday": int(dt.weekday()), "hour_utc": int(dt.hour),
            "zone_width": zone_width,
            "shallow_stop": shallow_stop, "deep_stop": deep_stop,
            "shallow_target": shallow_target, "deep_target": deep_target,
            "shallow_rr": safe_div(shallow_target, shallow_stop),
            "deep_rr": safe_div(deep_target, deep_stop),
            "mean_rr": np.nanmean([safe_div(shallow_target, shallow_stop), safe_div(deep_target, deep_stop)]),
            "target_zone_ratio": safe_div((shallow_target + deep_target) / 2.0, zone_width),
            "stop_zone_ratio": safe_div((shallow_stop + deep_stop) / 2.0, zone_width),
            "minutes_since_prior_signal": mins_prior,
            "signals_30m": signals30, "signals_60m": signals60, "same_side_60m": same60, "same_side_streak": streak,
            "spread": spread, "prior_mid": mid[idx],
            "zone_near_distance": min(dists), "zone_far_distance": max(dists), "directional_zone_gap": directional_zone_gap,
            "ret_15m": ret15, "ret_60m": ret60, "range_30m": r30, "range_60m": r60,
            "zone_width_over_range60": safe_div(zone_width, r60),
            "stop_over_range60": safe_div((shallow_stop + deep_stop) / 2.0, r60),
            "target_over_range60": safe_div((shallow_target + deep_target) / 2.0, r60),
            **outcome,
        }
        out.append(row)
        prior_times.append(t); prior_sides.append(side)
    return pd.DataFrame(out)


def split_name(ts):
    t = pd.Timestamp(ts)
    if t <= TRAIN_END:
        return "DISCOVERY"
    if VAL_START <= t <= VAL_END:
        return "VALIDATION"
    if t >= HOLD_START:
        return "HOLDOUT_2026"
    return "OTHER"


def metrics(df: pd.DataFrame, mask=None):
    x = df if mask is None else df.loc[mask]
    pnl = x["setup_net_sgd"].fillna(0.0).to_numpy(float)
    pos = pnl[pnl > 0]; neg = pnl[pnl < 0]
    cum = np.cumsum(pnl)
    if len(cum):
        peaks = np.maximum.accumulate(np.r_[0.0, cum])[:-1]
        dd = float(np.max(peaks - cum))
    else:
        dd = 0.0
    monthly = x.assign(month=pd.to_datetime(x["time_utc"], utc=True).dt.to_period("M").astype(str)).groupby("month")["setup_net_sgd"].sum()
    best = float(pos.max()) if len(pos) else 0.0
    total_pos = float(pos.sum())
    return {
        "n": int(len(x)), "nonzero": int(np.count_nonzero(np.abs(pnl) > 1e-12)), "filled_setups": int((x["filled_tickets"] > 0).sum()),
        "net_sgd": float(pnl.sum()), "mean_sgd": float(pnl.mean()) if len(pnl) else 0.0,
        "median_sgd": float(np.median(pnl)) if len(pnl) else 0.0,
        "positive_setups": int((pnl > 0).sum()), "negative_setups": int((pnl < 0).sum()),
        "profit_factor": float(pos.sum() / abs(neg.sum())) if len(neg) and abs(neg.sum()) > 0 else (float("inf") if len(pos) else 0.0),
        "max_cum_drawdown_sgd": dd,
        "positive_months": int((monthly > 0).sum()), "months": int(len(monthly)),
        "best_setup_positive_pnl_share": float(best / total_pos) if total_pos > 0 else 0.0,
    }


def condition_mask(df, c):
    kind = c["kind"]
    if kind == "continuous":
        s = pd.to_numeric(df[c["feature"]], errors="coerce")
        return (s <= c["threshold"]) if c["op"] == "<=" else (s >= c["threshold"])
    if kind == "categorical":
        return df[c["feature"]].astype(str) == str(c["value"])
    raise ValueError(kind)


def candidate_univariates(train):
    out = []
    qs = np.arange(0.1, 1.0, 0.1)
    for f in CONT_FEATURES:
        vals = pd.to_numeric(train[f], errors="coerce").dropna()
        if vals.empty: continue
        for q, thr in zip(qs, vals.quantile(qs).to_numpy()):
            for op in ("<=", ">="):
                c = {"selector_family": "A_UNIVARIATE", "kind": "continuous", "feature": f, "op": op,
                     "threshold": float(thr), "quantile": float(q), "feature_family": FEATURE_FAMILY[f]}
                m = condition_mask(train, c); mt = metrics(train, m)
                if mt["n"] >= 150 and mt["nonzero"] >= 25:
                    c.update({f"train_{k}": v for k, v in mt.items()}); out.append(c)
    for f in CAT_FEATURES:
        for value in sorted(train[f].dropna().astype(str).unique()):
            c = {"selector_family": "A_UNIVARIATE", "kind": "categorical", "feature": f, "value": value,
                 "feature_family": FEATURE_FAMILY[f]}
            m = condition_mask(train, c); mt = metrics(train, m)
            if mt["n"] >= 150 and mt["nonzero"] >= 25:
                c.update({f"train_{k}": v for k, v in mt.items()}); out.append(c)
    out.sort(key=lambda c: (c["train_mean_sgd"], c["train_net_sgd"]), reverse=True)
    return out


def candidate_pairs(train, unis):
    # Keep best discovery screens per feature family, then cap pairs at 50 by discovery result.
    top = []
    for fam in sorted(set(c["feature_family"] for c in unis)):
        top.extend([c for c in unis if c["feature_family"] == fam][:8])
    pairs = []
    for i, a in enumerate(top):
        for b in top[i+1:]:
            if a["feature_family"] == b["feature_family"]: continue
            ma = condition_mask(train, a); mb = condition_mask(train, b); m = ma & mb
            mt = metrics(train, m)
            if mt["n"] < 150 or mt["nonzero"] < 25: continue
            c = {"selector_family": "B_TWO_CONDITION", "a": a, "b": b,
                 **{f"train_{k}": v for k, v in mt.items()}}
            pairs.append(c)
    pairs.sort(key=lambda c: (c["train_mean_sgd"], c["train_net_sgd"]), reverse=True)
    return pairs[:50]


def pair_mask(df, c):
    return condition_mask(df, c["a"]) & condition_mask(df, c["b"])


def prepare_matrix(train, other):
    tr = train[FEATURES].copy(); ot = other[FEATURES].copy()
    med = {}
    for f in CONT_FEATURES:
        tr[f] = pd.to_numeric(tr[f], errors="coerce"); ot[f] = pd.to_numeric(ot[f], errors="coerce")
        med[f] = float(tr[f].median()) if tr[f].notna().any() else 0.0
        tr[f] = tr[f].fillna(med[f]); ot[f] = ot[f].fillna(med[f])
    allx = pd.concat([tr, ot], axis=0, keys=["train", "other"])
    allx = pd.get_dummies(allx, columns=CAT_FEATURES, dummy_na=True, dtype=float)
    tr_x = allx.xs("train").copy(); ot_x = allx.xs("other").copy()
    scaler = StandardScaler()
    tr_x[CONT_FEATURES] = scaler.fit_transform(tr_x[CONT_FEATURES])
    ot_x[CONT_FEATURES] = scaler.transform(ot_x[CONT_FEATURES])
    return tr_x.to_numpy(float), ot_x.to_numpy(float), list(tr_x.columns)


def validation_gate(m):
    return m["net_sgd"] > 0 and m["profit_factor"] > 1.05 and m["n"] >= 100


def gate_rank(m):
    # lower DD first, then higher net after mandatory gates
    return (-m["max_cum_drawdown_sgd"], m["net_sgd"])


def select_best_from_candidates(candidates, val, family):
    scored = []
    for c in candidates:
        if family == "A_UNIVARIATE": mask = condition_mask(val, c)
        else: mask = pair_mask(val, c)
        vm = metrics(val, mask)
        row = {**c, **{f"val_{k}": v for k, v in vm.items()}, "validation_pass": validation_gate(vm)}
        scored.append(row)
    passed = [x for x in scored if x["validation_pass"]]
    best = None
    if passed:
        passed.sort(key=lambda x: (-x["val_max_cum_drawdown_sgd"], x["val_net_sgd"]), reverse=True)
        best = passed[0]
    return best, scored


def model_candidates(train, val, family):
    Xtr, Xv, cols = prepare_matrix(train, val)
    y = train["setup_net_sgd"].to_numpy(float)
    fracs = [0.1, 0.2, 0.3, 0.4, 0.5]
    out = []
    if family == "C_RIDGE":
        for alpha in [0.1, 1.0, 10.0, 100.0]:
            model = Ridge(alpha=alpha).fit(Xtr, y)
            pred = model.predict(Xv)
            for frac in fracs:
                thr = float(np.quantile(pred, 1.0-frac)); mask = pred >= thr
                vm = metrics(val, pd.Series(mask, index=val.index))
                out.append({"selector_family": family, "alpha": alpha, "fraction": frac, "validation_threshold": thr,
                            "val_metrics": vm, "validation_pass": validation_gate(vm), "columns": cols,
                            "coef": model.coef_.tolist(), "intercept": float(model.intercept_)})
    else:
        for leaf in [7, 15]:
            for lr in [0.03, 0.07]:
                for it in [100, 200]:
                    for l2 in [1.0, 10.0]:
                        model = HistGradientBoostingRegressor(max_leaf_nodes=leaf, learning_rate=lr, max_iter=it,
                                                              l2_regularization=l2, random_state=5701).fit(Xtr, y)
                        pred = model.predict(Xv)
                        for frac in fracs:
                            thr = float(np.quantile(pred, 1.0-frac)); mask = pred >= thr
                            vm = metrics(val, pd.Series(mask, index=val.index))
                            out.append({"selector_family": family, "max_leaf_nodes": leaf, "learning_rate": lr,
                                        "max_iter": it, "l2_regularization": l2, "fraction": frac,
                                        "validation_threshold": thr, "val_metrics": vm,
                                        "validation_pass": validation_gate(vm)})
    passed = [x for x in out if x["validation_pass"]]
    if not passed: return None, out
    passed.sort(key=lambda x: (-x["val_metrics"]["max_cum_drawdown_sgd"], x["val_metrics"]["net_sgd"]), reverse=True)
    return passed[0], out


def fit_predict_model(train, target, spec):
    Xtr, Xt, cols = prepare_matrix(train, target)
    y = train["setup_net_sgd"].to_numpy(float)
    if spec["selector_family"] == "C_RIDGE":
        model = Ridge(alpha=float(spec["alpha"])).fit(Xtr, y)
    else:
        model = HistGradientBoostingRegressor(max_leaf_nodes=int(spec["max_leaf_nodes"]),
            learning_rate=float(spec["learning_rate"]), max_iter=int(spec["max_iter"]),
            l2_regularization=float(spec["l2_regularization"]), random_state=5701).fit(Xtr, y)
    return model.predict(Xt)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    market = load_market()
    signals, tickets = simulate_independent_outcomes(market)
    features = build_features(signals, market, tickets)
    features["split"] = features["time_utc"].map(split_name)
    features.to_csv(OUT_DIR / "setup_feature_outcomes.csv", index=False)
    tickets.to_csv(OUT_DIR / "independent_tickets.csv", index=False)

    train = features[features["split"] == "DISCOVERY"].copy()
    val = features[features["split"] == "VALIDATION"].copy()
    hold = features[features["split"] == "HOLDOUT_2026"].copy()
    baseline = {"discovery": metrics(train), "validation": metrics(val), "holdout": metrics(hold)}

    unis = candidate_univariates(train)
    best_a, scored_a = select_best_from_candidates(unis, val, "A_UNIVARIATE")
    pairs = candidate_pairs(train, unis)
    best_b, scored_b = select_best_from_candidates(pairs, val, "B_TWO_CONDITION")
    best_c, scored_c = model_candidates(train, val, "C_RIDGE")
    best_d, scored_d = model_candidates(train, val, "D_HGBR")

    family_best = []
    if best_a:
        family_best.append(("A_UNIVARIATE", best_a, {k[4:]: v for k, v in best_a.items() if k.startswith("val_")}))
    if best_b:
        family_best.append(("B_TWO_CONDITION", best_b, {k[4:]: v for k, v in best_b.items() if k.startswith("val_")}))
    if best_c:
        family_best.append(("C_RIDGE", best_c, best_c["val_metrics"]))
    if best_d:
        family_best.append(("D_HGBR", best_d, best_d["val_metrics"]))

    final = None
    hold_metrics = None
    hold_mask = None
    if family_best:
        family_best.sort(key=lambda item: (-item[2]["max_cum_drawdown_sgd"], item[2]["net_sgd"]), reverse=True)
        fam, spec, vm = family_best[0]
        final = {"family": fam, "spec": spec, "validation_metrics": vm}
        if fam == "A_UNIVARIATE":
            hold_mask = condition_mask(hold, spec)
        elif fam == "B_TWO_CONDITION":
            hold_mask = pair_mask(hold, spec)
        else:
            pred = fit_predict_model(train, hold, spec)
            # Selection fraction is frozen; threshold is recomputed as a rank fraction in target period,
            # which does not read target outcomes and matches the preregistered top-fraction selector.
            q = float(np.quantile(pred, 1.0-float(spec["fraction"])))
            hold_mask = pd.Series(pred >= q, index=hold.index)
        hold_metrics = metrics(hold, hold_mask)
        hold_success = (
            hold_metrics["net_sgd"] > 0
            and hold_metrics["profit_factor"] >= 1.10
            and hold_metrics["n"] >= 75
            and hold_metrics["max_cum_drawdown_sgd"] < baseline["holdout"]["max_cum_drawdown_sgd"]
            and hold_metrics["positive_months"] >= 4
            and hold_metrics["best_setup_positive_pnl_share"] <= 0.50
        )
        final["holdout_metrics"] = hold_metrics
        final["holdout_success"] = bool(hold_success)
        final["holdout_selected_uids"] = hold.loc[hold_mask, "uid"].tolist()

    summary = {
        "schema": "V57_CAUSAL_SIGNAL_SELECTION_RESEARCH_V1",
        "classification": "RETROSPECTIVE_PUBLIC_M1_SIGNAL_SELECTION_RESEARCH_NOT_BLUEBERRY_CERTIFICATION",
        "protocol_commit_before_scoring": "bdfc746ff492576fe2deac27627f012c01e64e0a",
        "baseline": baseline,
        "candidate_counts": {"univariate": len(unis), "two_condition": len(pairs), "ridge": len(scored_c), "hgbr": len(scored_d)},
        "family_best": [{"family": f, "validation_metrics": m} for f, _, m in family_best],
        "final_selector": final,
        "evidence_state": "PROMISING_RETROSPECTIVE_SELECTOR" if final and final.get("holdout_success") else "NO_RETROSPECTIVE_SELECTOR_PROMOTION",
        "real_orders": False,
        "live_ready": False,
        "limitations": [
            "Public M1 Bid/Ask proxy, not Blueberry historical ticks.",
            "Compact reply-linked management omits richer global/unlinked provider actions.",
            "2026 is a retrospective no-refit holdout, not pristine unseen evidence.",
            "M1 intrabar ambiguity is resolved adversely.",
            "Any positive selector remains hypothesis-generation only and requires prospective shadow evidence."
        ],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    # Compact candidate tables for audit.
    def flatten(rows, path):
        flat=[]
        for r in rows:
            q={k:v for k,v in r.items() if k not in {"a","b","columns","coef","val_metrics"}}
            if "a" in r: q["a_json"] = json.dumps(r["a"], default=str)
            if "b" in r: q["b_json"] = json.dumps(r["b"], default=str)
            if "val_metrics" in r:
                q.update({f"val_{k}":v for k,v in r["val_metrics"].items()})
            flat.append(q)
        pd.DataFrame(flat).to_csv(OUT_DIR/path,index=False)
    flatten(scored_a, "univariate_candidates.csv")
    flatten(scored_b, "pair_candidates.csv")
    flatten(scored_c, "ridge_candidates.csv")
    flatten(scored_d, "hgbr_candidates.csv")

    if final and hold_mask is not None:
        h = hold.copy(); h["selected"] = hold_mask.values
        h.to_csv(OUT_DIR / "holdout_scored.csv", index=False)
    print(json.dumps(summary, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
