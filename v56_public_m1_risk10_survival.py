from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from v56_canonical_policy import canonical_partial_close_count, canonical_zone_entries
from v56_public_m1_account_survival import (
    Account, Ticket, START_SGD, SGD_PER_USD_PROXY, COMMISSION_SGD_SIDE,
    LEVERAGE, MARGIN_CALL_PCT, STOPOUT_PCT, OZ,
    load_market, load_inputs, schedule_first_touch, iso,
)

RISK_CAP_PCT = 10.0
OUT_DIR = Path('xauusd_replay/v56_risk10_survival_output')


class RiskAccount(Account):
    def stop_risk_sgd(self, t: Ticket) -> float:
        entry = t.fill_price if t.state == 'OPEN' and t.fill_price is not None else t.requested_entry
        if t.side == 'BUY':
            distance = max(0.0, float(entry) - float(t.sl))
        else:
            distance = max(0.0, float(t.sl) - float(entry))
        return distance * OZ * SGD_PER_USD_PROXY

    def reserved_stop_risk_sgd(self) -> float:
        return sum(self.stop_risk_sgd(t) for t in self.open_map.values()) + sum(self.stop_risk_sgd(t) for t in self.pending_map.values())

    def risk_cap_sgd(self, i: int) -> float:
        return max(0.0, self.equity(i, 'open')) * RISK_CAP_PCT / 100.0

    def arm_with_risk(self, t: Ticket, i: int) -> bool:
        candidate = self.stop_risk_sgd(t)
        existing = self.reserved_stop_risk_sgd()
        cap = self.risk_cap_sgd(i)
        self.tickets.append(t)
        self.by_uid[t.uid].append(t)
        if existing + candidate > cap + 1e-9:
            t.state = 'REJECTED'
            t.exit_idx = i
            t.exit_reason = 'MAX_RESERVED_STOP_RISK_10PCT'
            self.rejections['MAX_RESERVED_STOP_RISK_10PCT'] += 1
            return False
        self.pending_map[t.ticket_id] = t
        self.events['TICKET_ARMED'] += 1
        return True


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    market = load_market()
    A = {k: market[k].to_numpy(dtype='float64') for k in ['bo','bh','bl','bc','ao','ah','al','ac']}
    A['ts'] = market['timestamp'].to_numpy(dtype='int64')
    submissions, management, setup_meta, first_cancel, no_data_signals = load_inputs(A['ts'])
    account = RiskAccount(A)
    fill_events = defaultdict(list)
    first_signal_idx = min(submissions) if submissions else len(A['ts'])
    last_i = len(A['ts']) - 1
    max_reserved = 0.0
    max_cap_utilization = 0.0

    for i in range(first_signal_idx, len(A['ts'])):
        for s in submissions.get(i, ()):
            cancel_i = first_cancel.get(s['uid'], len(A['ts']))
            for layer, entry in enumerate(canonical_zone_entries(s['side'], s['lo'], s['hi']), 1):
                t = Ticket(
                    f"{s['uid']}__L{layer}", s['uid'], layer, s['side'], float(entry),
                    float(s['sl']), float(s['tp']), submit_idx=i
                )
                if account.arm_with_risk(t, i):
                    fi = schedule_first_touch(t, i, cancel_i, A)
                    t.scheduled_fill_idx = fi
                    if fi is not None:
                        fill_events[fi].append(t)

        for uid, actions in management.get(i, ()):
            if 'cancel_pending' in actions:
                for t in list(account.setup_pending(uid)):
                    account.cancel(t, i, 'PROVIDER_CANCEL_PENDING')
            opens = account.setup_open(uid)
            if 'close_full' in actions:
                for t in list(opens):
                    px = A['bo'][i] if t.side == 'BUY' else A['ao'][i]
                    account.close(t, i, px, 'PROVIDER_CLOSE_FULL')
            elif 'close_partial' in actions:
                opens = account.setup_open(uid)
                count = canonical_partial_close_count(len(opens))
                for _ in range(count):
                    t = account.worst(opens)
                    if t is None:
                        break
                    px = A['bo'][i] if t.side == 'BUY' else A['ao'][i]
                    account.close(t, i, px, 'PROVIDER_CLOSE_PARTIAL_CANONICAL')
                    opens.remove(t)
            if 'move_sl_to_entry' in actions:
                for t in account.setup_open(uid):
                    t.sl = float(t.fill_price)

        for t in fill_events.pop(i, ()):
            if t.state != 'PENDING':
                continue
            if t.side == 'BUY':
                fill_price = A['ao'][i] if A['ao'][i] <= t.requested_entry else t.requested_entry
            else:
                fill_price = A['bo'][i] if A['bo'][i] >= t.requested_entry else t.requested_entry
            account.fill(t, i, fill_price)

        setup_targets = set()
        for t in list(account.open_map.values()):
            target_touch = A['bh'][i] >= t.tp if t.side == 'BUY' else A['al'][i] <= t.tp
            if t.side == 'BUY':
                hit_sl = A['bl'][i] <= t.sl
                adverse_px = A['bo'][i] if A['bo'][i] <= t.sl else t.sl
            else:
                hit_sl = A['ah'][i] >= t.sl
                adverse_px = A['ao'][i] if A['ao'][i] >= t.sl else t.sl
            if t.fill_idx == i and (hit_sl or target_touch):
                t.ambiguous_fail_closed = True
                account.close(t, i, adverse_px, 'AMBIGUOUS_FILL_BAR_FAIL_CLOSED')
            elif hit_sl and target_touch:
                t.ambiguous_fail_closed = True
                account.close(t, i, adverse_px, 'AMBIGUOUS_EXIT_BAR_FAIL_CLOSED')
            elif hit_sl:
                account.close(t, i, adverse_px, 'STOP')
            elif target_touch:
                setup_targets.add(t.uid)

        for uid in setup_targets:
            opens = account.setup_open(uid)
            if opens:
                tp = setup_meta[uid]['tp']
                for t in list(opens):
                    account.close(t, i, tp, 'SINGLE_FINAL_TP')
                for t in list(account.setup_pending(uid)):
                    account.cancel(t, i, 'FINAL_TP_CANCEL_PENDING')

        reserved = account.reserved_stop_risk_sgd()
        cap = account.risk_cap_sgd(i)
        max_reserved = max(max_reserved, reserved)
        if cap > 0:
            max_cap_utilization = max(max_cap_utilization, reserved / cap)

        if account.broker_check(i):
            last_i = i
            break
        account.record_equity(account.equity(i, 'close'))

        if i % 250000 == 0:
            print('bar', i, 'cash', round(account.cash,2), 'open', len(account.open_map), 'pending', len(account.pending_map), 'reserved', round(reserved,2), 'cap', round(cap,2), flush=True)

    if not account.stopout_seen:
        last_i = len(A['ts']) - 1
        for t in list(account.pending_map.values()): account.cancel(t, last_i, 'DATA_END_PENDING_CANCEL')
        for t in list(account.open_map.values()):
            px = A['bc'][last_i] if t.side == 'BUY' else A['ac'][last_i]
            account.close(t, last_i, px, 'DATA_END_MTM')
        account.record_equity(account.cash)

    filled = [t for t in account.tickets if t.fill_idx is not None]
    closed = [t for t in filled if t.exit_idx is not None]
    summary = {
        'version': 'V5.6_PUBLIC_M1_RISK10_SURVIVAL_V1',
        'classification': 'FROZEN_SECONDARY_RISK_LANE_PUBLIC_M1_NOT_BLUEBERRY_CERTIFICATION',
        'method': {
            'starting_balance_sgd': START_SGD,
            'risk_cap_pct_current_equity': RISK_CAP_PCT,
            'risk_definition': 'sum of remaining stop downside for OPEN and PENDING canonical tickets',
            'risk_rejection_timing': 'at arm before any outcome is read',
            'lot_per_accepted_ticket': 0.01,
            'zone': 'two boundary tickets, no synthetic midpoint',
            'provider_management': 'compact reply-linked management only',
            'commission_sgd_proxy_per_side_001': COMMISSION_SGD_SIDE,
            'leverage': LEVERAGE,
            'margin_call_pct': MARGIN_CALL_PCT,
            'stopout_pct': STOPOUT_PCT,
            'swap': 'not applied',
            'blueberry_ticks': False,
            'real_orders': False,
            'live_ready': False,
        },
        'data': {
            'm1_bars': len(A['ts']), 'first_utc': iso(A['ts'][0]), 'last_market_utc': iso(A['ts'][-1]),
            'signals_total': len(setup_meta), 'signals_after_public_data': no_data_signals,
            'management_events': sum(len(v) for v in management.values()),
        },
        'account': {
            'final_test_time_utc': iso(A['ts'][last_i]),
            'ending_cash_sgd_proxy': account.cash,
            'min_equity_sgd_proxy': account.min_equity,
            'max_drawdown_sgd_proxy': account.max_drawdown,
            'max_open_tickets': account.max_open,
            'max_reserved_stop_risk_sgd': max_reserved,
            'max_observed_risk_cap_utilization': max_cap_utilization,
            'margin_call_80_seen': account.margin_call_seen,
            'first_margin_call_80_utc': iso(A['ts'][account.first_margin_call_idx]) if account.first_margin_call_idx is not None else None,
            'stopout_50_seen': account.stopout_seen,
            'stopout_utc': iso(A['ts'][account.stopout_idx]) if account.stopout_idx is not None else None,
            'cash_after_forced_liquidation_sgd_proxy': account.stopout_cash_sgd,
            'account_survived_public_m1_period': not account.stopout_seen,
        },
        'execution': {
            'candidate_tickets_considered': len(account.tickets),
            'tickets_accepted_at_arm': account.events['TICKET_ARMED'],
            'risk_rejected_at_arm': account.rejections['MAX_RESERVED_STOP_RISK_10PCT'],
            'filled_tickets': len(filled),
            'insufficient_free_margin_rejections': account.rejections['INSUFFICIENT_FREE_MARGIN_AT_FILL'],
            'positive_closed_tickets': sum(t.gross_pnl_usd > 0 for t in closed),
            'negative_closed_tickets': sum(t.gross_pnl_usd < 0 for t in closed),
            'ambiguous_fail_closed': sum(t.ambiguous_fail_closed for t in account.tickets),
            'event_counts': dict(account.events),
            'rejection_counts': dict(account.rejections),
        },
        'limitations': [
            'Public M1 Bid/Ask, not Blueberry historical ticks.',
            'Compact management omits richer global/unlinked actions, explicit reentries/rounds, and named-entry nuances.',
            'M1 intrabar ambiguity is resolved adversely.',
            'Historical Blueberry commission, swap, exact intraday SGD conversion, and exact historical margin formula remain uncertified.',
            'This secondary risk lane does not replace the raw provider result and is not live-ready.'
        ]
    }
    (OUT_DIR/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    pd.DataFrame([asdict(t) for t in account.tickets]).to_csv(OUT_DIR/'tickets.csv',index=False)
    print(json.dumps(summary,indent=2),flush=True)


if __name__ == '__main__':
    main()
