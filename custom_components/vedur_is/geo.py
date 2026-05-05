"""Geographic helpers for the Vedur.is integration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt
from typing import Any

from .api import Station

EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True, slots=True)
class Coordinate:
    """A latitude/longitude coordinate."""

    latitude: float
    longitude: float


def distance_km(first: Coordinate, second: Coordinate) -> float:
    """Return the haversine distance between two coordinates in kilometers."""
    lat1 = radians(first.latitude)
    lon1 = radians(first.longitude)
    lat2 = radians(second.latitude)
    lon2 = radians(second.longitude)
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1

    haversine = (
        sin(delta_lat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * asin(sqrt(haversine))


def nearest_station(
    coordinate: Coordinate,
    stations: Iterable[Station],
) -> tuple[Station, float] | None:
    """Return the nearest station with usable coordinates."""
    nearest: tuple[Station, float] | None = None

    for station in stations:
        if station.latitude is None or station.longitude is None:
            continue

        station_distance = distance_km(
            coordinate,
            Coordinate(station.latitude, station.longitude),
        )
        if nearest is None or station_distance < nearest[1]:
            nearest = (station, station_distance)

    return nearest


def resolve_person_coordinate(
    state: str | None,
    attributes: Mapping[str, Any],
    zones: Mapping[str, Coordinate],
) -> Coordinate | None:
    """Resolve a person location from direct attributes or a zone name."""
    direct = _coordinate_from_mapping(attributes)
    if direct is not None:
        return direct

    if not state:
        return None

    return zones.get(state.casefold())


def _coordinate_from_mapping(values: Mapping[str, Any]) -> Coordinate | None:
    latitude = values.get("latitude")
    longitude = values.get("longitude")
    if latitude is None or longitude is None:
        return None

    try:
        return Coordinate(float(latitude), float(longitude))
    except (TypeError, ValueError):
        return None
