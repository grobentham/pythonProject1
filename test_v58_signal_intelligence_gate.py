from __future__ import annotations

import unittest

from v58_signal_intelligence_gate import (
    AccountEvidence,
    Decision,
    FeatureObservation,
    ModelEvidence,
    SignalIntelligenceGate,
    SignalProposal,
    required_information_contract,
)

NOW = 1_800_000_000_000


def obs(value, age_ms=1_000, quality="OBSERVED"):
    return FeatureObservation(value=value, observed_at_ms=NOW - age_ms, source="TEST", quality=quality)


def good_features():
    return {
        "bid": obs(2450.00, quality="BROKER"),
        "ask": obs(2450.20, quality="BROKER"),
        "spread": obs(0.20, quality="BROKER"),
        "spread_median_30m": obs(0.18, quality="BROKER"),
        "ret_15m": obs(0.0010),
        "ret_60m": obs(0.0020),
        "range_30m": obs(8.0),
        "range_60m": obs(12.0),
        "zone_near_distance": obs(2.0),
        "zone_far_distance": obs(5.0),
        "dxy_ret_15m": obs(-0.0005),
        "dxy_ret_60m": obs(-0.0010),
        "us10y_change_bps_15m": obs(-0.8),
        "us10y_change_bps_60m": obs(-1.5),
        "minutes_to_next_high_impact": obs(90.0),
        "minutes_since_last_high_impact": obs(60.0),
        "high_impact_event_known": obs(True, quality="FIRST_PARTY"),
        "breaking_news_risk": obs(False, quality="FIRST_PARTY"),
    }


def proposal():
    return SignalProposal(
        uid="sig-1", signal_time_ms=NOW - 5_000, side="BUY",
        lo=2445.0, hi=2448.0, sl=2439.0, tp=2458.0,
        provider_round=1, layer=1,
    )


def account(ok=True):
    return AccountEvidence(
        balance_sgd=1000.0, equity_sgd=1000.0, free_margin_sgd=900.0,
        projected_reserved_stop_risk_pct=4.0, projected_free_margin_pct=90.0,
        drawdown_from_hwm_pct=0.0, consecutive_losses=0, risk_gate_ok=ok,
    )


def strong_model(**overrides):
    d = dict(
        model_version="V57_FROZEN_SELECTOR_TEST",
        certified=True,
        score_observed_at_ms=NOW - 1_000,
        estimated_win_probability=0.61,
        estimated_ev_sgd=4.20,
        analog_n=140,
        analog_profit_factor=1.35,
        analog_mean_sgd=3.90,
    )
    d.update(overrides)
    return ModelEvidence(**d)


class TestSignalIntelligenceGate(unittest.TestCase):
    def setUp(self):
        self.gate = SignalIntelligenceGate()

    def decide(self, features=None, acct=None, model=None, prop=None):
        return self.gate.decide(
            prop or proposal(), NOW, features or good_features(),
            acct or account(), strong_model() if model is None else model,
        )

    def test_complete_certified_positive_context_can_take(self):
        r = self.decide()
        self.assertEqual(r.decision, Decision.TAKE)
        self.assertIn("FROZEN_EDGE_AND_CONTEXT_GATES_PASS", r.reasons)

    def test_missing_cross_market_data_abstains(self):
        f = good_features(); del f["dxy_ret_15m"]
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("dxy_ret_15m", r.missing_features)

    def test_stale_market_data_abstains(self):
        f = good_features(); f["bid"] = obs(2450.0, age_ms=120_000, quality="BROKER")
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("bid", r.stale_features)

    def test_future_observation_is_invalid(self):
        f = good_features()
        f["ret_15m"] = FeatureObservation(0.1, NOW + 1, "TEST", "OBSERVED")
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("ret_15m", r.invalid_features)

    def test_risk_failure_overrides_strong_model(self):
        r = self.decide(acct=account(False))
        self.assertEqual(r.decision, Decision.REJECT)
        self.assertEqual(r.reasons, ("ACCOUNT_RISK_GATE_FAILED",))

    def test_wide_spread_waits(self):
        f = good_features(); f["spread"] = obs(1.20, quality="BROKER")
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.WAIT)
        self.assertIn("SPREAD_OR_LIQUIDITY_UNFAVORABLE", r.reasons)

    def test_spread_multiple_shock_waits(self):
        f = good_features(); f["spread"] = obs(0.80, quality="BROKER"); f["spread_median_30m"] = obs(0.20, quality="BROKER")
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.WAIT)

    def test_imminent_high_impact_event_waits(self):
        f = good_features(); f["minutes_to_next_high_impact"] = obs(8.0)
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.WAIT)
        self.assertIn("HIGH_IMPACT_EVENT_IMMINENT", r.reasons)

    def test_post_event_discovery_waits(self):
        f = good_features(); f["minutes_since_last_high_impact"] = obs(2.0)
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.WAIT)
        self.assertIn("POST_EVENT_PRICE_DISCOVERY", r.reasons)

    def test_breaking_news_waits(self):
        f = good_features(); f["breaking_news_risk"] = obs(True, quality="FIRST_PARTY")
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.WAIT)
        self.assertIn("BREAKING_NEWS_RISK_ACTIVE", r.reasons)

    def test_unknown_event_state_abstains(self):
        f = good_features(); f["high_impact_event_known"] = obs(False, quality="FIRST_PARTY")
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("MACRO_EVENT_STATE_UNKNOWN", r.reasons)

    def test_no_model_abstains(self):
        r = self.gate.decide(proposal(), NOW, good_features(), account(), None)
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("NO_FROZEN_EDGE_MODEL", r.reasons)

    def test_uncertified_model_abstains(self):
        r = self.decide(model=strong_model(certified=False))
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("EDGE_MODEL_NOT_CERTIFIED", r.reasons)

    def test_future_model_score_abstains(self):
        r = self.decide(model=strong_model(score_observed_at_ms=NOW + 1))
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("MODEL_SCORE_FROM_FUTURE", r.reasons)

    def test_stale_model_score_abstains(self):
        r = self.decide(model=strong_model(score_observed_at_ms=NOW - 600_000))
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("MODEL_SCORE_STALE", r.reasons)

    def test_negative_edge_rejects(self):
        r = self.decide(model=strong_model(estimated_ev_sgd=-0.20, analog_profit_factor=0.95))
        self.assertEqual(r.decision, Decision.REJECT)
        self.assertIn("NO_POSITIVE_CERTIFIED_EDGE", r.reasons)

    def test_marginal_edge_is_reduced_only(self):
        r = self.decide(model=strong_model(analog_n=50, analog_profit_factor=1.04, estimated_ev_sgd=1.0))
        self.assertEqual(r.decision, Decision.TAKE_REDUCED)
        self.assertIn("MARGINAL_EDGE_REDUCED_EXPOSURE_ONLY", r.reasons)

    def test_wrong_side_geometry_rejects(self):
        p = SignalProposal("bad", NOW - 1000, "BUY", 2445, 2448, 2455, 2460)
        r = self.decide(prop=p)
        self.assertEqual(r.decision, Decision.REJECT)
        self.assertIn("WRONG_SIDE_PROVIDER_GEOMETRY", r.reasons)

    def test_invalid_quote_state_abstains(self):
        f = good_features(); f["bid"] = obs(2451.0, quality="BROKER"); f["ask"] = obs(2450.0, quality="BROKER")
        r = self.decide(f)
        self.assertEqual(r.decision, Decision.INSUFFICIENT_DATA)
        self.assertIn("INVALID_MARKET_QUOTE_STATE", r.reasons)

    def test_information_contract_exposes_all_domains(self):
        c = required_information_contract()
        self.assertEqual(set(c), {"market", "cross_market", "events_news", "account", "edge_model"})
        self.assertIn("dxy_ret_15m", c["cross_market"])
        self.assertIn("risk_gate_ok", c["account"])
        self.assertIn("estimated_ev_sgd", c["edge_model"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestSignalIntelligenceGate)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
