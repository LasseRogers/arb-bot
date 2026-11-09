"""Paper-trade the gap strategy on live quotes. No real orders are placed.

Trading rules live in arbbot.gap.strategy, shared with the replay backtest. This
module only wires it to live data:
  - streams the coin from Hyperliquid and the hedge from cTrader
  - builds the hedge venue's session schedule from cTrader, refreshed daily
  - seeds the rolling normal from recent 5-minute history
  - records every quote update per venue, plus a daily schedule snapshot, to data/ticks/
  - persists strategy state to data/paper/gold_state.json, so a restart keeps open trades
  - logs events and closed trades (with Hyperliquid funding) to data/paper/
"""

import argparse
import csv
import json
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anyio
import anyio.to_thread
import pandas as pd
from ctrader_api_client.events.types import SpotEvent

from arbbot.gap import history
from arbbot.gap.session import Schedule, from_trading_hours
from arbbot.gap.strategy import Params, Quote, Strategy, Trade
from arbbot.venues import ctrader, hyperliquid, hyperliquid_ws

PAPER = Path("data/paper")
STATE = PAPER / "gold_state.json"
TRADES = PAPER / "gold_trades.csv"
EVENTS = PAPER / "gold_events.csv"
TICKS = Path("data/ticks")
SAVE_EVERY_S = 60
FLUSH_EVERY_S = 5
SCHEDULE_CHECK_S = 600
SCHEDULE_PAST, SCHEDULE_AHEAD = timedelta(days=7), timedelta(days=14)
FUNDING_TRIES = 3


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


# --- hedge venue ---------------------------------------------------------------

def swap_per_unit(symbol) -> tuple[float, float]:
    """cTrader swap in USD per unit per rollover, from the symbol's pip-based rates."""
    pip = 10 ** -symbol.pip_position
    return float(symbol.swap.long) * pip, float(symbol.swap.short) * pip


def build_schedule(symbol, today: date) -> Schedule:
    return from_trading_hours(symbol.trading_hours, symbol.swap.triple_day,
                              today - SCHEDULE_PAST, today + SCHEDULE_AHEAD)


def snapshot_schedule(schedule: Schedule, pair: str, today: date) -> None:
    """Keep the schedule that was live each day, so a replay uses the same one."""
    path = TICKS / pair / f"schedule-{today.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schedule.to_dict(), indent=1))


async def keep_schedule_fresh(client, account_id: int, symbol_id: int, strat: Strategy,
                              pair: str, built: date) -> None:
    while True:
        await anyio.sleep(SCHEDULE_CHECK_S)
        today = datetime.now(timezone.utc).date()
        if today == built:
            continue
        try:
            symbol = await client.symbols.get_by_id(account_id, symbol_id)
            strat.schedule = build_schedule(symbol, today)
        except Exception as e:  # keep trading on yesterday's schedule, retry next check
            print(f"  schedule refresh failed ({e!r}), keeping the old one")
            continue
        snapshot_schedule(strat.schedule, pair, today)
        built = today
        print(f"  schedule refreshed for {today}")


# --- funding -------------------------------------------------------------------

def coin_funding(coin: str, sign: int, notional: float, opened: datetime, closed: datetime) -> float:
    """Hyperliquid funding received (+) or paid (-) on the coin leg over the trade.

    Positive rate: longs pay. The coin leg is long for a "long" trade (sign +1).
    """
    records = hyperliquid.get_funding_history(coin, int(opened.timestamp() * 1000),
                                              int(closed.timestamp() * 1000))
    return sum(-sign * float(r["fundingRate"]) * notional for r in records)


def funding_or_none(coin: str, trade: Trade) -> float | None:
    for attempt in range(FUNDING_TRIES):
        try:
            return coin_funding(coin, trade.sign, trade.coin_entry, trade.opened, trade.closed)
        except Exception as e:
            print(f"  funding lookup failed ({e!r}), try {attempt + 1}/{FUNDING_TRIES}")
            time.sleep(2 ** attempt)
    return None


def backfill_funding(coin: str) -> None:
    """Fill in funding for trades logged while the lookup was failing."""
    if not TRADES.exists():
        return
    with TRADES.open(newline="") as f:
        rows = list(csv.DictReader(f))
    pending = [r for r in rows if r["funding"] == ""]
    if not pending:
        return
    for r in pending:
        try:
            funding = coin_funding(coin, 1 if r["direction"] == "long" else -1, float(r["coin_entry"]),
                                   datetime.fromisoformat(r["opened"]), datetime.fromisoformat(r["closed"]))
        except Exception as e:
            print(f"funding backfill failed ({e!r}), will retry on next start")
            return
        r["funding"] = round(funding, 4)
        r["net"] = round(float(r["net_before_funding"]) + funding, 4)
    tmp = TRADES.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(TRADES)
    print(f"funding backfilled for {len(pending)} trade(s)")


def trade_row(trade: Trade, params: Params, funding: float | None) -> dict:
    before = trade.net(params)
    return {
        "direction": trade.direction,
        "opened": trade.opened.isoformat(timespec="seconds"),
        "closed": trade.closed.isoformat(timespec="seconds"),
        "exit_reason": trade.exit_reason,
        "carried": trade.carried,
        "target_gap": round(trade.target_gap, 4),
        "entry_gap": round(trade.entry_gap, 4),
        "entry_slippage": round(trade.entry_slippage, 4),
        "exit_target": round(trade.exit_target, 4),
        "exit_gap": round(trade.exit_gap, 4),
        "exit_slippage": round(trade.exit_slippage, 4),
        "coin_entry": trade.coin_entry,
        "hedge_entry": trade.hedge_entry,
        "coin_exit": trade.coin_exit,
        "hedge_exit": trade.hedge_exit,
        "gross": round(trade.gross(), 4),
        "fees": round(trade.fees(params), 4),
        "rollovers": trade.rollovers,
        "swap": round(trade.swap(params), 4),
        "net_before_funding": round(before, 4),
        "funding": "" if funding is None else round(funding, 4),
        "net": "" if funding is None else round(before + funding, 4),
    }


# --- quotes --------------------------------------------------------------------

class TickRecorder:
    """One row per quote update per venue, to data/ticks/<pair>/<UTC date>.csv."""

    def __init__(self, pair: str):
        self.dir = TICKS / pair
        self.rows: list[tuple] = []
        self.flushed = time.monotonic()

    def record(self, venue: str, bid: float, ask: float) -> None:
        now = datetime.now(timezone.utc)
        self.rows.append((now.isoformat(timespec="milliseconds"), venue, bid, ask))

    def maybe_flush(self) -> None:
        if time.monotonic() - self.flushed >= FLUSH_EVERY_S:
            self.flush()

    def flush(self) -> None:
        self.flushed = time.monotonic()
        if not self.rows:
            return
        rows, self.rows = self.rows, []
        by_day: dict[str, list] = {}
        for row in rows:
            by_day.setdefault(row[0][:10], []).append(row)
        self.dir.mkdir(parents=True, exist_ok=True)
        for day, day_rows in by_day.items():
            path = self.dir / f"{day}.csv"
            is_new = not path.exists()
            with path.open("a", newline="") as f:
                writer = csv.writer(f)
                if is_new:
                    writer.writerow(["timestamp", "venue", "bid", "ask"])
                writer.writerows(day_rows)


def quote_from(feed: dict, now: datetime, mono: float) -> Quote | None:
    if feed["bid"] is None or feed["ask"] is None:
        return None
    return Quote(feed["bid"], feed["ask"], now - timedelta(seconds=mono - feed["at"]))


# --- main ----------------------------------------------------------------------

async def run(args: argparse.Namespace, strat: Strategy) -> None:
    pair = f"{args.hl}-{args.ct}".lower()
    ticks = TickRecorder(pair)
    async with ctrader.connect() as (client, account_id):
        symbol_id = await ctrader.find_symbol_id(client, account_id, args.ct)
        symbol = await client.symbols.get_by_id(account_id, symbol_id)
        swap_long, swap_short = swap_per_unit(symbol)
        strat.params = replace(strat.params, swap_long=swap_long, swap_short=swap_short)
        print(f"cTrader swap per unit per rollover: long {swap_long:+.4f}, short {swap_short:+.4f}")

        today = datetime.now(timezone.utc).date()
        strat.schedule = build_schedule(symbol, today)
        snapshot_schedule(strat.schedule, pair, today)
        now = datetime.now(timezone.utc)
        upcoming = [s for s in strat.schedule.sessions if s[1] > now][:1]
        if upcoming:
            print(f"cTrader session: {upcoming[0][0]:%a %H:%M} to {upcoming[0][1]:%a %H:%M} UTC")

        ct = {"bid": None, "ask": None, "at": 0.0}

        @client.on(SpotEvent, symbol_id=symbol_id)
        async def on_spot(e: SpotEvent) -> None:
            # cTrader only sends the side that changed; keep the other one.
            if e.bid is not None:
                ct["bid"] = float(e.bid)
            if e.ask is not None:
                ct["ask"] = float(e.ask)
            ct["at"] = time.monotonic()
            if ct["bid"] is not None and ct["ask"] is not None:
                ticks.record("ctrader", ct["bid"], ct["ask"])

        await client.market_data.subscribe_spots(account_id, [symbol_id])
        hl = {"bid": None, "ask": None, "at": 0.0}
        async with anyio.create_task_group() as tg:
            tg.start_soon(hyperliquid_ws.stream_top_of_book, args.hl, hl,
                          lambda bid, ask: ticks.record("hyperliquid", bid, ask))
            tg.start_soon(keep_schedule_fresh, client, account_id, symbol_id, strat, pair, today)
            try:
                await loop(args, strat, hl, ct, ticks)
            finally:
                tg.cancel_scope.cancel()


async def loop(args: argparse.Namespace, strat: Strategy, hl: dict, ct: dict, ticks: TickRecorder) -> None:
    last_print = last_save = 0.0
    print(f"paper trading {args.hl} vs {args.ct}, entry ±{strat.params.entry:g}, "
          f"tick {strat.params.tick:g}. Ctrl+C to stop.\n")
    try:
        while True:
            now, mono = datetime.now(timezone.utc), time.monotonic()
            ticks.maybe_flush()
            coin, hedge = quote_from(hl, now, mono), quote_from(ct, now, mono)
            if coin is None or hedge is None:
                if mono - last_print >= args.print_every:
                    missing = [n for n, q in ((args.hl, coin), (args.ct, hedge)) if q is None]
                    print(f"{now:%H:%M:%S}  waiting for first quote from {', '.join(missing)}")
                    last_print = mono
                await anyio.sleep(args.interval)
                continue

            events = strat.step(now, coin, hedge)
            for ev in events:
                append_csv(EVENTS, {"time": ev.at.isoformat(timespec="seconds"), "event": ev.name,
                                    "detail": ev.detail})
                print(f"  ** {ev.name}: {ev.detail}")
                if ev.trade is not None:
                    funding = await anyio.to_thread.run_sync(funding_or_none, args.hl, ev.trade)
                    if funding is None:
                        print("  ** funding pending: filled in on next start")
                    else:
                        strat.stats["net"] += funding
                    append_csv(TRADES, trade_row(ev.trade, strat.params, funding))
            if events or mono - last_save >= SAVE_EVERY_S:
                save_strategy(strat)
                last_save = mono

            if mono - last_print >= args.print_every:
                normal = strat.normal(now)
                normal_txt = f"{normal:+.2f}" if normal is not None else "n/a"
                sched = strat.schedule
                window = ("entries on" if sched.entries_allowed(now)
                          else "quoting only" if sched.quoting_allowed(now) else "closed")
                print(f"{now:%H:%M:%S}  gap {coin.mid - hedge.mid:+.2f}  normal {normal_txt}  "
                      f"[{strat.state}, {window}]  quote age {args.hl} {(now - coin.at).total_seconds():.1f}s, "
                      f"{args.ct} {(now - hedge.at).total_seconds():.1f}s")
                last_print = mono
            await anyio.sleep(args.interval)
    finally:
        ticks.flush()
        save_strategy(strat)


def main() -> None:
    args = parse_args()
    backfill_funding(args.hl)
    mid = hyperliquid.get_mid_price(args.hl)
    params = Params(tick=hyperliquid.price_tick(args.hl, mid))
    print(f"loading history for {args.hl} and {args.ct}...")
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
