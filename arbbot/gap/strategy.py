"""Gap strategy core: a pure state machine shared by paper trading, replay and live.

No I/O. Feed it quotes with step(); it returns events, and finished trades end up in
the events that close them.

Entry and exit orders rest as post-only maker orders on the coin, priced off the
hedge quote so a fill locks in the target gap:
  long  (gap too low):  buy coin at <= hedge_bid + normal - entry, sell hedge at bid
  short (gap too high): sell coin at >= hedge_ask + normal + entry, buy hedge at ask
Exits target a gap of normal. A coin fill is hedged hedge_delay_s later at the hedge
quote current at that time. Gap = coin mid - hedge mid.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

WEEKDAY_FRIDAY = 4


@dataclass(frozen=True)
class Params:
    entry: float = 3.0
    window_h: float = 6.0
    min_samples: int = 12
    sample_every_s: float = 300
    entry_timeout_s: float = 60
    hedge_delay_s: float = 0.5
    stale_after_s: float = 5.0
    tick: float = 0.1
    coin_maker: float = 0.00015
    coin_taker: float = 0.00045
    hedge_commission: float = 30 / 1_000_000
    swap_long: float = -0.5397  # USD per unit per night when long the hedge leg
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
    exit_reason: str = ""
    coin_fees: float = 0.0

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    @property
    def entry_gap(self) -> float:
        return self.coin_entry - self.hedge_entry

    @property
    def exit_gap(self) -> float:
        return self.coin_exit - self.hedge_exit

    def gross(self) -> float:
        return self.sign * (self.coin_exit - self.coin_entry) - self.sign * (self.hedge_exit - self.hedge_entry)

    def fees(self, p: Params) -> float:
        return self.coin_fees + (self.hedge_entry + self.hedge_exit) * p.hedge_commission

    def swap(self, p: Params) -> float:
        per_night = p.swap_short if self.direction == "long" else p.swap_long
        return per_night * swap_nights(self.opened, self.closed)

    def net(self, p: Params) -> float:
        return self.gross() - self.fees(p) + self.swap(p)


@dataclass
class Event:
    at: datetime
    name: str
    detail: str
    trade: Trade | None = None


def swap_nights(start: datetime, end: datetime) -> int:
    """cTrader rollovers at 22:00 UTC in (start, end]; Wednesday counts triple, weekends none."""
    nights = 0
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        t = day + timedelta(hours=22)
        if start < t <= end and t.weekday() < 5:
            nights += 3 if t.weekday() == 2 else 1
        day += timedelta(days=1)
    return nights


def entries_allowed(now: datetime) -> bool:
    """No new trades near the hedge venue's nightly break, on Friday evening or at weekends."""
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    if 21 * 60 + 30 <= minutes < 23 * 60 + 5:
        return False
    return not (now.weekday() == WEEKDAY_FRIDAY and now.hour >= 20)


def must_flatten(now: datetime) -> bool:
    """Close open trades before the hedge venue shuts for the weekend."""
    return now.weekday() == WEEKDAY_FRIDAY and (now.hour, now.minute) >= (21, 30)


def round_down(px: float, tick: float) -> float:
    return math.floor(px / tick + 1e-9) * tick


def round_up(px: float, tick: float) -> float:
    return math.ceil(px / tick - 1e-9) * tick


def filled(order: Order, coin: Quote) -> bool:
    """Conservative: count a fill only once the opposite side trades at or through our price."""
    return coin.ask <= order.price if order.side == "buy" else coin.bid >= order.price


@dataclass
class Strategy:
    params: Params = field(default_factory=Params)
    state: str = "flat"  # flat, entry_resting, entry_hedging, open, exit_resting, exit_hedging
    order: Order | None = None
    trade: Trade | None = None
    hedge_due: datetime | None = None
    samples: list[tuple[datetime, float]] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {
        "placed": 0, "filled": 0, "missed": 0, "cancelled": 0, "closed": 0, "net": 0.0})

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
        if self.trade.direction == "long":  # sell coin, buy hedge back at ask
            px = max(round_up(hedge.ask + normal, p.tick), coin.bid + p.tick)
            return Order("sell", px, "exit", now, normal)
        px = min(round_down(hedge.bid + normal, p.tick), coin.ask - p.tick)
        return Order("buy", px, "exit", now, normal)

    # --- main loop ---------------------------------------------------------------

    def step(self, now: datetime, coin: Quote, hedge: Quote) -> list[Event]:
        p = self.params
        events: list[Event] = []
        stale = ((now - coin.at).total_seconds() > p.stale_after_s
                 or (now - hedge.at).total_seconds() > p.stale_after_s)

        if self.state in ("entry_hedging", "exit_hedging"):
            if not stale and now >= self.hedge_due:
                self._complete_hedge(now, hedge, events)
            return events

        if stale:
            if self.state == "entry_resting":
                self._cancel_entry(now, "stale quotes", events)
            elif self.state == "exit_resting":
                self.state, self.order = "open", None
                events.append(Event(now, "exit cancelled", "stale quotes; keeping trade open"))
            return events

        gap = coin.mid - hedge.mid
        normal = self.normal(now)
        self.add_sample(now, gap)
        if normal is None:
            return events

        if self.state in ("open", "exit_resting") and must_flatten(now):
            self._flatten(now, coin, hedge, events)
            return events

        if self.state == "flat":
            if entries_allowed(now) and abs(gap - normal) >= p.entry:
                direction = "long" if gap < normal else "short"
                self.order = self.entry_order(direction, normal, coin, hedge, now)
                self.state = "entry_resting"
                self.stats["placed"] += 1
                events.append(Event(now, "entry order",
                                    f"{self.order.side} coin at {self.order.price:.2f} for gap "
                                    f"{self.order.target_gap:+.2f} (gap {gap:+.2f}, normal {normal:+.2f})"))

        elif self.state == "entry_resting":
            if filled(self.order, coin):
                direction = "long" if self.order.side == "buy" else "short"
                self.trade = Trade(direction, now, self.order.target_gap, self.order.price,
                                   coin_fees=self.order.price * p.coin_maker)
                self.state, self.hedge_due = "entry_hedging", now + timedelta(seconds=p.hedge_delay_s)
                self.stats["filled"] += 1
                events.append(Event(now, "entry filled", f"{self.order.side} coin {self.order.price:.2f}"))
                self.order = None
            elif abs(gap - normal) < p.entry:
                self._cancel_entry(now, f"gap {gap:+.2f} back within {p.entry:g} of normal {normal:+.2f}", events)
            elif (now - self.order.placed).total_seconds() > p.entry_timeout_s:
                self.state, self.order = "flat", None
                self.stats["missed"] += 1
                events.append(Event(now, "entry missed", "order never traded"))
            else:
                direction = "long" if self.order.side == "buy" else "short"
                self.order = self._reprice(self.entry_order(direction, normal, coin, hedge, now))

        elif self.state == "open":
            if self._back_at_normal(gap, normal):
                self.order = self.exit_order(normal, coin, hedge, now)
                self.state = "exit_resting"
                events.append(Event(now, "exit order", f"{self.order.side} coin at {self.order.price:.2f}"))

        elif self.state == "exit_resting":
            if filled(self.order, coin):
                self.trade.coin_exit = self.order.price
                self.trade.coin_fees += self.order.price * p.coin_maker
                self.trade.exit_reason = "normal"
                self.state, self.hedge_due = "exit_hedging", now + timedelta(seconds=p.hedge_delay_s)
                events.append(Event(now, "exit filled", f"{self.order.side} coin {self.order.price:.2f}"))
                self.order = None
            elif not self._back_at_normal(gap, normal):
                self.state, self.order = "open", None
                events.append(Event(now, "exit cancelled", f"gap {gap:+.2f} moved away from normal {normal:+.2f}"))
            else:
                self.order = self._reprice(self.exit_order(normal, coin, hedge, now))
        return events

    # --- helpers ---------------------------------------------------------------

    def _reprice(self, new: Order) -> Order:
        """Follow the hedge quote but keep the original placement time for the timeout."""
        new.placed = self.order.placed
        return new

    def _back_at_normal(self, gap: float, normal: float) -> bool:
        return gap >= normal if self.trade.direction == "long" else gap <= normal

    def _cancel_entry(self, now: datetime, why: str, events: list[Event]) -> None:
        self.state, self.order = "flat", None
        self.stats["cancelled"] += 1
        events.append(Event(now, "entry cancelled", why))

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
        """Weekend close: both legs taken immediately at the quote."""
        t = self.trade
        t.coin_exit = coin.bid if t.direction == "long" else coin.ask
        t.coin_fees += t.coin_exit * self.params.coin_taker
        t.hedge_exit = hedge.ask if t.direction == "long" else hedge.bid
        t.exit_reason = "weekend"
        self.order = None
        self._close(now, events)

    def _close(self, now: datetime, events: list[Event]) -> None:
        t, p = self.trade, self.params
        t.closed = now
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
            "samples": self.samples,
            "stats": self.stats,
        })

    @classmethod
    def from_dict(cls, data: dict, params: Params | None = None) -> Strategy:
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
            state=d["state"],
            order=Order(**d["order"]) if d["order"] else None,
            trade=Trade(**d["trade"]) if d["trade"] else None,
            hedge_due=d["hedge_due"],
            samples=[(ts, g) for ts, g in d["samples"]],
            stats=d["stats"],
        )
