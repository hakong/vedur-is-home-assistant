"""Sensor platform for the Icelandic Met Office Weather integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    EntityCategory,
    PERCENTAGE,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .alerts_coordinator import VedurIsAlertsDataUpdateCoordinator
from .api import (
    OBSERVATION_SOURCE_UNAVAILABLE,
    ForecastPoint,
    Observation,
    Station,
    VedurIsApiClient,
    WeatherAlert,
)
from .const import (
    ATTR_ALERT_AREAS,
    ATTR_ALERT_COUNT,
    ATTR_ALERT_HIGHEST_COLOR,
    ATTR_ALERT_HIGHEST_SEVERITY,
    ATTR_ALERTS,
    ATTR_DATA_STALE,
    ATTR_FORECAST_STATION_DISTANCE_KM,
    ATTR_FORECAST_STATION_ID,
    ATTR_FORECAST_STATION_NAME,
    ATTR_LAST_SUCCESSFUL_UPDATE,
    ATTR_OBSERVATION_SOURCE,
    ATTR_OBSERVATION_STATION_DISTANCE_KM,
    ATTR_OBSERVATION_STATION_ID,
    ATTR_OBSERVATION_STATION_NAME,
    ATTR_OBSERVATION_TIME,
    ATTR_SOURCE_ERRORS,
    ATTR_STALE_SOURCES,
    ATTR_STATION_ID,
    ATTRIBUTION,
    CONF_DEVICE_TRACKER_ENTITY_IDS,
    CONF_ENABLE_DEVICE_TRACKER_WEATHER,
    CONF_ENABLE_HOME_WEATHER,
    CONF_ENABLE_PERSON_WEATHER,
    CONF_ENABLE_STATION_WEATHER,
    CONF_STATION_IDS,
    CONF_STATIONS,
    DOMAIN,
)
from .geo import (
    Coordinate,
    nearest_station,
    resolve_person_coordinate,
    resolve_tracker_coordinate,
)
from .weather_coordinator import VedurIsWeatherDataUpdateCoordinator
from .weather_utils import condition_from_forecast_text, condition_from_observation

_LOGGER = logging.getLogger(__name__)
MAX_OBSERVATION_DISTANCE_KM = 150.0


@dataclass(frozen=True, kw_only=True)
class VedurIsSensorEntityDescription(SensorEntityDescription):
    """Describes a Vedur.is sensor."""

    available_fn: Callable[[Observation], bool] = lambda observation: True


SENSOR_DESCRIPTIONS: tuple[VedurIsSensorEntityDescription, ...] = (
    VedurIsSensorEntityDescription(
        key="t",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VedurIsSensorEntityDescription(
        key="rh",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VedurIsSensorEntityDescription(
        key="td",
        translation_key="dew_point",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VedurIsSensorEntityDescription(
        key="f",
        translation_key="wind_speed",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VedurIsSensorEntityDescription(
        key="fg",
        translation_key="wind_gust",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        suggested_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VedurIsSensorEntityDescription(
        key="d",
        translation_key="wind_direction",
        native_unit_of_measurement=DEGREE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VedurIsSensorEntityDescription(
        key="p",
        translation_key="pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VedurIsSensorEntityDescription(
        key="r",
        translation_key="precipitation",
        device_class=SensorDeviceClass.PRECIPITATION,
        native_unit_of_measurement="mm",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)

CONDITION_SENSOR_DESCRIPTION = VedurIsSensorEntityDescription(
    key="condition",
    translation_key="condition",
    icon="mdi:weather-partly-cloudy",
)

LOCATION_SENSOR_DESCRIPTIONS = (
    CONDITION_SENSOR_DESCRIPTION,
    *SENSOR_DESCRIPTIONS,
)


@dataclass(frozen=True, slots=True)
class WeatherSensorSource:
    """Describe one weather device/location for diagnostic sensors."""

    key: str
    name: str
    device_identifier: str
    model: str
    source_type: str
    source_entity_id: str | None = None
    station: Station | None = None


async def async_setup_entry(
    hass: Any,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vedur.is sensors from a config entry."""
    from homeassistant.helpers import aiohttp_client

    coordinator: VedurIsWeatherDataUpdateCoordinator = entry.runtime_data
    alerts_coordinator = VedurIsAlertsDataUpdateCoordinator(
        hass,
        VedurIsApiClient(aiohttp_client.async_get_clientsession(hass)),
        entry,
    )
    await alerts_coordinator.async_refresh()

    entities: list[SensorEntity] = [VedurIsWeatherAlertsSensor(alerts_coordinator)]
    entry_config = entry.options or entry.data
    if _should_create_home_weather(hass, entry):
        entities.extend(
            _location_sensors(
                coordinator,
                WeatherSensorSource(
                    key="home_weather",
                    name="Home",
                    device_identifier="home_weather",
                    model="Home weather",
                    source_type="home",
                    source_entity_id="zone.home",
                ),
            )
        )
    if _should_create_person_weather(hass, entry):
        for person_entity_id in hass.states.async_entity_ids("person"):
            entities.extend(
                _location_sensors(
                    coordinator,
                    WeatherSensorSource(
                        key=person_entity_id,
                        name=_entity_name(
                            hass.states.get(person_entity_id),
                            person_entity_id,
                        ),
                        device_identifier=f"person_weather:{person_entity_id}",
                        model="Person-following weather",
                        source_type="person",
                        source_entity_id=person_entity_id,
                    ),
                )
            )
    if entry_config.get(CONF_ENABLE_DEVICE_TRACKER_WEATHER, True):
        for tracker_entity_id in entry_config.get(
            CONF_DEVICE_TRACKER_ENTITY_IDS,
            [],
        ):
            entities.extend(
                _location_sensors(
                    coordinator,
                    WeatherSensorSource(
                        key=tracker_entity_id,
                        name=_entity_name(
                            hass.states.get(tracker_entity_id),
                            tracker_entity_id,
                        ),
                        device_identifier=(
                            f"device_tracker_weather:{tracker_entity_id}"
                        ),
                        model="Device tracker weather",
                        source_type="tracker",
                        source_entity_id=tracker_entity_id,
                    ),
                )
            )

    station_ids: list[int] = []
    if entry_config.get(CONF_ENABLE_STATION_WEATHER, True):
        station_ids = [
            int(station_id) for station_id in entry_config.get(CONF_STATION_IDS, [])
        ]
    stored_stations = {
        station.station_id: station
        for station in (
            Station.from_api(station_data)
            for station_data in entry_config.get(CONF_STATIONS, [])
        )
    }
    for station_id in station_ids:
        station = (
            coordinator.data.stations.get(station_id)
            if coordinator.data is not None
            else None
        ) or stored_stations.get(station_id) or Station(
            station_id=station_id,
            name=str(station_id),
            abbr=None,
            station_type="sj",
            latitude=None,
            longitude=None,
            elevation=None,
            owner=None,
            start_year=None,
        )
        entities.extend(
            VedurIsSensor(coordinator, station, description)
            for description in SENSOR_DESCRIPTIONS
        )
        entities.append(
            VedurIsLocationSensor(
                coordinator,
                WeatherSensorSource(
                    key=str(station.station_id),
                    name=station.name,
                    device_identifier=str(station.station_id),
                    model="Automatic weather station",
                    source_type="station",
                    station=station,
                ),
                CONDITION_SENSOR_DESCRIPTION,
            )
        )

    _LOGGER.debug(
        "Adding Vedur.is sensors for stations %s: %s entities",
        station_ids,
        len(entities),
    )
    async_add_entities(entities)


def _location_sensors(
    coordinator: VedurIsWeatherDataUpdateCoordinator,
    source: WeatherSensorSource,
) -> list[VedurIsLocationSensor]:
    """Return disabled diagnostic sensors for one weather location."""
    return [
        VedurIsLocationSensor(coordinator, source, description)
        for description in LOCATION_SENSOR_DESCRIPTIONS
    ]


class VedurIsWeatherAlertsSensor(
    CoordinatorEntity[VedurIsAlertsDataUpdateCoordinator],
    SensorEntity,
):
    """Representation of active Icelandic Met Office weather alerts."""

    _attr_has_entity_name = False
    _attr_name = "Weather alerts"
    _attr_translation_key = "weather_alerts"
    _attr_unique_id = f"{DOMAIN}_weather_alerts"
    _attr_attribution = ATTRIBUTION

    @property
    def native_value(self) -> int | None:
        """Return the active alert count."""
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data)

    @property
    def icon(self) -> str:
        """Return a warning icon when active alerts exist."""
        if self.native_value:
            return "mdi:alert"
        return "mdi:shield-check"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return active weather alert details."""
        alerts = self.coordinator.data or ()
        return {
            ATTR_ALERT_COUNT: len(alerts),
            ATTR_ALERT_HIGHEST_COLOR: _highest_alert_color(alerts),
            ATTR_ALERT_HIGHEST_SEVERITY: _highest_alert_severity(alerts),
            ATTR_ALERT_AREAS: sorted(
                {
                    alert.area
                    for alert in alerts
                    if alert.area is not None
                }
            ),
            ATTR_ALERTS: [alert.as_attribute_dict() for alert in alerts],
            ATTR_DATA_STALE: self.coordinator.data_stale,
            ATTR_STALE_SOURCES: ["alerts"] if self.coordinator.data_stale else [],
            ATTR_SOURCE_ERRORS: dict(self.coordinator.source_errors),
        }


class VedurIsSensor(
    CoordinatorEntity[VedurIsWeatherDataUpdateCoordinator],
    SensorEntity,
):
    """Representation of a Vedur.is observation sensor."""

    entity_description: VedurIsSensorEntityDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: VedurIsWeatherDataUpdateCoordinator,
        station: Station,
        description: VedurIsSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._station = station
        self._attr_unique_id = f"{DOMAIN}_{station.station_id}_{description.key}"
        self._attr_device_info = self._device_info(station)

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        observation = self._observation
        return (
            super().available
            and observation is not None
            and self.entity_description.available_fn(observation)
            and observation.value_source(self.entity_description.key)
            != OBSERVATION_SOURCE_UNAVAILABLE
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor state."""
        if self._observation is None:
            return None
        return self._observation.value(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        observation = self._observation
        attrs = {
            ATTR_STATION_ID: self._station.station_id,
            ATTR_OBSERVATION_TIME: observation.time.isoformat()
            if observation and observation.time
            else None,
        }
        if observation is not None:
            attrs[ATTR_OBSERVATION_SOURCE] = observation.value_source(
                self.entity_description.key
            )
        attrs.update(_data_diagnostics(self.coordinator.data))
        return attrs

    @property
    def _observation(self) -> Observation | None:
        """Return the latest observation for this sensor's station."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.observations.get(self._station.station_id)

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


class VedurIsLocationSensor(
    CoordinatorEntity[VedurIsWeatherDataUpdateCoordinator],
    SensorEntity,
):
    """A diagnostic sensor for a weather device/location."""

    entity_description: VedurIsSensorEntityDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: VedurIsWeatherDataUpdateCoordinator,
        source: WeatherSensorSource,
        description: VedurIsSensorEntityDescription,
    ) -> None:
        """Initialize a weather location sensor."""
        super().__init__(coordinator)
        self._source = source
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{source.key}_{description.key}"
        self._attr_device_info = self._device_info(source)

    async def async_added_to_hass(self) -> None:
        """Subscribe to source location changes."""
        await super().async_added_to_hass()
        if self._source.source_entity_id is None:
            return
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source.source_entity_id],
                self._async_source_state_changed,
            )
        )

    @callback
    def _async_source_state_changed(self, event: Event[State | None]) -> None:
        """Write a new state when the source location changes."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether this current weather value is available."""
        if not super().available:
            return False

        if self.entity_description.key == "condition":
            return self._condition is not None

        observation = self._observation
        observation_station = self._nearest_observation_station
        if observation is None or observation_station is None:
            return False
        if (
            self._source.source_type != "station"
            and observation_station[1] > MAX_OBSERVATION_DISTANCE_KM
        ):
            return False
        return (
            observation.value_source(self.entity_description.key)
            != OBSERVATION_SOURCE_UNAVAILABLE
        )

    @property
    def native_value(self) -> Any:
        """Return the current weather value."""
        if self.entity_description.key == "condition":
            return self._condition
        observation = self._observation
        if observation is None:
            return None
        return observation.value(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return weather source and station metadata."""
        observation = self._observation
        attrs: dict[str, Any] = {}
        if self._source.source_entity_id is not None:
            attrs["source_entity_id"] = self._source.source_entity_id

        observation_station = self._nearest_observation_station
        if observation_station is not None:
            station, distance = observation_station
            attrs.update(
                {
                    ATTR_OBSERVATION_STATION_ID: station.station_id,
                    ATTR_OBSERVATION_STATION_NAME: station.name,
                    ATTR_OBSERVATION_STATION_DISTANCE_KM: round(distance, 2),
                }
            )

        forecast_station = self._nearest_forecast_station
        if forecast_station is not None:
            station, distance = forecast_station
            attrs.update(
                {
                    ATTR_FORECAST_STATION_ID: station.station_id,
                    ATTR_FORECAST_STATION_NAME: station.name,
                    ATTR_FORECAST_STATION_DISTANCE_KM: round(distance, 2),
                }
            )

        if observation is not None:
            attrs[ATTR_OBSERVATION_TIME] = (
                observation.time.isoformat() if observation.time else None
            )
            if self.entity_description.key != "condition":
                attrs[ATTR_OBSERVATION_SOURCE] = observation.value_source(
                    self.entity_description.key
                )
        attrs.update(_data_diagnostics(self.coordinator.data))
        return attrs

    @property
    def _condition(self) -> str | None:
        """Return the current mapped weather condition."""
        forecast_point = self._current_forecast_point
        if forecast_point is not None:
            return condition_from_forecast_text(
                forecast_point.weather_text,
                forecast_point.time,
            )
        if self._observation is not None:
            return condition_from_observation(self._observation)
        return None

    @property
    def _coordinate(self) -> Coordinate | None:
        """Return the current source coordinate."""
        if self._source.source_type == "station":
            station = self._source.station
            if (
                station is None
                or station.latitude is None
                or station.longitude is None
            ):
                return None
            return Coordinate(station.latitude, station.longitude)

        if self._source.source_entity_id is None:
            return None
        state = self.hass.states.get(self._source.source_entity_id)
        if state is None:
            return None
        if self._source.source_type == "person":
            return resolve_person_coordinate(
                state.state,
                state.attributes,
                _zones_by_name(self.hass),
            )
        return resolve_tracker_coordinate(state.attributes)

    @property
    def _nearest_observation_station(self) -> tuple[Station, float] | None:
        """Return the station supplying current observations."""
        data = self.coordinator.data
        if data is None:
            return None
        if self._source.source_type == "station":
            station = self._source.station
            if station is None or station.station_id not in data.observations:
                return None
            return station, 0.0
        coordinate = self._coordinate
        if coordinate is None:
            return None
        stations = (
            data.stations[station_id]
            for station_id in data.observations
            if station_id in data.stations
        )
        return nearest_station(coordinate, stations)

    @property
    def _nearest_forecast_station(self) -> tuple[Station, float] | None:
        """Return the station supplying forecast conditions."""
        data = self.coordinator.data
        coordinate = self._coordinate
        if data is None or coordinate is None:
            return None
        stations = (
            data.stations[station_id]
            for station_id in data.forecasts
            if station_id in data.stations
        )
        return nearest_station(coordinate, stations)

    @property
    def _observation(self) -> Observation | None:
        """Return the applicable current observation."""
        data = self.coordinator.data
        station_result = self._nearest_observation_station
        if data is None or station_result is None:
            return None
        return data.observations.get(station_result[0].station_id)

    @property
    def _current_forecast_point(self) -> ForecastPoint | None:
        """Return the current or next forecast point."""
        data = self.coordinator.data
        station_result = self._nearest_forecast_station
        if data is None or station_result is None:
            return None
        station_forecast = data.forecasts.get(station_result[0].station_id)
        if station_forecast is None or not station_forecast.forecasts:
            return None
        now = datetime.now()
        for forecast in station_forecast.forecasts:
            if forecast.time >= now:
                return forecast
        return station_forecast.forecasts[-1]

    def _device_info(self, source: WeatherSensorSource) -> DeviceInfo:
        """Return device registry metadata matching the weather entity."""
        return {
            "identifiers": {(DOMAIN, source.device_identifier)},
            "name": source.name,
            "manufacturer": (
                source.station.owner
                if source.station is not None and source.station.owner
                else "Icelandic Met Office"
            ),
            "model": source.model,
        }


_ALERT_COLOR_RANK = {
    "yellow": 1,
    "orange": 2,
    "red": 3,
}

_ALERT_SEVERITY_RANK = {
    "minor": 1,
    "moderate": 2,
    "severe": 3,
    "extreme": 4,
}


def _highest_alert_color(alerts: tuple[WeatherAlert, ...]) -> str | None:
    return _highest_ranked_value(
        (alert.color for alert in alerts),
        _ALERT_COLOR_RANK,
    )


def _highest_alert_severity(alerts: tuple[WeatherAlert, ...]) -> str | None:
    return _highest_ranked_value(
        (alert.severity for alert in alerts),
        _ALERT_SEVERITY_RANK,
    )


def _highest_ranked_value(
    values: Any,
    ranks: dict[str, int],
) -> str | None:
    ranked_values = [
        value
        for value in values
        if isinstance(value, str) and value.casefold() in ranks
    ]
    if not ranked_values:
        return None
    return max(ranked_values, key=lambda value: ranks[value.casefold()])


def _entity_name(state: State | None, entity_id: str) -> str:
    """Return a friendly source entity name."""
    if state is not None and state.name:
        return state.name
    return entity_id.split(".", 1)[-1].replace("_", " ")


def _should_create_person_weather(hass: HomeAssistant, entry: Any) -> bool:
    """Return if this entry owns global person-following weather devices."""
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
    """Return if this entry owns the global Home weather device."""
    entry_config = entry.options or entry.data
    if not entry_config.get(CONF_ENABLE_HOME_WEATHER, True):
        return False
    entries = hass.config_entries.async_entries(DOMAIN)
    return bool(entries and entries[0].entry_id == entry.entry_id)


def _zones_by_name(hass: HomeAssistant) -> dict[str, Coordinate]:
    """Return zone coordinates indexed by common zone names."""
    zones: dict[str, Coordinate] = {}
    for state in hass.states.async_all("zone"):
        coordinate = resolve_tracker_coordinate(state.attributes)
        if coordinate is None:
            continue
        object_id = state.entity_id.removeprefix("zone.")
        for key in (state.state, state.name, object_id, state.entity_id):
            if key:
                zones[key.casefold()] = coordinate
    return zones


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
