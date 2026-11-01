"""Scaffolding only: no data collection or entities are enabled yet."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Accept only explicit research entries; never reinterpret production data."""
    if entry.data != {"research_scaffold": True}:
        raise ConfigEntryError("This research branch cannot load production entries")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """The scaffold has no background tasks or platforms to unload."""
    return True
