"""Tests for the observation polling schedule."""

from datetime import datetime, timedelta, timezone
import unittest

from custom_components.vedur_is.polling import next_aligned_poll_delay


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
