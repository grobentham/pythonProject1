import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from v55_certification import (
    CertificationError,
    DurableState,
    ProspectiveLedger,
    build_coverage_ledger,
    certify_semantics,
    certify_ticks,
    day_block_bootstrap,
    load_policy,
    parse_ts,
    sha256_file,
)


class V55Tests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy()

    def test_policy_is_non_authorizing_and_frozen(self):
        self.assertFalse(self.policy["execution"]["real_orders"])
        self.assertFalse(self.policy["execution"]["order_authorization"])
        self.assertFalse(self.policy["live_ready_state_exists"])
        self.assertFalse(self.policy["frozen_provider_plan"]["strategy_mutation_authorized"])
        self.assertEqual(self.policy["small_account_projection"]["max_active_setups_including_pending"], 1)
        self.assertEqual(self.policy["small_account_projection"]["volume_lot"], 0.01)
        self.assertEqual(self.policy["execution"]["latency_grid_seconds"], [0, 1, 2, 5, 10, 30])
        self.assertEqual(self.policy["execution"]["stop_out_pct"], 50.0)
        self.assertFalse(self.policy["execution"]["margin_call_forced_liquidation"])

    def test_parse_timestamp(self):
        self.assertEqual(parse_ts("2026-08-27T16:36:49Z").year, 2026)
        self.assertEqual(parse_ts("1700000000000").tzinfo, dt.timezone.utc)

    def test_transactional_idempotency_and_collision_and_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            db = DurableState(Path(td) / "state.sqlite3")
            try:
                self.assertEqual(db.apply_atomic("k1", {"message": 1}, {"position": {"qty": 1}}), "COMMITTED")
                before = db.snapshot()
                self.assertEqual(db.apply_atomic("k1", {"message": 1}, {"position": {"qty": 999}}), "DUPLICATE_NOOP")
                self.assertEqual(db.snapshot(), before)
                with self.assertRaises(CertificationError):
                    db.apply_atomic("k1", {"message": 2}, {"position": {"qty": 2}})
                with self.assertRaises(RuntimeError):
                    db.apply_atomic("k2", {"message": 3}, {"position": {"qty": 3}}, crash_before_commit=True)
                self.assertNotIn("other", db.snapshot())
                self.assertEqual(db.snapshot(), before)
            finally:
                db.close()

    def test_prospective_hash_chain_and_gate(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prospective.sqlite3"
            ledger = ProspectiveLedger(p, sha256_file(Path(__file__).with_name("v55_policy.json")))
            try:
                start = dt.datetime(2026, 8, 28, tzinfo=dt.timezone.utc)
                for i in range(100):
                    day = i % 30
                    ts = start + dt.timedelta(days=day, minutes=i)
                    ledger.append(ts.isoformat(), "PRIMARY_SETUP_OBSERVED", {"i": i}, primary_setup=True)
                status = ledger.status(self.policy)
                self.assertTrue(status["hash_chain_valid"])
                self.assertTrue(status["prospective_gate_passed"])
                self.assertEqual(status["release_state"], "PROSPECTIVE_SHADOW_CERTIFIED")
            finally:
                ledger.close()

    def test_tick_certification_and_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ticks = root / "ticks"
            ticks.mkdir()
            tick_file = ticks / "2026-08-20.csv"
            tick_file.write_text(
                "time_msc,bid,ask\n"
                "1787184000000,3300.00,3300.10\n"
                "1787184001000,3300.05,3300.15\n",
                encoding="utf-8",
            )
            report = certify_ticks(ticks, root / "out")
            self.assertEqual(report["status"], "PASS")
            signals = root / "signals.csv"
            signals.write_text(
                "message_id,datetime_iso\n"
                "1,2026-08-20T00:00:00Z\n"
                "2,2023-05-09T00:00:00Z\n",
                encoding="utf-8",
            )
            cov = build_coverage_ledger(signals, root / "out" / "V55_TICK_CERTIFICATION.json", root / "coverage.csv")
            self.assertEqual(cov["signals"], 2)
            self.assertEqual(cov["uncovered"], 1)
            self.assertFalse(cov["full_history_claim_authorized"])

    def test_crossed_quote_fails_tick_certification(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            p = root / "bad.csv"
            p.write_text("timestamp,bid,ask\n2026-08-20T00:00:00Z,3300.20,3300.10\n", encoding="utf-8")
            report = certify_ticks(p, root / "out")
            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(report["crossed_quotes"], 1)

    def test_semantic_gate_requires_200_and_zero_false_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            labels = root / "labels.csv"
            preds = root / "preds.csv"
            with labels.open("w", newline="", encoding="utf-8") as f1, preds.open("w", newline="", encoding="utf-8") as f2:
                lw = csv.DictWriter(f1, fieldnames=["message_id", "expected_action", "expected_scope"])
                pw = csv.DictWriter(f2, fieldnames=["message_id", "predicted_action", "predicted_scope", "executable"])
                lw.writeheader(); pw.writeheader()
                for i in range(200):
                    lw.writerow({"message_id": i, "expected_action": "CLOSE_ALL", "expected_scope": "REPLY_TARGET"})
                    pw.writerow({"message_id": i, "predicted_action": "CLOSE_ALL", "predicted_scope": "REPLY_TARGET", "executable": "true"})
            report = certify_semantics(labels, preds, self.policy)
            self.assertEqual(report["status"], "PASS")
            with preds.open("a", newline="", encoding="utf-8") as f:
                pass

    def test_day_block_bootstrap_is_not_iid_ticket_shuffle(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "trades.csv"
            p.write_text(
                "exit_time_utc,net_pnl_sgd\n"
                "2026-01-01T10:00:00Z,2\n"
                "2026-01-01T11:00:00Z,-1\n"
                "2026-01-02T10:00:00Z,-3\n"
                "2026-01-03T10:00:00Z,4\n",
                encoding="utf-8",
            )
            report = day_block_bootstrap(p, iterations=100, seed=55)
            self.assertFalse(report["iid_ticket_shuffle_used"])
            self.assertEqual(report["day_blocks"], 3)
            self.assertTrue(report["diagnostic_only"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
