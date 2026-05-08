"""Sensor platform for the Icelandic Met Office Weather integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .alerts_coordinator import VedurIsAlertsDataUpdateCoordinator
from .api import (
    OBSERVATION_SOURCE_UNAVAILABLE,
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
    ATTR_OBSERVATION_SOURCE,
    ATTR_OBSERVATION_TIME,
    ATTR_STATION_ID,
    ATTRIBUTION,
    DOMAIN,
)
from .coordinator import VedurIsDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


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
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VedurIsSensorEntityDescription(
        key="fg",
        translation_key="wind_gust",
        device_class=SensorDeviceClass.WIND_SPEED,
        native_unit_of_measurement=UnitOfSpeed.METERS_PER_SECOND,
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


async def async_setup_entry(
    hass: Any,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vedur.is sensors from a config entry."""
    from homeassistant.helpers import aiohttp_client

    coordinator: VedurIsDataUpdateCoordinator = entry.runtime_data
    alerts_coordinator = VedurIsAlertsDataUpdateCoordinator(
        hass,
        VedurIsApiClient(aiohttp_client.async_get_clientsession(hass)),
        entry,
    )
    await alerts_coordinator.async_refresh()

    entities: list[SensorEntity] = [VedurIsWeatherAlertsSensor(alerts_coordinator)]
    for station_id in coordinator.station_ids:
        station = coordinator.stations.get(station_id) or Station(
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

    _LOGGER.debug(
        "Adding Vedur.is sensors for stations %s: %s entities",
        coordinator.station_ids,
        len(entities),
    )
    async_add_entities(entities)


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
        }


class VedurIsSensor(CoordinatorEntity[VedurIsDataUpdateCoordinator], SensorEntity):
    """Representation of a Vedur.is observation sensor."""

    entity_description: VedurIsSensorEntityDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(
        self,
        coordinator: VedurIsDataUpdateCoordinator,
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
        return attrs

    @property
    def _observation(self) -> Observation | None:
        """Return the latest observation for this sensor's station."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._station.station_id)

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
