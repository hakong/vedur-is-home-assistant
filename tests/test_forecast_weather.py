"""Forecast precipitation checks using Home Assistant entity classes."""

from datetime import datetime, timedelta
from dataclasses import replace
from importlib.util import find_spec
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HAS_HOME_ASSISTANT = find_spec("homeassistant") is not None
if HAS_HOME_ASSISTANT:
    from homeassistant.core import HomeAssistant

    from custom_components.vedur_is.api import (
        ForecastPoint,
        ForecastPrecipitation,
        Observation,
        Station,
        StationForecast,
    )
    from custom_components.vedur_is.services import _forecast_response
    from custom_components.vedur_is.weather import (
        VedurIsDeviceTrackerWeatherEntity,
        VedurIsHomeWeatherEntity,
        VedurIsPersonWeatherEntity,
        VedurIsStationWeatherEntity,
    )
    from custom_components.vedur_is.weather_coordinator import (
        VedurIsWeatherData,
        VedurIsWeatherDataUpdateCoordinator,
    )
    from custom_components.vedur_is.sensor import (
        CONDITION_SENSOR_DESCRIPTION, VedurIsLocationSensor, WeatherSensorSource,
    )


@unittest.skipUnless(HAS_HOME_ASSISTANT, "Requires the Home Assistant runtime")
class TestWeatherForecastPrecipitation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant("/tmp/vedur-forecast-test")
        self.day = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        attrs = {"latitude": 64.07, "longitude": -21.91, "friendly_name": "Test"}
        for entity_id in ("zone.home", "person.test", "device_tracker.route"):
            self.hass.states.async_set(entity_id, "home", attrs)
        self.station = Station(
            1474, "Station", None, "sj", 64.07, -21.91, None, None, None
        )
        self.other = Station(1, "Other", None, "sj", 64.13, -21.91, None, None, None)
        self.stations = {1474: self.station, 1: self.other}
        self.forecast = StationForecast(
            1474,
            "Station",
            None,
            None,
            tuple(
                ForecastPoint(self.day + timedelta(hours=h), 5, 2, "N", "Cloudy")
                for h in range(48)
            ),
            tuple(
                ForecastPrecipitation(
                    self.day + timedelta(hours=h),
                    self.day + timedelta(hours=h + 1),
                    1.5 if h == 12 else 0,
                )
                for h in range(48)
            ),
        )
        self.entry = SimpleNamespace(
            options={}, data={"device_tracker_entity_ids": ["device_tracker.route"]}
        )
        self.coordinator = SimpleNamespace(
            hass=self.hass,
            data=VedurIsWeatherData(self.stations, {}, {1474: self.forecast}),
            config_entry=self.entry,
        )

    async def test_all_weather_entities_include_amounts_and_unit_conversion(self):
        entities = (
            VedurIsHomeWeatherEntity(self.coordinator),
            VedurIsPersonWeatherEntity(self.coordinator, "person.test"),
            VedurIsDeviceTrackerWeatherEntity(self.coordinator, "device_tracker.route"),
            VedurIsStationWeatherEntity(self.coordinator, self.station),
        )
        for entity in entities:
            with self.subTest(entity=type(entity).__name__):
                entity.hass = self.hass
                hourly = await entity.async_forecast_hourly()
                self.assertEqual(hourly[13]["native_precipitation"], 1.5)
                self.assertNotIn("precipitation_probability", hourly[13])
                converted = entity._convert_forecast(hourly)
                self.assertEqual(converted[13]["precipitation"], 1.5)
                daily = await entity.async_forecast_daily()
                self.assertEqual(daily[0]["native_precipitation"], 1.5)
                twice = await entity.async_forecast_twice_daily()
                self.assertTrue(
                    any(x.get("native_precipitation") == 1.5 for x in twice)
                )
                self.assertEqual(
                    entity.extra_state_attributes["forecast_precipitation_source"],
                    "gottvedur_is",
                )

    async def test_source_selection_deduplicates_and_honors_toggles(self):
        def select():
            return (
                VedurIsWeatherDataUpdateCoordinator._precipitation_forecast_station_ids(
                    self.coordinator, self.stations, {1474: self.forecast}
                )
            )

        self.assertEqual(select(), {1474})

        self.entry.data.update(
            {
                "enable_home_weather": False,
                "enable_person_weather": False,
                "enable_device_tracker_weather": False,
                "enable_station_weather": False,
            }
        )
        self.assertEqual(select(), set())
        self.entry.data.update({"enable_station_weather": True, "station_ids": [1]})
        self.assertEqual(select(), {1474})

    async def test_weather_and_diagnostic_conditions_share_provider_and_solar_fallback(self):
        now = datetime.now().replace(second=0, microsecond=0)
        point = ForecastPoint(now, 5, 2, 90, "Rain", "ClearSkyNight", "gottvedur_is")
        forecast = replace(self.forecast, forecasts=(point,))
        self.coordinator.data = replace(self.coordinator.data, forecasts={1474: forecast})
        entities = (
            VedurIsHomeWeatherEntity(self.coordinator),
            VedurIsPersonWeatherEntity(self.coordinator, "person.test"),
            VedurIsDeviceTrackerWeatherEntity(self.coordinator, "device_tracker.route"),
            VedurIsStationWeatherEntity(self.coordinator, self.station),
        )
        sensor = VedurIsLocationSensor(
            self.coordinator,
            WeatherSensorSource(key="home_weather", name="Home", device_identifier="home_weather", model="Home weather", source_type="home", source_entity_id="zone.home"),
            CONDITION_SENSOR_DESCRIPTION,
        )
        sensor.hass = self.hass
        for entity in entities:
            entity.hass = self.hass
            self.assertEqual(entity.condition, "clear-night")
            self.assertEqual(entity.extra_state_attributes["condition_source"], "gottvedur_forecast")
            self.assertEqual(entity.extra_state_attributes["condition_station_id"], 1474)
        self.assertEqual(sensor.native_value, "clear-night")
        self.assertEqual(sensor.extra_state_attributes["condition_raw"], "ClearSkyNight")
        self.assertNotIn("observation_time", sensor.extra_state_attributes)
        observation = Observation.from_api(
            {"station": 1474, "time": now.isoformat(), "icon": "Hail", "t": 5},
            value_source="gottvedur_is",
        )
        self.coordinator.data = replace(self.coordinator.data, observations={1474: observation})
        for entity in entities:
            self.assertEqual(entity.condition, "hail")
            self.assertEqual(entity.extra_state_attributes["condition_source"], "gottvedur_observation")
        self.assertEqual(sensor.native_value, "hail")
        morning = datetime(2026, 10, 23, 8)
        xml = replace(forecast, forecasts=(ForecastPoint(morning, 5, 2, "N", "Clear sky"),))
        self.coordinator.data = replace(self.coordinator.data, observations={}, forecasts={1474: xml})
        self.hass.config.latitude = 0
        self.hass.config.longitude = 60
        with (
            patch("custom_components.vedur_is.weather_utils.utc_now", return_value=morning),
            patch("custom_components.vedur_is.weather.utc_now", return_value=morning),
            patch("custom_components.vedur_is.sensor.utc_now", return_value=morning),
        ):
            for entity in entities:
                self.assertEqual(entity.condition, "clear-night")
                self.assertEqual(entity.extra_state_attributes["condition_source"], "xml_forecast")
                self.assertEqual(entity.extra_state_attributes["condition_station_id"], 1474)
            self.assertEqual(sensor.native_value, "clear-night")

    async def test_location_service_uses_the_same_amounts(self):
        for kind in ("hourly", "daily", "twice_daily"):
            with self.subTest(kind=kind):
                forecasts = _forecast_response(
                    self.forecast.forecasts,
                    kind,
                    precipitation=self.forecast.precipitation,
                )
                self.assertTrue(
                    any(x.get("native_precipitation") == 1.5 for x in forecasts)
                )


if __name__ == "__main__":
    unittest.main()
