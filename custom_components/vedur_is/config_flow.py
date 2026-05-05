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
    CONF_ENABLE_PERSON_WEATHER,
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
                enable_person_weather = user_input.get(
                    CONF_ENABLE_PERSON_WEATHER,
                    True,
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
                        CONF_ENABLE_PERSON_WEATHER: enable_person_weather,
                    },
                )

        return self.async_show_form(
            step_id="select",
            data_schema=self._select_schema(enable_person_weather=True),
            errors=errors,
        )

    async def _async_get_stations(self) -> list[Station]:
        """Fetch all active automatic weather stations."""
        client = VedurIsApiClient(aiohttp_client.async_get_clientsession(self.hass))
        return await client.async_get_stations()

    def _select_schema(self, *, enable_person_weather: bool) -> vol.Schema:
        """Return the station selection schema."""
        return _select_schema(
            self._stations,
            default_station_ids=[],
            default_enable_person_weather=enable_person_weather,
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
        enable_person_weather = _enable_person_weather(self._config_entry)

        if user_input is not None:
            selected_ids = [
                int(station_id)
                for station_id in user_input.get(CONF_STATION_IDS, [])
            ]
            enable_person_weather = user_input.get(
                CONF_ENABLE_PERSON_WEATHER,
                True,
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
                entry_data = {
                    CONF_STATION_IDS: selected_ids,
                    CONF_STATIONS: [
                        self._stations[station_id].as_storage_dict()
                        for station_id in selected_ids
                    ],
                    CONF_ENABLE_PERSON_WEATHER: enable_person_weather,
                }
                _async_remove_station_registry_entries(self.hass, removed_ids)
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
                default_enable_person_weather=enable_person_weather,
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
    default_enable_person_weather: bool,
) -> vol.Schema:
    """Return the station selection schema."""
    options = [
        selector.SelectOptionDict(
            value=str(station.station_id),
            label=f"{station.name} ({station.station_id})",
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
                CONF_ENABLE_PERSON_WEATHER,
                default=default_enable_person_weather,
            ): selector.BooleanSelector(),
        }
    )


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


def _enable_person_weather(entry: ConfigEntry) -> bool:
    """Return whether person-following weather entities should be created."""
    return bool(
        entry.options.get(
            CONF_ENABLE_PERSON_WEATHER,
            entry.data.get(CONF_ENABLE_PERSON_WEATHER, True),
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
