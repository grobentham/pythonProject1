import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from v55_certification import CertificationError, load_policy, sha256_file
from v55_hardening import (
    DurableStateV2,
    ProspectiveLedgerV2,
    build_coverage_ledger_v2,
    certify_dedicated_account_snapshot,
    certify_reference_execution_microcases,
    certify_semantics_strict,
    certify_tick_inventory_v2,
    day_block_bootstrap_v2,
    master_release_state,
)

UTC = dt.timezone.utc


class V55HardeningTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy()
        self.policy_hash = sha256_file(Path(__file__).with_name("v55_policy.json"))

    def test_tick_inventory_records_every_day_inside_multiday_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / "ticks.csv"
            p.write_text(
                "time_msc,bid,ask\n"
                "1787183999000,3300.0,3300.1\n"
                "1787184001000,3300.1,3300.2\n",
                encoding="utf-8",
            )
            report = certify_tick_inventory_v2(p, root / "out")
            self.assertEqual(report["status"], "PASS")
            self.assertGreaterEqual(len(report["all_utc_days_observed"]), 2)

            signals = root / "signals.csv"
            days = report["all_utc_days_observed"]
            signals.write_text(
                "message_id,datetime_iso\n"
                f"1,{days[0]}T12:00:00Z\n"
                f"2,{days[1]}T12:00:00Z\n",
                encoding="utf-8",
            )
            cov = build_coverage_ledger_v2(signals, root / "out" / "V55_TICK_INVENTORY_V2.json", root / "coverage.csv")
            self.assertEqual(cov["covered_day_available"], 2)

    def test_semantic_gate_fails_wrong_action_even_when_executable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); labels = root / "labels.csv"; preds = root / "preds.csv"
            with labels.open("w", newline="", encoding="utf-8") as f1, preds.open("w", newline="", encoding="utf-8") as f2:
                lw = csv.DictWriter(f1, fieldnames=["message_id", "expected_action", "expected_scope"])
                pw = csv.DictWriter(f2, fieldnames=["message_id", "predicted_action", "predicted_scope", "executable"])
                lw.writeheader(); pw.writeheader()
                for i in range(200):
                    lw.writerow({"message_id": i, "expected_action": "CLOSE_ALL", "expected_scope": "REPLY_TARGET"})
                    pw.writerow({"message_id": i, "predicted_action": "CLOSE_ALL", "predicted_scope": "REPLY_TARGET", "executable": "true"})
            ok = certify_semantics_strict(labels, preds, self.policy)
            self.assertEqual(ok["status"], "PASS")
            rows = list(csv.DictReader(preds.open(encoding="utf-8")))
            rows[0]["predicted_action"] = "MOVE_SL_TO_BE"
            with preds.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["message_id", "predicted_action", "predicted_scope", "executable"]); w.writeheader(); w.writerows(rows)
            bad = certify_semantics_strict(labels, preds, self.policy)
            self.assertEqual(bad["status"], "FAIL")
            self.assertIn("ACTION_MISMATCH_DETECTED", bad["blockers"])

    def test_semantic_gate_fails_missed_actionable_and_wrong_scope(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); labels = root / "labels.csv"; preds = root / "preds.csv"
            with labels.open("w", newline="", encoding="utf-8") as f1, preds.open("w", newline="", encoding="utf-8") as f2:
                lw = csv.DictWriter(f1, fieldnames=["message_id", "expected_action", "expected_scope"])
                pw = csv.DictWriter(f2, fieldnames=["message_id", "predicted_action", "predicted_scope", "executable"])
                lw.writeheader(); pw.writeheader()
                for i in range(200):
                    lw.writerow({"message_id": i, "expected_action": "CLOSE_ALL", "expected_scope": "REPLY_TARGET"})
                    action = "NO_EXECUTION" if i == 0 else "CLOSE_ALL"
                    scope = "GLOBAL" if i == 1 else "REPLY_TARGET"
                    pw.writerow({"message_id": i, "predicted_action": action, "predicted_scope": scope, "executable": "false" if i == 0 else "true"})
            r = certify_semantics_strict(labels, preds, self.policy)
            self.assertEqual(r["status"], "FAIL")
            self.assertIn("MISSED_ACTIONABLE_COMMAND_DETECTED", r["blockers"])
            self.assertIn("SCOPE_MISMATCH_DETECTED", r["blockers"])

    def test_durable_state_rolls_back_at_each_injected_crash_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            db = DurableStateV2(Path(td) / "state.sqlite3")
            try:
                self.assertEqual(db.apply_atomic("base", {"m": 0}, {"position": {"qty": 0}}), "COMMITTED")
                baseline = db.snapshot()
                for phase in ("after_mutations", "after_journal"):
                    with self.assertRaises(RuntimeError):
                        db.apply_atomic("x-" + phase, {"m": phase}, {"position": {"qty": 1}}, crash_phase=phase)
                    self.assertEqual(db.snapshot(), baseline)
                self.assertEqual(db.apply_atomic("base", {"m": 0}, {"position": {"qty": 9}}), "DUPLICATE_NOOP")
                with self.assertRaises(CertificationError):
                    db.apply_atomic("base", {"m": 99}, {"position": {"qty": 9}})
            finally:
                db.close()

    def test_prospective_rejects_preboundary_and_counts_primary_days_only(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = ProspectiveLedgerV2(Path(td) / "p.sqlite3", self.policy, self.policy_hash)
            try:
                boundary = dt.datetime.fromisoformat(self.policy["prospective_boundary_utc"].replace("Z", "+00:00"))
                with self.assertRaises(CertificationError):
                    ledger.append((boundary - dt.timedelta(seconds=1)).isoformat(), "OLD", {})
                # 29 non-primary days must not satisfy the active-day gate.
                for i in range(29):
                    ledger.append((boundary + dt.timedelta(days=i, seconds=1)).isoformat(), "HEARTBEAT", {"i": i})
                for i in range(100):
                    ledger.append((boundary + dt.timedelta(days=29, minutes=i)).isoformat(), "PRIMARY", {"i": i}, primary_setup=True)
                status = ledger.status()
                self.assertEqual(status["primary_setups"], 100)
                self.assertEqual(status["primary_active_days"], 1)
                self.assertFalse(status["prospective_gate_passed"])
            finally:
                ledger.close()

    def test_prospective_rejects_out_of_order_event(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = ProspectiveLedgerV2(Path(td) / "p.sqlite3", self.policy, self.policy_hash)
            try:
                b = dt.datetime.fromisoformat(self.policy["prospective_boundary_utc"].replace("Z", "+00:00"))
                ledger.append((b + dt.timedelta(minutes=2)).isoformat(), "A", {})
                with self.assertRaises(CertificationError):
                    ledger.append((b + dt.timedelta(minutes=1)).isoformat(), "B", {})
            finally:
                ledger.close()

    def test_bootstrap_preserves_trade_order_and_counts_loss_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.csv"
            p.write_text(
                "exit_time_utc,net_pnl_sgd\n"
                "2026-01-01T10:00:00Z,-1\n"
                "2026-01-01T11:00:00Z,-1\n"
                "2026-01-01T12:00:00Z,-1\n",
                encoding="utf-8",
            )
            r = day_block_bootstrap_v2(p, iterations=50, seed=1, starting_balance=100, shutdown_drawdown=0.99, max_consecutive_losses=3)
            self.assertTrue(r["within_day_trade_order_preserved"])
            self.assertEqual(r["consecutive_loss_shutdown_probability"], 1.0)

    def test_bootstrap_counts_instant_ruin(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.csv"
            p.write_text("exit_time_utc,net_pnl_sgd\n2026-01-01T10:00:00Z,-150\n", encoding="utf-8")
            r = day_block_bootstrap_v2(p, iterations=20, seed=1, starting_balance=100, shutdown_drawdown=0.15, max_consecutive_losses=3)
            self.assertEqual(r["ruin_probability"], 1.0)

    def test_reference_bid_ask_execution_microcases(self):
        r = certify_reference_execution_microcases(self.policy)
        self.assertEqual(r["status"], "PASS")
        self.assertTrue(all(r["cases"].values()))

    def test_dedicated_account_invariant_blocks_preexisting_exposure(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "probe.json"
            p.write_text(json.dumps({"order_send_called": False, "existing_positions_by_symbol_volume": {"XAUUSD.i": 0.01}, "existing_pending_orders_by_symbol_count": {}}), encoding="utf-8")
            r = certify_dedicated_account_snapshot(p)
            self.assertEqual(r["status"], "FAIL")
            self.assertIn("PREEXISTING_POSITION_EXPOSURE", r["blockers"])

    def test_master_state_can_never_be_live_ready(self):
        r = master_release_state([{"schema": "OK", "status": "PASS"}], prospective={"prospective_gate_passed": True})
        self.assertEqual(r["release_state"], "PROSPECTIVE_SHADOW_CERTIFIED")
        self.assertFalse(r["live_ready"])
        self.assertFalse(r["real_orders_authorized"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
