"""Tests for the observation polling schedule."""

from datetime import datetime, timedelta, timezone
import unittest

from custom_components.vedur_is.polling import (
    MAX_BACKOFF_JITTER_SECONDS,
    backoff_poll_delay,
    next_aligned_poll_delay,
)


class TestPollingSchedule(unittest.TestCase):
    """Tests for wall-clock-aligned observation polling."""

    def test_boundary_waits_for_publication_offset(self) -> None:
        """A boundary refresh is scheduled after the publication delay."""
        now = datetime(2026, 7, 15, 19, 30, tzinfo=timezone.utc)

        self.assertEqual(next_aligned_poll_delay(now), timedelta(seconds=75))

    def test_exact_poll_time_schedules_the_next_interval(self) -> None:
        """A completed aligned poll does not immediately run again."""
        now = datetime(2026, 7, 15, 19, 31, 15, tzinfo=timezone.utc)

        self.assertEqual(next_aligned_poll_delay(now), timedelta(minutes=10))

    def test_arbitrary_start_time_aligns_to_next_slot(self) -> None:
        """An arbitrary startup phase is corrected after its first fetch."""
        now = datetime(2026, 7, 15, 19, 34, 20, tzinfo=timezone.utc)

        self.assertEqual(
            next_aligned_poll_delay(now),
            timedelta(minutes=6, seconds=55),
        )

    def test_alignment_crosses_the_hour(self) -> None:
        """The final interval of an hour aligns into the next hour."""
        now = datetime(2026, 7, 15, 19, 59, tzinfo=timezone.utc)

        self.assertEqual(
            next_aligned_poll_delay(now),
            timedelta(minutes=2, seconds=15),
        )

    def test_naive_datetime_is_rejected(self) -> None:
        """Alignment cannot silently depend on the host timezone."""
        with self.assertRaises(ValueError):
            next_aligned_poll_delay(datetime(2026, 7, 15, 19, 30))

    def test_backoff_starts_with_short_recovery_delays(self) -> None:
        """Transient failures retry quickly before slowing down."""
        expected_minutes = (2, 5, 10, 20, 40, 80, 160, 320, 360)

        self.assertEqual(
            tuple(
                backoff_poll_delay(failure_count)
                for failure_count in range(1, len(expected_minutes) + 1)
            ),
            tuple(timedelta(minutes=minutes) for minutes in expected_minutes),
        )

    def test_backoff_remains_capped_after_many_failures(self) -> None:
        """Long outages never schedule retries more than six hours apart."""
        self.assertEqual(backoff_poll_delay(100), timedelta(hours=6))

    def test_backoff_jitter_is_small_and_bounded(self) -> None:
        """Retry jitter cannot materially extend the configured delay."""
        self.assertEqual(
            backoff_poll_delay(1, jitter_seconds=300),
            timedelta(minutes=2, seconds=MAX_BACKOFF_JITTER_SECONDS),
        )
        self.assertEqual(
            backoff_poll_delay(1, jitter_seconds=-10),
            timedelta(minutes=2),
        )

    def test_backoff_rejects_non_failure_counts(self) -> None:
        """Backoff is only defined after an update failure."""
        with self.assertRaises(ValueError):
            backoff_poll_delay(0)
