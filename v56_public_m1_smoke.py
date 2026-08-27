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
OZ = LOT * CONTRACT_OZ_PER_LOT  # 1 oz per 0.01 XAUUSD lot
START_SGD = 1000.0
SGD_PER_USD_PROXY = 1.0 / 0.7875
COMMISSION_USD_PER_SIDE_001 = 0.035  # provisional current Raw/Direct schedule, not historical certification
LEVERAGE = 500.0
MARGIN_CALL_PCT = 80.0
STOPOUT_PCT = 50.0
DATA_DIR = Path('xauusd_replay/data')
INPUT_DIR = Path('xauusd_replay')
OUT_DIR = Path('xauusd_replay/v56_smoke_output')


def ms(value: str) -> int:
    return int(pd.Timestamp(value).timestamp() * 1000)


def iso(ts_ms: int) -> str:
    return pd.Timestamp(int(ts_ms), unit='ms', tz='UTC').isoformat()


def first_ge(ts: np.ndarray, value: int) -> int:
    return int(np.searchsorted(ts, int(value), side='left'))


def load_market() -> pd.DataFrame:
    def side(name: str, prefix: str) -> pd.DataFrame:
        files = sorted((DATA_DIR / name).glob('*.csv'))
        if not files:
            raise RuntimeError(f'No {name} files')
        chunks = []
        for p in files:
            d = pd.read_csv(p, usecols=['timestamp', 'open', 'high', 'low', 'close'])
            for c in ['timestamp', 'open', 'high', 'low', 'close']:
                d[c] = pd.to_numeric(d[c], errors='coerce')
            d = d.dropna()
            d['timestamp'] = d['timestamp'].astype('int64')
            d = d.rename(columns={c: f'{prefix}{c[0]}' for c in ['open', 'high', 'low', 'close']})
            chunks.append(d)
        return pd.concat(chunks, ignore_index=True).drop_duplicates('timestamp', keep='last').sort_values('timestamp')

    bid = side('bid', 'b')
    ask = side('ask', 'a')
    x = bid.merge(ask, on='timestamp', how='inner').sort_values('timestamp').reset_index(drop=True)
    if x.empty:
        raise RuntimeError('No merged bid/ask M1 bars')
    return x


def load_inputs():
    signals = []
    with (INPUT_DIR / 'signals.csv').open(newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            signals.append({
                'uid': r['uid'], 'time_ms': ms(r['time_utc']), 'time_utc': r['time_utc'],
                'side': r['side'], 'lo': float(r['lo']), 'hi': float(r['hi']),
                'sl': float(r['sl']), 'tp': float(r['tp']),
            })
    signals.sort(key=lambda x: (x['time_ms'], x['uid']))

    events = defaultdict(list)
    with (INPUT_DIR / 'management_compact.csv').open(newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            actions = tuple(a for a in r['actions'].split('|') if a)
            events[r['root_uid']].append((ms(r['time_utc']), actions))
    for value in events.values():
        value.sort()
    return signals, events


@dataclass
class TicketResult:
    uid: str
    layer: int
    side: str
    requested_entry: float
    sl_initial: float
    tp: float
    state: str = 'PENDING'
    fill_idx: Optional[int] = None
    fill_price: Optional[float] = None
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    gross_pnl_usd: float = 0.0
    ambiguous_fail_closed: bool = False


def close_ticket(t: TicketResult, i: int, price: float, reason: str):
    if t.state != 'OPEN':
        return
    t.state = 'CLOSED'
    t.exit_idx = int(i)
    t.exit_price = float(price)
    t.exit_reason = reason
    if t.side == 'BUY':
        t.gross_pnl_usd = (t.exit_price - float(t.fill_price)) * OZ
    else:
        t.gross_pnl_usd = (float(t.fill_price) - t.exit_price) * OZ


def cancel_ticket(t: TicketResult, i: int, reason: str):
    if t.state == 'PENDING':
        t.state = 'CANCELLED'
        t.exit_idx = int(i)
        t.exit_reason = reason


def simulate_setup(s, evs, A):
    ts = A['ts']
    n = len(ts)
    sub = first_ge(ts, s['time_ms'])
    entries = canonical_zone_entries(s['side'], s['lo'], s['hi'])
    tickets = [
        TicketResult(s['uid'], layer + 1, s['side'], float(entry), float(s['sl']), float(s['tp']))
        for layer, entry in enumerate(entries)
    ]
    if sub >= n:
        for t in tickets:
            t.state = 'NO_DATA'
            t.exit_reason = 'NO_DATA_AFTER_SIGNAL'
        return tickets

    # Map Telegram management to the first complete M1 bar beginning at/after its timestamp.
    mgmt = defaultdict(set)
    for t_ms, actions in evs:
        if t_ms < s['time_ms']:
            continue
        idx = first_ge(ts, t_ms)
        if idx < n:
            mgmt[idx].update(actions)
    mgmt_indices = sorted(mgmt)
    mg_pos = 0
    cursor = sub
    current_sl = {t.layer: float(t.sl_initial) for t in tickets}

    def active():
        return any(t.state in {'PENDING', 'OPEN'} for t in tickets)

    def pending():
        return [t for t in tickets if t.state == 'PENDING']

    def opens():
        return [t for t in tickets if t.state == 'OPEN']

    def worst(open_list):
        if not open_list:
            return None
        if s['side'] == 'BUY':
            return max(open_list, key=lambda t: float(t.fill_price))
        return min(open_list, key=lambda t: float(t.fill_price))

    def management_at(i: int):
        acts = mgmt.get(i, set())
        if not acts:
            return
        if 'cancel_pending' in acts:
            for t in pending():
                cancel_ticket(t, i, 'PROVIDER_CANCEL_PENDING')
        if 'close_full' in acts:
            px = A['bo'][i] if s['side'] == 'BUY' else A['ao'][i]
            for t in list(opens()):
                close_ticket(t, i, px, 'PROVIDER_CLOSE_FULL')
        elif 'close_partial' in acts:
            open_list = opens()
            count = canonical_partial_close_count(len(open_list))
            px = A['bo'][i] if s['side'] == 'BUY' else A['ao'][i]
            for _ in range(count):
                t = worst(open_list)
                if t is None:
                    break
                close_ticket(t, i, px, 'PROVIDER_CLOSE_PARTIAL_CANONICAL')
                open_list.remove(t)
        if 'move_sl_to_entry' in acts:
            for t in opens():
                current_sl[t.layer] = float(t.fill_price)

    def market_event_mask(start: int, end: int):
        if end <= start:
            return None
        mask = np.zeros(end - start, dtype=bool)
        for t in tickets:
            if t.state == 'PENDING':
                if s['side'] == 'BUY':
                    mask |= A['al'][start:end] <= t.requested_entry
                else:
                    mask |= A['bh'][start:end] >= t.requested_entry
            elif t.state == 'OPEN':
                stop = current_sl[t.layer]
                if s['side'] == 'BUY':
                    mask |= (A['bl'][start:end] <= stop) | (A['bh'][start:end] >= s['tp'])
                else:
                    mask |= (A['ah'][start:end] >= stop) | (A['al'][start:end] <= s['tp'])
        idxs = np.flatnonzero(mask)
        return None if not len(idxs) else start + int(idxs[0])

    def process_market_bar(i: int):
        # Fill both canonical boundary tickets if the same M1 bar reaches both levels.
        for t in list(pending()):
            if s['side'] == 'BUY':
                touched = A['al'][i] <= t.requested_entry
                if not touched:
                    continue
                fill = A['ao'][i] if A['ao'][i] <= t.requested_entry else t.requested_entry
            else:
                touched = A['bh'][i] >= t.requested_entry
                if not touched:
                    continue
                fill = A['bo'][i] if A['bo'][i] >= t.requested_entry else t.requested_entry
            t.state = 'OPEN'
            t.fill_idx = int(i)
            t.fill_price = float(fill)
            current_sl[t.layer] = float(t.sl_initial)

        open_before_exit = list(opens())
        if not open_before_exit:
            return
        target_touch = (A['bh'][i] >= s['tp']) if s['side'] == 'BUY' else (A['al'][i] <= s['tp'])

        # Conservative M1 chronology: same-fill-bar exits and bars touching both SL and TP are charged to SL.
        for t in list(open_before_exit):
            stop = current_sl[t.layer]
            if s['side'] == 'BUY':
                hit_sl = A['bl'][i] <= stop
                hit_tp = target_touch
                adverse_px = A['bo'][i] if A['bo'][i] <= stop else stop
            else:
                hit_sl = A['ah'][i] >= stop
                hit_tp = target_touch
                adverse_px = A['ao'][i] if A['ao'][i] >= stop else stop
            if t.fill_idx == i and (hit_sl or hit_tp):
                t.ambiguous_fail_closed = True
                close_ticket(t, i, adverse_px, 'AMBIGUOUS_FILL_BAR_FAIL_CLOSED')
            elif hit_sl and hit_tp:
                t.ambiguous_fail_closed = True
                close_ticket(t, i, adverse_px, 'AMBIGUOUS_EXIT_BAR_FAIL_CLOSED')
            elif hit_sl:
                close_ticket(t, i, adverse_px, 'STOP')

        # A single published TP is the final TP for every surviving open ticket; pending tickets cancel there.
        if target_touch:
            surviving = list(opens())
            if surviving:
                for t in surviving:
                    close_ticket(t, i, s['tp'], 'SINGLE_FINAL_TP')
                for t in pending():
                    cancel_ticket(t, i, 'FINAL_TP_CANCEL_PENDING')

    while cursor < n and active():
        while mg_pos < len(mgmt_indices) and mgmt_indices[mg_pos] < cursor:
            mg_pos += 1
        next_mgmt = mgmt_indices[mg_pos] if mg_pos < len(mgmt_indices) else n
        market_i = market_event_mask(cursor, next_mgmt)
        if market_i is not None:
            process_market_bar(market_i)
            cursor = market_i + 1
            continue
        if next_mgmt < n:
            management_at(next_mgmt)
            mg_pos += 1
            if active():
                process_market_bar(next_mgmt)
            cursor = next_mgmt + 1
            continue
        break

    last = n - 1
    for t in tickets:
        if t.state == 'PENDING':
            cancel_ticket(t, last, 'DATA_END_PENDING_CANCEL')
        elif t.state == 'OPEN':
            px = A['bc'][last] if s['side'] == 'BUY' else A['ac'][last]
            close_ticket(t, last, px, 'DATA_END_MTM')
    return tickets


def portfolio_diagnostic(tickets, A):
    n = len(A['ts'])
    cashflow = np.zeros(n, dtype='float64')
    lc = np.zeros(n + 1); le = np.zeros(n + 1)
    sc = np.zeros(n + 1); se = np.zeros(n + 1)
    fills = 0
    overnight_tickets = 0
    ticket_nights = 0

    for t in tickets:
        if t.fill_idx is None or t.exit_idx is None or t.fill_price is None:
            continue
        fills += 1
        cashflow[t.fill_idx] -= COMMISSION_USD_PER_SIDE_001 * SGD_PER_USD_PROXY
        cashflow[t.exit_idx] += (t.gross_pnl_usd - COMMISSION_USD_PER_SIDE_001) * SGD_PER_USD_PROXY
        if t.exit_idx > t.fill_idx:
            if t.side == 'BUY':
                lc[t.fill_idx] += OZ; lc[t.exit_idx] -= OZ
                le[t.fill_idx] += t.fill_price * OZ; le[t.exit_idx] -= t.fill_price * OZ
            else:
                sc[t.fill_idx] += OZ; sc[t.exit_idx] -= OZ
                se[t.fill_idx] += t.fill_price * OZ; se[t.exit_idx] -= t.fill_price * OZ
        start_day = pd.Timestamp(int(A['ts'][t.fill_idx]), unit='ms', tz='UTC').date()
        end_day = pd.Timestamp(int(A['ts'][t.exit_idx]), unit='ms', tz='UTC').date()
        nights = max(0, (end_day - start_day).days)
        if nights:
            overnight_tickets += 1
            ticket_nights += nights

    balance = START_SGD + np.cumsum(cashflow)
    lcc = np.cumsum(lc[:-1]); lee = np.cumsum(le[:-1])
    scc = np.cumsum(sc[:-1]); see = np.cumsum(se[:-1])
    floating_sgd = ((lcc * A['bc'] - lee) + (see - scc * A['ac'])) * SGD_PER_USD_PROXY
    equity = balance + floating_sgd
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    active_count = lcc + scc
    mid = (A['bc'] + A['ac']) / 2.0
    used_margin = active_count * mid * OZ / LEVERAGE * SGD_PER_USD_PROXY
    mask = used_margin > 0
    margin_level = np.full(n, np.inf)
    margin_level[mask] = equity[mask] / used_margin[mask] * 100.0
    free_margin = equity - used_margin

    def first_where(cond):
        idx = np.flatnonzero(cond)
        return None if not len(idx) else int(idx[0])

    call_i = first_where(mask & (margin_level <= MARGIN_CALL_PCT))
    stop_i = first_where(mask & (margin_level <= STOPOUT_PCT))
    free_i = first_where(mask & (free_margin < 0))
    zero_i = first_where(equity <= 0)

    return {
        'starting_balance_sgd': START_SGD,
        'final_realized_balance_sgd_proxy': float(balance[-1]),
        'final_equity_sgd_proxy': float(equity[-1]),
        'min_equity_sgd_proxy': float(np.nanmin(equity)),
        'max_drawdown_sgd_proxy': float(np.nanmax(dd)),
        'max_concurrent_tickets': int(np.nanmax(active_count)),
        'filled_tickets': fills,
        'overnight_tickets': overnight_tickets,
        'ticket_nights': ticket_nights,
        'min_margin_level_pct_gross_stream': float(np.nanmin(margin_level[mask])) if mask.any() else None,
        'margin_call_80_seen': call_i is not None,
        'first_margin_call_80_utc': iso(A['ts'][call_i]) if call_i is not None else None,
        'stopout_50_seen': stop_i is not None,
        'first_stopout_50_utc': iso(A['ts'][stop_i]) if stop_i is not None else None,
        'negative_free_margin_seen': free_i is not None,
        'first_negative_free_margin_utc': iso(A['ts'][free_i]) if free_i is not None else None,
        'equity_zero_or_below': zero_i is not None,
        'first_equity_zero_or_below_utc': iso(A['ts'][zero_i]) if zero_i is not None else None,
        'important_limitation': 'Margin metrics are diagnostics on the gross canonical stream; fills are not re-simulated after insufficient-margin rejection or broker stop-out.',
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    market = load_market()
    signals, events = load_inputs()
    A = {k: market[k].to_numpy(dtype='float64') for k in ['bo','bh','bl','bc','ao','ah','al','ac']}
    A['ts'] = market['timestamp'].to_numpy(dtype='int64')
    print('M1', len(market), iso(A['ts'][0]), iso(A['ts'][-1]), 'signals', len(signals), 'management', sum(len(v) for v in events.values()), flush=True)

    tickets = []
    for i, s in enumerate(signals, 1):
        tickets.extend(simulate_setup(s, events.get(s['uid'], []), A))
        if i % 250 == 0 or i == len(signals):
            print('simulated', i, '/', len(signals), 'tickets', len(tickets), flush=True)

    filled = [t for t in tickets if t.fill_idx is not None]
    gross = float(sum(t.gross_pnl_usd for t in filled))
    commission = float(len(filled) * 2 * COMMISSION_USD_PER_SIDE_001)
    net = gross - commission
    status_counts = Counter(t.exit_reason or t.state for t in tickets)
    summary = {
        'version': 'V5.6_PUBLIC_M1_CANONICAL_SMOKE_V1',
        'classification': 'PUBLIC_M1_SMOKE_NOT_BLUEBERRY_CERTIFICATION',
        'method': {
            'canonical_zone': 'two boundary tickets; no synthetic midpoint',
            'target': 'single published TP is final target for all surviving exposure',
            'partial': '2 opens -> close 1 worst entry; 1 open -> full close',
            'management_source': 'preserved compact reply-linked table only',
            'reentries_and_richer_global_scope': 'not present in compact smoke input; therefore under-modeled',
            'intraminute_ambiguity': 'fail closed to stop',
            'price_data': 'public Dukascopy-derived M1 separate Bid/Ask via Market-Data-Lab',
            'historical_blueberry_ticks': False,
            'starting_balance_sgd': START_SGD,
            'sgd_per_usd_proxy': SGD_PER_USD_PROXY,
            'commission_usd_per_side_001_provisional': COMMISSION_USD_PER_SIDE_001,
            'swap': 'not applied; unresolved historical broker truth',
            'slippage_markup': 'not applied beyond observed public Bid/Ask spread',
            'leverage_margin_diagnostic': LEVERAGE,
        },
        'data': {
            'merged_m1_bars': len(market),
            'first_utc': iso(A['ts'][0]),
            'last_utc': iso(A['ts'][-1]),
            'signals': len(signals),
            'management_events': sum(len(v) for v in events.values()),
            'canonical_tickets_created': len(tickets),
        },
        'outcome': {
            'filled_tickets': len(filled),
            'gross_pnl_usd': gross,
            'provisional_commission_usd': commission,
            'net_pnl_usd_before_swap_slippage': net,
            'gross_pnl_sgd_proxy': gross * SGD_PER_USD_PROXY,
            'net_pnl_sgd_proxy_before_swap_slippage': net * SGD_PER_USD_PROXY,
            'simple_final_balance_sgd_proxy_before_swap_slippage': START_SGD + net * SGD_PER_USD_PROXY,
            'positive_tickets': sum(t.gross_pnl_usd > 0 for t in filled),
            'negative_tickets': sum(t.gross_pnl_usd < 0 for t in filled),
            'zero_tickets': sum(abs(t.gross_pnl_usd) < 1e-12 for t in filled),
            'ambiguous_fail_closed_tickets': sum(t.ambiguous_fail_closed for t in tickets),
            'status_counts': dict(status_counts),
        },
        'portfolio_diagnostic': portfolio_diagnostic(tickets, A),
        'authorization': {'real_orders': False, 'live_ready': False},
    }

    (OUT_DIR / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    rows = []
    for t in tickets:
        d = asdict(t)
        d['fill_time_utc'] = iso(A['ts'][t.fill_idx]) if t.fill_idx is not None else None
        d['exit_time_utc'] = iso(A['ts'][t.exit_idx]) if t.exit_idx is not None else None
        rows.append(d)
    pd.DataFrame(rows).to_csv(OUT_DIR / 'tickets.csv', index=False)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
