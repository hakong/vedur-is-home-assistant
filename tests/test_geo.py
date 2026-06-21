"""Tests for Vedur.is geographic helpers."""

from __future__ import annotations

import unittest

from custom_components.vedur_is.api import Station
from custom_components.vedur_is.geo import (
    Coordinate,
    distance_km,
    nearest_station,
    nearest_stations,
    resolve_person_coordinate,
    resolve_tracker_coordinate,
)


def _station(
    station_id: int,
    name: str,
    latitude: float,
    longitude: float,
) -> Station:
    return Station(
        station_id=station_id,
        name=name,
        abbr=None,
        station_type="sj",
        latitude=latitude,
        longitude=longitude,
        elevation=None,
        owner=None,
        start_year=None,
    )


class TestGeoHelpers(unittest.TestCase):
    """Tests for person coordinate and nearest-station helpers."""

    def test_distance_km(self) -> None:
        """Distance uses kilometers."""
        distance = distance_km(Coordinate(64.14, -21.94), Coordinate(65.68, -18.1))

        self.assertGreater(distance, 240)
        self.assertLess(distance, 260)

    def test_nearest_station(self) -> None:
        """The station with the smallest coordinate distance is selected."""
        result = nearest_station(
            Coordinate(64.09, -21.91),
            [
                _station(3470, "Akureyri", 65.68, -18.1),
                _station(31475, "Garðabær - Kauptún", 64.08, -21.9),
            ],
        )

        self.assertIsNotNone(result)
        self.assertEqual(result[0].station_id, 31475)  # type: ignore[index]

    def test_nearest_stations(self) -> None:
        """Stations are returned by distance and can be limited."""
        results = nearest_stations(
            Coordinate(64.09, -21.91),
            [
                _station(1, "Far", 65.68, -18.1),
                _station(2, "Near", 64.08, -21.9),
                _station(3, "Nearer", 64.085, -21.905),
            ],
            limit=2,
        )

        self.assertEqual([station.station_id for station, _ in results], [3, 2])

    def test_resolve_person_coordinate_from_direct_attributes(self) -> None:
        """Direct latitude and longitude attributes win."""
        coordinate = resolve_person_coordinate(
            "home",
            {"latitude": "64.1", "longitude": "-21.9"},
            {"home": Coordinate(1, 2)},
        )

        self.assertEqual(coordinate, Coordinate(64.1, -21.9))

    def test_resolve_person_coordinate_from_zone(self) -> None:
        """A zone state can be resolved to the zone coordinate."""
        coordinate = resolve_person_coordinate(
            "Home",
            {},
            {"home": Coordinate(64.1, -21.9)},
        )

        self.assertEqual(coordinate, Coordinate(64.1, -21.9))

    def test_resolve_tracker_coordinate_from_direct_attributes(self) -> None:
        """Device trackers use direct latitude and longitude attributes."""
        coordinate = resolve_tracker_coordinate(
            {"latitude": "65.68", "longitude": "-18.1"},
        )

        self.assertEqual(coordinate, Coordinate(65.68, -18.1))

    def test_resolve_tracker_coordinate_does_not_use_zone_state(self) -> None:
        """Device tracker weather does not infer coordinates from state names."""
        coordinate = resolve_tracker_coordinate({})

        self.assertIsNone(coordinate)
