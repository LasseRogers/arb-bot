"""How often the cTrader gold spread is above a limit, time-weighted, from recorded ticks."""
import glob
import sys

import pandas as pd

LIMIT = float(sys.argv[1]) if len(sys.argv) > 1 else 0.30
MAX_HOLD_S = 60  # a quote older than this is a closure, not a spread

frames = []
for path in glob.glob("data/**/*.csv", recursive=True):
    head = pd.read_csv(path, nrows=0).columns
    if "hedge_bid" in head:  # one row per step, both venues
        df = pd.read_csv(path, usecols=["timestamp", "hedge_bid", "hedge_ask"])
        df.columns = ["timestamp", "bid", "ask"]
    elif "venue" in head:
        df = pd.read_csv(path)
        df = df[df.venue == "ctrader"][["timestamp", "bid", "ask"]]
    else:
        continue
    frames.append(df)

df = pd.concat(frames)
df["timestamp"] = pd.to_datetime(df.timestamp, utc=True, format="ISO8601")
df = df.sort_values("timestamp").drop_duplicates().reset_index(drop=True)
df["spread"] = df.ask - df.bid
df["held"] = df.timestamp.diff().shift(-1).dt.total_seconds().clip(upper=MAX_HOLD_S).fillna(0)
df = df[df.held > 0]

total = df.held.sum()
print(f"{len(df)} cTrader quotes, {total / 3600:.1f} hours, "
      f"{df.timestamp.min():%Y-%m-%d %H:%M} to {df.timestamp.max():%Y-%m-%d %H:%M} UTC")
med = (df.sort_values("spread").held.cumsum() >= total / 2).idxmax()
print(f"median spread {df.loc[med, 'spread']:.2f}")
for q in (0.9, 0.99):
    print(f"p{int(q * 100)} spread (by quote count) {df.spread.quantile(q):.2f}")
print(f"time above {LIMIT:.2f}: {df[df.spread > LIMIT].held.sum() / total:.1%}")
hourly = df.assign(hour=df.timestamp.dt.hour, over=(df.spread > LIMIT) * df.held)
by_hour = hourly.groupby("hour")[["over", "held"]].sum()
print("hours (UTC) above limit >10% of the time:",
      ", ".join(f"{h}: {r.over / r.held:.0%}" for h, r in by_hour.iterrows() if r.over / r.held > 0.1) or "none")
