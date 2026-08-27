from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from v56_canonical_policy import canonical_partial_close_count, canonical_zone_entries

LOT = 0.01
CONTRACT_OZ_PER_LOT = 100.0
OZ = LOT * CONTRACT_OZ_PER_LOT  # 1 oz per 0.01 XAUUSD ticket
START_SGD = 1000.0
SGD_PER_USD_PROXY = 1.0 / 0.7875
COMMISSION_USD_PER_SIDE_001 = 0.035  # provisional current Raw/Direct schedule, not historical certification
COMMISSION_SGD_SIDE = COMMISSION_USD_PER_SIDE_001 * SGD_PER_USD_PROXY
LEVERAGE = 500.0
MARGIN_CALL_PCT = 80.0
STOPOUT_PCT = 50.0
DATA_DIR = Path('xauusd_replay/data')
INPUT_DIR = Path('xauusd_replay')
OUT_DIR = Path('xauusd_replay/v56_account_survival_output')


def ms(value: str) -> int:
    return int(pd.Timestamp(value).timestamp() * 1000)


def iso(value: int) -> str:
    return pd.Timestamp(int(value), unit='ms', tz='UTC').isoformat()


def first_ge(ts: np.ndarray, value: int) -> int:
    return int(np.searchsorted(ts, int(value), side='left'))


def load_market() -> pd.DataFrame:
    def side(name: str, prefix: str):
        chunks = []
        for p in sorted((DATA_DIR / name).glob('*.csv')):
            d = pd.read_csv(p, usecols=['timestamp', 'open', 'high', 'low', 'close'])
            for c in ['timestamp', 'open', 'high', 'low', 'close']:
                d[c] = pd.to_numeric(d[c], errors='coerce')
            d = d.dropna()
            d['timestamp'] = d['timestamp'].astype('int64')
            d = d.rename(columns={c: f'{prefix}{c[0]}' for c in ['open', 'high', 'low', 'close']})
            chunks.append(d)
        if not chunks:
            raise RuntimeError(f'No {name} M1 files')
        return pd.concat(chunks, ignore_index=True).drop_duplicates('timestamp', keep='last').sort_values('timestamp')

    x = side('bid', 'b').merge(side('ask', 'a'), on='timestamp', how='inner').sort_values('timestamp').reset_index(drop=True)
    if x.empty:
        raise RuntimeError('No merged M1 Bid/Ask bars')
    return x


def load_inputs(ts):
    submissions = defaultdict(list)
    management = defaultdict(list)
    setup_meta = {}
    no_data_signals = 0

    with (INPUT_DIR / 'signals.csv').open(newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            t = ms(r['time_utc'])
            idx = first_ge(ts, t)
            row = {
                'uid': r['uid'], 'time_ms': t, 'side': r['side'],
                'lo': float(r['lo']), 'hi': float(r['hi']), 'sl': float(r['sl']), 'tp': float(r['tp'])
            }
            setup_meta[r['uid']] = row
            if idx >= len(ts):
                no_data_signals += 1
            else:
                submissions[idx].append(row)

    with (INPUT_DIR / 'management_compact.csv').open(newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            idx = first_ge(ts, ms(r['time_utc']))
            if idx < len(ts):
                management[idx].append((r['root_uid'], tuple(a for a in r['actions'].split('|') if a)))

    first_cancel = {}
    for idx, rows in management.items():
        for uid, actions in rows:
            if 'cancel_pending' in actions:
                first_cancel[uid] = min(idx, first_cancel.get(uid, idx))

    return submissions, management, setup_meta, first_cancel, no_data_signals


@dataclass
class Ticket:
    ticket_id: str
    uid: str
    layer: int
    side: str
    requested_entry: float
    sl: float
    tp: float
    state: str = 'PENDING'
    submit_idx: Optional[int] = None
    scheduled_fill_idx: Optional[int] = None
    fill_idx: Optional[int] = None
    fill_price: Optional[float] = None
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    gross_pnl_usd: float = 0.0
    ambiguous_fail_closed: bool = False


class Account:
    def __init__(self, A):
        self.A = A
        self.cash = START_SGD
        self.tickets: list[Ticket] = []
        self.by_uid = defaultdict(list)
        self.open_map: dict[str, Ticket] = {}
        self.pending_map: dict[str, Ticket] = {}
        self.rejections = Counter()
        self.events = Counter()
        self.margin_call_seen = False
        self.first_margin_call_idx = None
        self.stopout_seen = False
        self.stopout_idx = None
        self.stopout_cash_sgd = None
        self.stopout_equity_before_sgd = None
        self.peak_equity = START_SGD
        self.max_drawdown = 0.0
        self.min_equity = START_SGD
        self.max_open = 0

    def opens(self):
        return list(self.open_map.values())

    def pending(self):
        return list(self.pending_map.values())

    def setup_open(self, uid):
        return [t for t in self.by_uid.get(uid, ()) if t.state == 'OPEN']

    def setup_pending(self, uid):
        return [t for t in self.by_uid.get(uid, ()) if t.state == 'PENDING']

    def arm(self, t: Ticket):
        self.tickets.append(t)
        self.by_uid[t.uid].append(t)
        self.pending_map[t.ticket_id] = t
        self.events['TICKET_ARMED'] += 1

    def equity(self, i, mode='open'):
        if mode == 'open':
            buy_px, sell_px = self.A['bo'][i], self.A['ao'][i]
        elif mode == 'close':
            buy_px, sell_px = self.A['bc'][i], self.A['ac'][i]
        elif mode == 'adverse':
            buy_px, sell_px = self.A['bl'][i], self.A['ah'][i]
        else:
            raise ValueError(mode)
        floating_usd = 0.0
        for t in self.open_map.values():
            floating_usd += (buy_px - t.fill_price) * OZ if t.side == 'BUY' else (t.fill_price - sell_px) * OZ
        return self.cash + floating_usd * SGD_PER_USD_PROXY

    def used_margin(self, i, mode='open', extra_price=None):
        if mode == 'open':
            mid = (self.A['bo'][i] + self.A['ao'][i]) / 2.0
        elif mode == 'close':
            mid = (self.A['bc'][i] + self.A['ac'][i]) / 2.0
        else:
            mid = (self.A['bl'][i] + self.A['ah'][i]) / 2.0
        used = len(self.open_map) * max(mid, 0.0) * OZ / LEVERAGE * SGD_PER_USD_PROXY
        if extra_price is not None:
            used += max(float(extra_price), 0.0) * OZ / LEVERAGE * SGD_PER_USD_PROXY
        return used

    def can_fill(self, i, fill_price):
        projected_equity = self.equity(i, 'open') - COMMISSION_SGD_SIDE
        projected_margin = self.used_margin(i, 'open', extra_price=fill_price)
        return projected_equity - projected_margin >= -1e-9

    def fill(self, t, i, price):
        if t.state != 'PENDING':
            return False
        if not self.can_fill(i, price):
            t.state = 'REJECTED'; t.exit_idx = i; t.exit_reason = 'INSUFFICIENT_FREE_MARGIN_AT_FILL'
            self.pending_map.pop(t.ticket_id, None)
            self.rejections['INSUFFICIENT_FREE_MARGIN_AT_FILL'] += 1
            return False
        self.pending_map.pop(t.ticket_id, None)
        self.open_map[t.ticket_id] = t
        t.state = 'OPEN'; t.fill_idx = i; t.fill_price = float(price)
        self.cash -= COMMISSION_SGD_SIDE
        self.events['FILL'] += 1
        self.max_open = max(self.max_open, len(self.open_map))
        return True

    def close(self, t, i, price, reason):
        if t.state != 'OPEN':
            return
        gross = (float(price) - t.fill_price) * OZ if t.side == 'BUY' else (t.fill_price - float(price)) * OZ
        self.cash += gross * SGD_PER_USD_PROXY - COMMISSION_SGD_SIDE
        self.open_map.pop(t.ticket_id, None)
        t.state = 'CLOSED'; t.exit_idx = i; t.exit_price = float(price); t.exit_reason = reason; t.gross_pnl_usd = gross
        self.events[reason] += 1

    def cancel(self, t, i, reason):
        if t.state == 'PENDING':
            self.pending_map.pop(t.ticket_id, None)
            t.state = 'CANCELLED'; t.exit_idx = i; t.exit_reason = reason
            self.events[reason] += 1

    @staticmethod
    def worst(open_list):
        if not open_list:
            return None
        return max(open_list, key=lambda t: t.fill_price) if open_list[0].side == 'BUY' else min(open_list, key=lambda t: t.fill_price)

    def record_equity(self, value):
        value = float(value)
        self.min_equity = min(self.min_equity, value)
        self.peak_equity = max(self.peak_equity, value)
        self.max_drawdown = max(self.max_drawdown, self.peak_equity - value)

    def broker_check(self, i):
        if not self.open_map:
            self.record_equity(self.cash)
            return False
        eq = self.equity(i, 'adverse')
        used = self.used_margin(i, 'adverse')
        level = float('inf') if used <= 0 else eq / used * 100.0
        self.record_equity(eq)
        if level <= MARGIN_CALL_PCT and not self.margin_call_seen:
            self.margin_call_seen = True
            self.first_margin_call_idx = i
        if level <= STOPOUT_PCT:
            self.stopout_seen = True
            self.stopout_idx = i
            self.stopout_equity_before_sgd = eq
            for t in list(self.open_map.values()):
                px = self.A['bl'][i] if t.side == 'BUY' else self.A['ah'][i]
                self.close(t, i, px, 'BROKER_STOP_OUT_FORCED_LIQUIDATION')
            for t in list(self.pending_map.values()):
                self.cancel(t, i, 'STOP_OUT_CANCEL_PENDING')
            self.stopout_cash_sgd = self.cash
            self.record_equity(self.cash)
            return True
        return False


def schedule_first_touch(t: Ticket, start: int, end: int, A):
    if end <= start:
        return None
    if t.side == 'BUY':
        hits = np.flatnonzero(A['al'][start:end] <= t.requested_entry)
    else:
        hits = np.flatnonzero(A['bh'][start:end] >= t.requested_entry)
    return None if not len(hits) else start + int(hits[0])


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    market = load_market()
    A = {k: market[k].to_numpy(dtype='float64') for k in ['bo','bh','bl','bc','ao','ah','al','ac']}
    A['ts'] = market['timestamp'].to_numpy(dtype='int64')
    submissions, management, setup_meta, first_cancel, no_data_signals = load_inputs(A['ts'])
    account = Account(A)
    fill_events = defaultdict(list)
    first_signal_idx = min(submissions) if submissions else len(A['ts'])
    last_i = len(A['ts']) - 1

    for i in range(first_signal_idx, len(A['ts'])):
        # Signal cards are armed before same-M1 follow-ups; by this bar open both messages are already known.
        for s in submissions.get(i, ()):
            cancel_i = first_cancel.get(s['uid'], len(A['ts']))
            for layer, entry in enumerate(canonical_zone_entries(s['side'], s['lo'], s['hi']), 1):
                t = Ticket(
                    f"{s['uid']}__L{layer}", s['uid'], layer, s['side'], float(entry),
                    float(s['sl']), float(s['tp']), submit_idx=i
                )
                account.arm(t)
                fi = schedule_first_touch(t, i, cancel_i, A)
                t.scheduled_fill_idx = fi
                if fi is not None:
                    fill_events[fi].append(t)

        # Telegram follow-ups execute at the first M1 bar open at/after the message timestamp.
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

        # Only pre-indexed first touches are checked. A rejected marketable limit is treated as broker-rejected, not retried.
        for t in fill_events.pop(i, ()):
            if t.state != 'PENDING':
                continue
            if t.side == 'BUY':
                fill_price = A['ao'][i] if A['ao'][i] <= t.requested_entry else t.requested_entry
            else:
                fill_price = A['bo'][i] if A['bo'][i] >= t.requested_entry else t.requested_entry
            account.fill(t, i, fill_price)

        # Conservative M1 chronology: stop before target whenever order is unknowable; fill-bar ambiguity is charged to stop.
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

        # Single published TP is final for every surviving open ticket; unfilled sibling cancels at that TP.
        for uid in setup_targets:
            opens = account.setup_open(uid)
            if opens:
                tp = setup_meta[uid]['tp']
                for t in list(opens):
                    account.close(t, i, tp, 'SINGLE_FINAL_TP')
                for t in list(account.setup_pending(uid)):
                    account.cancel(t, i, 'FINAL_TP_CANCEL_PENDING')

        # Broker survival gate. Once 50% is reached, liquidate and terminate; no impossible post-stopout recovery is credited.
        if account.broker_check(i):
            last_i = i
            break
        account.record_equity(account.equity(i, 'close'))

        if i % 250000 == 0:
            print('bar', i, '/', len(A['ts']), 'cash', round(account.cash, 2), 'open', len(account.open_map), 'pending', len(account.pending_map), flush=True)

    if not account.stopout_seen:
        last_i = len(A['ts']) - 1
        for t in list(account.pending_map.values()):
            account.cancel(t, last_i, 'DATA_END_PENDING_CANCEL')
        for t in list(account.open_map.values()):
            px = A['bc'][last_i] if t.side == 'BUY' else A['ac'][last_i]
            account.close(t, last_i, px, 'DATA_END_MTM')
        account.record_equity(account.cash)

    filled = [t for t in account.tickets if t.fill_idx is not None]
    closed = [t for t in filled if t.exit_idx is not None]
    summary = {
        'version': 'V5.6_PUBLIC_M1_CAUSAL_ACCOUNT_SURVIVAL_V2',
        'classification': 'PUBLIC_M1_CAUSAL_SURVIVAL_SMOKE_NOT_BLUEBERRY_CERTIFICATION',
        'method': {
            'starting_balance_sgd': START_SGD,
            'lot_per_ticket': LOT,
            'zone': 'two boundary tickets, no synthetic midpoint',
            'provider_management': 'compact reply-linked management only',
            'entry_commission_sgd_proxy': COMMISSION_SGD_SIDE,
            'exit_commission_sgd_proxy': COMMISSION_SGD_SIDE,
            'fx': 'constant historical proxy only, not broker intraday conversion',
            'margin': '1:500 gross CFD proxy; new fills rejected when projected free margin < 0',
            'margin_call_pct': MARGIN_CALL_PCT,
            'stopout_pct': STOPOUT_PCT,
            'stopout_rule': 'conservative all-open forced liquidation at adverse M1 executable side, then terminate replay',
            'intraminute_ambiguity': 'fail closed to stop',
            'pending_limit_lifetime': 'until provider cancel, final TP cancellation, data end, or stopout; no invented TTL',
            'swap': 'not applied',
            'blueberry_ticks': False,
            'real_orders': False,
            'live_ready': False,
        },
        'data': {
            'm1_bars': len(A['ts']),
            'first_utc': iso(A['ts'][0]),
            'last_market_utc': iso(A['ts'][-1]),
            'signals_total': len(setup_meta),
            'signals_after_public_data': no_data_signals,
            'management_events': sum(len(v) for v in management.values()),
        },
        'account': {
            'final_test_time_utc': iso(A['ts'][last_i]),
            'ending_cash_sgd_proxy': account.cash,
            'min_equity_sgd_proxy': account.min_equity,
            'max_drawdown_sgd_proxy': account.max_drawdown,
            'max_open_tickets': account.max_open,
            'margin_call_80_seen': account.margin_call_seen,
            'first_margin_call_80_utc': iso(A['ts'][account.first_margin_call_idx]) if account.first_margin_call_idx is not None else None,
            'stopout_50_seen': account.stopout_seen,
            'stopout_utc': iso(A['ts'][account.stopout_idx]) if account.stopout_idx is not None else None,
            'equity_before_forced_liquidation_sgd_proxy': account.stopout_equity_before_sgd,
            'cash_after_forced_liquidation_sgd_proxy': account.stopout_cash_sgd,
            'account_survived_public_m1_period': not account.stopout_seen,
        },
        'execution': {
            'tickets_armed_before_termination': account.events['TICKET_ARMED'],
            'filled_tickets': len(filled),
            'closed_tickets': len(closed),
            'insufficient_free_margin_rejections': account.rejections['INSUFFICIENT_FREE_MARGIN_AT_FILL'],
            'positive_closed_tickets': sum(t.gross_pnl_usd > 0 for t in closed),
            'negative_closed_tickets': sum(t.gross_pnl_usd < 0 for t in closed),
            'ambiguous_fail_closed': sum(t.ambiguous_fail_closed for t in account.tickets),
            'event_counts': dict(account.events),
            'rejection_counts': dict(account.rejections),
        },
        'limitations': [
            'Public Dukascopy-derived M1 Bid/Ask, not Blueberry tick data.',
            'Compact management omits richer global/unlinked instructions, explicit reentries/rounds, and named-entry nuances from the full canonical grammar.',
            'M1 intrabar chronology is unknowable; ambiguous fill/SL/TP bars are charged adversely.',
            'Historical Blueberry commission, swap, exact intraday SGD conversion, and exact historical margin formula are not independently certified.',
            'This is a survival smoke test, not a live-readiness or profitability certification.'
        ]
    }
    (OUT_DIR / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    pd.DataFrame([asdict(t) for t in account.tickets]).to_csv(OUT_DIR / 'tickets.csv', index=False)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
