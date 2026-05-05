"""Data coordinator for the Vedur.is integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CannotConnect, InvalidResponse, Observation, Station, VedurIsApiClient
from .const import (
    CONF_STATION_IDS,
    CONF_STATIONS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class VedurIsDataUpdateCoordinator(DataUpdateCoordinator[dict[int, Observation]]):
    """Coordinate vedur.is observation updates."""

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
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=DEFAULT_SCAN_INTERVAL_MINUTES),
        )
        self.client = client
        entry_config = entry.options or entry.data
        self.station_ids: list[int] = [
            int(station_id) for station_id in entry_config[CONF_STATION_IDS]
        ]
        _LOGGER.debug("Setting up Vedur.is stations: %s", self.station_ids)
        self.stations: dict[int, Station] = {}

        for station_data in entry_config.get(CONF_STATIONS, []):
            station = Station.from_api(station_data)
            self.stations[station.station_id] = station

    async def _async_update_data(self) -> dict[int, Observation]:
        """Fetch latest data from vedur.is."""
        try:
            return await self.client.async_get_latest_observations(
                self.station_ids,
                fallback_station_ids=self.station_ids,
            )
        except (CannotConnect, InvalidResponse) as err:
            raise UpdateFailed(str(err)) from err
