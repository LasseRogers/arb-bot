from datetime import datetime, timedelta, timezone

import pytest

from arbbot.gap.strategy import (
    Order, Params, Quote, Strategy, entries_allowed, filled, must_flatten, swap_nights,
)

MON_10 = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
NORMAL = 5.0


def seeded(now: datetime = MON_10, normal: float = NORMAL) -> Strategy:
    s = Strategy()
    s.samples = [(now - timedelta(minutes=5 * i), normal) for i in range(20, 0, -1)]
    return s


def q(bid: float, ask: float, at: datetime) -> Quote:
    return Quote(bid, ask, at)


def step(s: Strategy, t: datetime, coin: tuple[float, float], hedge: tuple[float, float] = (4000.0, 4000.1)):
    return s.step(t, q(*coin, t), q(*hedge, t))


# --- helpers -----------------------------------------------------------------

@pytest.mark.parametrize("start, end, nights", [
    (MON_10, MON_10 + timedelta(days=1), 1),                      # Mon 22:00
    (MON_10 + timedelta(days=1), MON_10 + timedelta(days=3), 4),  # Tue 22:00 + Wed triple
    (MON_10 + timedelta(days=4), MON_10 + timedelta(days=7), 1),  # Fri 22:00 only, weekend free
    (MON_10, MON_10 + timedelta(hours=11), 0),                    # before 22:00
    (MON_10 + timedelta(hours=11, minutes=30), MON_10 + timedelta(hours=12, minutes=30), 1),  # across 22:00
])
def test_swap_nights(start, end, nights):
    assert swap_nights(start, end) == nights


@pytest.mark.parametrize("when, allowed", [
    (MON_10, True),
    (MON_10.replace(hour=22), False),                  # nightly break
    (MON_10 + timedelta(days=4, hours=10, minutes=30), False),  # Friday 20:30
    (MON_10 + timedelta(days=5), False),               # Saturday
])
def test_entries_allowed(when, allowed):
    assert entries_allowed(when) is allowed


def test_must_flatten_only_friday_evening():
    assert must_flatten(MON_10 + timedelta(days=4, hours=11, minutes=30))
    assert not must_flatten(MON_10 + timedelta(days=4, hours=11))
    assert not must_flatten(MON_10.replace(hour=23))


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
    s = Strategy()
    s.samples = [(MON_10 - timedelta(minutes=5 * i), NORMAL) for i in range(1, 6)]
    assert s.normal(MON_10) is None


# --- full cycle --------------------------------------------------------------

def test_long_round_trip_locks_in_edge():
    s, t = seeded(), MON_10
    # Hedge mid 4000.05, coin mid 4001.10 -> gap 1.05, more than 3 below normal 5.
    ev = step(s, t, (4001.0, 4001.2))
    assert s.state == "entry_resting" and ev[0].name == "entry order"
    assert s.order.side == "buy"
    assert s.order.price <= 4000.0 + NORMAL - s.params.entry  # gap at fill <= target

    t += timedelta(seconds=1)
    step(s, t, (4000.9, s.order.price))  # a seller crosses our price
    assert s.state == "entry_hedging"

    t += timedelta(seconds=0.2)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "entry_hedging"  # hedge delay not over yet

    t += timedelta(seconds=0.5)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "open"
    assert s.trade.entry_gap <= s.trade.target_gap + 1e-9

    t += timedelta(minutes=10)
    step(s, t, (4005.0, 4005.2))  # gap 5.05, back at normal
    assert s.state == "exit_resting" and s.order.side == "sell"

    t += timedelta(seconds=1)
    step(s, t, (s.order.price, 4005.3))
    assert s.state == "exit_hedging"

    t += timedelta(seconds=1)
    ev = step(s, t, (4005.0, 4005.2))
    closed = [e for e in ev if e.name == "closed"]
    assert closed and s.state == "flat"
    trade = closed[0].trade
    assert trade.exit_reason == "normal"
    assert trade.gross() == pytest.approx(trade.exit_gap - trade.entry_gap)
    assert trade.gross() > s.params.entry * 0.9
    assert s.stats["closed"] == 1


def test_entry_cancelled_when_gap_returns_before_fill():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    t += timedelta(seconds=2)
    ev = step(s, t, (4003.0, 4003.2))  # gap 3.05, within 3 of normal
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
    ev = s.step(t, q(4001.0, 4001.2, t), q(4000.0, 4000.1, t - timedelta(seconds=20)))
    assert s.state == "flat" and ev[-1].detail == "stale quotes"


def test_weekend_flatten_closes_at_taker():
    fri = MON_10 + timedelta(days=4)
    s, t = seeded(fri), fri
    step(s, t, (4001.0, 4001.2))
    t += timedelta(seconds=1)
    step(s, t, (4000.9, s.order.price))
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))
    assert s.state == "open"

    late = fri.replace(hour=21, minute=31)
    s.samples = [(late - timedelta(minutes=5 * i), NORMAL) for i in range(20, 0, -1)]
    ev = step(s, late, (4001.0, 4001.2))
    closed = [e for e in ev if e.name == "closed"]
    assert closed and closed[0].trade.exit_reason == "weekend"
    assert s.state == "flat"


def test_state_round_trips_through_dict():
    s, t = seeded(), MON_10
    step(s, t, (4001.0, 4001.2))
    t += timedelta(seconds=1)
    step(s, t, (4000.9, s.order.price))
    t += timedelta(seconds=1)
    step(s, t, (4000.9, 4001.1))

    restored = Strategy.from_dict(s.to_dict())
    assert restored.state == s.state == "open"
    assert repr(restored.trade) == repr(s.trade)  # repr, since NaN != NaN
    assert restored.samples == s.samples
    assert restored.stats == s.stats
