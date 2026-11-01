"""Minimal setup flow for an isolated research HA instance."""

from homeassistant import config_entries

from .const import DOMAIN


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Reserve a research entry without creating weather entities."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title="Vedur research", data={"research_scaffold": True}
            )
        return self.async_show_form(step_id="user")
