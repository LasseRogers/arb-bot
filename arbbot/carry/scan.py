"""Rank Hyperliquid markets by how much, and how reliably, funding has paid.

Positive funding: longs pay shorts, so a short collects it. Negative: the reverse.
Markets are ranked by their average over --days, keeping only those whose
funding pointed the same way at least --min-consistency of the hours.
"""

import argparse
import time
from pathlib import Path

import pandas as pd

from arbbot.venues import hyperliquid

HOURS_PER_YEAR = 24 * 365


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--min-volume", type=float, default=1_000_000, help="min 24h volume in USD")
    parser.add_argument("--days", type=float, default=14, help="history to average over")
    parser.add_argument("--min-consistency", type=float, default=0.8,
                        help="share of hours funding must point the same way")
    parser.add_argument("--top", type=int, default=20, help="rows to print")
    return parser.parse_args()


def current_markets(min_volume: float) -> pd.DataFrame:
    rows = []
    for dex in ["", *hyperliquid.get_perp_dexs()]:
        for m in hyperliquid.get_markets(dex):
            if m["volume"] >= min_volume:
                rows.append({**m, "dex": dex or "main"})
    df = pd.DataFrame(rows)
    df["now_yearly"] = df["funding"] * HOURS_PER_YEAR
    return df


def history_stats(coin: str, days: float) -> tuple[float, float]:
    """(average yearly rate, share of hours with the same sign as the average)."""
    end = int(time.time() * 1000)
    start = end - int(days * 24 * 3_600_000)
    rates = pd.Series(
        [float(r["fundingRate"]) for r in hyperliquid.get_funding_history(coin, start, end)]
    )
    if rates.empty:
        return float("nan"), float("nan")
    avg = rates.mean()
    same = (rates > 0).mean() if avg > 0 else (rates < 0).mean()
    return avg * HOURS_PER_YEAR, same


def main() -> None:
    args = parse_args()
    df = current_markets(args.min_volume)

    stats = []
    for i, coin in enumerate(df["coin"], 1):
        print(f"\rchecking history {i}/{len(df)}", end="", flush=True)
        stats.append(history_stats(coin, args.days))
    print()

    df = df.assign(
        avg_yearly=[s[0] for s in stats],
        same_direction=[s[1] for s in stats],
    )
    df["collect_by"] = df["avg_yearly"].map(lambda v: "promise-sell" if v > 0 else "buy")

    out = Path("data/funding_scan.csv")
    out.parent.mkdir(exist_ok=True)
    df.sort_values("avg_yearly", key=abs, ascending=False).to_csv(out, index=False)

    ranked = (
        df[df["same_direction"] >= args.min_consistency]
        .sort_values("avg_yearly", key=abs, ascending=False)
        .head(args.top)
    )

    print(f"\n{len(df)} markets with >= ${args.min_volume:,.0f} daily volume; "
          f"{(df['same_direction'] >= args.min_consistency).sum()} were consistent "
          f"(>= {args.min_consistency:.0%} same direction). Top {len(ranked)}:\n")
    print(f"{'market':<16}{'dex':<6}{f'{args.days:g}d avg/yr':>13}{'same dir':>10}"
          f"{'collect by':>14}{'now/yr':>9}{'daily vol':>14}")
    for r in ranked.itertuples():
        print(f"{r.coin:<16}{r.dex:<6}{r.avg_yearly:>+13.0%}{r.same_direction:>10.0%}"
              f"{r.collect_by:>14}{r.now_yearly:>+9.0%}{r.volume:>14,.0f}")
    print(f"\nall {len(df)} markets saved to {out}")


if __name__ == "__main__":
    main()
