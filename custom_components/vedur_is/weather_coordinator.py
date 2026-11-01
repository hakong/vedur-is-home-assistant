"""Weather data coordinator for person-following Vedur.is weather entities."""

from __future__ import annotations

from asyncio import sleep
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import logging
from random import uniform
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    CannotConnect,
    InvalidResponse,
    Observation,
    Station,
    StationForecast,
    VedurIsApiClient,
)
from .const import (
    CONF_DEVICE_TRACKER_ENTITY_IDS,
    CONF_ENABLE_DEVICE_TRACKER_WEATHER,
    CONF_ENABLE_HOME_WEATHER,
    CONF_ENABLE_PERSON_WEATHER,
    CONF_ENABLE_STATION_WEATHER,
    CONF_STATION_IDS,
    CONF_STATIONS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from .geo import (
    Coordinate,
    nearest_station,
    resolve_person_coordinate,
    resolve_tracker_coordinate,
)
from .polling import (
    MAX_BACKOFF_JITTER_SECONDS,
    backoff_poll_delay,
    next_aligned_poll_delay,
)
from .observation_utils import (
    MAX_PRECIPITATION_AGE,
    current_precipitation,
    precipitation_attributes,
)
from .time_utils import utc_naive

_LOGGER = logging.getLogger(__name__)
MAX_FALLBACK_OBSERVATION_DISTANCE_KM = 150.0
SOURCE_FORECASTS = "forecasts"
SOURCE_OBSERVATIONS = "observations"
SOURCE_PRECIPITATION = "precipitation"
SOURCE_STATIONS = "stations"
STATION_CACHE_TTL = timedelta(hours=12)
FORECAST_CACHE_TTL = timedelta(hours=1)
EARLY_POLL_TOLERANCE = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class VedurIsWeatherData:
    """All station data needed by person-following weather entities."""

    stations: dict[int, Station]
    observations: dict[int, Observation]
    forecasts: dict[int, StationForecast]
    stale_sources: frozenset[str] = frozenset()
    source_errors: Mapping[str, str] = field(default_factory=dict)
    last_successful_update: datetime | None = None
    precipitation_diagnostics: Mapping[int, Mapping[str, Any]] = field(
        default_factory=dict
    )
    precipitation_metadata: Mapping[int, Observation] = field(default_factory=dict)

    @property
    def data_stale(self) -> bool:
        """Return whether any source is using stale fallback data."""
        return bool(self.stale_sources)


class VedurIsWeatherDataUpdateCoordinator(DataUpdateCoordinator[VedurIsWeatherData]):
    """Coordinate station, observation, and forecast updates for weather entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: VedurIsApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN}_weather",
            config_entry=entry,
            update_interval=timedelta(minutes=DEFAULT_SCAN_INTERVAL_MINUTES),
        )
        self.client = client
        self._source_failure_counts: dict[str, int] = {}
        self._source_retry_at: dict[str, datetime] = {}
        self._source_errors: dict[str, str] = {}
        self._observations_next_update: datetime | None = None
        self._stations_cache: dict[int, Station] | None = None
        self._stations_fetched_at: datetime | None = None
        self._forecasts_cache: dict[int, StationForecast] | None = None
        self._forecasts_fetched_at: datetime | None = None
        self._precipitation_expiry_unsub: Callable[[], None] | None = None
        self._precipitation_expiry_at: datetime | None = None
        self._precipitation_stopped = False

    async def _async_update_data(self) -> VedurIsWeatherData:
        """Fetch the latest full weather data set from vedur.is."""
        now = datetime.now(timezone.utc)
        previous = self.data
        source_errors: dict[str, str] = {}
        stale_sources: set[str] = set()

        stations_by_id = await self._async_get_stations(
            now,
            previous,
            source_errors,
            stale_sources,
        )

        aws_station_ids = [
            station.station_id
            for station in stations_by_id.values()
            if station.station_type and station.station_type.casefold() == "sj"
        ]
        fallback_station_ids = self._fallback_station_ids(
            stations_by_id,
            aws_station_ids,
        )
        observations = await self._async_get_observations(
            aws_station_ids,
            fallback_station_ids,
            previous,
            source_errors,
            stale_sources,
        )
        precipitation_metadata = {
            station_id: observation
            for station_id, observation in self.client.gottvedur_observation_metadata.items()
            if station_id in fallback_station_ids
        }
        precipitation_errors = dict(self.client.gottvedur_observation_errors)
        forecasts = await self._async_get_forecasts(
            datetime.now(timezone.utc),
            stations_by_id,
            previous,
            source_errors,
            stale_sources,
        )
        forecasts = await self.client.async_enrich_forecast_precipitation(
            forecasts,
            self._precipitation_forecast_station_ids(stations_by_id, forecasts),
        )

        precipitation_diagnostics = self._precipitation_diagnostics(
            observations,
            fallback_station_ids,
            source_errors,
            stale_sources,
            source_metadata=precipitation_metadata,
            fetch_errors=precipitation_errors,
        )

        self._schedule_next_update(datetime.now(timezone.utc))
        last_successful_update = (
            now
            if not source_errors
            else previous.last_successful_update
            if previous is not None
            else None
        )

        _LOGGER.debug(
            "Fetched Vedur.is weather data: %s stations, %s observations, "
            "%s forecast stations, stale sources: %s",
            len(stations_by_id),
            len(observations),
            len(forecasts),
            sorted(stale_sources),
        )
        data = VedurIsWeatherData(
            stations=stations_by_id,
            observations=observations,
            forecasts=forecasts,
            stale_sources=frozenset(stale_sources),
            source_errors=source_errors,
            last_successful_update=last_successful_update,
            precipitation_diagnostics=precipitation_diagnostics,
            precipitation_metadata=precipitation_metadata,
        )
        self._schedule_precipitation_expiry(data)
        return data

    async def async_shutdown(self) -> None:
        """Cancel local expiry notifications as well as normal coordinator work."""
        self._precipitation_stopped = True
        self._cancel_precipitation_expiry()
        await super().async_shutdown()

    @callback
    def _cancel_precipitation_expiry(self) -> None:
        if self._precipitation_expiry_unsub is not None:
            self._precipitation_expiry_unsub()
            self._precipitation_expiry_unsub = None
        self._precipitation_expiry_at = None

    @callback
    def _schedule_precipitation_expiry(self, data: VedurIsWeatherData) -> None:
        """Schedule the earliest published reading's deadline, without polling."""
        if self._precipitation_stopped:
            return
        now = datetime.now(timezone.utc)
        deadlines = {
            (utc_naive(precipitation.end) + MAX_PRECIPITATION_AGE).replace(
                tzinfo=timezone.utc
            )
            for observation in (
                *data.observations.values(),
                *data.precipitation_metadata.values(),
            )
            if (precipitation := current_precipitation(observation, now=now))
            is not None
        }
        deadline = min(deadlines) if deadlines else None
        if deadline is not None and deadline == self._precipitation_expiry_at:
            return
        self._cancel_precipitation_expiry()
        if deadline is None:
            return
        self._precipitation_expiry_at = deadline

        @callback
        def expire(_now: datetime) -> None:
            if self._precipitation_stopped or self._precipitation_expiry_at != deadline:
                return
            self._cancel_precipitation_expiry()
            data = self.data
            if data is None:
                return
            errors = dict(data.source_errors)
            errors.pop(SOURCE_PRECIPITATION, None)
            stale = set(data.stale_sources) - {SOURCE_PRECIPITATION}
            station_ids = set(data.precipitation_metadata) | set(
                data.precipitation_diagnostics
            )
            station_ids.update(
                station_id
                for station_id, observation in data.observations.items()
                if observation.precipitation is not None
            )
            fetch_errors = {
                station_id: details["precipitation_fetch_error"]
                for station_id, details in data.precipitation_diagnostics.items()
                if details.get("precipitation_fetch_error") is not None
            }
            diagnostics = self._precipitation_diagnostics(
                data.observations,
                station_ids,
                errors,
                stale,
                source_metadata=data.precipitation_metadata,
                fetch_errors=fetch_errors,
            )
            self.data = replace(
                data,
                stale_sources=frozenset(stale),
                source_errors=errors,
                precipitation_diagnostics=diagnostics,
            )
            # Unlike async_set_updated_data, this does not reset the polling timer.
            self.async_update_listeners()
            self._schedule_precipitation_expiry(self.data)

        self._precipitation_expiry_unsub = async_track_point_in_utc_time(
            self.hass, expire, deadline
        )

    async def _async_get_stations(
        self,
        now: datetime,
        previous: VedurIsWeatherData | None,
        source_errors: dict[str, str],
        stale_sources: set[str],
    ) -> dict[int, Station]:
        """Return station metadata, using cached data during outages."""
        if (
            self._stations_cache is not None
            and self._stations_fetched_at is not None
            and now - self._stations_fetched_at < STATION_CACHE_TTL
        ):
            return self._stations_cache
        if not self._source_ready(SOURCE_STATIONS, now, source_errors, stale_sources):
            return self._stations_cache or (
                previous.stations if previous else self._stored_stations()
            )

        try:
            stations = await self.client.async_get_stations(station_type=None)
        except (CannotConnect, InvalidResponse) as err:
            self._source_failed(SOURCE_STATIONS, err)
            source_errors[SOURCE_STATIONS] = str(err)
            stale_sources.add(SOURCE_STATIONS)
            if self._stations_cache is not None:
                return self._stations_cache
            if previous is not None and previous.stations:
                return previous.stations
            return self._stored_stations()

        stations_by_id = {station.station_id: station for station in stations}
        self._source_succeeded(SOURCE_STATIONS)
        self._stations_cache = stations_by_id
        self._stations_fetched_at = now
        return stations_by_id

    def _stored_stations(self) -> dict[int, Station]:
        """Return stations stored in the config entry, if any."""
        entry_config = self.config_entry.options or self.config_entry.data
        return {
            station.station_id: station
            for station in (
                Station.from_api(station_data)
                for station_data in entry_config.get(CONF_STATIONS, [])
            )
        }

    async def _async_get_observations(
        self,
        aws_station_ids: list[int],
        fallback_station_ids: set[int],
        previous: VedurIsWeatherData | None,
        source_errors: dict[str, str],
        stale_sources: set[str],
        *,
        now: datetime | None = None,
    ) -> dict[int, Observation]:
        """Return current observations, falling back to stale data on errors."""
        if not aws_station_ids:
            return previous.observations if previous is not None else {}
        now = now or datetime.now(timezone.utc)
        if not self._source_ready(
            SOURCE_OBSERVATIONS, now, source_errors, stale_sources
        ):
            return previous.observations if previous else {}
        while (
            self._observations_next_update is not None
            and now < self._observations_next_update
        ):
            remaining = self._observations_next_update - now
            if remaining > EARLY_POLL_TOLERANCE:
                return previous.observations if previous else {}
            # An early timer must not publish the old snapshot before the real poll.
            await sleep(max(remaining.total_seconds(), 0.001))
            now = datetime.now(timezone.utc)

        try:
            observations = await self.client.async_get_latest_observations(
                aws_station_ids,
                fallback_station_ids=fallback_station_ids,
                refresh_fallback=True,
            )
        except (CannotConnect, InvalidResponse) as err:
            self._source_failed(SOURCE_OBSERVATIONS, err)
            source_errors[SOURCE_OBSERVATIONS] = str(err)
            stale_sources.add(SOURCE_OBSERVATIONS)
            if previous is not None:
                return previous.observations
            return {}
        self._source_succeeded(SOURCE_OBSERVATIONS)
        completed = datetime.now(timezone.utc)
        self._observations_next_update = completed + next_aligned_poll_delay(completed)
        return observations

    async def _async_get_forecasts(
        self,
        now: datetime,
        stations_by_id: dict[int, Station],
        previous: VedurIsWeatherData | None,
        source_errors: dict[str, str],
        stale_sources: set[str],
    ) -> dict[int, StationForecast]:
        """Return XML forecasts, falling back to stale data on errors."""
        if not stations_by_id:
            return previous.forecasts if previous is not None else {}

        if (
            self._forecasts_cache is not None
            and self._forecasts_fetched_at is not None
            and now - self._forecasts_fetched_at < FORECAST_CACHE_TTL
        ):
            return self._forecasts_cache
        if not self._source_ready(SOURCE_FORECASTS, now, source_errors, stale_sources):
            return previous.forecasts if previous else {}

        try:
            forecasts = await self.client.async_get_forecasts(stations_by_id.keys())
        except (CannotConnect, InvalidResponse) as err:
            self._source_failed(SOURCE_FORECASTS, err)
            source_errors[SOURCE_FORECASTS] = str(err)
            stale_sources.add(SOURCE_FORECASTS)
            if previous is not None:
                return previous.forecasts
            return {}

        self._forecasts_cache = forecasts
        self._source_succeeded(SOURCE_FORECASTS)
        self._forecasts_fetched_at = now
        return forecasts

    def _schedule_next_update(self, completed_at: datetime) -> None:
        """One failing product must not delay the healthy observation schedule."""
        self.update_interval = next_aligned_poll_delay(completed_at)
        for retry_at in self._source_retry_at.values():
            if retry_at > completed_at:
                self.update_interval = min(
                    self.update_interval, retry_at - completed_at
                )

    def _source_failed(self, source: str, error: Exception) -> None:
        failures = self._source_failure_counts.get(source, 0) + 1
        self._source_failure_counts[source] = failures
        self._source_errors[source] = str(error)
        self._source_retry_at[source] = datetime.now(timezone.utc) + backoff_poll_delay(
            failures,
            jitter_seconds=uniform(0, MAX_BACKOFF_JITTER_SECONDS),
        )

    def _source_succeeded(self, source: str) -> None:
        self._source_failure_counts.pop(source, None)
        self._source_errors.pop(source, None)
        self._source_retry_at.pop(source, None)

    def _source_ready(
        self, source: str, now: datetime, errors: dict[str, str], stale: set[str]
    ) -> bool:
        retry_at = self._source_retry_at.get(source)
        if retry_at is None or now >= retry_at:
            return True
        errors[source] = self._source_errors[source]
        stale.add(source)
        return False

    def _precipitation_diagnostics(
        self,
        observations: dict[int, Observation],
        station_ids: set[int],
        errors: dict[str, str],
        stale: set[str],
        *,
        source_metadata: Mapping[int, Observation] | None = None,
        fetch_errors: Mapping[int, str] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Report secondary-source problems without changing other products' health."""
        diagnostics: dict[int, dict[str, Any]] = {}
        if source_metadata is None:
            source_metadata = self.client.gottvedur_observation_metadata
        if fetch_errors is None:
            fetch_errors = self.client.gottvedur_observation_errors
        problems = {
            station_id: message
            for station_id, message in fetch_errors.items()
            if station_id in station_ids
        }
        for station_id in station_ids:
            observation = source_metadata.get(station_id) or observations.get(station_id)
            attrs = precipitation_attributes(observation)
            status = attrs["precipitation_status"]
            if station_id in problems and attrs["precipitation_fetch_error"] is None:
                attrs["precipitation_fetch_error"] = problems[station_id]
                if status == "missing":
                    status = attrs["precipitation_status"] = "source_error"
            if status != "available" or attrs["precipitation_fetch_error"] is not None:
                diagnostics[station_id] = {
                    key: value
                    for key, value in attrs.items()
                    if key in {
                        "precipitation_status",
                        "precipitation_age_minutes",
                        "precipitation_max_age_minutes",
                        "precipitation_last_successful_fetch",
                        "precipitation_fetch_error",
                        "precipitation_period_start",
                        "precipitation_period_end",
                        "precipitation_using_cached_data",
                    }
                }
            if status in {"stale", "future_timestamp"}:
                problems.setdefault(
                    station_id,
                    "Precipitation measurement timestamp is stale or invalid",
                )
        if problems:
            errors[SOURCE_PRECIPITATION] = "; ".join(
                f"{station_id}: {message}"
                for station_id, message in sorted(problems.items())
            )
            stale.add(SOURCE_PRECIPITATION)
        return diagnostics

    def _precipitation_forecast_station_ids(
        self,
        stations_by_id: dict[int, Station],
        forecasts: dict[int, StationForecast],
    ) -> set[int]:
        """Select forecast stations used by enabled locations and station devices."""
        candidates = [
            stations_by_id[station_id]
            for station_id in forecasts
            if station_id in stations_by_id
        ]
        entry_config = self.config_entry.options or self.config_entry.data
        selected_ids: set[int] = set()

        def select(coordinate: Coordinate | None) -> None:
            if coordinate is None:
                return
            nearest = nearest_station(coordinate, candidates)
            if nearest is not None:
                selected_ids.add(nearest[0].station_id)

        if entry_config.get(CONF_ENABLE_STATION_WEATHER, True):
            for station_id in entry_config.get(CONF_STATION_IDS, []):
                station = stations_by_id.get(int(station_id))
                if station is not None:
                    if station.station_id in forecasts:
                        selected_ids.add(station.station_id)
                    elif station.latitude is not None and station.longitude is not None:
                        select(Coordinate(station.latitude, station.longitude))

        if (
            entry_config.get(CONF_ENABLE_HOME_WEATHER, True)
            and (home := self.hass.states.get("zone.home")) is not None
        ):
            select(_coordinate_from_state(home))

        if entry_config.get(CONF_ENABLE_PERSON_WEATHER, True):
            zones = _zones_by_name(self.hass)
            for person in self.hass.states.async_all("person"):
                select(resolve_person_coordinate(person.state, person.attributes, zones))

        if entry_config.get(CONF_ENABLE_DEVICE_TRACKER_WEATHER, True):
            for entity_id in entry_config.get(CONF_DEVICE_TRACKER_ENTITY_IDS, []):
                if (tracker := self.hass.states.get(entity_id)) is not None:
                    select(resolve_tracker_coordinate(tracker.attributes))
        return selected_ids

    def _fallback_station_ids(
        self,
        stations_by_id: dict[int, Station],
        aws_station_ids: list[int],
    ) -> set[int]:
        """Return station ids that should receive gottvedur.is fallback data."""
        entry_config = self.config_entry.options or self.config_entry.data
        station_ids = set()
        if entry_config.get(CONF_ENABLE_STATION_WEATHER, True):
            station_ids.update(
                int(station_id)
                for station_id in entry_config.get(CONF_STATION_IDS, [])
            )
        aws_stations = [
            stations_by_id[station_id]
            for station_id in aws_station_ids
            if station_id in stations_by_id
        ]
        if entry_config.get(CONF_ENABLE_HOME_WEATHER, True):
            home_station_id = self._nearest_home_station_id(aws_stations)
            if home_station_id is not None:
                station_ids.add(home_station_id)

        if entry_config.get(CONF_ENABLE_DEVICE_TRACKER_WEATHER, True):
            station_ids.update(
                self._nearest_device_tracker_station_ids(aws_stations)
            )

        if not entry_config.get(CONF_ENABLE_PERSON_WEATHER, True):
            return station_ids

        zones = _zones_by_name(self.hass)
        for person_state in self.hass.states.async_all("person"):
            coordinate = resolve_person_coordinate(
                person_state.state,
                person_state.attributes,
                zones,
            )
            if coordinate is None:
                continue

            nearest = nearest_station(coordinate, aws_stations)
            if nearest is None or nearest[1] > MAX_FALLBACK_OBSERVATION_DISTANCE_KM:
                continue

            station_ids.add(nearest[0].station_id)

        return station_ids

    def _nearest_device_tracker_station_ids(
        self,
        aws_stations: list[Station],
    ) -> set[int]:
        """Return nearest AWS station ids for configured device trackers."""
        entry_config = self.config_entry.options or self.config_entry.data
        station_ids: set[int] = set()

        for tracker_entity_id in entry_config.get(
            CONF_DEVICE_TRACKER_ENTITY_IDS,
            [],
        ):
            state = self.hass.states.get(tracker_entity_id)
            if state is None:
                continue

            coordinate = resolve_tracker_coordinate(state.attributes)
            if coordinate is None:
                continue

            nearest = nearest_station(coordinate, aws_stations)
            if nearest is None or nearest[1] > MAX_FALLBACK_OBSERVATION_DISTANCE_KM:
                continue

            station_ids.add(nearest[0].station_id)

        return station_ids

    def _nearest_home_station_id(self, aws_stations: list[Station]) -> int | None:
        """Return the nearest AWS station id for the Home Assistant home zone."""
        home_state = self.hass.states.get("zone.home")
        if home_state is None:
            return None

        coordinate = _coordinate_from_state(home_state)
        if coordinate is None:
            return None

        nearest = nearest_station(coordinate, aws_stations)
        if nearest is None or nearest[1] > MAX_FALLBACK_OBSERVATION_DISTANCE_KM:
            return None

        return nearest[0].station_id


def _zones_by_name(hass: HomeAssistant) -> dict[str, Coordinate]:
    zones: dict[str, Coordinate] = {}
    for state in hass.states.async_all("zone"):
        coordinate = _coordinate_from_state(state)
        if coordinate is None:
            continue

        object_id = state.entity_id.removeprefix("zone.")
        for key in (state.state, state.name, object_id, state.entity_id):
            if key:
                zones[key.casefold()] = coordinate

    return zones


def _coordinate_from_state(state: State) -> Coordinate | None:
    latitude = state.attributes.get("latitude")
    longitude = state.attributes.get("longitude")
    if latitude is None or longitude is None:
        return None

    try:
        return Coordinate(float(latitude), float(longitude))
    except (TypeError, ValueError):
        return None
