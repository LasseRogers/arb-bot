"""Rank Hyperliquid markets by funding, then check how consistent it has been.

Positive funding: longs pay shorts, so a short collects it. Negative: the reverse.
"""

import argparse
import time
from pathlib import Path

import pandas as pd

import hyperliquid

HOURS_PER_YEAR = 24 * 365


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-volume", type=float, default=1_000_000, help="min 24h volume in USD")
    parser.add_argument("--top", type=int, default=15, help="markets to check history for")
    parser.add_argument("--days", type=float, default=14, help="history to check")
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
    rates = pd.Series([float(r["fundingRate"]) for r in hyperliquid.get_funding_history(coin, start, end)])
    if rates.empty:
        return float("nan"), float("nan")
    avg = rates.mean()
    same = (rates > 0).mean() if avg > 0 else (rates < 0).mean()
    return avg * HOURS_PER_YEAR, same


def main() -> None:
    args = parse_args()
    df = current_markets(args.min_volume)
    top = df.reindex(df["now_yearly"].abs().sort_values(ascending=False).index).head(args.top)

    stats = [history_stats(c, args.days) for c in top["coin"]]
    top = top.assign(
        avg_yearly=[s[0] for s in stats],
        same_direction=[s[1] for s in stats],
        collect_by=["promise-sell" if s[0] > 0 else "buy" for s in stats],
    ).sort_values("avg_yearly", key=abs, ascending=False)

    out = Path("data/funding_scan.csv")
    out.parent.mkdir(exist_ok=True)
    top.to_csv(out, index=False)

    print(f"{len(df)} markets with >= ${args.min_volume:,.0f} daily volume; top {len(top)} by current rate:\n")
    print(f"{'market':<16}{'dex':<6}{'now/yr':>9}{f'{args.days:g}d avg/yr':>13}"
          f"{'same dir':>10}{'collect by':>14}{'daily vol':>14}")
    for r in top.itertuples():
        print(f"{r.coin:<16}{r.dex:<6}{r.now_yearly:>+9.0%}{r.avg_yearly:>+13.0%}"
              f"{r.same_direction:>10.0%}{r.collect_by:>14}{r.volume:>14,.0f}")
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
