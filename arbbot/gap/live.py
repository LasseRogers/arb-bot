"""Paper-trade the gap strategy on live quotes. No real orders are placed.

Entries and exits on the Hyperliquid coin are simulated as resting maker orders,
counted as filled only once the market trades at or through their price. The
cTrader leg is filled at its live quote the moment the coin leg fills.
Normal is the median gap over the previous --window hours.
"""

import argparse
import csv
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import anyio.to_thread
import pandas as pd
from ctrader_api_client.events.types import SpotEvent

from arbbot.gap import history
from arbbot.venues import ctrader, hyperliquid

SAMPLE_EVERY = timedelta(minutes=5)
MIN_SAMPLES = 12
STALE_AFTER_S = 120
HL_MAKER = 0.00015
CT_COMMISSION = 30 / 1_000_000
TRADES = Path("data/paper/gold_trades.csv")
EVENTS = Path("data/paper/gold_events.csv")


@dataclass
class Order:
    side: str  # "buy" or "sell" on the Hyperliquid coin
    price: float
    placed: float  # time.monotonic() when placed

    def filled_by(self, bid: float, ask: float) -> bool:
        """Count a fill only once the market has traded at or through our price."""
        if self.side == "buy":
            return ask <= self.price or bid < self.price
        return bid >= self.price or ask > self.price


@dataclass
class Trade:
    direction: str  # "long": long the coin, short cTrader. "short": the reverse.
    opened: str
    hl_entry: float
    ct_entry: float
    closed: str = ""
    hl_exit: float = 0.0
    ct_exit: float = 0.0

    def gross(self) -> float:
        s = 1 if self.direction == "long" else -1
        return s * (self.hl_exit - self.hl_entry) - s * (self.ct_exit - self.ct_entry)

    def fees(self) -> float:
        return ((self.hl_entry + self.hl_exit) * HL_MAKER
                + (self.ct_entry + self.ct_exit) * CT_COMMISSION)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--hl", default="PAXG", help="Hyperliquid coin")
    parser.add_argument("--ct", default="XAUUSD", help="cTrader symbol")
    parser.add_argument("--window", type=float, default=6, help="hours used for 'normal'")
    parser.add_argument("--entry", type=float, default=3, help="distance from normal to start")
    parser.add_argument("--order-timeout", type=float, default=60, help="seconds a resting order waits")
    parser.add_argument("--interval", type=float, default=5, help="seconds between checks")
    return parser.parse_args()


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def append_csv(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def event(now: datetime, name: str, detail: str) -> None:
    append_csv(EVENTS, {"time": iso(now), "event": name, "detail": detail})
    print(f"  ** {name}: {detail}")


def entries_allowed(now: datetime) -> bool:
    """No new trades near cTrader's nightly break (21:30-23:05 UTC) or on Friday evening."""
    minutes = now.hour * 60 + now.minute
    if 21 * 60 + 30 <= minutes < 23 * 60 + 5:
        return False
    return not (now.weekday() == 4 and now.hour >= 20)


def seed_samples(hl_coin: str, ct_symbol: str) -> list[tuple[datetime, float]]:
    a = history.fetch_closes(f"hl:{hl_coin}", 3, "5m")
    b = history.fetch_closes(f"ctrader:{ct_symbol}", 3, "5m")
    df = pd.concat([a, b], axis=1, join="inner")
    gap = df.iloc[:, 0] - df.iloc[:, 1]
    return [(ts.to_pydatetime(), float(g)) for ts, g in gap.items()]


def normal_gap(samples: list[tuple[datetime, float]], now: datetime, window: timedelta) -> float | None:
    recent = [g for ts, g in samples if ts >= now - window]
    return statistics.median(recent) if len(recent) >= MIN_SAMPLES else None


async def run(args: argparse.Namespace, samples: list[tuple[datetime, float]], stats: dict) -> None:
    window = timedelta(hours=args.window)
    async with ctrader.connect() as (client, account_id):
        symbol_id = await ctrader.find_symbol_id(client, account_id, args.ct)
        ct = {"bid": None, "ask": None, "at": 0.0}

        @client.on(SpotEvent, symbol_id=symbol_id)
        async def on_spot(e: SpotEvent) -> None:
            # cTrader only sends the side that changed; keep the other one.
            if e.bid is not None:
                ct["bid"] = float(e.bid)
            if e.ask is not None:
                ct["ask"] = float(e.ask)
            ct["at"] = time.monotonic()

        await client.market_data.subscribe_spots(account_id, [symbol_id])
        print(f"paper trading {args.hl} vs {args.ct}: start at normal ±{args.entry:g}, "
              f"orders wait {args.order_timeout:g}s. Ctrl+C to stop.\n")

        state, order, trade = "flat", None, None
        last_sample = samples[-1][0] if samples else datetime.min.replace(tzinfo=timezone.utc)

        while True:
            now = datetime.now(timezone.utc)
            mono = time.monotonic()
            stamp = now.strftime("%H:%M:%S")

            if ct["bid"] is None or mono - ct["at"] > STALE_AFTER_S:
                if state == "entry_pending":
                    state = "flat"
                    stats["missed"] += 1
                    event(now, "entry cancelled", f"no fresh {args.ct} price")
                print(f"{stamp}  no fresh {args.ct} price (market closed?), waiting [{state}]")
                await anyio.sleep(args.interval)
                continue

            bids, asks = await anyio.to_thread.run_sync(hyperliquid.get_book, args.hl)
            hl_bid, hl_ask = bids[0][0], asks[0][0]
            ct_bid, ct_ask = ct["bid"], ct["ask"]
            gap = (hl_bid + hl_ask) / 2 - (ct_bid + ct_ask) / 2
            normal = normal_gap(samples, now, window)
            if now - last_sample >= SAMPLE_EVERY:
                samples.append((now, gap))
                last_sample = now
            if normal is None:
                print(f"{stamp}  gap {gap:+.2f}  (not enough history for normal yet)")
                await anyio.sleep(args.interval)
                continue

            if state == "flat":
                if entries_allowed(now) and abs(gap - normal) >= args.entry:
                    side = "sell" if gap > normal else "buy"
                    order = Order(side, hl_ask if side == "sell" else hl_bid, mono)
                    state = "entry_pending"
                    stats["placed"] += 1
                    event(now, "entry order", f"{side} {args.hl} at {order.price:.2f} "
                                              f"(gap {gap:+.2f}, normal {normal:+.2f})")

            elif state == "entry_pending":
                if order.filled_by(hl_bid, hl_ask):
                    direction = "long" if order.side == "buy" else "short"
                    ct_px = ct_bid if direction == "long" else ct_ask
                    trade = Trade(direction, iso(now), order.price, ct_px)
                    state = "open"
                    stats["filled"] += 1
                    event(now, "entry filled", f"{order.side} {args.hl} {order.price:.2f}, "
                                               f"{'sell' if direction == 'long' else 'buy'} {args.ct} {ct_px:.2f}")
                elif mono - order.placed > args.order_timeout:
                    state = "flat"
                    stats["missed"] += 1
                    event(now, "entry missed", f"{order.side} {args.hl} at {order.price:.2f} never traded")

            elif state == "open":
                back = gap >= normal if trade.direction == "long" else gap <= normal
                if back:
                    side = "sell" if trade.direction == "long" else "buy"
                    order = Order(side, hl_ask if side == "sell" else hl_bid, mono)
                    state = "exit_pending"
                    event(now, "exit order", f"{side} {args.hl} at {order.price:.2f}")

            elif state == "exit_pending":
                if order.filled_by(hl_bid, hl_ask):
                    trade.hl_exit = order.price
                    trade.ct_exit = ct_ask if trade.direction == "long" else ct_bid
                    trade.closed = iso(now)
                    net = trade.gross() - trade.fees()
                    append_csv(TRADES, asdict(trade) | {
                        "gross": round(trade.gross(), 4), "fees": round(trade.fees(), 4), "net": round(net, 4)})
                    stats["closed"] += 1
                    stats["net"] += net
                    state = "flat"
                    event(now, "closed", f"{trade.direction}: gross {trade.gross():+.2f}, "
                                         f"fees {trade.fees():.2f}, net {net:+.2f}")
                elif mono - order.placed > args.order_timeout:
                    order = Order(order.side, hl_ask if order.side == "sell" else hl_bid, mono)

            print(f"{stamp}  gap {gap:+.2f}  normal {normal:+.2f}  [{state}]")
            await anyio.sleep(args.interval)


def main() -> None:
    args = parse_args()
    print("loading recent history for the normal gap...")
    samples = seed_samples(args.hl, args.ct)
    print(f"{len(samples)} history samples loaded")
    stats = {"placed": 0, "filled": 0, "missed": 0, "closed": 0, "net": 0.0}
    try:
        anyio.run(run, args, samples, stats)
    except KeyboardInterrupt:
        pass
    print(f"\nstopped. entry orders {stats['placed']}, filled {stats['filled']}, "
          f"missed {stats['missed']}; closed trades {stats['closed']}, net {stats['net']:+.2f}")
    print(f"details in {TRADES} and {EVENTS}")


if __name__ == "__main__":
    main()
