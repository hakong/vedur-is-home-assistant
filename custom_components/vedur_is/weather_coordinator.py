"""Weather data coordinator for person-following Vedur.is weather entities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
from random import uniform

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
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

_LOGGER = logging.getLogger(__name__)
MAX_FALLBACK_OBSERVATION_DISTANCE_KM = 150.0
SOURCE_FORECASTS = "forecasts"
SOURCE_OBSERVATIONS = "observations"
SOURCE_STATIONS = "stations"
STATION_CACHE_TTL = timedelta(hours=12)
FORECAST_CACHE_TTL = timedelta(hours=1)
MAX_BACKOFF_INTERVAL = timedelta(hours=6)
MAX_BACKOFF_JITTER = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class VedurIsWeatherData:
    """All station data needed by person-following weather entities."""

    stations: dict[int, Station]
    observations: dict[int, Observation]
    forecasts: dict[int, StationForecast]
    stale_sources: frozenset[str] = frozenset()
    source_errors: Mapping[str, str] = field(default_factory=dict)
    last_successful_update: datetime | None = None

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
        self._base_update_interval = timedelta(minutes=DEFAULT_SCAN_INTERVAL_MINUTES)
        self._failure_count = 0
        self._stations_cache: dict[int, Station] | None = None
        self._stations_fetched_at: datetime | None = None
        self._forecasts_cache: dict[int, StationForecast] | None = None
        self._forecasts_fetched_at: datetime | None = None

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
        forecasts = await self._async_get_forecasts(
            now,
            stations_by_id,
            previous,
            source_errors,
            stale_sources,
        )

        self._record_update_result(source_errors)
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
        return VedurIsWeatherData(
            stations=stations_by_id,
            observations=observations,
            forecasts=forecasts,
            stale_sources=frozenset(stale_sources),
            source_errors=source_errors,
            last_successful_update=last_successful_update,
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

        try:
            stations = await self.client.async_get_stations(station_type=None)
        except (CannotConnect, InvalidResponse) as err:
            source_errors[SOURCE_STATIONS] = str(err)
            stale_sources.add(SOURCE_STATIONS)
            if self._stations_cache is not None:
                return self._stations_cache
            if previous is not None and previous.stations:
                return previous.stations
            return self._stored_stations()

        stations_by_id = {station.station_id: station for station in stations}
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
    ) -> dict[int, Observation]:
        """Return current observations, falling back to stale data on errors."""
        if not aws_station_ids:
            return previous.observations if previous is not None else {}

        try:
            return await self.client.async_get_latest_observations(
                aws_station_ids,
                fallback_station_ids=fallback_station_ids,
            )
        except (CannotConnect, InvalidResponse) as err:
            source_errors[SOURCE_OBSERVATIONS] = str(err)
            stale_sources.add(SOURCE_OBSERVATIONS)
            if previous is not None:
                return previous.observations
            return {}

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

        try:
            forecasts = await self.client.async_get_forecasts(stations_by_id.keys())
        except (CannotConnect, InvalidResponse) as err:
            source_errors[SOURCE_FORECASTS] = str(err)
            stale_sources.add(SOURCE_FORECASTS)
            if previous is not None:
                return previous.forecasts
            return {}

        self._forecasts_cache = forecasts
        self._forecasts_fetched_at = now
        return forecasts

    def _record_update_result(self, source_errors: Mapping[str, str]) -> None:
        """Adjust update interval after errors to avoid hammering upstream."""
        if not source_errors:
            self._failure_count = 0
            self.update_interval = self._base_update_interval
            return

        self._failure_count += 1
        backoff = self._base_update_interval * (2 ** min(self._failure_count, 4))
        jitter_seconds = min(
            MAX_BACKOFF_JITTER,
            self._base_update_interval,
        ).total_seconds()
        jitter = timedelta(
            seconds=uniform(0, jitter_seconds),
        )
        self.update_interval = min(backoff + jitter, MAX_BACKOFF_INTERVAL)

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
