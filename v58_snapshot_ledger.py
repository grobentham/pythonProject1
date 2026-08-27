from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from v58_signal_intelligence_gate import (
    AccountEvidence,
    DecisionResult,
    FeatureObservation,
    ModelEvidence,
    SignalProposal,
)

GENESIS_HASH = "0" * 64


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _model_dict(model: Optional[ModelEvidence]):
    return None if model is None else asdict(model)


def make_snapshot_payload(
    sequence: int,
    prev_hash: str,
    decision_time_ms: int,
    proposal: SignalProposal,
    features: Mapping[str, FeatureObservation],
    account: AccountEvidence,
    model: Optional[ModelEvidence],
    result: DecisionResult,
) -> Dict[str, Any]:
    return {
        "schema": "XAUUSD_V58_DECISION_SNAPSHOT_V1",
        "sequence": int(sequence),
        "prev_hash": str(prev_hash),
        "decision_time_ms": int(decision_time_ms),
        "proposal": asdict(proposal),
        "features": {k: asdict(v) for k, v in sorted(features.items())},
        "account": asdict(account),
        "model": _model_dict(model),
        "decision": result.to_dict(),
    }


class SnapshotLedger:
    """Append-only hash-chained prospective decision ledger.

    The ledger records exactly what the gate knew at decision time. Outcomes are
    intentionally not part of the decision snapshot and must be joined later by
    immutable UID/timestamp keys. Existing rows are never edited by this class.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _last(self):
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS_HASH
        last = None
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)
        if last is None:
            return 0, GENESIS_HASH
        return int(last["sequence"]), str(last["record_hash"])

    def append(
        self,
        decision_time_ms: int,
        proposal: SignalProposal,
        features: Mapping[str, FeatureObservation],
        account: AccountEvidence,
        model: Optional[ModelEvidence],
        result: DecisionResult,
    ) -> Dict[str, Any]:
        last_seq, prev_hash = self._last()
        payload = make_snapshot_payload(
            last_seq + 1, prev_hash, decision_time_ms,
            proposal, features, account, model, result,
        )
        record_hash = _hash_payload(payload)
        record = dict(payload)
        record["record_hash"] = record_hash
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(_canonical_json(record) + "\n")
            f.flush()
        return record

    def verify(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"ok": True, "records": 0, "tail_hash": GENESIS_HASH}
        prev_hash = GENESIS_HASH
        expected_seq = 1
        records = 0
        with self.path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                rec = json.loads(line)
                if int(rec.get("sequence", -1)) != expected_seq:
                    return {"ok": False, "line": lineno, "reason": "SEQUENCE_BREAK"}
                if rec.get("prev_hash") != prev_hash:
                    return {"ok": False, "line": lineno, "reason": "PREV_HASH_BREAK"}
                stored_hash = rec.get("record_hash")
                payload = dict(rec)
                payload.pop("record_hash", None)
                calculated = _hash_payload(payload)
                if stored_hash != calculated:
                    return {"ok": False, "line": lineno, "reason": "RECORD_HASH_MISMATCH"}
                prev_hash = stored_hash
                expected_seq += 1
                records += 1
        return {"ok": True, "records": records, "tail_hash": prev_hash}
