"""Config-entry-scoped device registry lookups."""

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr


@callback
def async_get_entry_device(
    registry: dr.DeviceRegistry,
    config_entry_id: str,
    identifier: tuple[str, str],
) -> dr.DeviceEntry | None:
    """Find a device without deprecated global or ownership-property lookups."""
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(registry, config_entry_id)
            if identifier in device.identifiers
        ),
        None,
    )
