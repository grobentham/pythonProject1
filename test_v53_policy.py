from v53_policy import (
    downside_stop_distance,
    explicit_provider_entries,
    limit_fill_price,
    partial_close_count,
    select_explicit_entries,
    synthetic_zone_entries,
)


def main():
    checks = []

    # Downside-only risk: BE/profit-protected stops consume zero remaining risk.
    checks += [
        downside_stop_distance("BUY", 100, 95) == 5,
        downside_stop_distance("BUY", 100, 100) == 0,
        downside_stop_distance("BUY", 100, 105) == 0,
        downside_stop_distance("SELL", 100, 105) == 5,
        downside_stop_distance("SELL", 100, 100) == 0,
        downside_stop_distance("SELL", 100, 95) == 0,
    ]

    # Resting LIMIT gap price improvement.
    checks += [
        limit_fill_price("BUY", 2893, 2865, 2866) == 2866,
        limit_fill_price("SELL", 2893, 2920, 2921) == 2920,
        limit_fill_price("BUY", 2893, 2894, 2895) == 2893,
        limit_fill_price("SELL", 2893, 2891, 2892) == 2893,
    ]

    # Executable partial-close rounding.
    checks += [
        partial_close_count(1, "CEIL_HALF") == 1,
        partial_close_count(2, "CEIL_HALF") == 1,
        partial_close_count(3, "CEIL_HALF") == 2,
        partial_close_count(3, "FLOOR_HALF") == 1,
    ]

    # Zone translation and >3 explicit entry freeze.
    checks += [
        synthetic_zone_entries("BUY", 100, 102, 3) == [102.0, 101.0, 100.0],
        synthetic_zone_entries("SELL", 100, 102, 3) == [100.0, 101.0, 102.0],
        select_explicit_entries([100, 101, 102, 103], "BUY") == [100.0, 101.0, 102.0],
        select_explicit_entries([100, 101, 102, 103], "SELL") == [103.0, 102.0, 101.0],
    ]

    # Explicit-provider text extraction strong forms only.
    buy = {"side": "BUY", "text": "Buy entries: 4625 / 4624 / 4623", "zone_low": 4623, "zone_high": 4625}
    sell = {"side": "SELL", "text": "Sell entry 4625\nSell entry 4626\nSell entry 4627", "zone_low": 4625, "zone_high": 4627}
    checks += [
        explicit_provider_entries(buy, "BUY", 4623, 4625) == [4625.0, 4624.0, 4623.0],
        explicit_provider_entries(sell, "SELL", 4625, 4627) == [4625.0, 4626.0, 4627.0],
    ]

    passed = sum(bool(x) for x in checks)
    if passed != len(checks):
        raise AssertionError(f"V5.3 semantic tests failed: {passed}/{len(checks)} PASS")
    print(f"V5.3 semantic tests: {passed}/{len(checks)} PASS")


if __name__ == "__main__":
    main()
