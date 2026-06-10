"""Tests for Vedur.is forecast aggregation helpers."""

from __future__ import annotations

from datetime import datetime
import unittest

from custom_components.vedur_is.api import ForecastPoint
from custom_components.vedur_is.forecast_utils import (
    daily_forecast_dicts,
    hourly_forecast_dicts,
    twice_daily_forecast_dicts,
)


def _point(
    timestamp: str,
    temperature: float,
    wind_speed: float,
    wind_bearing: str,
    weather_text: str,
) -> ForecastPoint:
    return ForecastPoint(
        time=datetime.fromisoformat(timestamp),
        temperature=temperature,
        wind_speed=wind_speed,
        wind_bearing=wind_bearing,
        weather_text=weather_text,
    )


POINTS = (
    _point("2026-05-05T00:00:00", 1, 2, "N", "Clear sky"),
    _point("2026-05-05T06:00:00", 2, 3, "NE", "Cloudy"),
    _point("2026-05-05T12:00:00", 7, 5, "E", "Light rain"),
    _point("2026-05-05T18:00:00", 4, 4, "SE", "Partly cloudy"),
    _point("2026-05-06T00:00:00", 0, 6, "S", "Snow"),
    _point("2026-05-06T12:00:00", 5, 2, "SW", "Overcast"),
)


class TestForecastUtils(unittest.TestCase):
    """Tests for hourly, daily, and twice-daily forecast output."""

    def test_hourly_forecast_dicts(self) -> None:
        """Hourly forecast dictionaries preserve each XML point."""
        forecasts = hourly_forecast_dicts(POINTS[:1], now=datetime(2026, 5, 5, 0, 30))

        self.assertEqual(forecasts[0]["datetime"], "2026-05-05T00:00:00")
        self.assertEqual(forecasts[0]["condition"], "clear-night")
        self.assertEqual(forecasts[0]["native_temperature"], 1)
        self.assertEqual(forecasts[0]["native_wind_speed"], 2)

    def test_daily_forecast_dicts(self) -> None:
        """Daily forecast dictionaries include high, low, and period condition."""
        forecasts = daily_forecast_dicts(POINTS, now=datetime(2026, 5, 5, 0, 30))

        self.assertEqual(len(forecasts), 2)
        self.assertEqual(forecasts[0]["datetime"], "2026-05-05T00:00:00")
        self.assertEqual(forecasts[0]["condition"], "rainy")
        self.assertEqual(forecasts[0]["native_temperature"], 7)
        self.assertEqual(forecasts[0]["native_templow"], 1)
        self.assertEqual(forecasts[0]["native_wind_speed"], 5)
        self.assertEqual(forecasts[0]["wind_bearing"], "E")

    def test_twice_daily_forecast_dicts(self) -> None:
        """Twice-daily forecast dictionaries include required is_daytime flag."""
        forecasts = twice_daily_forecast_dicts(
            POINTS,
            now=datetime(2026, 5, 5, 0, 30),
        )

        self.assertEqual(forecasts[0]["datetime"], "2026-05-04T18:00:00")
        self.assertFalse(forecasts[0]["is_daytime"])
        self.assertEqual(forecasts[1]["datetime"], "2026-05-05T06:00:00")
        self.assertTrue(forecasts[1]["is_daytime"])
        self.assertEqual(forecasts[1]["condition"], "rainy")
        self.assertEqual(forecasts[1]["native_temperature"], 7)
        self.assertEqual(forecasts[1]["native_templow"], 2)

    def test_hourly_forecast_dicts_filter_past_points(self) -> None:
        """Hourly forecasts do not return points older than the current hour."""
        forecasts = hourly_forecast_dicts(POINTS, now=datetime(2026, 5, 5, 7, 15))

        self.assertEqual(forecasts[0]["datetime"], "2026-05-05T12:00:00")

    def test_daily_condition_uses_common_daytime_weather(self) -> None:
        """Daily condition is based on common daytime weather."""
        points = (
            _point("2026-05-05T06:00:00", 2, 3, "NE", "Cloudy"),
            _point("2026-05-05T12:00:00", 6, 4, "E", "Cloudy"),
            _point("2026-05-05T18:00:00", 4, 5, "SE", "Heavy rain"),
        )

        forecasts = daily_forecast_dicts(points, now=datetime(2026, 5, 5, 0, 0))

        self.assertEqual(forecasts[0]["condition"], "cloudy")

    def test_daily_condition_does_not_overweight_overnight_rain(self) -> None:
        """A few rainy night hours do not dominate a mostly sunny day."""
        points = (
            _point("2026-06-12T01:00:00", 12, 3, "E", "Light rain"),
            _point("2026-06-12T02:00:00", 11, 2, "E", "Light rain"),
            _point("2026-06-12T03:00:00", 11, 1, "E", "Light rain"),
            _point("2026-06-12T08:00:00", 14, 0, "E", "Partly cloudy"),
            _point("2026-06-12T09:00:00", 15, 1, "E", "Clear sky"),
            _point("2026-06-12T10:00:00", 16, 2, "SE", "Clear sky"),
            _point("2026-06-12T11:00:00", 17, 2, "SE", "Clear sky"),
            _point("2026-06-12T12:00:00", 18, 3, "SE", "Partly cloudy"),
            _point("2026-06-12T13:00:00", 18, 4, "S", "Cloudy"),
            _point("2026-06-12T14:00:00", 19, 4, "S", "Cloudy"),
            _point("2026-06-12T15:00:00", 21, 3, "S", "Partly cloudy"),
            _point("2026-06-12T16:00:00", 21, 3, "S", "Clear sky"),
            _point("2026-06-12T17:00:00", 21, 3, "S", "Clear sky"),
            _point("2026-06-12T18:00:00", 21, 3, "S", "Clear sky"),
            _point("2026-06-12T19:00:00", 20, 2, "S", "Clear sky"),
            _point("2026-06-12T20:00:00", 20, 2, "S", "Clear sky"),
            _point("2026-06-12T21:00:00", 18, 2, "S", "Clear sky"),
        )

        forecasts = daily_forecast_dicts(points, now=datetime(2026, 6, 12, 0, 0))

        self.assertEqual(forecasts[0]["condition"], "sunny")

    def test_twice_daily_condition_uses_common_segment_weather(self) -> None:
        """Twice-daily periods are not dominated by one cloudy/rainy point."""
        points = (
            _point("2026-06-12T06:00:00", 12, 1, "E", "Overcast"),
            _point("2026-06-12T07:00:00", 13, 0, "E", "Overcast"),
            _point("2026-06-12T08:00:00", 14, 0, "E", "Partly cloudy"),
            _point("2026-06-12T09:00:00", 15, 1, "E", "Clear sky"),
            _point("2026-06-12T10:00:00", 16, 2, "SE", "Clear sky"),
            _point("2026-06-12T11:00:00", 17, 2, "SE", "Clear sky"),
            _point("2026-06-12T12:00:00", 18, 3, "SE", "Partly cloudy"),
            _point("2026-06-12T13:00:00", 18, 4, "S", "Cloudy"),
            _point("2026-06-12T14:00:00", 19, 4, "S", "Cloudy"),
            _point("2026-06-12T15:00:00", 21, 3, "S", "Partly cloudy"),
            _point("2026-06-12T16:00:00", 21, 3, "S", "Clear sky"),
            _point("2026-06-12T17:00:00", 21, 3, "S", "Clear sky"),
        )

        forecasts = twice_daily_forecast_dicts(
            points,
            now=datetime(2026, 6, 12, 0, 0),
        )

        self.assertEqual(forecasts[0]["condition"], "sunny")
