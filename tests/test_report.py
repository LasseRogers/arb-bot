import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from arbbot.gap import report
from arbbot.gap.session import Schedule

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)  # Friday, gold open


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ticks = tmp_path / "data/ticks/paxg-xauusd"
    paper = tmp_path / "data/paper"
    ticks.mkdir(parents=True)
    paper.mkdir(parents=True)
    rows = ["timestamp,venue,bid,ask"]
    for i in range(0, 24 * 60, 1):
        t = NOW - timedelta(minutes=24 * 60 - i)
        rows.append(f"{t.isoformat(timespec='milliseconds')},hyperliquid,4000.0,4000.1")
        rows.append(f"{t.isoformat(timespec='milliseconds')},ctrader,4010.0,4010.1")
    (ticks / "2026-11-13.csv").write_text("\n".join(rows) + "\n")
    sched = Schedule.standard_fx(date(2026, 11, 1), date(2026, 11, 30))
    (ticks / "schedule-2026-11-13.json").write_text(json.dumps(sched.to_dict()))
    (paper / "gold_events.csv").write_text(
        "time,event,detail\n2026-11-10T14:28:34+00:00,start,x\n"
        f"{(NOW - timedelta(hours=2)).isoformat(timespec='seconds')},entry order,y\n")
    (paper / "gold_trades.csv").write_text(
        "closed,direction,exit_reason,net,net_before_funding,entry_slippage\n"
        f"{(NOW - timedelta(hours=3)).isoformat(timespec='seconds')},long,normal,-2.15,-1.90,-0.15\n"
        "2026-11-10T20:57:09+00:00,short,normal,,-0.91,0.02\n")
    (paper / "gold_state.json").write_text(json.dumps(
        {"state": "flat", "trade": None, "stats": {"requests": 2311, "volume": 16128.0, "budget_peak": 59.0}}))
    os.utime(paper / "gold_state.json", (NOW.timestamp(), NOW.timestamp()))
    monkeypatch.setattr(report, "bot_running", lambda: True)
    return tmp_path


def test_daily_report(run_dir):
    text = report.daily(NOW)
    assert "2 of 50 trades" in text
    assert "(1 with funding pending)" in text
    assert "= 14% (≤ 50%)" in text
    assert "Uptime 24 h: 100.0%" in text


def test_health_alerts_once_and_on_recovery(run_dir, monkeypatch):
    sent = []
    monkeypatch.setattr(report, "send", lambda text, dry_run=False: sent.append(text))
    report.health(NOW, False)
    assert sent == []  # all fine, quiet
    monkeypatch.setattr(report, "bot_running", lambda: False)
    report.health(NOW, False)
    report.health(NOW + timedelta(minutes=15), False)
    assert sent == ["Gold bot: bot not running"]  # once, not every 15 minutes
    monkeypatch.setattr(report, "bot_running", lambda: True)
    report.health(NOW, False)
    assert sent[-1] == "Gold bot: recovered, all ok"


def test_stale_ctrader_only_matters_while_gold_is_open(run_dir):
    saturday = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    assert not any("cTrader" in p for p in report.problems(saturday))
    assert any("cTrader" in p for p in report.problems(NOW + timedelta(minutes=30)))


@pytest.mark.parametrize("cmdline, match", [
    ("python -m arbbot.gap.live", True),
    ("/home/lasr/projects/arb-bot/venv/bin/python -m arbbot.gap.live", True),
    ("/usr/bin/python3.13 -m arbbot.gap.live", True),
    ("tmux new-session -d -s gold -c /x source venv/bin/activate && python -m arbbot.gap.live; exec bash", False),
    ("python -m arbbot.gap.report health", False),
])
def test_bot_pattern(cmdline, match):
    import re
    assert bool(re.search(report.BOT_PATTERN, cmdline)) is match
