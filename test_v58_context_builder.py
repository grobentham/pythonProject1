from __future__ import annotations

import unittest

from v58_context_builder import EventState, QuotePoint, ScalarPoint, build_context_features
from v58_signal_intelligence_gate import SignalProposal

NOW = 1_800_000_000_000


def proposal():
    return SignalProposal("sig", NOW - 5_000, "BUY", 2445.0, 2448.0, 2439.0, 2458.0)


class TestContextBuilder(unittest.TestCase):
    def test_builds_complete_causal_context_from_sufficient_history(self):
        quotes = []
        for m in range(0, 66):
            ts = NOW - (65 - m) * 60_000
            mid = 2440.0 + m * 0.10
            quotes.append(QuotePoint(ts, mid - 0.10, mid + 0.10))
        dxy = [ScalarPoint(NOW - (65 - m) * 60_000, 100.0 + m * 0.01) for m in range(66)]
        y10 = [ScalarPoint(NOW - (65 - m) * 60_000, 4.00 + m * 0.001) for m in range(66)]
        ev = EventState(NOW - 1_000, True, NOW + 90 * 60_000, NOW - 60 * 60_000, False)
        f = build_context_features(proposal(), NOW, quotes, dxy, y10, ev)
        required = {
            "bid", "ask", "spread", "spread_median_30m", "ret_15m", "ret_60m",
            "range_30m", "range_60m", "zone_near_distance", "zone_far_distance",
            "dxy_ret_15m", "dxy_ret_60m", "us10y_change_bps_15m", "us10y_change_bps_60m",
            "minutes_to_next_high_impact", "minutes_since_last_high_impact",
            "high_impact_event_known", "breaking_news_risk",
        }
        self.assertTrue(required.issubset(f))
        self.assertGreater(f["ret_60m"].value, 0)
        self.assertGreater(f["dxy_ret_60m"].value, 0)
        self.assertGreater(f["us10y_change_bps_60m"].value, 0)

    def test_future_market_points_are_never_used(self):
        quotes = [
            QuotePoint(NOW - 60 * 60_000, 2400, 2400.2),
            QuotePoint(NOW, 2450, 2450.2),
            QuotePoint(NOW + 1, 9999, 10000),
        ]
        f = build_context_features(proposal(), NOW, quotes, [], [], None)
        self.assertAlmostEqual(f["bid"].value, 2450.0)
        self.assertLess(f["bid"].value, 3000)

    def test_incomplete_history_omits_long_lookbacks(self):
        quotes = [QuotePoint(NOW - i * 60_000, 2450 - i * 0.1, 2450.2 - i * 0.1) for i in range(10, -1, -1)]
        f = build_context_features(proposal(), NOW, quotes, [], [], None)
        self.assertNotIn("ret_60m", f)
        self.assertNotIn("range_60m", f)

    def test_missing_event_feed_does_not_invent_event_state(self):
        f = build_context_features(proposal(), NOW, [], [], [], None)
        self.assertNotIn("high_impact_event_known", f)
        self.assertNotIn("breaking_news_risk", f)

    def test_event_minutes_are_computed_at_decision_boundary(self):
        ev = EventState(NOW - 1000, True, NOW + 12 * 60_000, NOW - 3 * 60_000, True)
        f = build_context_features(proposal(), NOW, [], [], [], ev)
        self.assertAlmostEqual(f["minutes_to_next_high_impact"].value, 12.0)
        self.assertAlmostEqual(f["minutes_since_last_high_impact"].value, 3.0)
        self.assertTrue(f["breaking_news_risk"].value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
