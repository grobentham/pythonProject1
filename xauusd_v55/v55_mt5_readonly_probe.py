"""Read-only MetaTrader 5 calibration probe for V5.5.

This module NEVER calls order_send(), position_close(), history mutation, or any
trade-changing API. It records current-environment evidence only. Current MT5
calculations must not be relabelled as historical broker truth.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any


def _jsonable(value: Any):
    if hasattr(value, "_asdict"):
        return {k: _jsonable(v) for k, v in value._asdict().items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_account(account) -> dict[str, Any]:
    d = account._asdict()
    # Deliberately omit login/name identifiers. Preserve only broker-calculation facts.
    keep = [
        "trade_mode", "leverage", "limit_orders", "margin_so_mode", "trade_allowed",
        "trade_expert", "margin_mode", "currency_digits", "fifo_close", "balance",
        "credit", "profit", "equity", "margin", "margin_free", "margin_level",
        "margin_so_call", "margin_so_so", "currency", "server", "company"
    ]
    return {k: _jsonable(d.get(k)) for k in keep if k in d}


def _safe_symbol(symbol) -> dict[str, Any]:
    d = symbol._asdict()
    keep = [
        "name", "description", "path", "currency_base", "currency_profit", "currency_margin",
        "digits", "point", "trade_contract_size", "volume_min", "volume_max", "volume_step",
        "trade_tick_size", "trade_tick_value", "trade_tick_value_profit", "trade_tick_value_loss",
        "trade_calc_mode", "margin_initial", "margin_maintenance", "trade_stops_level",
        "trade_freeze_level", "swap_mode", "swap_long", "swap_short", "swap_rollover3days"
    ]
    return {k: _jsonable(d.get(k)) for k in keep if k in d}


def run(symbol_name: str, volume: float) -> dict[str, Any]:
    import MetaTrader5 as mt5

    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    try:
        account = mt5.account_info()
        symbol = mt5.symbol_info(symbol_name)
        if account is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        if symbol is None:
            raise RuntimeError(f"symbol_info({symbol_name}) failed: {mt5.last_error()}")
        if not symbol.visible:
            # symbol_select changes Market Watch visibility, not orders/account exposure.
            if not mt5.symbol_select(symbol_name, True):
                raise RuntimeError(f"symbol_select({symbol_name}) failed: {mt5.last_error()}")
            symbol = mt5.symbol_info(symbol_name)
        tick = mt5.symbol_info_tick(symbol_name)
        if tick is None or tick.bid <= 0 or tick.ask <= 0:
            raise RuntimeError(f"No executable bid/ask quote for {symbol_name}")

        buy_open = float(tick.ask)
        sell_open = float(tick.bid)
        buy_profit_up_1 = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol_name, volume, buy_open, buy_open + 1.0)
        buy_profit_down_1 = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol_name, volume, buy_open, buy_open - 1.0)
        sell_profit_down_1 = mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, symbol_name, volume, sell_open, sell_open - 1.0)
        sell_profit_up_1 = mt5.order_calc_profit(mt5.ORDER_TYPE_SELL, symbol_name, volume, sell_open, sell_open + 1.0)
        buy_margin = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, symbol_name, volume, buy_open)
        sell_margin = mt5.order_calc_margin(mt5.ORDER_TYPE_SELL, symbol_name, volume, sell_open)

        positions = mt5.positions_get() or ()
        orders = mt5.orders_get() or ()
        position_summary = {}
        for p in positions:
            key = str(getattr(p, "symbol", "UNKNOWN"))
            position_summary[key] = position_summary.get(key, 0.0) + float(getattr(p, "volume", 0.0))
        order_summary = {}
        for o in orders:
            key = str(getattr(o, "symbol", "UNKNOWN"))
            order_summary[key] = order_summary.get(key, 0) + 1

        return {
            "schema": "V55_MT5_READ_ONLY_PROBE_V1",
            "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "metatrader5_version": getattr(mt5, "__version__", None),
            "symbol": symbol_name,
            "volume_lot": volume,
            "account": _safe_account(account),
            "symbol_info": _safe_symbol(symbol),
            "quote": {"bid": float(tick.bid), "ask": float(tick.ask), "time_msc": int(getattr(tick, "time_msc", 0))},
            "order_calc_profit": {
                "buy_plus_1_price_unit_account_ccy": buy_profit_up_1,
                "buy_minus_1_price_unit_account_ccy": buy_profit_down_1,
                "sell_minus_1_price_unit_account_ccy": sell_profit_down_1,
                "sell_plus_1_price_unit_account_ccy": sell_profit_up_1
            },
            "order_calc_margin": {
                "buy_current_environment_account_ccy": buy_margin,
                "sell_current_environment_account_ccy": sell_margin
            },
            "existing_positions_by_symbol_volume": position_summary,
            "existing_pending_orders_by_symbol_count": order_summary,
            "current_environment_only": True,
            "historical_truth_claimed": False,
            "order_send_called": False,
            "real_orders_authorized": False,
            "warning": "order_calc_profit/order_calc_margin are current-environment calibration evidence only. They do not reconstruct historical commissions, swaps, margin schedules, or FX conversion rules."
        }
    finally:
        mt5.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD.i")
    ap.add_argument("--volume", type=float, default=0.01)
    ap.add_argument("--output", type=Path, default=Path("V55_MT5_READ_ONLY_PROBE.json"))
    args = ap.parse_args()
    report = run(args.symbol, args.volume)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(json.dumps(report, indent=2))
    print(f"SHA256 {digest}  {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
