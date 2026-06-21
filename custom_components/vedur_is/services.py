"""Service actions for the Icelandic Met Office Weather integration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_TYPE
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import aiohttp_client

from .api import (
    ForecastPoint,
    Observation,
    Station,
    VedurIsApiClient,
)
from .const import DOMAIN
from .forecast_utils import (
    daily_forecast_dicts,
    hourly_forecast_dicts,
    twice_daily_forecast_dicts,
)
from .geo import Coordinate, nearest_station
from .weather_coordinator import VedurIsWeatherData

SERVICE_GET_FORECAST_FOR_LOCATION = "get_forecast_for_location"

ATTR_FORECAST = "forecast"
ATTR_FORECAST_STATION = "forecast_station"
ATTR_LATITUDE = "latitude"
ATTR_LONGITUDE = "longitude"
ATTR_OBSERVATION = "observation"
ATTR_OBSERVATION_STATION = "observation_station"

FORECAST_TYPE_HOURLY = "hourly"
FORECAST_TYPE_DAILY = "daily"
FORECAST_TYPE_TWICE_DAILY = "twice_daily"
FORECAST_TYPES = (
    FORECAST_TYPE_HOURLY,
    FORECAST_TYPE_DAILY,
    FORECAST_TYPE_TWICE_DAILY,
)

GET_FORECAST_FOR_LOCATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_LATITUDE): vol.Coerce(float),
        vol.Required(ATTR_LONGITUDE): vol.Coerce(float),
        vol.Optional(CONF_TYPE, default=FORECAST_TYPE_HOURLY): vol.In(FORECAST_TYPES),
    }
)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up integration service actions."""

    async def get_forecast_for_location(call: ServiceCall) -> ServiceResponse:
        """Return nearest Vedur current observation and forecast for a coordinate."""
        coordinate = Coordinate(
            call.data[ATTR_LATITUDE],
            call.data[ATTR_LONGITUDE],
        )
        forecast_type = call.data[CONF_TYPE]
        data = await _async_get_lookup_data(hass)

        observation_result = _nearest_observation_station(coordinate, data)
        forecast_result = _nearest_forecast_station(coordinate, data)
        if forecast_result is None:
            raise HomeAssistantError("No Vedur forecast station is available")

        forecast_station, forecast_distance = forecast_result
        station_forecast = data.forecasts[forecast_station.station_id]
        response: ServiceResponse = {
            ATTR_FORECAST_STATION: _station_response(
                forecast_station,
                forecast_distance,
            ),
            ATTR_FORECAST: _forecast_response(
                station_forecast.forecasts,
                forecast_type,
            ),
        }

        if observation_result is not None:
            observation_station, observation_distance = observation_result
            response[ATTR_OBSERVATION_STATION] = _station_response(
                observation_station,
                observation_distance,
            )
            response[ATTR_OBSERVATION] = _observation_response(
                data.observations[observation_station.station_id]
            )
        else:
            response[ATTR_OBSERVATION_STATION] = None
            response[ATTR_OBSERVATION] = None

        return response

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_FORECAST_FOR_LOCATION,
        get_forecast_for_location,
        schema=GET_FORECAST_FOR_LOCATION_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def _async_get_lookup_data(hass: HomeAssistant) -> VedurIsWeatherData:
    """Return weather lookup data from the coordinator or direct API calls."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = getattr(getattr(entry, "runtime_data", None), "data", None)
        if data is not None and data.stations and data.forecasts:
            return data

    client = VedurIsApiClient(aiohttp_client.async_get_clientsession(hass))
    stations = await client.async_get_stations(station_type=None)
    stations_by_id = {station.station_id: station for station in stations}
    aws_station_ids = [
        station.station_id
        for station in stations
        if station.station_type and station.station_type.casefold() == "sj"
    ]
    observations = await client.async_get_latest_observations(aws_station_ids)
    forecasts = await client.async_get_forecasts(stations_by_id.keys())
    return VedurIsWeatherData(
        stations=stations_by_id,
        observations=observations,
        forecasts=forecasts,
    )


def _nearest_observation_station(
    coordinate: Coordinate,
    data: VedurIsWeatherData,
) -> tuple[Station, float] | None:
    """Return the nearest station with current observation data."""
    stations = (
        data.stations[station_id]
        for station_id in data.observations
        if station_id in data.stations
    )
    return nearest_station(coordinate, stations)


def _nearest_forecast_station(
    coordinate: Coordinate,
    data: VedurIsWeatherData,
) -> tuple[Station, float] | None:
    """Return the nearest station with forecast data."""
    stations = (
        data.stations[station_id]
        for station_id in data.forecasts
        if station_id in data.stations
    )
    return nearest_station(coordinate, stations)


def _station_response(station: Station, distance: float) -> dict[str, Any]:
    """Return a JSON-serializable station response."""
    return {
        "station_id": station.station_id,
        "name": station.name,
        "abbreviation": station.abbr,
        "type": station.station_type,
        "owner": station.owner,
        "latitude": station.latitude,
        "longitude": station.longitude,
        "elevation": station.elevation,
        "distance_km": round(distance, 2),
    }


def _observation_response(observation: Observation) -> dict[str, Any]:
    """Return a JSON-serializable observation response."""
    return {
        "station_id": observation.station_id,
        "name": observation.name,
        "time": observation.time.isoformat() if observation.time else None,
        "values": _json_safe_mapping(observation.values),
        "value_sources": dict(observation.value_sources),
    }


def _forecast_response(
    points: Iterable[ForecastPoint],
    forecast_type: str,
) -> list[dict[str, Any]]:
    """Return forecast dictionaries for the requested forecast type."""
    if forecast_type == FORECAST_TYPE_DAILY:
        return daily_forecast_dicts(points)
    if forecast_type == FORECAST_TYPE_TWICE_DAILY:
        return twice_daily_forecast_dicts(points)
    return hourly_forecast_dicts(points)


def _json_safe_mapping(values: Iterable[tuple[str, Any]] | Any) -> dict[str, Any]:
    """Return a JSON-safe dictionary from an observation mapping."""
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in dict(values).items()
    }
