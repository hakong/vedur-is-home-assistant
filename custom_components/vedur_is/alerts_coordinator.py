"""Weather alert coordinator for the Icelandic Met Office Weather integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CannotConnect, InvalidResponse, VedurIsApiClient, WeatherAlert
from .const import ALERT_SCAN_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)


class VedurIsAlertsDataUpdateCoordinator(
    DataUpdateCoordinator[tuple[WeatherAlert, ...]]
):
    """Coordinate vedur.is weather alert updates."""

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
            name=f"{DOMAIN}_alerts",
            config_entry=entry,
            update_interval=timedelta(minutes=ALERT_SCAN_INTERVAL_MINUTES),
        )
        self.client = client

    async def _async_update_data(self) -> tuple[WeatherAlert, ...]:
        """Fetch active weather alerts from vedur.is."""
        try:
            return await self.client.async_get_weather_alerts()
        except (CannotConnect, InvalidResponse) as err:
            raise UpdateFailed(str(err)) from err
