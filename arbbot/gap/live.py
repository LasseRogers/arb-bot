"""Watch live quotes for a Hyperliquid coin and a cTrader symbol, and print the
signals the gap strategy would act on. No orders are placed.

Normal is the median gap over the previous --window hours, seeded from 5-minute
history at startup and extended with a live sample every 5 minutes.
"""

import argparse
import statistics
import time
from datetime import datetime, timedelta, timezone

import anyio
import anyio.to_thread
import pandas as pd
from ctrader_api_client.events.types import SpotEvent

from arbbot.gap import history
from arbbot.venues import ctrader, hyperliquid

SAMPLE_EVERY = timedelta(minutes=5)
MIN_SAMPLES = 12
STALE_AFTER_S = 120


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--hl", default="PAXG", help="Hyperliquid coin")
    parser.add_argument("--ct", default="XAUUSD", help="cTrader symbol")
    parser.add_argument("--window", type=float, default=6, help="hours used for 'normal'")
    parser.add_argument("--entry", type=float, default=3, help="distance from normal to start")
    parser.add_argument("--interval", type=float, default=5, help="seconds between checks")
    return parser.parse_args()


def seed_samples(hl_coin: str, ct_symbol: str) -> list[tuple[datetime, float]]:
    a = history.fetch_closes(f"hl:{hl_coin}", 3, "5m")
    b = history.fetch_closes(f"ctrader:{ct_symbol}", 3, "5m")
    gap = pd.concat([a, b], axis=1, join="inner").pipe(lambda df: df.iloc[:, 0] - df.iloc[:, 1])
    return [(ts.to_pydatetime(), float(g)) for ts, g in gap.items()]


def normal_gap(samples: list[tuple[datetime, float]], now: datetime, window: timedelta) -> float | None:
    recent = [g for ts, g in samples if ts >= now - window]
    return statistics.median(recent) if len(recent) >= MIN_SAMPLES else None


async def run(args: argparse.Namespace, samples: list[tuple[datetime, float]]) -> None:
    window = timedelta(hours=args.window)
    async with ctrader.connect() as (client, account_id):
        symbol_id = await ctrader.find_symbol_id(client, account_id, args.ct)
        ct = {"bid": None, "ask": None, "at": 0.0}

        @client.on(SpotEvent, symbol_id=symbol_id)
        async def on_spot(event: SpotEvent) -> None:
            # cTrader only sends the side that changed; keep the other one.
            if event.bid is not None:
                ct["bid"] = float(event.bid)
            if event.ask is not None:
                ct["ask"] = float(event.ask)
            ct["at"] = time.monotonic()

        await client.market_data.subscribe_spots(account_id, [symbol_id])
        print(f"watching {args.hl} vs {args.ct}: start at normal ±{args.entry:g}, "
              f"normal = median of last {args.window:g}h. Ctrl+C to stop.\n")

        position: dict | None = None  # {"side": "short"|"long", "gap": entry gap}
        last_sample = samples[-1][0] if samples else datetime.min.replace(tzinfo=timezone.utc)

        while True:
            now = datetime.now(timezone.utc)
            stamp = now.strftime("%H:%M:%S")
            if ct["bid"] is None or time.monotonic() - ct["at"] > STALE_AFTER_S:
                print(f"{stamp}  no fresh {args.ct} price (market closed?), waiting")
                await anyio.sleep(args.interval)
                continue

            bids, asks = await anyio.to_thread.run_sync(hyperliquid.get_book, args.hl)
            gap = (bids[0][0] + asks[0][0]) / 2 - (ct["bid"] + ct["ask"]) / 2
            normal = normal_gap(samples, now, window)
            if now - last_sample >= SAMPLE_EVERY:
                samples.append((now, gap))
                last_sample = now

            if normal is None:
                print(f"{stamp}  gap {gap:+.2f}  (not enough history for normal yet)")
            elif position is None:
                line = f"{stamp}  gap {gap:+.2f}  normal {normal:+.2f}"
                if gap >= normal + args.entry:
                    position = {"side": "short", "gap": gap}
                    line += f"  >> WOULD START: sell {args.hl}, buy {args.ct}"
                elif gap <= normal - args.entry:
                    position = {"side": "long", "gap": gap}
                    line += f"  >> WOULD START: buy {args.hl}, sell {args.ct}"
                print(line)
            else:
                moved = position["gap"] - gap if position["side"] == "short" else gap - position["gap"]
                line = f"{stamp}  gap {gap:+.2f}  normal {normal:+.2f}  open {position['side']}, gap moved {moved:+.2f}"
                back = gap <= normal if position["side"] == "short" else gap >= normal
                if back:
                    line += "  >> WOULD CLOSE"
                    position = None
                print(line)
            await anyio.sleep(args.interval)


def main() -> None:
    args = parse_args()
    print("loading recent history for the normal gap...")
    samples = seed_samples(args.hl, args.ct)
    print(f"{len(samples)} history samples loaded")
    try:
        anyio.run(run, args, samples)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
