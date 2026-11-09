import argparse

import numpy as np
import pandas as pd
import pytest

from arbbot.carry.walkforward import TAKER, Market, Position, candidates, pick, run_window, simulate

T0 = pd.Timestamp("2026-01-01", tz="UTC")


def market(coin="AAA", rate=1.5e-5, price=lambda i: 10.0, days=120, spot_volume=5e6, perp_volume=50e6,
           max_leverage=10):
    hours = pd.date_range(T0, periods=days * 24, freq="h")
    bar_ends = pd.date_range(T0 + pd.Timedelta(hours=4), periods=days * 6, freq="4h")
    closes = np.array([price(i) for i in range(len(bar_ends))], dtype=float)
    daily_idx = pd.date_range(T0, periods=days, freq="D")
    daily_close = closes[5::6][:days]
    return Market(
        coin, "@1", 1 / (2 * max_leverage),
        pd.Series(rate, index=hours),
        pd.DataFrame({"close": closes, "high": closes}, index=bar_ends),
        pd.DataFrame({"close": daily_close, "low": daily_close, "usd_volume": perp_volume}, index=daily_idx),
        pd.DataFrame({"usd_volume": spot_volume}, index=daily_idx),
    )


ARGS = argparse.Namespace(lookback_days=28, min_positive=0.7, min_perp_volume=10e6, min_spot_volume=1e6,
                          switch_margin=0.05, floor=0.10, max_margin=0.5, top=2, notional=1000)


def test_flat_market_earns_funding_minus_two_taker_fees():
    m = market()
    pos = Position("AAA", 500, 0.25, 0.2, T0 + pd.Timedelta(days=30), T0 + pd.Timedelta(days=58))
    r = simulate(pos, m)
    assert r.funding == pytest.approx(500 * 1.5e-5 * 28 * 24, rel=1e-6)
    assert r.fees == pytest.approx(2 * 500 * TAKER, rel=1e-6)
    assert r.cash.sum() == pytest.approx(r.funding - r.fees)
    assert not r.liquidated and r.rebalances == 0


def test_rising_price_rebalances_and_shrinks_the_position():
    m = market(price=lambda i: 10.0 * (1 + 0.004 * i))  # +39% over the 28 days held: 10% steps
    pos = Position("AAA", 500, 0.25, 0.2, T0 + pd.Timedelta(days=30), T0 + pd.Timedelta(days=58))
    r = simulate(pos, m)
    assert r.rebalances == 3 and not r.liquidated


def test_candidates_use_only_the_lookback():
    m = market()
    m.funding[m.funding.index >= T0 + pd.Timedelta(days=40)] = 1.0  # huge funding after t
    c = candidates({"AAA": m}, T0 + pd.Timedelta(days=40), ARGS)
    assert c.at["AAA", "funding"] == pytest.approx(1.5e-5 * 8760)


def test_volume_and_listing_filters():
    t = T0 + pd.Timedelta(days=40)
    assert candidates({"A": market("A", spot_volume=5e5)}, t, ARGS).empty
    assert candidates({"A": market("A", perp_volume=5e6)}, t, ARGS).empty
    assert candidates({"A": market("A")}, T0 + pd.Timedelta(days=20), ARGS).empty  # not listed long enough


def test_pick_floor_switch_and_top():
    ranked = pd.DataFrame({"positive": 0.9, "margin": 0.25, "score": [0.30, 0.20, 0.17, 0.05]},
                          index=["A", "B", "C", "D"])
    assert pick(ranked, [], ARGS) == ["A", "B"]
    assert pick(ranked, ["C", "B"], ARGS) == ["A", "B"]   # A beats C by 13 points: swap; B stays
    assert pick(ranked.loc[["B", "C"]], ["C"], ARGS) == ["C", "B"]  # B only 3 points better: C stays
    assert pick(ranked.loc[["D"]], [], ARGS) == []  # under the floor: cash


def test_window_keeps_incumbent_without_paying_rotation():
    markets = {"A": market("A", rate=3e-5), "B": market("B", rate=2.5e-5)}
    start = T0 + pd.Timedelta(days=30)

    def choose(t, held):
        return [("A", 500, 0.25, 0.2), ("B", 500, 0.25, 0.2)]

    res, capital, p1, p2 = run_window(markets, start, choose, ARGS)
    assert p1 == p2 == ["A", "B"]
    assert res.fees == pytest.approx(2 * 2 * 500 * TAKER, rel=1e-6)  # one entry and one exit each
    assert capital == pytest.approx(1250)
