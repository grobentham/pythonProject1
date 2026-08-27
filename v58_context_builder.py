from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from v58_signal_intelligence_gate import FeatureObservation, SignalProposal


@dataclass(frozen=True)
class QuotePoint:
    ts_ms: int
    bid: float
    ask: float


@dataclass(frozen=True)
class ScalarPoint:
    ts_ms: int
    value: float


@dataclass(frozen=True)
class EventState:
    observed_at_ms: int
    state_known: bool
    next_high_impact_ms: Optional[int]
    last_high_impact_ms: Optional[int]
    breaking_news_risk: bool
    source: str = "EVENT_FEED"


def _causal_points(points: Iterable, decision_time_ms: int):
    return sorted((p for p in points if p.ts_ms <= decision_time_ms), key=lambda p: p.ts_ms)


def _nearest_at_or_before(points: Sequence, target_ms: int):
    out = None
    for p in points:
        if p.ts_ms <= target_ms:
            out = p
        else:
            break
    return out


def _return_over(points: Sequence[ScalarPoint], decision_time_ms: int, lookback_ms: int) -> Optional[float]:
    pts = _causal_points(points, decision_time_ms)
    if not pts:
        return None
    end = pts[-1]
    start = _nearest_at_or_before(pts, decision_time_ms - lookback_ms)
    if start is None or start.value == 0:
        return None
    return float(end.value / start.value - 1.0)


def _change_over(points: Sequence[ScalarPoint], decision_time_ms: int, lookback_ms: int) -> Optional[float]:
    pts = _causal_points(points, decision_time_ms)
    if not pts:
        return None
    end = pts[-1]
    start = _nearest_at_or_before(pts, decision_time_ms - lookback_ms)
    if start is None:
        return None
    return float(end.value - start.value)


def _quote_mid(q: QuotePoint) -> float:
    return (float(q.bid) + float(q.ask)) / 2.0


def build_context_features(
    proposal: SignalProposal,
    decision_time_ms: int,
    xau_quotes: Sequence[QuotePoint],
    dxy_prices: Sequence[ScalarPoint],
    us10y_yields_pct: Sequence[ScalarPoint],
    event_state: Optional[EventState],
) -> Dict[str, FeatureObservation]:
    """Build the V5.8 information contract from causal observations only.

    Missing lookback history intentionally causes the corresponding feature to
    be absent. The intelligence gate will then return INSUFFICIENT_DATA.
    """
    out: Dict[str, FeatureObservation] = {}
    quotes = _causal_points(xau_quotes, decision_time_ms)
    if quotes:
        q = quotes[-1]
        if q.bid > 0 and q.ask > 0:
            spread = float(q.ask - q.bid)
            out["bid"] = FeatureObservation(q.bid, q.ts_ms, "BROKER_XAUUSD", "BROKER")
            out["ask"] = FeatureObservation(q.ask, q.ts_ms, "BROKER_XAUUSD", "BROKER")
            out["spread"] = FeatureObservation(spread, q.ts_ms, "BROKER_XAUUSD", "BROKER")

            q30 = [x for x in quotes if x.ts_ms >= decision_time_ms - 30 * 60_000]
            if q30:
                spreads = [float(x.ask - x.bid) for x in q30 if x.ask >= x.bid]
                if spreads:
                    out["spread_median_30m"] = FeatureObservation(
                        float(median(spreads)), q.ts_ms, "BROKER_XAUUSD", "BROKER"
                    )

            mids = [ScalarPoint(x.ts_ms, _quote_mid(x)) for x in quotes]
            r15 = _return_over(mids, decision_time_ms, 15 * 60_000)
            r60 = _return_over(mids, decision_time_ms, 60 * 60_000)
            if r15 is not None:
                out["ret_15m"] = FeatureObservation(r15, q.ts_ms, "BROKER_XAUUSD", "BROKER")
            if r60 is not None:
                out["ret_60m"] = FeatureObservation(r60, q.ts_ms, "BROKER_XAUUSD", "BROKER")

            for mins, name in ((30, "range_30m"), (60, "range_60m")):
                w = [x for x in quotes if x.ts_ms >= decision_time_ms - mins * 60_000]
                # Require history reaching the start of the lookback rather than
                # pretending a short partial window is a complete one.
                if w and w[0].ts_ms <= decision_time_ms - mins * 60_000 + 60_000:
                    vals = [_quote_mid(x) for x in w]
                    out[name] = FeatureObservation(max(vals) - min(vals), q.ts_ms, "BROKER_XAUUSD", "BROKER")

            mid = _quote_mid(q)
            lo, hi = sorted((float(proposal.lo), float(proposal.hi)))
            if mid < lo:
                near, far = lo - mid, hi - mid
            elif mid > hi:
                near, far = mid - hi, mid - lo
            else:
                near, far = 0.0, max(mid - lo, hi - mid)
            out["zone_near_distance"] = FeatureObservation(float(near), q.ts_ms, "DERIVED_CAUSAL", "OBSERVED")
            out["zone_far_distance"] = FeatureObservation(float(far), q.ts_ms, "DERIVED_CAUSAL", "OBSERVED")

    dxy = _causal_points(dxy_prices, decision_time_ms)
    if dxy:
        r15 = _return_over(dxy, decision_time_ms, 15 * 60_000)
        r60 = _return_over(dxy, decision_time_ms, 60 * 60_000)
        if r15 is not None:
            out["dxy_ret_15m"] = FeatureObservation(r15, dxy[-1].ts_ms, "DXY_FEED", "OBSERVED")
        if r60 is not None:
            out["dxy_ret_60m"] = FeatureObservation(r60, dxy[-1].ts_ms, "DXY_FEED", "OBSERVED")

    yields = _causal_points(us10y_yields_pct, decision_time_ms)
    if yields:
        # Input series is yield in percentage points. Difference *100 = bp.
        c15 = _change_over(yields, decision_time_ms, 15 * 60_000)
        c60 = _change_over(yields, decision_time_ms, 60 * 60_000)
        if c15 is not None:
            out["us10y_change_bps_15m"] = FeatureObservation(c15 * 100.0, yields[-1].ts_ms, "US10Y_FEED", "OBSERVED")
        if c60 is not None:
            out["us10y_change_bps_60m"] = FeatureObservation(c60 * 100.0, yields[-1].ts_ms, "US10Y_FEED", "OBSERVED")

    if event_state is not None and event_state.observed_at_ms <= decision_time_ms:
        if event_state.next_high_impact_ms is None:
            to_next = -1.0
        else:
            to_next = (event_state.next_high_impact_ms - decision_time_ms) / 60_000.0
        if event_state.last_high_impact_ms is None:
            since_last = -1.0
        else:
            since_last = (decision_time_ms - event_state.last_high_impact_ms) / 60_000.0
        quality = "FIRST_PARTY"
        out["minutes_to_next_high_impact"] = FeatureObservation(to_next, event_state.observed_at_ms, event_state.source, quality)
        out["minutes_since_last_high_impact"] = FeatureObservation(since_last, event_state.observed_at_ms, event_state.source, quality)
        out["high_impact_event_known"] = FeatureObservation(bool(event_state.state_known), event_state.observed_at_ms, event_state.source, quality)
        out["breaking_news_risk"] = FeatureObservation(bool(event_state.breaking_news_risk), event_state.observed_at_ms, event_state.source, quality)

    return out
