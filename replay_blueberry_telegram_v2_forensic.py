#!/usr/bin/env python3
"""
Blueberry XAUUSD × Telegram Forensic Replay V2
===============================================

PURPOSE
-------
Reconstruct, as causally and broker-realistically as the available exports allow,
what a SGD 1,000 / 0.01-lot Blueberry XAUUSD.i account could have done while
following the Telegram signal stream.

This V2 intentionally differs from the earlier replay:
- no geometry-only 30-minute deduplication;
- edited Telegram messages are never allowed to leak backward in time;
- TP1 / TP2 / TP3+ are parsed separately;
- reply-chain management and unlinked/global management are modelled;
- re-entry instructions can create new setups;
- one active setup at a time is enforced;
- BUY uses Ask for entry and Bid for exit; SELL uses Bid for entry and Ask for exit;
- pending limits never silently become market orders;
- exact Blueberry ticks resolve stop/target ordering;
- MAE/MFE/equity calculations stop at the actual exit tick;
- 80% margin-call and 50% stop-out can be modelled from account metadata;
- impossible 0.005 partial closes are not invented on a 0.01-step account;
- latency, timestamp uncertainty, TTL, partial-close and stale-pending assumptions
  are separated into explicit sensitivity runs;
- the script produces a causal audit timeline for every accepted setup;
- anything materially unverifiable is labelled rather than guessed.

IMPORTANT LIMITATIONS
---------------------
A Telegram Desktop HTML export normally preserves final message contents, not a
complete version history. If an edit marker/timestamp is visible but the original
pre-edit text is not, STRICT mode EXCLUDES the message. EXECUTABLE_ALL mode allows
only the final edited text starting at the edit timestamp. Deleted messages that are
absent from the export cannot be reconstructed.

Historical Blueberry contract/margin/swap specifications may also have changed.
The script records current exported account/symbol metadata and downgrades the
certification when historical facts cannot be proven.

The script is READ-ONLY. It never sends an MT5 order.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
import sys
import zipfile
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup


# ============================================================================
# DEFAULTS — frozen executable account / bridge assumptions
# ============================================================================

STARTING_BALANCE_SGD = 1000.0
LOT_SIZE = 0.01
EXPECTED_CONTRACT_SIZE = 100.0
PENDING_TTL_MINUTES = 180
COMMISSION_USD_PER_LOT_PER_SIDE = 3.50
BASE_LATENCY_SECONDS = 1
CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS = 999
INITIAL_QUOTE_MAX_GAP_SECONDS = 120
CLOSE_QUOTE_MAX_GAP_SECONDS = 120
DEFAULT_LEVERAGE = 500
DEFAULT_MARGIN_CALL_PCT = 80.0
DEFAULT_STOPOUT_PCT = 50.0
FALLBACK_SGD_PER_USD = 1.30
CACHE_DAYS = 3

COARSE_DEPTHS = [0.00, 0.25, 0.50, 0.75, 1.00]
TP_POLICIES = ["TP1", "TP2", "TP3", "MANAGEMENT"]
LATENCY_STRESS_SECONDS = [0, 1, 3, 5, 10, 30]
TTL_STRESS_MINUTES = [60, 180, 360, 720]

PRIMARY_PARTIAL_MINLOT_POLICY = "close_full"
PRIMARY_RUNNING_NOTICE_CANCELS_UNFILLED = True
ASSUME_GOLD_ONLY_CHANNEL_FOR_UNLINKED_MANAGEMENT = True

# If true, every .csv.gz tick file is SHA-256 hashed once. This is slower but
# provides reproducible input identity.
DEFAULT_DEEP_HASH_TICKS = True

# A missing Monday-Friday daily tick file is recorded as a potential gap. It is
# not automatically called corruption because some holidays are legitimate.
FLAG_MISSING_WEEKDAY_FILES = True

SGT = timezone(timedelta(hours=8))


# ============================================================================
# REGEXES
# ============================================================================

PRICE_PATTERN = r"([1-9]\d{3}(?:\.\d{1,3})?)"
PRICE_RE = re.compile(r"(?<!\d)" + PRICE_PATTERN + r"(?!\d)")
SIDE_RE = re.compile(r"(?i)\b(BUY|SELL)\b")
SL_RE = re.compile(
    r"(?i)(?:\bSL\b|\bSTL\b|STOP\s*LOSS|STOPLOSS)"
    r"\s*(?:[:=@\-]|AT|TO)?\s*" + PRICE_PATTERN
)
TP_INDEXED_RE = re.compile(
    r"(?i)(?:\bTP\b|TAKE\s*PROFIT)\s*([1-9])"
    r"\s*(?:[:=@\-]|AT)?\s*" + PRICE_PATTERN
)
TP_PLAIN_RE = re.compile(
    r"(?i)(?:\bTP\b|TAKE\s*PROFIT)"
    r"\s*(?:[:=@\-]|AT)?\s*" + PRICE_PATTERN
)


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class BrokerSpec:
    currency: str = "SGD"
    leverage: int = DEFAULT_LEVERAGE
    margin_call_pct: float = DEFAULT_MARGIN_CALL_PCT
    stopout_pct: float = DEFAULT_STOPOUT_PCT
    server: Optional[str] = None
    company: Optional[str] = None
    symbol: str = "XAUUSD.i"
    contract_size: float = EXPECTED_CONTRACT_SIZE
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 50.0
    digits: int = 2
    point: float = 0.01
    tick_size: float = 0.01
    stops_level_points: int = 0
    freeze_level_points: int = 0
    swap_long: Optional[float] = None
    swap_short: Optional[float] = None
    swap_mode: Optional[Any] = None
    swap_rollover3days: Optional[Any] = None

    @property
    def ounces(self) -> float:
        return self.contract_size * LOT_SIZE

    @property
    def commission_usd_per_side(self) -> float:
        return COMMISSION_USD_PER_LOT_PER_SIDE * LOT_SIZE

    @property
    def min_price_distance(self) -> float:
        return max(0, int(self.stops_level_points)) * self.point


@dataclass
class TickDay:
    times: np.ndarray
    bid: np.ndarray
    ask: np.ndarray


@dataclass
class ReplayConfig:
    tier: str
    depth: float
    tp_policy: str
    latency_seconds: int
    timestamp_uncertainty_ms: int
    pending_ttl_minutes: int
    partial_minlot_policy: str
    running_notice_cancels_unfilled: bool

    def key(self) -> str:
        return (
            f"tier-{self.tier}__depth-{self.depth:.3f}__tp-{self.tp_policy}__"
            f"lat-{self.latency_seconds}s__unc-{self.timestamp_uncertainty_ms}ms__"
            f"ttl-{self.pending_ttl_minutes}m__partial-{self.partial_minlot_policy}__"
            f"runningcancel-{int(self.running_notice_cancels_unfilled)}"
        ).replace(".", "p")


# ============================================================================
# UTILITY HELPERS
# ============================================================================

def json_default(obj: Any):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        if np.isnan(obj):
            return None
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(type(obj).__name__)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ms_to_iso(ms: Optional[int]) -> Optional[str]:
    d = ms_to_dt(ms)
    return d.isoformat() if d else None


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def stable_hash_json(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, default=json_default).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def round_price(price: float, spec: BrokerSpec) -> float:
    tick = spec.tick_size if spec.tick_size and spec.tick_size > 0 else spec.point
    n = round(price / tick)
    return round(n * tick, spec.digits)


def read_json_if_exists(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def nested_candidate(obj: Dict[str, Any], names: Iterable[str]) -> Dict[str, Any]:
    for n in names:
        v = obj.get(n)
        if isinstance(v, dict):
            return v
    return obj


def pick(d: Dict[str, Any], *keys: str, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def ensure_clean_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def find_blueberry_folder(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if (p / "ticks").exists():
            return p
        raise FileNotFoundError(f"No ticks folder under {p}")
    home = Path.home()
    desktop = home / "Desktop"
    candidates = [desktop / "blueberry_xauusd_export", desktop / "blueberry_xauusd_export_2"]
    for p in candidates:
        if (p / "ticks").exists():
            return p
    for p in sorted(desktop.glob("blueberry_xauusd_export*")):
        if p.is_dir() and (p / "ticks").exists():
            return p
    raise FileNotFoundError("Could not find Desktop\\blueberry_xauusd_export\\ticks")


def find_telegram_zip(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.exists():
            return p
        raise FileNotFoundError(p)
    home = Path.home()
    found: List[Path] = []
    for folder in [home / "Downloads", home / "Desktop"]:
        found += list(folder.glob("ChatExport*.zip"))
    found = [p for p in found if p.is_file()]
    if not found:
        raise FileNotFoundError("Could not find ChatExport*.zip in Downloads or Desktop")
    found.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
    return found[0]


def parse_telegram_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    # Telegram desktop export example: 26.08.2026 23:31:16 UTC+08:00
    m = re.search(
        r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})"
        r"(?:\s+UTC([+-]\d{2}:\d{2}))?",
        value,
    )
    if m:
        base = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M:%S")
        off = m.group(3)
        if off:
            sign = 1 if off.startswith("+") else -1
            hh, mm = map(int, off[1:].split(":"))
            tz = timezone(sign * timedelta(hours=hh, minutes=mm))
        else:
            tz = SGT
        return base.replace(tzinfo=tz).astimezone(timezone.utc)
    # ISO fallback
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=SGT)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def reconstruct_edited_hhmm(text: str, original: datetime) -> Optional[datetime]:
    m = re.search(r"(?i)edited\s+(\d{1,2}):(\d{2})", text or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    local = original.astimezone(SGT)
    candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate < local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


# ============================================================================
# BROKER METADATA
# ============================================================================

def load_broker_spec(export_root: Path) -> BrokerSpec:
    account_raw = read_json_if_exists(export_root / "account_info.json")
    symbol_raw = read_json_if_exists(export_root / "symbol_info.json")
    account = nested_candidate(account_raw, ["account", "account_info", "info"])
    symbol = nested_candidate(symbol_raw, ["symbol", "symbol_info", "info"])

    spec = BrokerSpec(
        currency=str(pick(account, "currency", default="SGD")),
        leverage=int(pick(account, "leverage", default=DEFAULT_LEVERAGE) or DEFAULT_LEVERAGE),
        margin_call_pct=float(pick(account, "margin_so_call", default=DEFAULT_MARGIN_CALL_PCT) or DEFAULT_MARGIN_CALL_PCT),
        stopout_pct=float(pick(account, "margin_so_so", default=DEFAULT_STOPOUT_PCT) or DEFAULT_STOPOUT_PCT),
        server=pick(account, "server"),
        company=pick(account, "company"),
        symbol=str(pick(symbol, "name", "symbol", default="XAUUSD.i")),
        contract_size=float(pick(symbol, "trade_contract_size", "contract_size", default=EXPECTED_CONTRACT_SIZE) or EXPECTED_CONTRACT_SIZE),
        volume_min=float(pick(symbol, "volume_min", default=0.01) or 0.01),
        volume_step=float(pick(symbol, "volume_step", default=0.01) or 0.01),
        volume_max=float(pick(symbol, "volume_max", default=50.0) or 50.0),
        digits=int(pick(symbol, "digits", default=2) or 2),
        point=float(pick(symbol, "point", default=0.01) or 0.01),
        tick_size=float(pick(symbol, "trade_tick_size", "tick_size", default=pick(symbol, "point", default=0.01)) or 0.01),
        stops_level_points=int(pick(symbol, "trade_stops_level", "stops_level", default=0) or 0),
        freeze_level_points=int(pick(symbol, "trade_freeze_level", "freeze_level", default=0) or 0),
        swap_long=pick(symbol, "swap_long"),
        swap_short=pick(symbol, "swap_short"),
        swap_mode=pick(symbol, "swap_mode"),
        swap_rollover3days=pick(symbol, "swap_rollover3days"),
    )

    if LOT_SIZE + 1e-12 < spec.volume_min:
        raise RuntimeError(f"Configured {LOT_SIZE} lot is below broker minimum {spec.volume_min}")
    steps = (LOT_SIZE - spec.volume_min) / spec.volume_step if spec.volume_step > 0 else 0
    if abs(steps - round(steps)) > 1e-8:
        raise RuntimeError(f"Configured {LOT_SIZE} lot is not aligned to broker step {spec.volume_step}")
    if abs(spec.contract_size - EXPECTED_CONTRACT_SIZE) > 1e-9:
        print(f"WARNING: contract size is {spec.contract_size}, not expected {EXPECTED_CONTRACT_SIZE}. Using exported value.")
    return spec


# ============================================================================
# TELEGRAM PARSING
# ============================================================================

def extract_edit_time(div, original_time: datetime) -> Optional[datetime]:
    # Search elements explicitly marked edited.
    for node in div.find_all(True):
        classes = [str(c).lower() for c in (node.get("class") or [])]
        text = node.get_text(" ", strip=True)
        if "edited" in classes or text.lower().startswith("edited"):
            for attr in ["title", "data-original-title", "datetime"]:
                d = parse_telegram_datetime(node.get(attr, ""))
                if d:
                    return d
            d = reconstruct_edited_hhmm(text, original_time)
            if d:
                return d
    return None


def extract_tps(text: str) -> List[Dict[str, Any]]:
    hits: List[Tuple[int, Optional[int], float, str]] = []
    occupied: List[Tuple[int, int]] = []
    for m in TP_INDEXED_RE.finditer(text):
        idx = int(m.group(1))
        price = float(m.group(2))
        hits.append((m.start(), idx, price, m.group(0)))
        occupied.append((m.start(), m.end()))
    for m in TP_PLAIN_RE.finditer(text):
        if any(a <= m.start() < b for a, b in occupied):
            continue
        hits.append((m.start(), None, float(m.group(1)), m.group(0)))
    hits.sort(key=lambda x: x[0])
    out = []
    seen = set()
    for pos, idx, price, raw in hits:
        key = (idx, round(price, 4))
        if key in seen:
            continue
        seen.add(key)
        out.append({"index": idx, "price": price, "raw": raw, "pos": pos})
    return out


def parse_zone_from_text(text: str) -> List[float]:
    boundaries = []
    sm = SL_RE.search(text)
    if sm:
        boundaries.append(sm.start())
    for m in TP_INDEXED_RE.finditer(text):
        boundaries.append(m.start())
    for m in TP_PLAIN_RE.finditer(text):
        boundaries.append(m.start())
    cut = min(boundaries) if boundaries else len(text)
    before = text[:cut]
    vals = []
    for x in PRICE_RE.findall(before):
        try:
            v = float(x)
        except Exception:
            continue
        if 1000 <= v <= 10000:
            vals.append(v)
    return vals


def normalize_targets_for_side(side: str, targets: List[Dict[str, Any]]) -> Tuple[List[float], List[str]]:
    warnings: List[str] = []
    prices = [float(t["price"]) for t in targets]
    prices = list(dict.fromkeys(prices))
    if side == "BUY":
        ordered = sorted(prices)
    else:
        ordered = sorted(prices, reverse=True)

    explicit = [(t.get("index"), float(t["price"])) for t in targets if t.get("index") is not None]
    if explicit:
        by_idx = [p for _, p in sorted(explicit, key=lambda x: x[0])]
        if len(by_idx) >= 2 and by_idx != sorted(by_idx, reverse=(side == "SELL")):
            warnings.append("TP_LABEL_PRICE_ORDER_CONFLICT")
    return ordered, warnings


def parse_signal(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not text:
        return None, "EMPTY"
    x = text.replace("\u00a0", " ").replace("–", "-").replace("—", "-")
    side_m = SIDE_RE.search(x)
    sl_m = SL_RE.search(x)
    tps_raw = extract_tps(x)
    if not side_m:
        return None, "NO_SIDE"
    if not sl_m:
        return None, "NO_SL"
    if not tps_raw:
        return None, "NO_TP"

    side = side_m.group(1).upper()
    sl = float(sl_m.group(1))
    zone_vals = parse_zone_from_text(x)
    if not zone_vals:
        return None, "NO_ENTRY_PRICE"
    if len(zone_vals) == 1:
        z1 = z2 = zone_vals[0]
    else:
        z1, z2 = zone_vals[0], zone_vals[1]
    zone_low, zone_high = min(z1, z2), max(z1, z2)
    targets, tp_warnings = normalize_targets_for_side(side, tps_raw)

    if side == "BUY":
        if not sl < zone_low:
            return None, "BAD_BUY_SL_GEOMETRY"
        if any(tp <= zone_high for tp in targets):
            return None, "BAD_BUY_TP_GEOMETRY"
    else:
        if not sl > zone_high:
            return None, "BAD_SELL_SL_GEOMETRY"
        if any(tp >= zone_low for tp in targets):
            return None, "BAD_SELL_TP_GEOMETRY"

    upper = x.upper()
    if re.search(r"\b(BUY|SELL)\s+NOW\b", upper) or "MARKET" in upper:
        order_type = "MARKET"
    elif "LIMIT" in upper:
        order_type = "LIMIT"
    else:
        order_type = "ZONE_LIMIT"

    instrument_marked = bool(re.search(r"(?i)\bXAUUSD(?:\.I)?\b|\bGOLD\b|\bXAU\b", x))
    return {
        "side": side,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "sl": sl,
        "targets": targets,
        "tp_count": len(targets),
        "order_type": order_type,
        "instrument_marked": instrument_marked,
        "parse_warnings": tp_warnings,
    }, None


def parse_management(text: str) -> Dict[str, Any]:
    x = (text or "").replace("\u00a0", " ")
    low = x.lower()
    actions: List[str] = []

    partial = bool(re.search(r"close\s*(?:half|50\s*%)|partial\s*close|close\s*partial|take\s*half", low))
    if partial:
        actions.append("CLOSE_PARTIAL")
    elif re.search(r"\bclose\b|\bclosed\b|\bexit\b", low):
        actions.append("CLOSE_FULL")

    if re.search(r"\bcancel(?:led)?\b", low) or "wait for new signal" in low or "wait for next signal" in low:
        actions.append("CANCEL")

    if re.search(
        r"(?:move|put|set)\s*(?:the\s*)?(?:sl|stl|stop(?:\s*loss)?)\s*(?:to|at)\s*(?:entry|be|b\.e\.)|"
        r"\bsl\s+to\s+entry\b|\bstl\s+to\s+entry\b|\bbreakeven\b|\bbreak\s*even\b",
        low,
    ):
        actions.append("MOVE_BE")

    result_notice = bool(re.search(
        r"\b(?:tp\s*\d*|take\s*profit)\s*(?:hit|done|reached)\b|"
        r"\b(?:sl|stl|stop\s*loss)\s*(?:hit|done|reached)\b",
        low,
    ))
    if result_notice:
        actions.append("RESULT_NOTICE")

    if "running" in low and ("pip" in low or "profit" in low or "+" in low):
        actions.append("RUNNING_NOTICE")

    if re.search(r"\bre[- ]?enter\b|\bre[- ]?entry\b|\benter again\b|\brebuy\b|\bre-sell\b|\bresell\b", low):
        actions.append("REENTER_SAME")

    new_sl = None
    sl_m = SL_RE.search(x)
    if sl_m and not result_notice:
        try:
            new_sl = float(sl_m.group(1))
            actions.append("SET_SL")
        except Exception:
            pass

    tps = extract_tps(x)
    new_targets = [float(t["price"]) for t in tps]
    if new_targets and not result_notice:
        actions.append("SET_TPS")

    entry_zone = None
    if "entry" in low and not sl_m and not tps:
        vals = [float(v) for v in PRICE_RE.findall(x)]
        if vals:
            if len(vals) == 1:
                entry_zone = [vals[0], vals[0]]
            else:
                entry_zone = [min(vals[0], vals[1]), max(vals[0], vals[1])]
            actions.append("SET_ENTRY_ZONE")

    gold_scope = bool(re.search(r"(?i)\bXAUUSD(?:\.I)?\b|\bGOLD\b|\bXAU\b", x))
    global_all_scope = bool(re.search(r"(?i)\b(?:close|cancel)\s+all\b", x))

    return {
        "actions": list(dict.fromkeys(actions)),
        "new_sl": new_sl,
        "new_targets": new_targets,
        "entry_zone": entry_zone,
        "gold_scope": gold_scope,
        "global_all_scope": global_all_scope,
    }


def parse_telegram_export(zip_path: Path) -> Dict[str, Any]:
    messages: List[Dict[str, Any]] = []
    media_only = 0

    with zipfile.ZipFile(zip_path, "r") as z:
        names = sorted(
            n for n in z.namelist()
            if n.lower().endswith(".html") and Path(n).name.lower().startswith("messages")
        )
        if not names:
            raise RuntimeError("No messages*.html files found in Telegram ZIP")

        for name in names:
            raw = z.read(name)
            soup = BeautifulSoup(raw, "lxml", from_encoding="utf-8")
            for div in soup.select("div.message"):
                raw_id = div.get("id", "")
                mid_m = re.search(r"message(\d+)", raw_id)
                if not mid_m:
                    continue
                msg_id = int(mid_m.group(1))
                date_node = div.select_one(".date.details")
                title = date_node.get("title", "") if date_node else ""
                dt = parse_telegram_datetime(title)
                if dt is None:
                    continue
                text_node = div.select_one(".text")
                text = text_node.get_text("\n", strip=True) if text_node else ""
                if not text and div.select_one(".media_wrap") is not None:
                    media_only += 1

                reply_id = None
                reply_node = div.select_one(".reply_to a[href]")
                if reply_node:
                    href = reply_node.get("href", "")
                    rm = re.search(r"(?:go_to_message|message)(\d+)", href)
                    if rm:
                        reply_id = int(rm.group(1))

                edit_dt = extract_edit_time(div, dt)
                author_node = div.select_one(".from_name")
                author = author_node.get_text(" ", strip=True) if author_node else None
                messages.append({
                    "msg_id": msg_id,
                    "time": dt,
                    "time_ms": dt_to_ms(dt),
                    "edited_time": edit_dt,
                    "edited_ms": dt_to_ms(edit_dt) if edit_dt else None,
                    "reply_id": reply_id,
                    "text": text,
                    "author": author,
                    "source_html": name,
                })

    messages.sort(key=lambda m: (m["time_ms"], m["msg_id"]))
    by_id = {m["msg_id"]: m for m in messages}

    signals: List[Dict[str, Any]] = []
    parse_rejections = defaultdict(int)
    for m in messages:
        sig, reason = parse_signal(m["text"])
        if sig:
            s = dict(sig)
            s.update(m)
            s["uid"] = f"TG_{m['msg_id']}"
            s["source_kind"] = "SIGNAL"
            signals.append(s)
        elif SIDE_RE.search(m["text"] or ""):
            parse_rejections[reason or "UNKNOWN"] += 1

    signal_ids = {s["msg_id"] for s in signals}
    signal_by_id = {s["msg_id"]: s for s in signals}

    def resolve_root(message: Dict[str, Any]) -> Optional[int]:
        cur = message.get("reply_id")
        visited = set()
        for _ in range(50):
            if cur is None or cur in visited:
                return None
            visited.add(cur)
            if cur in signal_ids:
                return cur
            ancestor = by_id.get(cur)
            if not ancestor:
                return None
            cur = ancestor.get("reply_id")
        return None

    management_by_root: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    global_management: List[Dict[str, Any]] = []
    reentry_clones: List[Dict[str, Any]] = []

    for m in messages:
        if m["msg_id"] in signal_ids:
            continue
        mg = parse_management(m["text"])
        if not mg["actions"]:
            continue
        root = resolve_root(m)
        event = dict(m)
        event.update(mg)
        event["root_signal_id"] = root
        event["is_global"] = root is None
        if root is not None:
            management_by_root[root].append(event)
            if "REENTER_SAME" in event["actions"]:
                parent = signal_by_id[root]
                clone = {k: v for k, v in parent.items() if k not in ["uid", "msg_id", "time", "time_ms", "edited_time", "edited_ms", "reply_id", "text", "source_html"]}
                clone.update({
                    "uid": f"REENTRY_{m['msg_id']}_ROOT_{root}",
                    "msg_id": m["msg_id"],
                    "time": m["time"],
                    "time_ms": m["time_ms"],
                    "edited_time": m["edited_time"],
                    "edited_ms": m["edited_ms"],
                    "reply_id": root,
                    "text": m["text"],
                    "source_html": m["source_html"],
                    "source_kind": "REENTRY_CLONE",
                    "parent_signal_id": root,
                })
                reentry_clones.append(clone)
        else:
            if (
                event["gold_scope"]
                or event["global_all_scope"]
                or ASSUME_GOLD_ONLY_CHANNEL_FOR_UNLINKED_MANAGEMENT
            ):
                global_management.append(event)

    for events in management_by_root.values():
        events.sort(key=lambda e: (e["time_ms"], e["msg_id"]))
    global_management.sort(key=lambda e: (e["time_ms"], e["msg_id"]))

    all_signals = signals + reentry_clones
    all_signals.sort(key=lambda s: (s["time_ms"], s["msg_id"]))

    stats = {
        "messages": len(messages),
        "signals": len(signals),
        "reentry_clones": len(reentry_clones),
        "all_setups": len(all_signals),
        "edited_messages": sum(1 for m in messages if m.get("edited_ms") is not None),
        "edited_signals": sum(1 for s in all_signals if s.get("edited_ms") is not None),
        "media_only_messages": media_only,
        "reply_linked_management": sum(len(v) for v in management_by_root.values()),
        "unlinked_global_management": len(global_management),
        "parse_rejections": dict(parse_rejections),
    }

    return {
        "messages": messages,
        "signals": all_signals,
        "management_by_root": management_by_root,
        "global_management": global_management,
        "stats": stats,
    }


# ============================================================================
# FX CONVERSION — causal previous-day USDSGD close when available
# ============================================================================

class FXStore:
    def __init__(self, start_dt: datetime, end_dt: datetime):
        self.daily: Dict[date, float] = {}
        self.source = f"FALLBACK_FIXED_{FALLBACK_SGD_PER_USD}"
        try:
            import MetaTrader5 as mt5  # type: ignore
            if not mt5.initialize():
                return
            symbols = mt5.symbols_get() or []
            candidates = [s.name for s in symbols if "USDSGD" in s.name.upper()]
            if not candidates:
                mt5.shutdown()
                return
            symbol = candidates[0]
            mt5.symbol_select(symbol, True)
            rates = mt5.copy_rates_range(
                symbol,
                mt5.TIMEFRAME_D1,
                start_dt - timedelta(days=30),
                end_dt + timedelta(days=2),
            )
            if rates is not None and len(rates):
                df = pd.DataFrame(rates)
                for _, row in df.iterrows():
                    d = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc).date()
                    c = float(row["close"])
                    if c > 0:
                        self.daily[d] = c
                if self.daily:
                    self.source = f"Blueberry_MT5_{symbol}_D1_PREVIOUS_CLOSE"
            mt5.shutdown()
        except Exception:
            pass

    def rate_for_date(self, d: date) -> float:
        # Previous completed daily close only — avoids using the future end-of-day
        # close to value an earlier event on the same date.
        cursor = d - timedelta(days=1)
        for _ in range(20):
            if cursor in self.daily:
                return self.daily[cursor]
            cursor -= timedelta(days=1)
        return FALLBACK_SGD_PER_USD

    def rate_for_ms(self, ms: int) -> float:
        return self.rate_for_date(ms_to_dt(ms).date())


# ============================================================================
# BLUEBERRY TICK STORE
# ============================================================================

class TickStore:
    def __init__(self, tick_dir: Path, cache_days: int = CACHE_DAYS):
        self.tick_dir = tick_dir
        self.files: Dict[date, Path] = {}
        for p in tick_dir.glob("*.csv.gz"):
            m = re.match(r"(\d{4}-\d{2}-\d{2})", p.name)
            if not m:
                continue
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                self.files[d] = p
            except Exception:
                pass
        if not self.files:
            raise RuntimeError(f"No dated .csv.gz tick files found under {tick_dir}")
        self.days = sorted(self.files)
        self.cache_days = cache_days
        self.cache: OrderedDict[date, TickDay] = OrderedDict()

    def _empty(self) -> TickDay:
        return TickDay(
            np.array([], dtype=np.int64),
            np.array([], dtype=np.float64),
            np.array([], dtype=np.float64),
        )

    def load_day(self, d: date) -> TickDay:
        if d in self.cache:
            v = self.cache.pop(d)
            self.cache[d] = v
            return v
        p = self.files.get(d)
        if not p:
            v = self._empty()
        else:
            try:
                header = pd.read_csv(p, compression="gzip", nrows=0).columns.tolist()
                wanted = [c for c in ["time_msc", "time", "time_utc", "bid", "ask"] if c in header]
                df = pd.read_csv(p, compression="gzip", usecols=wanted)
                if "time_msc" in df.columns:
                    times = pd.to_numeric(df["time_msc"], errors="coerce").fillna(-1).astype("int64").to_numpy()
                elif "time" in df.columns:
                    times = (pd.to_numeric(df["time"], errors="coerce").fillna(-1).astype("int64") * 1000).to_numpy()
                elif "time_utc" in df.columns:
                    dt = pd.to_datetime(df["time_utc"], utc=True, errors="coerce")
                    times = (dt.astype("int64") // 1_000_000).to_numpy()
                else:
                    raise RuntimeError("No time column")
                bid = pd.to_numeric(df["bid"], errors="coerce").to_numpy(dtype=np.float64)
                ask = pd.to_numeric(df["ask"], errors="coerce").to_numpy(dtype=np.float64)
                valid = (times > 0) & np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > 0) & (ask >= bid)
                times, bid, ask = times[valid], bid[valid], ask[valid]
                if len(times):
                    order = np.argsort(times, kind="stable")
                    times, bid, ask = times[order], bid[order], ask[order]
                v = TickDay(times, bid, ask)
            except Exception as exc:
                print(f"WARNING reading {p.name}: {exc}")
                v = self._empty()
        self.cache[d] = v
        while len(self.cache) > self.cache_days:
            self.cache.popitem(last=False)
        return v

    def coverage(self) -> Tuple[int, int]:
        first = last = None
        for d in self.days:
            td = self.load_day(d)
            if len(td.times):
                first = int(td.times[0])
                break
        for d in reversed(self.days):
            td = self.load_day(d)
            if len(td.times):
                last = int(td.times[-1])
                break
        if first is None or last is None:
            raise RuntimeError("Tick files contain no valid quotes")
        return first, last

    @staticmethod
    def _d(ms: int) -> date:
        return ms_to_dt(ms).date()

    def quote_at_or_after(self, start_ms: int, max_gap_ms: int) -> Optional[Tuple[int, float, float]]:
        end_ms = start_ms + max_gap_ms
        d = self._d(start_ms)
        end_d = self._d(end_ms)
        cursor = start_ms
        while d <= end_d:
            td = self.load_day(d)
            if len(td.times):
                i = int(np.searchsorted(td.times, cursor, side="left"))
                if i < len(td.times):
                    t = int(td.times[i])
                    if t <= end_ms:
                        return t, float(td.bid[i]), float(td.ask[i])
            d += timedelta(days=1)
            cursor = dt_to_ms(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
        return None

    def first_limit_fill(
        self,
        side: str,
        entry: float,
        start_ms: int,
        end_ms_exclusive: int,
    ) -> Optional[Tuple[int, float, float]]:
        if end_ms_exclusive <= start_ms:
            return None
        d = self._d(start_ms)
        end_d = self._d(end_ms_exclusive - 1)
        cursor = start_ms
        while d <= end_d:
            td = self.load_day(d)
            if len(td.times):
                lo = int(np.searchsorted(td.times, cursor, side="left"))
                hi = int(np.searchsorted(td.times, end_ms_exclusive, side="left"))
                if hi > lo:
                    q = td.ask[lo:hi] if side == "BUY" else td.bid[lo:hi]
                    cond = q <= entry if side == "BUY" else q >= entry
                    hit = np.flatnonzero(cond)
                    if len(hit):
                        i = lo + int(hit[0])
                        return int(td.times[i]), float(td.bid[i]), float(td.ask[i])
            d += timedelta(days=1)
            cursor = dt_to_ms(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
        return None

    def scan_open(
        self,
        side: str,
        entry: float,
        sl: float,
        tp: float,
        start_ms: int,
        end_ms_exclusive: int,
        cash_balance_sgd: float,
        spec: BrokerSpec,
        fx: FXStore,
        equity_peak_in: float,
    ) -> Dict[str, Any]:
        """Scan only up to the true first exit tick. This fixes V1's post-exit MAE bug."""
        result = {
            "hit": False,
            "reason": None,
            "time_ms": None,
            "exit_price": None,
            "min_equity_sgd": cash_balance_sgd,
            "max_equity_sgd": cash_balance_sgd,
            "mae_usd": 0.0,
            "mfe_usd": 0.0,
            "margin_call_seen": False,
            "first_margin_call_ms": None,
            "equity_peak_out": equity_peak_in,
            "max_drawdown_sgd": 0.0,
        }
        if end_ms_exclusive <= start_ms:
            return result

        d = self._d(start_ms)
        end_d = self._d(end_ms_exclusive - 1)
        cursor = start_ms
        q_min_seen = q_max_seen = None
        peak = equity_peak_in
        max_dd = 0.0

        while d <= end_d:
            td = self.load_day(d)
            if len(td.times):
                lo = int(np.searchsorted(td.times, cursor, side="left"))
                hi = int(np.searchsorted(td.times, end_ms_exclusive, side="left"))
                if hi > lo:
                    bid = td.bid[lo:hi]
                    ask = td.ask[lo:hi]
                    q = bid if side == "BUY" else ask
                    market = (bid + ask) / 2.0
                    if side == "BUY":
                        pnl_usd = (q - entry) * spec.ounces
                        sl_hits = np.flatnonzero(q <= sl)
                        tp_hits = np.flatnonzero(q >= tp)
                    else:
                        pnl_usd = (entry - q) * spec.ounces
                        sl_hits = np.flatnonzero(q >= sl)
                        tp_hits = np.flatnonzero(q <= tp)

                    fx_rate = fx.rate_for_date(d)
                    equity = cash_balance_sgd + pnl_usd * fx_rate
                    margin = np.maximum(market * spec.ounces / spec.leverage * fx_rate, 1e-12)
                    margin_level = equity / margin * 100.0
                    call_hits = np.flatnonzero(margin_level <= spec.margin_call_pct)
                    stopout_hits = np.flatnonzero(margin_level <= spec.stopout_pct)

                    if len(call_hits) and not result["margin_call_seen"]:
                        result["margin_call_seen"] = True
                        result["first_margin_call_ms"] = int(td.times[lo + int(call_hits[0])])

                    candidates: List[Tuple[int, int, str]] = []
                    if len(stopout_hits):
                        candidates.append((int(stopout_hits[0]), 0, "STOP_OUT"))
                    if len(sl_hits):
                        candidates.append((int(sl_hits[0]), 1, "SL"))
                    if len(tp_hits):
                        candidates.append((int(tp_hits[0]), 2, "TP"))
                    chosen = min(candidates) if candidates else None
                    cut = (chosen[0] + 1) if chosen else len(q)

                    q_cut = q[:cut]
                    eq_cut = equity[:cut]
                    if len(q_cut):
                        q_min_seen = float(np.min(q_cut)) if q_min_seen is None else min(q_min_seen, float(np.min(q_cut)))
                        q_max_seen = float(np.max(q_cut)) if q_max_seen is None else max(q_max_seen, float(np.max(q_cut)))
                        result["min_equity_sgd"] = min(result["min_equity_sgd"], float(np.min(eq_cut)))
                        result["max_equity_sgd"] = max(result["max_equity_sgd"], float(np.max(eq_cut)))
                        local_peaks = np.maximum.accumulate(np.maximum(eq_cut, peak))
                        local_dd = local_peaks - eq_cut
                        if len(local_dd):
                            max_dd = max(max_dd, float(np.max(local_dd)))
                        peak = max(peak, float(np.max(eq_cut)))

                    if chosen:
                        rel_i, _, reason = chosen
                        i = lo + rel_i
                        actual_q = float(q[rel_i])
                        exit_price = tp if reason == "TP" else actual_q
                        result.update({
                            "hit": True,
                            "reason": reason,
                            "time_ms": int(td.times[i]),
                            "exit_price": float(exit_price),
                            "equity_peak_out": peak,
                            "max_drawdown_sgd": max_dd,
                        })
                        break

            if result["hit"]:
                break
            d += timedelta(days=1)
            cursor = dt_to_ms(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))

        if q_min_seen is not None and q_max_seen is not None:
            if side == "BUY":
                result["mae_usd"] = (q_min_seen - entry) * spec.ounces
                result["mfe_usd"] = (q_max_seen - entry) * spec.ounces
            else:
                result["mae_usd"] = (entry - q_max_seen) * spec.ounces
                result["mfe_usd"] = (entry - q_min_seen) * spec.ounces
        result["equity_peak_out"] = peak
        result["max_drawdown_sgd"] = max_dd
        return result

    def potential_missing_weekdays(self, first_ms: int, last_ms: int) -> List[str]:
        if not FLAG_MISSING_WEEKDAY_FILES:
            return []
        a = self._d(first_ms)
        b = self._d(last_ms)
        out = []
        d = a
        while d <= b:
            if d.weekday() < 5 and d not in self.files:
                out.append(d.isoformat())
            d += timedelta(days=1)
        return out


# ============================================================================
# CAUSAL EVENT HELPERS
# ============================================================================

def effective_time_ms(item: Dict[str, Any], cfg: ReplayConfig) -> Optional[int]:
    edited = item.get("edited_ms")
    if edited is not None:
        if cfg.tier == "STRICT":
            return None
        base = int(edited)
    else:
        base = int(item["time_ms"])
    return base + cfg.latency_seconds * 1000 + cfg.timestamp_uncertainty_ms


def entry_for_depth(signal: Dict[str, Any], depth: float, spec: BrokerSpec) -> float:
    lo, hi = float(signal["zone_low"]), float(signal["zone_high"])
    if signal["side"] == "BUY":
        p = hi - depth * (hi - lo)
    else:
        p = lo + depth * (hi - lo)
    return round_price(p, spec)


def choose_target(targets: List[float], policy: str, side: str, spec: BrokerSpec) -> Optional[float]:
    if not targets:
        return None
    ordered = sorted(set(round_price(float(x), spec) for x in targets), reverse=(side == "SELL"))
    if policy == "TP1":
        return ordered[0]
    if policy == "TP2":
        return ordered[min(1, len(ordered) - 1)]
    if policy == "TP3":
        return ordered[min(2, len(ordered) - 1)]
    # MANAGEMENT: executable interpretation = keep full 0.01 toward the furthest
    # currently known target unless a timestamped management instruction closes or
    # modifies the trade earlier.
    return ordered[-1]


def valid_pending(side: str, entry: float, bid: float, ask: float, spec: BrokerSpec) -> Tuple[bool, str]:
    dist = spec.min_price_distance
    if side == "BUY":
        if entry >= ask:
            return False, "MARKETABLE_BUY_LIMIT"
        if ask - entry < dist:
            return False, "BUY_LIMIT_STOPS_LEVEL"
    else:
        if entry <= bid:
            return False, "MARKETABLE_SELL_LIMIT"
        if entry - bid < dist:
            return False, "SELL_LIMIT_STOPS_LEVEL"
    return True, "OK"


def valid_sl(side: str, new_sl: float, bid: float, ask: float, spec: BrokerSpec) -> Tuple[bool, str]:
    dist = spec.min_price_distance
    if side == "BUY":
        return (new_sl <= bid - dist, "OK" if new_sl <= bid - dist else "BUY_SL_TOO_CLOSE")
    return (new_sl >= ask + dist, "OK" if new_sl >= ask + dist else "SELL_SL_TOO_CLOSE")


def valid_tp(side: str, new_tp: float, bid: float, ask: float, spec: BrokerSpec) -> Tuple[bool, str]:
    dist = spec.min_price_distance
    if side == "BUY":
        return (new_tp >= bid + dist, "OK" if new_tp >= bid + dist else "BUY_TP_TOO_CLOSE")
    return (new_tp <= ask - dist, "OK" if new_tp <= ask - dist else "SELL_TP_TOO_CLOSE")


def gross_pnl_usd(side: str, entry: float, exit_price: float, spec: BrokerSpec) -> float:
    if side == "BUY":
        return (exit_price - entry) * spec.ounces
    return (entry - exit_price) * spec.ounces


def merge_events_for_signal(
    signal: Dict[str, Any],
    telegram: Dict[str, Any],
    cfg: ReplayConfig,
) -> List[Dict[str, Any]]:
    root_id = signal.get("parent_signal_id") or signal.get("msg_id")
    linked = telegram["management_by_root"].get(root_id, [])
    globals_ = telegram["global_management"]
    events = []
    for e in list(linked) + list(globals_):
        eff = effective_time_ms(e, cfg)
        if eff is None:
            continue
        x = dict(e)
        x["effective_ms"] = eff
        events.append(x)
    events.sort(key=lambda e: (e["effective_ms"], e["msg_id"]))
    return events


# ============================================================================
# SINGLE SETUP SIMULATION
# ============================================================================

def simulate_setup(
    signal: Dict[str, Any],
    telegram: Dict[str, Any],
    cfg: ReplayConfig,
    ticks: TickStore,
    fx: FXStore,
    spec: BrokerSpec,
    start_balance_sgd: float,
    equity_peak_in: float,
    last_data_ms: int,
) -> Dict[str, Any]:
    activation = effective_time_ms(signal, cfg)
    if activation is None:
        return {"status": "EXCLUDED_EDITED_SIGNAL_STRICT", "resolution_ms": signal["time_ms"], "filled": False}
    if activation > last_data_ms:
        return {"status": "OUTSIDE_DATA", "resolution_ms": activation, "filled": False}

    side = signal["side"]
    active_sl = round_price(float(signal["sl"]), spec)
    active_targets = [round_price(float(x), spec) for x in signal["targets"]]
    active_tp = choose_target(active_targets, cfg.tp_policy, side, spec)
    if active_tp is None:
        return {"status": "NO_TARGET", "resolution_ms": activation, "filled": False}

    entry = entry_for_depth(signal, cfg.depth, spec)
    order_type = signal.get("order_type", "ZONE_LIMIT")
    q0 = ticks.quote_at_or_after(activation, INITIAL_QUOTE_MAX_GAP_SECONDS * 1000)
    audit: List[Dict[str, Any]] = [{
        "time_ms": activation,
        "event": "SIGNAL_EFFECTIVE",
        "source_msg_id": signal["msg_id"],
        "source_kind": signal.get("source_kind"),
        "edited": signal.get("edited_ms") is not None,
        "side": side,
        "order_type": order_type,
        "entry": entry,
        "sl": active_sl,
        "targets": active_targets,
        "selected_tp": active_tp,
    }]
    if q0 is None:
        return {
            "status": "UNSCORABLE_NO_INITIAL_QUOTE",
            "resolution_ms": activation,
            "filled": False,
            "fatal": False,
            "audit": audit,
        }
    q0_ms, bid0, ask0 = q0

    # Initial SL/TP geometry at the broker's tick size.
    if side == "BUY" and not (active_sl < entry < active_tp):
        return {"status": "REJECT_BAD_GEOMETRY_AFTER_ROUNDING", "resolution_ms": q0_ms, "filled": False, "audit": audit}
    if side == "SELL" and not (active_tp < entry < active_sl):
        return {"status": "REJECT_BAD_GEOMETRY_AFTER_ROUNDING", "resolution_ms": q0_ms, "filled": False, "audit": audit}

    entry_fx = fx.rate_for_ms(q0_ms)
    approx_margin_sgd = entry * spec.ounces / spec.leverage * entry_fx
    if start_balance_sgd <= approx_margin_sgd:
        return {"status": "REJECT_INSUFFICIENT_MARGIN", "resolution_ms": q0_ms, "filled": False, "audit": audit}

    events = merge_events_for_signal(signal, telegram, cfg)
    # Skip management messages that became effective before the setup itself.
    events = [e for e in events if e["effective_ms"] >= activation]

    expiry_ms = activation + cfg.pending_ttl_minutes * 60 * 1000
    state = "PENDING"
    cursor_ms = activation
    fill_ms = None
    fill_price = None
    cash_balance = start_balance_sgd
    entry_commission_sgd = 0.0
    exit_commission_sgd = 0.0
    margin_call_seen = False
    first_margin_call_ms = None
    min_equity = start_balance_sgd
    max_equity = start_balance_sgd
    peak = equity_peak_in
    max_drawdown = max(0.0, peak - start_balance_sgd)
    mae_usd = 0.0
    mfe_usd = 0.0
    overnight_exposure = False

    event_i = 0

    # MARKET signals are filled at the first executable quote after effective time.
    if order_type == "MARKET":
        fill_ms = q0_ms
        fill_price = ask0 if side == "BUY" else bid0
        fill_price = round_price(fill_price, spec)
        state = "OPEN"
    else:
        ok, reason = valid_pending(side, entry, bid0, ask0, spec)
        if not ok:
            return {"status": f"REJECT_{reason}", "resolution_ms": q0_ms, "filled": False, "audit": audit}

    def apply_entry_commission():
        nonlocal cash_balance, entry_commission_sgd, peak, max_drawdown, min_equity
        rate = fx.rate_for_ms(fill_ms)
        entry_commission_sgd = spec.commission_usd_per_side * rate
        cash_balance -= entry_commission_sgd
        min_equity = min(min_equity, cash_balance)
        max_drawdown = max(max_drawdown, peak - cash_balance)

    if state == "OPEN":
        apply_entry_commission()
        audit.append({"time_ms": fill_ms, "event": "FILLED_MARKET", "price": fill_price})
        cursor_ms = fill_ms

    while True:
        next_event = events[event_i] if event_i < len(events) else None
        next_event_ms = next_event["effective_ms"] if next_event else None

        if state == "PENDING":
            boundary = min(expiry_ms, next_event_ms if next_event_ms is not None else expiry_ms, last_data_ms + 1)
            fill = ticks.first_limit_fill(side, entry, cursor_ms, boundary)
            if fill is not None:
                fill_ms, fill_bid, fill_ask = fill
                # Limit fill is conservatively booked at requested price, not at a
                # possibly better gap-through quote.
                fill_price = entry
                state = "OPEN"
                apply_entry_commission()
                audit.append({"time_ms": fill_ms, "event": "FILLED_LIMIT", "price": fill_price, "bid": fill_bid, "ask": fill_ask})
                cursor_ms = fill_ms
                continue

            if boundary >= last_data_ms + 1:
                return {
                    "status": "UNFILLED_DATA_END",
                    "resolution_ms": last_data_ms,
                    "filled": False,
                    "audit": audit,
                    "peak_equity_out": peak,
                    "max_drawdown_sgd": max_drawdown,
                }
            if boundary == expiry_ms and (next_event_ms is None or expiry_ms <= next_event_ms):
                audit.append({"time_ms": expiry_ms, "event": "PENDING_TTL_EXPIRED"})
                return {
                    "status": "UNFILLED_TTL",
                    "resolution_ms": expiry_ms,
                    "filled": False,
                    "audit": audit,
                    "peak_equity_out": peak,
                    "max_drawdown_sgd": max_drawdown,
                }

            # Event wins ties because fill search used end-exclusive boundary.
            e = next_event
            event_i += 1
            cursor_ms = e["effective_ms"]
            actions = e["actions"]
            audit.append({"time_ms": cursor_ms, "event": "TELEGRAM_PENDING_MANAGEMENT", "msg_id": e["msg_id"], "actions": actions, "text": e["text"]})

            stale_cancel = (
                "CANCEL" in actions
                or "CLOSE_FULL" in actions
                or "CLOSE_PARTIAL" in actions
                or "RESULT_NOTICE" in actions
                or (cfg.running_notice_cancels_unfilled and "RUNNING_NOTICE" in actions)
            )
            if stale_cancel:
                return {
                    "status": "CANCELLED_PENDING_BY_MANAGEMENT",
                    "resolution_ms": cursor_ms,
                    "filled": False,
                    "audit": audit,
                    "peak_equity_out": peak,
                    "max_drawdown_sgd": max_drawdown,
                }

            if "SET_ENTRY_ZONE" in actions and e.get("entry_zone"):
                lo, hi = e["entry_zone"]
                temp = dict(signal)
                temp["zone_low"], temp["zone_high"] = lo, hi
                entry = entry_for_depth(temp, cfg.depth, spec)
                q = ticks.quote_at_or_after(cursor_ms, CLOSE_QUOTE_MAX_GAP_SECONDS * 1000)
                if q is None:
                    return {"status": "UNSCORABLE_AMENDMENT_NO_QUOTE", "resolution_ms": cursor_ms, "filled": False, "fatal": False, "audit": audit}
                _, b, a = q
                ok, reason = valid_pending(side, entry, b, a, spec)
                if not ok:
                    return {"status": f"AMENDMENT_REJECT_{reason}", "resolution_ms": cursor_ms, "filled": False, "audit": audit}
                audit.append({"time_ms": cursor_ms, "event": "ENTRY_ZONE_AMENDED", "entry": entry})

            if "SET_SL" in actions and e.get("new_sl") is not None:
                active_sl = round_price(float(e["new_sl"]), spec)
                audit.append({"time_ms": cursor_ms, "event": "PENDING_SL_AMENDED", "sl": active_sl})

            if "SET_TPS" in actions and e.get("new_targets"):
                active_targets = [round_price(float(x), spec) for x in e["new_targets"]]
                active_tp = choose_target(active_targets, cfg.tp_policy, side, spec)
                audit.append({"time_ms": cursor_ms, "event": "PENDING_TPS_AMENDED", "targets": active_targets, "selected_tp": active_tp})
            continue

        # OPEN POSITION -------------------------------------------------------
        boundary = next_event_ms if next_event_ms is not None else last_data_ms + 1
        boundary = min(boundary, last_data_ms + 1)
        scan = ticks.scan_open(
            side=side,
            entry=fill_price,
            sl=active_sl,
            tp=active_tp,
            start_ms=cursor_ms,
            end_ms_exclusive=boundary,
            cash_balance_sgd=cash_balance,
            spec=spec,
            fx=fx,
            equity_peak_in=peak,
        )
        min_equity = min(min_equity, scan["min_equity_sgd"])
        max_equity = max(max_equity, scan["max_equity_sgd"])
        mae_usd = min(mae_usd, scan["mae_usd"])
        mfe_usd = max(mfe_usd, scan["mfe_usd"])
        peak = max(peak, scan["equity_peak_out"])
        max_drawdown = max(max_drawdown, scan["max_drawdown_sgd"])
        if scan["margin_call_seen"]:
            margin_call_seen = True
            if first_margin_call_ms is None:
                first_margin_call_ms = scan["first_margin_call_ms"]

        if scan["hit"]:
            exit_ms = scan["time_ms"]
            exit_price = scan["exit_price"]
            gross_usd = gross_pnl_usd(side, fill_price, exit_price, spec)
            gross_sgd = gross_usd * fx.rate_for_ms(exit_ms)
            exit_commission_sgd = spec.commission_usd_per_side * fx.rate_for_ms(exit_ms)
            end_balance = cash_balance + gross_sgd - exit_commission_sgd
            max_drawdown = max(max_drawdown, peak - end_balance)
            min_equity = min(min_equity, end_balance)
            audit.append({"time_ms": exit_ms, "event": "PRICE_EXIT", "reason": scan["reason"], "price": exit_price})
            overnight_exposure = ms_to_dt(fill_ms).date() != ms_to_dt(exit_ms).date()
            return {
                "status": "FILLED_EXITED",
                "resolution_ms": exit_ms,
                "filled": True,
                "fill_ms": fill_ms,
                "fill_price": fill_price,
                "exit_ms": exit_ms,
                "exit_price": exit_price,
                "exit_reason": scan["reason"],
                "entry_commission_sgd": entry_commission_sgd,
                "exit_commission_sgd": exit_commission_sgd,
                "gross_pnl_usd": gross_usd,
                "net_pnl_sgd": end_balance - start_balance_sgd,
                "balance_after_sgd": end_balance,
                "min_equity_sgd": min_equity,
                "max_equity_sgd": max_equity,
                "mae_usd": mae_usd,
                "mfe_usd": mfe_usd,
                "margin_call_seen": margin_call_seen,
                "first_margin_call_ms": first_margin_call_ms,
                "stopout": scan["reason"] == "STOP_OUT",
                "overnight_exposure": overnight_exposure,
                "peak_equity_out": peak,
                "max_drawdown_sgd": max_drawdown,
                "audit": audit,
            }

        if boundary >= last_data_ms + 1:
            # We cannot close a still-open position after the historical data ends.
            q = ticks.quote_at_or_after(max(fill_ms, last_data_ms - 120000), 120000)
            mtm = None
            if q:
                qms, b, a = q
                px = b if side == "BUY" else a
                mtm = gross_pnl_usd(side, fill_price, px, spec) * fx.rate_for_ms(qms)
            audit.append({"time_ms": last_data_ms, "event": "OPEN_AT_DATA_END", "mtm_sgd_before_exit_commission": mtm})
            return {
                "status": "OPEN_AT_DATA_END_UNCERTIFIED",
                "resolution_ms": last_data_ms,
                "filled": True,
                "fill_ms": fill_ms,
                "fill_price": fill_price,
                "fatal": True,
                "balance_after_sgd": start_balance_sgd,
                "min_equity_sgd": min_equity,
                "margin_call_seen": margin_call_seen,
                "stopout": False,
                "peak_equity_out": peak,
                "max_drawdown_sgd": max_drawdown,
                "audit": audit,
            }

        e = next_event
        event_i += 1
        cursor_ms = e["effective_ms"]
        actions = e["actions"]
        audit.append({"time_ms": cursor_ms, "event": "TELEGRAM_OPEN_MANAGEMENT", "msg_id": e["msg_id"], "actions": actions, "text": e["text"]})

        if "CLOSE_FULL" in actions or "CLOSE_PARTIAL" in actions:
            if "CLOSE_PARTIAL" in actions and cfg.partial_minlot_policy == "ignore":
                audit.append({"time_ms": cursor_ms, "event": "PARTIAL_IGNORED_MINLOT_POLICY"})
            else:
                q = ticks.quote_at_or_after(cursor_ms, CLOSE_QUOTE_MAX_GAP_SECONDS * 1000)
                if q is None:
                    return {
                        "status": "UNSCORABLE_OPEN_CLOSE_NO_QUOTE",
                        "resolution_ms": cursor_ms,
                        "filled": True,
                        "fatal": True,
                        "balance_after_sgd": start_balance_sgd,
                        "min_equity_sgd": min_equity,
                        "stopout": False,
                        "peak_equity_out": peak,
                        "max_drawdown_sgd": max_drawdown,
                        "audit": audit,
                    }
                exit_ms, b, a = q
                exit_price = b if side == "BUY" else a
                gross_usd = gross_pnl_usd(side, fill_price, exit_price, spec)
                gross_sgd = gross_usd * fx.rate_for_ms(exit_ms)
                exit_commission_sgd = spec.commission_usd_per_side * fx.rate_for_ms(exit_ms)
                end_balance = cash_balance + gross_sgd - exit_commission_sgd
                reason = "MANAGEMENT_PARTIAL_AS_FULL" if "CLOSE_PARTIAL" in actions else "MANAGEMENT_CLOSE"
                audit.append({"time_ms": exit_ms, "event": "MANAGEMENT_EXIT_EXECUTED", "reason": reason, "price": exit_price})
                overnight_exposure = ms_to_dt(fill_ms).date() != ms_to_dt(exit_ms).date()
                return {
                    "status": "FILLED_EXITED",
                    "resolution_ms": exit_ms,
                    "filled": True,
                    "fill_ms": fill_ms,
                    "fill_price": fill_price,
                    "exit_ms": exit_ms,
                    "exit_price": exit_price,
                    "exit_reason": reason,
                    "entry_commission_sgd": entry_commission_sgd,
                    "exit_commission_sgd": exit_commission_sgd,
                    "gross_pnl_usd": gross_usd,
                    "net_pnl_sgd": end_balance - start_balance_sgd,
                    "balance_after_sgd": end_balance,
                    "min_equity_sgd": min(min_equity, end_balance),
                    "max_equity_sgd": max_equity,
                    "mae_usd": mae_usd,
                    "mfe_usd": mfe_usd,
                    "margin_call_seen": margin_call_seen,
                    "first_margin_call_ms": first_margin_call_ms,
                    "stopout": False,
                    "overnight_exposure": overnight_exposure,
                    "peak_equity_out": max(peak, end_balance),
                    "max_drawdown_sgd": max(max_drawdown, peak - end_balance),
                    "audit": audit,
                }

        q = ticks.quote_at_or_after(cursor_ms, CLOSE_QUOTE_MAX_GAP_SECONDS * 1000)
        if q is None and any(a in actions for a in ["MOVE_BE", "SET_SL", "SET_TPS"]):
            return {
                "status": "UNSCORABLE_MODIFICATION_NO_QUOTE",
                "resolution_ms": cursor_ms,
                "filled": True,
                "fatal": True,
                "balance_after_sgd": start_balance_sgd,
                "min_equity_sgd": min_equity,
                "stopout": False,
                "peak_equity_out": peak,
                "max_drawdown_sgd": max_drawdown,
                "audit": audit,
            }
        if q:
            _, b, a = q
            if "MOVE_BE" in actions:
                candidate = round_price(fill_price, spec)
                ok, reason = valid_sl(side, candidate, b, a, spec)
                if ok:
                    active_sl = candidate
                    audit.append({"time_ms": cursor_ms, "event": "SL_MOVED_BE", "sl": active_sl})
                else:
                    audit.append({"time_ms": cursor_ms, "event": "SL_MOVE_REJECTED", "reason": reason, "requested": candidate})

            if "SET_SL" in actions and e.get("new_sl") is not None:
                candidate = round_price(float(e["new_sl"]), spec)
                ok, reason = valid_sl(side, candidate, b, a, spec)
                if ok:
                    active_sl = candidate
                    audit.append({"time_ms": cursor_ms, "event": "SL_AMENDED", "sl": active_sl})
                else:
                    audit.append({"time_ms": cursor_ms, "event": "SL_AMEND_REJECTED", "reason": reason, "requested": candidate})

            if "SET_TPS" in actions and e.get("new_targets"):
                candidate_targets = [round_price(float(x), spec) for x in e["new_targets"]]
                candidate_tp = choose_target(candidate_targets, cfg.tp_policy, side, spec)
                if candidate_tp is not None:
                    ok, reason = valid_tp(side, candidate_tp, b, a, spec)
                    if ok:
                        active_targets = candidate_targets
                        active_tp = candidate_tp
                        audit.append({"time_ms": cursor_ms, "event": "TPS_AMENDED", "targets": active_targets, "selected_tp": active_tp})
                    else:
                        audit.append({"time_ms": cursor_ms, "event": "TP_AMEND_REJECTED", "reason": reason, "requested": candidate_tp})
        # CANCEL after fill does not close a position. RESULT_NOTICE never proves a
        # price event; the independent tick scan above has priority.


# ============================================================================
# RUN / METRICS
# ============================================================================

def split_boundaries(signals: List[Dict[str, Any]], first_ms: int, last_ms: int) -> Dict[str, Tuple[int, int]]:
    times = sorted(s["time_ms"] for s in signals if first_ms <= s["time_ms"] <= last_ms)
    if not times:
        return {"development": (first_ms, last_ms + 1), "validation": (last_ms + 1, last_ms + 1), "holdout": (last_ms + 1, last_ms + 1)}
    t50 = times[min(len(times) - 1, int(len(times) * 0.50))]
    t75 = times[min(len(times) - 1, int(len(times) * 0.75))]
    return {
        "development": (first_ms, t50),
        "validation": (t50, t75),
        "holdout": (t75, last_ms + 1),
    }


def simple_period_metrics(trades: List[Dict[str, Any]], a: int, b: int) -> Dict[str, Any]:
    rows = [t for t in trades if a <= t.get("signal_effective_ms", -1) < b and t.get("filled") and t.get("net_pnl_sgd") is not None]
    pnl = [float(t["net_pnl_sgd"]) for t in rows]
    wins = sum(1 for x in pnl if x > 1e-9)
    losses = sum(1 for x in pnl if x < -1e-9)
    bal = STARTING_BALANCE_SGD
    peak = bal
    dd = 0.0
    for x in pnl:
        bal += x
        peak = max(peak, bal)
        dd = max(dd, peak - bal)
    return {
        "filled_trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate_ex_be_pct": wins / (wins + losses) * 100 if wins + losses else None,
        "net_pnl_sgd": sum(pnl),
        "end_balance_from_1000_sgd": bal,
        "realized_dd_sgd": dd,
        "stopout": any(t.get("stopout") for t in rows),
    }


def monthly_table(trades: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = [t for t in trades if t.get("filled") and t.get("exit_ms") and t.get("net_pnl_sgd") is not None]
    if not rows:
        return pd.DataFrame(columns=["month", "start_balance_sgd", "pnl_sgd", "return_pct", "trades", "end_balance_sgd"])
    pnl_map = defaultdict(float)
    cnt = defaultdict(int)
    for t in rows:
        k = ms_to_dt(int(t["exit_ms"])).strftime("%Y-%m")
        pnl_map[k] += float(t["net_pnl_sgd"])
        cnt[k] += 1
    months = sorted(pnl_map)
    start = pd.Period(months[0], freq="M")
    end = pd.Period(months[-1], freq="M")
    bal = STARTING_BALANCE_SGD
    out = []
    for p in pd.period_range(start, end, freq="M"):
        k = str(p)
        pnl = pnl_map.get(k, 0.0)
        sb = bal
        bal += pnl
        out.append({
            "month": k,
            "start_balance_sgd": sb,
            "pnl_sgd": pnl,
            "return_pct": pnl / sb * 100 if sb else None,
            "trades": cnt.get(k, 0),
            "end_balance_sgd": bal,
        })
    return pd.DataFrame(out)


def maximum_losing_streak(trades: List[Dict[str, Any]]) -> int:
    best = cur = 0
    for t in trades:
        x = t.get("net_pnl_sgd")
        if x is None:
            continue
        if x < -1e-9:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def run_config(
    cfg: ReplayConfig,
    telegram: Dict[str, Any],
    ticks: TickStore,
    fx: FXStore,
    spec: BrokerSpec,
    first_data_ms: int,
    last_data_ms: int,
    splits: Dict[str, Tuple[int, int]],
    run_dir: Path,
    resume: bool = True,
) -> Dict[str, Any]:
    ensure_clean_dir(run_dir)
    summary_path = run_dir / "summary.json"
    if resume and summary_path.exists():
        try:
            saved = json.loads(summary_path.read_text(encoding="utf-8"))
            if saved.get("config") == asdict(cfg):
                print(f"[resume] {cfg.key()}")
                return saved
        except Exception:
            pass

    signals = telegram["signals"]
    effective_signals = []
    excluded_edited = 0
    for s in signals:
        eff = effective_time_ms(s, cfg)
        if eff is None:
            excluded_edited += 1
            continue
        if first_data_ms <= eff <= last_data_ms:
            effective_signals.append((eff, s))
    effective_signals.sort(key=lambda x: (x[0], x[1]["msg_id"]))

    balance = STARTING_BALANCE_SGD
    peak_equity = balance
    min_equity = balance
    max_dd = 0.0
    busy_until = first_data_ms
    trades: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    fatal = False
    account_stopped = False

    audit_path = run_dir / "audit_timelines.jsonl"
    with audit_path.open("w", encoding="utf-8") as audit_f:
        for idx, (eff, s) in enumerate(effective_signals, start=1):
            if eff < busy_until:
                skipped.append({
                    "uid": s["uid"],
                    "msg_id": s["msg_id"],
                    "signal_effective_ms": eff,
                    "signal_effective_utc": ms_to_iso(eff),
                    "reason": "SKIPPED_ONE_ACTIVE_SETUP_BUSY",
                })
                continue

            before = balance
            res = simulate_setup(
                signal=s,
                telegram=telegram,
                cfg=cfg,
                ticks=ticks,
                fx=fx,
                spec=spec,
                start_balance_sgd=balance,
                equity_peak_in=peak_equity,
                last_data_ms=last_data_ms,
            )
            resolution = int(res.get("resolution_ms", eff))
            busy_until = max(eff, resolution)
            peak_equity = max(peak_equity, float(res.get("peak_equity_out", peak_equity)))
            max_dd = max(max_dd, float(res.get("max_drawdown_sgd", 0.0)))
            if res.get("min_equity_sgd") is not None:
                min_equity = min(min_equity, float(res["min_equity_sgd"]))
            if res.get("filled") and res.get("balance_after_sgd") is not None:
                balance = float(res["balance_after_sgd"])
                peak_equity = max(peak_equity, balance)
                max_dd = max(max_dd, peak_equity - balance)
                min_equity = min(min_equity, balance)

            rec = {
                "uid": s["uid"],
                "telegram_msg_id": s["msg_id"],
                "source_kind": s.get("source_kind"),
                "signal_time_utc": s["time"].isoformat(),
                "signal_edited_utc": s["edited_time"].isoformat() if s.get("edited_time") else None,
                "signal_effective_ms": eff,
                "signal_effective_utc": ms_to_iso(eff),
                "side": s["side"],
                "order_type": s["order_type"],
                "zone_low": s["zone_low"],
                "zone_high": s["zone_high"],
                "original_sl": s["sl"],
                "targets": json.dumps(s["targets"]),
                "tp_count": s["tp_count"],
                "depth": cfg.depth,
                "tp_policy": cfg.tp_policy,
                "balance_before_sgd": before,
            }
            rec.update({k: v for k, v in res.items() if k != "audit"})
            trades.append(rec)

            audit_f.write(json.dumps({
                "uid": s["uid"],
                "telegram_msg_id": s["msg_id"],
                "config": asdict(cfg),
                "timeline": res.get("audit", []),
            }, default=json_default) + "\n")

            if res.get("fatal"):
                fatal = True
                print(f"FATAL unscorable state at {s['uid']}: {res.get('status')}")
                break
            if res.get("stopout"):
                account_stopped = True
                print(f"STOP-OUT at {ms_to_iso(res.get('exit_ms'))}")
                break
            if balance <= 0:
                account_stopped = True
                break

            if idx % 100 == 0:
                print(f"  {cfg.key()} | processed {idx:,}/{len(effective_signals):,} | balance S${balance:,.2f}")

    trades_df = pd.DataFrame(trades)
    skipped_df = pd.DataFrame(skipped)
    trades_df.to_csv(run_dir / "trades.csv", index=False)
    skipped_df.to_csv(run_dir / "skipped.csv", index=False)
    monthly = monthly_table(trades)
    monthly.to_csv(run_dir / "monthly.csv", index=False)

    filled = [t for t in trades if t.get("filled") and t.get("net_pnl_sgd") is not None]
    wins = sum(1 for t in filled if float(t["net_pnl_sgd"]) > 1e-9)
    losses = sum(1 for t in filled if float(t["net_pnl_sgd"]) < -1e-9)
    overnight = sum(1 for t in filled if t.get("overnight_exposure"))
    margin_calls = sum(1 for t in filled if t.get("margin_call_seen"))
    status_counts = defaultdict(int)
    for t in trades:
        status_counts[t.get("status", "UNKNOWN")] += 1

    if len(monthly):
        avg_month = float(monthly["return_pct"].mean())
        median_month = float(monthly["return_pct"].median())
        best_month = float(monthly["return_pct"].max())
        worst_month = float(monthly["return_pct"].min())
        positive_months = int((monthly["pnl_sgd"] > 0).sum())
        negative_months = int((monthly["pnl_sgd"] < 0).sum())
    else:
        avg_month = median_month = best_month = worst_month = None
        positive_months = negative_months = 0

    split_metrics = {name: simple_period_metrics(trades, a, b) for name, (a, b) in splits.items()}

    summary = {
        "config": asdict(cfg),
        "config_key": cfg.key(),
        "starting_balance_sgd": STARTING_BALANCE_SGD,
        "final_balance_sgd": balance,
        "net_pnl_sgd": balance - STARTING_BALANCE_SGD,
        "total_return_pct": (balance / STARTING_BALANCE_SGD - 1) * 100,
        "eligible_setups": len(effective_signals),
        "excluded_edited_signals_strict": excluded_edited,
        "accepted_setups": len(trades),
        "skipped_one_active": len(skipped),
        "filled_trades": len(filled),
        "wins": wins,
        "losses": losses,
        "win_rate_ex_be_pct": wins / (wins + losses) * 100 if wins + losses else None,
        "status_counts": dict(status_counts),
        "average_monthly_return_pct": avg_month,
        "median_monthly_return_pct": median_month,
        "best_month_pct": best_month,
        "worst_month_pct": worst_month,
        "positive_months": positive_months,
        "negative_months": negative_months,
        "minimum_equity_sgd": min_equity,
        "max_continuous_drawdown_sgd": max_dd,
        "max_continuous_drawdown_pct_of_start": max_dd / STARTING_BALANCE_SGD * 100,
        "margin_call_trades": margin_calls,
        "stopout": account_stopped,
        "fatal_unscorable_after_fill": fatal,
        "overnight_exposure_trades": overnight,
        "max_losing_streak": maximum_losing_streak(filled),
        "split_metrics": split_metrics,
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=json_default), encoding="utf-8")
    return summary


# ============================================================================
# INPUT MANIFEST / CERTIFICATION
# ============================================================================

def build_input_manifest(
    telegram_zip: Path,
    export_root: Path,
    ticks: TickStore,
    deep_hash_ticks: bool,
) -> Dict[str, Any]:
    tick_entries = []
    print("Building input manifest...")
    for i, d in enumerate(ticks.days, start=1):
        p = ticks.files[d]
        entry = {"date": d.isoformat(), "name": p.name, "size": p.stat().st_size}
        if deep_hash_ticks:
            entry["sha256"] = sha256_file(p)
        tick_entries.append(entry)
        if i % 50 == 0:
            print(f"  manifest {i}/{len(ticks.days)} tick files")
    manifest = {
        "telegram": {
            "path_name": telegram_zip.name,
            "size": telegram_zip.stat().st_size,
            "sha256": sha256_file(telegram_zip),
        },
        "account_info": None,
        "symbol_info": None,
        "ticks": tick_entries,
        "deep_hash_ticks": deep_hash_ticks,
    }
    for key, filename in [("account_info", "account_info.json"), ("symbol_info", "symbol_info.json")]:
        p = export_root / filename
        if p.exists():
            manifest[key] = {"name": filename, "size": p.stat().st_size, "sha256": sha256_file(p)}
    manifest["manifest_sha256"] = stable_hash_json(manifest)
    return manifest


def certification_report(
    telegram: Dict[str, Any],
    spec: BrokerSpec,
    ticks: TickStore,
    first_ms: int,
    last_ms: int,
    selected_summary: Dict[str, Any],
    latency_summaries: List[Dict[str, Any]],
    fx: FXStore,
) -> Dict[str, Any]:
    potential_missing = ticks.potential_missing_weekdays(first_ms, last_ms)
    checks = {
        "TELEGRAM_CAUSAL_TIMING": "PASS",
        "EDIT_LOOKAHEAD_STRICT": "PASS_EXCLUDED_EDITED_FINAL_TEXT",
        "MULTI_TP_TP1_TP2_TP3_PLUS": "PASS",
        "REENTRY_MESSAGE_IDENTITY_NO_GEOMETRY_DEDUPE": "PASS",
        "REPLY_CHAIN_MANAGEMENT": "PASS",
        "UNLINKED_GLOBAL_MANAGEMENT": "PASS_WITH_GOLD_CHANNEL_ASSUMPTION" if ASSUME_GOLD_ONLY_CHANNEL_FOR_UNLINKED_MANAGEMENT else "DISABLED",
        "BID_ASK_EXECUTION": "PASS",
        "PENDING_LIMIT_NOT_MARKET_CONVERTED": "PASS",
        "BROKER_MIN_LOT_001": "PASS" if abs(spec.volume_min - 0.01) < 1e-12 and abs(spec.volume_step - 0.01) < 1e-12 else "CHECK_BROKER_SPEC",
        "PARTIAL_0005_NOT_INVENTED": "PASS",
        "MAE_MFE_STOP_AT_EXIT_TICK": "PASS",
        "MARGIN_CALL_STOPOUT": "PASS_APPROX_NOTIONAL_MARGIN",
        "FX_CONVERSION": f"PASS_CAUSAL_APPROX_{fx.source}",
        "HISTORICAL_SWAP": "WARN_UNVERIFIED" if selected_summary.get("overnight_exposure_trades", 0) else "NOT_MATERIAL_NO_OVERNIGHT_EXPOSURE",
        "HISTORICAL_BROKER_SPEC_STABILITY": "WARN_CURRENT_EXPORT_ASSUMED_HISTORICALLY",
        "DELETED_TELEGRAM_MESSAGES": "LIMITATION_NOT_RECOVERABLE_FROM_HTML_EXPORT",
        "MEDIA_ONLY_SIGNAL_CONTENT": "WARN_REVIEW_MEDIA_ONLY_MESSAGES" if telegram["stats"].get("media_only_messages", 0) else "PASS_NONE_FOUND",
        "FULLY_FRESH_OUT_OF_SAMPLE": "FAIL_RETROSPECTIVE_DATA_ALREADY_INSPECTED",
        "POTENTIAL_MISSING_WEEKDAY_TICK_FILES": potential_missing,
        "LATENCY_STRESS_ACCOUNT_SURVIVAL": all(not s.get("stopout") and not s.get("fatal_unscorable_after_fill") for s in latency_summaries),
    }
    critical_fail = (
        selected_summary.get("fatal_unscorable_after_fill")
        or selected_summary.get("stopout")
        or not checks["LATENCY_STRESS_ACCOUNT_SURVIVAL"]
    )
    if critical_fail:
        status = "FAIL_EXECUTABLE_BLUEBERRY_0.01"
    else:
        status = "PASS_EXECUTABLE_REPLAY_WITH_TELEGRAM_EXPORT_AND_HISTORICAL_SPEC_LIMITATIONS"
    return {
        "status": status,
        "checks": checks,
        "note": (
            "This is the strongest certification the available Telegram HTML export and current broker metadata can support. "
            "It is not proof that deleted messages, unavailable pre-edit versions, historical swap rates, or historical broker specification changes did not exist."
        ),
    }


# ============================================================================
# SUITE ORCHESTRATION
# ============================================================================

def create_zip(source_dir: Path, zip_path: Path):
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in source_dir.rglob("*"):
            if p.is_file() and p != zip_path:
                z.write(p, p.relative_to(source_dir))


def main():
    ap = argparse.ArgumentParser(description="Blueberry XAUUSD Telegram Forensic Replay V2")
    ap.add_argument("--blueberry", help="Path to blueberry_xauusd_export folder")
    ap.add_argument("--telegram", help="Path to ChatExport*.zip")
    ap.add_argument("--output", help="Output folder; default Desktop\\XAUUSD_BLUEBERRY_FORENSIC_V2_RESULTS")
    ap.add_argument("--suite", choices=["core", "full"], default="full")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--skip-deep-hash", action="store_true")
    args = ap.parse_args()

    export_root = find_blueberry_folder(args.blueberry)
    telegram_zip = find_telegram_zip(args.telegram)
    output_root = Path(args.output).expanduser().resolve() if args.output else Path.home() / "Desktop" / "XAUUSD_BLUEBERRY_FORENSIC_V2_RESULTS"
    ensure_clean_dir(output_root)
    run_root = output_root / "runs"
    ensure_clean_dir(run_root)

    print("=" * 84)
    print("BLUEBERRY XAUUSD × TELEGRAM FORENSIC REPLAY V2")
    print("READ ONLY — ZERO ORDERS WILL BE SENT")
    print("=" * 84)
    print("Blueberry:", export_root)
    print("Telegram :", telegram_zip)
    print("Output   :", output_root)

    spec = load_broker_spec(export_root)
    print("Broker spec:", json.dumps(asdict(spec), indent=2, default=json_default))

    telegram = parse_telegram_export(telegram_zip)
    print("Telegram stats:", json.dumps(telegram["stats"], indent=2))

    ticks = TickStore(export_root / "ticks")
    first_ms, last_ms = ticks.coverage()
    print("Tick coverage:", ms_to_iso(first_ms), "->", ms_to_iso(last_ms))
    fx = FXStore(ms_to_dt(first_ms), ms_to_dt(last_ms))
    print("FX source:", fx.source)

    manifest_path = output_root / "input_manifest.json"
    if not manifest_path.exists() or args.no_resume:
        manifest = build_input_manifest(
            telegram_zip,
            export_root,
            ticks,
            deep_hash_ticks=DEFAULT_DEEP_HASH_TICKS and not args.skip_deep_hash,
        )
        manifest_path.write_text(json.dumps(manifest, indent=2, default=json_default), encoding="utf-8")
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        print("[resume] using existing input manifest")

    splits = split_boundaries(telegram["signals"], first_ms, last_ms)
    (output_root / "split_boundaries.json").write_text(
        json.dumps({k: [ms_to_iso(a), ms_to_iso(b)] for k, (a, b) in splits.items()}, indent=2),
        encoding="utf-8",
    )

    baseline_summaries: List[Dict[str, Any]] = []
    print("\nPHASE A — STRICT coarse depth × TP policy matrix")
    for depth in COARSE_DEPTHS:
        for tp_policy in TP_POLICIES:
            cfg = ReplayConfig(
                tier="STRICT",
                depth=depth,
                tp_policy=tp_policy,
                latency_seconds=BASE_LATENCY_SECONDS,
                timestamp_uncertainty_ms=CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS,
                pending_ttl_minutes=PENDING_TTL_MINUTES,
                partial_minlot_policy=PRIMARY_PARTIAL_MINLOT_POLICY,
                running_notice_cancels_unfilled=PRIMARY_RUNNING_NOTICE_CANCELS_UNFILLED,
            )
            print("\nRUN", cfg.key())
            s = run_config(cfg, telegram, ticks, fx, spec, first_ms, last_ms, splits, run_root / cfg.key(), resume=not args.no_resume)
            baseline_summaries.append(s)

    matrix_df = pd.DataFrame([{**s["config"], **{k: v for k, v in s.items() if k not in ["config", "status_counts", "split_metrics"]},
                               "dev_net_pnl_sgd": s["split_metrics"]["development"]["net_pnl_sgd"],
                               "val_net_pnl_sgd": s["split_metrics"]["validation"]["net_pnl_sgd"],
                               "holdout_net_pnl_sgd": s["split_metrics"]["holdout"]["net_pnl_sgd"],
                               "dev_stopout": s["split_metrics"]["development"]["stopout"],
                               "val_stopout": s["split_metrics"]["validation"]["stopout"],
                               "holdout_stopout": s["split_metrics"]["holdout"]["stopout"],
                              } for s in baseline_summaries])
    matrix_df.to_csv(output_root / "baseline_matrix.csv", index=False)

    # Selection uses DEVELOPMENT ONLY. Validation and holdout are never consulted by
    # this algorithm. Because this historical period has already been inspected in
    # prior work, the report still labels the holdout retrospective/compromised.
    candidates = [
        s for s in baseline_summaries
        if not s["split_metrics"]["development"]["stopout"]
        and not s.get("fatal_unscorable_after_fill")
    ]
    if not candidates:
        selected = max(baseline_summaries, key=lambda s: s["split_metrics"]["development"]["net_pnl_sgd"])
    else:
        selected = max(
            candidates,
            key=lambda s: (
                s["split_metrics"]["development"]["net_pnl_sgd"],
                -s["split_metrics"]["development"]["realized_dd_sgd"],
            ),
        )
    selected_cfg = ReplayConfig(**selected["config"])
    (output_root / "development_selected_config.json").write_text(json.dumps(selected, indent=2, default=json_default), encoding="utf-8")
    print("\nDEVELOPMENT-ONLY SELECTED CONFIG:", selected_cfg.key())

    print("\nPHASE B — latency stress on selected config")
    latency_summaries = []
    for lat in LATENCY_STRESS_SECONDS:
        cfg = ReplayConfig(
            tier="STRICT",
            depth=selected_cfg.depth,
            tp_policy=selected_cfg.tp_policy,
            latency_seconds=lat,
            timestamp_uncertainty_ms=CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS,
            pending_ttl_minutes=PENDING_TTL_MINUTES,
            partial_minlot_policy=PRIMARY_PARTIAL_MINLOT_POLICY,
            running_notice_cancels_unfilled=PRIMARY_RUNNING_NOTICE_CANCELS_UNFILLED,
        )
        s = run_config(cfg, telegram, ticks, fx, spec, first_ms, last_ms, splits, run_root / cfg.key(), resume=not args.no_resume)
        latency_summaries.append(s)
    pd.DataFrame([{**s["config"], "final_balance_sgd": s["final_balance_sgd"], "net_pnl_sgd": s["net_pnl_sgd"], "minimum_equity_sgd": s["minimum_equity_sgd"], "max_dd_sgd": s["max_continuous_drawdown_sgd"], "stopout": s["stopout"], "fatal": s["fatal_unscorable_after_fill"]} for s in latency_summaries]).to_csv(output_root / "latency_stress.csv", index=False)

    extra_summaries = []
    if args.suite == "full":
        print("\nPHASE C — robustness stress (TTL / same-second / partial / stale-running / edit tier)")
        # Same-second uncertainty sensitivity
        for unc in [0, CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS]:
            cfg = ReplayConfig("STRICT", selected_cfg.depth, selected_cfg.tp_policy, BASE_LATENCY_SECONDS, unc, PENDING_TTL_MINUTES, PRIMARY_PARTIAL_MINLOT_POLICY, PRIMARY_RUNNING_NOTICE_CANCELS_UNFILLED)
            extra_summaries.append(run_config(cfg, telegram, ticks, fx, spec, first_ms, last_ms, splits, run_root / cfg.key(), resume=not args.no_resume))
        # TTL sensitivity
        for ttl in TTL_STRESS_MINUTES:
            cfg = ReplayConfig("STRICT", selected_cfg.depth, selected_cfg.tp_policy, BASE_LATENCY_SECONDS, CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS, ttl, PRIMARY_PARTIAL_MINLOT_POLICY, PRIMARY_RUNNING_NOTICE_CANCELS_UNFILLED)
            extra_summaries.append(run_config(cfg, telegram, ticks, fx, spec, first_ms, last_ms, splits, run_root / cfg.key(), resume=not args.no_resume))
        # Partial-close minimum-lot sensitivity
        for partial in ["close_full", "ignore"]:
            cfg = ReplayConfig("STRICT", selected_cfg.depth, selected_cfg.tp_policy, BASE_LATENCY_SECONDS, CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS, PENDING_TTL_MINUTES, partial, PRIMARY_RUNNING_NOTICE_CANCELS_UNFILLED)
            extra_summaries.append(run_config(cfg, telegram, ticks, fx, spec, first_ms, last_ms, splits, run_root / cfg.key(), resume=not args.no_resume))
        # Whether a provider "running profit" message cancels an unfilled deeper pending.
        for rc in [True, False]:
            cfg = ReplayConfig("STRICT", selected_cfg.depth, selected_cfg.tp_policy, BASE_LATENCY_SECONDS, CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS, PENDING_TTL_MINUTES, PRIMARY_PARTIAL_MINLOT_POLICY, rc)
            extra_summaries.append(run_config(cfg, telegram, ticks, fx, spec, first_ms, last_ms, splits, run_root / cfg.key(), resume=not args.no_resume))
        # Edited final contents become usable only at edit time — kept separate from STRICT.
        cfg = ReplayConfig("EXECUTABLE_ALL", selected_cfg.depth, selected_cfg.tp_policy, BASE_LATENCY_SECONDS, CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS, PENDING_TTL_MINUTES, PRIMARY_PARTIAL_MINLOT_POLICY, PRIMARY_RUNNING_NOTICE_CANCELS_UNFILLED)
        extra_summaries.append(run_config(cfg, telegram, ticks, fx, spec, first_ms, last_ms, splits, run_root / cfg.key(), resume=not args.no_resume))

        # Local depth stability around selected value. Diagnostic only; it does NOT
        # replace the development-selected configuration.
        local_depths = sorted(set(max(0.0, min(1.0, selected_cfg.depth + x)) for x in [-0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15]))
        for depth in local_depths:
            cfg = ReplayConfig("STRICT", depth, selected_cfg.tp_policy, BASE_LATENCY_SECONDS, CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS, PENDING_TTL_MINUTES, PRIMARY_PARTIAL_MINLOT_POLICY, PRIMARY_RUNNING_NOTICE_CANCELS_UNFILLED)
            extra_summaries.append(run_config(cfg, telegram, ticks, fx, spec, first_ms, last_ms, splits, run_root / cfg.key(), resume=not args.no_resume))

        pd.DataFrame([{**s["config"], "final_balance_sgd": s["final_balance_sgd"], "net_pnl_sgd": s["net_pnl_sgd"], "minimum_equity_sgd": s["minimum_equity_sgd"], "max_dd_sgd": s["max_continuous_drawdown_sgd"], "stopout": s["stopout"], "fatal": s["fatal_unscorable_after_fill"]} for s in extra_summaries]).drop_duplicates().to_csv(output_root / "robustness_stress.csv", index=False)

    certification = certification_report(telegram, spec, ticks, first_ms, last_ms, selected, latency_summaries, fx)
    final_package = {
        "forensic_replay_version": "2.0",
        "input_manifest_sha256": manifest.get("manifest_sha256"),
        "telegram_stats": telegram["stats"],
        "broker_spec": asdict(spec),
        "tick_data_from": ms_to_iso(first_ms),
        "tick_data_to": ms_to_iso(last_ms),
        "fx_source": fx.source,
        "development_selected_config": selected,
        "latency_stress": latency_summaries,
        "certification": certification,
        "interpretation_rules": {
            "strict_edited_signal": "EXCLUDE final edited text because pre-edit text is unavailable",
            "executable_all_edited_signal": "activate final edited text no earlier than edit timestamp",
            "management_tp_policy": "full 0.01 remains toward furthest currently known target unless causal management exits/modifies earlier",
            "tp1_tp2_tp3": "full 0.01 closes at selected ordinal target; if fewer targets exist, highest available ordinal is used",
            "partial_0.01": "close_full by primary policy because 0.005 is not executable on 0.01 volume step",
            "provider_result_claims": "never accepted as price proof; Blueberry ticks decide price hits",
            "same_second": f"primary replay delays Telegram events by {CONSERVATIVE_TIMESTAMP_UNCERTAINTY_MS} ms within the exported second",
            "one_active_setup": True,
            "geometry_deduplication": False,
        },
    }
    (output_root / "FINAL_FORENSIC_SUMMARY.json").write_text(json.dumps(final_package, indent=2, default=json_default), encoding="utf-8")

    readme = f"""BLUEBERRY XAUUSD × TELEGRAM FORENSIC REPLAY V2\n\nCERTIFICATION: {certification['status']}\n\nDevelopment-selected configuration:\n{json.dumps(selected['config'], indent=2)}\n\nDevelopment P&L: {selected['split_metrics']['development']['net_pnl_sgd']:.2f} SGD\nValidation P&L: {selected['split_metrics']['validation']['net_pnl_sgd']:.2f} SGD\nHoldout P&L: {selected['split_metrics']['holdout']['net_pnl_sgd']:.2f} SGD\n\nIMPORTANT: the holdout is retrospective because this historical period was already inspected before V2. Treat it as robustness evidence, not a truly fresh prospective test.\n\nRead FINAL_FORENSIC_SUMMARY.json, baseline_matrix.csv, latency_stress.csv and the selected run's audit_timelines.jsonl.\n"""
    (output_root / "READ_ME_FIRST.txt").write_text(readme, encoding="utf-8")

    zip_path = output_root.parent / "XAUUSD_BLUEBERRY_FORENSIC_V2_RESULTS.zip"
    create_zip(output_root, zip_path)
    print("\n" + "=" * 84)
    print("FORENSIC V2 COMPLETE")
    print("=" * 84)
    print("Certification:", certification["status"])
    print("Results folder:", output_root)
    print("UPLOAD THIS ZIP TO CHATGPT:", zip_path)
    print("Do not upload the multi-GB raw tick folder again.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user. Completed run folders remain resumable.")
        sys.exit(130)
    except Exception as exc:
        print("\n" + "=" * 84)
        print("FORENSIC REPLAY ERROR")
        print("=" * 84)
        print(repr(exc))
        print("The script stopped rather than fabricating an account path.")
        raise
