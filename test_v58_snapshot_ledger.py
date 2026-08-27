from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from v58_signal_intelligence_gate import (
    AccountEvidence,
    Decision,
    DecisionResult,
    FeatureObservation,
    ModelEvidence,
    SignalProposal,
)
from v58_snapshot_ledger import GENESIS_HASH, SnapshotLedger

NOW = 1_800_000_000_000


def proposal(uid="sig-1"):
    return SignalProposal(uid, NOW - 5_000, "BUY", 2445.0, 2448.0, 2439.0, 2458.0)


def features():
    return {"bid": FeatureObservation(2450.0, NOW - 1000, "BROKER", "BROKER")}


def account():
    return AccountEvidence(1000.0, 1000.0, 900.0, 4.0, 90.0, 0.0, 0, True)


def model():
    return ModelEvidence("M1", True, NOW - 1000, 0.60, 3.0, 100, 1.2, 2.5)


def result():
    return DecisionResult(Decision.TAKE, ("TEST_PASS",), diagnostics={"x": 1})


class TestSnapshotLedger(unittest.TestCase):
    def test_empty_ledger_verifies(self):
        with tempfile.TemporaryDirectory() as td:
            v = SnapshotLedger(Path(td) / "ledger.jsonl").verify()
            self.assertTrue(v["ok"])
            self.assertEqual(v["records"], 0)
            self.assertEqual(v["tail_hash"], GENESIS_HASH)

    def test_append_builds_hash_chain(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ledger.jsonl"
            ledger = SnapshotLedger(p)
            r1 = ledger.append(NOW, proposal("a"), features(), account(), model(), result())
            r2 = ledger.append(NOW + 1, proposal("b"), features(), account(), None, DecisionResult(Decision.INSUFFICIENT_DATA, ("NO_MODEL",)))
            self.assertEqual(r1["sequence"], 1)
            self.assertEqual(r1["prev_hash"], GENESIS_HASH)
            self.assertEqual(r2["sequence"], 2)
            self.assertEqual(r2["prev_hash"], r1["record_hash"])
            v = ledger.verify()
            self.assertTrue(v["ok"])
            self.assertEqual(v["records"], 2)
            self.assertEqual(v["tail_hash"], r2["record_hash"])

    def test_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ledger.jsonl"
            ledger = SnapshotLedger(p)
            ledger.append(NOW, proposal(), features(), account(), model(), result())
            row = json.loads(p.read_text(encoding="utf-8").strip())
            row["decision"]["decision"] = "REJECT"
            p.write_text(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            v = ledger.verify()
            self.assertFalse(v["ok"])
            self.assertEqual(v["reason"], "RECORD_HASH_MISMATCH")

    def test_future_outcome_is_not_stored_in_snapshot_schema(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ledger.jsonl"
            ledger = SnapshotLedger(p)
            rec = ledger.append(NOW, proposal(), features(), account(), model(), result())
            self.assertNotIn("outcome", rec)
            self.assertNotIn("future_pnl", rec)
            self.assertEqual(rec["schema"], "XAUUSD_V58_DECISION_SNAPSHOT_V1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
