"""Provider condition translation and station-specific solar classification."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import logging
from typing import Any

from astral import Observer
from astral.sun import elevation, sunrise, sunset

from .api import ForecastPoint, Station, StationForecast
from .geo import coordinate_from_values
from .observation_utils import MAX_FUTURE_CLOCK_SKEW, MAX_OBSERVATION_AGE
from .time_utils import utc_isoformat, utc_naive, utc_now

_LOGGER = logging.getLogger(__name__)

CONDITION_CLEAR_NIGHT = "clear-night"
CONDITION_CLOUDY = "cloudy"
CONDITION_EXCEPTIONAL = "exceptional"
CONDITION_FOG = "fog"
CONDITION_HAIL = "hail"
CONDITION_LIGHTNING = "lightning"
CONDITION_PARTLY_CLOUDY = "partlycloudy"
CONDITION_POURING = "pouring"
CONDITION_RAINY = "rainy"
CONDITION_SNOWY = "snowy"
CONDITION_SNOWY_RAINY = "snowy-rainy"
CONDITION_SUNNY = "sunny"
CONDITION_WINDY = "windy"

# Gottvedur's renderer and translations identify Storm as wind (Icelandic Rok).
# HA has no freezing-rain, blowing-snow, or dust-specific condition vocabulary.
GOTTVEDUR_ICON_CONDITIONS = {
    "ClearSky": CONDITION_SUNNY,
    "ClearSkyNight": CONDITION_CLEAR_NIGHT,
    "PartlyCloudy": CONDITION_PARTLY_CLOUDY,
    "PartlyCloudyNight": CONDITION_PARTLY_CLOUDY,
    "Cloudy": CONDITION_CLOUDY,
    "CloudyNight": CONDITION_CLOUDY,
    "Overcast": CONDITION_CLOUDY,
    "LightRain": CONDITION_RAINY,
    "Rain": CONDITION_RAINY,
    "LightSleet": CONDITION_SNOWY_RAINY,
    "Sleet": CONDITION_SNOWY_RAINY,
    "LightSnow": CONDITION_SNOWY,
    "Snow": CONDITION_SNOWY,
    "Storm": CONDITION_WINDY,
    "LightRainShowers": CONDITION_RAINY,
    "LightRainShowersNight": CONDITION_RAINY,
    "RainShowers": CONDITION_RAINY,
    "RainShowersNight": CONDITION_RAINY,
    "SleetShowers": CONDITION_SNOWY_RAINY,
    "SleetShowersNight": CONDITION_SNOWY_RAINY,
    "SnowShowers": CONDITION_SNOWY,
    "SnowShowersNight": CONDITION_SNOWY,
    "DustDevil": CONDITION_EXCEPTIONAL,
    "DustStorm": CONDITION_EXCEPTIONAL,
    "BlowingSnow": CONDITION_SNOWY,
    "Fog": CONDITION_FOG,
    "FogDrizzle": CONDITION_FOG,
    "FogMist": CONDITION_FOG,
    "LightDrizzle": CONDITION_RAINY,
    "Drizzle": CONDITION_RAINY,
    "FreezingRain": CONDITION_RAINY,
    "Hail": CONDITION_HAIL,
    "HailNight": CONDITION_HAIL,
    "LightThunder": CONDITION_LIGHTNING,
    "Thunder": CONDITION_LIGHTNING,
}

_CLEAR_FORECAST_TEXTS = {"clear sky", "heiðskírt"}
_FORECAST_TEXT_CONDITIONS = {
    "partly cloudy": CONDITION_PARTLY_CLOUDY,
    "léttskýjað": CONDITION_PARTLY_CLOUDY,
    "cloudy": CONDITION_CLOUDY,
    "skýjað": CONDITION_CLOUDY,
    "overcast": CONDITION_CLOUDY,
    "alskýjað": CONDITION_CLOUDY,
    "light rain": CONDITION_RAINY,
    "lítils háttar rigning": CONDITION_RAINY,
    "rain": CONDITION_RAINY,
    "rigning": CONDITION_RAINY,
    "light sleet": CONDITION_SNOWY_RAINY,
    "lítils háttar slydda": CONDITION_SNOWY_RAINY,
    "sleet": CONDITION_SNOWY_RAINY,
    "slydda": CONDITION_SNOWY_RAINY,
    "light snow": CONDITION_SNOWY,
    "lítils háttar snjókoma": CONDITION_SNOWY,
    "snow": CONDITION_SNOWY,
    "snjókoma": CONDITION_SNOWY,
    "rain showers": CONDITION_RAINY,
    "skúrir": CONDITION_RAINY,
    "sleet showers": CONDITION_SNOWY_RAINY,
    "slydduél": CONDITION_SNOWY_RAINY,
    "snow showers": CONDITION_SNOWY,
    "snjóél": CONDITION_SNOWY,
    "dust devil": CONDITION_EXCEPTIONAL,
    "skýstrókar": CONDITION_EXCEPTIONAL,
    "dust storm": CONDITION_EXCEPTIONAL,
    "moldrok": CONDITION_EXCEPTIONAL,
    "blowing snow": CONDITION_SNOWY,
    "skafrenningur": CONDITION_SNOWY,
    "fog": CONDITION_FOG,
    "þoka": CONDITION_FOG,
    "light drizzle": CONDITION_RAINY,
    "lítils háttar súld": CONDITION_RAINY,
    "drizzle": CONDITION_RAINY,
    "súld": CONDITION_RAINY,
    "freezing rain": CONDITION_RAINY,
    "frostrigning": CONDITION_RAINY,
    "hail": CONDITION_HAIL,
    "hagl": CONDITION_HAIL,
    "light thunder": CONDITION_LIGHTNING,
    "lítils háttar þrumuveður": CONDITION_LIGHTNING,
    "thunder": CONDITION_LIGHTNING,
    "þrumuveður": CONDITION_LIGHTNING,
    "heavy rain": CONDITION_POURING,
}


@lru_cache(maxsize=128)
def _unmapped(source: str, raw: str) -> None:
    """Warn once per bounded set of unmapped codes, without guessing their meaning."""
    _LOGGER.warning("Unmapped %s weather condition: %r", source, raw)


def condition_from_icon(icon: str | None) -> str | None:
    """Translate the provider's explicit icon, including its day/night choice."""
    if not isinstance(icon, str) or not icon:
        return None
    condition = GOTTVEDUR_ICON_CONDITIONS.get(icon)
    if condition is None:
        _unmapped("Gottvedur", icon)
    return condition


def condition_from_forecast_text(
    text: str | None,
    forecast_time: datetime | None = None,
    *,
    station: Station | None = None,
) -> str | None:
    """Translate XML text; resolve clear skies using the forecast station's sun."""
    normalized = (text or "").strip().casefold()
    if not normalized:
        return None
    if normalized in _CLEAR_FORECAST_TEXTS:
        return _clear_condition(forecast_time, station)
    condition = _FORECAST_TEXT_CONDITIONS.get(normalized)
    if condition is None:
        _unmapped("XML", text or "")
    return condition


def condition_from_forecast_point(
    point: ForecastPoint,
    station: Station | None = None,
    *,
    at_time: datetime | None = None,
) -> str | None:
    """Prefer Gottvedur, with an exact-time XML text fallback."""
    return condition_from_icon(point.icon) or condition_from_forecast_text(
        point.weather_text,
        at_time if at_time is not None else point.time,
        station=station,
    )


def forecast_condition_attributes(
    point: ForecastPoint | None,
    station: Station | None,
    *,
    observation: Any = None,
    observation_station: Station | None = None,
) -> dict[str, Any]:
    """Keep the raw condition and its valid time distinct from observation time."""
    if observation is not None and condition_from_observation(observation) is not None:
        timestamp = observation.value_time("icon")
        return {
            "condition_source": "gottvedur_observation"
            if observation.value_source("icon") == "gottvedur_is"
            else "official_observation",
            "condition_raw": observation.value("icon"),
            "condition_time": utc_isoformat(timestamp) if timestamp else None,
            "condition_station_id": observation_station.station_id
            if observation_station
            else None,
        }
    if point is None:
        return {"condition_source": None, "condition_raw": None, "condition_time": None}
    provider = condition_from_icon(point.icon) is not None
    return {
        "condition_source": "gottvedur_forecast" if provider else "xml_forecast",
        "condition_raw": point.icon if provider else point.weather_text,
        "forecast_icon": point.icon,
        "condition_time": utc_isoformat(point.time),
        "condition_station_id": station.station_id if station is not None else None,
    }


def condition_from_observation(values: Any) -> str | None:
    """Use an observed provider icon if supplied; never infer phase from numbers."""
    condition = condition_from_icon(values.value("icon"))
    if condition is None:
        return None
    timestamp = values.value_time("icon")
    if timestamp is None or not (
        -MAX_FUTURE_CLOCK_SKEW
        <= utc_now() - utc_naive(timestamp)
        <= MAX_OBSERVATION_AGE
    ):
        return None
    return condition


@lru_cache(maxsize=512)
def _solar_events(
    latitude: float, longitude: float, day: date
) -> tuple[datetime, datetime]:
    observer = Observer(latitude, longitude)
    return sunrise(observer, day, tzinfo=timezone.utc), sunset(
        observer, day, tzinfo=timezone.utc
    )


def _clear_condition(at_time: datetime | None, station: Station | None) -> str | None:
    if at_time is None or station is None:
        return None
    coordinate = coordinate_from_values(station.latitude, station.longitude)
    if coordinate is None:
        return None
    when = utc_naive(at_time).replace(tzinfo=timezone.utc)
    lat, lon = coordinate.latitude, coordinate.longitude
    try:
        rise, setting = _solar_events(lat, lon, when.date())
        is_day = (
            rise <= when < setting if rise < setting else when >= rise or when < setting
        )
    except ValueError:
        # Polar day/night can have no sunrise or sunset on the requested date.
        is_day = elevation(Observer(lat, lon), when) > 0
    return CONDITION_SUNNY if is_day else CONDITION_CLEAR_NIGHT


def current_forecast_point(
    points: Iterable[ForecastPoint],
    now: datetime | None = None,
) -> ForecastPoint | None:
    """Use the closest valid forecast instant; never reuse expired forecasts forever."""
    current = utc_naive(now) if now is not None else utc_now()
    ordered = sorted(points, key=lambda point: utc_naive(point.time))
    if not ordered or current < utc_naive(ordered[0].time) - timedelta(hours=1):
        return None
    if current > utc_naive(ordered[-1].time) + timedelta(hours=1):
        return None
    return min(
        ordered,
        key=lambda point: (abs(utc_naive(point.time) - current), utc_naive(point.time)),
    )


def current_station_forecast_point(
    forecast: StationForecast,
    now: datetime | None = None,
) -> ForecastPoint | None:
    """Use XML when the primary product does not cover the current time."""
    return current_forecast_point(forecast.forecasts, now) or current_forecast_point(
        forecast.xml_forecasts,
        now,
    )
