"""Weather alert coordinator for the Icelandic Met Office Weather integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import logging
from random import uniform

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CannotConnect, InvalidResponse, VedurIsApiClient, WeatherAlert
from .const import ALERT_SCAN_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)
MAX_BACKOFF_INTERVAL = timedelta(hours=6)
MAX_BACKOFF_JITTER = timedelta(minutes=5)


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
        self._base_update_interval = timedelta(minutes=ALERT_SCAN_INTERVAL_MINUTES)
        self._failure_count = 0
        self.data_stale = False
        self.source_errors: Mapping[str, str] = {}

    async def _async_update_data(self) -> tuple[WeatherAlert, ...]:
        """Fetch active weather alerts from vedur.is."""
        try:
            alerts = await self.client.async_get_weather_alerts()
        except (CannotConnect, InvalidResponse) as err:
            self._record_update_result({"alerts": str(err)})
            self.data_stale = self.data is not None
            self.source_errors = {"alerts": str(err)}
            if self.data is not None:
                return self.data
            raise UpdateFailed(str(err)) from err

        self._record_update_result({})
        self.data_stale = False
        self.source_errors = {}
        return alerts

    def _record_update_result(self, source_errors: Mapping[str, str]) -> None:
        """Adjust update interval after errors to avoid hammering upstream."""
        if not source_errors:
            self._failure_count = 0
            self.update_interval = self._base_update_interval
            return

        self._failure_count += 1
        backoff = self._base_update_interval * (2 ** min(self._failure_count, 5))
        jitter_seconds = min(
            MAX_BACKOFF_JITTER,
            self._base_update_interval,
        ).total_seconds()
        jitter = timedelta(
            seconds=uniform(0, jitter_seconds),
        )
        self.update_interval = min(backoff + jitter, MAX_BACKOFF_INTERVAL)
