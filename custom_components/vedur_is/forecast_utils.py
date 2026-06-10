"""Forecast aggregation helpers for Vedur.is weather entities."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, time, timedelta
from typing import Any

from .api import ForecastPoint
from .weather_utils import (
    CONDITION_CLEAR_NIGHT,
    CONDITION_CLOUDY,
    CONDITION_EXCEPTIONAL,
    CONDITION_FOG,
    CONDITION_PARTLY_CLOUDY,
    CONDITION_POURING,
    CONDITION_RAINY,
    CONDITION_SNOWY,
    CONDITION_SNOWY_RAINY,
    CONDITION_SUNNY,
    CONDITION_WINDY,
    condition_from_forecast_text,
)

ForecastDict = dict[str, Any]

TWICE_DAILY_DAY_START_HOUR = 8
TWICE_DAILY_NIGHT_START_HOUR = 18

_CONDITION_PRIORITY = {
    CONDITION_POURING: 90,
    CONDITION_SNOWY_RAINY: 85,
    CONDITION_SNOWY: 80,
    CONDITION_RAINY: 70,
    CONDITION_FOG: 60,
    CONDITION_WINDY: 50,
    CONDITION_CLOUDY: 40,
    CONDITION_PARTLY_CLOUDY: 30,
    CONDITION_SUNNY: 20,
    CONDITION_CLEAR_NIGHT: 20,
    CONDITION_EXCEPTIONAL: 10,
}


def hourly_forecast_dicts(
    points: Iterable[ForecastPoint],
    now: datetime | None = None,
) -> list[ForecastDict]:
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
        for forecast in _future_points(points, now)
    ]


def daily_forecast_dicts(
    points: Iterable[ForecastPoint],
    now: datetime | None = None,
) -> list[ForecastDict]:
    """Aggregate Vedur XML time points into daily forecast dictionaries."""
    groups: dict[datetime, list[ForecastPoint]] = {}
    for point in _future_points(points, now):
        day_start = datetime.combine(point.time.date(), time.min)
        groups.setdefault(day_start, []).append(point)

    forecasts: list[ForecastDict] = []
    for day_start in sorted(groups):
        day_points = sorted(groups[day_start], key=lambda point: point.time)
        target = day_start.replace(hour=12)
        representative = _representative_condition_point(
            day_points,
            target,
            preferred_start=8,
            preferred_end=22,
        )
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


def twice_daily_forecast_dicts(
    points: Iterable[ForecastPoint],
    now: datetime | None = None,
) -> list[ForecastDict]:
    """Aggregate Vedur XML time points into day/night forecast dictionaries."""
    groups: dict[tuple[datetime, bool], list[ForecastPoint]] = {}
    for point in _future_points(points, now):
        segment_start, is_daytime = _segment_start(point.time)
        groups.setdefault((segment_start, is_daytime), []).append(point)

    forecasts: list[ForecastDict] = []
    for segment_start, is_daytime in sorted(groups):
        segment_points = sorted(
            groups[(segment_start, is_daytime)],
            key=lambda point: point.time,
        )
        target = _segment_target(segment_start, is_daytime)
        representative = _representative_condition_point(segment_points, target)
        windy = _max_by(segment_points, "wind_speed")

        forecast: ForecastDict = {
            "datetime": segment_start.isoformat(),
            "condition": condition_from_forecast_text(
                representative.weather_text,
                representative.time,
            ),
            "is_daytime": is_daytime,
        }
        if temperatures := _values(segment_points, "temperature"):
            forecast["native_temperature"] = max(temperatures)
            forecast["native_templow"] = min(temperatures)
        if windy is not None:
            forecast["native_wind_speed"] = windy.wind_speed
            forecast["wind_bearing"] = windy.wind_bearing
        forecasts.append(forecast)

    return forecasts


def _future_points(
    points: Iterable[ForecastPoint],
    now: datetime | None,
) -> list[ForecastPoint]:
    cutoff = _forecast_cutoff(now or datetime.now())
    return sorted(
        (point for point in points if point.time >= cutoff),
        key=lambda point: point.time,
    )


def _forecast_cutoff(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _segment_start(forecast_time: datetime) -> tuple[datetime, bool]:
    day_start = datetime.combine(forecast_time.date(), time.min)
    if (
        TWICE_DAILY_DAY_START_HOUR
        <= forecast_time.hour
        < TWICE_DAILY_NIGHT_START_HOUR
    ):
        return day_start.replace(hour=TWICE_DAILY_DAY_START_HOUR), True
    if forecast_time.hour < TWICE_DAILY_DAY_START_HOUR:
        return day_start - timedelta(hours=24 - TWICE_DAILY_NIGHT_START_HOUR), False
    return day_start.replace(hour=TWICE_DAILY_NIGHT_START_HOUR), False


def _segment_target(segment_start: datetime, is_daytime: bool) -> datetime:
    """Return the representative target time for a day or night segment."""
    if is_daytime:
        day_hours = TWICE_DAILY_NIGHT_START_HOUR - TWICE_DAILY_DAY_START_HOUR
        return segment_start + timedelta(hours=day_hours / 2)

    night_hours = 24 - TWICE_DAILY_NIGHT_START_HOUR + TWICE_DAILY_DAY_START_HOUR
    return segment_start + timedelta(hours=night_hours / 2)


def _representative_condition_point(
    points: list[ForecastPoint],
    target: datetime,
    *,
    preferred_start: int | None = None,
    preferred_end: int | None = None,
) -> ForecastPoint:
    preferred_points = _preferred_condition_points(
        points,
        preferred_start,
        preferred_end,
    )
    condition_counts = Counter(
        condition_from_forecast_text(point.weather_text, point.time)
        for point in preferred_points
    )
    return max(
        preferred_points,
        key=lambda point: (
            condition_counts[condition_from_forecast_text(
                point.weather_text,
                point.time,
            )],
            -abs(point.time - target).total_seconds(),
            _condition_priority(point),
        ),
    )


def _preferred_condition_points(
    points: list[ForecastPoint],
    preferred_start: int | None,
    preferred_end: int | None,
) -> list[ForecastPoint]:
    if preferred_start is None or preferred_end is None:
        return points

    preferred = [
        point
        for point in points
        if preferred_start <= point.time.hour < preferred_end
    ]
    return preferred or points


def _condition_priority(point: ForecastPoint) -> int:
    return _CONDITION_PRIORITY.get(
        condition_from_forecast_text(point.weather_text, point.time),
        0,
    )


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
