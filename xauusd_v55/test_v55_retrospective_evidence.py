import json
import unittest
from pathlib import Path


class V55RetrospectiveEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).with_name("V55_RETROSPECTIVE_BLUEBERRY_FIXED_DEPTH_CHECKPOINT.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def test_fixed_depth_0p75_is_disqualified_for_s100(self):
        c = self.data["s100_fixed_ticket_invariance_check"]
        self.assertEqual(c["starting_balance_sgd"], 100.0)
        self.assertEqual(c["high_water_drawdown_shutdown_fraction"], 0.15)
        self.assertAlmostEqual(c["initial_shutdown_floor_sgd"], 85.0)
        self.assertLess(
            c["counterfactual_balance_after_first_partial_month_if_same_fixed_depth_stream_continued_without_shutdown_sgd"],
            c["initial_shutdown_floor_sgd"],
        )
        self.assertEqual(
            c["fixed_depth_0p75_s100_verdict"],
            "DISQUALIFIED_BY_15PCT_HIGH_WATER_DRAWDOWN_GATE",
        )

    def test_fixed_depth_result_cannot_be_relabelled_as_adaptive_v55(self):
        guard = self.data["important_scope_guard"]
        self.assertFalse(guard["adaptive_v55_5pct_risk_filtered_stream_evaluated"])
        self.assertEqual(
            guard["adaptive_v55_s100_verdict"],
            "UNSCORED_REQUIRES_PER_TRADE_BROKER_TICK_REPLAY",
        )

    def test_checkpoint_never_authorizes_live_orders(self):
        p = self.data["promotion"]
        self.assertFalse(p["historical_profitability_certified"])
        self.assertFalse(p["s100_survival_certified"])
        self.assertFalse(p["prospective_certified"])
        self.assertFalse(p["live_ready"])
        self.assertFalse(p["real_orders_authorized"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
