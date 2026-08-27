from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import random
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

UTC = dt.timezone.utc
ROOT = Path(__file__).resolve().parent
DEFAULT_POLICY = ROOT / "v55_policy.json"


class CertificationError(RuntimeError):
    pass


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    policy = load_json(path)
    if policy.get("schema") != "XAUUSD_V55_FORENSIC_CERTIFICATION_POLICY_V1":
        raise CertificationError("Unexpected V5.5 policy schema")
    if policy.get("execution", {}).get("real_orders") is not False:
        raise CertificationError("V5.5 policy must remain read-only/no-real-orders")
    if policy.get("live_ready_state_exists") is not False:
        raise CertificationError("V5.5 must not define LIVE_READY")
    return policy


def parse_ts(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nat", "nan", "none"}:
        return None
    try:
        n = float(s)
        if math.isfinite(n):
            # MetaTrader time_msc, Unix milliseconds, or Unix seconds.
            if abs(n) > 1e12:
                n /= 1000.0
            elif abs(n) > 1e10:
                n /= 1000.0
            if 0 < n < 1e11:
                return dt.datetime.fromtimestamp(n, tz=UTC)
    except ValueError:
        pass
    s = s.replace("Z", "+00:00")
    try:
        out = dt.datetime.fromisoformat(s)
        if out.tzinfo is None:
            out = out.replace(tzinfo=UTC)
        return out.astimezone(UTC)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            out = dt.datetime.strptime(s, fmt)
            if out.tzinfo is None:
                out = out.replace(tzinfo=UTC)
            return out.astimezone(UTC)
        except ValueError:
            continue
    return None


def iso(ts: Optional[dt.datetime]) -> Optional[str]:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z") if ts else None


def _open_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def _choose_column(headers: Iterable[str], names: Iterable[str]) -> Optional[str]:
    lookup = {h.strip().lower(): h for h in headers}
    for name in names:
        if name.lower() in lookup:
            return lookup[name.lower()]
    return None


def _float(value: Any) -> Optional[float]:
    try:
        out = float(str(value).strip())
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


@dataclass
class TickFileAudit:
    path: str
    sha256: str
    rows: int
    min_timestamp_utc: Optional[str]
    max_timestamp_utc: Optional[str]
    duplicate_adjacent_rows: int
    nonmonotonic_timestamps: int
    invalid_timestamps: int
    nonpositive_bid_ask: int
    crossed_quotes: int
    zero_spread_quotes: int
    max_gap_seconds: Optional[float]
    status: str
    error: Optional[str] = None


def audit_tick_file(path: Path) -> TickFileAudit:
    digest = sha256_file(path)
    rows = dup = nonmono = invalid_ts = nonpositive = crossed = zero_spread = 0
    min_ts = max_ts = prev_ts = None
    prev_key = None
    max_gap = None
    try:
        with _open_text(path) as f:
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
            bidcol = _choose_column(reader.fieldnames, ["bid", "Bid", "BID"])
            askcol = _choose_column(reader.fieldnames, ["ask", "Ask", "ASK"])
            if not tcol or not bidcol or not askcol:
                raise CertificationError(f"Missing timestamp/bid/ask columns: {reader.fieldnames}")
            for row in reader:
                rows += 1
                ts = parse_ts(row.get(tcol))
                bid = _float(row.get(bidcol))
                ask = _float(row.get(askcol))
                if ts is None:
                    invalid_ts += 1
                else:
                    min_ts = ts if min_ts is None or ts < min_ts else min_ts
                    max_ts = ts if max_ts is None or ts > max_ts else max_ts
                    if prev_ts is not None:
                        if ts < prev_ts:
                            nonmono += 1
                        gap = (ts - prev_ts).total_seconds()
                        if gap >= 0:
                            max_gap = gap if max_gap is None else max(max_gap, gap)
                    prev_ts = ts
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    nonpositive += 1
                elif ask < bid:
                    crossed += 1
                elif ask == bid:
                    zero_spread += 1
                key = (iso(ts), bid, ask)
                if prev_key is not None and key == prev_key:
                    dup += 1
                prev_key = key
    except Exception as exc:
        return TickFileAudit(str(path), digest, rows, iso(min_ts), iso(max_ts), dup, nonmono,
                             invalid_ts, nonpositive, crossed, zero_spread, max_gap, "FAIL", str(exc))
    fatal = invalid_ts + nonmono + nonpositive + crossed
    status = "PASS" if rows > 0 and fatal == 0 else "FAIL"
    return TickFileAudit(str(path), digest, rows, iso(min_ts), iso(max_ts), dup, nonmono,
                         invalid_ts, nonpositive, crossed, zero_spread, max_gap, status)


def discover_tick_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    out = []
    for p in root.rglob("*"):
        low = p.name.lower()
        if p.is_file() and (low.endswith(".csv") or low.endswith(".csv.gz")):
            out.append(p)
    return sorted(out)


def certify_ticks(tick_root: Path, out_dir: Path) -> dict[str, Any]:
    files = discover_tick_files(tick_root)
    audits = [audit_tick_file(p) for p in files]
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "V55_TICK_FILE_CERTIFICATION.csv"
    fields = list(TickFileAudit.__dataclass_fields__)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for a in audits:
            w.writerow(a.__dict__)
    mins = [parse_ts(a.min_timestamp_utc) for a in audits if a.min_timestamp_utc]
    maxs = [parse_ts(a.max_timestamp_utc) for a in audits if a.max_timestamp_utc]
    tick_days = sorted({parse_ts(a.min_timestamp_utc).date().isoformat() for a in audits if a.min_timestamp_utc})
    report = {
        "schema": "V55_TICK_CERTIFICATION_V1",
        "files": len(audits),
        "rows": sum(a.rows for a in audits),
        "failed_files": sum(a.status != "PASS" for a in audits),
        "global_min_timestamp_utc": iso(min(mins)) if mins else None,
        "global_max_timestamp_utc": iso(max(maxs)) if maxs else None,
        "tick_days_with_files": tick_days,
        "duplicate_adjacent_rows": sum(a.duplicate_adjacent_rows for a in audits),
        "nonmonotonic_timestamps": sum(a.nonmonotonic_timestamps for a in audits),
        "invalid_timestamps": sum(a.invalid_timestamps for a in audits),
        "nonpositive_bid_ask": sum(a.nonpositive_bid_ask for a in audits),
        "crossed_quotes": sum(a.crossed_quotes for a in audits),
        "zero_spread_quotes": sum(a.zero_spread_quotes for a in audits),
        "status": "PASS" if audits and all(a.status == "PASS" for a in audits) else "FAIL",
        "important": "Large time gaps are reported but are not automatically corruption because metals sessions close.",
        "file_ledger": str(csv_path),
    }
    (out_dir / "V55_TICK_CERTIFICATION.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _iter_csv(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def build_coverage_ledger(signals_csv: Path, tick_cert_json: Path, out_csv: Path) -> dict[str, Any]:
    cert = load_json(tick_cert_json)
    min_ts = parse_ts(cert.get("global_min_timestamp_utc"))
    max_ts = parse_ts(cert.get("global_max_timestamp_utc"))
    tick_days = set(cert.get("tick_days_with_files") or [])
    rows = list(_iter_csv(signals_csv))
    if not rows:
        raise CertificationError("Signals CSV is empty")
    headers = rows[0].keys()
    tcol = _choose_column(headers, ["effective_utc", "effective_ts", "datetime_iso", "timestamp", "message_datetime", "datetime"])
    idcol = _choose_column(headers, ["setup_uid", "signal_id", "message_id", "id", "instruction_id"])
    if not tcol:
        raise CertificationError(f"Cannot find signal timestamp column in {list(headers)}")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    covered = uncovered = invalid = 0
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source_row", "signal_id", "signal_timestamp_utc", "coverage_status", "reason"])
        w.writeheader()
        for i, row in enumerate(rows, start=1):
            ts = parse_ts(row.get(tcol))
            sid = row.get(idcol) if idcol else str(i)
            if ts is None:
                status, reason = "UNCOVERED", "INVALID_SIGNAL_TIMESTAMP"
                invalid += 1
                uncovered += 1
            elif not min_ts or not max_ts or ts < min_ts or ts > max_ts:
                status, reason = "UNCOVERED", "OUTSIDE_CERTIFIED_GLOBAL_TICK_RANGE"
                uncovered += 1
            elif ts.date().isoformat() not in tick_days:
                status, reason = "UNCOVERED", "NO_CERTIFIED_TICK_FILE_FOR_UTC_DAY"
                uncovered += 1
            else:
                status, reason = "COVERED_DAY_AVAILABLE", "REQUIRES_CAUSAL_NEXT_TICK_LOOKUP_DURING_REPLAY"
                covered += 1
            w.writerow({"source_row": i, "signal_id": sid, "signal_timestamp_utc": iso(ts), "coverage_status": status, "reason": reason})
    result = {
        "schema": "V55_SIGNAL_TICK_COVERAGE_V1",
        "signals": len(rows),
        "covered_day_available": covered,
        "uncovered": uncovered,
        "invalid_timestamp": invalid,
        "full_history_claim_authorized": uncovered == 0 and covered == len(rows),
        "coverage_ledger": str(out_csv),
        "note": "COVERED_DAY_AVAILABLE does not prove fillability; replay must still find a causal executable bid/ask tick after each event.",
    }
    return result


def certify_parent(parent_zip: Path, provider_dir: Optional[Path], policy: dict[str, Any]) -> dict[str, Any]:
    expected_parent = policy["parent_release"]["sha256"]
    actual_parent = sha256_file(parent_zip)
    artifacts = {}
    plan_ok = True
    if provider_dir:
        for name, expected in policy["frozen_provider_plan"]["artifact_sha256"].items():
            p = provider_dir / name
            actual = sha256_file(p) if p.exists() else None
            artifacts[name] = {"expected": expected, "actual": actual, "match": actual == expected}
            plan_ok = plan_ok and actual == expected
    return {
        "schema": "V55_PARENT_INTEGRITY_V1",
        "parent_expected_sha256": expected_parent,
        "parent_actual_sha256": actual_parent,
        "parent_match": actual_parent == expected_parent,
        "provider_plan": artifacts,
        "provider_plan_match": plan_ok if provider_dir else None,
        "status": "PASS" if actual_parent == expected_parent and (provider_dir is None or plan_ok) else "FAIL",
    }


def certify_broker(account_json: Path, symbol_json: Path, probe_json: Optional[Path], policy: dict[str, Any]) -> dict[str, Any]:
    account = load_json(account_json)
    symbol = load_json(symbol_json)
    probe = load_json(probe_json) if probe_json and probe_json.exists() else None
    exp = policy["broker_evidence"]
    def get(obj: dict[str, Any], *names: str):
        for n in names:
            if n in obj:
                return obj[n]
        return None
    checks = {
        "currency": get(account, "currency") == exp["expected_account_currency"],
        "leverage": _float(get(account, "leverage")) == float(exp["expected_leverage"]),
        "symbol": get(symbol, "name", "symbol") == exp["expected_symbol"],
        "contract_size": _float(get(symbol, "trade_contract_size", "contract_size")) == float(exp["expected_contract_size"]),
        "volume_min": _float(get(symbol, "volume_min")) == float(exp["expected_volume_min"]),
        "volume_step": _float(get(symbol, "volume_step")) == float(exp["expected_volume_step"]),
    }
    blockers = []
    if not all(checks.values()):
        blockers.append("BROKER_IDENTITY_OR_CONTRACT_SPEC_MISMATCH")
    tick_value = _float(get(symbol, "trade_tick_value", "tick_value"))
    margin_initial = _float(get(symbol, "margin_initial"))
    if tick_value in (None, 0.0):
        blockers.append("STATIC_TICK_VALUE_UNUSABLE_REQUIRE_CALC_PROFIT_OR_VERIFIED_FORMULA")
    if margin_initial in (None, 0.0):
        blockers.append("STATIC_MARGIN_INITIAL_UNUSABLE_REQUIRE_CALIBRATED_MARGIN_PROBE")
    if probe is None:
        blockers.append("CURRENT_MT5_READ_ONLY_PROBE_MISSING")
    else:
        if probe.get("order_send_called") is not False:
            blockers.append("PROBE_SAFETY_ATTESTATION_INVALID")
        if not probe.get("order_calc_profit"):
            blockers.append("ORDER_CALC_PROFIT_PROBE_MISSING")
        if not probe.get("order_calc_margin"):
            blockers.append("ORDER_CALC_MARGIN_PROBE_MISSING")
    blockers += [
        "HISTORICAL_COMMISSION_NOT_ACCOUNT_EVIDENCED",
        "HISTORICAL_SWAP_NOT_CAUSALLY_EVIDENCED",
        "HISTORICAL_MARGIN_NOT_PROVEN_BY_CURRENT_ENVIRONMENT_PROBE",
    ]
    return {
        "schema": "V55_BROKER_CERTIFICATION_V1",
        "checks": checks,
        "account_info_sha256": sha256_file(account_json),
        "symbol_info_sha256": sha256_file(symbol_json),
        "probe_sha256": sha256_file(probe_json) if probe_json and probe_json.exists() else None,
        "blockers": sorted(set(blockers)),
        "status": "PASS_IDENTITY_ONLY_COSTS_STILL_BLOCKED" if all(checks.values()) else "FAIL",
        "important": "A current order_calc_margin/order_calc_profit probe calibrates the current environment only; it does not prove historical broker economics.",
    }


def certify_semantics(labels_csv: Path, predictions_csv: Path, policy: dict[str, Any]) -> dict[str, Any]:
    labels = {str(r.get("message_id", "")).strip(): r for r in _iter_csv(labels_csv) if str(r.get("message_id", "")).strip()}
    preds = {str(r.get("message_id", "")).strip(): r for r in _iter_csv(predictions_csv) if str(r.get("message_id", "")).strip()}
    compared = false_exec = action_mismatch = scope_mismatch = missing = 0
    details = []
    nonexec_labels = {"NO_EXECUTION", "AMBIGUOUS", "IGNORE", "FAIL_CLOSED"}
    for mid, label in labels.items():
        pred = preds.get(mid)
        if pred is None:
            missing += 1
            continue
        compared += 1
        la = str(label.get("expected_action", "")).strip().upper()
        ls = str(label.get("expected_scope", "")).strip().upper()
        pa = str(pred.get("predicted_action", pred.get("action", ""))).strip().upper()
        ps = str(pred.get("predicted_scope", pred.get("scope", ""))).strip().upper()
        executable = str(pred.get("executable", "true")).strip().lower() in {"1", "true", "yes", "y"}
        fe = la in nonexec_labels and executable and pa not in nonexec_labels
        am = bool(la and pa and la != pa)
        sm = bool(ls and ps and ls != ps)
        false_exec += int(fe)
        action_mismatch += int(am)
        scope_mismatch += int(sm)
        if fe or am or sm:
            details.append({"message_id": mid, "expected_action": la, "predicted_action": pa,
                            "expected_scope": ls, "predicted_scope": ps, "false_execution": fe})
    gate = policy["semantic_certification"]
    blockers = []
    if len(labels) < gate["minimum_human_labeled_messages"]:
        blockers.append("INSUFFICIENT_HUMAN_LABELED_MESSAGES")
    if false_exec > gate["maximum_false_execution_count"]:
        blockers.append("FALSE_EXECUTION_DETECTED")
    if missing:
        blockers.append("MISSING_PREDICTIONS_FOR_LABELED_MESSAGES")
    return {
        "schema": "V55_SEMANTIC_CERTIFICATION_V1",
        "human_labels": len(labels),
        "predictions": len(preds),
        "compared": compared,
        "missing_predictions": missing,
        "false_execution_count": false_exec,
        "action_mismatch_count": action_mismatch,
        "scope_mismatch_count": scope_mismatch,
        "blockers": blockers,
        "mismatches": details[:500],
        "status": "PASS" if not blockers else "FAIL",
    }


class DurableState:
    """Transactional state + idempotency journal for future shadow/demo integration."""
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS kv_state(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS instruction_journal(
          idempotency_key TEXT PRIMARY KEY,
          payload_sha256 TEXT NOT NULL,
          committed_at_utc TEXT NOT NULL,
          resulting_state_sha256 TEXT NOT NULL
        );
        """)
        self.db.commit()

    def snapshot(self) -> dict[str, Any]:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key,value_json FROM kv_state ORDER BY key")}

    def apply_atomic(self, idempotency_key: str, payload: dict[str, Any], mutations: dict[str, Any], crash_before_commit: bool = False) -> str:
        payload_hash = sha256_bytes(canonical_json(payload).encode())
        with self.db:
            old = self.db.execute("SELECT payload_sha256 FROM instruction_journal WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if old:
                if old[0] != payload_hash:
                    raise CertificationError("FAIL_CLOSED_IDEMPOTENCY_COLLISION")
                return "DUPLICATE_NOOP"
            for key, value in mutations.items():
                self.db.execute("INSERT INTO kv_state(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                                (key, canonical_json(value)))
            resulting_hash = sha256_bytes(canonical_json(self.snapshot()).encode())
            self.db.execute("INSERT INTO instruction_journal VALUES(?,?,?,?)",
                            (idempotency_key, payload_hash, iso(dt.datetime.now(UTC)), resulting_hash))
            if crash_before_commit:
                raise RuntimeError("INJECTED_CRASH_BEFORE_COMMIT")
        return "COMMITTED"

    def close(self):
        self.db.close()


class ProspectiveLedger:
    def __init__(self, path: Path, policy_hash: str):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          event_time_utc TEXT NOT NULL,
          event_type TEXT NOT NULL,
          primary_setup INTEGER NOT NULL,
          integrity_break INTEGER NOT NULL,
          reconciliation_failure INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          policy_sha256 TEXT NOT NULL,
          previous_hash TEXT NOT NULL,
          event_hash TEXT NOT NULL UNIQUE)""")
        self.db.commit()
        self.policy_hash = policy_hash

    def append(self, event_time_utc: str, event_type: str, payload: dict[str, Any], primary_setup=False,
               integrity_break=False, reconciliation_failure=False) -> str:
        ts = parse_ts(event_time_utc)
        if ts is None:
            raise CertificationError("Invalid prospective event timestamp")
        with self.db:
            last = self.db.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last[0] if last else "GENESIS"
            body = {"event_time_utc": iso(ts), "event_type": event_type, "primary_setup": bool(primary_setup),
                    "integrity_break": bool(integrity_break), "reconciliation_failure": bool(reconciliation_failure),
                    "payload": payload, "policy_sha256": self.policy_hash, "previous_hash": prev}
            h = sha256_bytes(canonical_json(body).encode())
            self.db.execute("INSERT INTO events(event_time_utc,event_type,primary_setup,integrity_break,reconciliation_failure,payload_json,policy_sha256,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                            (iso(ts), event_type, int(primary_setup), int(integrity_break), int(reconciliation_failure), canonical_json(payload), self.policy_hash, prev, h))
        return h

    def verify(self) -> bool:
        prev = "GENESIS"
        for row in self.db.execute("SELECT event_time_utc,event_type,primary_setup,integrity_break,reconciliation_failure,payload_json,policy_sha256,previous_hash,event_hash FROM events ORDER BY seq"):
            ts, et, primary, integ, recon, payload_json, policy_hash, prev_stored, h = row
            if prev_stored != prev or policy_hash != self.policy_hash:
                return False
            body = {"event_time_utc": ts, "event_type": et, "primary_setup": bool(primary), "integrity_break": bool(integ),
                    "reconciliation_failure": bool(recon), "payload": json.loads(payload_json), "policy_sha256": policy_hash, "previous_hash": prev}
            if sha256_bytes(canonical_json(body).encode()) != h:
                return False
            prev = h
        return True

    def status(self, policy: dict[str, Any]) -> dict[str, Any]:
        rows = list(self.db.execute("SELECT event_time_utc,primary_setup,integrity_break,reconciliation_failure FROM events ORDER BY seq"))
        times = [parse_ts(r[0]) for r in rows]
        times = [x for x in times if x]
        primary = sum(r[1] for r in rows)
        active_days = len({x.date() for x in times})
        calendar_days = ((max(times).date() - min(times).date()).days + 1) if times else 0
        integrity = sum(r[2] for r in rows)
        recon = sum(r[3] for r in rows)
        gate = policy["prospective_gate"]
        passed = self.verify() and primary >= gate["minimum_primary_setups"] and active_days >= gate["minimum_active_days"] and calendar_days >= gate["minimum_calendar_days"] and integrity <= gate["maximum_integrity_breaks"] and recon <= gate["maximum_reconciliation_failures"]
        return {"hash_chain_valid": self.verify(), "primary_setups": primary, "active_days": active_days,
                "calendar_days": calendar_days, "integrity_breaks": integrity, "reconciliation_failures": recon,
                "prospective_gate_passed": passed,
                "release_state": "PROSPECTIVE_SHADOW_CERTIFIED" if passed else "RESEARCH_ENGINE_CERTIFICATION_INCOMPLETE"}

    def close(self):
        self.db.close()


def day_block_bootstrap(trades_csv: Path, iterations: int = 10000, seed: int = 55,
                        starting_balance: float = 100.0, shutdown_drawdown: float = 0.15) -> dict[str, Any]:
    rows = list(_iter_csv(trades_csv))
    if not rows:
        raise CertificationError("No trade rows")
    headers = rows[0].keys()
    pnl_col = _choose_column(headers, ["net_pnl_sgd", "pnl_sgd", "net_pnl", "pnl"])
    tcol = _choose_column(headers, ["exit_time_utc", "exit_timestamp", "closed_at", "timestamp", "datetime"])
    if not pnl_col or not tcol:
        raise CertificationError("Need net PnL and exit timestamp columns")
    days: dict[str, float] = {}
    for r in rows:
        ts = parse_ts(r.get(tcol))
        pnl = _float(r.get(pnl_col))
        if ts is None or pnl is None:
            continue
        days[ts.date().isoformat()] = days.get(ts.date().isoformat(), 0.0) + pnl
    blocks = list(days.values())
    if not blocks:
        raise CertificationError("No valid day blocks")
    rng = random.Random(seed)
    finals, maxdds = [], []
    shutdowns = ruins = 0
    for _ in range(iterations):
        equity = high = starting_balance
        maxdd = 0.0
        shut = False
        for _j in range(len(blocks)):
            equity += rng.choice(blocks)
            high = max(high, equity)
            dd = (high - equity) / high if high > 0 else 1.0
            maxdd = max(maxdd, dd)
            if dd >= shutdown_drawdown:
                shut = True
                break
            if equity <= 0:
                ruins += 1
                break
        shutdowns += int(shut)
        finals.append(equity)
        maxdds.append(maxdd)
    finals_sorted = sorted(finals)
    return {
        "schema": "V55_DAY_BLOCK_BOOTSTRAP_V1",
        "diagnostic_only": True,
        "iid_ticket_shuffle_used": False,
        "day_blocks": len(blocks),
        "iterations": iterations,
        "seed": seed,
        "starting_balance": starting_balance,
        "shutdown_drawdown_fraction": shutdown_drawdown,
        "shutdown_probability": shutdowns / iterations,
        "ruin_probability": ruins / iterations,
        "median_final_equity": statistics.median(finals),
        "p05_final_equity": finals_sorted[max(0, int(iterations * 0.05) - 1)],
        "median_max_drawdown_fraction": statistics.median(maxdds),
        "note": "Resamples whole realized days to preserve within-day clustering. It is diagnostic and not a profitability guarantee."
    }


def write_report(obj: dict[str, Any], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="XAUUSD V5.5 forensic certification overlay (read-only)")
    ap.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify-parent")
    p.add_argument("--parent-zip", type=Path, required=True)
    p.add_argument("--provider-dir", type=Path)
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("ticks")
    p.add_argument("--ticks", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)

    p = sub.add_parser("coverage")
    p.add_argument("--signals", type=Path, required=True)
    p.add_argument("--tick-cert", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("broker")
    p.add_argument("--account-info", type=Path, required=True)
    p.add_argument("--symbol-info", type=Path, required=True)
    p.add_argument("--probe", type=Path)
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("semantics")
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--predictions", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("bootstrap")
    p.add_argument("--trades", type=Path, required=True)
    p.add_argument("--iterations", type=int, default=10000)
    p.add_argument("--output", type=Path, required=True)

    p = sub.add_parser("prospective-status")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)

    args = ap.parse_args(argv)
    policy = load_policy(args.policy)
    if args.cmd == "verify-parent":
        report = certify_parent(args.parent_zip, args.provider_dir, policy)
    elif args.cmd == "ticks":
        report = certify_ticks(args.ticks, args.output_dir)
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "PASS" else 2
    elif args.cmd == "coverage":
        report = build_coverage_ledger(args.signals, args.tick_cert, args.output)
        sidecar = args.output.with_suffix(".summary.json")
        write_report(report, sidecar)
        print(json.dumps(report, indent=2))
        return 0
    elif args.cmd == "broker":
        report = certify_broker(args.account_info, args.symbol_info, args.probe, policy)
    elif args.cmd == "semantics":
        report = certify_semantics(args.labels, args.predictions, policy)
    elif args.cmd == "bootstrap":
        report = day_block_bootstrap(args.trades, args.iterations,
                                     starting_balance=policy["small_account_projection"]["starting_balance_sgd"],
                                     shutdown_drawdown=policy["small_account_projection"]["hard_drawdown_fraction"])
    elif args.cmd == "prospective-status":
        ledger = ProspectiveLedger(args.db, sha256_file(args.policy))
        try:
            report = ledger.status(policy)
        finally:
            ledger.close()
    else:
        raise AssertionError(args.cmd)
    write_report(report, args.output)
    print(json.dumps(report, indent=2))
    return 0 if report.get("status", "PASS") != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
