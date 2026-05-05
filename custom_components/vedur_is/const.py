"""Constants for the Vedur.is integration."""

from __future__ import annotations

DOMAIN = "vedur_is"

PLATFORMS: list[str] = ["sensor", "weather"]

CONF_STATION_IDS = "station_ids"
CONF_STATIONS = "stations"
CONF_ENABLE_PERSON_WEATHER = "enable_person_weather"

SENSOR_KEYS = ("t", "rh", "td", "f", "fg", "d", "p", "r")

DEFAULT_SCAN_INTERVAL_MINUTES = 60

ATTR_OBSERVATION_TIME = "observation_time"
ATTR_STATION_ID = "station_id"

ATTR_FORECAST_STATION_DISTANCE_KM = "forecast_station_distance_km"
ATTR_FORECAST_STATION_ID = "forecast_station_id"
ATTR_FORECAST_STATION_NAME = "forecast_station_name"
ATTR_OBSERVATION_STATION_DISTANCE_KM = "observation_station_distance_km"
ATTR_OBSERVATION_STATION_ID = "observation_station_id"
ATTR_OBSERVATION_STATION_NAME = "observation_station_name"
