"""Availability is bounded by measurement time, not successful HTTP downloads."""

import asyncio
from dataclasses import replace
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

    async def test_scheduled_poll_refreshes_cache_despite_late_previous_response(self):
        self.now = datetime(2026, 11, 1, 4, 21, 18, 400000, tzinfo=timezone.utc)
        first = (
            await self.client.async_get_latest_observations(
                [1470], fallback_station_ids=[1470], refresh_fallback=True
            )
        )[1470]
        self.now = datetime(2026, 11, 1, 4, 31, 15, tzinfo=timezone.utc)
        second = (
            await self.client.async_get_latest_observations(
                [1470], fallback_station_ids=[1470], refresh_fallback=True
            )
        )[1470]
        self.assertNotEqual(
            first.precipitation_fetched_at, second.precipitation_fetched_at
        )
        self.assertEqual(
            self.client._async_get_gottvedur_latest_observation.await_count, 2
        )
        await self.client.async_get_gottvedur_latest_observations([1470])
        self.assertEqual(
            self.client._async_get_gottvedur_latest_observation.await_count, 2
        )

    async def test_scheduled_refresh_does_not_bypass_failure_backoff(self):
        first = (await self.client.async_get_gottvedur_latest_observations([1470]))[
            1470
        ]
        self.now += timedelta(minutes=5)
        self.client._async_get_gottvedur_latest_observation.side_effect = CannotConnect(
            "offline"
        )
        await self.client.async_get_gottvedur_latest_observations([1470], refresh=True)
        self.now += timedelta(seconds=1)
        retained = (
            await self.client.async_get_gottvedur_latest_observations(
                [1470], refresh=True
            )
        )[1470]
        self.assertEqual(
            self.client._async_get_gottvedur_latest_observation.await_count, 2
        )
        self.assertEqual(
            retained.precipitation_fetched_at, first.precipitation_fetched_at
        )
        self.assertEqual(retained.precipitation.end, first.precipitation.end)
        self.assertTrue(
            precipitation_attributes(retained)["precipitation_using_cached_data"]
        )

    async def test_scheduled_null_replaces_a_still_cached_positive_value(self):
        self.client._async_get_gottvedur_latest_observation.side_effect = lambda sid: (
            hourly_observation(self.now, amount=1.1, station_id=sid)
        )
        await self.client.async_get_gottvedur_latest_observations([1470])
        self.now += timedelta(minutes=5)
        self.client._async_get_gottvedur_latest_observation.side_effect = lambda sid: (
            hourly_observation(self.now, amount=None, station_id=sid)
        )
        result = (
            await self.client.async_get_gottvedur_latest_observations(
                [1470], refresh=True
            )
        )[1470]
        self.assertIsNone(measurement_value(result, "r"))
        self.assertEqual(
            precipitation_attributes(result)["precipitation_status"], "missing"
        )

    async def test_reading_expires_at_its_thirty_minute_deadline(self):
        observation = hourly_observation(self.now, amount=0.2)
        self.now += timedelta(minutes=30, microseconds=-1)
        self.assertEqual(measurement_value(observation, "r"), 0.2)
        self.now += timedelta(microseconds=1)
        self.assertIsNone(measurement_value(observation, "r"))
        self.assertEqual(
            precipitation_attributes(observation)["precipitation_status"], "stale"
        )

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
        for module in ("api", "weather_coordinator", "observation_utils"):
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
        self.expiry_timers = []

        def register_timer(hass, action, deadline):
            timer = SimpleNamespace(action=action, deadline=deadline, cancelled=False)
            self.expiry_timers.append(timer)

            def cancel():
                timer.cancelled = True

            return cancel

        timers = patch(
            "custom_components.vedur_is.weather_coordinator.async_track_point_in_utc_time",
            side_effect=register_timer,
        )
        timers.start()
        self.addCleanup(timers.stop)

        async def advance_sleep(seconds):
            self.now += timedelta(seconds=seconds)

        sleeping = patch(
            "custom_components.vedur_is.weather_coordinator.sleep",
            side_effect=advance_sleep,
        )
        self.sleep = sleeping.start()
        self.addCleanup(sleeping.stop)
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

    def _publish_reading(self, *, age_minutes=20, amount=0.2, error=None):
        primary = Observation.from_api(
            {"station": 1470, "time": self.now.isoformat(), "t": 8, "f": 4}
        )
        source = replace(
            hourly_observation(
                self.now - timedelta(minutes=age_minutes), amount=amount
            ),
            precipitation_fetched_at=self.now,
            precipitation_fetch_error=error,
        )
        observation = merge_observation_fallback(primary, source, now=self.now)
        metadata = {1470: replace(source, values={}, value_times={}, value_sources={})}
        errors, stale = {}, set()
        diagnostics = self.coordinator._precipitation_diagnostics(
            {1470: observation},
            {1470},
            errors,
            stale,
            source_metadata=metadata,
            fetch_errors={1470: error} if error else {},
        )
        self.coordinator.data = VedurIsWeatherData(
            {1470: self.station},
            {1470: observation},
            {},
            source_errors=errors,
            stale_sources=frozenset(stale),
            last_successful_update=self.now,
            precipitation_diagnostics=diagnostics,
            precipitation_metadata=metadata,
        )
        self.coordinator._schedule_precipitation_expiry(self.coordinator.data)
        return observation

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

    async def test_early_wakeup_waits_and_publishes_only_the_completed_refresh(self):
        self.now = datetime(2026, 11, 1, 4, 41, 14, 900000, tzinfo=timezone.utc)
        self._publish_reading(age_minutes=31)
        deadline = self.now + timedelta(seconds=0.1)
        self.coordinator._observations_next_update = deadline
        seen = []

        async def fetch(*args, **kwargs):
            self.assertEqual(self.now, deadline)
            self.assertEqual(seen, [])
            self.assertTrue(kwargs["refresh_fallback"])
            self.now += timedelta(seconds=3)
            return {
                1470: hourly_observation(self.now - timedelta(minutes=11), amount=0.4)
            }

        self.client.async_get_latest_observations.side_effect = fetch
        with patch.object(
            self.coordinator,
            "async_update_listeners",
            side_effect=lambda: seen.append(
                measurement_value(self.coordinator.data.observations[1470], "r")
            ),
        ):
            await self.coordinator.async_refresh()
        self.sleep.assert_awaited_once_with(0.1)
        self.assertEqual(seen, [0.4])
        self.client.async_get_latest_observations.assert_awaited_once()
        self.assertTrue(self.coordinator.last_update_success)

    async def test_early_wakeup_cancellation_does_not_fetch(self):
        self.coordinator._observations_next_update = self.now + timedelta(seconds=0.1)
        self.sleep.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.coordinator._async_get_observations(
                [1470], {1470}, self.coordinator.data, {}, set()
            )
        self.client.async_get_latest_observations.assert_not_awaited()

    async def test_expiry_updates_real_entity_properties_without_http_or_retiming(self):
        from custom_components.vedur_is.sensor import SENSOR_DESCRIPTIONS, VedurIsSensor
        from custom_components.vedur_is.weather import VedurIsStationWeatherEntity

        observation = self._publish_reading()
        sensor = VedurIsSensor(
            self.coordinator,
            self.station,
            next(item for item in SENSOR_DESCRIPTIONS if item.key == "r"),
        )
        weather = VedurIsStationWeatherEntity(self.coordinator, self.station)
        sensor.hass = weather.hass = self.hass
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 0.2)
        original_success = self.coordinator.data.last_successful_update
        self.coordinator.update_interval = timedelta(minutes=7)
        next_poll = self.now + timedelta(minutes=3)
        self.coordinator._observations_next_update = next_poll
        self.coordinator._source_retry_at[SOURCE_FORECASTS] = self.now + timedelta(
            minutes=5
        )
        retries = dict(self.coordinator._source_retry_at)
        timer = self.expiry_timers[-1]
        self.assertEqual(timer.deadline, self.now + timedelta(minutes=10))
        self.now = timer.deadline
        seen = []
        with patch.object(
            self.coordinator,
            "async_update_listeners",
            side_effect=lambda: seen.append(
                (
                    sensor.available,
                    weather.native_temperature,
                    weather.extra_state_attributes["precipitation_status"],
                )
            ),
        ):
            timer.action(self.now)
        self.assertEqual(seen, [(False, 8, "stale")])
        self.assertIsNone(sensor.native_value)
        self.assertIs(self.coordinator.data.observations[1470], observation)
        self.assertEqual(self.coordinator.data.last_successful_update, original_success)
        self.assertEqual(
            self.coordinator.data.observations[1470].precipitation_fetched_at,
            observation.precipitation_fetched_at,
        )
        self.assertEqual(self.coordinator.update_interval, timedelta(minutes=7))
        self.assertEqual(self.coordinator._observations_next_update, next_poll)
        self.assertEqual(self.coordinator._source_retry_at, retries)
        self.client.async_get_latest_observations.assert_not_awaited()
        self.client.async_get_forecasts.assert_not_awaited()
        self.assertIsNone(self.coordinator._precipitation_expiry_at)

    async def test_new_reading_cancels_old_deadline_and_ignores_queued_old_callback(
        self,
    ):
        self._publish_reading()
        old = self.expiry_timers[-1]
        self.now += timedelta(minutes=5)
        self._publish_reading(age_minutes=0, amount=0.7)
        new = self.expiry_timers[-1]
        self.assertTrue(old.cancelled)
        self.assertGreater(new.deadline, old.deadline)
        self.now = old.deadline
        with patch.object(self.coordinator, "async_update_listeners") as notify:
            old.action(self.now)
        notify.assert_not_called()
        self.assertEqual(
            measurement_value(self.coordinator.data.observations[1470], "r"), 0.7
        )
        self.assertEqual(self.coordinator._precipitation_expiry_at, new.deadline)

    async def test_repeated_window_does_not_postpone_expiry_to_download_time(self):
        self._publish_reading()
        timer = self.expiry_timers[-1]
        self.now += timedelta(minutes=5)
        observation = replace(
            self.coordinator.data.observations[1470], precipitation_fetched_at=self.now
        )
        self.coordinator.data = replace(
            self.coordinator.data, observations={1470: observation}
        )
        self.coordinator._schedule_precipitation_expiry(self.coordinator.data)
        self.assertEqual(len(self.expiry_timers), 1)
        self.assertEqual(self.coordinator._precipitation_expiry_at, timer.deadline)

    async def test_null_reading_cancels_previous_window_deadline(self):
        self._publish_reading()
        timer = self.expiry_timers[-1]
        self._publish_reading(age_minutes=0, amount=None)
        self.assertTrue(timer.cancelled)
        self.assertIsNone(self.coordinator._precipitation_expiry_at)
        self.assertIsNone(
            measurement_value(self.coordinator.data.observations[1470], "r")
        )

    async def test_expiry_keeps_error_context_and_other_source_health(self):
        observation = self._publish_reading(error="offline")
        self.coordinator.data = replace(
            self.coordinator.data,
            source_errors={
                **self.coordinator.data.source_errors,
                SOURCE_FORECASTS: "forecast offline",
            },
            stale_sources=frozenset({SOURCE_PRECIPITATION, SOURCE_FORECASTS}),
        )
        timer = self.expiry_timers[-1]
        self.now = timer.deadline
        timer.action(self.now)
        details = self.coordinator.data.precipitation_diagnostics[1470]
        self.assertEqual(details["precipitation_status"], "stale")
        self.assertEqual(details["precipitation_fetch_error"], "offline")
        self.assertFalse(details["precipitation_using_cached_data"])
        self.assertEqual(
            self.coordinator.data.source_errors[SOURCE_FORECASTS], "forecast offline"
        )
        self.assertEqual(self.coordinator.data.observations[1470], observation)
        self.assertIn(
            "offline", self.coordinator.data.source_errors[SOURCE_PRECIPITATION]
        )

    async def test_expiry_does_not_peek_at_new_unpublished_client_cache(self):
        old = self._publish_reading()
        timer = self.expiry_timers[-1]
        self.client.gottvedur_observation_metadata = {
            1470: hourly_observation(self.now, amount=2)
        }
        self.now = timer.deadline
        timer.action(self.now)
        self.assertIs(self.coordinator.data.observations[1470], old)
        self.assertIsNone(measurement_value(old, "r"))
        self.assertEqual(
            self.coordinator.data.precipitation_diagnostics[1470][
                "precipitation_status"
            ],
            "stale",
        )
        self.assertEqual(
            self.coordinator.data.precipitation_metadata[1470].precipitation.end,
            old.precipitation.end,
        )

    async def test_expiry_schedules_next_unexpired_station_without_a_busy_loop(self):
        self._publish_reading()
        first = self.expiry_timers[-1]
        second = hourly_observation(self.now, amount=0.1, station_id=1474)
        self.coordinator.data = replace(
            self.coordinator.data,
            observations={**self.coordinator.data.observations, 1474: second},
        )
        self.coordinator._schedule_precipitation_expiry(self.coordinator.data)
        self.now = first.deadline
        first.action(self.now)
        next_timer = self.expiry_timers[-1]
        self.assertGreater(next_timer.deadline, first.deadline)
        self.assertEqual(next_timer.deadline, self.now + timedelta(minutes=20))
        self.now = next_timer.deadline
        next_timer.action(self.now)
        self.assertIsNone(self.coordinator._precipitation_expiry_at)
        self.assertEqual(len(self.expiry_timers), 2)

    async def test_shutdown_cancels_expiry_and_late_refresh_cannot_restart_it(self):
        self._publish_reading()
        timer = self.expiry_timers[-1]
        await self.coordinator.async_shutdown()
        self.assertTrue(timer.cancelled)
        self.now = timer.deadline
        with patch.object(self.coordinator, "async_update_listeners") as notify:
            timer.action(self.now)
            self.coordinator._schedule_precipitation_expiry(self.coordinator.data)
        notify.assert_not_called()
        self.assertIsNone(self.coordinator._precipitation_expiry_at)
        self.assertEqual(len(self.expiry_timers), 1)

    async def test_genuine_expiry_during_slow_refresh_is_not_hidden(self):
        self._publish_reading(age_minutes=29)
        timer = self.expiry_timers[-1]
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_fetch(*args, **kwargs):
            started.set()
            await release.wait()
            return {1470: hourly_observation(self.now, amount=0.3)}

        self.client.async_get_latest_observations.side_effect = slow_fetch
        seen = []
        with patch.object(
            self.coordinator,
            "async_update_listeners",
            side_effect=lambda: seen.append(
                measurement_value(self.coordinator.data.observations[1470], "r")
            ),
        ):
            task = asyncio.create_task(self.coordinator.async_refresh())
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                self.now = timer.deadline
                timer.action(self.now)
                self.assertEqual(seen, [None])
            finally:
                release.set()
                await task
        self.assertEqual(seen, [None, 0.3])
        self.client.async_get_latest_observations.assert_awaited_once()


@unittest.skipUnless(HAS_HOME_ASSISTANT, "Requires the Home Assistant runtime")
class TestRealExpiryTimer(unittest.IsolatedAsyncioTestCase):
    async def test_real_ha_timer_expires_data_without_resetting_polling(self):
        hass = HomeAssistant("/tmp/vedur-expiry-timer-test")
        client = SimpleNamespace(async_get_latest_observations=AsyncMock())
        entry = SimpleNamespace(
            entry_id="test", data={}, options={}, async_on_unload=lambda callback: None
        )
        coordinator = VedurIsWeatherDataUpdateCoordinator(hass, client, entry)
        self.addAsyncCleanup(coordinator.async_shutdown)
        now = datetime.now(timezone.utc)
        observation = replace(
            hourly_observation(now - timedelta(minutes=30) + timedelta(seconds=1)),
            precipitation_fetched_at=now,
        )
        coordinator.data = VedurIsWeatherData(
            {}, {1470: observation}, {}, last_successful_update=now
        )
        interval = coordinator.update_interval
        expired = asyncio.Event()
        with patch.object(
            coordinator, "async_update_listeners", side_effect=expired.set
        ):
            coordinator._schedule_precipitation_expiry(coordinator.data)
            await asyncio.wait_for(expired.wait(), timeout=5)
        self.assertIsNone(measurement_value(coordinator.data.observations[1470], "r"))
        self.assertEqual(
            coordinator.data.precipitation_diagnostics[1470]["precipitation_status"],
            "stale",
        )
        self.assertEqual(coordinator.data.last_successful_update, now)
        self.assertEqual(coordinator.update_interval, interval)
        client.async_get_latest_observations.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
