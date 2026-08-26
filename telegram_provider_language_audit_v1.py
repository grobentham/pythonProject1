#!/usr/bin/env python3
"""
Deep language/state audit for the GOLD/XAUUSD Telegram provider export.

This script does NOT backtest and does NOT place orders. Its only job is to learn
how the provider actually communicates across the full Telegram export before we
change the execution model again.

Outputs:
- PROVIDER_LANGUAGE_REPORT.md
- messages_all.csv
- management_messages.csv
- phrase_templates.csv
- intent_counts.csv
- intent_examples.csv
- language_drift_by_year.csv
- round_reentry_messages.csv
- round_reentry_context.csv
- event_sequences.csv
- overlap_candidates.csv
- ambiguous_action_like.csv
- PROVIDER_LANGUAGE_LEXICON.json
- TELEGRAM_PROVIDER_LANGUAGE_AUDIT.zip
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from bs4 import BeautifulSoup

SGT = timezone(timedelta(hours=8))
MAX_NEARBY_SIGNAL_HOURS = 6
CONTEXT_MESSAGES_EACH_SIDE = 4


def find_zip(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        return p
    home = Path.home()
    candidates: List[Path] = []
    for folder in [home / "Downloads", home / "Desktop"]:
        candidates.extend(folder.glob("ChatExport*.zip"))
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        raise FileNotFoundError("No ChatExport*.zip found in Downloads or Desktop")
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
    return candidates[0]


def parse_dt(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    m = re.search(
        r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})(?:\s+UTC([+-]\d{2}:\d{2}))?",
        value,
    )
    if m:
        base = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M:%S")
        off = m.group(3)
        if off:
            sign = 1 if off[0] == "+" else -1
            hh, mm = map(int, off[1:].split(":"))
            tz = timezone(sign * timedelta(hours=hh, minutes=mm))
        else:
            tz = SGT
        return base.replace(tzinfo=tz).astimezone(timezone.utc)
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=SGT)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def clean_text(text: str) -> str:
    x = unicodedata.normalize("NFKC", text or "")
    x = x.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    x = re.sub(r"[\t\r]+", " ", x)
    x = re.sub(r"\n{3,}", "\n\n", x)
    x = re.sub(r"[ ]{2,}", " ", x)
    return x.strip()


def strip_emoji_symbols(text: str) -> str:
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in {"So", "Sk"}:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def template_text(text: str) -> str:
    x = clean_text(text).lower()
    x = re.sub(r"https?://\S+|www\.\S+", " <url> ", x)
    x = re.sub(r"@\w+", " <user> ", x)
    x = re.sub(r"\b\d{1,3}(?:\.\d+)?\s*%", " <pct> ", x)
    x = re.sub(r"[+-]?\d+(?:\.\d+)?\s*(?:pips?|pip)\b", " <pips> ", x)
    x = re.sub(r"\b\d{4,5}(?:\.\d{1,3})?\b", " <price> ", x)
    x = re.sub(r"\b\d+(?:\.\d+)?\b", " <n> ", x)
    x = strip_emoji_symbols(x)
    x = re.sub(r"[^a-z0-9<>+\-/%'\s]", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    return x


def words(text: str) -> List[str]:
    return re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?|\d+(?:\.\d+)?", text or "")


def parse_reply_id(div) -> Optional[int]:
    for a in div.select(".reply_to a, .reply_to.details a, a[href*='go_to_message']"):
        href = a.get("href", "")
        m = re.search(r"go_to_message(\d+)", href)
        if m:
            return int(m.group(1))
    return None


def parse_msg_id(div) -> Optional[int]:
    raw = div.get("id", "")
    m = re.search(r"message(\d+)", raw)
    if m:
        return int(m.group(1))
    return None


def read_export(zip_path: Path) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    with zipfile.ZipFile(zip_path, "r") as z:
        htmls = sorted(n for n in z.namelist() if n.lower().endswith(".html") and "messages" in n.lower())
        if not htmls:
            htmls = sorted(n for n in z.namelist() if n.lower().endswith(".html"))
        print(f"HTML files: {len(htmls)}")
        for ix, name in enumerate(htmls, 1):
            raw = z.read(name)
            soup = BeautifulSoup(raw, "html.parser")
            for div in soup.select("div.message"):
                msg_id = parse_msg_id(div)
                if msg_id is None:
                    continue
                date_node = div.select_one(".date.details")
                dt = parse_dt(date_node.get("title", "") if date_node else "")
                if dt is None:
                    continue
                text_node = div.select_one(".text")
                text = clean_text(text_node.get_text("\n", strip=True) if text_node else "")
                author_node = div.select_one(".from_name")
                author = clean_text(author_node.get_text(" ", strip=True) if author_node else "")
                media_nodes = div.select(".media_wrap, .photo_wrap, .video_file_wrap, .document_wrap")
                messages.append({
                    "msg_id": msg_id,
                    "time_utc": dt.isoformat(),
                    "time_ms": int(dt.timestamp() * 1000),
                    "year": dt.astimezone(SGT).year,
                    "reply_id": parse_reply_id(div),
                    "author": author,
                    "text": text,
                    "template": template_text(text),
                    "word_count": len(words(text)),
                    "has_media": bool(media_nodes),
                    "source_html": name,
                })
            if ix % 10 == 0 or ix == len(htmls):
                print(f"Parsed {ix}/{len(htmls)} HTML files; messages={len(messages):,}")
    messages.sort(key=lambda m: (m["time_ms"], m["msg_id"]))
    return messages


SIDE = re.compile(r"(?i)\b(buy|sell)\b")
SL = re.compile(r"(?i)\b(?:sl|stl|stop\s*loss|stoploss)\b")
TP = re.compile(r"(?i)\b(?:tp\s*\d*|take\s*profit|target\s*\d*)\b")
ENTRY = re.compile(r"(?i)\b(?:entry|zone)\b")


def is_signal(text: str) -> bool:
    low = (text or "").lower()
    structured = bool(SIDE.search(low) and SL.search(low) and (TP.search(low) or "target" in low))
    setup = bool(("trade setup" in low or "signal" in low) and SIDE.search(low) and (ENTRY.search(low) or re.search(r"\b\d{4,5}\b", low)))
    compact = bool(SIDE.search(low) and re.search(r"\b\d{4,5}(?:\.\d+)?\b", low) and (SL.search(low) or TP.search(low)))
    return structured or setup or compact


ROUND_WORDS = {
    "first": 1, "one": 1,
    "second": 2, "two": 2,
    "third": 3, "three": 3,
    "fourth": 4, "four": 4,
    "fifth": 5, "five": 5,
}


def extract_round(text: str) -> Optional[int]:
    low = (text or "").lower()
    for pat in [r"\bround\s*#?\s*(\d+)\b", r"\br\s*(\d+)\b", r"\b(\d+)(?:st|nd|rd|th)\s+round\b"]:
        m = re.search(pat, low)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    m = re.search(r"\b(first|second|third|fourth|fifth|one|two|three|four|five)\s+round\b", low)
    return ROUND_WORDS.get(m.group(1)) if m else None


def extract_tp(text: str) -> Optional[int]:
    low = (text or "").lower()
    m = re.search(r"\btp\s*#?\s*([1-9])\b|\btarget\s*#?\s*([1-9])\b", low)
    if m:
        return int(m.group(1) or m.group(2))
    return None


# Multi-label semantic rules. These are deliberately broad: the audit is meant to
# surface phrase families and ambiguous wording for human review, not silently force
# every message into one trading action.
INTENT_PATTERNS: Dict[str, List[str]] = {
    "ROUND_REENTRY": [
        r"\bjoin\s+(?:for\s+)?round\s*\d+\b", r"\bjoin\s+round\s*\d+\b",
        r"\bround\s*\d+\s+(?:if|when)\s+(?:it\s+)?hit(?:s)?\s+entry\b",
        r"\b(?:re[- ]?enter|re[- ]?entry|enter\s+again|re[- ]?join|join\s+again)\b",
        r"\b(?:rebuy|re-sell|resell)\b", r"\b(?:same|old|previous)\s+entry\b",
        r"\b(?:back|come\s+back|return)\s+to\s+entry\b", r"\banother\s+round\b",
        r"\bnext\s+round\b", r"\b(?:2nd|second|3rd|third)\s+(?:entry|round)\b",
    ],
    "ROUND_CANCEL": [
        r"\bcancel(?:led)?\s+(?:for\s+)?round\s*\d+\b", r"\bcancel\s+round\s*\d+\b",
        r"\bround\s*\d+\s+cancel(?:led)?\b", r"\bskip\s+round\s*\d+\b",
        r"\bno\s+round\s*\d+\b",
    ],
    "MISSED_NO_FILL": [
        r"\bmissed\b", r"\bmiss\s+guys\b", r"\bnot\s+hit\s+entry\b",
        r"\bdid(?:\s+not|n't)\s+hit\s+entry\b", r"\bnever\s+hit\s+entry\b",
        r"\bno\s+(?:entry|fill)\b", r"\bentry\s+not\s+hit\b",
        r"\bdid(?:\s+not|n't)\s+(?:reach|touch)\s+entry\b",
    ],
    "KEEP_RUNNER": [
        r"\bkeep\s+(?:some|a\s+few|few|part|remaining|the\s+rest)\s+(?:trade|trades|position|positions)?\b",
        r"\bkeep\s+(?:some\s+)?(?:trade|trades|position|positions)\s+(?:to|for)\s+tp\s*[23]\b",
        r"\bhold\s+(?:some|remaining|the\s+rest)\b", r"\bleave\s+(?:some|a\s+runner|runner|remaining)\b",
        r"\b(?:let|leave)\s+(?:it|some)\s+run\b", r"\bkeep\s+(?:a\s+)?runner\b",
        r"\brunner\s+(?:to|for)\s+tp\s*[23]\b",
    ],
    "CLOSE_PARTIAL": [
        r"\bclose\s+(?:half|50\s*%|some|partial|part)\b", r"\bpartial\s+close\b",
        r"\bclose\s+partial\b", r"\btake\s+(?:half|some)\b", r"\bbook\s+(?:half|some|partial)\b",
        r"\bsecure\s+(?:half|some|partial)\b", r"\btake\s+some\s+profit\b",
    ],
    "CLOSE_FULL": [
        r"\bclose\s+all\b", r"\bclose\s+(?:the\s+)?trade\b", r"\bclose\s+(?:now|here)\b",
        r"\bexit\s+(?:now|here|all)\b", r"\bbook\s+all\b", r"\bsecure\s+all\b",
        r"\btake\s+all\b", r"\bdone\s+(?:for\s+today|for\s+now)\b",
    ],
    "CANCEL_PENDING": [
        r"\bcancel(?:led)?\b", r"\bdelete\s+(?:the\s+)?(?:pending|order)\b",
        r"\bremove\s+(?:the\s+)?(?:pending|order)\b", r"\bdon't\s+enter\b", r"\bdo\s+not\s+enter\b",
        r"\bskip\s+(?:the\s+)?trade\b", r"\bignore\s+(?:the\s+)?(?:signal|entry|trade)\b",
        r"\bwait\s+for\s+(?:a\s+)?(?:new|next)\s+signal\b",
    ],
    "MOVE_BE": [
        r"\bbreak\s*even\b", r"\bbreakeven\b", r"\bb\.e\.\b", r"\bmove\s+(?:sl|stl|stop(?:\s+loss)?)\s+(?:to|at)\s+(?:entry|be)\b",
        r"\b(?:sl|stl)\s+(?:to|at)\s+entry\b", r"\brisk\s*free\b", r"\bfree\s+trade\b",
    ],
    "SECURE_PROFIT_SL": [
        r"\bsecure\s+(?:profit|profits)\b", r"\block\s+(?:profit|profits)\b",
        r"\bmove\s+(?:sl|stop)\s+(?:to|in)\s+profit\b", r"\bprotect\s+(?:profit|profits)\b",
        r"\btrail(?:ing)?\s+(?:sl|stop)\b",
    ],
    "AMEND_SL": [
        r"\b(?:new|change|update|move|set)\s+(?:sl|stl|stop(?:\s+loss)?)\b",
        r"\b(?:sl|stl|stop(?:\s+loss)?)\s*(?:[:=@]|to|at)\s*\d{4,5}(?:\.\d+)?\b",
    ],
    "AMEND_TP": [
        r"\b(?:new|change|update|set)\s+tp\b", r"\btp\s*[1-9]?\s*(?:[:=@]|to|at)\s*\d{4,5}(?:\.\d+)?\b",
        r"\btarget\s*[1-9]?\s*(?:[:=@]|to|at)\s*\d{4,5}(?:\.\d+)?\b",
    ],
    "AMEND_ENTRY": [
        r"\b(?:new|change|update|move|set)\s+entry\b", r"\bentry\s*(?:[:=@]|to|at)\s*\d{4,5}(?:\.\d+)?\b",
        r"\bentry\s+zone\s*(?:[:=@]|to|at)?\s*\d{4,5}",
    ],
    "TP_HIT": [
        r"\b(?:hit|hits|hitting|done|reach(?:ed)?|touch(?:ed)?)\s+tp\s*[1-9]\b",
        r"\btp\s*[1-9]\s+(?:hit|hits|done|reached|touched)\b", r"\bhit\s+(?:the\s+)?target\b",
        r"\btarget\s*[1-9]\s+(?:hit|done|reached)\b",
    ],
    "SL_HIT": [
        r"\b(?:hit|hits|hitting|done|reach(?:ed)?|touch(?:ed)?)\s+(?:sl|stop(?:\s+loss)?)\b",
        r"\b(?:sl|stop(?:\s+loss)?)\s+(?:hit|done|reached|touched)\b", r"\bstopped\s+out\b",
    ],
    "RUNNING_PROFIT": [
        r"\brunning\s+[+-]?\d+(?:\.\d+)?\s*pips?\b", r"\brunning\s+(?:profit|profits)\b",
        r"\bcurrently\s+[+-]?\d+(?:\.\d+)?\s*pips?\b", r"\bfloating\s+(?:profit|profits|[+-]?\d+)\b",
        r"(?:^|\s)\+\s*\d+(?:\.\d+)?\s*pips?\b",
    ],
    "WAIT_HOLD": [
        r"\bwait\b", r"\bhold\b", r"\bstand\s*by\b", r"\bdon't\s+close\b", r"\bdo\s+not\s+close\b",
        r"\bstill\s+valid\b", r"\bkeep\s+holding\b",
    ],
    "NO_MORE_ENTRY": [
        r"\bno\s+more\s+entry\b", r"\bdon't\s+join\b", r"\bdo\s+not\s+join\b",
        r"\btoo\s+late\s+to\s+enter\b", r"\bdo\s+not\s+chase\b", r"\bdon't\s+chase\b",
    ],
    "RESULT_CLAIM": [
        r"\bprofit\s*[+:]\s*[+-]?\d+", r"\b[+-]?\d+(?:\.\d+)?\s*pips?\s*(?:profit|secured|done|hit)?\b",
        r"\bplayed\s+out\s+perfectly\b", r"\bwin(?:ning)?\s+trade\b",
    ],
}

COMPILED = {k: [re.compile(p, re.I) for p in pats] for k, pats in INTENT_PATTERNS.items()}


def classify(text: str) -> List[str]:
    x = clean_text(text)
    labels = []
    for intent, regs in COMPILED.items():
        if any(r.search(x) for r in regs):
            labels.append(intent)
    return labels


ACTION_HINT = re.compile(
    r"(?i)\b(?:tp\s*\d*|sl|stop|entry|round|cancel|close|exit|hold|keep|running|pip|profit|missed|join|re.?enter|re.?join|break.?even|be|secure|lock|wait|target|runner)\b"
)


def action_like(text: str) -> bool:
    x = clean_text(text)
    if not x:
        return False
    return bool(ACTION_HINT.search(x)) and len(words(x)) <= 35


def resolve_signal_root(msg_id: int, by_id: Dict[int, Dict[str, Any]], signal_ids: set[int]) -> Tuple[Optional[int], Optional[int]]:
    cur = by_id.get(msg_id, {}).get("reply_id")
    visited = set()
    hops = 0
    while cur is not None and cur not in visited and hops < 50:
        visited.add(cur)
        hops += 1
        if cur in signal_ids:
            return cur, hops
        parent = by_id.get(cur)
        if not parent:
            break
        cur = parent.get("reply_id")
    return None, None


def nearest_previous_signal(msg: Dict[str, Any], signals: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[float]]:
    # Binary search would be faster, but 40k messages is still tiny. This uses a
    # pre-built moving pointer in attach_context() rather than scanning here.
    return None, None


def attach_context(messages: List[Dict[str, Any]]) -> None:
    by_id = {m["msg_id"]: m for m in messages}
    signal_ids = {m["msg_id"] for m in messages if m["is_signal"]}
    recent_signals: List[Dict[str, Any]] = []
    for m in messages:
        root, hops = resolve_signal_root(m["msg_id"], by_id, signal_ids)
        m["explicit_signal_root"] = root
        m["reply_hops_to_signal"] = hops
        while recent_signals and m["time_ms"] - recent_signals[0]["time_ms"] > MAX_NEARBY_SIGNAL_HOURS * 3600 * 1000:
            recent_signals.pop(0)
        if m["is_signal"]:
            recent_signals.append(m)
            m["nearest_signal_id"] = m["msg_id"]
            m["nearest_signal_gap_min"] = 0.0
            m["context_link_type"] = "SELF_SIGNAL"
        else:
            prev = recent_signals[-1] if recent_signals else None
            if root is not None:
                m["nearest_signal_id"] = root
                m["nearest_signal_gap_min"] = (m["time_ms"] - by_id[root]["time_ms"]) / 60000.0 if root in by_id else None
                m["context_link_type"] = "EXPLICIT_REPLY_CHAIN"
            elif prev is not None:
                m["nearest_signal_id"] = prev["msg_id"]
                m["nearest_signal_gap_min"] = (m["time_ms"] - prev["time_ms"]) / 60000.0
                m["context_link_type"] = "INFERRED_NEARBY_PREVIOUS_SIGNAL"
            else:
                m["nearest_signal_id"] = None
                m["nearest_signal_gap_min"] = None
                m["context_link_type"] = "UNLINKED"


def event_label(m: Dict[str, Any]) -> str:
    labels = m.get("intents") or []
    priority = [
        "ROUND_CANCEL", "ROUND_REENTRY", "MISSED_NO_FILL", "KEEP_RUNNER",
        "CLOSE_FULL", "CLOSE_PARTIAL", "MOVE_BE", "SECURE_PROFIT_SL",
        "AMEND_ENTRY", "AMEND_SL", "AMEND_TP", "TP_HIT", "SL_HIT",
        "RUNNING_PROFIT", "CANCEL_PENDING", "NO_MORE_ENTRY", "WAIT_HOLD", "RESULT_CLAIM",
    ]
    for p in priority:
        if p in labels:
            n = m.get("round_number")
            t = m.get("tp_number")
            if p.startswith("ROUND_") and n:
                return f"{p}_R{n}"
            if p == "TP_HIT" and t:
                return f"TP{t}_HIT"
            return p
    return "OTHER_ACTION" if m.get("action_like") else "NON_ACTION"


def context_window(messages: List[Dict[str, Any]], idx: int, radius: int = CONTEXT_MESSAGES_EACH_SIDE) -> str:
    lo = max(0, idx - radius)
    hi = min(len(messages), idx + radius + 1)
    lines = []
    for j in range(lo, hi):
        m = messages[j]
        marker = ">>>" if j == idx else "   "
        local = datetime.fromisoformat(m["time_utc"]).astimezone(SGT).strftime("%Y-%m-%d %H:%M:%S")
        txt = clean_text(m["text"]).replace("\n", " | ")[:500]
        lines.append(f"{marker} [{local}] id={m['msg_id']} reply={m.get('reply_id')} {txt}")
    return "\n".join(lines)


def make_outputs(messages: List[Dict[str, Any]], out: Path, source_zip: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for m in messages:
        m["is_signal"] = is_signal(m["text"])
        m["intents"] = classify(m["text"])
        m["round_number"] = extract_round(m["text"])
        m["tp_number"] = extract_tp(m["text"])
        m["action_like"] = action_like(m["text"])
    attach_context(messages)
    for m in messages:
        m["event_label"] = event_label(m)

    # Full message table
    msg_rows = []
    for m in messages:
        r = dict(m)
        r["intents"] = "|".join(m["intents"])
        msg_rows.append(r)
    df = pd.DataFrame(msg_rows)
    df.to_csv(out / "messages_all.csv", index=False)

    mgmt = [m for m in messages if m["intents"] or (m["action_like"] and not m["is_signal"])]
    mgdf = pd.DataFrame([{**m, "intents": "|".join(m["intents"])} for m in mgmt])
    mgdf.to_csv(out / "management_messages.csv", index=False)

    # Intent counts / examples
    counts = Counter()
    by_intent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for m in messages:
        for intent in m["intents"]:
            counts[intent] += 1
            by_intent[intent].append(m)
    pd.DataFrame([{"intent": k, "count": v} for k, v in counts.most_common()]).to_csv(out / "intent_counts.csv", index=False)

    ex_rows = []
    for intent, arr in sorted(by_intent.items()):
        seen = set()
        for m in arr:
            temp = m["template"]
            if temp in seen:
                continue
            seen.add(temp)
            ex_rows.append({
                "intent": intent,
                "msg_id": m["msg_id"],
                "time_utc": m["time_utc"],
                "round_number": m["round_number"],
                "tp_number": m["tp_number"],
                "context_link_type": m["context_link_type"],
                "text": m["text"],
                "template": temp,
            })
            if len(seen) >= 50:
                break
    pd.DataFrame(ex_rows).to_csv(out / "intent_examples.csv", index=False)

    # Phrase-template census. This is the key anti-regex-blindness artifact.
    tc = Counter(m["template"] for m in messages if m["template"])
    template_rows = []
    first_for_template: Dict[str, Dict[str, Any]] = {}
    for m in messages:
        first_for_template.setdefault(m["template"], m)
    for temp, count in tc.most_common():
        m = first_for_template[temp]
        template_rows.append({
            "template": temp,
            "count": count,
            "first_example_msg_id": m["msg_id"],
            "first_example_text": m["text"],
            "example_intents": "|".join(m["intents"]),
            "action_like": m["action_like"],
        })
    pd.DataFrame(template_rows).to_csv(out / "phrase_templates.csv", index=False)

    # Language drift by year.
    years = sorted(set(m["year"] for m in messages))
    drift_rows = []
    all_intents = sorted(counts)
    for y in years:
        ys = [m for m in messages if m["year"] == y]
        row = {"year": y, "messages": len(ys), "signals": sum(m["is_signal"] for m in ys), "action_like": sum(m["action_like"] for m in ys)}
        for intent in all_intents:
            row[intent] = sum(intent in m["intents"] for m in ys)
        drift_rows.append(row)
    pd.DataFrame(drift_rows).to_csv(out / "language_drift_by_year.csv", index=False)

    # Round/reentry focused audit with full local context.
    round_msgs = [m for m in messages if m["round_number"] is not None or "ROUND_REENTRY" in m["intents"] or "ROUND_CANCEL" in m["intents"]]
    pd.DataFrame([{**m, "intents": "|".join(m["intents"])} for m in round_msgs]).to_csv(out / "round_reentry_messages.csv", index=False)
    idx_by_id = {m["msg_id"]: i for i, m in enumerate(messages)}
    ctx_rows = []
    for m in round_msgs:
        i = idx_by_id[m["msg_id"]]
        ctx_rows.append({
            "msg_id": m["msg_id"], "time_utc": m["time_utc"], "round_number": m["round_number"],
            "intents": "|".join(m["intents"]), "text": m["text"],
            "explicit_signal_root": m["explicit_signal_root"], "nearest_signal_id": m["nearest_signal_id"],
            "context_link_type": m["context_link_type"], "context": context_window(messages, i),
        })
    pd.DataFrame(ctx_rows).to_csv(out / "round_reentry_context.csv", index=False)

    # Event sequences grouped by explicit reply root where possible, otherwise by
    # a clearly-labelled nearest previous signal inference.
    groups: Dict[Tuple[Any, str], List[Dict[str, Any]]] = defaultdict(list)
    for m in mgmt:
        sid = m.get("nearest_signal_id")
        if sid is None:
            continue
        groups[(sid, m["context_link_type"])].append(m)
    seq_counter = Counter()
    seq_rows = []
    for (sid, link_type), arr in groups.items():
        arr.sort(key=lambda x: (x["time_ms"], x["msg_id"]))
        seq = " > ".join(m["event_label"] for m in arr if m["event_label"] != "NON_ACTION")
        if not seq:
            continue
        seq_counter[(seq, link_type)] += 1
        seq_rows.append({
            "signal_id": sid, "link_type": link_type,
            "first_event_time_utc": arr[0]["time_utc"], "last_event_time_utc": arr[-1]["time_utc"],
            "event_count": len(arr), "sequence": seq,
            "message_ids": "|".join(str(m["msg_id"]) for m in arr),
            "texts": " || ".join(clean_text(m["text"]).replace("\n", " | ")[:300] for m in arr),
        })
    pd.DataFrame(seq_rows).to_csv(out / "event_sequences.csv", index=False)
    pd.DataFrame([
        {"sequence": seq, "link_type": link, "count": count}
        for (seq, link), count in seq_counter.most_common()
    ]).to_csv(out / "event_sequence_counts.csv", index=False)

    # Detect the exact architecture that broke V3: KEEP_RUNNER followed by a new
    # ROUND_REENTRY before the sequence ends. These are candidates for concurrent
    # cohorts, not proof; every row carries the source texts for review.
    overlap_rows = []
    for (sid, link_type), arr in groups.items():
        arr.sort(key=lambda x: (x["time_ms"], x["msg_id"]))
        keep_i = [i for i, m in enumerate(arr) if "KEEP_RUNNER" in m["intents"]]
        re_i = [i for i, m in enumerate(arr) if "ROUND_REENTRY" in m["intents"]]
        for ki in keep_i:
            later = [ri for ri in re_i if ri > ki]
            if not later:
                continue
            ri = later[0]
            overlap_rows.append({
                "signal_id": sid, "link_type": link_type,
                "keep_msg_id": arr[ki]["msg_id"], "keep_time_utc": arr[ki]["time_utc"], "keep_text": arr[ki]["text"],
                "reentry_msg_id": arr[ri]["msg_id"], "reentry_time_utc": arr[ri]["time_utc"], "reentry_text": arr[ri]["text"],
                "round_number": arr[ri]["round_number"],
                "minutes_between": (arr[ri]["time_ms"] - arr[ki]["time_ms"]) / 60000.0,
            })
    pd.DataFrame(overlap_rows).to_csv(out / "overlap_candidates.csv", index=False)

    # Anything that looks operational but is still unclassified is the main review
    # queue. This is what prevents another 'join round 2' blind spot.
    ambiguous = [m for m in messages if m["action_like"] and not m["intents"] and not m["is_signal"]]
    amb_rows = []
    for m in ambiguous:
        i = idx_by_id[m["msg_id"]]
        amb_rows.append({
            "msg_id": m["msg_id"], "time_utc": m["time_utc"], "text": m["text"], "template": m["template"],
            "reply_id": m["reply_id"], "explicit_signal_root": m["explicit_signal_root"],
            "nearest_signal_id": m["nearest_signal_id"], "context_link_type": m["context_link_type"],
            "context": context_window(messages, i),
        })
    pd.DataFrame(amb_rows).to_csv(out / "ambiguous_action_like.csv", index=False)

    # Lexicon package: all regex families plus top real phrase templates/examples.
    lexicon = {
        "source_zip": source_zip.name,
        "message_count": len(messages),
        "signal_like_count": sum(m["is_signal"] for m in messages),
        "management_or_action_like_count": len(mgmt),
        "unclassified_action_like_count": len(ambiguous),
        "round_reentry_message_count": len(round_msgs),
        "overlap_candidate_count": len(overlap_rows),
        "intent_counts": dict(counts),
        "intent_patterns": INTENT_PATTERNS,
        "top_templates": template_rows[:500],
        "notes": [
            "Multi-label classification is intentionally broad and diagnostic, not an execution policy.",
            "INFERRED_NEARBY_PREVIOUS_SIGNAL context is not the same as an explicit Telegram reply link.",
            "Every unclassified action-like message must be reviewed before claiming provider-faithful replay.",
            "Round/reentry cohorts must be independently addressable when the provider keeps an earlier runner alive.",
        ],
    }
    (out / "PROVIDER_LANGUAGE_LEXICON.json").write_text(json.dumps(lexicon, indent=2, ensure_ascii=False), encoding="utf-8")

    # Markdown report.
    top_intents = counts.most_common(20)
    top_action_templates = [r for r in template_rows if r["action_like"]][:100]
    report = []
    report.append("# Telegram Provider Language Audit\n")
    report.append(f"Source: `{source_zip.name}`  ")
    report.append(f"Messages parsed: **{len(messages):,}**  ")
    report.append(f"Signal-like messages: **{sum(m['is_signal'] for m in messages):,}**  ")
    report.append(f"Management/action-like messages: **{len(mgmt):,}**  ")
    report.append(f"Round/re-entry messages: **{len(round_msgs):,}**  ")
    report.append(f"Concurrent-cohort overlap candidates: **{len(overlap_rows):,}**  ")
    report.append(f"Unclassified action-like messages requiring review: **{len(ambiguous):,}**\n")
    report.append("## Key rule\n")
    report.append("No backtest should be called provider-faithful until `ambiguous_action_like.csv` has been reduced to an acceptable, explicitly reviewed remainder and round-specific cancellation/re-entry semantics are represented as separate cohorts.\n")
    report.append("## Intent counts\n")
    report.append("| Intent | Count |\n|---|---:|")
    for k, v in top_intents:
        report.append(f"| {k} | {v:,} |")
    report.append("\n## Top action-like phrase templates\n")
    report.append("| Count | Template | Example |\n|---:|---|---|")
    for r in top_action_templates[:60]:
        ex = clean_text(r["first_example_text"]).replace("|", "\\|").replace("\n", " / ")[:180]
        temp = r["template"].replace("|", "\\|")[:180]
        report.append(f"| {r['count']} | `{temp}` | {ex} |")
    report.append("\n## Files to review first\n")
    report.append("1. `round_reentry_context.csv` — all round/rejoin language with local message context.")
    report.append("2. `overlap_candidates.csv` — keep-runner then new-round cases requiring concurrent cohorts.")
    report.append("3. `ambiguous_action_like.csv` — operational-looking messages not yet covered by any semantic family.")
    report.append("4. `event_sequence_counts.csv` — recurring provider conversation/state patterns.")
    report.append("5. `language_drift_by_year.csv` — wording changes over time that can break fixed regexes.")
    report.append("\n## Execution-engine implications\n")
    report.append("- A provider message is not just a keyword: it belongs to a round/cohort, setup, and state.")
    report.append("- `Running X pips` is a status update unless another explicit instruction changes orders.")
    report.append("- `Done TP1` / `Hit TP1` is outcome confirmation; independent Blueberry ticks remain the execution truth.")
    report.append("- `Keep some trade to TP2` means a runner can survive TP1.")
    report.append("- `Join round 2 if hit entry` creates a separate conditional cohort; it must not overwrite the earlier runner.")
    report.append("- `Cancel for round 2` must address Round 2 only, not Round 1 survivors.")
    report.append("- `Missed` is normally a no-fill/result state, not automatically a close instruction.")
    report.append("- Generic unlinked `cancel`/`close` must fail closed unless round/setup scope can be resolved.")
    (out / "PROVIDER_LANGUAGE_REPORT.md").write_text("\n".join(report), encoding="utf-8")

    # Zip results.
    zip_out = out.parent / "TELEGRAM_PROVIDER_LANGUAGE_AUDIT.zip"
    if zip_out.exists():
        zip_out.unlink()
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out))
    print("\nAUDIT COMPLETE")
    print("Output folder:", out)
    print("Upload ZIP:", zip_out)
    print("Messages:", len(messages))
    print("Round/reentry messages:", len(round_msgs))
    print("Overlap candidates:", len(overlap_rows))
    print("Unclassified action-like:", len(ambiguous))


def main() -> None:
    ap = argparse.ArgumentParser(description="Deep audit of Telegram provider language and management state semantics")
    ap.add_argument("--telegram", help="Path to ChatExport*.zip")
    ap.add_argument("--output", help="Output directory; default Desktop\\TELEGRAM_PROVIDER_LANGUAGE_AUDIT")
    args = ap.parse_args()
    src = find_zip(args.telegram)
    out = Path(args.output).expanduser().resolve() if args.output else Path.home() / "Desktop" / "TELEGRAM_PROVIDER_LANGUAGE_AUDIT"
    print("Telegram:", src)
    print("Output  :", out)
    messages = read_export(src)
    if not messages:
        raise RuntimeError("No Telegram messages parsed")
    make_outputs(messages, out, src)


if __name__ == "__main__":
    main()
