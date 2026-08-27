from __future__ import annotations

import argparse
import json
from pathlib import Path

from v55_certification import (
    certify_broker,
    certify_parent,
    load_policy,
    sha256_file,
)
from v55_hardening import (
    build_coverage_ledger_v2,
    certify_dedicated_account_snapshot,
    certify_reference_execution_microcases,
    certify_semantics_strict,
    certify_tick_inventory_v2,
    day_block_bootstrap_v2,
    master_release_state,
)


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="V5.5 forensic certification master orchestrator (non-authorizing)")
    ap.add_argument("--parent-zip", type=Path)
    ap.add_argument("--provider-dir", type=Path)
    ap.add_argument("--ticks", type=Path)
    ap.add_argument("--signals", type=Path)
    ap.add_argument("--account-info", type=Path)
    ap.add_argument("--symbol-info", type=Path)
    ap.add_argument("--probe", type=Path)
    ap.add_argument("--semantic-labels", type=Path)
    ap.add_argument("--semantic-predictions", type=Path)
    ap.add_argument("--trades", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("V55_CERTIFICATION_OUTPUT"))
    args = ap.parse_args()

    policy_path = Path(__file__).with_name("v55_policy.json")
    policy = load_policy(policy_path)
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    reports = []
    missing = []

    ref = certify_reference_execution_microcases(policy)
    write_json(out / "V55_REFERENCE_EXECUTION_MICROCASES.json", ref)
    reports.append(ref)

    if args.parent_zip:
        parent = certify_parent(args.parent_zip, args.provider_dir, policy)
        if args.provider_dir is None:
            parent.setdefault("blockers", []).append("FROZEN_PROVIDER_ARTIFACT_DIRECTORY_NOT_SUPPLIED")
            parent["status"] = "FAIL"
        write_json(out / "V55_PARENT_INTEGRITY.json", parent)
        reports.append(parent)
    else:
        missing.append("PARENT_V54_ZIP")

    tick_report = None
    if args.ticks:
        tick_report = certify_tick_inventory_v2(args.ticks, out)
        reports.append(tick_report)
    else:
        missing.append("BLUEBERRY_TICK_ARCHIVE_OR_DIRECTORY")

    if args.signals and tick_report:
        coverage = build_coverage_ledger_v2(args.signals, out / "V55_TICK_INVENTORY_V2.json", out / "V55_SIGNAL_TICK_COVERAGE_V2.csv")
        write_json(out / "V55_SIGNAL_TICK_COVERAGE_V2.json", coverage)
        if coverage["uncovered"]:
            coverage["status"] = "FAIL"
            coverage["blockers"] = ["TELEGRAM_HISTORY_NOT_FULLY_COVERED_BY_CERTIFIED_TICKS"]
        else:
            coverage["status"] = "PASS"
        reports.append(coverage)
    elif not args.signals:
        missing.append("FROZEN_PROVIDER_SIGNAL_LEDGER")

    if args.account_info and args.symbol_info:
        broker = certify_broker(args.account_info, args.symbol_info, args.probe, policy)
        write_json(out / "V55_BROKER_CERTIFICATION.json", broker)
        # Identity can pass while historical costs remain explicitly blocked.
        if broker.get("blockers"):
            broker["status"] = "FAIL"
        reports.append(broker)
        if args.probe:
            isolated = certify_dedicated_account_snapshot(args.probe)
            write_json(out / "V55_DEDICATED_ACCOUNT_SNAPSHOT.json", isolated)
            reports.append(isolated)
    else:
        missing.append("BROKER_ACCOUNT_AND_SYMBOL_SNAPSHOTS")

    if args.semantic_labels and args.semantic_predictions:
        sem = certify_semantics_strict(args.semantic_labels, args.semantic_predictions, policy)
        write_json(out / "V55_SEMANTIC_CERTIFICATION_V2.json", sem)
        reports.append(sem)
    else:
        missing.append("200_PLUS_HUMAN_LABELS_AND_FROZEN_PARSER_PREDICTIONS")

    if args.trades:
        boot = day_block_bootstrap_v2(
            args.trades,
            iterations=10000,
            seed=55,
            starting_balance=policy["small_account_projection"]["starting_balance_sgd"],
            shutdown_drawdown=policy["small_account_projection"]["hard_drawdown_fraction"],
            max_consecutive_losses=policy["small_account_projection"]["max_consecutive_losses"],
        )
        write_json(out / "V55_DAY_BLOCK_BOOTSTRAP_V2.json", boot)
        # Diagnostic only; not used as a pass/fail promotion gate.
    else:
        missing.append("CAUSAL_REPLAY_TRADE_LEDGER_FOR_DIAGNOSTIC_BOOTSTRAP")

    synthetic_missing_report = {
        "schema": "V55_REQUIRED_EVIDENCE_PRESENCE_V1",
        "status": "PASS" if not missing else "FAIL",
        "blockers": [f"MISSING_{x}" for x in missing],
    }
    reports.append(synthetic_missing_report)
    write_json(out / "V55_REQUIRED_EVIDENCE_PRESENCE.json", synthetic_missing_report)

    master = master_release_state(reports)
    master.update({
        "policy_sha256": sha256_file(policy_path),
        "prospective_boundary_utc": policy["prospective_boundary_utc"],
        "provider_strategy_mutation_authorized": False,
        "live_ready_state_exists": False,
        "note": "A green software self-test is not historical-profitability certification. External broker/tick/semantic evidence gates must also pass.",
    })
    write_json(out / "V55_MASTER_CERTIFICATION.json", master)
    print(json.dumps(master, indent=2))
    return 0 if master["release_state"] != "RESEARCH_ENGINE_CERTIFICATION_INCOMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
