"""Clock indirection.

Evals need reproducible output, and half of ITSM is timestamps. The store
therefore reads "now" from a :class:`Clock` that defaults to a frozen instant
matching the seed fixture. Set ``SNOW_LIVE_CLOCK=1`` for wall-clock behaviour.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

SN_FORMAT = "%Y-%m-%d %H:%M:%S"
FROZEN_NOW = datetime(2026, 8, 22, 14, 30, 0, tzinfo=timezone.utc)


class Clock:
    """Frozen by default; advances by a fixed step on every call when frozen."""

    def __init__(self, start: datetime | None = None, *, live: bool | None = None):
        if live is None:
            live = os.environ.get("SNOW_LIVE_CLOCK", "").lower() in ("1", "true", "yes")
        self.live = live
        self._current = start or FROZEN_NOW
        self._tick = timedelta(seconds=1)

    def now(self) -> datetime:
        if self.live:
            return datetime.now(timezone.utc)
        self._current = self._current + self._tick
        return self._current

    def peek(self) -> datetime:
        """Current instant without advancing the frozen clock."""
        if self.live:
            return datetime.now(timezone.utc)
        return self._current

    def stamp(self) -> str:
        """Current time as a ServiceNow-formatted string."""
        return self.now().strftime(SN_FORMAT)


def parse_stamp(value: str) -> datetime:
    """Parse a ServiceNow timestamp; returns epoch on unparseable input."""
    try:
        return datetime.strptime(value, SN_FORMAT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
