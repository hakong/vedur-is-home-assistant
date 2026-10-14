"""Tests for forecast precipitation intervals and complete-period totals."""

from datetime import datetime, timedelta, timezone
import asyncio
import unittest

from custom_components.vedur_is.api import (
    ForecastPoint,
    ForecastPrecipitation,
    InvalidResponse,
    StationForecast,
    VedurIsApiClient,
    parse_gottvedur_forecast_precipitation,
)
from custom_components.vedur_is.forecast_utils import (
    daily_forecast_dicts,
    hourly_forecast_dicts,
    twice_daily_forecast_dicts,
)
from tests.test_api import FakeSession

DAY = datetime(2026, 10, 15)


def _point(hour: int) -> ForecastPoint:
    return ForecastPoint(DAY + timedelta(hours=hour), 5, 2, "N", "Cloudy")


def _interval(start: int, end: int, amount: float | None) -> ForecastPrecipitation:
    return ForecastPrecipitation(
        DAY + timedelta(hours=start), DAY + timedelta(hours=end), amount
    )


class TestForecastPrecipitation(unittest.TestCase):
    def test_chart_intervals_and_nulls(self) -> None:
        rows = [
            {"time": "2026-10-15T17:00:00", "model": "ig", "precipitation": 0},
            {"time": "2026-10-15T18:00:00", "model": "ig", "precipitation": None},
            {"time": "2026-10-15T21:00:00", "model": "ecm-is", "precipitation": 6},
            {"time": "2026-10-16T00:00:00", "model": "ecm-is", "precipitation": 3},
            {"time": "2026-10-16T06:00:00", "model": "ecm-is", "precipitation": 4},
        ]
        intervals = parse_gottvedur_forecast_precipitation({"nextDays": rows})
        self.assertEqual(
            [i.end - i.start for i in intervals],
            [timedelta(hours=h) for h in (1, 1, 3, 3, 6)],
        )
        self.assertEqual([i.amount for i in intervals], [0, None, 6, 3, 4])
        self.assertEqual(intervals[3].start, DAY.replace(hour=21))
        self.assertEqual(intervals[3].end, DAY + timedelta(days=1))

    def test_invalid_amounts_remain_unknown(self) -> None:
        for value in (
            None,
            "",
            "?",
            "not a number",
            -1,
            float("nan"),
            float("inf"),
            True,
        ):
            with self.subTest(value=value):
                intervals = parse_gottvedur_forecast_precipitation(
                    {
                        "nextDays": [
                            {
                                "time": DAY.isoformat(),
                                "model": "ig",
                                "precipitation": value,
                            }
                        ]
                    }
                )
                self.assertIsNone(intervals[0].amount)

    def test_bad_payloads_are_rejected(self) -> None:
        row = {"time": DAY.isoformat(), "model": "ig", "precipitation": 1}
        for payload in (
            None,
            {},
            {"nextDays": None},
            {"nextDays": [None]},
            {"nextDays": [{"time": "bad"}]},
            {"nextDays": [row, row]},
        ):
            with self.subTest(payload=payload), self.assertRaises(InvalidResponse):
                parse_gottvedur_forecast_precipitation(payload)

    def test_utc_offsets_and_unsorted_input(self) -> None:
        intervals = parse_gottvedur_forecast_precipitation(
            {
                "nextDays": [
                    {
                        "time": "2026-10-15T03:00:00+01:00",
                        "model": "ig",
                        "precipitation": 2,
                    },
                    {"time": "2026-10-15T01:00:00Z", "model": "ig", "precipitation": 1},
                ]
            }
        )
        self.assertEqual(intervals[0], _interval(0, 1, 1))
        self.assertEqual(intervals[1], _interval(1, 2, 2))

    def test_unknown_model_and_missing_ig_hour_are_not_stretched(self) -> None:
        rows = [
            {"time": "2026-10-15T01:00:00", "model": "ig", "precipitation": 1},
            {"time": "2026-10-15T03:00:00", "model": "ig", "precipitation": 2},
            {"time": "2026-10-15T04:00:00", "model": "unknown", "precipitation": 3},
        ]
        self.assertEqual(
            parse_gottvedur_forecast_precipitation({"nextDays": rows}),
            (_interval(0, 1, 1),),
        )

    def test_hourly_does_not_expose_longer_accumulations(self) -> None:
        forecasts = hourly_forecast_dicts(
            [_point(1), _point(2), _point(3), _point(6)],
            now=DAY,
            precipitation=[
                _interval(0, 1, 0),
                _interval(1, 2, None),
                _interval(3, 6, 6),
            ],
        )
        self.assertEqual(forecasts[0]["native_precipitation"], 0)
        for forecast in forecasts[1:]:
            self.assertNotIn("native_precipitation", forecast)
            self.assertNotIn("precipitation_probability", forecast)

    def test_daily_sums_mixed_intervals_once_including_midnight(self) -> None:
        intervals = [_interval(h, h + 1, 1) for h in range(18)]
        intervals += [_interval(18, 21, 6), _interval(21, 24, 3)]
        # Midnight's amount belongs to the preceding day, not the next day.
        forecasts = daily_forecast_dicts(
            [_point(12), _point(24)],
            now=DAY,
            precipitation=intervals,
        )
        self.assertEqual(forecasts[0]["native_precipitation"], 27)
        self.assertNotIn("native_precipitation", forecasts[1])

    def test_full_six_hour_day_sums_without_multiplying_amounts(self) -> None:
        forecasts = daily_forecast_dicts(
            [_point(12)],
            now=DAY,
            precipitation=[_interval(h, h + 6, 3) for h in range(0, 24, 6)],
        )
        self.assertEqual(forecasts[0]["native_precipitation"], 12)

    def test_daily_missing_null_overlap_and_partial_coverage_are_unknown(self) -> None:
        complete = [_interval(h, h + 1, 0) for h in range(24)]
        cases = [
            complete[:12] + complete[13:],
            complete[:12] + [_interval(12, 13, None)] + complete[13:],
            complete + [_interval(12, 13, 1)],
            complete[8:],
            complete[:-1],
        ]
        for intervals in cases:
            with self.subTest(intervals=intervals):
                forecasts = daily_forecast_dicts(
                    [_point(12)], now=DAY, precipitation=intervals
                )
                self.assertNotIn("native_precipitation", forecasts[0])
        forecasts = daily_forecast_dicts([_point(12)], now=DAY, precipitation=complete)
        self.assertEqual(forecasts[0]["native_precipitation"], 0)

    def test_partial_today_is_not_a_full_daily_total(self) -> None:
        forecasts = daily_forecast_dicts(
            [_point(12)],
            now=DAY.replace(hour=12),
            precipitation=[_interval(h, h + 1, 1) for h in range(11, 24)],
        )
        self.assertNotIn("native_precipitation", forecasts[0])

    def test_twice_daily_sums_complete_intervals_without_splitting(self) -> None:
        intervals = [_interval(h, h + 1, 1) for h in range(8, 32)]
        forecasts = twice_daily_forecast_dicts(
            [_point(12), _point(20)],
            now=DAY,
            precipitation=intervals,
        )
        self.assertEqual(forecasts[0]["native_precipitation"], 10)
        self.assertEqual(forecasts[1]["native_precipitation"], 14)
        forecasts = twice_daily_forecast_dicts(
            [_point(12)],
            now=DAY,
            precipitation=[_interval(h, h + 3, 3) for h in range(6, 18, 3)],
        )
        self.assertNotIn("native_precipitation", forecasts[0])


class TestForecastPrecipitationClient(unittest.TestCase):
    def _forecasts(self) -> dict[int, StationForecast]:
        return {
            station_id: StationForecast(
                station_id, str(station_id), None, None, (_point(1),)
            )
            for station_id in (1474, 31475)
        }

    def test_selected_stations_are_deduplicated_and_cached(self) -> None:
        session = FakeSession(
            {
                "nextDays": [
                    {
                        "time": (DAY + timedelta(hours=1)).isoformat(),
                        "model": "ig",
                        "precipitation": 0.3,
                    }
                ]
            }
        )
        client = VedurIsApiClient(session)

        async def fetch():
            return await asyncio.gather(
                *(
                    client.async_enrich_forecast_precipitation(
                        self._forecasts(), [1474, 1474, 999]
                    )
                    for _ in range(2)
                )
            )

        results = asyncio.run(fetch())
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1], [("stationId", "1474")])
        self.assertEqual(results[0][1474].precipitation[0].amount, 0.3)
        self.assertEqual(results[0][31475].precipitation, ())

    def test_failure_is_unknown_and_retries_after_cooldown(self) -> None:
        session = FakeSession({}, status=503)
        client = VedurIsApiClient(session)
        for _ in range(2):
            result = asyncio.run(
                client.async_enrich_forecast_precipitation(self._forecasts(), [1474])
            )
            self.assertEqual(result[1474].precipitation, ())
        self.assertEqual(len(session.calls), 1)
        client._gottvedur_forecast_cache[1474] = (
            datetime.now(timezone.utc) - timedelta(minutes=16),
            None,
        )
        session.status = 200
        session.payload = {
            "nextDays": [{"time": DAY.isoformat(), "model": "ig", "precipitation": 0}]
        }
        result = asyncio.run(
            client.async_enrich_forecast_precipitation(self._forecasts(), [1474])
        )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(result[1474].precipitation[0].amount, 0)

    def test_empty_selection_makes_no_requests(self) -> None:
        session = FakeSession({})
        client = VedurIsApiClient(session)
        original = self._forecasts()
        result = asyncio.run(client.async_enrich_forecast_precipitation(original, []))
        self.assertEqual(result, original)
        self.assertEqual(session.calls, [])
