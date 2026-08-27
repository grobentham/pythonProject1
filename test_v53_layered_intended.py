from datetime import datetime, timezone

from v53_engine import Ticket, V53PortfolioEngine
from v53_engine_patch import apply_v53_integrity_patch


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
        return None


PatchedEngine = apply_v53_integrity_patch(V53PortfolioEngine)


def engine():
    return PatchedEngine(
        DummyV2(), DummyTicks(), DummyFX(), DummySpec(),
        {"starting_balance_sgd": 1000.0},
    )


def ticket(layer, entry, state="OPEN", side="BUY"):
    return Ticket(
        ticket_id=f"S1__R1__L{layer}",
        setup_uid="S1",
        round_no=1,
        layer_no=layer,
        revision=1,
        side=side,
        requested_entry=float(entry),
        sl=90.0 if side == "BUY" else 110.0,
        targets=[105.0, 110.0, 115.0] if side == "BUY" else [95.0, 90.0, 85.0],
        state=state,
        created_ms=0,
        fill_ms=0 if state == "OPEN" else None,
        fill_price=float(entry) if state == "OPEN" else None,
    )


def test_one_fill_tp1_closes_only_filled_and_cancels_pending():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98, "PENDING"), ticket(3, 96, "PENDING")]
    e._handle_target(("S1", 1), 1_000, 105.0, 105.1)
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[0].exit_reason == "TP1_SINGLE"
    assert e.tickets[1].state == "CANCELLED"
    assert e.tickets[2].state == "CANCELLED"
    assert e.round_state[("S1", 1)]["stage"] == 1


def test_two_fills_tp1_then_be_then_tp2():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96, "PENDING")]
    e._handle_target(("S1", 1), 1_000, 105.0, 105.1)
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[0].exit_reason == "TP1_SCALE"
    assert e.tickets[1].state == "OPEN"
    assert e.tickets[1].sl == e.tickets[1].fill_price == 98.0
    assert e.tickets[2].state == "CANCELLED"
    e._handle_target(("S1", 1), 2_000, 110.0, 110.1)
    assert e.tickets[1].state == "CLOSED"
    assert e.tickets[1].exit_reason == "TP2_FINAL"
    assert e.round_state[("S1", 1)]["stage"] == 2


def test_three_fills_tp1_be_tp2_tp3_exact_ladder():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96)]
    e._handle_target(("S1", 1), 1_000, 105.0, 105.1)
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[0].exit_reason == "TP1_SCALE"
    assert e.tickets[1].state == "OPEN" and e.tickets[1].sl == 98.0
    assert e.tickets[2].state == "OPEN" and e.tickets[2].sl == 96.0
    e._handle_target(("S1", 1), 2_000, 110.0, 110.1)
    assert e.tickets[1].state == "CLOSED"
    assert e.tickets[1].exit_reason == "TP2_SCALE"
    assert e.tickets[2].state == "OPEN"
    e._handle_target(("S1", 1), 3_000, 115.0, 115.1)
    assert e.tickets[2].state == "CLOSED"
    assert e.tickets[2].exit_reason == "TP3_FINAL"
    assert e.round_state[("S1", 1)]["stage"] == 3
    assert abs(e.cash - 1036.0) < 1e-9


def test_break_even_after_tp1_can_close_survivors_without_full_sl_loss():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96)]
    e._handle_target(("S1", 1), 1_000, 105.0, 105.1)
    for t in list(e._round_open(("S1", 1))):
        e._close_ticket(t, 2_000, t.fill_price, t.fill_price + 0.1, "BREAK_EVEN_SL", forced_price=t.fill_price)
    assert all(t.state == "CLOSED" for t in e.tickets)
    assert abs(e.cash - 1005.0) < 1e-9


def test_provider_close_full_overrides_remaining_ladder():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96)]
    e._apply_instruction(
        {"kind": "CLOSE_FULL", "setup_uid": "S1", "round_no": 1, "effective_ms": 1_000},
        bid=102.0, ask=102.1,
    )
    assert all(t.state == "CLOSED" for t in e.tickets)
    assert all(t.exit_reason == "PROVIDER_CLOSE" for t in e.tickets)


def test_sell_three_fill_ladder_is_symmetric():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [95.0, 90.0, 85.0], "stage": 0}
    e.tickets = [ticket(1, 100, side="SELL"), ticket(2, 102, side="SELL"), ticket(3, 104, side="SELL")]
    e._handle_target(("S1", 1), 1_000, 94.9, 95.0)
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[1].sl == 102.0 and e.tickets[2].sl == 104.0
    e._handle_target(("S1", 1), 2_000, 89.9, 90.0)
    assert e.tickets[1].state == "CLOSED" and e.tickets[2].state == "OPEN"
    e._handle_target(("S1", 1), 3_000, 84.9, 85.0)
    assert e.tickets[2].state == "CLOSED"


def test_tp1_then_provider_combined_partial_be_is_idempotent():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96)]
    e._handle_target(("S1", 1), 1_000, 105.0, 105.1)
    assert len(e._round_open(("S1", 1))) == 2
    text = "Running +35pips. Close 1/2. Stoploss to entry"
    e._apply_instruction(
        {"kind": "CLOSE_PARTIAL", "setup_uid": "S1", "round_no": 1, "effective_ms": 1_500, "text": text},
        bid=106.0, ask=106.1,
    )
    assert len(e._round_open(("S1", 1))) == 2
    assert e.counters["provider_partial_tp1_confirmation_suppressed"] == 1
    e._apply_instruction(
        {"kind": "MOVE_SL_TO_ENTRY_BE", "setup_uid": "S1", "round_no": 1, "effective_ms": 1_500, "text": text},
        bid=106.0, ask=106.1,
    )
    assert len(e._round_open(("S1", 1))) == 2
    assert all(t.sl == t.fill_price for t in e._round_open(("S1", 1)))


def test_later_standalone_partial_after_tp1_still_executes():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96)]
    e._handle_target(("S1", 1), 1_000, 105.0, 105.1)
    e._apply_instruction(
        {"kind": "CLOSE_PARTIAL", "setup_uid": "S1", "round_no": 1, "effective_ms": 1_500,
         "text": "Running +35pips Close 1/2 Stoploss to entry"},
        bid=106.0, ask=106.1,
    )
    assert len(e._round_open(("S1", 1))) == 2
    e._apply_instruction(
        {"kind": "CLOSE_PARTIAL", "setup_uid": "S1", "round_no": 1, "effective_ms": 2_500,
         "text": "Secure more profit - close 1/2"},
        bid=108.0, ask=108.1,
    )
    assert len(e._round_open(("S1", 1))) == 1
    assert sum((t.exit_reason or "").startswith("PROVIDER_PARTIAL") for t in e.tickets) == 1


def test_provider_partial_before_tp1_is_not_suppressed():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96)]
    e._apply_instruction(
        {"kind": "CLOSE_PARTIAL", "setup_uid": "S1", "round_no": 1, "effective_ms": 500,
         "text": "Running +35pips Close 1/2 Stoploss to entry"},
        bid=103.0, ask=103.1,
    )
    assert len(e._round_open(("S1", 1))) == 1
    assert e.counters["provider_partial_tp1_confirmation_suppressed"] == 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"Provider-intended layered tests: {len(tests)}/{len(tests)} PASS")
