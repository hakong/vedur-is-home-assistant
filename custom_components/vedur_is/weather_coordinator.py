"""Weather data coordinator for person-following Vedur.is weather entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    CannotConnect,
    InvalidResponse,
    Observation,
    Station,
    StationForecast,
    VedurIsApiClient,
)
from .const import (
    CONF_ENABLE_HOME_WEATHER,
    CONF_ENABLE_PERSON_WEATHER,
    CONF_ENABLE_STATION_WEATHER,
    CONF_STATION_IDS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from .geo import Coordinate, nearest_station, resolve_person_coordinate

_LOGGER = logging.getLogger(__name__)
MAX_FALLBACK_OBSERVATION_DISTANCE_KM = 150.0


@dataclass(frozen=True, slots=True)
class VedurIsWeatherData:
    """All station data needed by person-following weather entities."""

    stations: dict[int, Station]
    observations: dict[int, Observation]
    forecasts: dict[int, StationForecast]


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

    async def _async_update_data(self) -> VedurIsWeatherData:
        """Fetch the latest full weather data set from vedur.is."""
        try:
            stations = await self.client.async_get_stations(station_type=None)
            stations_by_id = {station.station_id: station for station in stations}
            aws_station_ids = [
                station.station_id
                for station in stations
                if station.station_type and station.station_type.casefold() == "sj"
            ]
            fallback_station_ids = self._fallback_station_ids(
                stations_by_id,
                aws_station_ids,
            )
            observations = await self.client.async_get_latest_observations(
                aws_station_ids,
                fallback_station_ids=fallback_station_ids,
            )
            forecasts = await self.client.async_get_forecasts(stations_by_id.keys())
        except (CannotConnect, InvalidResponse) as err:
            raise UpdateFailed(str(err)) from err

        _LOGGER.debug(
            "Fetched Vedur.is weather data: %s stations, %s observations, "
            "%s forecast stations",
            len(stations_by_id),
            len(observations),
            len(forecasts),
        )
        return VedurIsWeatherData(
            stations=stations_by_id,
            observations=observations,
            forecasts=forecasts,
        )

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
