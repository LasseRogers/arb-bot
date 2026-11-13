"""Gold run uptime: gaps over a minute in Hyperliquid ticks (the coin trades 24/7).

    python scripts/uptime.py [--since 2026-11-10T14:28Z] [--ticks data/ticks/paxg-xauusd]
"""
import argparse
import glob

import pandas as pd

parser = argparse.ArgumentParser()
parser.add_argument("--since", default="2026-11-10T14:28Z")
parser.add_argument("--ticks", default="data/ticks/paxg-xauusd")
parser.add_argument("--gap-min", type=float, default=1.0)
args = parser.parse_args()

df = pd.concat(pd.read_csv(f, usecols=["timestamp", "venue"])
               for f in sorted(glob.glob(f"{args.ticks}/????-??-??.csv")))
ts = pd.to_datetime(df[df.venue == "hyperliquid"].timestamp, utc=True, format="ISO8601")
ts = ts[ts >= pd.Timestamp(args.since)].sort_values().reset_index(drop=True)
gaps = ts.diff()
big = gaps[gaps > pd.Timedelta(minutes=args.gap_min)]
for i, g in big.items():
    print(f"{ts[i] - g:%a %Y-%m-%d %H:%M} -> {ts[i]:%H:%M} UTC  {g.total_seconds() / 60:7.1f} min")
span, down = ts.iloc[-1] - ts.iloc[0], big.sum()
print(f"down {down.total_seconds() / 3600:.2f} h of {span.total_seconds() / 3600:.1f} h, "
      f"uptime {1 - down / span:.2%}")
