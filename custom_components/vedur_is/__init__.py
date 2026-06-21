"""The Vedur.is integration."""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    CONF_DEVICE_TRACKER_ENTITY_IDS,
    CONF_ENABLE_DEVICE_TRACKER_WEATHER,
    CONF_ENABLE_HOME_WEATHER,
    CONF_ENABLE_PERSON_WEATHER,
    CONF_ENABLE_STATION_WEATHER,
    CONF_STATION_IDS,
    DOMAIN,
    ENTRY_TITLE,
    PLATFORMS,
    WEATHER_SENSOR_KEYS,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: Any, config: Any) -> bool:
    """Set up Vedur.is integration services."""
    from .services import async_setup_services

    await async_setup_services(hass)
    return True


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    """Set up Vedur.is from a config entry."""
    from homeassistant.helpers import aiohttp_client

    from .api import VedurIsApiClient
    from .weather_coordinator import VedurIsWeatherDataUpdateCoordinator

    if entry.title != ENTRY_TITLE:
        _LOGGER.debug("Normalizing Vedur.is config entry title")
        hass.config_entries.async_update_entry(entry, title=ENTRY_TITLE)

    if entry.options.get(CONF_STATION_IDS) and entry.options != entry.data:
        _LOGGER.debug("Promoting Vedur.is options to config entry data")
        hass.config_entries.async_update_entry(entry, data=dict(entry.options))

    _async_remove_stale_registry_entries(hass, entry)

    client = VedurIsApiClient(aiohttp_client.async_get_clientsession(hass))
    coordinator = VedurIsWeatherDataUpdateCoordinator(hass, client, entry)

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
    configured_tracker_entity_ids = set(
        entry_config.get(CONF_DEVICE_TRACKER_ENTITY_IDS, [])
    )
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    if not entry_config.get(CONF_ENABLE_DEVICE_TRACKER_WEATHER, True):
        _async_remove_device_tracker_weather_registry_entries(
            entity_registry,
            device_registry,
            entry,
        )
    if not entry_config.get(CONF_ENABLE_PERSON_WEATHER, True):
        _async_remove_person_weather_registry_entries(
            entity_registry,
            device_registry,
            entry,
        )
    if not entry_config.get(CONF_ENABLE_HOME_WEATHER, True):
        _async_remove_home_weather_registry_entries(
            entity_registry,
            device_registry,
            entry,
        )
    if not entry_config.get(CONF_ENABLE_STATION_WEATHER, True):
        _async_remove_selected_station_registry_entries(
            entity_registry,
            device_registry,
            entry,
            configured_station_ids,
        )

    for device in list(device_registry.devices.values()):
        vedur_identifiers = [
            identifier
            for identifier in device.identifiers
            if identifier[0] == DOMAIN
        ]
        if not vedur_identifiers or entry.entry_id not in device.config_entries:
            continue

        station_id = vedur_identifiers[0][1]
        if station_id == "home_weather" or station_id.startswith("person_weather:"):
            continue
        if station_id.startswith("device_tracker_weather:"):
            if not entry_config.get(CONF_ENABLE_DEVICE_TRACKER_WEATHER, True):
                continue
            tracker_entity_id = station_id.removeprefix("device_tracker_weather:")
            if (
                tracker_entity_id in configured_tracker_entity_ids
            ):
                continue
            _async_remove_device_tracker_weather_device(
                entity_registry,
                device_registry,
                device,
                station_id,
            )
            continue
        if station_id in configured_station_ids:
            continue

        _LOGGER.debug(
            "Removing stale Vedur.is station registry entries: %s",
            station_id,
        )
        for key in WEATHER_SENSOR_KEYS:
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


def _async_remove_device_tracker_weather_registry_entries(
    entity_registry: Any,
    device_registry: Any,
    entry: Any,
) -> None:
    """Remove all device tracker weather entities for a config entry."""
    for entity in list(entity_registry.entities.values()):
        if (
            entity.config_entry_id == entry.entry_id
            and entity.platform == DOMAIN
            and entity.unique_id
            and entity.unique_id.startswith(f"{DOMAIN}_device_tracker.")
        ):
            _LOGGER.debug(
                "Removing Vedur.is device tracker weather registry entry: %s",
                entity.entity_id,
            )
            entity_registry.async_remove(entity.entity_id)

    for device in list(device_registry.devices.values()):
        if entry.entry_id not in device.config_entries:
            continue

        if any(
            identifier[0] == DOMAIN
            and str(identifier[1]).startswith("device_tracker_weather:")
            for identifier in device.identifiers
        ):
            _LOGGER.debug(
                "Removing Vedur.is device tracker weather device: %s",
                device.name,
            )
            device_registry.async_remove_device(device.id)


def _async_remove_device_tracker_weather_device(
    entity_registry: Any,
    device_registry: Any,
    device: Any,
    identifier: str,
) -> None:
    """Remove one stale device tracker weather device and entity."""
    from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN

    tracker_entity_id = identifier.removeprefix("device_tracker_weather:")
    entity_id = entity_registry.async_get_entity_id(
        WEATHER_DOMAIN,
        DOMAIN,
        f"{DOMAIN}_{tracker_entity_id}_weather",
    )
    if entity_id:
        _LOGGER.debug(
            "Removing stale Vedur.is device tracker weather registry entry: %s",
            entity_id,
        )
        entity_registry.async_remove(entity_id)
    for key in WEATHER_SENSOR_KEYS:
        entity_id = entity_registry.async_get_entity_id(
            SENSOR_DOMAIN,
            DOMAIN,
            f"{DOMAIN}_{tracker_entity_id}_{key}",
        )
        if entity_id:
            entity_registry.async_remove(entity_id)
    _LOGGER.debug(
        "Removing stale Vedur.is device tracker weather device: %s",
        device.name,
    )
    device_registry.async_remove_device(device.id)


def _async_remove_person_weather_registry_entries(
    entity_registry: Any,
    device_registry: Any,
    entry: Any,
) -> None:
    """Remove person-following weather entities for a config entry."""
    for entity in list(entity_registry.entities.values()):
        if (
            entity.config_entry_id == entry.entry_id
            and entity.platform == DOMAIN
            and entity.unique_id
            and entity.unique_id.startswith(f"{DOMAIN}_person.")
        ):
            _LOGGER.debug(
                "Removing Vedur.is person weather registry entry: %s",
                entity.entity_id,
            )
            entity_registry.async_remove(entity.entity_id)

    for device in list(device_registry.devices.values()):
        if entry.entry_id not in device.config_entries:
            continue

        if any(
            identifier[0] == DOMAIN
            and str(identifier[1]).startswith("person_weather:")
            for identifier in device.identifiers
        ):
            _LOGGER.debug(
                "Removing Vedur.is person weather device registry entry: %s",
                device.name,
            )
            device_registry.async_remove_device(device.id)


def _async_remove_home_weather_registry_entries(
    entity_registry: Any,
    device_registry: Any,
    entry: Any,
) -> None:
    """Remove the Home weather entity and device for a config entry."""
    from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN

    entity_id = entity_registry.async_get_entity_id(
        WEATHER_DOMAIN,
        DOMAIN,
        f"{DOMAIN}_home_weather",
    )
    if entity_id:
        _LOGGER.debug("Removing Vedur.is Home weather registry entry: %s", entity_id)
        entity_registry.async_remove(entity_id)
    for key in WEATHER_SENSOR_KEYS:
        entity_id = entity_registry.async_get_entity_id(
            SENSOR_DOMAIN,
            DOMAIN,
            f"{DOMAIN}_home_weather_{key}",
        )
        if entity_id:
            entity_registry.async_remove(entity_id)

    device = device_registry.async_get_device(identifiers={(DOMAIN, "home_weather")})
    if device and entry.entry_id in device.config_entries:
        _LOGGER.debug("Removing Vedur.is Home weather device: %s", device.name)
        device_registry.async_remove_device(device.id)


def _async_remove_selected_station_registry_entries(
    entity_registry: Any,
    device_registry: Any,
    entry: Any,
    station_ids: set[str],
) -> None:
    """Remove selected station entities and devices for a config entry."""
    from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN

    for station_id in station_ids:
        for key in WEATHER_SENSOR_KEYS:
            entity_id = entity_registry.async_get_entity_id(
                SENSOR_DOMAIN,
                DOMAIN,
                f"{DOMAIN}_{station_id}_{key}",
            )
            if entity_id:
                _LOGGER.debug(
                    "Removing Vedur.is station diagnostic registry entry: %s",
                    entity_id,
                )
                entity_registry.async_remove(entity_id)

        entity_id = entity_registry.async_get_entity_id(
            WEATHER_DOMAIN,
            DOMAIN,
            f"{DOMAIN}_{station_id}_weather",
        )
        if entity_id:
            _LOGGER.debug(
                "Removing Vedur.is station weather registry entry: %s",
                entity_id,
            )
            entity_registry.async_remove(entity_id)

        device = device_registry.async_get_device(identifiers={(DOMAIN, station_id)})
        if device and entry.entry_id in device.config_entries:
            _LOGGER.debug("Removing Vedur.is station device: %s", device.name)
            device_registry.async_remove_device(device.id)
