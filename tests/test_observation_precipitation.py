"""Current precipitation has one provider, one accumulation window, and explicit units."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from custom_components.vedur_is.api import (
    BASE_URL,
    CannotConnect,
    Observation,
    Station,
    VedurIsApiClient,
    merge_observation_fallback,
    parse_gottvedur_observation_payload,
)
from custom_components.vedur_is.observation_utils import (
    current_precipitation,
    measurement_source,
    measurement_time,
    measurement_value,
    precipitation_attributes,
)
from custom_components.vedur_is.time_utils import utc_isoformat, utc_naive
from tests.test_api import FakeSession


def gott_observation(amount, timestamp=None, **extra):
    return parse_gottvedur_observation_payload(
        {
            "pageProps": {
                "latestObservation": {
                    "station": 1474,
                    "observationTime": timestamp
                    or datetime.now(timezone.utc).isoformat(),
                    "precipitation": amount,
                    "isManned": False,
                    **extra,
                },
            },
        }
    )


class TestObservationPrecipitation(unittest.TestCase):
    def test_saved_precipitation_window_evidence(self):
        with (
            Path(__file__).parent / "fixtures/precipitation-6459-2026-10-23.json"
        ).open() as source:
            payload = json.load(source)
        bins = payload["details"][1:]
        self.assertEqual(len(bins), 6)
        for first, second in zip(payload["details"], bins):
            self.assertEqual(
                datetime.fromisoformat(second["observationTime"])
                - datetime.fromisoformat(first["observationTime"]),
                timedelta(minutes=10),
            )
        self.assertAlmostEqual(
            sum(row["precipitation"] for row in bins),
            payload["latest"]["precipitation"],
        )
        self.assertAlmostEqual(
            bins[-1]["accumulatedPrecipitation"]
            - payload["details"][0]["accumulatedPrecipitation"],
            2.7,
        )
        self.assertEqual(bins[-1]["precipitation"], 1.2)

    def test_hourly_amount_is_not_a_ten_minute_amount_or_an_instantaneous_rate(self):
        observation = gott_observation(2.7)
        amount = observation.precipitation
        self.assertEqual(amount.amount, 2.7)
        self.assertEqual(amount.end - amount.start, timedelta(hours=1))
        self.assertEqual(amount.average_intensity, 2.7)
        self.assertEqual(measurement_value(observation, "r"), 2.7)
        self.assertEqual(measurement_source(observation, "r"), "gottvedur_is")
        self.assertIsNone(observation.value("r"))

    def test_zero_is_valid_dry_weather(self):
        observation = gott_observation(0)
        self.assertEqual(measurement_value(observation, "r"), 0)
        self.assertEqual(measurement_source(observation, "r"), "gottvedur_is")

    def test_missing_and_invalid_values_remain_unknown(self):
        for amount in (
            None,
            "",
            "?",
            "bad",
            "2.7",
            True,
            False,
            -0.1,
            float("nan"),
            float("inf"),
            -float("inf"),
        ):
            with self.subTest(amount=amount):
                observation = gott_observation(amount)
                self.assertIsNone(observation.precipitation)
                self.assertIsNone(measurement_value(observation, "r"))
                self.assertEqual(measurement_source(observation, "r"), "unavailable")
        missing = parse_gottvedur_observation_payload(
            {"pageProps": {"latestObservation": {"station": 1474}}}
        )
        self.assertIsNone(missing.precipitation)

    def test_manned_station_window_is_not_assumed_to_be_one_hour(self):
        observation = gott_observation(3, isManned=True)
        self.assertIsNone(observation.precipitation)

    def test_non_null_official_r_cannot_override_or_replace_hourly_measurement(self):
        official = Observation.from_api(
            {
                "station": 1474,
                "time": datetime.now(timezone.utc).isoformat(),
                "t": 5,
                "r": 1.2,
            }
        )
        self.assertIsNone(measurement_value(official, "r"))
        self.assertEqual(measurement_source(official, "r"), "unavailable")
        merged = merge_observation_fallback(
            official, gott_observation(2.7, temperature=10)
        )
        self.assertEqual(merged.value("r"), 1.2)  # Unmodified raw official API data.
        self.assertEqual(measurement_value(merged, "r"), 2.7)
        self.assertEqual(measurement_source(merged, "r"), "gottvedur_is")
        self.assertEqual(measurement_value(merged, "t"), 5)
        self.assertEqual(measurement_source(merged, "t"), "official_api")
        unknown = merge_observation_fallback(official, gott_observation(None))
        self.assertIsNone(measurement_value(unknown, "r"))

    def test_precipitation_uses_its_own_time_and_fixed_duration(self):
        now = datetime.now(timezone.utc)
        end = now - timedelta(minutes=10)
        official = Observation.from_api(
            {"station": 1474, "time": now.isoformat(), "r": 1}
        )
        merged = merge_observation_fallback(
            official, gott_observation(2.7, end.isoformat())
        )
        self.assertEqual(measurement_time(merged, "r"), utc_naive(end))
        attrs = precipitation_attributes(merged)
        self.assertEqual(attrs["precipitation_amount"], 2.7)
        self.assertEqual(attrs["precipitation_amount_unit"], "mm")
        self.assertEqual(attrs["precipitation_intensity"], 2.7)
        self.assertEqual(attrs["precipitation_intensity_unit"], "mm/h")
        self.assertEqual(attrs["precipitation_averaging_period_minutes"], 60)
        self.assertEqual(
            attrs["precipitation_period_start"], utc_isoformat(end - timedelta(hours=1))
        )
        self.assertEqual(attrs["precipitation_period_end"], utc_isoformat(end))

    def test_stale_and_future_hourly_values_cannot_borrow_official_timestamp(self):
        now = datetime.now(timezone.utc)
        official = Observation.from_api(
            {"station": 1474, "time": now.isoformat(), "r": 0}
        )
        for delta in (timedelta(minutes=31), -timedelta(minutes=6)):
            end = now - delta
            merged = merge_observation_fallback(
                official, gott_observation(2.7, end.isoformat())
            )
            self.assertIsNone(measurement_value(merged, "r"))
            self.assertEqual(measurement_source(merged, "r"), "unavailable")
            attrs = precipitation_attributes(merged)
            self.assertIsNone(attrs["precipitation_amount"])
            self.assertIsNone(attrs["precipitation_intensity"])
            self.assertEqual(attrs["precipitation_period_end"], utc_isoformat(end))

    def test_fresh_gott_value_is_not_rejected_because_official_time_is_old(self):
        now = datetime.now(timezone.utc)
        official = Observation.from_api(
            {
                "station": 1474,
                "time": (now - timedelta(hours=1)).isoformat(),
                "r": 0,
            }
        )
        merged = merge_observation_fallback(
            official, gott_observation(2.7, now.isoformat())
        )
        self.assertEqual(measurement_value(merged, "r"), 2.7)
        # Do not silently rejuvenate the other official measurements.
        self.assertEqual(merged.time, official.time)

    def test_freshness_normalizes_timezone_offsets(self):
        now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        observation = gott_observation(2.7, "2026-10-23T14:00:00+02:00")
        self.assertIsNotNone(current_precipitation(observation, now=now))
        self.assertIsNone(
            current_precipitation(observation, now=now + timedelta(minutes=31))
        )

    def test_client_fetches_even_when_official_r_is_non_null_and_reuses_cache(self):
        session = FakeSession(
            [
                {
                    "station": 1474,
                    "time": datetime.now(timezone.utc).isoformat(),
                    "r": 1.2,
                }
            ]
        )
        client = VedurIsApiClient(session)
        client._async_get_gottvedur_latest_observation = AsyncMock(
            return_value=gott_observation(2.7)
        )

        async def poll():
            for _ in range(2):
                observations = await client.async_get_latest_observations(
                    [1474], fallback_station_ids=[1474]
                )
                self.assertEqual(measurement_value(observations[1474], "r"), 2.7)

        asyncio.run(poll())
        client._async_get_gottvedur_latest_observation.assert_awaited_once_with(1474)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(
            all(
                url == f"{BASE_URL}/observations/aws/10min/latest"
                for url, _ in session.calls
            )
        )

    def test_source_failure_does_not_fall_back_to_official_r_or_zero(self):
        for raw in (0, 1.2, None):
            client = VedurIsApiClient(
                FakeSession(
                    [
                        {
                            "station": 1474,
                            "time": datetime.now(timezone.utc).isoformat(),
                            "r": raw,
                        }
                    ]
                )
            )
            client._async_get_gottvedur_latest_observation = AsyncMock(
                side_effect=CannotConnect("offline")
            )
            observations = asyncio.run(
                client.async_get_latest_observations(
                    [1474], fallback_station_ids=[1474]
                )
            )
            self.assertIsNone(measurement_value(observations[1474], "r"))
            self.assertEqual(measurement_source(observations[1474], "r"), "unavailable")


HAS_HOME_ASSISTANT = find_spec("homeassistant") is not None
if HAS_HOME_ASSISTANT:
    from homeassistant.components.sensor import (
        UNDEFINED,
        SensorDeviceClass,
        SensorStateClass,
    )
    from homeassistant.const import UnitOfVolumetricFlux
    from homeassistant.core import HomeAssistant

    from custom_components.vedur_is.sensor import (
        SENSOR_DESCRIPTIONS,
        VedurIsLocationSensor,
        VedurIsSensor,
        WeatherSensorSource,
    )
    from custom_components.vedur_is.services import _observation_response
    from custom_components.vedur_is.weather import (
        VedurIsDeviceTrackerWeatherEntity,
        VedurIsHomeWeatherEntity,
        VedurIsPersonWeatherEntity,
        VedurIsStationWeatherEntity,
    )
    from custom_components.vedur_is.weather_coordinator import VedurIsWeatherData


@unittest.skipUnless(HAS_HOME_ASSISTANT, "Requires the Home Assistant runtime")
class TestPrecipitationEntities(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant("/tmp/vedur-precipitation-test")
        self.now = datetime.now(timezone.utc)
        self.station = Station(
            1474, "Test station", None, "sj", 64.07, -21.91, None, None, None
        )
        attributes = {"latitude": 64.07, "longitude": -21.91, "friendly_name": "Test"}
        for entity_id in ("zone.home", "person.test", "device_tracker.route"):
            self.hass.states.async_set(entity_id, "home", attributes)
        official = Observation.from_api(
            {"station": 1474, "time": self.now.isoformat(), "t": 5, "r": 9}
        )
        self.observation = merge_observation_fallback(
            official,
            gott_observation(2.7, (self.now - timedelta(minutes=10)).isoformat()),
        )
        self.coordinator = SimpleNamespace(
            hass=self.hass,
            last_update_success=True,
            data=VedurIsWeatherData({1474: self.station}, {1474: self.observation}, {}),
            config_entry=SimpleNamespace(options={}, data={}),
        )
        self.description = next(item for item in SENSOR_DESCRIPTIONS if item.key == "r")

    def sensors(self):
        sources = (
            WeatherSensorSource(
                "home_weather",
                "Home",
                "home_weather",
                "Home weather",
                "home",
                "zone.home",
            ),
            WeatherSensorSource(
                "person.test",
                "Test",
                "person_weather:person.test",
                "Person-following weather",
                "person",
                "person.test",
            ),
            WeatherSensorSource(
                "device_tracker.route",
                "Route",
                "device_tracker_weather:device_tracker.route",
                "Device tracker weather",
                "tracker",
                "device_tracker.route",
            ),
        )
        sensors = [VedurIsSensor(self.coordinator, self.station, self.description)]
        sensors.extend(
            VedurIsLocationSensor(self.coordinator, source, self.description)
            for source in sources
        )
        for sensor in sensors:
            sensor.hass = self.hass
        return sensors

    async def test_station_home_person_tracker_sensor_classes_units_and_stable_ids(
        self,
    ):
        sensors = self.sensors()
        self.assertEqual(sensors[0].unique_id, "vedur_is_1474_r")
        self.assertEqual(sensors[1].unique_id, "vedur_is_home_weather_r")
        self.assertEqual(sensors[2].unique_id, "vedur_is_person.test_r")
        self.assertEqual(sensors[3].unique_id, "vedur_is_device_tracker.route_r")
        for sensor in sensors:
            with self.subTest(sensor=sensor.unique_id):
                self.assertTrue(sensor.available)
                self.assertEqual(sensor.native_value, 2.7)
                self.assertEqual(
                    sensor.device_class, SensorDeviceClass.PRECIPITATION_INTENSITY
                )
                self.assertEqual(
                    sensor.native_unit_of_measurement,
                    UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR,
                )
                self.assertEqual(sensor.state_class, SensorStateClass.MEASUREMENT)
                attrs = sensor.extra_state_attributes
                self.assertEqual(attrs["observation_source"], "gottvedur_is")
                self.assertEqual(
                    attrs["observation_time"],
                    utc_isoformat(self.observation.precipitation.end),
                )
                self.assertEqual(attrs["precipitation_amount"], 2.7)
                self.assertEqual(attrs["precipitation_averaging_period_minutes"], 60)

    async def test_all_precipitation_sensors_unavailable_without_valid_hourly_value(
        self,
    ):
        for observation in (
            replace(self.observation, precipitation=None),
            replace(
                self.observation,
                precipitation=gott_observation(
                    2.7, (self.now - timedelta(hours=1)).isoformat()
                ).precipitation,
            ),
        ):
            self.coordinator.data.observations[1474] = observation
            for sensor in self.sensors():
                self.assertFalse(sensor.available)
                self.assertIsNone(sensor.native_value)
                self.assertEqual(
                    sensor.extra_state_attributes["observation_source"], "unavailable"
                )

    async def test_weather_current_units_do_not_reuse_forecast_amount_unit(self):
        entities = (
            VedurIsHomeWeatherEntity(self.coordinator),
            VedurIsPersonWeatherEntity(self.coordinator, "person.test"),
            VedurIsDeviceTrackerWeatherEntity(self.coordinator, "device_tracker.route"),
            VedurIsStationWeatherEntity(self.coordinator, self.station),
        )
        for entity in entities:
            entity.hass = self.hass
            attrs = entity.extra_state_attributes
            self.assertEqual(entity.native_precipitation_unit, "mm")
            self.assertNotIn("precipitation", attrs)
            self.assertEqual(attrs["precipitation_intensity"], 2.7)
            self.assertEqual(attrs["precipitation_intensity_unit"], "mm/h")
            self.assertEqual(attrs["precipitation_amount_unit"], "mm")
            self.assertEqual(attrs["observation_value_sources"]["r"], "gottvedur_is")
            self.assertEqual(
                attrs["observation_value_times"]["r"],
                utc_isoformat(self.observation.precipitation.end),
            )
            self.coordinator.data.observations[1474] = replace(
                self.observation, precipitation=None
            )
            self.assertIn(
                "r", entity.extra_state_attributes["observation_unavailable_fields"]
            )
            self.assertIsNone(entity.extra_state_attributes["precipitation_intensity"])
            self.coordinator.data.observations[1474] = self.observation

    async def test_old_mm_override_is_ignored_without_rewriting_saved_options(
        self,
    ):
        sensor = self.sensors()[0]
        entry = SimpleNamespace(options={"sensor": {"unit_of_measurement": "mm"}})
        sensor.registry_entry = entry
        self.assertIs(
            sensor._custom_unit_or_undef("sensor", "unit_of_measurement"), UNDEFINED
        )
        sensor._async_read_entity_options()
        self.assertIs(sensor._sensor_option_unit_of_measurement, UNDEFINED)
        self.assertEqual(sensor.native_unit_of_measurement, "mm/h")
        self.assertEqual(
            entry.options["sensor"]["unit_of_measurement"],
            "mm",
        )

    async def test_lookup_keeps_raw_official_data_but_exposes_explicit_hourly_measurement(
        self,
    ):
        response = _observation_response(self.observation)
        self.assertEqual(response["values"]["r"], 9)
        self.assertEqual(response["value_sources"]["r"], "official_api")
        self.assertEqual(response["precipitation_intensity"], 2.7)
        self.assertEqual(response["precipitation_amount_unit"], "mm")
