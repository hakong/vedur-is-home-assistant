"""Weather platform for the Icelandic Met Office Weather integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.weather import (
    Forecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import UnitOfPressure, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .api import (
    ForecastPoint,
    OBSERVATION_SOURCE_UNAVAILABLE,
    Observation,
    Station,
    StationForecast,
)
from .const import (
    ATTR_DATA_STALE,
    ATTR_DEVICE_TRACKER_ENTITY_ID,
    ATTR_FORECAST_STATION_DISTANCE_KM,
    ATTR_FORECAST_STATION_ID,
    ATTR_FORECAST_STATION_NAME,
    ATTR_FORECAST_USES_NEARBY_STATION,
    ATTR_LAST_SUCCESSFUL_UPDATE,
    ATTR_OBSERVATION_STATION_DISTANCE_KM,
    ATTR_OBSERVATION_STATION_ID,
    ATTR_OBSERVATION_STATION_NAME,
    ATTR_OBSERVATION_TIME,
    ATTR_OBSERVATION_UNAVAILABLE_FIELDS,
    ATTR_OBSERVATION_VALUE_SOURCES,
    ATTR_SOURCE_ERRORS,
    ATTR_STALE_SOURCES,
    ATTR_STATION_ID,
    ATTR_STATION_HAS_DIRECT_FORECAST,
    ATTRIBUTION,
    CONF_DEVICE_TRACKER_ENTITY_IDS,
    CONF_ENABLE_DEVICE_TRACKER_WEATHER,
    CONF_ENABLE_DERIVED_FORECASTS,
    CONF_ENABLE_HOME_WEATHER,
    CONF_ENABLE_PERSON_WEATHER,
    CONF_ENABLE_STATION_WEATHER,
    CONF_STATION_IDS,
    CONF_STATIONS,
    DERIVED_FORECAST_ATTRIBUTION,
    DOMAIN,
    SENSOR_KEYS,
)
from .geo import (
    Coordinate,
    nearest_station,
    resolve_person_coordinate,
    resolve_tracker_coordinate,
)
from .forecast_utils import (
    daily_forecast_dicts,
    hourly_forecast_dicts,
    twice_daily_forecast_dicts,
)
from .weather_coordinator import VedurIsWeatherDataUpdateCoordinator
from .weather_utils import condition_from_forecast_text, condition_from_observation

MAX_OBSERVATION_DISTANCE_KM = 150.0
HOURLY_FORECAST_FEATURES = WeatherEntityFeature.FORECAST_HOURLY
DERIVED_FORECAST_FEATURES = (
    WeatherEntityFeature.FORECAST_DAILY
    | WeatherEntityFeature.FORECAST_HOURLY
    | WeatherEntityFeature.FORECAST_TWICE_DAILY
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up person-following Vedur.is weather entities."""
    coordinator: VedurIsWeatherDataUpdateCoordinator = entry.runtime_data

    entry_config = entry.options or entry.data
    stations: dict[int, Station] = {}
    for station_data in entry_config.get(CONF_STATIONS, []):
        station = Station.from_api(station_data)
        stations[station.station_id] = station

    entities: list[WeatherEntity] = []
    if _should_create_home_weather(hass, entry):
        entities.append(VedurIsHomeWeatherEntity(coordinator))
    if _should_create_person_weather(hass, entry):
        entities.extend(
            VedurIsPersonWeatherEntity(coordinator, person_entity_id)
            for person_entity_id in hass.states.async_entity_ids("person")
        )
    if entry_config.get(CONF_ENABLE_DEVICE_TRACKER_WEATHER, True):
        entities.extend(
            VedurIsDeviceTrackerWeatherEntity(coordinator, tracker_entity_id)
            for tracker_entity_id in entry_config.get(
                CONF_DEVICE_TRACKER_ENTITY_IDS,
                [],
            )
        )
    if entry_config.get(CONF_ENABLE_STATION_WEATHER, True):
        entities.extend(
            VedurIsStationWeatherEntity(
                coordinator,
                stations.get(station_id)
                or coordinator.data.stations.get(station_id)
                or Station(
                    station_id=station_id,
                    name=str(station_id),
                    abbr=None,
                    station_type="sj",
                    latitude=None,
                    longitude=None,
                    elevation=None,
                    owner=None,
                    start_year=None,
                ),
            )
            for station_id in (
                int(station_id)
                for station_id in entry_config.get(CONF_STATION_IDS, [])
            )
        )
    async_add_entities(entities)


class VedurIsPersonWeatherEntity(
    SingleCoordinatorWeatherEntity[VedurIsWeatherDataUpdateCoordinator],
    WeatherEntity,
):
    """Weather entity that follows one Home Assistant person."""

    _attr_has_entity_name = False
    _attr_native_precipitation_unit = "mm"
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND

    def __init__(
        self,
        coordinator: VedurIsWeatherDataUpdateCoordinator,
        person_entity_id: str,
    ) -> None:
        """Initialize the weather entity."""
        super().__init__(coordinator)
        self._person_entity_id = person_entity_id
        self._attr_unique_id = f"{DOMAIN}_{person_entity_id}_weather"
        self._attr_name = _person_name(coordinator.hass.states.get(person_entity_id))
        self._attr_device_info = self._person_device_info(
            person_entity_id,
            self._attr_name,
        )

    @property
    def supported_features(self) -> WeatherEntityFeature:
        """Return supported forecast feature flags."""
        return _forecast_features(self.coordinator)

    @property
    def attribution(self) -> str:
        """Return data attribution text."""
        return _weather_attribution(self.coordinator)

    async def async_added_to_hass(self) -> None:
        """Subscribe to person state changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._person_entity_id],
                self._async_person_state_changed,
            )
        )

    @callback
    def _async_person_state_changed(self, event: Event[State | None]) -> None:
        """Write a new weather state when the tracked person moves."""
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_update_listeners(None))

    @property
    def available(self) -> bool:
        """Return if weather data is available for the person's location."""
        observation_result = self._nearest_observation_station
        forecast_result = self._nearest_forecast_station
        return (
            super().available
            and self._person_coordinate is not None
            and (
                (
                    observation_result is not None
                    and observation_result[1] <= MAX_OBSERVATION_DISTANCE_KM
                    and self._observation is not None
                )
                or (forecast_result is not None and self._forecast is not None)
            )
        )

    @property
    def condition(self) -> str | None:
        """Return the current weather condition."""
        forecast_point = self._current_forecast_point
        if forecast_point is not None:
            return condition_from_forecast_text(
                forecast_point.weather_text,
                forecast_point.time,
            )

        observation = self._observation
        if observation is not None:
            return condition_from_observation(observation)
        return None

    @property
    def native_temperature(self) -> float | None:
        """Return the temperature in Celsius."""
        return self._observation_value("t")

    @property
    def humidity(self) -> float | None:
        """Return the relative humidity percentage."""
        return self._observation_value("rh")

    @property
    def native_dew_point(self) -> float | None:
        """Return the dew point in Celsius."""
        return self._observation_value("td")

    @property
    def native_wind_speed(self) -> float | None:
        """Return the wind speed in meters per second."""
        return self._observation_value("f")

    @property
    def native_wind_gust_speed(self) -> float | None:
        """Return the wind gust speed in meters per second."""
        return self._observation_value("fg")

    @property
    def wind_bearing(self) -> float | str | None:
        """Return the wind bearing."""
        observation = self._observation
        if observation is None:
            return None
        return observation.value("d")

    @property
    def native_pressure(self) -> float | None:
        """Return the pressure in hPa."""
        return self._observation_value("p")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return station metadata for the current person location."""
        observation_station = self._nearest_observation_station
        forecast_station = self._nearest_forecast_station

        attrs: dict[str, Any] = {"person_entity_id": self._person_entity_id}
        if observation_station is not None:
            station, station_distance = observation_station
            attrs.update(
                {
                    ATTR_OBSERVATION_STATION_ID: station.station_id,
                    ATTR_OBSERVATION_STATION_NAME: station.name,
                    ATTR_OBSERVATION_STATION_DISTANCE_KM: round(
                        station_distance,
                        2,
                    ),
                }
            )

        if forecast_station is not None:
            station, station_distance = forecast_station
            attrs.update(
                {
                    ATTR_FORECAST_STATION_ID: station.station_id,
                    ATTR_FORECAST_STATION_NAME: station.name,
                    ATTR_FORECAST_STATION_DISTANCE_KM: round(station_distance, 2),
                }
            )

        precipitation = self._observation_value("r")
        if precipitation is not None:
            attrs["precipitation"] = precipitation

        observation = self._observation
        if observation is not None and observation.time is not None:
            attrs[ATTR_OBSERVATION_TIME] = observation.time.isoformat()

        attrs.update(_observation_diagnostics(observation))
        attrs.update(_data_diagnostics(self.coordinator.data))
        return attrs

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        """Return hourly forecasts for the person's nearest forecast station."""
        station_forecast = self._forecast
        if station_forecast is None:
            return []

        return _ha_forecasts(hourly_forecast_dicts(station_forecast.forecasts))

    async def async_forecast_daily(self) -> list[Forecast] | None:
        """Return daily forecasts for the person's nearest forecast station."""
        station_forecast = self._forecast
        if station_forecast is None:
            return []

        return _ha_forecasts(daily_forecast_dicts(station_forecast.forecasts))

    async def async_forecast_twice_daily(self) -> list[Forecast] | None:
        """Return twice-daily forecasts for the nearest forecast station."""
        station_forecast = self._forecast
        if station_forecast is None:
            return []

        return _ha_forecasts(twice_daily_forecast_dicts(station_forecast.forecasts))

    @property
    def _forecast(self) -> StationForecast | None:
        """Return the XML forecast for the nearest forecast station."""
        station_result = self._nearest_forecast_station
        if station_result is None or self.coordinator.data is None:
            return None

        station = station_result[0]
        return self.coordinator.data.forecasts.get(station.station_id)

    @property
    def _person_coordinate(self) -> Coordinate | None:
        """Return the current person coordinate, if known."""
        state = self.hass.states.get(self._person_entity_id)
        if state is None:
            return None

        return resolve_person_coordinate(
            state.state,
            state.attributes,
            _zones_by_name(self.hass),
        )

    @property
    def _nearest_observation_station(self) -> tuple[Station, float] | None:
        """Return the nearest station with a current observation."""
        if self.coordinator.data is None or self._person_coordinate is None:
            return None

        stations = (
            self.coordinator.data.stations[station_id]
            for station_id in self.coordinator.data.observations
            if station_id in self.coordinator.data.stations
        )
        return nearest_station(self._person_coordinate, stations)

    @property
    def _nearest_forecast_station(self) -> tuple[Station, float] | None:
        """Return the nearest station with XML forecast data."""
        if self.coordinator.data is None or self._person_coordinate is None:
            return None

        stations = (
            self.coordinator.data.stations[station_id]
            for station_id in self.coordinator.data.forecasts
            if station_id in self.coordinator.data.stations
        )
        return nearest_station(self._person_coordinate, stations)

    @property
    def _observation(self) -> Observation | None:
        """Return the observation for the nearest observation station."""
        station_result = self._nearest_observation_station
        if station_result is None or self.coordinator.data is None:
            return None

        return self.coordinator.data.observations.get(station_result[0].station_id)

    @property
    def _current_forecast_point(self) -> ForecastPoint | None:
        """Return the current or next forecast point for this person."""
        station_result = self._nearest_forecast_station
        if station_result is None or self.coordinator.data is None:
            return None

        station_forecast = self._forecast
        if station_forecast is None:
            return None

        now = datetime.now()
        for forecast in station_forecast.forecasts:
            if forecast.time >= now:
                return forecast
        return station_forecast.forecasts[-1]

    def _observation_value(self, key: str) -> float | None:
        """Return a numeric observation value."""
        observation = self._observation
        if observation is None:
            return None

        value = observation.value(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _person_device_info(self, person_entity_id: str, name: str) -> DeviceInfo:
        """Return device registry metadata for a person-following weather entity."""
        return {
            "identifiers": {(DOMAIN, f"person_weather:{person_entity_id}")},
            "name": name,
            "manufacturer": "Icelandic Met Office",
            "model": "Person-following weather",
        }


class VedurIsHomeWeatherEntity(VedurIsPersonWeatherEntity):
    """Weather entity that follows the Home Assistant home zone."""

    def __init__(
        self,
        coordinator: VedurIsWeatherDataUpdateCoordinator,
    ) -> None:
        """Initialize the home weather entity."""
        super().__init__(coordinator, "zone.home")
        self._attr_unique_id = f"{DOMAIN}_home_weather"
        self._attr_name = "Home"
        self._attr_device_info = self._device_info()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return station metadata for the current home location."""
        attrs = super().extra_state_attributes
        attrs.pop("person_entity_id", None)
        attrs["zone_entity_id"] = self._person_entity_id
        return attrs

    def _device_info(self) -> DeviceInfo:
        """Return device registry metadata for the home weather entity."""
        return {
            "identifiers": {(DOMAIN, "home_weather")},
            "name": "Home",
            "manufacturer": "Icelandic Met Office",
            "model": "Home weather",
        }


class VedurIsDeviceTrackerWeatherEntity(VedurIsPersonWeatherEntity):
    """Weather entity that follows a configured device tracker."""

    def __init__(
        self,
        coordinator: VedurIsWeatherDataUpdateCoordinator,
        tracker_entity_id: str,
    ) -> None:
        """Initialize the device tracker weather entity."""
        super().__init__(coordinator, tracker_entity_id)
        self._device_tracker_entity_id = tracker_entity_id
        self._attr_unique_id = f"{DOMAIN}_{tracker_entity_id}_weather"
        self._attr_name = _entity_name(
            coordinator.hass.states.get(tracker_entity_id),
            tracker_entity_id,
        )
        self._attr_device_info = self._device_info(
            tracker_entity_id,
            self._attr_name,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return station metadata for the current tracker location."""
        attrs = super().extra_state_attributes
        attrs.pop("person_entity_id", None)
        attrs[ATTR_DEVICE_TRACKER_ENTITY_ID] = self._device_tracker_entity_id
        return attrs

    @property
    def _person_coordinate(self) -> Coordinate | None:
        """Return the current device tracker coordinate, if known."""
        state = self.hass.states.get(self._device_tracker_entity_id)
        if state is None:
            return None

        return resolve_tracker_coordinate(state.attributes)

    def _device_info(self, tracker_entity_id: str, name: str) -> DeviceInfo:
        """Return device registry metadata for a tracker weather entity."""
        return {
            "identifiers": {(DOMAIN, f"device_tracker_weather:{tracker_entity_id}")},
            "name": name,
            "manufacturer": "Icelandic Met Office",
            "model": "Device tracker weather",
        }


class VedurIsStationWeatherEntity(
    SingleCoordinatorWeatherEntity[VedurIsWeatherDataUpdateCoordinator],
    WeatherEntity,
):
    """Weather entity for one configured Vedur.is station."""

    _attr_has_entity_name = False
    _attr_native_precipitation_unit = "mm"
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND

    def __init__(
        self,
        coordinator: VedurIsWeatherDataUpdateCoordinator,
        station: Station,
    ) -> None:
        """Initialize the station weather entity."""
        super().__init__(coordinator)
        self._station = station
        self._attr_unique_id = f"{DOMAIN}_{station.station_id}_weather"
        self._attr_name = station.name
        self._attr_device_info = self._device_info(station)

    @property
    def supported_features(self) -> WeatherEntityFeature:
        """Return supported forecast feature flags."""
        return _forecast_features(self.coordinator)

    @property
    def attribution(self) -> str:
        """Return data attribution text."""
        return _weather_attribution(self.coordinator)

    @property
    def available(self) -> bool:
        """Return if weather data is available for the station."""
        return (
            super().available
            and (self._observation is not None or self._forecast is not None)
        )

    @property
    def condition(self) -> str | None:
        """Return the current weather condition."""
        forecast_point = self._current_forecast_point
        if forecast_point is not None:
            return condition_from_forecast_text(
                forecast_point.weather_text,
                forecast_point.time,
            )

        observation = self._observation
        if observation is not None:
            return condition_from_observation(observation)
        return None

    @property
    def native_temperature(self) -> float | None:
        """Return the temperature in Celsius."""
        return self._observation_value("t")

    @property
    def humidity(self) -> float | None:
        """Return the relative humidity percentage."""
        return self._observation_value("rh")

    @property
    def native_dew_point(self) -> float | None:
        """Return the dew point in Celsius."""
        return self._observation_value("td")

    @property
    def native_wind_speed(self) -> float | None:
        """Return the wind speed in meters per second."""
        return self._observation_value("f")

    @property
    def native_wind_gust_speed(self) -> float | None:
        """Return the wind gust speed in meters per second."""
        return self._observation_value("fg")

    @property
    def wind_bearing(self) -> float | str | None:
        """Return the wind bearing."""
        observation = self._observation
        if observation is None:
            return None
        return observation.value("d")

    @property
    def native_pressure(self) -> float | None:
        """Return the pressure in hPa."""
        return self._observation_value("p")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return station metadata for the station weather entity."""
        observation = self._observation
        attrs: dict[str, Any] = {
            ATTR_STATION_ID: self._station.station_id,
            ATTR_OBSERVATION_STATION_ID: self._station.station_id,
            ATTR_OBSERVATION_STATION_NAME: self._station.name,
        }

        forecast_result = self._nearest_forecast_station
        station_forecast = self._forecast
        if forecast_result is not None and station_forecast is not None:
            forecast_station, forecast_distance = forecast_result
            attrs.update(
                {
                    ATTR_FORECAST_STATION_ID: forecast_station.station_id,
                    ATTR_FORECAST_STATION_NAME: station_forecast.name,
                    ATTR_FORECAST_STATION_DISTANCE_KM: round(forecast_distance, 2),
                }
            )

        if observation and observation.time:
            attrs[ATTR_OBSERVATION_TIME] = observation.time.isoformat()

        precipitation = self._observation_value("r")
        if precipitation is not None:
            attrs["precipitation"] = precipitation

        attrs[ATTR_STATION_HAS_DIRECT_FORECAST] = self._has_direct_forecast
        attrs[ATTR_FORECAST_USES_NEARBY_STATION] = (
            forecast_result is not None
            and forecast_result[0].station_id != self._station.station_id
        )
        attrs.update(_observation_diagnostics(observation))
        attrs.update(_data_diagnostics(self.coordinator.data))
        return attrs

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        """Return hourly forecasts for the station."""
        station_forecast = self._forecast
        if station_forecast is None:
            return []

        return _ha_forecasts(hourly_forecast_dicts(station_forecast.forecasts))

    async def async_forecast_daily(self) -> list[Forecast] | None:
        """Return daily forecasts for the station."""
        station_forecast = self._forecast
        if station_forecast is None:
            return []

        return _ha_forecasts(daily_forecast_dicts(station_forecast.forecasts))

    async def async_forecast_twice_daily(self) -> list[Forecast] | None:
        """Return twice-daily forecasts for the station."""
        station_forecast = self._forecast
        if station_forecast is None:
            return []

        return _ha_forecasts(twice_daily_forecast_dicts(station_forecast.forecasts))

    @property
    def _observation(self) -> Observation | None:
        """Return the observation for this station."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.observations.get(self._station.station_id)

    @property
    def _forecast(self) -> StationForecast | None:
        """Return the nearest XML forecast for this station."""
        station_result = self._nearest_forecast_station
        if station_result is None or self.coordinator.data is None:
            return None

        return self.coordinator.data.forecasts.get(station_result[0].station_id)

    @property
    def _nearest_forecast_station(self) -> tuple[Station, float] | None:
        """Return the nearest station with XML forecast data."""
        if self.coordinator.data is None:
            return None

        station_coordinate = _station_coordinate(self._station)
        if station_coordinate is None:
            return None

        stations = (
            self.coordinator.data.stations[station_id]
            for station_id in self.coordinator.data.forecasts
            if station_id in self.coordinator.data.stations
        )
        return nearest_station(station_coordinate, stations)

    @property
    def _has_direct_forecast(self) -> bool:
        """Return whether the selected station has direct XML forecast data."""
        return (
            self.coordinator.data is not None
            and self._station.station_id in self.coordinator.data.forecasts
        )

    @property
    def _current_forecast_point(self) -> ForecastPoint | None:
        """Return the current or next forecast point for this station."""
        station_forecast = self._forecast
        if station_forecast is None:
            return None

        now = datetime.now()
        for forecast in station_forecast.forecasts:
            if forecast.time >= now:
                return forecast
        return station_forecast.forecasts[-1]

    def _observation_value(self, key: str) -> float | None:
        """Return a numeric observation value."""
        observation = self._observation
        if observation is None:
            return None

        value = observation.value(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _device_info(self, station: Station) -> DeviceInfo:
        """Return device registry metadata."""
        info: DeviceInfo = {
            "identifiers": {(DOMAIN, str(station.station_id))},
            "name": station.name,
            "manufacturer": station.owner or "Icelandic Met Office",
            "model": "Automatic weather station",
        }

        if station.abbr:
            info["configuration_url"] = (
                f"https://en.vedur.is/weather/stations/?s={station.abbr}"
            )

        return info


def _person_name(state: State | None) -> str:
    if state is None:
        return "Person"
    return state.name or state.entity_id.removeprefix("person.").replace("_", " ")


def _entity_name(state: State | None, entity_id: str) -> str:
    if state is not None and state.name:
        return state.name
    return entity_id.split(".", 1)[-1].replace("_", " ")


def _should_create_person_weather(hass: HomeAssistant, entry: Any) -> bool:
    """Return if this entry owns global person-following weather entities."""
    enabled_entries = [
        config_entry
        for config_entry in hass.config_entries.async_entries(DOMAIN)
        if (config_entry.options or config_entry.data).get(
            CONF_ENABLE_PERSON_WEATHER,
            True,
        )
    ]
    return bool(enabled_entries and enabled_entries[0].entry_id == entry.entry_id)


def _should_create_home_weather(hass: HomeAssistant, entry: Any) -> bool:
    """Return if this entry owns the global home weather entity."""
    entry_config = entry.options or entry.data
    if not entry_config.get(CONF_ENABLE_HOME_WEATHER, True):
        return False

    entries = hass.config_entries.async_entries(DOMAIN)
    return bool(entries and entries[0].entry_id == entry.entry_id)


def _observation_diagnostics(observation: Observation | None) -> dict[str, Any]:
    """Return compact observation source diagnostics."""
    if observation is None:
        sources = {key: OBSERVATION_SOURCE_UNAVAILABLE for key in SENSOR_KEYS}
    else:
        sources = {key: observation.value_source(key) for key in SENSOR_KEYS}

    return {
        ATTR_OBSERVATION_VALUE_SOURCES: sources,
        ATTR_OBSERVATION_UNAVAILABLE_FIELDS: [
            key
            for key, source in sources.items()
            if source == OBSERVATION_SOURCE_UNAVAILABLE
        ],
    }


def _data_diagnostics(data: Any) -> dict[str, Any]:
    """Return coordinator source diagnostics."""
    if data is None:
        return {
            ATTR_DATA_STALE: True,
            ATTR_STALE_SOURCES: [],
            ATTR_SOURCE_ERRORS: {},
            ATTR_LAST_SUCCESSFUL_UPDATE: None,
        }

    return {
        ATTR_DATA_STALE: data.data_stale,
        ATTR_STALE_SOURCES: sorted(data.stale_sources),
        ATTR_SOURCE_ERRORS: dict(data.source_errors),
        ATTR_LAST_SUCCESSFUL_UPDATE: data.last_successful_update.isoformat()
        if data.last_successful_update
        else None,
    }


def _derived_forecasts_enabled(
    coordinator: VedurIsWeatherDataUpdateCoordinator,
) -> bool:
    """Return whether derived daily and twice-daily forecasts are enabled."""
    entry_config = coordinator.config_entry.options or coordinator.config_entry.data
    return bool(entry_config.get(CONF_ENABLE_DERIVED_FORECASTS, False))


def _forecast_features(
    coordinator: VedurIsWeatherDataUpdateCoordinator,
) -> WeatherEntityFeature:
    """Return forecast features for this config entry."""
    if _derived_forecasts_enabled(coordinator):
        return DERIVED_FORECAST_FEATURES
    return HOURLY_FORECAST_FEATURES


def _weather_attribution(coordinator: VedurIsWeatherDataUpdateCoordinator) -> str:
    """Return attribution for weather entities."""
    if _derived_forecasts_enabled(coordinator):
        return DERIVED_FORECAST_ATTRIBUTION
    return ATTRIBUTION


def _ha_forecasts(forecasts: list[dict[str, Any]]) -> list[Forecast]:
    """Return Home Assistant Forecast objects from plain forecast dictionaries."""
    return [Forecast(**forecast) for forecast in forecasts]


def _zones_by_name(hass: HomeAssistant) -> dict[str, Coordinate]:
    zones: dict[str, Coordinate] = {}
    for state in hass.states.async_all("zone"):
        coordinate = _coordinate_from_state(state)
        if coordinate is None:
            continue

        object_id = state.entity_id.removeprefix("zone.")
        for key in (state.state, state.name, object_id, state.entity_id):
            if key:
                zones[key.casefold()] = coordinate

    return zones


def _coordinate_from_state(state: State) -> Coordinate | None:
    latitude = state.attributes.get("latitude")
    longitude = state.attributes.get("longitude")
    if latitude is None or longitude is None:
        return None

    try:
        return Coordinate(float(latitude), float(longitude))
    except (TypeError, ValueError):
        return None


def _station_coordinate(station: Station) -> Coordinate | None:
    if station.latitude is None or station.longitude is None:
        return None

    return Coordinate(station.latitude, station.longitude)
