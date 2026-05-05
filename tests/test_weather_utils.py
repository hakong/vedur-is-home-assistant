"""Tests for Vedur.is weather condition helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import unittest

from custom_components.vedur_is.weather_utils import (
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
    condition_from_observation,
)


class FakeObservation:
    """Minimal observation-like object."""

    def __init__(self, values: dict[str, Any]) -> None:
        """Initialize the fake observation."""
        self._values = values

    def value(self, key: str) -> Any:
        """Return an observation value."""
        return self._values.get(key)


class TestWeatherUtils(unittest.TestCase):
    """Tests for weather condition mapping."""

    def test_forecast_condition_mapping(self) -> None:
        """Common Vedur forecast texts map to Home Assistant conditions."""
        cases = {
            "Clear sky": CONDITION_SUNNY,
            "Partly cloudy": CONDITION_PARTLY_CLOUDY,
            "Cloudy": CONDITION_CLOUDY,
            "Overcast": CONDITION_CLOUDY,
            "Light rain": CONDITION_RAINY,
            "Rain showers": CONDITION_RAINY,
            "Heavy rain": CONDITION_POURING,
            "Snow showers": CONDITION_SNOWY,
            "Sleet": CONDITION_SNOWY_RAINY,
            "Fog": CONDITION_FOG,
            "Something unexpected": CONDITION_EXCEPTIONAL,
        }

        for text, condition in cases.items():
            with self.subTest(text=text):
                self.assertEqual(condition_from_forecast_text(text), condition)

    def test_clear_sky_at_night_maps_to_clear_night(self) -> None:
        """Clear sky uses clear-night during night hours."""
        self.assertEqual(
            condition_from_forecast_text(
                "Clear sky",
                datetime(2026, 5, 4, 23, 0, 0),
            ),
            CONDITION_CLEAR_NIGHT,
        )

    def test_observation_condition_mapping(self) -> None:
        """Observation fallback is conservative but useful."""
        self.assertEqual(
            condition_from_observation(FakeObservation({"r": 0.2, "t": 4})),
            CONDITION_RAINY,
        )
        self.assertEqual(
            condition_from_observation(FakeObservation({"r": 0.2, "t": -1})),
            CONDITION_SNOWY,
        )
        self.assertEqual(
            condition_from_observation(FakeObservation({"f": 18})),
            CONDITION_WINDY,
        )
        self.assertEqual(
            condition_from_observation(FakeObservation({})),
            CONDITION_EXCEPTIONAL,
        )
