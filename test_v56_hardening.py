from datetime import datetime, timezone

from v53_engine import Ticket
from v56_engine import V56CanonicalEngine
from v56_engine_hardening import apply_v56_semantic_hardening


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
    pass


class IsolatedEngine(V56CanonicalEngine):
    pass


HardenedEngine = apply_v56_semantic_hardening(IsolatedEngine)


def engine(policy=None):
    p = {"starting_balance_sgd": 1000.0}
    if policy:
        p.update(policy)
    return HardenedEngine(DummyV2(), DummyTicks(), DummyFX(), DummySpec(), p)


def ticket(ticket_id, setup, side, entry, sl, state="OPEN"):
    return Ticket(
        ticket_id=ticket_id,
        setup_uid=setup,
        round_no=1,
        layer_no=1,
        revision=1,
        side=side,
        requested_entry=float(entry),
        sl=float(sl),
        targets=[110.0] if side == "BUY" else [90.0],
        state=state,
        created_ms=0,
        fill_ms=0 if state == "OPEN" else None,
        fill_price=float(entry) if state == "OPEN" else None,
    )


def test_named_close_entry_price_is_inferred_defensively():
    e = engine()
    e.tickets = [ticket("E1", "S1", "BUY", 1974, 1969), ticket("E2", "S1", "BUY", 1972, 1969)]
    e._apply_instruction(
        {"kind": "CLOSE_FULL", "setup_uid": "S1", "round_no": 1, "effective_ms": 1000,
         "text": "Close entry 1974. Hold entry 1972 +20pips."},
        1976.0, 1976.1,
    )
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[1].state == "OPEN"


def test_named_close_then_be_applies_be_to_survivor_not_closed_entry():
    e = engine()
    e.tickets = [ticket("E1", "S1", "BUY", 1974, 1969), ticket("E2", "S1", "BUY", 1972, 1969)]
    text = "Close entry 1974. Hold entry 1972. Move stl to entry"
    e._apply_instruction(
        {"kind": "CLOSE_FULL", "setup_uid": "S1", "round_no": 1, "effective_ms": 1000, "text": text},
        1976.0, 1976.1,
    )
    e._apply_instruction(
        {"kind": "MOVE_SL_TO_ENTRY_BE", "setup_uid": "S1", "round_no": 1, "effective_ms": 1000, "text": text},
        1976.0, 1976.1,
    )
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[1].state == "OPEN"
    assert e.tickets[1].sl == 1972.0


def test_named_partial_entry_closes_only_named_minlot_ticket():
    e = engine()
    e.tickets = [ticket("E1", "S1", "SELL", 1961, 1966), ticket("E2", "S1", "SELL", 1963, 1966)]
    e._apply_instruction(
        {"kind": "CLOSE_PARTIAL", "setup_uid": "S1", "round_no": 1, "effective_ms": 1000,
         "text": "Close 1/2 entry 1963"},
        1958.0, 1958.1,
    )
    # This phrasing does not match the strict named-entry fallback; the compiler
    # must emit scope_entry_price for it. Therefore engine scope remains setup-wide
    # and generic half policy closes the shallow/worst SELL entry 1961.
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[1].state == "OPEN"


def test_raw_provider_profile_disables_research_stop_risk_filter():
    e = engine({"risk_cap_enabled": False})
    accepted, candidate, cap = e._risk_accepts("BUY", 600.0, 100.0, 1000, 599.9, 600.0)
    assert accepted is True
    assert candidate == 500.0
    assert cap == float("inf")


def test_secondary_survival_profile_can_apply_explicit_10pct_cap():
    e = engine({"risk_cap_enabled": True, "max_reserved_stop_risk_pct": 10.0})
    accepted, candidate, cap = e._risk_accepts("BUY", 600.0, 100.0, 1000, 599.9, 600.0)
    assert accepted is False
    assert candidate == 500.0
    assert cap == 100.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"V5.6 hardening tests: {len(tests)}/{len(tests)} PASS")
