from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import random
import sqlite3
import statistics
from pathlib import Path
from typing import Any, Iterable, Optional

from v55_certification import CertificationError, canonical_json, parse_ts, sha256_bytes, sha256_file, _choose_column, _float

UTC = dt.timezone.utc
NONEXEC_ACTIONS = {"NO_EXECUTION", "AMBIGUOUS", "IGNORE", "FAIL_CLOSED", "FAIL_CLOSED_NO_EXECUTION"}


def _iter_csv(path: Path):
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def _open_csv(path: Path):
    import gzip
    if path.name.lower().endswith(".csv.gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def _discover_csv(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file() and (p.name.lower().endswith(".csv") or p.name.lower().endswith(".csv.gz")))


def certify_tick_inventory_v2(tick_root: Path, out_dir: Path) -> dict[str, Any]:
    """Deep tick inventory that records every UTC day actually observed.

    V1 used each file's first timestamp as its day marker; that is insufficient for
    files spanning multiple UTC days. V2 derives coverage from every valid row.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    files = _discover_csv(tick_root)
    ledger: list[dict[str, Any]] = []
    all_days: set[str] = set()
    totals = {"rows": 0, "invalid_timestamps": 0, "nonmonotonic_timestamps": 0,
              "nonpositive_bid_ask": 0, "crossed_quotes": 0, "duplicate_exact_adjacent": 0}
    global_min = global_max = None

    for path in files:
        rows = invalid = nonmono = nonpositive = crossed = dup = 0
        min_ts = max_ts = prev_ts = None
        prev_key = None
        file_days: set[str] = set()
        error = None
        try:
            with _open_csv(path) as f:
                sample = f.read(8192)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                except csv.Error:
                    dialect = csv.excel
                reader = csv.DictReader(f, dialect=dialect)
                if not reader.fieldnames:
                    raise CertificationError("No CSV header")
                tcol = _choose_column(reader.fieldnames, ["time_msc", "timestamp", "datetime", "time", "datetime_iso"])
                bidcol = _choose_column(reader.fieldnames, ["bid"])
                askcol = _choose_column(reader.fieldnames, ["ask"])
                if not tcol or not bidcol or not askcol:
                    raise CertificationError(f"Missing timestamp/bid/ask columns: {reader.fieldnames}")
                for row in reader:
                    rows += 1
                    ts = parse_ts(row.get(tcol))
                    bid = _float(row.get(bidcol)); ask = _float(row.get(askcol))
                    if ts is None:
                        invalid += 1
                    else:
                        file_days.add(ts.date().isoformat())
                        min_ts = ts if min_ts is None or ts < min_ts else min_ts
                        max_ts = ts if max_ts is None or ts > max_ts else max_ts
                        if prev_ts is not None and ts < prev_ts:
                            nonmono += 1
                        prev_ts = ts
                    if bid is None or ask is None or bid <= 0 or ask <= 0:
                        nonpositive += 1
                    elif ask < bid:
                        crossed += 1
                    key = (ts.isoformat() if ts else None, bid, ask)
                    if prev_key == key:
                        dup += 1
                    prev_key = key
        except Exception as exc:
            error = str(exc)
        fatal = invalid + nonmono + nonpositive + crossed
        status = "PASS" if rows > 0 and fatal == 0 and error is None else "FAIL"
        all_days.update(file_days)
        totals["rows"] += rows
        totals["invalid_timestamps"] += invalid
        totals["nonmonotonic_timestamps"] += nonmono
        totals["nonpositive_bid_ask"] += nonpositive
        totals["crossed_quotes"] += crossed
        totals["duplicate_exact_adjacent"] += dup
        if min_ts is not None:
            global_min = min_ts if global_min is None or min_ts < global_min else global_min
        if max_ts is not None:
            global_max = max_ts if global_max is None or max_ts > global_max else global_max
        ledger.append({
            "path": str(path), "sha256": sha256_file(path), "rows": rows,
            "min_timestamp_utc": min_ts.isoformat().replace("+00:00", "Z") if min_ts else None,
            "max_timestamp_utc": max_ts.isoformat().replace("+00:00", "Z") if max_ts else None,
            "utc_days_observed": sorted(file_days), "invalid_timestamps": invalid,
            "nonmonotonic_timestamps": nonmono, "nonpositive_bid_ask": nonpositive,
            "crossed_quotes": crossed, "duplicate_exact_adjacent": dup, "status": status, "error": error,
        })

    manifest_material = [{"path": x["path"], "sha256": x["sha256"], "rows": x["rows"]} for x in ledger]
    report = {
        "schema": "V55_TICK_INVENTORY_V2",
        "files": len(ledger), **totals,
        "failed_files": sum(x["status"] != "PASS" for x in ledger),
        "global_min_timestamp_utc": global_min.isoformat().replace("+00:00", "Z") if global_min else None,
        "global_max_timestamp_utc": global_max.isoformat().replace("+00:00", "Z") if global_max else None,
        "all_utc_days_observed": sorted(all_days),
        "manifest_sha256": sha256_bytes(canonical_json(manifest_material).encode()),
        "status": "PASS" if ledger and all(x["status"] == "PASS" for x in ledger) else "FAIL",
        "file_ledger": ledger,
        "note": "Coverage days are derived from every valid tick row, not from filenames or only each file's first timestamp.",
    }
    (out_dir / "V55_TICK_INVENTORY_V2.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def build_coverage_ledger_v2(signals_csv: Path, tick_inventory_json: Path, out_csv: Path) -> dict[str, Any]:
    inv = json.loads(tick_inventory_json.read_text(encoding="utf-8"))
    days = set(inv.get("all_utc_days_observed") or [])
    rows = list(_iter_csv(signals_csv))
    if not rows:
        raise CertificationError("Signals CSV is empty")
    headers = rows[0].keys()
    tcol = _choose_column(headers, ["effective_utc", "effective_ts", "datetime_iso", "timestamp", "message_datetime", "datetime"])
    idcol = _choose_column(headers, ["setup_uid", "signal_id", "message_id", "id", "instruction_id"])
    if not tcol:
        raise CertificationError("Signal timestamp column missing")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    covered = uncovered = invalid = 0
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source_row", "signal_id", "signal_timestamp_utc", "coverage_status", "reason"])
        w.writeheader()
        for i, row in enumerate(rows, 1):
            ts = parse_ts(row.get(tcol)); sid = row.get(idcol) if idcol else str(i)
            if ts is None:
                status, reason = "UNCOVERED", "INVALID_SIGNAL_TIMESTAMP"; invalid += 1; uncovered += 1
            elif ts.date().isoformat() not in days:
                status, reason = "UNCOVERED", "NO_CERTIFIED_TICK_ROW_ON_UTC_DAY"; uncovered += 1
            else:
                status, reason = "COVERED_DAY_AVAILABLE", "STILL_REQUIRES_CAUSAL_NEXT_EXECUTABLE_QUOTE"; covered += 1
            w.writerow({"source_row": i, "signal_id": sid,
                        "signal_timestamp_utc": ts.isoformat().replace("+00:00", "Z") if ts else None,
                        "coverage_status": status, "reason": reason})
    return {"schema": "V55_SIGNAL_TICK_COVERAGE_V2", "signals": len(rows),
            "covered_day_available": covered, "uncovered": uncovered, "invalid_timestamp": invalid,
            "full_history_claim_authorized": uncovered == 0 and covered == len(rows),
            "coverage_ledger": str(out_csv)}


def certify_semantics_strict(labels_csv: Path, predictions_csv: Path, policy: dict[str, Any]) -> dict[str, Any]:
    labels = {str(r.get("message_id", "")).strip(): r for r in _iter_csv(labels_csv) if str(r.get("message_id", "")).strip()}
    preds = {str(r.get("message_id", "")).strip(): r for r in _iter_csv(predictions_csv) if str(r.get("message_id", "")).strip()}
    false_exec = action_mismatch = scope_mismatch = missing = missed_actionable = 0
    mismatches = []
    for mid, label in labels.items():
        pred = preds.get(mid)
        if pred is None:
            missing += 1; continue
        la = str(label.get("expected_action", "")).strip().upper()
        ls = str(label.get("expected_scope", "")).strip().upper()
        pa = str(pred.get("predicted_action", pred.get("action", ""))).strip().upper()
        ps = str(pred.get("predicted_scope", pred.get("scope", ""))).strip().upper()
        executable = str(pred.get("executable", "true")).strip().lower() in {"1", "true", "yes", "y"}
        label_nonexec = la in NONEXEC_ACTIONS
        pred_nonexec = pa in NONEXEC_ACTIONS or not executable
        fe = label_nonexec and not pred_nonexec
        missed = (not label_nonexec) and pred_nonexec
        am = la != pa
        sm = bool(ls) and ls != ps
        false_exec += int(fe); missed_actionable += int(missed); action_mismatch += int(am); scope_mismatch += int(sm)
        if fe or missed or am or sm:
            mismatches.append({"message_id": mid, "expected_action": la, "predicted_action": pa,
                               "expected_scope": ls, "predicted_scope": ps, "false_execution": fe,
                               "missed_actionable": missed})
    gate = policy["semantic_certification"]
    blockers = []
    if len(labels) < int(gate.get("minimum_human_labeled_messages", 200)):
        blockers.append("INSUFFICIENT_HUMAN_LABELED_MESSAGES")
    if false_exec > int(gate.get("maximum_false_execution_count", 0)):
        blockers.append("FALSE_EXECUTION_DETECTED")
    if missed_actionable > int(gate.get("maximum_missed_actionable_count", 0)):
        blockers.append("MISSED_ACTIONABLE_COMMAND_DETECTED")
    if action_mismatch > int(gate.get("maximum_action_mismatch_count", 0)):
        blockers.append("ACTION_MISMATCH_DETECTED")
    if scope_mismatch > int(gate.get("maximum_scope_mismatch_count", 0)):
        blockers.append("SCOPE_MISMATCH_DETECTED")
    if missing:
        blockers.append("MISSING_PREDICTIONS_FOR_LABELED_MESSAGES")
    return {"schema": "V55_SEMANTIC_CERTIFICATION_V2", "human_labels": len(labels), "predictions": len(preds),
            "missing_predictions": missing, "false_execution_count": false_exec,
            "missed_actionable_count": missed_actionable, "action_mismatch_count": action_mismatch,
            "scope_mismatch_count": scope_mismatch, "blockers": blockers,
            "mismatches": mismatches[:1000], "status": "PASS" if not blockers else "FAIL"}


class DurableStateV2:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS kv_state(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS instruction_journal(
          idempotency_key TEXT PRIMARY KEY,
          payload_sha256 TEXT NOT NULL,
          committed_at_utc TEXT NOT NULL,
          resulting_state_sha256 TEXT NOT NULL);
        """)
        self.db.commit()

    def snapshot(self) -> dict[str, Any]:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key,value_json FROM kv_state ORDER BY key")}

    def apply_atomic(self, key: str, payload: dict[str, Any], mutations: dict[str, Any], crash_phase: Optional[str] = None) -> str:
        ph = sha256_bytes(canonical_json(payload).encode())
        with self.db:
            old = self.db.execute("SELECT payload_sha256 FROM instruction_journal WHERE idempotency_key=?", (key,)).fetchone()
            if old:
                if old[0] != ph:
                    raise CertificationError("FAIL_CLOSED_IDEMPOTENCY_COLLISION")
                return "DUPLICATE_NOOP"
            for k, v in mutations.items():
                self.db.execute("INSERT INTO kv_state(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (k, canonical_json(v)))
            if crash_phase == "after_mutations":
                raise RuntimeError("INJECTED_CRASH_AFTER_MUTATIONS")
            state_hash = sha256_bytes(canonical_json(self.snapshot()).encode())
            self.db.execute("INSERT INTO instruction_journal VALUES(?,?,?,?)", (key, ph, dt.datetime.now(UTC).isoformat(), state_hash))
            if crash_phase == "after_journal":
                raise RuntimeError("INJECTED_CRASH_AFTER_JOURNAL")
        return "COMMITTED"

    def close(self):
        self.db.close()


class ProspectiveLedgerV2:
    def __init__(self, path: Path, policy: dict[str, Any], policy_hash: str):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.policy = policy; self.policy_hash = policy_hash
        self.boundary = parse_ts(policy["prospective_boundary_utc"])
        if self.boundary is None:
            raise CertificationError("Invalid prospective boundary")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,event_time_utc TEXT NOT NULL,event_type TEXT NOT NULL,
          primary_setup INTEGER NOT NULL,integrity_break INTEGER NOT NULL,reconciliation_failure INTEGER NOT NULL,
          payload_json TEXT NOT NULL,policy_sha256 TEXT NOT NULL,previous_hash TEXT NOT NULL,event_hash TEXT NOT NULL UNIQUE);
        """)
        for k, v in (("policy_sha256", policy_hash), ("prospective_boundary_utc", self.boundary.isoformat())):
            old = self.db.execute("SELECT value FROM metadata WHERE key=?", (k,)).fetchone()
            if old and old[0] != v:
                raise CertificationError("PROSPECTIVE_LEDGER_METADATA_MISMATCH")
            self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (k, v))
        self.db.commit()

    def append(self, event_time_utc: str, event_type: str, payload: dict[str, Any], *, primary_setup=False,
               integrity_break=False, reconciliation_failure=False) -> str:
        ts = parse_ts(event_time_utc)
        if ts is None:
            raise CertificationError("INVALID_PROSPECTIVE_TIMESTAMP")
        if ts < self.boundary:
            raise CertificationError("PREBOUNDARY_EVENT_REJECTED")
        last = self.db.execute("SELECT event_time_utc,event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        if last and ts < parse_ts(last[0]):
            raise CertificationError("OUT_OF_ORDER_PROSPECTIVE_EVENT_REJECTED")
        prev = last[1] if last else "GENESIS"
        body = {"event_time_utc": ts.isoformat().replace("+00:00", "Z"), "event_type": event_type,
                "primary_setup": bool(primary_setup), "integrity_break": bool(integrity_break),
                "reconciliation_failure": bool(reconciliation_failure), "payload": payload,
                "policy_sha256": self.policy_hash, "previous_hash": prev}
        h = sha256_bytes(canonical_json(body).encode())
        with self.db:
            self.db.execute("INSERT INTO events(event_time_utc,event_type,primary_setup,integrity_break,reconciliation_failure,payload_json,policy_sha256,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                            (body["event_time_utc"], event_type, int(primary_setup), int(integrity_break), int(reconciliation_failure),
                             canonical_json(payload), self.policy_hash, prev, h))
        return h

    def verify(self) -> bool:
        prev = "GENESIS"; last_ts = None
        for row in self.db.execute("SELECT event_time_utc,event_type,primary_setup,integrity_break,reconciliation_failure,payload_json,policy_sha256,previous_hash,event_hash FROM events ORDER BY seq"):
            ts_s, et, primary, integ, recon, payload_json, ph, prev_stored, h = row
            ts = parse_ts(ts_s)
            if ts is None or ts < self.boundary or (last_ts and ts < last_ts) or prev_stored != prev or ph != self.policy_hash:
                return False
            body = {"event_time_utc": ts_s, "event_type": et, "primary_setup": bool(primary),
                    "integrity_break": bool(integ), "reconciliation_failure": bool(recon),
                    "payload": json.loads(payload_json), "policy_sha256": ph, "previous_hash": prev}
            if sha256_bytes(canonical_json(body).encode()) != h:
                return False
            prev = h; last_ts = ts
        return True

    def status(self) -> dict[str, Any]:
        rows = list(self.db.execute("SELECT event_time_utc,primary_setup,integrity_break,reconciliation_failure FROM events ORDER BY seq"))
        primary_times = [parse_ts(r[0]) for r in rows if r[1]]
        primary_times = [x for x in primary_times if x]
        active_days = len({x.date() for x in primary_times})
        calendar_days = ((max(primary_times).date() - min(primary_times).date()).days + 1) if primary_times else 0
        primary = len(primary_times); integrity = sum(r[2] for r in rows); recon = sum(r[3] for r in rows)
        gate = self.policy["prospective_gate"]
        chain = self.verify()
        passed = (chain and primary >= gate["minimum_primary_setups"] and active_days >= gate["minimum_active_days"]
                  and calendar_days >= gate["minimum_calendar_days"] and integrity <= gate["maximum_integrity_breaks"]
                  and recon <= gate["maximum_reconciliation_failures"])
        return {"hash_chain_valid": chain, "primary_setups": primary, "primary_active_days": active_days,
                "primary_calendar_span_days": calendar_days, "integrity_breaks": integrity,
                "reconciliation_failures": recon, "prospective_gate_passed": passed,
                "release_state": "PROSPECTIVE_SHADOW_CERTIFIED" if passed else "RESEARCH_ENGINE_CERTIFICATION_INCOMPLETE"}

    def close(self):
        self.db.close()


def day_block_bootstrap_v2(trades_csv: Path, *, iterations: int = 10000, seed: int = 55,
                           starting_balance: float = 100.0, shutdown_drawdown: float = 0.15,
                           max_consecutive_losses: int = 3) -> dict[str, Any]:
    rows = list(_iter_csv(trades_csv))
    if not rows:
        raise CertificationError("No trade rows")
    headers = rows[0].keys()
    pnl_col = _choose_column(headers, ["net_pnl_sgd", "pnl_sgd", "net_pnl", "pnl"])
    tcol = _choose_column(headers, ["exit_time_utc", "exit_timestamp", "closed_at", "timestamp", "datetime"])
    if not pnl_col or not tcol:
        raise CertificationError("Need PnL and exit timestamp")
    parsed = []
    for r in rows:
        ts = parse_ts(r.get(tcol)); pnl = _float(r.get(pnl_col))
        if ts is not None and pnl is not None:
            parsed.append((ts, pnl))
    parsed.sort(key=lambda x: x[0])
    days: dict[str, list[float]] = {}
    for ts, pnl in parsed:
        days.setdefault(ts.date().isoformat(), []).append(pnl)
    blocks = list(days.values())
    if not blocks:
        raise CertificationError("No valid day blocks")
    rng = random.Random(seed)
    finals = []; maxdds = []; dd_shutdowns = loss_shutdowns = ruins = 0
    for _ in range(iterations):
        equity = high = starting_balance; maxdd = 0.0; loss_streak = 0; stopped = False
        for _j in range(len(blocks)):
            for pnl in rng.choice(blocks):
                equity += pnl
                if pnl < 0: loss_streak += 1
                elif pnl > 0: loss_streak = 0
                # pnl == 0 intentionally does not reset the loss streak.
                high = max(high, equity)
                dd = (high - equity) / high if high > 0 else 1.0
                maxdd = max(maxdd, dd)
                if equity <= 0:
                    ruins += 1; stopped = True; break
                if dd >= shutdown_drawdown:
                    dd_shutdowns += 1; stopped = True; break
                if loss_streak >= max_consecutive_losses:
                    loss_shutdowns += 1; stopped = True; break
            if stopped: break
        finals.append(equity); maxdds.append(maxdd)
    s = sorted(finals)
    return {"schema": "V55_DAY_BLOCK_BOOTSTRAP_V2", "diagnostic_only": True,
            "iid_ticket_shuffle_used": False, "within_day_trade_order_preserved": True,
            "day_blocks": len(blocks), "iterations": iterations, "seed": seed,
            "starting_balance": starting_balance, "shutdown_drawdown_fraction": shutdown_drawdown,
            "max_consecutive_losses": max_consecutive_losses,
            "drawdown_shutdown_probability": dd_shutdowns / iterations,
            "consecutive_loss_shutdown_probability": loss_shutdowns / iterations,
            "ruin_probability": ruins / iterations,
            "median_final_equity": statistics.median(finals),
            "p05_final_equity": s[max(0, int(iterations * 0.05) - 1)],
            "median_max_drawdown_fraction": statistics.median(maxdds)}


def limit_fill(side: str, limit_price: float, bid: float, ask: float) -> Optional[float]:
    side = side.upper()
    if side == "BUY": return ask if ask <= limit_price else None
    if side == "SELL": return bid if bid >= limit_price else None
    raise CertificationError("Unknown side")


def exit_fill(side: str, kind: str, level: float, bid: float, ask: float) -> Optional[float]:
    side = side.upper(); kind = kind.upper()
    if side == "BUY" and kind == "SL": return bid if bid <= level else None
    if side == "BUY" and kind == "TP": return bid if bid >= level else None
    if side == "SELL" and kind == "SL": return ask if ask >= level else None
    if side == "SELL" and kind == "TP": return ask if ask <= level else None
    raise CertificationError("Unknown side/kind")


def certify_reference_execution_microcases(policy: dict[str, Any]) -> dict[str, Any]:
    cases = {
        "buy_limit_uses_ask": limit_fill("BUY", 100.0, 99.7, 99.9) == 99.9,
        "sell_limit_uses_bid": limit_fill("SELL", 100.0, 100.2, 100.4) == 100.2,
        "buy_sl_uses_bid_and_gaps": exit_fill("BUY", "SL", 99.0, 98.6, 98.8) == 98.6,
        "sell_sl_uses_ask_and_gaps": exit_fill("SELL", "SL", 101.0, 101.2, 101.4) == 101.4,
        "buy_tp_uses_bid": exit_fill("BUY", "TP", 101.0, 101.2, 101.4) == 101.2,
        "sell_tp_uses_ask": exit_fill("SELL", "TP", 99.0, 98.7, 98.9) == 98.9,
        "same_timestamp_precedence_frozen": policy["execution"]["same_timestamp_precedence"] == [
            "BROKER_STOP_OUT", "SERVER_STOP_LOSS", "SERVER_TAKE_PROFIT", "PENDING_FILL", "PROVIDER_MANAGEMENT", "NEW_ENTRY_ARM"],
        "margin_call_not_forced_liquidation": policy["execution"]["margin_call_forced_liquidation"] is False,
        "stop_out_forced_liquidation": policy["execution"]["stop_out_forced_liquidation"] is True,
    }
    return {"schema": "V55_REFERENCE_EXECUTION_MICROCASES_V1", "cases": cases,
            "status": "PASS" if all(cases.values()) else "FAIL",
            "important": "This certifies the frozen V5.5 reference semantics only; it does not by itself prove the parent V5.4 engine uses identical code paths."}


def certify_dedicated_account_snapshot(probe_json: Path) -> dict[str, Any]:
    probe = json.loads(probe_json.read_text(encoding="utf-8"))
    pos = {k: float(v) for k, v in (probe.get("existing_positions_by_symbol_volume") or {}).items() if abs(float(v)) > 0}
    orders = {k: int(v) for k, v in (probe.get("existing_pending_orders_by_symbol_count") or {}).items() if int(v) > 0}
    blockers = []
    if pos: blockers.append("PREEXISTING_POSITION_EXPOSURE")
    if orders: blockers.append("PREEXISTING_PENDING_ORDER_EXPOSURE")
    if probe.get("order_send_called") is not False: blockers.append("READ_ONLY_ATTESTATION_INVALID")
    return {"schema": "V55_DEDICATED_ACCOUNT_SNAPSHOT_V1", "positions": pos, "pending_orders": orders,
            "blockers": blockers, "status": "PASS" if not blockers else "FAIL"}


def master_release_state(reports: Iterable[dict[str, Any]], prospective: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    reports = list(reports)
    blockers = []
    for r in reports:
        if r.get("status") == "FAIL": blockers.append(r.get("schema", "UNKNOWN_FAIL"))
        blockers.extend(r.get("blockers") or [])
    blockers = sorted(set(blockers))
    if prospective and prospective.get("prospective_gate_passed") and not blockers:
        state = "PROSPECTIVE_SHADOW_CERTIFIED"
    elif not blockers and reports:
        state = "HISTORICAL_REPLAY_CERTIFIED_RETROSPECTIVE_ONLY"
    else:
        state = "RESEARCH_ENGINE_CERTIFICATION_INCOMPLETE"
    return {"schema": "V55_MASTER_RELEASE_STATE_V1", "release_state": state,
            "live_ready": False, "real_orders_authorized": False, "blockers": blockers}
