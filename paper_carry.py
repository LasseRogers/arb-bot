"""Paper-trade a funding carry on Hyperliquid: spot long + equal perp short.

  open    record entry prices, size and margin (no real orders)
  status  value the position now, using live prices and the funding paid since entry
  close   value it one last time, append the result to data/paper/history.csv

State lives in data/paper/<coin>.json, so nothing needs to keep running.
"""

import argparse
import csv
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import hyperliquid
from recorder import slug

SPOT_MAKER = 0.0004
PERP_MAKER = 0.00015
LOW_MARGIN_WARNING = 0.3
HISTORY = Path("data/paper/history.csv")


@dataclass
class Valuation:
    coin: str
    opened: str
    valued: str
    hours: float
    units: float
    spot_entry: float
    spot_now: float
    perp_entry: float
    perp_now: float
    spot_pnl: float
    short_pnl: float
    funding: float
    fees: float
    total: float
    margin_left: float
    capital: float
    margin: float
    payments: int

    @property
    def yearly_return(self) -> float:
        """Total over all money put in (spot + margin), scaled to a year."""
        if self.hours <= 0:
            return 0.0
        return self.total / (self.capital + self.margin) * (24 * 365) / self.hours


def state_path(coin: str) -> Path:
    return Path("data/paper") / f"{slug(coin)}.json"


def live_prices(coin: str) -> tuple[float, float]:
    spot = hyperliquid.get_spot_markets()
    # Some coins trade spot under a wrapped name, e.g. ZEC as UZEC.
    name = next((n for n in (coin, f"U{coin}") if n in spot), None)
    if name is None:
        raise SystemExit(f"{coin} has no USDC spot market on Hyperliquid")
    return spot[name]["mid"], hyperliquid.get_mid_price(coin)


def load_state(coin: str) -> dict:
    path = state_path(coin)
    if not path.exists():
        raise SystemExit(f"no open paper trade for {coin}: run 'open' first")
    return json.loads(path.read_text())


def value(s: dict) -> Valuation:
    coin = s["coin"]
    spot_px, perp_px = live_prices(coin)
    now_ms = int(time.time() * 1000)

    # Short perp: positive funding is received.
    records = hyperliquid.get_funding_history(coin, s["opened_ms"], now_ms)
    funding = sum(s["units"] * float(r["fundingRate"]) * perp_px for r in records)

    spot_pnl = s["units"] * (spot_px - s["spot_entry"])
    short_pnl = s["units"] * (s["perp_entry"] - perp_px)
    exit_fees = s["units"] * (spot_px * SPOT_MAKER + perp_px * PERP_MAKER)
    fees = s["entry_fees"] + exit_fees

    return Valuation(
        coin=coin,
        opened=datetime.fromtimestamp(s["opened_ms"] / 1000, timezone.utc).isoformat(timespec="minutes"),
        valued=datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat(timespec="minutes"),
        hours=(now_ms - s["opened_ms"]) / 3_600_000,
        units=s["units"],
        spot_entry=s["spot_entry"],
        spot_now=spot_px,
        perp_entry=s["perp_entry"],
        perp_now=perp_px,
        spot_pnl=spot_pnl,
        short_pnl=short_pnl,
        funding=funding,
        fees=fees,
        total=spot_pnl + short_pnl + funding - fees,
        margin_left=s["margin"] + short_pnl + funding,
        capital=s.get("capital", s["units"] * s["spot_entry"]),
        margin=s["margin"],
        payments=len(records),
    )


def report(v: Valuation, heading: str) -> None:
    print(f"{v.coin} paper carry {heading}, {v.hours:.1f} hours since {v.opened} UTC")
    print(f"  price             spot {v.spot_entry:,.4f} -> {v.spot_now:,.4f}"
          f"   perp {v.perp_entry:,.4f} -> {v.perp_now:,.4f}")
    print(f"  coins bought        {v.spot_pnl:+.2f}")
    print(f"  promise-sold        {v.short_pnl:+.2f}")
    print(f"  rent collected      {v.funding:+.2f}  ({v.payments} hourly payments)")
    print(f"  fees (in + out)     {-v.fees:+.2f}")
    yearly = f"{v.yearly_return:+.1%} per year" if v.hours >= 24 else "per-year figure after 24h"
    print(f"  total               {v.total:+.2f}  ({yearly} on ${v.capital + v.margin:,.0f} put in)")
    print(f"  safety deposit left ${v.margin_left:,.2f} of ${v.margin:,.2f}")
    if v.margin_left < v.margin * LOW_MARGIN_WARNING:
        print("  WARNING: safety deposit is low; move money from the spot side")


def cmd_open(coin: str, capital: float, margin: float) -> None:
    path = state_path(coin)
    if path.exists():
        raise SystemExit(f"{path} exists: a paper trade for {coin} is already open")
    spot_px, perp_px = live_prices(coin)
    units = capital / spot_px
    state = {
        "coin": coin,
        "opened_ms": int(time.time() * 1000),
        "units": units,
        "capital": capital,
        "spot_entry": spot_px,
        "perp_entry": perp_px,
        "margin": margin,
        "entry_fees": units * (spot_px * SPOT_MAKER + perp_px * PERP_MAKER),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))
    print(f"opened paper carry: {units:.4f} {coin}")
    print(f"  bought spot at {spot_px:,.4f}, shorted perp at {perp_px:,.4f}")
    print(f"  ${capital:,.2f} in spot, ${margin:,.2f} margin on the short, "
          f"entry fees ${state['entry_fees']:.2f}")


def cmd_status(coin: str) -> None:
    report(value(load_state(coin)), "open")


def cmd_close(coin: str) -> None:
    v = value(load_state(coin))
    report(v, "CLOSED")

    row = asdict(v) | {"yearly_return": v.yearly_return}
    is_new = not HISTORY.exists()
    with HISTORY.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if is_new:
            writer.writeheader()
        writer.writerow(row)
    state_path(coin).unlink()
    print(f"  result added to {HISTORY}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    op = sub.add_parser("open", help="start a paper trade")
    op.add_argument("coin")
    op.add_argument("--capital", type=float, default=1000, help="USD of spot to buy")
    op.add_argument("--margin", type=float, default=300, help="USD safety deposit on the short")
    for name, text in (("status", "value the open paper trade"), ("close", "end it and record the result")):
        sub.add_parser(name, help=text).add_argument("coin")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "open":
        cmd_open(args.coin, args.capital, args.margin)
    elif args.command == "status":
        cmd_status(args.coin)
    else:
        cmd_close(args.coin)


if __name__ == "__main__":
    main()
