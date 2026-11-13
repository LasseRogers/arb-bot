"""Telegram reports for the gold paper run.

  daily    progress against docs/criteria.md, the last 24 hours, the open trade, budget
  health   silent unless something is wrong: bot not running, quotes stale while the
           market is open, state not saved. Alerts once per new problem, and once more
           when it clears.
  test     send a test message

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env. Run from the repo root.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from arbbot.gap.session import Schedule

PAPER = Path("data/paper")
EVENTS = PAPER / "gold_events.csv"
TRADES = PAPER / "gold_trades.csv"
STATE = PAPER / "gold_state.json"
HEALTH = PAPER / "health.json"
TICKS = Path("data/ticks/paxg-xauusd")
TARGET_TRADES, TARGET_DAYS = 50, 21
STALE = timedelta(minutes=5)
STATE_STALE = timedelta(minutes=10)
AFTER_OPEN = timedelta(minutes=20)  # cTrader needs a moment after the reopen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["daily", "health", "test"])
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    return parser.parse_args()


def send(text: str, dry_run: bool = False) -> None:
    if dry_run:
        print(text)
        return
    load_dotenv()
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=20)
    r.raise_for_status()


# --- what the files say ------------------------------------------------------------

BOT_PATTERN = r"(^|/)python[0-9.]* -m arbbot\.gap\.live"  # by hand or as a service, not tmux's own line


def bot_running() -> bool:
    return subprocess.run(["pgrep", "-f", BOT_PATTERN], capture_output=True).returncode == 0


def last_quotes() -> dict[str, datetime]:
    """Receive time of the newest quote per venue, from the newest tick file."""
    files = sorted(TICKS.glob("????-??-??.csv"))
    out: dict[str, datetime] = {}
    for path in reversed(files[-2:]):
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode(errors="ignore").splitlines()[1:]
        for line in reversed(lines):
            parts = line.split(",")
            if len(parts) == 4 and parts[1] not in out:
                try:
                    out[parts[1]] = datetime.fromisoformat(parts[0])
                except ValueError:
                    continue
        if len(out) == 2:
            break
    return out


def schedule() -> Schedule | None:
    files = sorted(TICKS.glob("schedule-*.json"))
    return Schedule.from_dict(json.loads(files[-1].read_text())) if files else None


def problems(now: datetime) -> list[str]:
    if not bot_running():
        return ["bot not running"]  # stale quotes and state follow from it
    found = []
    if STATE.exists():
        saved = datetime.fromtimestamp(STATE.stat().st_mtime, tz=timezone.utc)
        if now - saved > STATE_STALE:
            found.append(f"state not saved for {(now - saved).total_seconds() / 60:.0f} min")
    quotes = last_quotes()
    hl = quotes.get("hyperliquid")
    if hl is None or now - hl > STALE:
        found.append("no Hyperliquid quotes" + (f" for {(now - hl).total_seconds() / 60:.0f} min" if hl else ""))
    sched = schedule()
    if sched is not None:
        i = sched._index(now)
        settled = i is not None and now - sched.sessions[i][0] > AFTER_OPEN
        ct = quotes.get("ctrader")
        if settled and (ct is None or now - ct > STALE):
            found.append("no cTrader quotes while gold is open" +
                         (f" for {(now - ct).total_seconds() / 60:.0f} min" if ct else ""))
    return found


def uptime(now: datetime, hours: int = 24) -> float | None:
    """Share of minutes with a Hyperliquid quote: the coin trades around the clock."""
    start = now - timedelta(hours=hours)
    frames = []
    for path in sorted(TICKS.glob("????-??-??.csv"))[-2:]:
        df = pd.read_csv(path, usecols=["timestamp", "venue"])
        frames.append(df[df.venue == "hyperliquid"].timestamp)
    if not frames:
        return None
    ts = pd.to_datetime(pd.concat(frames), utc=True, format="ISO8601")
    minutes = ts[(ts >= start) & (ts < now)].dt.floor("min").nunique()
    return minutes / (hours * 60)


# --- messages ------------------------------------------------------------------------

def daily(now: datetime) -> str:
    events = pd.read_csv(EVENTS) if EVENTS.exists() else pd.DataFrame(columns=["time", "event", "detail"])
    events["when"] = pd.to_datetime(events.time, utc=True, format="ISO8601")
    starts = events[events.event == "start"]
    began = starts.when.min() if len(starts) else now
    day = events[events.when >= now - timedelta(hours=24)]
    trades = pd.read_csv(TRADES) if TRADES.exists() else pd.DataFrame(
        columns=["closed", "direction", "exit_reason", "net", "net_before_funding", "entry_slippage"])
    trades_day = trades[pd.to_datetime(trades.closed, utc=True, format="ISO8601") >= now - timedelta(hours=24)] \
        if len(trades) else trades
    state = json.loads(STATE.read_text()) if STATE.exists() else {"state": "unknown", "stats": {}, "trade": None}
    stats = state.get("stats", {})
    days = (now - began).total_seconds() / 86_400
    counts = day.event.value_counts()
    net_all = pd.to_numeric(trades.net, errors="coerce")
    pending = int(net_all.isna().sum())

    lines = [
        f"Gold paper run, {now:%a %d %b %H:%M} UTC",
        f"Progress: day {days:.1f} of {TARGET_DAYS}, {len(trades)} of {TARGET_TRADES} trades",
        "",
        "Last 24 h:",
        f"  entry orders {counts.get('entry order', 0)}, filled {counts.get('entry filled', 0)}, "
        f"cancelled {counts.get('entry cancelled', 0)}, missed {counts.get('entry missed', 0)}",
        f"  closed {len(trades_day)}, net {pd.to_numeric(trades_day.net, errors='coerce').sum():+.2f}",
    ]
    for t in trades_day.itertuples():
        lines.append(f"    {t.direction} ({t.exit_reason}) net {float(t.net_before_funding):+.2f} before funding")
    lines += [
        "",
        f"Since start: net {net_all.sum():+.2f} over {len(trades)} trades"
        + (f" ({pending} with funding pending)" if pending else ""),
    ]
    if len(trades):
        lines.append(f"  mean entry slippage {pd.to_numeric(trades.entry_slippage).mean():+.2f} "
                     f"(criteria: ≤ 0.75)")
    trade = state.get("trade")
    if trade:
        opened = datetime.fromisoformat(trade["opened"]["__dt__"])
        entry_gap = trade["coin_entry"] - trade["hedge_entry"]
        lines.append(f"Open: {trade['direction']} since {opened:%a %H:%M} UTC, entry gap {entry_gap:+.2f}")
    else:
        lines.append(f"Open: none ({state.get('state')})")
    req, vol, peak = stats.get("requests", 0), stats.get("volume", 0.0), stats.get("budget_peak", 0.0)
    ratio = f"{req / vol:.0%}" if vol else "n/a"
    lines.append(f"Requests {req} / volume ${vol:,.0f} = {ratio} (≤ 50%), peak {peak:,.0f} (≤ 5,000)")
    up = uptime(now)
    if up is not None:
        lines.append(f"Uptime 24 h: {up:.1%} (≥ 95% over the run)")
    issues = problems(now)
    lines.append("Health: " + ("; ".join(issues) if issues else "ok"))
    return "\n".join(lines)


def health(now: datetime, dry_run: bool) -> None:
    current = problems(now)
    previous = json.loads(HEALTH.read_text()).get("problems", []) if HEALTH.exists() else []
    new = [p for p in current if p.split(" for ")[0] not in {q.split(" for ")[0] for q in previous}]
    cleared = not current and previous
    if new:
        send("Gold bot: " + "; ".join(current), dry_run)
    elif cleared:
        send("Gold bot: recovered, all ok", dry_run)
    HEALTH.write_text(json.dumps({"checked": now.isoformat(), "problems": current}))


def main() -> None:
    args = parse_args()
    now = datetime.now(timezone.utc)
    if args.mode == "test":
        send(f"Gold bot reports are set up ({now:%Y-%m-%d %H:%M} UTC).", args.dry_run)
    elif args.mode == "daily":
        send(daily(now), args.dry_run)
    else:
        health(now, args.dry_run)


if __name__ == "__main__":
    main()
