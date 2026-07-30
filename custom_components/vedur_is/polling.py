"""Polling schedule helpers for Vedur.is observations."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import floor

from .const import DEFAULT_SCAN_INTERVAL_MINUTES

OBSERVATION_PUBLICATION_OFFSET_SECONDS = 75
BACKOFF_INTERVALS_MINUTES = (2, 5, 10, 20, 40, 80, 160, 320, 360)
MAX_BACKOFF_JITTER_SECONDS = 30


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


def backoff_poll_delay(
    failure_count: int,
    *,
    jitter_seconds: float = 0,
) -> timedelta:
    """Return a bounded retry delay for consecutive update failures."""
    if failure_count < 1:
        raise ValueError("Failure count must be at least one")

    interval_index = min(failure_count - 1, len(BACKOFF_INTERVALS_MINUTES) - 1)
    base_delay = timedelta(minutes=BACKOFF_INTERVALS_MINUTES[interval_index])
    bounded_jitter = max(0, min(jitter_seconds, MAX_BACKOFF_JITTER_SECONDS))
    maximum_delay = timedelta(minutes=BACKOFF_INTERVALS_MINUTES[-1])
    return min(base_delay + timedelta(seconds=bounded_jitter), maximum_delay)
