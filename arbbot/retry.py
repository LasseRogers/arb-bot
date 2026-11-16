"""Retry a blocking call through a venue outage."""

import time

FIRST_WAIT_S, MAX_WAIT_S = 5, 300


def until_ok(what: str, fn, *args):
    """Call fn(*args) until it succeeds, waiting 5 s, 10 s, … up to 5 min between tries."""
    wait = FIRST_WAIT_S
    while True:
        try:
            return fn(*args)
        except Exception as e:  # outages show up as HTTP 5xx, timeouts and connection errors
            print(f"  {what} failed ({e!r}), retrying in {wait:g}s")
            time.sleep(wait)
            wait = min(wait * 2, MAX_WAIT_S)
