"""Small helpers shared across the project."""

import re


def slug(spec: str) -> str:
    """File-name-safe form of a market spec, e.g. 'hl:xyz:GOLD' -> 'hl-xyz-gold'."""
    return re.sub(r"[^a-z0-9]+", "-", spec.lower()).strip("-")
