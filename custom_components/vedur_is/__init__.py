"""The Vedur.is integration."""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    CONF_ENABLE_PERSON_WEATHER,
    CONF_STATION_IDS,
    DOMAIN,
    ENTRY_TITLE,
    PLATFORMS,
    SENSOR_KEYS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    """Set up Vedur.is from a config entry."""
    from homeassistant.helpers import aiohttp_client

    from .api import VedurIsApiClient
    from .coordinator import VedurIsDataUpdateCoordinator

    if entry.title != ENTRY_TITLE:
        _LOGGER.debug("Normalizing Vedur.is config entry title")
        hass.config_entries.async_update_entry(entry, title=ENTRY_TITLE)

    if entry.options.get(CONF_STATION_IDS) and entry.options != entry.data:
        _LOGGER.debug("Promoting Vedur.is options to config entry data")
        hass.config_entries.async_update_entry(entry, data=dict(entry.options))

    _async_remove_stale_registry_entries(hass, entry)

    client = VedurIsApiClient(aiohttp_client.async_get_clientsession(hass))
    coordinator = VedurIsDataUpdateCoordinator(hass, client, entry)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: Any, entry: Any) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: Any, entry: Any) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_remove_stale_registry_entries(hass: Any, entry: Any) -> None:
    """Remove station devices that are no longer configured for this entry."""
    from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entry_config = entry.options or entry.data
    configured_station_ids = {
        str(station_id)
        for station_id in entry_config.get(CONF_STATION_IDS, [])
    }
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    if not entry_config.get(CONF_ENABLE_PERSON_WEATHER, True):
        _async_remove_person_weather_registry_entries(entity_registry, entry)

    for device in list(device_registry.devices.values()):
        vedur_identifiers = [
            identifier
            for identifier in device.identifiers
            if identifier[0] == DOMAIN
        ]
        if not vedur_identifiers or entry.entry_id not in device.config_entries:
            continue

        station_id = vedur_identifiers[0][1]
        if station_id in configured_station_ids:
            continue

        _LOGGER.debug(
            "Removing stale Vedur.is station registry entries: %s",
            station_id,
        )
        for key in SENSOR_KEYS:
            entity_id = entity_registry.async_get_entity_id(
                SENSOR_DOMAIN,
                DOMAIN,
                f"{DOMAIN}_{station_id}_{key}",
            )
            if entity_id:
                entity_registry.async_remove(entity_id)
        entity_id = entity_registry.async_get_entity_id(
            WEATHER_DOMAIN,
            DOMAIN,
            f"{DOMAIN}_{station_id}_weather",
        )
        if entity_id:
            entity_registry.async_remove(entity_id)
        device_registry.async_remove_device(device.id)


def _async_remove_person_weather_registry_entries(
    entity_registry: Any,
    entry: Any,
) -> None:
    """Remove person-following weather entities for a config entry."""
    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN

    for entity in list(entity_registry.entities.values()):
        if (
            entity.config_entry_id == entry.entry_id
            and entity.platform == DOMAIN
            and entity.entity_id.startswith(f"{WEATHER_DOMAIN}.")
            and entity.unique_id
            and entity.unique_id.startswith(f"{DOMAIN}_person.")
        ):
            _LOGGER.debug(
                "Removing Vedur.is person weather registry entry: %s",
                entity.entity_id,
            )
            entity_registry.async_remove(entity.entity_id)
