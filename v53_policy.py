from __future__ import annotations

import json
import math
import os
import re
from typing import Any, List, Optional

import numpy as np

RISK_PCT = float(os.environ.get("V53_MAX_STOP_RISK_PCT", "10"))
MAX_PER_ROUND = int(os.environ.get("V53_MAX_TICKETS_PER_ROUND", "3"))
PARTIAL_POLICY = os.environ.get("V53_PARTIAL_POLICY", "CEIL_HALF").upper()
ZONE_POLICY = os.environ.get("V53_ZONE_POLICY", "EVEN_3").upper()
_TTL = os.environ.get("V53_SAFETY_TTL_HOURS", "NONE").strip().upper()
SAFETY_TTL_HOURS = None if _TTL in {"", "NONE", "OFF", "0"} else float(_TTL)


def getv(obj: Any, *names: str, default=None):
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return default


def as_float(value, default=None):
    try:
        return float(str(value).strip().replace(",", ""))
    except Exception:
        return default


def as_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def list_floats(value) -> List[float]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, np.ndarray)):
        out = []
        for item in value:
            v = as_float(item)
            if v is not None:
                out.append(v)
        return out
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return list_floats(parsed)
        except Exception:
            pass
        return [float(x) for x in re.findall(r"(?<!\d)([1-9]\d{3}(?:\.\d{1,3})?)(?!\d)", value)]
    v = as_float(value)
    return [] if v is None else [v]


def instruction_kind(ins) -> str:
    return str(getv(ins, "kind", "event", "action", "intent", default="") or "").upper()


def instruction_text(ins) -> str:
    return str(getv(ins, "text", "raw_text", "source_text", "message_text", default="") or "")


def setup_uid(ins) -> str:
    return str(getv(ins, "setup_uid", "setup_id", "scope_setup_uid", "parent_setup_uid", default="") or "")


def round_no(ins) -> int:
    return as_int(getv(ins, "round_no", "round", "round_number", default=1), 1) or 1


def effective_ms(ins) -> int:
    return as_int(getv(ins, "effective_ms", "time_ms", "timestamp_ms", default=0), 0) or 0


def side_of(ins) -> Optional[str]:
    side = str(getv(ins, "side", "direction", default="") or "").upper()
    if "BUY" in side:
        return "BUY"
    if "SELL" in side:
        return "SELL"
    match = re.search(r"\b(BUY|SELL)\b", instruction_text(ins).upper())
    return match.group(1) if match else None


def downside_stop_distance(side: str, entry: float, sl: float) -> float:
    """Only remaining downside counts. BE/profit-protected stops consume zero risk."""
    if side == "BUY":
        return max(0.0, float(entry) - float(sl))
    return max(0.0, float(sl) - float(entry))


def limit_fill_price(side: str, requested_limit: float, bid: float, ask: float) -> float:
    """Resting limits can receive price improvement, never worse than the limit."""
    if side == "BUY":
        return min(float(requested_limit), float(ask))
    return max(float(requested_limit), float(bid))


def partial_close_count(n_open: int, policy: str = PARTIAL_POLICY) -> int:
    if n_open <= 0:
        return 0
    if n_open == 1:
        return 1
    if policy == "FLOOR_HALF":
        return max(1, n_open // 2)
    return max(1, math.ceil(n_open / 2.0))


def explicit_provider_entries(ins, side: Optional[str], zone_low=None, zone_high=None) -> List[float]:
    """Use structured explicit entry fields first; infer text entries only from strong forms."""
    for field in ("explicit_entries", "entry_prices", "provider_entries", "entries"):
        values = list_floats(getv(ins, field, default=None))
        if values:
            return list(dict.fromkeys(values))

    text = instruction_text(ins)
    values: List[float] = []
    entry_word = re.compile(r"(?i)\bentr(?:y|ies)\b")
    if entry_word.search(text):
        for line in re.split(r"[\n;]+", text):
            low = line.lower()
            if entry_word.search(line) and not re.search(r"\b(?:sl|stop|tp|take profit)\b", low):
                values.extend(list_floats(line))

    # Strong compact forms such as BUY 4625 / 4624 / 4623. This is only
    # considered when the side is explicit and the extracted values lie inside
    # the stated provider zone below.
    if not values and side:
        for line in re.split(r"[\n;]+", text):
            if not re.search(rf"(?i)\b{re.escape(side)}\b", line):
                continue
            if re.search(r"(?i)\b(?:sl|stop|tp|take profit)\b", line):
                continue
            vals = list_floats(line)
            if len(vals) >= 2:
                values.extend(vals)
                break

    values = list(dict.fromkeys(values))
    if zone_low is not None and zone_high is not None:
        lo, hi = min(zone_low, zone_high), max(zone_low, zone_high)
        values = [x for x in values if lo - 0.05 <= x <= hi + 0.05]
    return values


def select_explicit_entries(values: List[float], side: str, n: int = MAX_PER_ROUND) -> List[float]:
    values = list(dict.fromkeys(float(x) for x in values))
    if len(values) <= n:
        return values
    # Frozen rule when provider supplies more than the executable ticket count:
    # choose the deepest/better entries rather than hindsight-optimizing outcomes.
    return sorted(values, reverse=(side == "SELL"))[:n]


def synthetic_zone_entries(side: str, zone_low: float, zone_high: float, n: int = MAX_PER_ROUND) -> List[float]:
    lo, hi = min(float(zone_low), float(zone_high)), max(float(zone_low), float(zone_high))
    if n <= 0:
        return []
    if ZONE_POLICY == "DEEPEST_WHOLE_3":
        integers = [float(x) for x in range(math.ceil(lo), math.floor(hi) + 1)]
        if integers:
            ordered = sorted(integers) if side == "BUY" else sorted(integers, reverse=True)
            return ordered[:n]
    if n == 1:
        return [(lo + hi) / 2.0]
    if n == 2:
        return ([hi, lo] if side == "BUY" else [lo, hi])[:n]
    return ([hi, (lo + hi) / 2.0, lo] if side == "BUY" else [lo, (lo + hi) / 2.0, hi])[:n]
