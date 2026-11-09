"""Paper-trade the gap strategy on live quotes. No real orders are placed.

Trading rules live in arbbot.gap.strategy, shared with the replay backtest. This
module only wires it to live data:
  - streams the coin from Hyperliquid and the hedge from cTrader
  - seeds the rolling normal from recent 5-minute history
  - records every top-of-book change to data/ticks/ for replay
  - persists strategy state to data/paper/gold_state.json, so a restart keeps open trades
  - logs events and closed trades (with Hyperliquid funding) to data/paper/
"""

import argparse
import csv
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import anyio.to_thread
import pandas as pd
from ctrader_api_client.events.types import SpotEvent

from arbbot.gap import history
from arbbot.gap.strategy import Params, Quote, Strategy, Trade
from arbbot.venues import ctrader, hyperliquid, hyperliquid_ws

PAPER = Path("data/paper")
STATE = PAPER / "gold_state.json"
TRADES = PAPER / "gold_trades.csv"
EVENTS = PAPER / "gold_events.csv"
TICKS = Path("data/ticks")
SAVE_EVERY_S = 60
FLUSH_EVERY_S = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--hl", default="PAXG", help="Hyperliquid coin")
    parser.add_argument("--ct", default="XAUUSD", help="cTrader symbol")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between strategy steps")
    parser.add_argument("--print-every", type=float, default=5, help="seconds between status lines")
    return parser.parse_args()


def append_csv(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def seed_samples(hl_coin: str, ct_symbol: str, window_h: float) -> list[tuple[datetime, float]]:
    a = history.fetch_closes(f"hl:{hl_coin}", max(window_h / 24 * 2, 3), "5m")
    b = history.fetch_closes(f"ctrader:{ct_symbol}", max(window_h / 24 * 2, 3), "5m")
    df = pd.concat([a, b], axis=1, join="inner")
    gap = df.iloc[:, 0] - df.iloc[:, 1]
    return [(ts.to_pydatetime(), float(g)) for ts, g in gap.items()]


def load_strategy(params: Params, samples: list[tuple[datetime, float]]) -> Strategy:
    """Resume a saved state if there is one. Resting orders don't survive a restart."""
    if not STATE.exists():
        strat = Strategy(params=params)
    else:
        strat = Strategy.from_dict(json.loads(STATE.read_text()), params)
        if strat.state == "entry_resting":
            strat.state, strat.order = "flat", None
        elif strat.state == "exit_resting":
            strat.state, strat.order = "open", None
        print(f"resumed saved state: {strat.state}")
    strat.samples = samples
    return strat


def save_strategy(strat: Strategy) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(strat.to_dict()))
    tmp.replace(STATE)


async def read_swap(client, account_id: int, symbol_id: int) -> tuple[float, float]:
    """cTrader swap in USD per unit per night, from the symbol's pip-based rates."""
    symbol = await client.symbols.get_by_id(account_id, symbol_id)
    pip = 10 ** -symbol.pip_position
    return float(symbol.swap.long) * pip, float(symbol.swap.short) * pip


def coin_funding(trade: Trade, coin: str) -> float:
    """Hyperliquid funding received (+) or paid (-) on the coin leg over the trade."""
    start = int(trade.opened.timestamp() * 1000)
    end = int(trade.closed.timestamp() * 1000)
    records = hyperliquid.get_funding_history(coin, start, end)
    # Positive rate: longs pay. The coin leg is long for a "long" trade.
    return sum(-trade.sign * float(r["fundingRate"]) * trade.coin_entry for r in records)


def trade_row(trade: Trade, params: Params, funding: float) -> dict:
    net = trade.net(params) + funding
    return {
        "direction": trade.direction,
        "opened": trade.opened.isoformat(timespec="seconds"),
        "closed": trade.closed.isoformat(timespec="seconds"),
        "exit_reason": trade.exit_reason,
        "target_gap": round(trade.target_gap, 4),
        "entry_gap": round(trade.entry_gap, 4),
        "exit_gap": round(trade.exit_gap, 4),
        "coin_entry": trade.coin_entry,
        "hedge_entry": trade.hedge_entry,
        "coin_exit": trade.coin_exit,
        "hedge_exit": trade.hedge_exit,
        "gross": round(trade.gross(), 4),
        "fees": round(trade.fees(params), 4),
        "swap": round(trade.swap(params), 4),
        "funding": round(funding, 4),
        "net": round(net, 4),
    }


class TickRecorder:
    """Writes one row per top-of-book change to data/ticks/<pair>-<UTC date>.csv."""

    def __init__(self, pair: str):
        self.pair, self.rows, self.last = pair, [], None
        self.flushed = time.monotonic()

    def record(self, now: datetime, coin: Quote, hedge: Quote) -> None:
        key = (coin.bid, coin.ask, hedge.bid, hedge.ask)
        if key != self.last:
            self.rows.append((now.isoformat(timespec="milliseconds"), *key))
            self.last = key
        if time.monotonic() - self.flushed >= FLUSH_EVERY_S:
            self.flush()

    def flush(self) -> None:
        self.flushed = time.monotonic()
        if not self.rows:
            return
        by_day: dict[str, list] = {}
        for row in self.rows:
            by_day.setdefault(row[0][:10], []).append(row)
        TICKS.mkdir(parents=True, exist_ok=True)
        for day, rows in by_day.items():
            path = TICKS / f"{self.pair}-{day}.csv"
            is_new = not path.exists()
            with path.open("a", newline="") as f:
                writer = csv.writer(f)
                if is_new:
                    writer.writerow(["timestamp", "coin_bid", "coin_ask", "hedge_bid", "hedge_ask"])
                writer.writerows(rows)
        self.rows = []


def quote_from(feed: dict, now: datetime, mono: float) -> Quote | None:
    if feed["bid"] is None:
        return None
    return Quote(feed["bid"], feed["ask"], now - timedelta(seconds=mono - feed["at"]))


async def run(args: argparse.Namespace, strat: Strategy) -> None:
    async with ctrader.connect() as (client, account_id):
        symbol_id = await ctrader.find_symbol_id(client, account_id, args.ct)
        swap_long, swap_short = await read_swap(client, account_id, symbol_id)
        strat.params = replace(strat.params, swap_long=swap_long, swap_short=swap_short)
        print(f"cTrader swap per unit per night: long {swap_long:+.4f}, short {swap_short:+.4f}")

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
        hl = {"bid": None, "ask": None, "at": 0.0}
        async with anyio.create_task_group() as tg:
            tg.start_soon(hyperliquid_ws.stream_top_of_book, args.hl, hl)
            await loop(args, strat, hl, ct)


async def loop(args: argparse.Namespace, strat: Strategy, hl: dict, ct: dict) -> None:
    ticks = TickRecorder(f"{args.hl}-{args.ct}".lower())
    last_print = last_save = 0.0
    print(f"paper trading {args.hl} vs {args.ct}, entry ±{strat.params.entry:g}. Ctrl+C to stop.\n")
    try:
        while True:
            now, mono = datetime.now(timezone.utc), time.monotonic()
            coin, hedge = quote_from(hl, now, mono), quote_from(ct, now, mono)
            if coin is None or hedge is None:
                if mono - last_print >= args.print_every:
                    missing = [n for n, q in ((args.hl, coin), (args.ct, hedge)) if q is None]
                    print(f"{now:%H:%M:%S}  waiting for first quote from {', '.join(missing)}")
                    last_print = mono
                await anyio.sleep(args.interval)
                continue

            ticks.record(now, coin, hedge)
            events = strat.step(now, coin, hedge)
            for ev in events:
                append_csv(EVENTS, {"time": ev.at.isoformat(timespec="seconds"), "event": ev.name,
                                    "detail": ev.detail})
                print(f"  ** {ev.name}: {ev.detail}")
                if ev.trade is not None:
                    funding = await anyio.to_thread.run_sync(coin_funding, ev.trade, args.hl)
                    append_csv(TRADES, trade_row(ev.trade, strat.params, funding))
            if events or mono - last_save >= SAVE_EVERY_S:
                save_strategy(strat)
                last_save = mono

            if mono - last_print >= args.print_every:
                normal = strat.normal(now)
                normal_txt = f"{normal:+.2f}" if normal is not None else "n/a"
                print(f"{now:%H:%M:%S}  gap {coin.mid - hedge.mid:+.2f}  normal {normal_txt}  "
                      f"[{strat.state}]  quote age {args.hl} {(now - coin.at).total_seconds():.1f}s, "
                      f"{args.ct} {(now - hedge.at).total_seconds():.1f}s")
                last_print = mono
            await anyio.sleep(args.interval)
    finally:
        ticks.flush()
        save_strategy(strat)


def main() -> None:
    args = parse_args()
    params = Params()
    print("loading recent history for the normal gap...")
    strat = load_strategy(params, seed_samples(args.hl, args.ct, params.window_h))
    print(f"{len(strat.samples)} history samples loaded")
    try:
        anyio.run(run, args, strat)
    except KeyboardInterrupt:
        pass
    s = strat.stats
    print(f"\nstopped [{strat.state}]. entry orders {s['placed']}, filled {s['filled']}, "
          f"missed {s['missed']}, cancelled {s['cancelled']}; closed {s['closed']}, net {s['net']:+.2f}")


if __name__ == "__main__":
    main()
