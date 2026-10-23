"""Helpers for selecting current Vedur.is observations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .api import (
    OBSERVATION_SOURCE_GOTTVEDUR_IS,
    OBSERVATION_SOURCE_UNAVAILABLE,
    Observation,
    ObservationPrecipitation,
    Station,
)
from .const import (
    ATTR_OBSERVATION_USES_NEARBY_STATION,
    ATTR_PREFERRED_OBSERVATION_STATION_DISTANCE_KM,
    ATTR_PREFERRED_OBSERVATION_STATION_ID,
    ATTR_PREFERRED_OBSERVATION_STATION_NAME,
)
from .geo import Coordinate, nearest_station, nearest_stations
from .time_utils import utc_isoformat, utc_naive, utc_now

MAX_OBSERVATION_AGE = timedelta(minutes=30)
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
MAX_OBSERVATION_FALLBACK_DISTANCE_KM = 25.0
MAX_ADDITIONAL_FALLBACK_DISTANCE_KM = 15.0


def current_precipitation(
    observation: Observation | None, *, now: datetime | None = None
) -> ObservationPrecipitation | None:
    """Validate precipitation against its own timestamp, not the official time."""
    if observation is None or observation.precipitation is None:
        return None
    precipitation = observation.precipitation
    current_time = utc_naive(now) if now is not None else utc_now()
    age = current_time - utc_naive(precipitation.end)
    if not -MAX_FUTURE_CLOCK_SKEW <= age <= MAX_OBSERVATION_AGE:
        return None
    return precipitation


def measurement_value(observation: Observation, key: str) -> Any:
    """Read an entity measurement; legacy entity key r now means hourly intensity."""
    if key == "r":
        precipitation = current_precipitation(observation)
        return precipitation.average_intensity if precipitation is not None else None
    return observation.value(key)


def measurement_source(observation: Observation, key: str) -> str:
    """Never advertise official ten-minute r as the hourly intensity source."""
    if key == "r":
        return (
            OBSERVATION_SOURCE_GOTTVEDUR_IS
            if current_precipitation(observation) is not None
            else OBSERVATION_SOURCE_UNAVAILABLE
        )
    return observation.value_source(key)


def measurement_time(observation: Observation, key: str) -> datetime | None:
    """Retain the precipitation window end even when that reading has gone stale."""
    if key == "r":
        return observation.precipitation.end if observation.precipitation else None
    return observation.value_time(key)


def precipitation_attributes(observation: Observation | None) -> dict[str, Any]:
    """Keep current intensity separate from weather forecast amounts and units."""
    precipitation = current_precipitation(observation)
    window = observation.precipitation if observation else None
    return {
        "precipitation_intensity": (
            precipitation.average_intensity if precipitation else None
        ),
        "precipitation_intensity_unit": "mm/h",
        "precipitation_amount": precipitation.amount if precipitation else None,
        "precipitation_amount_unit": "mm",
        "precipitation_averaging_period_minutes": 60,
        "precipitation_period_start": utc_isoformat(window.start) if window else None,
        "precipitation_period_end": utc_isoformat(window.end) if window else None,
        "precipitation_source": (
            OBSERVATION_SOURCE_GOTTVEDUR_IS
            if precipitation
            else OBSERVATION_SOURCE_UNAVAILABLE
        ),
    }


@dataclass(frozen=True, slots=True)
class ObservationStationSelection:
    """An observation station selected for a target coordinate."""

    station: Station
    distance_km: float
    preferred_station: Station
    preferred_distance_km: float

    @property
    def uses_fallback(self) -> bool:
        """Return whether a nearby station replaced the preferred station."""
        return self.station.station_id != self.preferred_station.station_id


def select_observation_station(
    coordinate: Coordinate,
    stations: Iterable[Station],
    observations: Mapping[int, Observation],
    *,
    preferred_station: Station | None = None,
    now: datetime | None = None,
) -> ObservationStationSelection | None:
    """Select a fresh nearby observation without making a large spatial jump."""
    station_list = [
        station
        for station in stations
        if station.station_type is None
        or station.station_type.casefold() == "sj"
    ]
    preferred_result = (
        _station_distance(coordinate, preferred_station)
        if preferred_station is not None
        else nearest_station(coordinate, station_list)
    )
    if preferred_result is None:
        return None

    preferred, preferred_distance = preferred_result
    if observation_is_fresh(observations.get(preferred.station_id), now=now):
        return ObservationStationSelection(
            station=preferred,
            distance_km=preferred_distance,
            preferred_station=preferred,
            preferred_distance_km=preferred_distance,
        )

    for station, distance in nearest_stations(coordinate, station_list):
        if station.station_id == preferred.station_id:
            continue
        if distance > MAX_OBSERVATION_FALLBACK_DISTANCE_KM:
            break
        if distance - preferred_distance > MAX_ADDITIONAL_FALLBACK_DISTANCE_KM:
            continue
        if not observation_is_fresh(observations.get(station.station_id), now=now):
            continue
        return ObservationStationSelection(
            station=station,
            distance_km=distance,
            preferred_station=preferred,
            preferred_distance_km=preferred_distance,
        )

    return None


def observation_is_fresh(
    observation: Observation | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether an observation is recent enough for current weather."""
    if observation is None or observation.time is None:
        return False

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    observation_time = observation.time
    if observation_time.tzinfo is None:
        observation_time = observation_time.replace(tzinfo=timezone.utc)
    age = current_time - observation_time
    return -MAX_FUTURE_CLOCK_SKEW <= age <= MAX_OBSERVATION_AGE


def observation_selection_attributes(
    selection: ObservationStationSelection | None,
) -> dict[str, Any]:
    """Return preferred-station and observation-fallback metadata."""
    if selection is None:
        return {ATTR_OBSERVATION_USES_NEARBY_STATION: False}
    return {
        ATTR_OBSERVATION_USES_NEARBY_STATION: selection.uses_fallback,
        ATTR_PREFERRED_OBSERVATION_STATION_ID: (
            selection.preferred_station.station_id
        ),
        ATTR_PREFERRED_OBSERVATION_STATION_NAME: selection.preferred_station.name,
        ATTR_PREFERRED_OBSERVATION_STATION_DISTANCE_KM: round(
            selection.preferred_distance_km,
            2,
        ),
    }


def _station_distance(
    coordinate: Coordinate,
    station: Station,
) -> tuple[Station, float] | None:
    """Return one station and its distance from a coordinate."""
    result = nearest_station(coordinate, [station])
    if result is None:
        return None
    return result
