"""Config flow for the Vedur.is integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, device_registry as dr
from homeassistant.helpers import entity_registry as er, selector

from .api import CannotConnect, InvalidResponse, Station, VedurIsApiClient
from .const import (
    CONF_DEVICE_TRACKER_ENTITY_IDS,
    CONF_ENABLE_DEVICE_TRACKER_WEATHER,
    CONF_ENABLE_DERIVED_FORECASTS,
    CONF_ENABLE_HOME_WEATHER,
    CONF_ENABLE_PERSON_WEATHER,
    CONF_ENABLE_STATION_WEATHER,
    CONF_STATION_IDS,
    CONF_STATIONS,
    DOMAIN,
    ENTRY_TITLE,
    SENSOR_KEYS,
)


class VedurIsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vedur.is."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._stations: dict[int, Station] = {}

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> VedurIsOptionsFlow:
        """Create the options flow."""
        return VedurIsOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Fetch stations and select one or more."""
        errors: dict[str, str] = {}

        try:
            stations = await self._async_get_stations()
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidResponse:
            errors["base"] = "invalid_response"
        else:
            if not stations:
                errors["base"] = "no_stations"
            else:
                self._stations = {station.station_id: station for station in stations}
                return await self.async_step_select()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select optional active stations."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_ids = [
                int(station_id)
                for station_id in user_input.get(CONF_STATION_IDS, [])
            ]

            if already_configured := self._already_configured(selected_ids):
                errors[CONF_STATION_IDS] = "already_configured"
                self.context["description_placeholders"] = {
                    "stations": ", ".join(
                        str(station_id) for station_id in already_configured
                    )
                }
            else:
                enable_home_weather = user_input.get(CONF_ENABLE_HOME_WEATHER, True)
                enable_device_tracker_weather = user_input.get(
                    CONF_ENABLE_DEVICE_TRACKER_WEATHER,
                    True,
                )
                enable_person_weather = user_input.get(
                    CONF_ENABLE_PERSON_WEATHER, True
                )
                enable_station_weather = user_input.get(
                    CONF_ENABLE_STATION_WEATHER, True
                )
                enable_derived_forecasts = user_input.get(
                    CONF_ENABLE_DERIVED_FORECASTS, False
                )
                device_tracker_entity_ids = user_input.get(
                    CONF_DEVICE_TRACKER_ENTITY_IDS,
                    [],
                )
                await self.async_set_unique_id(
                    "stations:"
                    + ",".join(str(station_id) for station_id in sorted(selected_ids))
                )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=ENTRY_TITLE,
                    data={
                        CONF_STATION_IDS: selected_ids,
                        CONF_STATIONS: [
                            self._stations[station_id].as_storage_dict()
                            for station_id in selected_ids
                        ],
                        CONF_DEVICE_TRACKER_ENTITY_IDS: device_tracker_entity_ids,
                        CONF_ENABLE_DEVICE_TRACKER_WEATHER: (
                            enable_device_tracker_weather
                        ),
                        CONF_ENABLE_HOME_WEATHER: enable_home_weather,
                        CONF_ENABLE_PERSON_WEATHER: enable_person_weather,
                        CONF_ENABLE_STATION_WEATHER: enable_station_weather,
                        CONF_ENABLE_DERIVED_FORECASTS: enable_derived_forecasts,
                    },
                )

        return self.async_show_form(
            step_id="select",
            data_schema=self._select_schema(
                device_tracker_entity_ids=[],
                enable_device_tracker_weather=True,
                enable_home_weather=True,
                enable_person_weather=True,
                enable_station_weather=True,
                enable_derived_forecasts=False,
            ),
            errors=errors,
        )

    async def _async_get_stations(self) -> list[Station]:
        """Fetch all active automatic weather stations."""
        client = VedurIsApiClient(aiohttp_client.async_get_clientsession(self.hass))
        return await client.async_get_stations()

    def _select_schema(
        self,
        *,
        device_tracker_entity_ids: list[str],
        enable_device_tracker_weather: bool,
        enable_home_weather: bool,
        enable_person_weather: bool,
        enable_station_weather: bool,
        enable_derived_forecasts: bool,
    ) -> vol.Schema:
        """Return the station selection schema."""
        return _select_schema(
            self._stations,
            default_station_ids=[],
            default_device_tracker_entity_ids=device_tracker_entity_ids,
            default_enable_device_tracker_weather=enable_device_tracker_weather,
            default_enable_home_weather=enable_home_weather,
            default_enable_person_weather=enable_person_weather,
            default_enable_station_weather=enable_station_weather,
            default_enable_derived_forecasts=enable_derived_forecasts,
        )

    def _already_configured(self, station_ids: list[int]) -> list[int]:
        """Return selected station IDs that are already configured."""
        return _already_configured(
            self.hass.config_entries.async_entries(DOMAIN),
            station_ids,
        )


class VedurIsOptionsFlow(config_entries.OptionsFlow):
    """Handle Vedur.is options."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._stations = _stored_stations(config_entry)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Fetch stations and update selected stations."""
        errors: dict[str, str] = {}

        try:
            stations = await self._async_get_stations()
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidResponse:
            errors["base"] = "invalid_response"
        else:
            for station in stations:
                self._stations[station.station_id] = station
            return await self.async_step_select()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Update the selected stations."""
        errors: dict[str, str] = {}
        current_ids = _configured_station_ids(self._config_entry)
        current_device_tracker_entity_ids = _configured_device_tracker_entity_ids(
            self._config_entry
        )
        enable_device_tracker_weather = _enable_device_tracker_weather(
            self._config_entry
        )
        enable_home_weather = _enable_home_weather(self._config_entry)
        enable_person_weather = _enable_person_weather(self._config_entry)
        enable_station_weather = _enable_station_weather(self._config_entry)
        enable_derived_forecasts = _enable_derived_forecasts(self._config_entry)

        if user_input is not None:
            selected_ids = [
                int(station_id)
                for station_id in user_input.get(CONF_STATION_IDS, [])
            ]
            enable_home_weather = user_input.get(CONF_ENABLE_HOME_WEATHER, True)
            enable_device_tracker_weather = user_input.get(
                CONF_ENABLE_DEVICE_TRACKER_WEATHER,
                True,
            )
            enable_person_weather = user_input.get(CONF_ENABLE_PERSON_WEATHER, True)
            enable_station_weather = user_input.get(
                CONF_ENABLE_STATION_WEATHER, True
            )
            enable_derived_forecasts = user_input.get(
                CONF_ENABLE_DERIVED_FORECASTS, False
            )
            device_tracker_entity_ids = user_input.get(
                CONF_DEVICE_TRACKER_ENTITY_IDS,
                [],
            )

            if already_configured := _already_configured(
                self.hass.config_entries.async_entries(DOMAIN),
                selected_ids,
                ignore_entry_id=self._config_entry.entry_id,
            ):
                errors[CONF_STATION_IDS] = "already_configured"
                self.context["description_placeholders"] = {
                    "stations": ", ".join(
                        str(station_id) for station_id in already_configured
                    )
                }
            else:
                removed_ids = sorted(set(current_ids) - set(selected_ids))
                removed_device_tracker_entity_ids = sorted(
                    set(current_device_tracker_entity_ids)
                    - set(device_tracker_entity_ids)
                )
                entry_data = {
                    CONF_STATION_IDS: selected_ids,
                    CONF_STATIONS: [
                        self._stations[station_id].as_storage_dict()
                        for station_id in selected_ids
                    ],
                    CONF_DEVICE_TRACKER_ENTITY_IDS: device_tracker_entity_ids,
                    CONF_ENABLE_DEVICE_TRACKER_WEATHER: (
                        enable_device_tracker_weather
                    ),
                    CONF_ENABLE_HOME_WEATHER: enable_home_weather,
                    CONF_ENABLE_PERSON_WEATHER: enable_person_weather,
                    CONF_ENABLE_STATION_WEATHER: enable_station_weather,
                    CONF_ENABLE_DERIVED_FORECASTS: enable_derived_forecasts,
                }
                _async_remove_station_registry_entries(self.hass, removed_ids)
                _async_remove_device_tracker_registry_entries(
                    self.hass,
                    removed_device_tracker_entity_ids,
                )
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    title=ENTRY_TITLE,
                    data=entry_data,
                    options=entry_data,
                )
                return self.async_create_entry(title="", data=entry_data)

        return self.async_show_form(
            step_id="select",
            data_schema=_select_schema(
                self._stations,
                current_ids,
                default_device_tracker_entity_ids=current_device_tracker_entity_ids,
                default_enable_device_tracker_weather=enable_device_tracker_weather,
                default_enable_home_weather=enable_home_weather,
                default_enable_person_weather=enable_person_weather,
                default_enable_station_weather=enable_station_weather,
                default_enable_derived_forecasts=enable_derived_forecasts,
            ),
            errors=errors,
        )

    async def _async_get_stations(self) -> list[Station]:
        """Fetch all active automatic weather stations."""
        client = VedurIsApiClient(aiohttp_client.async_get_clientsession(self.hass))
        return await client.async_get_stations()


def _select_schema(
    stations: dict[int, Station],
    default_station_ids: list[int],
    *,
    default_device_tracker_entity_ids: list[str],
    default_enable_device_tracker_weather: bool,
    default_enable_home_weather: bool,
    default_enable_person_weather: bool,
    default_enable_station_weather: bool,
    default_enable_derived_forecasts: bool,
) -> vol.Schema:
    """Return the station selection schema."""
    options = [
        selector.SelectOptionDict(
            value=str(station.station_id),
            label=_station_selector_label(station),
        )
        for station in sorted(
            stations.values(),
            key=lambda station: station.name.casefold(),
        )
    ]

    return vol.Schema(
        {
            vol.Required(
                CONF_STATION_IDS,
                default=[str(station_id) for station_id in default_station_ids],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_DEVICE_TRACKER_ENTITY_IDS,
                default=default_device_tracker_entity_ids,
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="device_tracker",
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_ENABLE_DEVICE_TRACKER_WEATHER,
                default=default_enable_device_tracker_weather,
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_HOME_WEATHER,
                default=default_enable_home_weather,
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_PERSON_WEATHER,
                default=default_enable_person_weather,
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_STATION_WEATHER,
                default=default_enable_station_weather,
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_DERIVED_FORECASTS,
                default=default_enable_derived_forecasts,
            ): selector.BooleanSelector(),
        }
    )


def _station_selector_label(station: Station) -> str:
    """Return a human-friendly station selector label."""
    label = f"{station.name} ({station.station_id})"
    if station.owner:
        label = f"{label} - {station.owner}"
    return label


def _already_configured(
    entries: list[ConfigEntry],
    station_ids: list[int],
    *,
    ignore_entry_id: str | None = None,
) -> list[int]:
    """Return selected station IDs that are already configured."""
    selected = set(station_ids)
    configured: set[int] = set()

    for entry in entries:
        if entry.entry_id == ignore_entry_id:
            continue
        configured.update(_configured_station_ids(entry))

    return sorted(selected & configured)


def _configured_station_ids(entry: ConfigEntry) -> list[int]:
    """Return configured station IDs from entry options or data."""
    station_ids = entry.options.get(
        CONF_STATION_IDS,
        entry.data.get(CONF_STATION_IDS, []),
    )
    return [int(station_id) for station_id in station_ids]


def _configured_device_tracker_entity_ids(entry: ConfigEntry) -> list[str]:
    """Return configured device tracker entity IDs from entry options or data."""
    return list(
        entry.options.get(
            CONF_DEVICE_TRACKER_ENTITY_IDS,
            entry.data.get(CONF_DEVICE_TRACKER_ENTITY_IDS, []),
        )
    )


def _enable_device_tracker_weather(entry: ConfigEntry) -> bool:
    """Return whether selected device tracker weather should be created."""
    return bool(
        entry.options.get(
            CONF_ENABLE_DEVICE_TRACKER_WEATHER,
            entry.data.get(CONF_ENABLE_DEVICE_TRACKER_WEATHER, True),
        )
    )


def _enable_person_weather(entry: ConfigEntry) -> bool:
    """Return whether person-following weather entities should be created."""
    return bool(
        entry.options.get(
            CONF_ENABLE_PERSON_WEATHER,
            entry.data.get(CONF_ENABLE_PERSON_WEATHER, True),
        )
    )


def _enable_home_weather(entry: ConfigEntry) -> bool:
    """Return whether Home weather should be created."""
    return bool(
        entry.options.get(
            CONF_ENABLE_HOME_WEATHER,
            entry.data.get(CONF_ENABLE_HOME_WEATHER, True),
        )
    )


def _enable_station_weather(entry: ConfigEntry) -> bool:
    """Return whether selected station weather entities should be created."""
    return bool(
        entry.options.get(
            CONF_ENABLE_STATION_WEATHER,
            entry.data.get(CONF_ENABLE_STATION_WEATHER, True),
        )
    )


def _enable_derived_forecasts(entry: ConfigEntry) -> bool:
    """Return whether derived daily and twice-daily forecasts are enabled."""
    return bool(
        entry.options.get(
            CONF_ENABLE_DERIVED_FORECASTS,
            entry.data.get(CONF_ENABLE_DERIVED_FORECASTS, False),
        )
    )


def _stored_stations(entry: ConfigEntry) -> dict[int, Station]:
    """Return stored station metadata from entry options or data."""
    stations: dict[int, Station] = {}
    station_data = entry.options.get(
        CONF_STATIONS,
        entry.data.get(CONF_STATIONS, []),
    )
    for item in station_data:
        station = Station.from_api(item)
        stations[station.station_id] = station
    return stations


def _async_remove_station_registry_entries(
    hass: HomeAssistant, station_ids: list[int]
) -> None:
    """Remove entity and device registry entries for removed stations."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN

    for station_id in station_ids:
        station_id_str = str(station_id)
        for key in SENSOR_KEYS:
            unique_id = f"{DOMAIN}_{station_id}_{key}"
            entity_id = entity_registry.async_get_entity_id(
                SENSOR_DOMAIN,
                DOMAIN,
                unique_id,
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

        device = device_registry.async_get_device(
            identifiers={(DOMAIN, station_id_str)}
        )
        if device:
            device_registry.async_remove_device(device.id)


def _async_remove_device_tracker_registry_entries(
    hass: HomeAssistant,
    tracker_entity_ids: list[str],
) -> None:
    """Remove entity and device registry entries for removed device trackers."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN

    for tracker_entity_id in tracker_entity_ids:
        unique_id = f"{DOMAIN}_{tracker_entity_id}_weather"
        entity_id = entity_registry.async_get_entity_id(
            WEATHER_DOMAIN,
            DOMAIN,
            unique_id,
        )
        if entity_id:
            entity_registry.async_remove(entity_id)

        device = device_registry.async_get_device(
            identifiers={(DOMAIN, f"device_tracker_weather:{tracker_entity_id}")}
        )
        if device:
            device_registry.async_remove_device(device.id)
