"""Write tests/data/golden_<path>.json from whatever arbbot is on the path.

Run it against the pinned commit, never the working tree:
    git worktree add /tmp/pinned <commit>
    PYTHONPATH=/tmp/pinned python tests/make_golden.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(1, str(Path(__file__).parent))
from arbbot.gap import strategy  # noqa: E402
from golden_scenario import PATHS, run  # noqa: E402

print(f"using {strategy.__file__}")
out = Path(__file__).parent / "data"
out.mkdir(exist_ok=True)
for name in PATHS:
    events = run(strategy.Strategy, strategy.Quote, name)
    (out / f"golden_{name}.json").write_text(json.dumps(events, indent=0))
    names = [e[1] for e in events if e[0] != "stats"]
    print(f"{name}: {len(events)} events, " + ", ".join(f"{n} {names.count(n)}" for n in sorted(set(names))))
