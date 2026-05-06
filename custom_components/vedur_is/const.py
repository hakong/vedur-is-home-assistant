"""Constants for the Icelandic Met Office Weather integration."""

from __future__ import annotations

DOMAIN = "vedur_is"
INTEGRATION_NAME = "Icelandic Met Office Weather"
ENTRY_TITLE = INTEGRATION_NAME
ATTRIBUTION = "Data provided by the Icelandic Met Office (vedur.is)."

PLATFORMS: list[str] = ["sensor", "weather"]

CONF_STATION_IDS = "station_ids"
CONF_STATIONS = "stations"
CONF_ENABLE_HOME_WEATHER = "enable_home_weather"
CONF_ENABLE_PERSON_WEATHER = "enable_person_weather"
CONF_ENABLE_STATION_WEATHER = "enable_station_weather"

SENSOR_KEYS = ("t", "rh", "td", "f", "fg", "d", "p", "r")

DEFAULT_SCAN_INTERVAL_MINUTES = 60

ATTR_OBSERVATION_TIME = "observation_time"
ATTR_OBSERVATION_UNAVAILABLE_FIELDS = "observation_unavailable_fields"
ATTR_OBSERVATION_VALUE_SOURCES = "observation_value_sources"
ATTR_STATION_ID = "station_id"

ATTR_FORECAST_STATION_DISTANCE_KM = "forecast_station_distance_km"
ATTR_FORECAST_STATION_ID = "forecast_station_id"
ATTR_FORECAST_STATION_NAME = "forecast_station_name"
ATTR_FORECAST_USES_NEARBY_STATION = "forecast_uses_nearby_station"
ATTR_OBSERVATION_STATION_DISTANCE_KM = "observation_station_distance_km"
ATTR_OBSERVATION_STATION_ID = "observation_station_id"
ATTR_OBSERVATION_STATION_NAME = "observation_station_name"
ATTR_OBSERVATION_SOURCE = "observation_source"
ATTR_STATION_HAS_DIRECT_FORECAST = "station_has_direct_forecast"
