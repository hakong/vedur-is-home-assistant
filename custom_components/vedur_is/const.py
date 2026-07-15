"""Constants for the Icelandic Met Office Weather integration."""

from __future__ import annotations

DOMAIN = "vedur_is"
INTEGRATION_NAME = "Icelandic Met Office Weather"
ENTRY_TITLE = INTEGRATION_NAME
ATTRIBUTION = "Data provided by the Icelandic Met Office (vedur.is)."
DERIVED_FORECAST_ATTRIBUTION = (
    "Data provided by the Icelandic Met Office (vedur.is). "
    "Daily and twice-daily forecasts are derived by this integration."
)

PLATFORMS: list[str] = ["sensor", "weather"]

CONF_STATION_IDS = "station_ids"
CONF_STATIONS = "stations"
CONF_DEVICE_TRACKER_ENTITY_IDS = "device_tracker_entity_ids"
CONF_ENABLE_DEVICE_TRACKER_WEATHER = "enable_device_tracker_weather"
CONF_ENABLE_HOME_WEATHER = "enable_home_weather"
CONF_ENABLE_PERSON_WEATHER = "enable_person_weather"
CONF_ENABLE_STATION_WEATHER = "enable_station_weather"
CONF_ENABLE_DERIVED_FORECASTS = "enable_derived_forecasts"
CONF_LOCATION = "location"
CONF_PLACE_QUERY = "place_query"
CONF_PLACE_RESULT = "place_result"

SENSOR_KEYS = ("t", "rh", "td", "f", "fg", "d", "p", "r")
WEATHER_SENSOR_KEYS = ("condition", *SENSOR_KEYS)

DEFAULT_SCAN_INTERVAL_MINUTES = 10
ALERT_SCAN_INTERVAL_MINUTES = 15

ATTR_ALERTS = "alerts"
ATTR_ALERT_AREAS = "alert_areas"
ATTR_ALERT_COUNT = "alert_count"
ATTR_ALERT_HIGHEST_COLOR = "alert_highest_color"
ATTR_ALERT_HIGHEST_SEVERITY = "alert_highest_severity"
ATTR_DEVICE_TRACKER_ENTITY_ID = "device_tracker_entity_id"
ATTR_DATA_STALE = "data_stale"
ATTR_LAST_SUCCESSFUL_UPDATE = "last_successful_update"
ATTR_OBSERVATION_TIME = "observation_time"
ATTR_OBSERVATION_UNAVAILABLE_FIELDS = "observation_unavailable_fields"
ATTR_OBSERVATION_VALUE_SOURCES = "observation_value_sources"
ATTR_STALE_SOURCES = "stale_sources"
ATTR_STATION_ID = "station_id"
ATTR_SOURCE_ERRORS = "source_errors"

ATTR_FORECAST_STATION_DISTANCE_KM = "forecast_station_distance_km"
ATTR_FORECAST_STATION_ID = "forecast_station_id"
ATTR_FORECAST_STATION_NAME = "forecast_station_name"
ATTR_FORECAST_USES_NEARBY_STATION = "forecast_uses_nearby_station"
ATTR_OBSERVATION_STATION_DISTANCE_KM = "observation_station_distance_km"
ATTR_OBSERVATION_STATION_ID = "observation_station_id"
ATTR_OBSERVATION_STATION_NAME = "observation_station_name"
ATTR_OBSERVATION_SOURCE = "observation_source"
ATTR_STATION_HAS_DIRECT_FORECAST = "station_has_direct_forecast"
