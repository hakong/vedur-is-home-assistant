"""Verify the research scaffold cannot silently replace a production entry."""

from types import SimpleNamespace
import unittest

try:
    from homeassistant.exceptions import ConfigEntryError
    from custom_components.vedur_is import async_setup_entry, async_unload_entry
except ModuleNotFoundError:
    HA_AVAILABLE = False
else:
    HA_AVAILABLE = True


@unittest.skipUnless(
    HA_AVAILABLE, "Run the HA scaffold checks in a Home Assistant container"
)
class ScaffoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_entry_has_no_platforms_or_network(self):
        entry = SimpleNamespace(data={"research_scaffold": True})
        self.assertTrue(await async_setup_entry(None, entry))
        self.assertTrue(await async_unload_entry(None, entry))

    async def test_existing_production_entry_is_rejected(self):
        with self.assertRaises(ConfigEntryError):
            await async_setup_entry(None, SimpleNamespace(data={"station_ids": [1474]}))
