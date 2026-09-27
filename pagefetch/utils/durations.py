"""Duration parsing."""

from __future__ import annotations

import re

_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>[smhd])$", re.IGNORECASE)
_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str | int | float) -> int:
    """Parse seconds or a compact duration such as ``24h`` into seconds."""
    if isinstance(value, bool):
        raise ValueError("duration must be a positive number or duration string")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError("duration cannot be negative")
        return round(value)
    if not isinstance(value, str):
        raise TypeError("duration must be a number or string")
    stripped = value.strip()
    # Accept bare digit strings (``"3600"``, ``"0"``) so YAML/CLI values that
    # arrive quoted as strings flow through the same code path as ints.
    if stripped.isdigit():
        return int(stripped)
    match = _DURATION_RE.fullmatch(stripped)
    if not match:
        raise ValueError("duration must use s, m, h, or d (for example, '24h')")
    return int(float(match.group("value")) * _MULTIPLIERS[match.group("unit").lower()])

