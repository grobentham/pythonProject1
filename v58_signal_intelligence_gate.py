from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


class Decision(str, Enum):
    TAKE = "TAKE"
    TAKE_REDUCED = "TAKE_REDUCED"
    WAIT = "WAIT"
    REJECT = "REJECT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class DataStatus(str, Enum):
    OK = "OK"
    MISSING = "MISSING"
    STALE = "STALE"
    INVALID = "INVALID"


@dataclass(frozen=True)
class FeatureObservation:
    value: Any
    observed_at_ms: int
    source: str
    quality: str = "OBSERVED"


@dataclass(frozen=True)
class SignalProposal:
    uid: str
    signal_time_ms: int
    side: str
    lo: float
    hi: float
    sl: float
    tp: float
    provider_round: int = 1
    layer: int = 1


@dataclass(frozen=True)
class ModelEvidence:
    model_version: str
    certified: bool
    score_observed_at_ms: int
    estimated_win_probability: Optional[float] = None
    estimated_ev_sgd: Optional[float] = None
    analog_n: int = 0
    analog_profit_factor: Optional[float] = None
    analog_mean_sgd: Optional[float] = None


@dataclass(frozen=True)
class AccountEvidence:
    balance_sgd: float
    equity_sgd: float
    free_margin_sgd: float
    projected_reserved_stop_risk_pct: float
    projected_free_margin_pct: float
    drawdown_from_hwm_pct: float
    consecutive_losses: int
    risk_gate_ok: bool


@dataclass(frozen=True)
class DecisionPolicy:
    # Data freshness. These are intentionally strict and apply at decision time.
    max_market_age_ms: int = 90_000
    max_cross_market_age_ms: int = 180_000
    max_calendar_age_ms: int = 6 * 60 * 60 * 1000
    max_news_age_ms: int = 15 * 60 * 1000
    max_model_age_ms: int = 5 * 60 * 1000

    # Execution / event gates.
    max_spread_usd: float = 1.00
    spread_multiple_limit: float = 3.0
    high_impact_wait_minutes: int = 15
    post_event_wait_minutes: int = 5

    # Edge/evidence gates. These do not create edge; they only define what a
    # separately calibrated, frozen selector must demonstrate before TAKE.
    min_analog_n: int = 75
    min_analog_profit_factor: float = 1.10
    min_estimated_ev_sgd: float = 0.0
    min_win_probability: float = 0.50

    # Reduced-size band. V5.8 never changes broker minimum lot sizing itself;
    # this is only an advisory state for a downstream sizing policy.
    reduced_ev_sgd: float = 0.0
    reduced_min_analog_n: int = 40

    # Fail closed when these are unavailable.
    required_market_features: Tuple[str, ...] = (
        "bid", "ask", "spread", "spread_median_30m",
        "ret_15m", "ret_60m", "range_30m", "range_60m",
        "zone_near_distance", "zone_far_distance",
    )
    required_cross_market_features: Tuple[str, ...] = (
        "dxy_ret_15m", "dxy_ret_60m", "us10y_change_bps_15m", "us10y_change_bps_60m",
    )
    required_event_features: Tuple[str, ...] = (
        "minutes_to_next_high_impact", "minutes_since_last_high_impact",
        "high_impact_event_known", "breaking_news_risk",
    )


@dataclass(frozen=True)
class DecisionResult:
    decision: Decision
    reasons: Tuple[str, ...]
    missing_features: Tuple[str, ...] = ()
    stale_features: Tuple[str, ...] = ()
    invalid_features: Tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value
        return d


class SignalIntelligenceGate:
    """Causal, fail-closed gate between provider idea and execution.

    Important invariants:
    - Provider direction is an input, never treated as truth.
    - Only observations timestamped at or before decision_time_ms are accepted.
    - Missing/stale/invalid required context produces INSUFFICIENT_DATA.
    - Hard risk failure produces REJECT regardless of model score.
    - Event/spread uncertainty can produce WAIT rather than a directional guess.
    - TAKE requires a separately frozen/certified model plus minimum historical
      analogue support. V5.8 itself does not train or optimize that model.
    - This module does not place orders and does not authorize live trading.
    """

    def __init__(self, policy: DecisionPolicy | None = None):
        self.policy = policy or DecisionPolicy()

    @staticmethod
    def _num(v: Any) -> Optional[float]:
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        if x != x or x in (float("inf"), float("-inf")):
            return None
        return x

    def _check_feature(
        self,
        name: str,
        features: Mapping[str, FeatureObservation],
        decision_time_ms: int,
        max_age_ms: int,
    ) -> Tuple[DataStatus, Optional[FeatureObservation]]:
        obs = features.get(name)
        if obs is None:
            return DataStatus.MISSING, None
        if obs.observed_at_ms > decision_time_ms:
            return DataStatus.INVALID, obs
        age = decision_time_ms - obs.observed_at_ms
        if age > max_age_ms:
            return DataStatus.STALE, obs
        if obs.quality not in {"OBSERVED", "FIRST_PARTY", "BROKER", "FROZEN_MODEL"}:
            return DataStatus.INVALID, obs
        return DataStatus.OK, obs

    def _validate_group(
        self,
        names: Iterable[str],
        features: Mapping[str, FeatureObservation],
        decision_time_ms: int,
        max_age_ms: int,
    ) -> Tuple[list[str], list[str], list[str]]:
        missing: list[str] = []
        stale: list[str] = []
        invalid: list[str] = []
        for n in names:
            status, _ = self._check_feature(n, features, decision_time_ms, max_age_ms)
            if status is DataStatus.MISSING:
                missing.append(n)
            elif status is DataStatus.STALE:
                stale.append(n)
            elif status is DataStatus.INVALID:
                invalid.append(n)
        return missing, stale, invalid

    def decide(
        self,
        proposal: SignalProposal,
        decision_time_ms: int,
        features: Mapping[str, FeatureObservation],
        account: AccountEvidence,
        model: Optional[ModelEvidence],
    ) -> DecisionResult:
        p = self.policy
        reasons: list[str] = []
        missing: list[str] = []
        stale: list[str] = []
        invalid: list[str] = []

        if decision_time_ms < proposal.signal_time_ms:
            return DecisionResult(
                Decision.INSUFFICIENT_DATA,
                ("DECISION_PRECEDES_SIGNAL",),
                invalid_features=("decision_time_ms",),
            )

        side = proposal.side.upper()
        if side not in {"BUY", "SELL"}:
            return DecisionResult(
                Decision.REJECT,
                ("INVALID_PROVIDER_SIDE",),
                invalid_features=("side",),
            )
        lo, hi = sorted((proposal.lo, proposal.hi))
        if not (lo > 0 and hi > 0 and proposal.sl > 0 and proposal.tp > 0):
            return DecisionResult(
                Decision.REJECT,
                ("INVALID_PROVIDER_GEOMETRY",),
                invalid_features=("geometry",),
            )
        if side == "BUY" and not (proposal.sl < hi and proposal.tp > lo):
            return DecisionResult(
                Decision.REJECT,
                ("WRONG_SIDE_PROVIDER_GEOMETRY",),
                invalid_features=("geometry",),
            )
        if side == "SELL" and not (proposal.sl > lo and proposal.tp < hi):
            return DecisionResult(
                Decision.REJECT,
                ("WRONG_SIDE_PROVIDER_GEOMETRY",),
                invalid_features=("geometry",),
            )

        for names, age in (
            (p.required_market_features, p.max_market_age_ms),
            (p.required_cross_market_features, p.max_cross_market_age_ms),
            (p.required_event_features, p.max_news_age_ms),
        ):
            m, s, i = self._validate_group(names, features, decision_time_ms, age)
            missing += m; stale += s; invalid += i

        if missing or stale or invalid:
            if missing:
                reasons.append("REQUIRED_CONTEXT_MISSING")
            if stale:
                reasons.append("REQUIRED_CONTEXT_STALE")
            if invalid:
                reasons.append("REQUIRED_CONTEXT_INVALID_OR_FUTURE")
            return DecisionResult(
                Decision.INSUFFICIENT_DATA,
                tuple(reasons),
                tuple(sorted(set(missing))),
                tuple(sorted(set(stale))),
                tuple(sorted(set(invalid))),
            )

        # Numeric sanity for fields used in hard gates.
        required_numeric = [
            "bid", "ask", "spread", "spread_median_30m",
            "minutes_to_next_high_impact", "minutes_since_last_high_impact",
            "dxy_ret_15m", "dxy_ret_60m", "us10y_change_bps_15m", "us10y_change_bps_60m",
            "ret_15m", "ret_60m", "range_30m", "range_60m",
        ]
        nums: Dict[str, float] = {}
        for name in required_numeric:
            x = self._num(features[name].value)
            if x is None:
                invalid.append(name)
            else:
                nums[name] = x
        if invalid:
            return DecisionResult(
                Decision.INSUFFICIENT_DATA,
                ("NON_NUMERIC_REQUIRED_CONTEXT",),
                invalid_features=tuple(sorted(set(invalid))),
            )

        if nums["ask"] < nums["bid"] or nums["spread"] < 0 or nums["spread_median_30m"] <= 0:
            return DecisionResult(
                Decision.INSUFFICIENT_DATA,
                ("INVALID_MARKET_QUOTE_STATE",),
                invalid_features=("bid/ask/spread",),
            )

        if not account.risk_gate_ok:
            return DecisionResult(
                Decision.REJECT,
                ("ACCOUNT_RISK_GATE_FAILED",),
                diagnostics={
                    "projected_reserved_stop_risk_pct": account.projected_reserved_stop_risk_pct,
                    "projected_free_margin_pct": account.projected_free_margin_pct,
                    "drawdown_from_hwm_pct": account.drawdown_from_hwm_pct,
                    "consecutive_losses": account.consecutive_losses,
                },
            )

        # Execution quality gates are temporary-state abstentions, not alpha calls.
        spread_multiple = nums["spread"] / nums["spread_median_30m"]
        if nums["spread"] > p.max_spread_usd or spread_multiple > p.spread_multiple_limit:
            return DecisionResult(
                Decision.WAIT,
                ("SPREAD_OR_LIQUIDITY_UNFAVORABLE",),
                diagnostics={"spread": nums["spread"], "spread_multiple": spread_multiple},
            )

        known_event = bool(features["high_impact_event_known"].value)
        breaking_news = bool(features["breaking_news_risk"].value)
        if breaking_news:
            return DecisionResult(Decision.WAIT, ("BREAKING_NEWS_RISK_ACTIVE",))
        if not known_event:
            return DecisionResult(Decision.INSUFFICIENT_DATA, ("MACRO_EVENT_STATE_UNKNOWN",))
        if 0 <= nums["minutes_to_next_high_impact"] <= p.high_impact_wait_minutes:
            return DecisionResult(
                Decision.WAIT,
                ("HIGH_IMPACT_EVENT_IMMINENT",),
                diagnostics={"minutes_to_event": nums["minutes_to_next_high_impact"]},
            )
        if 0 <= nums["minutes_since_last_high_impact"] <= p.post_event_wait_minutes:
            return DecisionResult(
                Decision.WAIT,
                ("POST_EVENT_PRICE_DISCOVERY",),
                diagnostics={"minutes_since_event": nums["minutes_since_last_high_impact"]},
            )

        if model is None:
            return DecisionResult(Decision.INSUFFICIENT_DATA, ("NO_FROZEN_EDGE_MODEL",))
        if not model.certified:
            return DecisionResult(Decision.INSUFFICIENT_DATA, ("EDGE_MODEL_NOT_CERTIFIED",))
        if model.score_observed_at_ms > decision_time_ms:
            return DecisionResult(
                Decision.INSUFFICIENT_DATA,
                ("MODEL_SCORE_FROM_FUTURE",),
                invalid_features=("model_score",),
            )
        if decision_time_ms - model.score_observed_at_ms > p.max_model_age_ms:
            return DecisionResult(
                Decision.INSUFFICIENT_DATA,
                ("MODEL_SCORE_STALE",),
                stale_features=("model_score",),
            )

        ev = model.estimated_ev_sgd
        prob = model.estimated_win_probability
        if ev is None or prob is None or model.analog_profit_factor is None:
            return DecisionResult(Decision.INSUFFICIENT_DATA, ("EDGE_EVIDENCE_INCOMPLETE",))
        if not all(self._num(x) is not None for x in (ev, prob, model.analog_profit_factor)):
            return DecisionResult(Decision.INSUFFICIENT_DATA, ("EDGE_EVIDENCE_INVALID",))

        diagnostics = {
            "model_version": model.model_version,
            "estimated_ev_sgd": float(ev),
            "estimated_win_probability": float(prob),
            "analog_n": int(model.analog_n),
            "analog_profit_factor": float(model.analog_profit_factor),
            "analog_mean_sgd": model.analog_mean_sgd,
            "spread": nums["spread"],
            "spread_multiple": spread_multiple,
            "provider_side": side,
            "provider_round": proposal.provider_round,
            "provider_layer": proposal.layer,
        }

        if (
            model.analog_n >= p.min_analog_n
            and model.analog_profit_factor >= p.min_analog_profit_factor
            and float(ev) > p.min_estimated_ev_sgd
            and float(prob) >= p.min_win_probability
        ):
            return DecisionResult(Decision.TAKE, ("FROZEN_EDGE_AND_CONTEXT_GATES_PASS",), diagnostics=diagnostics)

        if (
            model.analog_n >= p.reduced_min_analog_n
            and model.analog_profit_factor >= 1.0
            and float(ev) > p.reduced_ev_sgd
            and float(prob) >= p.min_win_probability
        ):
            return DecisionResult(Decision.TAKE_REDUCED, ("MARGINAL_EDGE_REDUCED_EXPOSURE_ONLY",), diagnostics=diagnostics)

        return DecisionResult(Decision.REJECT, ("NO_POSITIVE_CERTIFIED_EDGE",), diagnostics=diagnostics)


def required_information_contract(policy: DecisionPolicy | None = None) -> Dict[str, Sequence[str]]:
    p = policy or DecisionPolicy()
    return {
        "market": p.required_market_features,
        "cross_market": p.required_cross_market_features,
        "events_news": p.required_event_features,
        "account": (
            "balance_sgd", "equity_sgd", "free_margin_sgd",
            "projected_reserved_stop_risk_pct", "projected_free_margin_pct",
            "drawdown_from_hwm_pct", "consecutive_losses", "risk_gate_ok",
        ),
        "edge_model": (
            "model_version", "certified", "score_observed_at_ms",
            "estimated_win_probability", "estimated_ev_sgd", "analog_n",
            "analog_profit_factor", "analog_mean_sgd",
        ),
    }
