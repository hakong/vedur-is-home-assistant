"""Helpers for selecting current Vedur.is observations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .api import Observation, Station
from .const import (
    ATTR_OBSERVATION_USES_NEARBY_STATION,
    ATTR_PREFERRED_OBSERVATION_STATION_DISTANCE_KM,
    ATTR_PREFERRED_OBSERVATION_STATION_ID,
    ATTR_PREFERRED_OBSERVATION_STATION_NAME,
)
from .geo import Coordinate, nearest_station, nearest_stations

MAX_OBSERVATION_AGE = timedelta(minutes=30)
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)
MAX_OBSERVATION_FALLBACK_DISTANCE_KM = 25.0
MAX_ADDITIONAL_FALLBACK_DISTANCE_KM = 15.0


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
