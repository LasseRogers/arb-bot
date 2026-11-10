"""The pinned trading rules, frozen as event logs: any behaviour change fails here.

The files are generated at the pinned commit by tests/make_golden.py.
"""
import json
from pathlib import Path

import pytest

from arbbot.gap.strategy import Quote, Strategy
from golden_scenario import PATHS, run

DATA = Path(__file__).parent / "data"


@pytest.mark.parametrize("path", list(PATHS))
def test_defaults_reproduce_the_pinned_rules(path):
    assert [list(e) for e in run(Strategy, Quote, path)] == json.loads((DATA / f"golden_{path}.json").read_text())
