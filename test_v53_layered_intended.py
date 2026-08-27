from datetime import datetime, timezone

from v53_engine import Ticket, V53PortfolioEngine


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


def engine():
    return V53PortfolioEngine(
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

    # TP1: the worst/shallowest BUY entry exits; E2/E3 move to own BE.
    e._handle_target(("S1", 1), 1_000, 105.0, 105.1)
    assert e.tickets[0].state == "CLOSED"
    assert e.tickets[0].exit_reason == "TP1_SCALE"
    assert e.tickets[1].state == "OPEN" and e.tickets[1].sl == 98.0
    assert e.tickets[2].state == "OPEN" and e.tickets[2].sl == 96.0

    # TP2: next-worst remaining entry exits; deepest ticket remains.
    e._handle_target(("S1", 1), 2_000, 110.0, 110.1)
    assert e.tickets[1].state == "CLOSED"
    assert e.tickets[1].exit_reason == "TP2_SCALE"
    assert e.tickets[2].state == "OPEN"

    # TP3: final/deepest ticket exits.
    e._handle_target(("S1", 1), 3_000, 115.0, 115.1)
    assert e.tickets[2].state == "CLOSED"
    assert e.tickets[2].exit_reason == "TP3_FINAL"
    assert e.round_state[("S1", 1)]["stage"] == 3

    # With zero commission and FX=1: (105-100)+(110-98)+(115-96)=36.
    assert abs(e.cash - 1036.0) < 1e-9


def test_break_even_after_tp1_can_close_survivors_without_full_sl_loss():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96)]
    e._handle_target(("S1", 1), 1_000, 105.0, 105.1)

    # Simulate price returning to each survivor's own break-even after TP1.
    for t in list(e._round_open(("S1", 1))):
        e._close_ticket(t, 2_000, t.fill_price, t.fill_price + 0.1, "BREAK_EVEN_SL", forced_price=t.fill_price)

    assert all(t.state == "CLOSED" for t in e.tickets)
    assert abs(e.cash - 1005.0) < 1e-9


def test_provider_close_full_overrides_remaining_ladder():
    e = engine()
    e.round_state[("S1", 1)] = {"targets": [105.0, 110.0, 115.0], "stage": 0}
    e.tickets = [ticket(1, 100), ticket(2, 98), ticket(3, 96)]

    # Explicit Telegram/provider close must flatten the scoped setup immediately.
    e._apply_instruction(
        {"kind": "CLOSE_FULL", "setup_uid": "S1", "round_no": 1, "effective_ms": 1_000},
        bid=102.0,
        ask=102.1,
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"Provider-intended layered tests: {len(tests)}/{len(tests)} PASS")
