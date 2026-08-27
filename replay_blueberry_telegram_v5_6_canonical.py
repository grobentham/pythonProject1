#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import os
import shlex
import sys
import zipfile
from pathlib import Path

from v53_engine_patch import apply_v53_integrity_patch
from v56_engine import V56CanonicalEngine
from v56_engine_hardening import apply_v56_semantic_hardening

VERSION = "V5.6_PROVIDER_CANONICAL_INTERPRETATION"
BASE_OUT_NAME = "XAUUSD_BLUEBERRY_PROVIDER_CANONICAL_V5_6_RESULTS"
LAST_ENGINE = None
ACTIVE_PROFILE = None
ACTIVE_OUT_NAME = None

PROFILES = {
    # This is the primary answer to "how did the provider perform on S$1,000?"
    # It follows canonical provider-authorized 0.01 tickets and broker margin,
    # without silently filtering trades through our research risk cap.
    "CANONICAL_PROVIDER_SGD1000": {
        "starting_balance_sgd": 1000.0,
        "risk_cap_enabled": False,
        "profile_role": "RAW_PROVIDER_CANONICAL_PRIMARY",
    },
    # Secondary safety-overlay diagnostic. It must never replace the raw-provider
    # result merely because one profile performs better historically.
    "CANONICAL_SURVIVAL_SGD1000": {
        "starting_balance_sgd": 1000.0,
        "risk_cap_enabled": True,
        "max_reserved_stop_risk_pct": 10.0,
        "profile_role": "RISK_CONSTRAINED_SECONDARY",
    },
}


def selected_profile():
    name = os.environ.get("V56_PROFILE", "CANONICAL_PROVIDER_SGD1000").strip().upper()
    if name not in PROFILES:
        raise ValueError(f"Unknown V56_PROFILE={name!r}; choose one of {sorted(PROFILES)}")
    return name, dict(PROFILES[name])


def find_v52_source() -> Path:
    here = Path(__file__).resolve().parent
    names = [
        "replay_blueberry_telegram_v5_provider_faithful.py",
        "replay_blueberry_telegram_v5_2_provider_faithful.py",
    ]
    for folder in (here, here.parent):
        for name in names:
            candidate = folder / name
            if candidate.exists():
                return candidate

    candidates = []
    for root in (Path.home() / "Downloads", Path.home() / "Desktop"):
        if not root.exists():
            continue
        for pattern in (
            "**/replay_blueberry_telegram_v5_provider_faithful.py",
            "**/*v5*provider*faithful*.py",
        ):
            try:
                candidates.extend(root.glob(pattern))
            except Exception:
                pass
    candidates = [p for p in candidates if p.is_file() and "v5_3" not in p.name.lower() and "v5_6" not in p.name.lower()]
    if not candidates:
        raise FileNotFoundError(
            "Could not find the extracted frozen V5.2 provider-language compiler. "
            "V5.6 intentionally reuses that compiler and changes only the frozen canonical interpretation/execution overlay."
        )
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def extract_v52_runner_args(v52_source: Path):
    folder = v52_source.parent
    batch_files = sorted(folder.glob("*V52*.bat")) + sorted(folder.glob("RUN_V5_FULL*.bat"))
    for batch in batch_files:
        try:
            raw = batch.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        raw = raw.replace("^\r\n", " ").replace("^\n", " ")
        for line in raw.splitlines():
            if "python" not in line.lower() or v52_source.name.lower() not in line.lower():
                continue
            index = line.lower().find(v52_source.name.lower())
            tail = line[index + len(v52_source.name):].strip().lstrip('"').strip()
            tail = tail.replace("%~dp0", str(folder) + os.sep)
            tail = tail.replace("%USERPROFILE%", str(Path.home()))
            try:
                return shlex.split(tail, posix=False)
            except Exception:
                return tail.split()
    return []


def write_v56_extras(engine):
    desktop = Path.home() / "Desktop"
    result_dirs = [p for p in desktop.glob("XAUUSD_BLUEBERRY_PROVIDER_CANONICAL_V5_6*") if p.is_dir()]
    if not result_dirs:
        result_dirs = [p for p in desktop.glob("XAUUSD_BLUEBERRY_PROVIDER_FAITHFUL_V5*") if p.is_dir()]
    if not result_dirs:
        print("[V5.6] Reporter result directory not found; replay completed but extras could not be attached.")
        return None
    result_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = result_dirs[0]

    summary = engine.integrity_summary()
    summary.update({
        "canonical_interpretation_frozen_before_pnl": True,
        "provider_language_spec": "PROVIDER_LANGUAGE_CANONICAL_V5_6.md",
        "historical_test_profile": ACTIVE_PROFILE,
        "historical_test_profile_config": PROFILES.get(ACTIVE_PROFILE, {}),
        "historical_only": True,
        "real_orders": False,
        "live_ready": False,
    })
    (out / "V56_CANONICAL_EXECUTION_SUMMARY.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )

    rows = engine.ticket_rows
    if rows:
        with (out / "v56_ticket_ledger.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    if engine.audit:
        fields = sorted({key for row in engine.audit for key in row})
        with (out / "v56_execution_audit.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(engine.audit)

    (out / "V56_CANONICAL_FROZEN.txt").write_text(
        f"V5.6 canonical provider interpretation was frozen before this P&L run.\nProfile: {ACTIVE_PROFILE}\n"
        "Historical research only. Real orders disabled.\n",
        encoding="utf-8",
    )

    result_zip = desktop / f"{ACTIVE_OUT_NAME}.zip"
    if result_zip.exists():
        result_zip.unlink()
    with zipfile.ZipFile(result_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in out.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(out))
    print(f"[V5.6] Final result ZIP: {result_zip}")
    return result_zip


def main():
    global LAST_ENGINE, ACTIVE_PROFILE, ACTIVE_OUT_NAME
    ACTIVE_PROFILE, profile_cfg = selected_profile()
    ACTIVE_OUT_NAME = f"{BASE_OUT_NAME}_{ACTIVE_PROFILE}"

    v52_source = find_v52_source()
    print(f"[V5.6] Frozen provider-language compiler: {v52_source}", flush=True)
    print(f"[V5.6] Historical profile: {ACTIVE_PROFILE} {profile_cfg}", flush=True)
    print("[V5.6] Canonical semantics: TWO_BOUNDARY_ZONE / SINGLE_TP_DYNAMIC / EXPLICIT_MULTI_TP / OR_CLOSE_ALL", flush=True)

    spec = importlib.util.spec_from_file_location("v52_core_v56", v52_source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to import frozen V5.2 provider-language compiler")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    HardenedCanonicalEngine = apply_v56_semantic_hardening(V56CanonicalEngine)
    PatchedCanonicalEngine = apply_v53_integrity_patch(HardenedCanonicalEngine)

    class CapturingV56Engine(PatchedCanonicalEngine):
        def __init__(self, *args, **kwargs):
            global LAST_ENGINE
            super().__init__(*args, **kwargs)
            # The parent compiler may supply its own research policy. The V5.6
            # profile is frozen later in the stack so the primary provider run
            # cannot accidentally inherit the old 10% research filter.
            merged = dict(self.policy) if isinstance(self.policy, dict) else {}
            merged.update(profile_cfg)
            self.policy = merged
            start = float(profile_cfg["starting_balance_sgd"])
            self.cash = start
            self.starting_balance_sgd = start
            self.peak_equity = start
            self.min_equity_sgd = start
            self.max_equity_sgd = start
            self.peak = start
            self.max_drawdown_sgd = 0.0
            self.max_dd = 0.0
            self.max_drawdown = 0.0
            LAST_ENGINE = self

    module.PortfolioEngine = CapturingV56Engine
    if hasattr(module, "VERSION"):
        module.VERSION = VERSION
    if hasattr(module, "OUT_NAME"):
        module.OUT_NAME = ACTIVE_OUT_NAME

    forwarded = extract_v52_runner_args(v52_source)
    print(f"[V5.6] Forwarded frozen compiler arguments: {forwarded}", flush=True)
    sys.argv = [str(v52_source)] + forwarded
    try:
        module.main()
    finally:
        if LAST_ENGINE is not None:
            write_v56_extras(LAST_ENGINE)


if __name__ == "__main__":
    main()
