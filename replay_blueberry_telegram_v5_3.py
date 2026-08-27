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

from v53_engine import V53PortfolioEngine
from v53_engine_patch import apply_v53_integrity_patch

VERSION = "V5.3_PROVIDER_FAITHFUL_EXECUTION_INTEGRITY"
LAST_ENGINE = None


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
        for pattern in ("**/replay_blueberry_telegram_v5_provider_faithful.py", "**/*v5*provider*faithful*.py"):
            try:
                candidates.extend(root.glob(pattern))
            except Exception:
                pass
    candidates = [p for p in candidates if p.is_file() and "v5_3" not in p.name.lower() and "v53" not in p.name.lower()]
    if not candidates:
        raise FileNotFoundError(
            "Could not find the extracted V5.2 source. Extract the V5.3 files into the same folder as "
            "XAUUSD_PROVIDER_FAITHFUL_V5_2_GAP_FILL_FIX and run again."
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


def write_v53_extras(engine):
    desktop = Path.home() / "Desktop"
    result_dirs = [p for p in desktop.glob("XAUUSD_BLUEBERRY_PROVIDER_FAITHFUL_V5*") if p.is_dir()]
    if not result_dirs:
        print("[V5.3] Reporter result directory not found; execution completed but extras could not be attached.")
        return None
    result_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = result_dirs[0]

    (out / "V53_EXECUTION_INTEGRITY_SUMMARY.json").write_text(
        json.dumps(engine.integrity_summary(), indent=2, default=str), encoding="utf-8"
    )
    rows = engine.ticket_rows
    if rows:
        with (out / "v53_ticket_ledger.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader(); writer.writerows(rows)
    if engine.audit:
        fields = sorted({key for row in engine.audit for key in row})
        with (out / "v53_execution_audit.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerows(engine.audit)

    (out / "V53_SUCCESS.txt").write_text("V5.3 execution-integrity overlay completed.\n", encoding="utf-8")
    result_zip = desktop / "XAUUSD_BLUEBERRY_PROVIDER_FAITHFUL_V5_3_RESULTS.zip"
    if result_zip.exists():
        result_zip.unlink()
    with zipfile.ZipFile(result_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in out.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(out))
    print(f"[V5.3] Final result ZIP: {result_zip}")
    return result_zip


def main():
    global LAST_ENGINE
    v52_source = find_v52_source()
    print(f"[V5.3] Frozen V5.2 compiler source: {v52_source}", flush=True)

    spec = importlib.util.spec_from_file_location("v52_core", v52_source)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to import V5.2 core")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    PatchedEngine = apply_v53_integrity_patch(V53PortfolioEngine)

    class CapturingV53Engine(PatchedEngine):
        def __init__(self, *args, **kwargs):
            global LAST_ENGINE
            super().__init__(*args, **kwargs)
            LAST_ENGINE = self

    # Freeze the V4.1/V5.2 provider-language compiler and replace ONLY execution mechanics.
    module.PortfolioEngine = CapturingV53Engine
    if hasattr(module, "VERSION"):
        module.VERSION = VERSION
    if hasattr(module, "OUT_NAME"):
        module.OUT_NAME = "XAUUSD_BLUEBERRY_PROVIDER_FAITHFUL_V5_3_RESULTS"

    forwarded = extract_v52_runner_args(v52_source)
    print(f"[V5.3] Forwarded V5.2 runner arguments: {forwarded}", flush=True)
    sys.argv = [str(v52_source)] + forwarded
    try:
        module.main()
    finally:
        if LAST_ENGINE is not None:
            write_v53_extras(LAST_ENGINE)


if __name__ == "__main__":
    main()
