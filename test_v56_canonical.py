from datetime import datetime, timezone

from v53_engine import Ticket
from v56_canonical_policy import (
    canonical_partial_close_count,
    canonical_zone_entries,
    primary_or_choice,
    reentry_mode,
    scope_priority,
    size_directive,
    target_assignment,
    target_mode,
    worst_entry,
)
from v56_engine import V56CanonicalEngine


class DummyV2:
    @staticmethod
    def round_price(value, spec):
        return round(float(value) / spec.tick_size) * spec.tick_size

    @staticmethod
    def ms_to_dt(ms):
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)


class DummyFX:
    @staticmethod
    def rate_for_ms(ms):
        return 1.0

    @staticmethod
    def rate_for_date(day):
        return 1.0


class DummySpec:
    ounces = 1.0
    leverage = 500
    tick_size = 0.01
    commission_usd_per_side = 0.0
    margin_call_pct = 80.0
    stopout_pct = 50.0


class DummyTicks:
    def quote_at_or_after(self, ms, tolerance_ms=120_000):
        return int(ms), 100.0, 100.1


def engine():
    return V56CanonicalEngine(DummyV2(), DummyTicks(), DummyFX(), DummySpec(), {"starting_balance_sgd": 1000.0})


def open_ticket(ticket_id, setup, side, entry, sl, round_no=1):
    return Ticket(
        ticket_id=ticket_id,
        setup_uid=setup,
        round_no=round_no,
        layer_no=1,
        revision=1,
        side=side,
        requested_entry=float(entry),
        sl=float(sl),
        targets=[],
        state="OPEN",
        created_ms=0,
        fill_ms=0,
        fill_price=float(entry),
    )


def test_real_buy_zone_is_two_boundaries_not_three_synthetic_entries():
    # Corpus example: BUY 1974-1972, later "Close entry 1974. Hold entry 1972".
    assert canonical_zone_entries("BUY", 1972, 1974) == [1974.0, 1972.0]
    assert 1973.0 not in canonical_zone_entries("BUY", 1972, 1974)
    assert worst_entry("BUY", [1974, 1972]) == 1974.0


def test_real_sell_zone_is_two_boundaries_and_worst_is_shallow_sell():
    # Corpus example: SELL 1981-1983, later "Close entry 1981. Hold entry 1983".
    assert canonical_zone_entries("SELL", 1981, 1983) == [1981.0, 1983.0]
    assert 1982.0 not in canonical_zone_entries("SELL", 1981, 1983)
    assert worst_entry("SELL", [1981, 1983]) == 1981.0


def test_single_price_is_one_ticket_and_explicit_list_gets_no_synthetic_extra():
    assert canonical_zone_entries("SELL", 1973, 1973) == [1973.0]
    assert canonical_zone_entries("BUY", 100, 103, [103, 101, 100]) == [103.0, 101.0, 100.0]


def test_single_tp_mode_does_not_invent_tp2_tp3():
    assert target_mode([1985]) == "SINGLE_FINAL_TP_DYNAMIC_MANAGEMENT"
    assert target_assignment(2, [1985]) == [1985.0, 1985.0]
    assert target_mode([1980, 1990]) == "EXPLICIT_MULTI_TP_LADDER"
    assert target_assignment(2, [1980, 1990]) == [1980.0, 1990.0]


def test_partial_projection_matches_minimum_lot_reality():
    assert canonical_partial_close_count(1) == 1
    assert canonical_partial_close_count(2) == 1
    assert canonical_partial_close_count(3) == 2


def test_reentry_language_is_stateful_not_generic_signal_detection():
    assert reentry_mode("Wait for the price to come back and buy again") == "CONDITIONAL_REENTRY"
    assert reentry_mode("Buy again. Use small lot") == "IMMEDIATE_OR_EXPLICIT_ROUND_REENTRY"
    assert reentry_mode("ROUND 2") == "IMMEDIATE_OR_EXPLICIT_ROUND_REENTRY"
    assert reentry_mode("Oops. Bad news. Wait for new signal") == "REENTRY_PROHIBITED_UNTIL_NEW_SIGNAL"


def test_small_lot_maps_to_broker_minimum_without_inventing_smaller_size():
    assert size_directive("One more, small lot") == "BLUEBERRY_MIN_0_01"
    assert size_directive("Small volume!!!") == "BLUEBERRY_MIN_0_01"
    assert size_directive("Final Signal. Big lot") == "UNSUPPORTED_SIZE_ESCALATION_FAIL_CLOSED"


def test_or_choice_is_frozen_before_pnl_to_close_all_primary():
    assert primary_or_choice("Close All with 70pips. Or move stoploss to 1917") == "CLOSE_ALL"
    assert primary_or_choice("Move stoploss to entry") is None


def test_ambiguous_scope_fails_closed():
    assert scope_priority(recent_compatible_contexts=2) == "FAIL_CLOSED_AMBIGUOUS"
    assert scope_priority(direct_reply=True, recent_compatible_contexts=2) == "DIRECT_REPLY"
    assert scope_priority(explicit_entry_price=True, recent_compatible_contexts=2) == "EXPLICIT_ENTRY_PRICE"


def test_engine_arms_two_boundary_tickets_for_normal_zone():
    e = engine()
    ins = {
        "kind": "ARM_LIMIT_ROUND", "setup_uid": "S1", "round_no": 1,
        "side": "BUY", "zone_low": 1972, "zone_high": 1974,
        "sl": 1969, "targets": [1985], "effective_ms": 1000,
    }
    e._arm_round(ins, 1000, 1973.0, 1973.1)
    assert [t.requested_entry for t in e.tickets] == [1974.0, 1972.0]
    assert e.round_state[("S1", 1)]["target_mode"] == "SINGLE_FINAL_TP_DYNAMIC_MANAGEMENT"


def test_engine_market_now_is_one_ticket_not_whole_zone():
    e = engine()
    ins = {
        "kind": "MARKET_ENTRY_REQUEST", "setup_uid": "S1", "round_no": 1,
        "side": "BUY", "zone_low": 99, "zone_high": 101,
        "sl": 95, "targets": [110], "order_type": "MARKET",
        "effective_ms": 1000,
    }
    e._arm_round(ins, 1000, 100.0, 100.1)
    assert len(e.tickets) == 1
    assert e.tickets[0].state == "OPEN"
    assert e.tickets[0].fill_price == 100.1


def test_engine_single_tp_closes_remaining_exposure_only_at_final_tp():
    e = engine()
    e.round_state[("S1", 1)] = {
        "side": "BUY", "targets": [110.0],
        "target_mode": "SINGLE_FINAL_TP_DYNAMIC_MANAGEMENT", "stage": 0,
    }
    e.tickets = [
        open_ticket("E1", "S1", "BUY", 100, 95),
        open_ticket("E2", "S1", "BUY", 98, 95),
    ]
    e._handle_target(("S1", 1), 2000, 110.0, 110.1)
    assert all(t.state == "CLOSED" for t in e.tickets)
    assert all(t.exit_reason == "SINGLE_FINAL_TP" for t in e.tickets)
    assert e.round_state[("S1", 1)]["stage"] == 1


def test_engine_explicit_two_tp_ladder_matches_entry1_entry2_intent():
    e = engine()
    e.round_state[("S1", 1)] = {
        "side": "BUY", "targets": [105.0, 110.0],
        "target_mode": "EXPLICIT_MULTI_TP_LADDER", "stage": 0,
    }
    e.tickets = [
        open_ticket("E1", "S1", "BUY", 100, 95),
        open_ticket("E2", "S1", "BUY", 98, 95),
    ]
    e._handle_target(("S1", 1), 1000, 105.0, 105.1)
    assert e.tickets[0].state == "CLOSED" and e.tickets[0].exit_reason == "TP1_SCALE"
    assert e.tickets[1].state == "OPEN" and e.tickets[1].sl == 98.0
    e._handle_target(("S1", 1), 2000, 110.0, 110.1)
    assert e.tickets[1].state == "CLOSED" and e.tickets[1].exit_reason == "TP2_FINAL"


def test_engine_close_all_buy_is_side_scoped_not_account_wide():
    e = engine()
    e.tickets = [
        open_ticket("B", "BUY1", "BUY", 100, 95),
        open_ticket("S", "SELL1", "SELL", 100, 105),
    ]
    e._apply_instruction({"kind": "CLOSE_FULL", "text": "Close all BUY", "effective_ms": 1000}, 102.0, 102.1)
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[1].state == "OPEN"


def test_engine_or_choice_executes_only_frozen_close_all_branch():
    e = engine()
    e.tickets = [
        open_ticket("E1", "S1", "BUY", 100, 95),
        open_ticket("E2", "S1", "BUY", 98, 95),
    ]
    ins = {
        "kind": "SET_SL_PRICE", "setup_uid": "S1", "round_no": 1,
        "source_msg_id": 187, "effective_ms": 1000,
        "text": "+75pips Close all or move stl to 1974",
        "new_sl": 1974,
    }
    e._apply_instruction(ins, 106.0, 106.1)
    assert all(t.state == "CLOSED" for t in e.tickets)
    assert e.counters["or_choice_primary_close_all"] == 1
    # A second expanded action from the same source message is suppressed.
    e._apply_instruction({**ins, "kind": "CLOSE_FULL"}, 106.0, 106.1)
    assert e.counters["or_choice_duplicate_action_suppressed"] == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"V5.6 canonical provider tests: {len(tests)}/{len(tests)} PASS")
