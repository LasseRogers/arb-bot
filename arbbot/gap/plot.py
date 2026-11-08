"""Plot prices and the gap from a file written by recorder.py."""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="e.g. data/hl-btc__binance-btcusdt.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    df = pd.read_csv(args.csv, parse_dates=["timestamp"], index_col="timestamp")
    a, b = df.columns[:2]
    normal = df["gap"].median()

    fig, (top, bottom) = plt.subplots(
        2, 1, sharex=True, figsize=(11, 6), height_ratios=[2, 1]
    )
    top.plot(df.index, df[a], label=a, lw=1)
    top.plot(df.index, df[b], label=b, lw=1)
    top.set_ylabel("price")
    top.legend(loc="upper left")

    bottom.plot(df.index, df["gap"], color="tab:purple", lw=1)
    bottom.axhline(normal, color="grey", ls="--", lw=1, label=f"median {normal:+.2f}")
    bottom.set_ylabel("gap")
    bottom.legend(loc="upper left")

    fig.suptitle(args.csv.stem)
    fig.autofmt_xdate()
    fig.tight_layout()

    out = Path("charts") / f"{args.csv.stem}.png"
    out.parent.mkdir(exist_ok=True)
    fig.savefig(out, dpi=120)

    print(f"saved {out}\n")
    print(df["gap"].describe().round(2).to_string())


if __name__ == "__main__":
    main()
