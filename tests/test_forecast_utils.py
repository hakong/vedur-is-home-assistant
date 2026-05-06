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

    def test_daily_condition_uses_significant_period_weather(self) -> None:
        """Daily condition is not limited to the nearest midday forecast point."""
        points = (
            _point("2026-05-05T06:00:00", 2, 3, "NE", "Cloudy"),
            _point("2026-05-05T12:00:00", 6, 4, "E", "Cloudy"),
            _point("2026-05-05T18:00:00", 4, 5, "SE", "Heavy rain"),
        )

        forecasts = daily_forecast_dicts(points, now=datetime(2026, 5, 5, 0, 0))

        self.assertEqual(forecasts[0]["condition"], "pouring")
