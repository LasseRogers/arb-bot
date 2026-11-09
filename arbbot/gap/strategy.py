"""Gap strategy core: a pure state machine shared by paper trading, replay and live.

No I/O. Feed it quotes with step(); it returns events, and finished trades end up in
the events that close them. Session timing comes from a Schedule of the hedge venue.

Entry and exit orders rest as post-only maker orders on the coin, priced off the
hedge quote so a fill locks in the target gap:
  long  (gap too low):  buy coin at <= hedge_bid + normal - entry, sell hedge at bid
  short (gap too high): sell coin at >= hedge_ask + normal + entry, buy hedge at ask
The exit rests from the moment the trade is hedged, targeting normal (less exit_offset),
and follows the rolling normal and hedge quote until it fills. A coin fill is hedged
hedge_delay_s later at the hedge quote current at that time. Gap = coin mid - hedge mid.

Repricing is throttled to stay inside Hyperliquid's request budget (one request per
USDC traded): at most one amend per reprice_min_s, and only when the price moves a
tick near the touch or reprice_far further out. Every place, amend (one modify) and
cancel counts in stats["requests"]; coin fills add to stats["volume"] at Params.size,
and stats["budget_peak"] keeps the worst requests - volume seen, for the buffer check.

Resting orders come off when quotes go stale, the hedge venue is about to close, or
the hedge moves fast (a jump, or a wide spread); no new orders until it calms down.
Open trades are closed at the quote before a weekend or holiday closure.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from arbbot.gap.session import Schedule

RESTING = ("entry_resting", "exit_resting")
HEDGING = ("entry_hedging", "exit_hedging")


@dataclass(frozen=True)
class Params:
    size: float = 1.0  # coin units per trade; the hedge minimum is 0.01 lot = 1 oz
    entry: float = 3.0
    exit_offset: float = 0.0  # take profit this far short of normal
    window_h: float = 6.0
    min_samples: int = 12
    sample_every_s: float = 300
    entry_timeout_s: float = 60
    hedge_delay_s: float = 0.5
    stale_after_s: float = 15.0
    reprice_min_s: float = 2.0
    reprice_near: float = 0.5  # within this of the touch, follow tick by tick
    reprice_far: float = 1.0
    jump: float = 1.5  # hedge mid move within jump_window_s that counts as fast
    jump_window_s: float = 3.0
    max_hedge_spread: float = 0.30
    calm_after_s: float = 10.0
    tick: float = 0.1
    coin_maker: float = 0.00015
    coin_taker: float = 0.00045
    hedge_commission: float = 30 / 1_000_000
    swap_long: float = -0.5397  # USD per unit per rollover when long the hedge leg
    swap_short: float = 0.368


@dataclass
class Quote:
    bid: float
    ask: float
    at: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class Order:
    side: str  # "buy" or "sell" on the coin
    price: float
    purpose: str  # "entry" or "exit"
    placed: datetime
    target_gap: float
    amended: datetime | None = None


@dataclass
class Trade:
    direction: str  # "long": long coin, short hedge. "short": the reverse.
    opened: datetime
    target_gap: float
    coin_entry: float
    hedge_entry: float = math.nan
    closed: datetime | None = None
    coin_exit: float = math.nan
    hedge_exit: float = math.nan
    exit_target: float = math.nan
    exit_reason: str = ""
    coin_fees: float = 0.0
    rollovers: int = 0
    carried: bool = False  # held through a weekend or holiday closure

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    @property
    def entry_gap(self) -> float:
        return self.coin_entry - self.hedge_entry

    @property
    def exit_gap(self) -> float:
        return self.coin_exit - self.hedge_exit

    @property
    def entry_slippage(self) -> float:
        """Gap given up between the coin fill and the hedge. Positive is a cost."""
        return self.sign * (self.entry_gap - self.target_gap)

    @property
    def exit_slippage(self) -> float:
        return self.sign * (self.exit_target - self.exit_gap)

    def gross(self) -> float:
        return self.sign * (self.exit_gap - self.entry_gap)

    def fees(self, p: Params) -> float:
        return self.coin_fees + (self.hedge_entry + self.hedge_exit) * p.hedge_commission

    def swap(self, p: Params) -> float:
        per_rollover = p.swap_short if self.direction == "long" else p.swap_long
        return per_rollover * self.rollovers

    def net(self, p: Params) -> float:
        return self.gross() - self.fees(p) + self.swap(p)


@dataclass
class Event:
    at: datetime
    name: str
    detail: str
    trade: Trade | None = None


def round_down(px: float, tick: float) -> float:
    return round(math.floor(px / tick + 1e-9) * tick, 10)


def round_up(px: float, tick: float) -> float:
    return round(math.ceil(px / tick - 1e-9) * tick, 10)


def filled(order: Order, coin: Quote) -> bool:
    """Conservative: count a fill only once the opposite side trades at or through our price."""
    return coin.ask <= order.price if order.side == "buy" else coin.bid >= order.price


@dataclass
class Strategy:
    params: Params = field(default_factory=Params)
    schedule: Schedule | None = None  # not persisted: rebuilt from the broker on start
    state: str = "flat"  # flat, entry_resting, entry_hedging, open, exit_resting, exit_hedging
    order: Order | None = None
    trade: Trade | None = None
    hedge_due: datetime | None = None
    last_step: datetime | None = None
    fast_until: datetime | None = None
    hedge_marks: deque = field(default_factory=deque)  # (time, mid), not persisted
    samples: list[tuple[datetime, float]] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {
        "placed": 0, "filled": 0, "missed": 0, "cancelled": 0, "closed": 0, "requests": 0,
        "volume": 0.0, "budget_peak": 0.0, "net": 0.0})

    # --- normal --------------------------------------------------------------

    def add_sample(self, now: datetime, gap: float) -> None:
        if not self.samples or (now - self.samples[-1][0]).total_seconds() >= self.params.sample_every_s:
            self.samples.append((now, gap))
        cutoff = now - timedelta(hours=self.params.window_h)
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.pop(0)

    def normal(self, now: datetime) -> float | None:
        """Median of samples strictly before now and inside the window: never looks ahead."""
        cutoff = now - timedelta(hours=self.params.window_h)
        recent = [g for ts, g in self.samples if cutoff <= ts < now]
        return statistics.median(recent) if len(recent) >= self.params.min_samples else None

    # --- pricing ---------------------------------------------------------------

    def entry_order(self, direction: str, normal: float, coin: Quote, hedge: Quote, now: datetime) -> Order:
        p = self.params
        if direction == "long":
            target = normal - p.entry
            px = min(round_down(hedge.bid + target, p.tick), coin.ask - p.tick)
            return Order("buy", px, "entry", now, target)
        target = normal + p.entry
        px = max(round_up(hedge.ask + target, p.tick), coin.bid + p.tick)
        return Order("sell", px, "entry", now, target)

    def exit_order(self, normal: float, coin: Quote, hedge: Quote, now: datetime) -> Order:
        p = self.params
        target = normal - self.trade.sign * p.exit_offset
        if self.trade.direction == "long":  # sell coin, buy hedge back at ask
            px = max(round_up(hedge.ask + target, p.tick), coin.bid + p.tick)
            return Order("sell", px, "exit", now, target)
        px = min(round_down(hedge.bid + target, p.tick), coin.ask - p.tick)
        return Order("buy", px, "exit", now, target)

    # --- main loop ---------------------------------------------------------------

    def step(self, now: datetime, coin: Quote, hedge: Quote) -> list[Event]:
        if self.schedule is None:
            raise RuntimeError("Strategy.schedule must be set before step()")
        events: list[Event] = []
        self._check_carried(now, events)
        self.last_step = now
        self._step(now, coin, hedge, events)
        return events

    def _step(self, now: datetime, coin: Quote, hedge: Quote, events: list[Event]) -> None:
        p, sched = self.params, self.schedule
        hedge_stale = (now - hedge.at).total_seconds() > p.stale_after_s
        stale = hedge_stale or (now - coin.at).total_seconds() > p.stale_after_s

        if self.state in HEDGING:
            if not hedge_stale and sched.is_open(now) and now >= self.hedge_due:
                self._complete_hedge(now, hedge, events)
            return

        if self.state in RESTING and (stale or not sched.quoting_allowed(now)):
            self._pull_order(now, "stale quotes" if stale else "hedge venue closing", events)
        if stale:
            return

        why_fast = self._fast_market(now, hedge)
        if why_fast:
            self.fast_until = now + timedelta(seconds=p.calm_after_s)
            if self.state in RESTING:
                self._pull_order(now, f"fast market: {why_fast}", events)
        calm = self.fast_until is None or now >= self.fast_until

        if self.state in ("open", "exit_resting") and sched.must_flatten(now):
            self._flatten(now, coin, hedge, events)
            return

        gap = coin.mid - hedge.mid
        normal = self.normal(now)
        self.add_sample(now, gap)
        if normal is None:
            return

        if self.state == "flat":
            if calm and sched.entries_allowed(now) and abs(gap - normal) >= p.entry:
                direction = "long" if gap < normal else "short"
                self.order = self.entry_order(direction, normal, coin, hedge, now)
                self.state = "entry_resting"
                self.stats["placed"] += 1
                self._request()
                events.append(Event(now, "entry order",
                                    f"{self.order.side} coin at {self.order.price:.2f} for gap "
                                    f"{self.order.target_gap:+.2f} (gap {gap:+.2f}, normal {normal:+.2f})"))

        elif self.state == "entry_resting":
            if filled(self.order, coin):
                direction = "long" if self.order.side == "buy" else "short"
                self.trade = Trade(direction, now, self.order.target_gap, self.order.price,
                                   coin_fees=self.order.price * p.coin_maker)
                self._traded(self.order.price)
                self.state, self.hedge_due = "entry_hedging", now + timedelta(seconds=p.hedge_delay_s)
                self.stats["filled"] += 1
                events.append(Event(now, "entry filled", f"{self.order.side} coin {self.order.price:.2f}"))
                self.order = None
            elif not sched.entries_allowed(now):
                self._pull_order(now, "entry window closed", events)
            elif abs(gap - normal) < p.entry:
                self._pull_order(now, f"gap {gap:+.2f} back within {p.entry:g} of normal {normal:+.2f}", events)
            elif (now - self.order.placed).total_seconds() > p.entry_timeout_s:
                self.state, self.order = "flat", None
                self.stats["missed"] += 1
                self._request()
                events.append(Event(now, "entry missed", "order never traded"))
            else:
                direction = "long" if self.order.side == "buy" else "short"
                self._reprice(self.entry_order(direction, normal, coin, hedge, now), coin, now)

        elif self.state == "open":
            if calm and sched.quoting_allowed(now):
                self.order = self.exit_order(normal, coin, hedge, now)
                self.state = "exit_resting"
                self._request()
                events.append(Event(now, "exit order", f"{self.order.side} coin at {self.order.price:.2f} "
                                                       f"for gap {self.order.target_gap:+.2f}"))

        elif self.state == "exit_resting":
            if filled(self.order, coin):
                self.trade.coin_exit = self.order.price
                self.trade.exit_target = self.order.target_gap
                self.trade.coin_fees += self.order.price * p.coin_maker
                self.trade.exit_reason = "normal"
                self._traded(self.order.price)
                self.state, self.hedge_due = "exit_hedging", now + timedelta(seconds=p.hedge_delay_s)
                events.append(Event(now, "exit filled", f"{self.order.side} coin {self.order.price:.2f}"))
                self.order = None
            else:
                self._reprice(self.exit_order(normal, coin, hedge, now), coin, now)

    # --- helpers ---------------------------------------------------------------

    def _request(self) -> None:
        st = self.stats
        st["requests"] = st.get("requests", 0) + 1
        st["budget_peak"] = max(st.get("budget_peak", 0.0), st["requests"] - st.get("volume", 0.0))

    def _traded(self, price: float) -> None:
        self.stats["volume"] = self.stats.get("volume", 0.0) + price * self.params.size

    def _reprice(self, new: Order, coin: Quote, now: datetime) -> None:
        """Amend the resting order if it moved enough and the last amend is old enough.

        Keeps the original placement time for the entry timeout.
        """
        p, old = self.params, self.order
        last = old.amended or old.placed
        if (now - last).total_seconds() < p.reprice_min_s:
            return
        from_touch = new.price - coin.ask if new.side == "sell" else coin.bid - new.price
        step = p.tick if from_touch <= p.reprice_near else p.reprice_far
        if abs(new.price - old.price) < step - 1e-9:
            return
        new.placed, new.amended = old.placed, now
        self.order = new
        self._request()

    def _fast_market(self, now: datetime, hedge: Quote) -> str:
        p, marks = self.params, self.hedge_marks
        marks.append((now, hedge.mid))
        while marks and (now - marks[0][0]).total_seconds() > p.jump_window_s:
            marks.popleft()
        if hedge.ask - hedge.bid > p.max_hedge_spread:
            return f"hedge spread {hedge.ask - hedge.bid:.2f}"
        move = max(abs(hedge.mid - m) for _, m in marks)
        return f"hedge moved {move:.2f}" if move >= p.jump else ""

    def _pull_order(self, now: datetime, why: str, events: list[Event]) -> None:
        self._request()
        if self.state == "entry_resting":
            self.state, self.order = "flat", None
            self.stats["cancelled"] += 1
            events.append(Event(now, "entry cancelled", why))
        else:
            self.state, self.order = "open", None
            events.append(Event(now, "exit cancelled", f"{why}; keeping trade open"))

    def _check_carried(self, now: datetime, events: list[Event]) -> None:
        if (self.trade is not None and not self.trade.carried and self.last_step is not None
                and self.schedule.long_closure_between(self.last_step, now)):
            self.trade.carried = True
            events.append(Event(now, "carried over close", f"{self.trade.direction} trade held through "
                                                           f"a weekend or holiday closure"))

    def _complete_hedge(self, now: datetime, hedge: Quote, events: list[Event]) -> None:
        t = self.trade
        if self.state == "entry_hedging":
            t.hedge_entry = hedge.bid if t.direction == "long" else hedge.ask
            self.state = "open"
            events.append(Event(now, "entry hedged", f"hedge at {t.hedge_entry:.2f}, gap {t.entry_gap:+.2f} "
                                                     f"(target {t.target_gap:+.2f})"))
            return
        t.hedge_exit = hedge.ask if t.direction == "long" else hedge.bid
        self._close(now, events)

    def _flatten(self, now: datetime, coin: Quote, hedge: Quote, events: list[Event]) -> None:
        """Close before a long closure: both legs taken immediately at the quote."""
        t = self.trade
        normal = self.normal(now)
        t.exit_target = self.order.target_gap if self.order else (normal if normal is not None else math.nan)
        t.coin_exit = coin.bid if t.direction == "long" else coin.ask
        t.coin_fees += t.coin_exit * self.params.coin_taker
        t.hedge_exit = hedge.ask if t.direction == "long" else hedge.bid
        t.exit_reason = "closure"
        self._traded(t.coin_exit)
        if self.order:
            self._request()
        self.order = None
        self._close(now, events)

    def _close(self, now: datetime, events: list[Event]) -> None:
        t, p = self.trade, self.params
        t.closed = now
        t.rollovers = self.schedule.rollovers(t.opened, now)
        net = t.net(p)
        self.stats["closed"] += 1
        self.stats["net"] += net
        events.append(Event(now, "closed", f"{t.direction} ({t.exit_reason}): gap {t.entry_gap:+.2f} -> "
                                           f"{t.exit_gap:+.2f}, net {net:+.2f}", trade=t))
        self.state, self.trade, self.hedge_due = "flat", None, None

    # --- persistence -------------------------------------------------------------

    def to_dict(self) -> dict:
        def enc(v):
            if isinstance(v, datetime):
                return {"__dt__": v.isoformat()}
            if isinstance(v, dict):
                return {k: enc(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [enc(x) for x in v]
            return v
        return enc({
            "state": self.state,
            "order": asdict(self.order) if self.order else None,
            "trade": asdict(self.trade) if self.trade else None,
            "hedge_due": self.hedge_due,
            "last_step": self.last_step,
            "samples": self.samples,
            "stats": self.stats,
        })

    @classmethod
    def from_dict(cls, data: dict, params: Params | None = None, schedule: Schedule | None = None) -> Strategy:
        def dec(v):
            if isinstance(v, dict):
                if set(v) == {"__dt__"}:
                    return datetime.fromisoformat(v["__dt__"])
                return {k: dec(x) for k, x in v.items()}
            if isinstance(v, list):
                return [dec(x) for x in v]
            return v
        d = dec(data)
        return cls(
            params=params or Params(),
            schedule=schedule,
            state=d["state"],
            order=Order(**d["order"]) if d["order"] else None,
            trade=Trade(**d["trade"]) if d["trade"] else None,
            hedge_due=d["hedge_due"],
            last_step=d.get("last_step"),
            samples=[(ts, g) for ts, g in d["samples"]],
            stats=d["stats"],
        )
