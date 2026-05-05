"""Sensor platform for the Vedur.is integration."""

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

from .api import Observation, Station
from .const import ATTR_OBSERVATION_TIME, ATTR_STATION_ID, DOMAIN
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
    coordinator: VedurIsDataUpdateCoordinator = entry.runtime_data

    entities: list[VedurIsSensor] = []
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


class VedurIsSensor(CoordinatorEntity[VedurIsDataUpdateCoordinator], SensorEntity):
    """Representation of a Vedur.is observation sensor."""

    entity_description: VedurIsSensorEntityDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True

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
            and observation.value(self.entity_description.key) is not None
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
        return {
            ATTR_STATION_ID: self._station.station_id,
            ATTR_OBSERVATION_TIME: observation.time.isoformat()
            if observation and observation.time
            else None,
        }

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
