import math
from datetime import date, datetime, timedelta, timezone

import pytest

from arbbot.gap.session import Schedule
from arbbot.gap.strategy import Order, Quote, Strategy, filled

SCHED = Schedule.standard_fx(date(2026, 11, 1), date(2026, 11, 30))
MON_10 = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
FRI_CLOSE = datetime(2026, 11, 13, 21, 57, tzinfo=timezone.utc)  # 16:57 New York
NORMAL = 5.0
HEDGE = (4000.0, 4000.1)


def seeded(now: datetime = MON_10, normal: float = NORMAL) -> Strategy:
    s = Strategy(schedule=SCHED)
    reseed(s, now, normal)
    return s


def reseed(s: Strategy, now: datetime, normal: float = NORMAL) -> None:
    s.samples = [(now - timedelta(minutes=5 * i), normal) for i in range(20, 0, -1)]


def q(bid: float, ask: float, at: datetime) -> Quote:
    return Quote(bid, ask, at)


def step(s: Strategy, t: datetime, coin: tuple[float, float], hedge: tuple[float, float] = HEDGE):
    return s.step(t, q(*coin, t), q(*hedge, t))


def names(events) -> list[str]:
    return [e.name for e in events]


def open_long(s: Strategy, t: datetime) -> datetime:
    """Gap 1.05 (coin mid 4001.10, hedge mid 4000.05): enter long, fill, hedge."""
    step(s, t, (4001.0, 4001.2))
    t += timedelta(seconds=1)
    step(s, t, (4000.9, s.order.price))
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "open"
    return t


def open_short(s: Strategy, t: datetime) -> datetime:
    """Gap 9.05: enter short."""
    step(s, t, (4009.0, 4009.2))
    t += timedelta(seconds=1)
    step(s, t, (s.order.price, 4009.3))
    t += timedelta(seconds=1)
    step(s, t, (4009.0, 4009.2))
    assert s.state == "open"
    return t


# --- helpers -----------------------------------------------------------------

def test_fill_rule_requires_trade_through():
    buy = Order("buy", 100.0, "entry", MON_10, 0.0)
    assert not filled(buy, q(99.9, 100.1, MON_10))
    assert filled(buy, q(99.9, 100.0, MON_10))
    sell = Order("sell", 100.0, "entry", MON_10, 0.0)
    assert not filled(sell, q(99.9, 100.1, MON_10))
    assert filled(sell, q(100.0, 100.1, MON_10))


def test_normal_never_uses_current_or_future_samples():
    s = seeded()
    s.samples.append((MON_10, 999.0))
    s.samples.append((MON_10 + timedelta(minutes=1), 999.0))
    assert s.normal(MON_10) == NORMAL


def test_normal_needs_enough_samples():
    s = Strategy(schedule=SCHED)
    s.samples = [(MON_10 - timedelta(minutes=5 * i), NORMAL) for i in range(1, 6)]
    assert s.normal(MON_10) is None


def test_step_needs_schedule():
    with pytest.raises(RuntimeError):
        Strategy().step(MON_10, q(1, 2, MON_10), q(1, 2, MON_10))


# --- full cycle --------------------------------------------------------------

def test_long_round_trip_locks_in_edge():
    s, t = seeded(), MON_10
    ev = step(s, t, (4001.0, 4001.2))
    assert s.state == "entry_resting" and ev[0].name == "entry order"
    assert s.order.side == "buy"
    assert s.order.price <= 4000.0 + NORMAL - s.params.entry

    t += timedelta(seconds=1)
    step(s, t, (4000.9, s.order.price))
    assert s.state == "entry_hedging"

    t += timedelta(seconds=0.2)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "entry_hedging"  # hedge delay not over yet

    t += timedelta(seconds=0.5)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "open"
    assert s.trade.entry_slippage <= 1e-9

    t += timedelta(seconds=0.5)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "exit_resting" and s.order.side == "sell"  # rests straight away
    assert s.order.price >= 4000.1 + NORMAL - 1e-9

    t += timedelta(minutes=10)
    step(s, t, (4005.0, 4005.2))
    t += timedelta(seconds=1)
    step(s, t, (s.order.price, 4005.3))
    assert s.state == "exit_hedging"

    t += timedelta(seconds=1)
    ev = step(s, t, (4005.0, 4005.2))
    closed = [e for e in ev if e.name == "closed"]
    assert closed and s.state == "flat"
    trade = closed[0].trade
    assert trade.exit_reason == "normal"
    assert trade.exit_slippage <= 1e-9
    assert trade.gross() > s.params.entry * 0.9
    assert trade.rollovers == 0
    assert s.stats["closed"] == 1


def test_short_round_trip():
    s, t = seeded(), MON_10
    step(s, t, (4009.0, 4009.2))
    assert s.order.side == "sell"
    assert s.order.price >= 4000.1 + NORMAL + s.params.entry - 1e-9
    open_short(seeded(), MON_10)


def test_hedge_slippage_is_recorded():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    target = s.order.target_gap
    t += timedelta(seconds=1)
    step(s, t, (4000.9, s.order.price))
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1), hedge=(3999.0, 3999.1))  # hedge moved 1.0 against us
    # Bought at 4001.1, one tick under the ask: gap 1.1 at the old hedge, 0.9 better than target.
    assert s.trade.entry_gap == pytest.approx(4001.1 - 3999.0)
    assert s.trade.entry_slippage == pytest.approx(s.trade.entry_gap - target)
    assert s.trade.entry_slippage == pytest.approx(0.1)


def test_exit_stays_when_gap_moves_away():
    s = seeded()
    t = open_long(s, MON_10)
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "exit_resting"
    t += timedelta(seconds=1)
    step(s, t, (3998.0, 3998.2))  # gap widens further
    assert s.state == "exit_resting"


def test_exit_follows_normal():
    s = seeded()
    t = open_long(s, MON_10)
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    before = s.order.price
    t += timedelta(seconds=3)
    reseed(s, t, NORMAL + 1)
    step(s, t, (4000.9, 4001.1))
    assert s.order.price == pytest.approx(before + 1)


def test_entry_follows_the_book_without_crossing():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    assert s.order.price == pytest.approx(4001.1)
    t += timedelta(seconds=3)
    step(s, t, (4001.3, 4001.5))  # coin ticks up, gap still far enough from normal
    assert s.state == "entry_resting"
    assert s.order.price == pytest.approx(4001.4)
    assert s.order.placed == MON_10


def test_entry_cancelled_when_gap_returns_before_fill():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    t += timedelta(seconds=2)
    ev = step(s, t, (4003.0, 4003.2))
    assert s.state == "flat" and ev[-1].name == "entry cancelled"
    assert s.stats["cancelled"] == 1


def test_entry_missed_after_timeout():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    t += timedelta(seconds=s.params.entry_timeout_s + 1)
    step(s, t, (4001.0, 4001.2))
    assert s.state == "flat" and s.stats["missed"] == 1


def test_stale_quote_cancels_resting_entry():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    t += timedelta(seconds=1)
    ev = s.step(t, q(4001.0, 4001.2, t), q(*HEDGE, t - timedelta(seconds=20)))
    assert s.state == "flat" and ev[-1].detail == "stale quotes"


def test_hedge_waits_only_for_the_hedge_quote():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    t += timedelta(seconds=1)
    step(s, t, (4000.9, s.order.price))
    t += timedelta(seconds=1)
    s.step(t, q(4000.9, 4001.1, t - timedelta(seconds=60)), q(*HEDGE, t))  # coin stale
    assert s.state == "open"


def test_entry_cancelled_when_window_closes():
    t = datetime(2026, 11, 9, 21, 28, tzinfo=timezone.utc)  # 16:28 New York, nightly block at 16:29
    s = seeded(t)
    step(s, t, (4001.0, 4001.2))
    assert s.state == "entry_resting"
    t += timedelta(minutes=2)
    reseed(s, t)
    ev = step(s, t, (4001.0, 4001.2))
    assert s.state == "flat" and ev[-1].detail == "entry window closed"


def test_exit_pulled_before_nightly_close_and_rests_again_after():
    t = datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)
    s = seeded(t)
    t = open_long(s, t)
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "exit_resting"

    t = datetime(2026, 11, 9, 21, 55, tzinfo=timezone.utc)  # 4 min before 16:59 New York
    reseed(s, t)
    ev = step(s, t, (4000.9, 4001.1))
    assert s.state == "open" and "hedge venue closing" in ev[-1].detail

    t = datetime(2026, 11, 9, 23, 5, tzinfo=timezone.utc)  # reopened
    reseed(s, t)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "exit_resting"


def test_flatten_before_weekend():
    t = FRI_CLOSE - timedelta(hours=3)
    s = seeded(t)
    t = open_long(s, t)

    late = FRI_CLOSE - timedelta(minutes=59)
    reseed(s, late)
    ev = step(s, late, (4001.0, 4001.2))
    closed = [e for e in ev if e.name == "closed"]
    assert closed and closed[0].trade.exit_reason == "closure"
    assert not math.isnan(closed[0].trade.exit_target)
    assert s.state == "flat"


def test_trade_held_through_weekend_is_flagged():
    t = FRI_CLOSE - timedelta(hours=3)
    s = seeded(t)
    open_long(s, t)
    monday = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
    reseed(s, monday)
    ev = step(s, monday, (4000.9, 4001.1))
    assert "carried over close" in names(ev)
    assert s.trade.carried


def test_swap_counted_per_rollover():
    s = seeded()
    t = open_long(s, MON_10)
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    wed = datetime(2026, 11, 11, 23, 30, tzinfo=timezone.utc)  # after Mon, Tue and Wed closes
    reseed(s, wed)
    step(s, wed, (4005.0, 4005.2))
    step(s, wed + timedelta(seconds=1), (s.order.price, 4005.3))
    ev = step(s, wed + timedelta(seconds=2), (4005.0, 4005.2))
    trade = [e for e in ev if e.name == "closed"][0].trade
    assert trade.rollovers == 5
    assert trade.swap(s.params) == pytest.approx(5 * s.params.swap_short)


def test_state_round_trips_through_dict():
    s = seeded()
    open_long(s, MON_10)
    restored = Strategy.from_dict(s.to_dict(), schedule=SCHED)
    assert restored.state == s.state == "open"
    assert repr(restored.trade) == repr(s.trade)  # repr, since NaN != NaN
    assert restored.samples == s.samples
    assert restored.stats == s.stats
    assert restored.last_step == s.last_step


def test_old_state_without_last_step_loads():
    d = seeded().to_dict()
    del d["last_step"]
    assert Strategy.from_dict(d).last_step is None


# --- request budget ------------------------------------------------------------

def test_reprice_throttled_in_time():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    step(s, t + timedelta(seconds=1), (4001.3, 4001.5))
    assert s.order.price == pytest.approx(4001.1)  # too soon
    step(s, t + timedelta(seconds=2), (4001.3, 4001.5))
    assert s.order.price == pytest.approx(4001.4)


def test_far_order_ignores_small_moves():
    s = seeded()
    t = open_long(s, MON_10)
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    before = s.order.price  # sell at ~4005.1, about 4 above the touch
    t += timedelta(seconds=3)
    step(s, t, (4000.9, 4001.1), hedge=(4000.5, 4000.6))  # target up 0.5
    assert s.order.price == before
    t += timedelta(seconds=3)
    step(s, t, (4000.9, 4001.1), hedge=(4001.0, 4001.1))  # target up 1.0
    assert s.order.price == pytest.approx(before + 1)


def test_requests_counted():
    s = seeded()
    t = open_long(s, MON_10)  # place
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))  # exit place
    step(s, t + timedelta(seconds=3), (4000.9, 4001.1), hedge=(4001.0, 4001.1))  # amend
    assert s.stats["requests"] == 3


# --- fast market -----------------------------------------------------------------

def test_hedge_jump_pulls_exit_until_calm():
    s = seeded()
    t = open_long(s, MON_10)
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "exit_resting"
    t += timedelta(seconds=1)
    ev = step(s, t, (4000.9, 4001.1), hedge=(4002.0, 4002.1))  # +2 in a second
    assert s.state == "open" and "fast market" in ev[-1].detail
    t += timedelta(seconds=5)
    step(s, t, (4000.9, 4001.1), hedge=(4002.0, 4002.1))
    assert s.state == "open"  # still cooling down
    t += timedelta(seconds=6)
    step(s, t, (4000.9, 4001.1), hedge=(4002.0, 4002.1))
    assert s.state == "exit_resting"


def test_wide_hedge_spread_blocks_entries():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2), hedge=(3999.8, 4000.3))
    assert s.state == "flat"


def test_flatten_ignores_fast_market():
    t = FRI_CLOSE - timedelta(hours=3)
    s = seeded(t)
    open_long(s, t)
    late = FRI_CLOSE - timedelta(minutes=59)
    reseed(s, late)
    ev = step(s, late, (4001.0, 4001.2), hedge=(3999.5, 4000.5))
    assert [e for e in ev if e.name == "closed"]


def test_volume_and_budget_peak():
    s = seeded()
    t = open_long(s, MON_10)
    assert s.stats["budget_peak"] == 1  # the placement, before any volume
    assert s.stats["volume"] == pytest.approx(4001.1)
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    assert s.stats["budget_peak"] == 1  # 2 requests against ~4001 traded
