"""Paper-trade a funding carry on Hyperliquid: spot long + equal perp short.

  open    record entry prices, size and margin (no real orders)
  status  value the position now, using live prices and the funding paid since entry

State lives in data/paper/<coin>.json, so nothing needs to keep running.
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import hyperliquid
from recorder import slug

SPOT_MAKER = 0.0004
PERP_MAKER = 0.00015
LOW_MARGIN_WARNING = 0.3


def state_path(coin: str) -> Path:
    return Path("data/paper") / f"{slug(coin)}.json"


def live_prices(coin: str) -> tuple[float, float]:
    spot = hyperliquid.get_spot_markets()
    if coin not in spot:
        raise SystemExit(f"{coin} has no USDC spot market on Hyperliquid")
    return spot[coin]["mid"], hyperliquid.get_mid_price(coin)


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
    path = state_path(coin)
    if not path.exists():
        raise SystemExit(f"no open paper trade for {coin}: run 'open' first")
    s = json.loads(path.read_text())
    spot_px, perp_px = live_prices(coin)
    now_ms = int(time.time() * 1000)

    # Short perp: positive funding is received. Hyperliquid pays on position value at the time.
    records = hyperliquid.get_funding_history(coin, s["opened_ms"], now_ms)
    funding = sum(s["units"] * float(r["fundingRate"]) * perp_px for r in records)

    spot_pnl = s["units"] * (spot_px - s["spot_entry"])
    short_pnl = s["units"] * (s["perp_entry"] - perp_px)
    exit_fees = s["units"] * (spot_px * SPOT_MAKER + perp_px * PERP_MAKER)
    margin_left = s["margin"] + short_pnl + funding
    total = spot_pnl + short_pnl + funding - s["entry_fees"] - exit_fees

    opened = datetime.fromtimestamp(s["opened_ms"] / 1000, timezone.utc)
    hours = (now_ms - s["opened_ms"]) / 3_600_000
    print(f"{coin} paper carry, open {hours:.1f} hours (since {opened:%Y-%m-%d %H:%M} UTC)")
    print(f"  price now           spot {spot_px:,.4f}   perp {perp_px:,.4f}")
    print(f"  coins bought        {spot_pnl:+.2f}")
    print(f"  promise-sold        {short_pnl:+.2f}")
    print(f"  rent collected      {funding:+.2f}  ({len(records)} hourly payments)")
    print(f"  fees (in + out)     {-(s['entry_fees'] + exit_fees):+.2f}")
    print(f"  total if closed now {total:+.2f}")
    print(f"  safety deposit left ${margin_left:,.2f} of ${s['margin']:,.2f}")
    if margin_left < s["margin"] * LOW_MARGIN_WARNING:
        print("  WARNING: safety deposit is low; move money from the spot side")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    op = sub.add_parser("open", help="start a paper trade")
    op.add_argument("coin")
    op.add_argument("--capital", type=float, default=1000, help="USD of spot to buy")
    op.add_argument("--margin", type=float, default=300, help="USD safety deposit on the short")
    st = sub.add_parser("status", help="value the open paper trade")
    st.add_argument("coin")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "open":
        cmd_open(args.coin, args.capital, args.margin)
    else:
        cmd_status(args.coin)


if __name__ == "__main__":
    main()
