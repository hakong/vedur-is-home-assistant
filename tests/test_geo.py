"""Tests for Vedur.is geographic helpers."""

from __future__ import annotations

import unittest

from custom_components.vedur_is.api import Station
from custom_components.vedur_is.geo import (
    Coordinate,
    distance_km,
    nearest_station,
    resolve_person_coordinate,
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
