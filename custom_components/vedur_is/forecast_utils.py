"""Forecast aggregation helpers for Vedur.is weather entities."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta
from typing import Any

from .api import ForecastPoint
from .weather_utils import condition_from_forecast_text

ForecastDict = dict[str, Any]


def hourly_forecast_dicts(points: Iterable[ForecastPoint]) -> list[ForecastDict]:
    """Return Home Assistant hourly forecast dictionaries."""
    return [
        {
            "datetime": forecast.time.isoformat(),
            "condition": condition_from_forecast_text(
                forecast.weather_text,
                forecast.time,
            ),
            "native_temperature": forecast.temperature,
            "native_wind_speed": forecast.wind_speed,
            "wind_bearing": forecast.wind_bearing,
        }
        for forecast in points
    ]


def daily_forecast_dicts(points: Iterable[ForecastPoint]) -> list[ForecastDict]:
    """Aggregate Vedur XML time points into daily forecast dictionaries."""
    groups: dict[datetime, list[ForecastPoint]] = {}
    for point in points:
        day_start = datetime.combine(point.time.date(), time.min)
        groups.setdefault(day_start, []).append(point)

    forecasts: list[ForecastDict] = []
    for day_start in sorted(groups):
        day_points = sorted(groups[day_start], key=lambda point: point.time)
        representative = _closest_to(day_points, day_start.replace(hour=12))
        windy = _max_by(day_points, "wind_speed")

        forecast: ForecastDict = {
            "datetime": day_start.isoformat(),
            "condition": condition_from_forecast_text(
                representative.weather_text,
                representative.time,
            ),
        }
        if temperatures := _values(day_points, "temperature"):
            forecast["native_temperature"] = max(temperatures)
            forecast["native_templow"] = min(temperatures)
        if windy is not None:
            forecast["native_wind_speed"] = windy.wind_speed
            forecast["wind_bearing"] = windy.wind_bearing
        forecasts.append(forecast)

    return forecasts


def twice_daily_forecast_dicts(points: Iterable[ForecastPoint]) -> list[ForecastDict]:
    """Aggregate Vedur XML time points into day/night forecast dictionaries."""
    groups: dict[tuple[datetime, bool], list[ForecastPoint]] = {}
    for point in points:
        segment_start, is_daytime = _segment_start(point.time)
        groups.setdefault((segment_start, is_daytime), []).append(point)

    forecasts: list[ForecastDict] = []
    for segment_start, is_daytime in sorted(groups):
        segment_points = sorted(
            groups[(segment_start, is_daytime)],
            key=lambda point: point.time,
        )
        representative = _closest_to(
            segment_points,
            segment_start + timedelta(hours=6),
        )
        windy = _max_by(segment_points, "wind_speed")

        forecast: ForecastDict = {
            "datetime": segment_start.isoformat(),
            "condition": condition_from_forecast_text(
                representative.weather_text,
                representative.time,
            ),
            "is_daytime": is_daytime,
        }
        if representative.temperature is not None:
            forecast["native_temperature"] = representative.temperature
        if temperatures := _values(segment_points, "temperature"):
            forecast["native_templow"] = min(temperatures)
        if windy is not None:
            forecast["native_wind_speed"] = windy.wind_speed
            forecast["wind_bearing"] = windy.wind_bearing
        forecasts.append(forecast)

    return forecasts


def _segment_start(forecast_time: datetime) -> tuple[datetime, bool]:
    day_start = datetime.combine(forecast_time.date(), time.min)
    if 6 <= forecast_time.hour < 18:
        return day_start.replace(hour=6), True
    if forecast_time.hour < 6:
        return day_start - timedelta(hours=6), False
    return day_start.replace(hour=18), False


def _closest_to(points: list[ForecastPoint], target: datetime) -> ForecastPoint:
    return min(points, key=lambda point: abs(point.time - target))


def _max_by(points: list[ForecastPoint], attr: str) -> ForecastPoint | None:
    available = [point for point in points if getattr(point, attr) is not None]
    if not available:
        return None
    return max(available, key=lambda point: getattr(point, attr))


def _values(points: list[ForecastPoint], attr: str) -> list[float]:
    return [
        value
        for point in points
        if (value := getattr(point, attr)) is not None
    ]
