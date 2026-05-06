"""Weather condition helpers for the Vedur.is integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

CONDITION_CLEAR_NIGHT = "clear-night"
CONDITION_CLOUDY = "cloudy"
CONDITION_EXCEPTIONAL = "exceptional"
CONDITION_FOG = "fog"
CONDITION_PARTLY_CLOUDY = "partlycloudy"
CONDITION_POURING = "pouring"
CONDITION_RAINY = "rainy"
CONDITION_SNOWY = "snowy"
CONDITION_SNOWY_RAINY = "snowy-rainy"
CONDITION_SUNNY = "sunny"
CONDITION_WINDY = "windy"

_CLEAR_FORECAST_TEXTS = {
    "clear sky",
    "heiðskírt",
}

_FORECAST_TEXT_CONDITIONS = {
    "alskýjað": CONDITION_CLOUDY,
    "cloudy": CONDITION_CLOUDY,
    "léttskýjað": CONDITION_PARTLY_CLOUDY,
    "light rain": CONDITION_RAINY,
    "light sleet": CONDITION_SNOWY_RAINY,
    "light snow": CONDITION_SNOWY,
    "lítils háttar rigning": CONDITION_RAINY,
    "lítils háttar slydda": CONDITION_SNOWY_RAINY,
    "lítils háttar snjókoma": CONDITION_SNOWY,
    "overcast": CONDITION_CLOUDY,
    "partly cloudy": CONDITION_PARTLY_CLOUDY,
    "rain": CONDITION_RAINY,
    "rain showers": CONDITION_RAINY,
    "rigning": CONDITION_RAINY,
    "skýjað": CONDITION_CLOUDY,
    "skúrir": CONDITION_RAINY,
    "sleet": CONDITION_SNOWY_RAINY,
    "sleet showers": CONDITION_SNOWY_RAINY,
    "slydda": CONDITION_SNOWY_RAINY,
    "slydduél": CONDITION_SNOWY_RAINY,
    "snow": CONDITION_SNOWY,
    "snow showers": CONDITION_SNOWY,
    "snjókoma": CONDITION_SNOWY,
    "snjóél": CONDITION_SNOWY,
}


def condition_from_forecast_text(
    text: str | None,
    forecast_time: datetime | None = None,
) -> str:
    """Map vedur XML forecast weather text to a Home Assistant condition."""
    normalized = (text or "").strip().casefold()
    if not normalized:
        return CONDITION_EXCEPTIONAL

    if normalized in _CLEAR_FORECAST_TEXTS:
        return _clear_condition(forecast_time)
    if normalized in _FORECAST_TEXT_CONDITIONS:
        return _FORECAST_TEXT_CONDITIONS[normalized]

    if "fog" in normalized:
        return CONDITION_FOG
    if "sleet" in normalized:
        return CONDITION_SNOWY_RAINY
    if "snow" in normalized:
        return CONDITION_SNOWY
    if "heavy rain" in normalized or "rain" in normalized and "heavy" in normalized:
        return CONDITION_POURING
    if "rain" in normalized or "drizzle" in normalized or "showers" in normalized:
        return CONDITION_RAINY
    if "partly cloudy" in normalized:
        return CONDITION_PARTLY_CLOUDY
    if "cloudy" in normalized or "overcast" in normalized:
        return CONDITION_CLOUDY
    if "clear" in normalized or "fair" in normalized:
        return _clear_condition(forecast_time)

    return CONDITION_EXCEPTIONAL


def condition_from_observation(values: Any) -> str:
    """Derive a conservative condition from current observation values."""
    precipitation = _float_or_none(values.value("r"))
    temperature = _float_or_none(values.value("t"))
    wind_speed = _float_or_none(values.value("f"))

    if precipitation is not None and precipitation > 0:
        if temperature is not None and temperature <= 0:
            return CONDITION_SNOWY
        return CONDITION_RAINY

    if wind_speed is not None and wind_speed >= 17.0:
        return CONDITION_WINDY

    return CONDITION_EXCEPTIONAL


def _clear_condition(forecast_time: datetime | None) -> str:
    if forecast_time is not None and (
        forecast_time.hour < 7 or forecast_time.hour >= 22
    ):
        return CONDITION_CLEAR_NIGHT
    return CONDITION_SUNNY


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
