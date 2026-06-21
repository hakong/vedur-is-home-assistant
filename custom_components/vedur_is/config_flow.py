"""Config flow for the Vedur.is integration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
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
    CONF_LOCATION,
    CONF_PLACE_QUERY,
    CONF_PLACE_RESULT,
    CONF_STATION_IDS,
    CONF_STATIONS,
    DOMAIN,
    ENTRY_TITLE,
    WEATHER_SENSOR_KEYS,
)
from .geo import Coordinate, nearest_stations

NEARBY_STATION_LIMIT = 12
PLACE_SEARCH_LIMIT = 5
PLACE_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
PLACE_SEARCH_USER_AGENT = (
    "vedur-is-home-assistant/0.1 "
    "(https://github.com/hakong/vedur-is-home-assistant)"
)
_PLACE_SEARCH_CACHE: dict[str, tuple[PlaceSearchResult, ...]] = {}


@dataclass(frozen=True, slots=True)
class PlaceSearchResult:
    """A location search result."""

    key: str
    label: str
    coordinate: Coordinate


class VedurIsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vedur.is."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._stations: dict[int, Station] = {}
        self._station_distances: dict[int, float] = {}
        self._nearby_coordinate: Coordinate | None = None
        self._place_results: dict[str, PlaceSearchResult] = {}
        self._selected_station_ids: list[int] = []

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
                return await self.async_step_menu()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user choose how to pick stations."""
        return self.async_show_menu(
            step_id="menu",
            menu_options=["settings", "search_place", "nearby", "select"],
        )

    async def async_step_search_place(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Search for a place before picking nearby stations."""
        errors: dict[str, str] = {}

        if user_input is not None:
            query = str(user_input.get(CONF_PLACE_QUERY, "")).strip()
            if not query:
                errors[CONF_PLACE_QUERY] = "required"
            else:
                try:
                    results = await _async_search_places(self.hass, query)
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except InvalidResponse:
                    errors["base"] = "invalid_response"
                else:
                    if not results:
                        errors["base"] = "place_not_found"
                    elif len(results) == 1:
                        self._nearby_coordinate = results[0].coordinate
                        return await self.async_step_nearby()
                    else:
                        self._place_results = {result.key: result for result in results}
                        return await self.async_step_select_place()

        return self.async_show_form(
            step_id="search_place",
            data_schema=_place_search_schema(),
            errors=errors,
        )

    async def async_step_select_place(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose a place search result."""
        errors: dict[str, str] = {}

        if user_input is not None:
            result = self._place_results.get(user_input.get(CONF_PLACE_RESULT))
            if result is None:
                errors[CONF_PLACE_RESULT] = "invalid_location"
            else:
                self._nearby_coordinate = result.coordinate
                return await self.async_step_nearby()

        return self.async_show_form(
            step_id="select_place",
            data_schema=_place_select_schema(self._place_results),
            errors=errors,
        )

    async def async_step_nearby(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a map location and show nearby stations."""
        errors: dict[str, str] = {}

        if user_input is not None:
            coordinate = _coordinate_from_location(user_input.get(CONF_LOCATION))
            if coordinate is None:
                errors[CONF_LOCATION] = "invalid_location"
            else:
                nearest = nearest_stations(
                    coordinate,
                    self._stations.values(),
                    limit=NEARBY_STATION_LIMIT,
                )
                if not nearest:
                    errors["base"] = "no_stations"
                else:
                    self._station_distances = {
                        station.station_id: distance
                        for station, distance in nearest
                    }
                    return await self.async_step_nearby_select()

        return self.async_show_form(
            step_id="nearby",
            data_schema=_location_schema(self.hass, self._nearby_coordinate),
            errors=errors,
        )

    async def async_step_nearby_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select from nearby stations."""
        station_choices = {
            station_id: self._stations[station_id]
            for station_id in self._station_distances
            if station_id in self._stations
        }
        errors: dict[str, str] = {}

        if user_input is not None:
            self._selected_station_ids = [
                int(station_id)
                for station_id in user_input.get(CONF_STATION_IDS, [])
            ]
            if already_configured := self._already_configured(
                self._selected_station_ids
            ):
                errors[CONF_STATION_IDS] = "already_configured"
                self.context["description_placeholders"] = {
                    "stations": ", ".join(
                        str(station_id) for station_id in already_configured
                    )
                }
            else:
                return await self.async_step_settings()

        return self.async_show_form(
            step_id="nearby_select",
            data_schema=_station_select_schema(
                station_choices,
                default_station_ids=self._selected_station_ids,
                station_distances=self._station_distances,
            ),
            errors=errors,
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select optional active stations."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._selected_station_ids = [
                int(station_id)
                for station_id in user_input.get(CONF_STATION_IDS, [])
            ]

            if already_configured := self._already_configured(
                self._selected_station_ids
            ):
                errors[CONF_STATION_IDS] = "already_configured"
                self.context["description_placeholders"] = {
                    "stations": ", ".join(
                        str(station_id) for station_id in already_configured
                    )
                }
            else:
                return await self.async_step_settings()

        return self.async_show_form(
            step_id="select",
            data_schema=_station_select_schema(
                self._stations,
                default_station_ids=self._selected_station_ids,
                station_distances={},
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Create the entry with selected settings."""
        if user_input is not None:
            selected_ids = self._selected_station_ids
            await self.async_set_unique_id(
                "stations:"
                + ",".join(str(station_id) for station_id in sorted(selected_ids))
            )
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=ENTRY_TITLE,
                data=_entry_data_from_settings(
                    self._stations,
                    selected_ids,
                    user_input,
                ),
            )

        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(
                device_tracker_entity_ids=[],
                enable_device_tracker_weather=True,
                enable_home_weather=True,
                enable_person_weather=True,
                enable_station_weather=True,
                enable_derived_forecasts=False,
            ),
        )

    async def _async_get_stations(self) -> list[Station]:
        """Fetch all active automatic weather stations."""
        client = VedurIsApiClient(aiohttp_client.async_get_clientsession(self.hass))
        return await client.async_get_stations()

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
        self._station_distances: dict[int, float] = {}
        self._nearby_coordinate: Coordinate | None = None
        self._place_results: dict[str, PlaceSearchResult] = {}
        self._selected_station_ids: list[int] = []

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
            return await self.async_step_menu()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
            errors=errors,
        )

    async def async_step_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Let the user choose how to pick stations."""
        return self.async_show_menu(
            step_id="menu",
            menu_options=["settings", "search_place", "nearby", "select"],
        )

    async def async_step_search_place(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Search for a place before picking nearby stations."""
        errors: dict[str, str] = {}

        if user_input is not None:
            query = str(user_input.get(CONF_PLACE_QUERY, "")).strip()
            if not query:
                errors[CONF_PLACE_QUERY] = "required"
            else:
                try:
                    results = await _async_search_places(self.hass, query)
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except InvalidResponse:
                    errors["base"] = "invalid_response"
                else:
                    if not results:
                        errors["base"] = "place_not_found"
                    elif len(results) == 1:
                        self._nearby_coordinate = results[0].coordinate
                        return await self.async_step_nearby()
                    else:
                        self._place_results = {result.key: result for result in results}
                        return await self.async_step_select_place()

        return self.async_show_form(
            step_id="search_place",
            data_schema=_place_search_schema(),
            errors=errors,
        )

    async def async_step_select_place(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose a place search result."""
        errors: dict[str, str] = {}

        if user_input is not None:
            result = self._place_results.get(user_input.get(CONF_PLACE_RESULT))
            if result is None:
                errors[CONF_PLACE_RESULT] = "invalid_location"
            else:
                self._nearby_coordinate = result.coordinate
                return await self.async_step_nearby()

        return self.async_show_form(
            step_id="select_place",
            data_schema=_place_select_schema(self._place_results),
            errors=errors,
        )

    async def async_step_nearby(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Pick a map location and show nearby stations."""
        errors: dict[str, str] = {}

        if user_input is not None:
            coordinate = _coordinate_from_location(user_input.get(CONF_LOCATION))
            if coordinate is None:
                errors[CONF_LOCATION] = "invalid_location"
            else:
                nearest = nearest_stations(
                    coordinate,
                    self._stations.values(),
                    limit=NEARBY_STATION_LIMIT,
                )
                if not nearest:
                    errors["base"] = "no_stations"
                else:
                    self._station_distances = {
                        station.station_id: distance
                        for station, distance in nearest
                    }
                    return await self.async_step_nearby_select()

        return self.async_show_form(
            step_id="nearby",
            data_schema=_location_schema(self.hass, self._nearby_coordinate),
            errors=errors,
        )

    async def async_step_nearby_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select from nearby stations."""
        station_choices = {
            station_id: self._stations[station_id]
            for station_id in self._station_distances
            if station_id in self._stations
        }
        errors: dict[str, str] = {}

        if user_input is not None:
            current_ids = _configured_station_ids(self._config_entry)
            selected_nearby_ids = [
                int(station_id)
                for station_id in user_input.get(CONF_STATION_IDS, [])
            ]
            self._selected_station_ids = _dedupe_station_ids(
                station_id
                for station_id in [*current_ids, *selected_nearby_ids]
                if station_id in self._stations
            )
            if already_configured := _already_configured(
                self.hass.config_entries.async_entries(DOMAIN),
                self._selected_station_ids,
                ignore_entry_id=self._config_entry.entry_id,
            ):
                errors[CONF_STATION_IDS] = "already_configured"
                self.context["description_placeholders"] = {
                    "stations": ", ".join(
                        str(station_id) for station_id in already_configured
                    )
                }
            else:
                return self._async_update_entry(self._selected_station_ids)

        return self.async_show_form(
            step_id="nearby_select",
            data_schema=_station_select_schema(
                station_choices,
                default_station_ids=[],
                station_distances=self._station_distances,
            ),
            errors=errors,
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Update the selected stations."""
        errors: dict[str, str] = {}
        current_ids = _configured_station_ids(self._config_entry)
        default_station_ids = self._selected_station_ids or current_ids

        if user_input is not None:
            selected_ids = [
                int(station_id)
                for station_id in user_input.get(CONF_STATION_IDS, [])
            ]

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
                return self._async_update_entry(selected_ids)

        return self.async_show_form(
            step_id="select",
            data_schema=_station_select_schema(
                self._stations,
                default_station_ids=[
                    station_id
                    for station_id in default_station_ids
                    if station_id in self._stations
                ],
                station_distances={},
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Update integration settings without changing stations."""
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
            return self._async_update_entry(
                _configured_station_ids(self._config_entry),
                settings_input=user_input,
            )

        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(
                device_tracker_entity_ids=current_device_tracker_entity_ids,
                enable_device_tracker_weather=enable_device_tracker_weather,
                enable_home_weather=enable_home_weather,
                enable_person_weather=enable_person_weather,
                enable_station_weather=enable_station_weather,
                enable_derived_forecasts=enable_derived_forecasts,
            ),
        )

    def _async_update_entry(
        self,
        station_ids: list[int],
        *,
        settings_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Update the config entry data and clean up removed registry entries."""
        current_ids = _configured_station_ids(self._config_entry)
        current_device_tracker_entity_ids = _configured_device_tracker_entity_ids(
            self._config_entry
        )
        selected_ids = _dedupe_station_ids(
            station_id for station_id in station_ids if station_id in self._stations
        )
        entry_data = _entry_data_from_settings(
            self._stations,
            selected_ids,
            settings_input,
            config_entry=self._config_entry,
        )
        device_tracker_entity_ids = entry_data[CONF_DEVICE_TRACKER_ENTITY_IDS]
        enable_device_tracker_weather = entry_data[
            CONF_ENABLE_DEVICE_TRACKER_WEATHER
        ]
        removed_ids = sorted(set(current_ids) - set(selected_ids))
        removed_device_tracker_entity_ids = sorted(
            set(current_device_tracker_entity_ids) - set(device_tracker_entity_ids)
        )
        if not enable_device_tracker_weather:
            removed_device_tracker_entity_ids = current_device_tracker_entity_ids

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

    async def _async_get_stations(self) -> list[Station]:
        """Fetch all active automatic weather stations."""
        client = VedurIsApiClient(aiohttp_client.async_get_clientsession(self.hass))
        return await client.async_get_stations()


def _settings_schema(
    *,
    device_tracker_entity_ids: list[str],
    enable_device_tracker_weather: bool,
    enable_home_weather: bool,
    enable_person_weather: bool,
    enable_station_weather: bool,
    enable_derived_forecasts: bool,
) -> vol.Schema:
    """Return the integration settings schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_DEVICE_TRACKER_ENTITY_IDS,
                default=device_tracker_entity_ids,
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="device_tracker",
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_ENABLE_DEVICE_TRACKER_WEATHER,
                default=enable_device_tracker_weather,
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_HOME_WEATHER,
                default=enable_home_weather,
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_PERSON_WEATHER,
                default=enable_person_weather,
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_STATION_WEATHER,
                default=enable_station_weather,
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_ENABLE_DERIVED_FORECASTS,
                default=enable_derived_forecasts,
            ): selector.BooleanSelector(),
        }
    )


def _station_select_schema(
    stations: dict[int, Station],
    *,
    default_station_ids: list[int],
    station_distances: Mapping[int, float],
) -> vol.Schema:
    """Return a focused station-only selection schema."""
    options = [
        selector.SelectOptionDict(
            value=str(station.station_id),
            label=_station_selector_label(
                station,
                station_distances.get(station.station_id),
            ),
        )
        for station in sorted(
            stations.values(),
            key=lambda station: _station_sort_key(station, station_distances),
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
            )
        }
    )


def _station_selector_label(
    station: Station,
    distance: float | None = None,
) -> str:
    """Return a human-friendly station selector label."""
    label = f"{station.name} ({station.station_id})"
    if distance is not None:
        label = f"{distance:05.1f} km - {label}"
    if station.owner:
        label = f"{label} - {station.owner}"
    return label


def _station_sort_key(
    station: Station,
    station_distances: Mapping[int, float],
) -> tuple[bool, float, str]:
    """Return a sort key for station selectors."""
    distance = station_distances.get(station.station_id)
    return (distance is None, distance or 0.0, station.name.casefold())


def _place_search_schema() -> vol.Schema:
    """Return a place search schema."""
    return vol.Schema({vol.Required(CONF_PLACE_QUERY): str})


def _place_select_schema(results: Mapping[str, PlaceSearchResult]) -> vol.Schema:
    """Return a place result selection schema."""
    options = [
        selector.SelectOptionDict(
            value=result.key,
            label=result.label,
        )
        for result in results.values()
    ]
    default = options[0]["value"] if options else None
    return vol.Schema(
        {
            vol.Required(CONF_PLACE_RESULT, default=default): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _location_schema(
    hass: HomeAssistant,
    default_coordinate: Coordinate | None = None,
) -> vol.Schema:
    """Return a location picker schema."""
    if default_coordinate is None:
        default_location = {
            "latitude": hass.config.latitude,
            "longitude": hass.config.longitude,
        }
    else:
        default_location = {
            "latitude": default_coordinate.latitude,
            "longitude": default_coordinate.longitude,
        }
    return vol.Schema(
        {
            vol.Required(
                CONF_LOCATION,
                default=default_location,
            ): selector.LocationSelector(
                selector.LocationSelectorConfig(
                    icon="mdi:weather-cloudy",
                )
            )
        }
    )


def _coordinate_from_location(value: Any) -> Coordinate | None:
    """Return a coordinate from a location selector value."""
    if not isinstance(value, Mapping):
        return None
    latitude = value.get("latitude")
    longitude = value.get("longitude")
    if latitude is None or longitude is None:
        return None
    try:
        return Coordinate(float(latitude), float(longitude))
    except (TypeError, ValueError):
        return None


async def _async_search_places(
    hass: HomeAssistant,
    query: str,
) -> tuple[PlaceSearchResult, ...]:
    """Search for Icelandic places using OpenStreetMap Nominatim."""
    cache_key = " ".join(query.casefold().split())
    if cache_key in _PLACE_SEARCH_CACHE:
        return _PLACE_SEARCH_CACHE[cache_key]

    session = aiohttp_client.async_get_clientsession(hass)
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": str(PLACE_SEARCH_LIMIT),
        "countrycodes": "is",
        "accept-language": "is,en",
    }
    headers = {"User-Agent": PLACE_SEARCH_USER_AGENT}

    try:
        async with session.get(
            PLACE_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=10,
        ) as response:
            if response.status >= 500:
                raise CannotConnect
            if response.status >= 400:
                raise InvalidResponse
            payload = await response.json(content_type=None)
    except aiohttp.ClientError as err:
        raise CannotConnect from err
    except TimeoutError as err:
        raise CannotConnect from err

    if not isinstance(payload, list):
        raise InvalidResponse

    results: list[PlaceSearchResult] = []
    for index, item in enumerate(payload):
        if not isinstance(item, Mapping):
            continue
        latitude = item.get("lat")
        longitude = item.get("lon")
        label = item.get("display_name") or query
        try:
            coordinate = Coordinate(float(latitude), float(longitude))
        except (TypeError, ValueError):
            continue
        key = f"{index}:{item.get('place_id', index)}"
        results.append(
            PlaceSearchResult(
                key=key,
                label=str(label),
                coordinate=coordinate,
            )
        )

    _PLACE_SEARCH_CACHE[cache_key] = tuple(results)
    return _PLACE_SEARCH_CACHE[cache_key]


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


def _entry_data_from_settings(
    stations: Mapping[int, Station],
    station_ids: list[int],
    settings_input: dict[str, Any] | None,
    *,
    config_entry: ConfigEntry | None = None,
) -> dict[str, Any]:
    """Return stored config entry data."""
    selected_ids = _dedupe_station_ids(
        station_id for station_id in station_ids if station_id in stations
    )
    return {
        CONF_STATION_IDS: selected_ids,
        CONF_STATIONS: [
            stations[station_id].as_storage_dict()
            for station_id in selected_ids
        ],
        CONF_DEVICE_TRACKER_ENTITY_IDS: _setting_value(
            settings_input,
            config_entry,
            CONF_DEVICE_TRACKER_ENTITY_IDS,
            [],
        ),
        CONF_ENABLE_DEVICE_TRACKER_WEATHER: _setting_value(
            settings_input,
            config_entry,
            CONF_ENABLE_DEVICE_TRACKER_WEATHER,
            True,
        ),
        CONF_ENABLE_HOME_WEATHER: _setting_value(
            settings_input,
            config_entry,
            CONF_ENABLE_HOME_WEATHER,
            True,
        ),
        CONF_ENABLE_PERSON_WEATHER: _setting_value(
            settings_input,
            config_entry,
            CONF_ENABLE_PERSON_WEATHER,
            True,
        ),
        CONF_ENABLE_STATION_WEATHER: _setting_value(
            settings_input,
            config_entry,
            CONF_ENABLE_STATION_WEATHER,
            True,
        ),
        CONF_ENABLE_DERIVED_FORECASTS: _setting_value(
            settings_input,
            config_entry,
            CONF_ENABLE_DERIVED_FORECASTS,
            False,
        ),
    }


def _setting_value(
    settings_input: dict[str, Any] | None,
    config_entry: ConfigEntry | None,
    key: str,
    default: Any,
) -> Any:
    """Return a setting from input, existing entry data, or default."""
    if settings_input is not None and key in settings_input:
        return settings_input[key]
    if config_entry is not None:
        return config_entry.options.get(key, config_entry.data.get(key, default))
    return default


def _dedupe_station_ids(station_ids: Iterable[int]) -> list[int]:
    """Return station IDs without duplicates while preserving order."""
    deduped: list[int] = []
    seen: set[int] = set()
    for station_id in station_ids:
        if station_id in seen:
            continue
        deduped.append(station_id)
        seen.add(station_id)
    return deduped


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
        for key in WEATHER_SENSOR_KEYS:
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
        for key in WEATHER_SENSOR_KEYS:
            entity_id = entity_registry.async_get_entity_id(
                SENSOR_DOMAIN,
                DOMAIN,
                f"{DOMAIN}_{tracker_entity_id}_{key}",
            )
            if entity_id:
                entity_registry.async_remove(entity_id)

        device = device_registry.async_get_device(
            identifiers={(DOMAIN, f"device_tracker_weather:{tracker_entity_id}")}
        )
        if device:
            device_registry.async_remove_device(device.id)
