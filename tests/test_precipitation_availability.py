"""Availability is bounded by measurement time, not successful HTTP downloads."""

import asyncio
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.vedur_is.api import (
    CannotConnect,
    Observation,
    Station,
    StationForecast,
    VedurIsApiClient,
    merge_observation_fallback,
    parse_gottvedur_observation_payload,
)
from custom_components.vedur_is.observation_utils import (
    current_precipitation,
    measurement_value,
    precipitation_attributes,
)
from custom_components.vedur_is.polling import next_aligned_poll_delay
from custom_components.vedur_is.time_utils import utc_naive
from tests.test_api import FakeSession


def hourly_observation(time, amount=0, station_id=1470, **values):
    return parse_gottvedur_observation_payload(
        {
            "pageProps": {
                "latestObservation": {
                    "station": station_id,
                    "observationTime": time.isoformat(),
                    "precipitation": amount,
                    "temperature": 7,
                    "isManned": False,
                    **values,
                }
            },
        }
    )


class TestPrecipitationAvailability(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 10, 31, 12, 1, 15, tzinfo=timezone.utc)
        clock = patch("custom_components.vedur_is.api.datetime", wraps=datetime)
        self.clock = clock.start()
        self.clock.now.side_effect = lambda *_: self.now
        self.addCleanup(clock.stop)
        freshness = patch(
            "custom_components.vedur_is.observation_utils.utc_now",
            side_effect=lambda: utc_naive(self.now),
        )
        freshness.start()
        self.addCleanup(freshness.stop)
        self.client = VedurIsApiClient(FakeSession([]))
        self.client._async_get_gottvedur_latest_observation = AsyncMock(
            side_effect=lambda sid: hourly_observation(self.now, station_id=sid),
        )

    async def test_failed_refresh_keeps_fresh_zero_without_retiming_it(self):
        first = (await self.client.async_get_gottvedur_latest_observations([1470]))[
            1470
        ]
        self.now += timedelta(minutes=11)
        self.client._async_get_gottvedur_latest_observation.side_effect = CannotConnect(
            "offline"
        )
        cached = (await self.client.async_get_gottvedur_latest_observations([1470]))[
            1470
        ]
        self.assertEqual(cached.time, first.time)
        self.assertEqual(cached.precipitation, first.precipitation)
        self.assertEqual(
            cached.precipitation_fetched_at, first.precipitation_fetched_at
        )
        self.assertEqual(measurement_value(cached, "r"), 0)
        self.assertEqual(cached.precipitation_fetch_error, "offline")
        attrs = precipitation_attributes(cached)
        self.assertEqual(attrs["precipitation_status"], "available")
        self.assertEqual(attrs["precipitation_age_minutes"], 11)
        self.assertTrue(attrs["precipitation_using_cached_data"])

    async def test_normal_cache_hits_do_not_advance_successful_fetch_time(self):
        first = (await self.client.async_get_gottvedur_latest_observations([1470]))[
            1470
        ]
        self.now += timedelta(minutes=5)
        cached = (await self.client.async_get_gottvedur_latest_observations([1470]))[
            1470
        ]
        self.assertEqual(
            cached.precipitation_fetched_at, first.precipitation_fetched_at
        )
        self.client._async_get_gottvedur_latest_observation.assert_awaited_once()

    async def test_cancellation_is_not_reported_as_a_source_failure(self):
        self.client._async_get_gottvedur_latest_observation.side_effect = (
            asyncio.CancelledError()
        )
        with self.assertRaises(asyncio.CancelledError):
            await self.client.async_get_gottvedur_latest_observations([1470])
        self.assertEqual(self.client.gottvedur_observation_errors, {})

    async def test_station_retries_are_bounded_and_independent(self):
        self.client._async_get_gottvedur_latest_observation.side_effect = CannotConnect(
            "offline"
        )
        await self.client.async_get_gottvedur_latest_observations([1470])
        first_retry = self.client._gottvedur_observation_retry_at[1470]
        await self.client.async_get_gottvedur_latest_observations([1470])
        self.client._async_get_gottvedur_latest_observation.assert_awaited_once()
        for delay in (5, 10, 10):
            self.now = self.client._gottvedur_observation_retry_at[1470]
            await self.client.async_get_gottvedur_latest_observations([1470])
            self.assertEqual(
                self.client._gottvedur_observation_retry_at[1470] - self.now,
                timedelta(minutes=delay),
            )
        self.assertEqual(
            first_retry, datetime(2026, 10, 31, 12, 3, 15, tzinfo=timezone.utc)
        )
        self.client._async_get_gottvedur_latest_observation.side_effect = lambda sid: (
            hourly_observation(self.now, station_id=sid)
        )
        healthy = await self.client.async_get_gottvedur_latest_observations([1474])
        self.assertIn(1474, healthy)
        self.assertIn(1470, self.client.gottvedur_observation_errors)

    async def test_expired_cache_does_not_supply_numbers_during_an_outage(self):
        await self.client.async_get_gottvedur_latest_observations([1470])
        self.now += timedelta(minutes=31)
        self.client._async_get_gottvedur_latest_observation.side_effect = CannotConnect(
            "offline"
        )
        self.assertEqual(
            await self.client.async_get_gottvedur_latest_observations([1470]), {}
        )
        self.client._session.payload = [
            {"station": 1470, "time": self.now.isoformat(), "t": 8}
        ]
        merged = (
            await self.client.async_get_latest_observations(
                [1470], fallback_station_ids=[1470]
            )
        )[1470]
        self.assertEqual(merged.value("t"), 8)
        self.assertIsNone(measurement_value(merged, "r"))
        attrs = precipitation_attributes(merged)
        self.assertEqual(attrs["precipitation_status"], "stale")
        self.assertEqual(attrs["precipitation_fetch_error"], "offline")
        self.assertFalse(attrs["precipitation_using_cached_data"])

    async def test_new_null_replaces_old_non_null_and_clears_errors(self):
        await self.client.async_get_gottvedur_latest_observations([1470])
        self.now += timedelta(minutes=11)
        self.client._async_get_gottvedur_latest_observation.side_effect = CannotConnect(
            "offline"
        )
        await self.client.async_get_gottvedur_latest_observations([1470])
        self.now += timedelta(minutes=3)
        self.client._async_get_gottvedur_latest_observation.side_effect = lambda sid: (
            hourly_observation(self.now, None, sid)
        )
        observation = (
            await self.client.async_get_gottvedur_latest_observations([1470])
        )[1470]
        self.assertIsNone(measurement_value(observation, "r"))
        self.assertEqual(
            precipitation_attributes(observation)["precipitation_status"], "missing"
        )
        self.assertIsNone(observation.precipitation_fetch_error)
        self.assertEqual(self.client.gottvedur_observation_errors, {})
        self.assertNotIn(1470, self.client._gottvedur_observation_failures)

    async def test_successful_download_does_not_rejuvenate_an_old_measurement(self):
        end = self.now - timedelta(minutes=40)
        self.client._async_get_gottvedur_latest_observation.side_effect = lambda sid: (
            hourly_observation(end, station_id=sid)
        )
        observation = (
            await self.client.async_get_gottvedur_latest_observations([1470])
        )[1470]
        attrs = precipitation_attributes(observation)
        self.assertEqual(attrs["precipitation_status"], "stale")
        self.assertEqual(attrs["precipitation_age_minutes"], 40)
        self.assertEqual(observation.precipitation_fetched_at, self.now)
        self.assertIsNone(current_precipitation(observation))
        self.client._session.payload = []
        self.assertEqual(
            await self.client.async_get_latest_observations(
                [1470], fallback_station_ids=[1470]
            ),
            {},
        )

    async def test_missing_fallback_never_turns_official_r_into_intensity(self):
        self.client._async_get_gottvedur_latest_observation.side_effect = CannotConnect(
            "offline"
        )
        self.client._session.payload = [
            {"station": 1470, "time": self.now.isoformat(), "t": 8, "r": 9}
        ]
        observation = (
            await self.client.async_get_latest_observations(
                [1470], fallback_station_ids=[1470]
            )
        )[1470]
        self.assertIsNone(measurement_value(observation, "r"))
        self.assertEqual(
            precipitation_attributes(observation)["precipitation_status"],
            "source_error",
        )

    async def test_a_different_station_is_not_silently_substituted(self):
        self.client._async_get_gottvedur_latest_observation.side_effect = lambda sid: (
            hourly_observation(self.now, station_id=1)
        )
        self.assertEqual(
            await self.client.async_get_gottvedur_latest_observations([1470]), {}
        )
        self.assertIn(1470, self.client.gottvedur_observation_errors)

    async def test_missing_source_timestamp_is_not_a_successful_measurement(self):
        self.client._async_get_gottvedur_latest_observation.side_effect = lambda sid: (
            Observation.from_api({"station": sid, "t": 7})
        )
        self.assertEqual(
            await self.client.async_get_gottvedur_latest_observations([1470]), {}
        )
        self.assertIn(1470, self.client.gottvedur_observation_errors)

    async def test_fresh_precipitation_error_context_survives_the_official_merge(self):
        await self.client.async_get_gottvedur_latest_observations([1470])
        self.now += timedelta(minutes=11)
        self.client._async_get_gottvedur_latest_observation.side_effect = CannotConnect(
            "offline"
        )
        self.client._session.payload = [
            {"station": 1470, "time": self.now.isoformat(), "t": 8}
        ]
        observation = (
            await self.client.async_get_latest_observations(
                [1470], fallback_station_ids=[1470]
            )
        )[1470]
        self.assertEqual(measurement_value(observation, "r"), 0)
        self.assertEqual(observation.value("t"), 8)
        self.assertEqual(observation.precipitation_fetch_error, "offline")
        self.assertEqual(
            precipitation_attributes(observation)["precipitation_age_minutes"], 11
        )

    async def test_future_clock_errors_are_not_current_precipitation(self):
        observation = hourly_observation(self.now + timedelta(minutes=6))
        self.assertEqual(
            precipitation_attributes(observation)["precipitation_status"],
            "future_timestamp",
        )
        self.assertIsNone(measurement_value(observation, "r"))

    async def test_old_numeric_fallback_cannot_borrow_a_fresh_official_time(self):
        official = Observation.from_api(
            {"station": 1470, "time": self.now.isoformat(), "t": None}
        )
        fallback = hourly_observation(self.now - timedelta(minutes=40))
        self.assertIsNone(merge_observation_fallback(official, fallback).value("t"))

    async def test_fresh_fallback_is_not_rejected_when_official_observation_lags(self):
        primary = Observation.from_api(
            {
                "station": 1470,
                "time": (self.now - timedelta(minutes=20)).isoformat(),
                "t": 5,
                "rh": None,
            }
        )
        fallback = hourly_observation(self.now, humidity=75)
        merged = merge_observation_fallback(primary, fallback)
        self.assertEqual(merged.value("rh"), 75)
        self.assertEqual(merged.value_time("rh"), fallback.time)
        self.assertEqual(merged.value("t"), 5)
        self.assertEqual(merged.time, primary.time)

    async def test_old_fallback_is_not_accepted_against_an_old_primary_clock(self):
        primary = Observation.from_api(
            {
                "station": 1470,
                "time": (self.now - timedelta(minutes=40)).isoformat(),
                "rh": None,
            }
        )
        fallback = hourly_observation(self.now - timedelta(minutes=45), humidity=75)
        self.assertIsNone(merge_observation_fallback(primary, fallback).value("rh"))

    async def test_future_fallback_is_checked_against_utc_not_primary_clock(self):
        primary = Observation.from_api(
            {
                "station": 1470,
                "time": (self.now + timedelta(minutes=5)).isoformat(),
                "rh": None,
            }
        )
        fallback = hourly_observation(self.now + timedelta(minutes=6), humidity=75)
        self.assertIsNone(merge_observation_fallback(primary, fallback).value("rh"))

    async def test_metadata_is_retained_without_exposing_rejected_numeric_values(self):
        self.client._async_get_gottvedur_latest_observation.side_effect = lambda sid: (
            hourly_observation(
                self.now - timedelta(minutes=40),
                station_id=sid,
            )
        )
        observations = await self.client.async_get_latest_observations(
            [1470], fallback_station_ids=[1470]
        )
        self.assertEqual(observations, {})
        record = self.client.gottvedur_observation_metadata[1470]
        self.assertEqual(record.values, {})
        self.assertIsNone(record.value("t"))
        self.assertEqual(record.precipitation_fetched_at, self.now)
        self.assertEqual(
            precipitation_attributes(record)["precipitation_status"], "stale"
        )
        self.assertIsNone(measurement_value(record, "r"))


HAS_HOME_ASSISTANT = find_spec("homeassistant") is not None
if HAS_HOME_ASSISTANT:
    from homeassistant.core import HomeAssistant
    from custom_components.vedur_is.weather_coordinator import (
        SOURCE_FORECASTS,
        SOURCE_OBSERVATIONS,
        SOURCE_PRECIPITATION,
        SOURCE_STATIONS,
        VedurIsWeatherData,
        VedurIsWeatherDataUpdateCoordinator,
    )


@unittest.skipUnless(HAS_HOME_ASSISTANT, "Requires the Home Assistant runtime")
class TestIndependentSources(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        for module in ("api", "weather_coordinator"):
            clock = patch(
                f"custom_components.vedur_is.{module}.datetime", wraps=datetime
            )
            mocked = clock.start()
            mocked.now.side_effect = lambda *_: self.now
            self.addCleanup(clock.stop)
        freshness = patch(
            "custom_components.vedur_is.observation_utils.utc_now",
            side_effect=lambda: utc_naive(self.now),
        )
        freshness.start()
        self.addCleanup(freshness.stop)
        self.hass = HomeAssistant("/tmp/vedur-source-health-test")
        self.station = Station(
            1470, "Reykjavik", None, "sj", 64.1, -21.9, None, None, None
        )
        self.entry = SimpleNamespace(
            entry_id="test",
            options={},
            data={"station_ids": [1470]},
            async_on_unload=lambda callback: None,
        )
        self.observation = Observation.from_api(
            {"station": 1470, "time": self.now.isoformat(), "t": 8}
        )
        self.client = SimpleNamespace(
            async_get_stations=AsyncMock(return_value=[self.station]),
            async_get_latest_observations=AsyncMock(
                return_value={1470: self.observation}
            ),
            async_get_forecasts=AsyncMock(
                return_value={1470: StationForecast(1470, "Reykjavik", None, None, ())}
            ),
            async_enrich_forecast_precipitation=AsyncMock(
                side_effect=lambda forecasts, ids: forecasts
            ),
            gottvedur_observation_errors={},
            gottvedur_observation_metadata={},
        )
        self.coordinator = VedurIsWeatherDataUpdateCoordinator(
            self.hass, self.client, self.entry
        )
        self.coordinator.data = VedurIsWeatherData(
            {1470: self.station}, {1470: self.observation}, {}
        )
        self.addAsyncCleanup(self.coordinator.async_shutdown)

    async def test_forecast_backoff_does_not_delay_healthy_observations(self):
        now = self.now
        for _ in range(8):
            self.coordinator._source_failed(
                SOURCE_FORECASTS, CannotConnect("forecast offline")
            )
        self.coordinator._schedule_next_update(now)
        self.assertEqual(self.coordinator.update_interval, next_aligned_poll_delay(now))
        errors, stale = {}, set()
        observations = await self.coordinator._async_get_observations(
            [1470], {1470}, self.coordinator.data, errors, stale, now=now
        )
        self.assertIn(1470, observations)
        self.client.async_get_latest_observations.assert_awaited_once()
        self.assertNotIn(SOURCE_OBSERVATIONS, errors)
        self.assertIn(SOURCE_FORECASTS, self.coordinator._source_retry_at)

    async def test_forecast_retry_wakeup_does_not_repoll_healthy_observations(self):
        now = self.now
        await self.coordinator._async_get_observations(
            [1470], {1470}, self.coordinator.data, {}, set(), now=now
        )
        await self.coordinator._async_get_observations(
            [1470],
            {1470},
            self.coordinator.data,
            {},
            set(),
            now=now + timedelta(seconds=1),
        )
        self.client.async_get_latest_observations.assert_awaited_once()

    async def test_precipitation_failure_is_visible_without_failing_numeric_weather(
        self,
    ):
        self.client.gottvedur_observation_errors = {1470: "offline"}
        data = await self.coordinator._async_update_data()
        self.assertEqual(data.observations[1470].value("t"), 8)
        self.assertIn(SOURCE_PRECIPITATION, data.stale_sources)
        self.assertIn("1470", data.source_errors[SOURCE_PRECIPITATION])
        self.assertNotIn(SOURCE_OBSERVATIONS, data.source_errors)
        self.assertEqual(self.coordinator._source_failure_counts, {})
        self.assertEqual(
            data.precipitation_diagnostics[1470]["precipitation_status"], "source_error"
        )
        self.assertEqual(
            data.precipitation_diagnostics[1470]["precipitation_fetch_error"], "offline"
        )

    async def test_successful_but_old_precipitation_is_marked_stale(self):
        observation = hourly_observation(self.now - timedelta(minutes=40))
        self.client.async_get_latest_observations.return_value = {1470: observation}
        data = await self.coordinator._async_update_data()
        self.assertIn(SOURCE_PRECIPITATION, data.stale_sources)
        self.assertIn("timestamp", data.source_errors[SOURCE_PRECIPITATION])

    async def test_station_metadata_backoff_does_not_skip_cached_metadata_or_observations(
        self,
    ):
        now = self.now
        self.coordinator._source_failed(SOURCE_STATIONS, CannotConnect("offline"))
        errors, stale = {}, set()
        stations = await self.coordinator._async_get_stations(
            now, self.coordinator.data, errors, stale
        )
        self.assertIn(1470, stations)
        self.client.async_get_stations.assert_not_awaited()
        self.assertIn(SOURCE_STATIONS, stale)
        self.assertTrue(
            self.coordinator._source_ready(SOURCE_OBSERVATIONS, now, errors, stale)
        )

    async def test_recovery_clears_only_the_recovered_source(self):
        for source in (SOURCE_STATIONS, SOURCE_FORECASTS):
            self.coordinator._source_failed(source, CannotConnect("offline"))
        self.coordinator._source_succeeded(SOURCE_FORECASTS)
        self.assertNotIn(SOURCE_FORECASTS, self.coordinator._source_retry_at)
        self.assertNotIn(SOURCE_FORECASTS, self.coordinator._source_errors)
        self.assertNotIn(SOURCE_FORECASTS, self.coordinator._source_failure_counts)
        self.assertIn(SOURCE_STATIONS, self.coordinator._source_retry_at)

    async def test_official_failure_does_not_block_forecasts_and_retries_are_throttled(
        self,
    ):
        self.client.async_get_latest_observations.side_effect = CannotConnect("offline")
        data = await self.coordinator._async_update_data()
        self.assertIn(1470, data.forecasts)
        self.assertIn(SOURCE_OBSERVATIONS, data.stale_sources)
        self.assertNotIn(SOURCE_FORECASTS, data.stale_sources)
        self.client.async_get_forecasts.assert_awaited_once()
        await self.coordinator._async_update_data()
        self.client.async_get_latest_observations.assert_awaited_once()
        self.assertEqual(
            self.coordinator._source_failure_counts[SOURCE_OBSERVATIONS], 1
        )

    async def test_poll_boundary_is_crossed_only_when_an_update_is_due(self):
        for microsecond, expected_fetches in ((750000, 2), (250000, 1)):
            with self.subTest(microsecond=microsecond):
                second = 14 if expected_fetches == 2 else 15
                self.now = datetime(
                    2026, 10, 31, 12, 1, second, microsecond, tzinfo=timezone.utc
                )
                self.coordinator._observations_next_update = None
                self.client.async_get_latest_observations.reset_mock()
                await self.coordinator._async_get_observations(
                    [1470], {1470}, self.coordinator.data, {}, set(), now=self.now
                )
                self.now += timedelta(seconds=1)
                await self.coordinator._async_get_observations(
                    [1470], {1470}, self.coordinator.data, {}, set(), now=self.now
                )
                self.assertEqual(
                    self.client.async_get_latest_observations.await_count,
                    expected_fetches,
                )

    async def test_retry_delays_start_when_each_failed_request_completes(self):
        async def delayed_failure(*args, **kwargs):
            self.now += timedelta(minutes=10)
            raise CannotConnect("offline")

        operations = (
            (
                SOURCE_STATIONS,
                self.client.async_get_stations,
                lambda start: self.coordinator._async_get_stations(
                    start, self.coordinator.data, {}, set()
                ),
            ),
            (
                SOURCE_OBSERVATIONS,
                self.client.async_get_latest_observations,
                lambda start: self.coordinator._async_get_observations(
                    [1470], {1470}, self.coordinator.data, {}, set(), now=start
                ),
            ),
            (
                SOURCE_FORECASTS,
                self.client.async_get_forecasts,
                lambda start: self.coordinator._async_get_forecasts(
                    start, {1470: self.station}, self.coordinator.data, {}, set()
                ),
            ),
        )
        with patch(
            "custom_components.vedur_is.weather_coordinator.uniform", return_value=0
        ):
            for source, fetch, operation in operations:
                with self.subTest(source=source):
                    start = self.now
                    fetch.side_effect = delayed_failure
                    await operation(start)
                    self.assertEqual(
                        self.coordinator._source_retry_at[source],
                        self.now + timedelta(minutes=2),
                    )
                    self.assertFalse(
                        self.coordinator._source_ready(
                            source, self.now + timedelta(seconds=119), {}, set()
                        )
                    )
                    self.assertTrue(
                        self.coordinator._source_ready(
                            source, self.now + timedelta(minutes=2), {}, set()
                        )
                    )

    async def test_stale_source_diagnostics_survive_an_absent_primary_row(self):
        client = VedurIsApiClient(FakeSession([]))
        client._async_get_gottvedur_latest_observation = AsyncMock(
            return_value=hourly_observation(self.now - timedelta(minutes=40))
        )
        client.async_get_stations = self.client.async_get_stations
        client.async_get_forecasts = self.client.async_get_forecasts
        client.async_enrich_forecast_precipitation = (
            self.client.async_enrich_forecast_precipitation
        )
        self.coordinator.client = client
        data = await self.coordinator._async_update_data()
        self.assertEqual(data.observations, {})
        self.assertTrue(data.data_stale)
        self.assertIn(SOURCE_PRECIPITATION, data.source_errors)
        self.assertIsNone(data.last_successful_update)
        detail = data.precipitation_diagnostics[1470]
        self.assertEqual(detail["precipitation_status"], "stale")
        self.assertEqual(detail["precipitation_age_minutes"], 40)
        self.assertIsNotNone(detail["precipitation_last_successful_fetch"])
        self.assertIsNotNone(detail["precipitation_period_end"])
        self.assertNotIn("precipitation_intensity", detail)
        from custom_components.vedur_is.sensor import (
            _data_diagnostics as sensor_diagnostics,
        )
        from custom_components.vedur_is.weather import (
            _data_diagnostics as weather_diagnostics,
        )

        for render in (sensor_diagnostics, weather_diagnostics):
            self.assertEqual(render(data)["precipitation_diagnostics"]["1470"], detail)


if __name__ == "__main__":
    unittest.main()
