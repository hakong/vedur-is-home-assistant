"""Tests for current-observation station selection."""

from datetime import datetime, timedelta, timezone
import unittest

from custom_components.vedur_is.api import Observation, Station
from custom_components.vedur_is.geo import Coordinate
from custom_components.vedur_is.observation_utils import (
    observation_is_fresh,
    select_observation_station,
)

NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def _station(station_id: int, longitude: float) -> Station:
    return Station(
        station_id=station_id,
        name=f"Station {station_id}",
        abbr=None,
        station_type="sj",
        latitude=64.0,
        longitude=longitude,
        elevation=None,
        owner=None,
        start_year=None,
    )


def _observation(station_id: int, age: timedelta) -> Observation:
    return Observation.from_api(
        {
            "station": station_id,
            "name": f"Station {station_id}",
            "time": (NOW - age).replace(tzinfo=None).isoformat(),
            "t": 5.0,
        }
    )


class TestObservationStationSelection(unittest.TestCase):
    """Tests for freshness- and distance-bounded observation fallback."""

    def test_uses_fresh_preferred_station(self) -> None:
        """The geographically nearest fresh station remains preferred."""
        stations = [_station(1, -21.9), _station(2, -21.88)]
        observations = {
            1: _observation(1, timedelta(minutes=10)),
            2: _observation(2, timedelta(minutes=1)),
        }

        result = select_observation_station(
            Coordinate(64.0, -21.9),
            stations,
            observations,
            now=NOW,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.station.station_id, 1)  # type: ignore[union-attr]
        self.assertFalse(result.uses_fallback)  # type: ignore[union-attr]

    def test_replaces_stale_station_with_nearby_fresh_station(self) -> None:
        """A nearby fresh station replaces a stale preferred station."""
        stations = [_station(1, -21.9), _station(2, -21.88)]
        observations = {
            1: _observation(1, timedelta(hours=2)),
            2: _observation(2, timedelta(minutes=1)),
        }

        result = select_observation_station(
            Coordinate(64.0, -21.9),
            stations,
            observations,
            now=NOW,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.station.station_id, 2)  # type: ignore[union-attr]
        self.assertEqual(  # type: ignore[union-attr]
            result.preferred_station.station_id,
            1,
        )
        self.assertTrue(result.uses_fallback)  # type: ignore[union-attr]

    def test_rejects_fallback_more_than_fifteen_km_farther(self) -> None:
        """A fresh but substantially farther station is not selected."""
        stations = [_station(1, -21.9), _station(2, -21.53)]
        observations = {
            1: _observation(1, timedelta(hours=2)),
            2: _observation(2, timedelta(minutes=1)),
        }

        result = select_observation_station(
            Coordinate(64.0, -21.9),
            stations,
            observations,
            now=NOW,
        )

        self.assertIsNone(result)

    def test_rejects_fallback_more_than_twenty_five_km_away(self) -> None:
        """Absolute fallback distance also limits rural station jumps."""
        preferred = _station(1, -21.49)
        stations = [preferred, _station(2, -21.37)]
        observations = {
            1: _observation(1, timedelta(hours=2)),
            2: _observation(2, timedelta(minutes=1)),
        }

        result = select_observation_station(
            Coordinate(64.0, -21.9),
            stations,
            observations,
            preferred_station=preferred,
            now=NOW,
        )

        self.assertIsNone(result)

    def test_urridaholt_falls_back_to_kauptun(self) -> None:
        """The currently stale Urriðaholt station can use nearby Kauptún."""
        urridaholt = Station(
            station_id=1474,
            name="Garðabær Urriðaholt",
            abbr=None,
            station_type="sj",
            latitude=64.072,
            longitude=-21.924,
            elevation=None,
            owner=None,
            start_year=None,
        )
        kauptun = Station(
            station_id=31475,
            name="Garðabær - Kauptún",
            abbr=None,
            station_type="sj",
            latitude=64.075,
            longitude=-21.903,
            elevation=None,
            owner=None,
            start_year=None,
        )
        observations = {
            1474: _observation(1474, timedelta(hours=15)),
            31475: _observation(31475, timedelta(minutes=10)),
        }

        result = select_observation_station(
            Coordinate(64.072, -21.924),
            [urridaholt, kauptun],
            observations,
            now=NOW,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.station.station_id, 31475)  # type: ignore[union-attr]
        self.assertLess(result.distance_km, 2)  # type: ignore[union-attr]
        self.assertTrue(result.uses_fallback)  # type: ignore[union-attr]

    def test_observation_freshness_is_thirty_minutes(self) -> None:
        """One missed interval is tolerated but old observations are not."""
        self.assertTrue(
            observation_is_fresh(
                _observation(1, timedelta(minutes=30)),
                now=NOW,
            )
        )
        self.assertFalse(
            observation_is_fresh(
                _observation(1, timedelta(minutes=31)),
                now=NOW,
            )
        )

    def test_large_future_clock_skew_is_rejected(self) -> None:
        """Bad future timestamps are not considered current observations."""
        self.assertTrue(
            observation_is_fresh(
                _observation(1, timedelta(minutes=-5)),
                now=NOW,
            )
        )
        self.assertFalse(
            observation_is_fresh(
                _observation(1, timedelta(minutes=-6)),
                now=NOW,
            )
        )
