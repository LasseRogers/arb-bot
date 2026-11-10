"""The pinned trading rules, frozen as an event log: any behaviour change fails here."""
import json
from pathlib import Path

from arbbot.gap.strategy import Quote, Strategy
from golden_scenario import run

GOLDEN = Path(__file__).parent / "data" / "golden_events.json"


def test_defaults_reproduce_the_pinned_rules():
    assert [list(e) for e in run(Strategy, Quote)] == json.loads(GOLDEN.read_text())
