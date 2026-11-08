"""For consistent funding earners on Hyperliquid's main dex, check where the coin
itself can be bought (spot), and how many days of funding repay the fees.

Cash-and-carry: buy spot, short the perp, collect funding. Fees are base-tier
maker rates, round trip (enter and exit) on both legs.
"""

import argparse
from pathlib import Path

import pandas as pd

from arbbot.venues import binance
from arbbot.venues import hyperliquid

PERP_MAKER = 0.00015
HL_SPOT_MAKER = 0.0004
BINANCE_SPOT = 0.001


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, default=Path("data/funding_scan.csv"))
    parser.add_argument("--min-consistency", type=float, default=0.8)
    parser.add_argument("--top", type=int, default=12)
    return parser.parse_args()


def breakeven_days(round_trip_cost: float, yearly_rate: float) -> float:
    return round_trip_cost / (yearly_rate / 365)


def main() -> None:
    args = parse_args()
    scan = pd.read_csv(args.scan)
    picks = (
        scan[(scan["dex"] == "main")
             & (scan["same_direction"] >= args.min_consistency)
             & (scan["avg_yearly"] > 0)]
        .sort_values("avg_yearly", ascending=False)
        .head(args.top)
    )
    hl_spot = hyperliquid.get_spot_markets()

    hl_cost = 2 * (HL_SPOT_MAKER + PERP_MAKER)
    bn_cost = 2 * (BINANCE_SPOT + PERP_MAKER)
    print(f"round-trip fees: HL spot route {hl_cost:.2%}, Binance spot route {bn_cost:.2%}\n")
    print(f"{'coin':<10}{'rent/yr':>9}  {'HL spot':<22}{'Binance spot':<22}{'break-even days':>16}")

    for r in picks.itertuples():
        hl = next((hl_spot[n] | {"name": n} for n in (r.coin, f"U{r.coin}") if n in hl_spot), None)
        bn_vol = binance.get_24h_quote_volume(f"{r.coin}USDT")

        hl_txt = f"{hl['name']} ${hl['volume']:,.0f}" if hl else "-"
        bn_txt = f"${bn_vol:,.0f}" if bn_vol else "-"
        days = [breakeven_days(c, r.avg_yearly) for c, ok in ((hl_cost, hl), (bn_cost, bn_vol)) if ok]
        days_txt = f"{min(days):.1f}" if days else "-"
        print(f"{r.coin:<10}{r.avg_yearly:>+9.0%}  {hl_txt:<22}{bn_txt:<22}{days_txt:>16}")


if __name__ == "__main__":
    main()
