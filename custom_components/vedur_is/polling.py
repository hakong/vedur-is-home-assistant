"""Polling schedule helpers for Vedur.is observations."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import floor

from .const import DEFAULT_SCAN_INTERVAL_MINUTES

OBSERVATION_PUBLICATION_OFFSET_SECONDS = 75


def next_aligned_poll_delay(now: datetime) -> timedelta:
    """Return the delay to the next observation publication window."""
    if now.tzinfo is None:
        raise ValueError("Polling alignment requires a timezone-aware datetime")

    interval_seconds = DEFAULT_SCAN_INTERVAL_MINUTES * 60
    current_timestamp = now.timestamp()
    next_slot = (
        floor(
            (current_timestamp - OBSERVATION_PUBLICATION_OFFSET_SECONDS)
            / interval_seconds
        )
        + 1
    )
    next_timestamp = (
        next_slot * interval_seconds + OBSERVATION_PUBLICATION_OFFSET_SECONDS
    )
    return timedelta(seconds=next_timestamp - current_timestamp)
