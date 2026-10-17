"""Registry cleanup without deprecated mapping or device ownership APIs."""

from importlib.util import find_spec
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

HAS_HOME_ASSISTANT = find_spec("homeassistant") is not None
if HAS_HOME_ASSISTANT:
    from homeassistant.const import DEGREE, EntityCategory
    from homeassistant.components.sensor import SensorStateClass
    from homeassistant.helpers import entity_registry as er
    from custom_components.vedur_is import (
        _async_enable_diagnostic_registry_entries,
        _async_remove_home_weather_registry_entries,
        _async_remove_selected_station_registry_entries,
        _async_remove_stale_registry_entries,
    )
    from custom_components.vedur_is.config_flow import (
        VedurIsOptionsFlow,
        _async_remove_device_tracker_registry_entries,
        _async_remove_station_registry_entries,
    )
    from custom_components.vedur_is.const import (
        CONF_DEVICE_TRACKER_ENTITY_IDS,
        CONF_ENABLE_PERSON_WEATHER,
        CONF_ENABLE_DEVICE_TRACKER_WEATHER,
        CONF_STATION_IDS,
        DOMAIN,
    )
    from custom_components.vedur_is.registry import async_get_entry_device
    from custom_components.vedur_is.sensor import SENSOR_DESCRIPTIONS


class TestRepositoryMetadata(unittest.TestCase):
    def test_manifest_uses_canonical_repository_and_owner(self):
        manifest = json.loads(
            (
                Path(__file__).parents[1] / "custom_components/vedur_is/manifest.json"
            ).read_text()
        )
        self.assertEqual(manifest["codeowners"], ["@hakong"])
        self.assertEqual(
            manifest["documentation"],
            "https://github.com/hakong/vedur-is-home-assistant",
        )
        self.assertEqual(
            manifest["issue_tracker"],
            "https://github.com/hakong/vedur-is-home-assistant/issues",
        )


class Device:
    def __init__(self, device_id, identifier):
        self.id = device_id
        self.name = identifier
        self.identifiers = {("vedur_is", identifier)}

    @property
    def config_entries(self):
        raise AssertionError("Deprecated device.config_entries accessed")


class Registry:
    def __init__(self):
        self.by_entry = {"own": [], "other": []}
        self.removed = []
        self.updates = []

    @property
    def devices(self):
        raise AssertionError("Raw registry.devices accessed")

    @property
    def entities(self):
        raise AssertionError("Raw registry.entities accessed")

    def entries(self, entry_id):
        return list(self.by_entry.get(entry_id, []))

    def async_get_device(self, **kwargs):
        raise AssertionError("Deprecated async_get_device called")

    def async_get_entity_id(self, *args):
        return None

    def async_remove_device(self, device_id):
        self.removed.append(device_id)
        for devices in self.by_entry.values():
            devices[:] = [device for device in devices if device.id != device_id]

    def async_remove(self, entity_id):
        self.removed.append(entity_id)

    def async_update_entity(self, entity_id, **changes):
        self.updates.append((entity_id, changes))


@unittest.skipUnless(HAS_HOME_ASSISTANT, "Requires Home Assistant")
class TestRegistryCleanup(unittest.TestCase):
    def setUp(self):
        self.devices = Registry()
        self.entities = Registry()
        self.hass = SimpleNamespace()
        self.entry = SimpleNamespace(entry_id="own", options={}, data={})
        for target, replacement in (
            (
                "homeassistant.helpers.device_registry.async_get",
                lambda hass: self.devices,
            ),
            (
                "homeassistant.helpers.entity_registry.async_get",
                lambda hass: self.entities,
            ),
            (
                "homeassistant.helpers.device_registry.async_entries_for_config_entry",
                lambda registry, entry_id: registry.entries(entry_id),
            ),
            (
                "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
                lambda registry, entry_id: registry.entries(entry_id),
            ),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_stale_stations_and_trackers_removed_without_touching_other_entries(self):
        self.devices.by_entry["own"] = [
            Device("station-keep", "1474"),
            Device("station-remove", "31475"),
            Device("station-remove-2", "1"),
            Device("home", "home_weather"),
            Device("person", "person_weather:person.test"),
            Device("tracker-keep", "device_tracker_weather:device_tracker.route"),
            Device("tracker-remove", "device_tracker_weather:device_tracker.old"),
        ]
        self.devices.by_entry["other"] = [Device("other-station", "31475")]
        self.entry.data = {
            CONF_STATION_IDS: [1474],
            CONF_DEVICE_TRACKER_ENTITY_IDS: ["device_tracker.route"],
        }
        _async_remove_stale_registry_entries(self.hass, self.entry)
        self.assertCountEqual(
            self.devices.removed,
            ["station-remove", "station-remove-2", "tracker-remove"],
        )
        self.assertEqual(self.devices.by_entry["other"][0].id, "other-station")

    def test_disabled_person_and_tracker_weather_only_remove_owned_devices(self):
        self.devices.by_entry["own"] = [
            Device("person", "person_weather:person.test"),
            Device("tracker", "device_tracker_weather:device_tracker.route"),
            Device("home", "home_weather"),
        ]
        self.devices.by_entry["other"] = [
            Device("other-person", "person_weather:person.test")
        ]
        self.entry.options = {
            CONF_ENABLE_PERSON_WEATHER: False,
            CONF_ENABLE_DEVICE_TRACKER_WEATHER: False,
        }
        _async_remove_stale_registry_entries(self.hass, self.entry)
        self.assertCountEqual(self.devices.removed, ["person", "tracker"])
        self.assertEqual(self.devices.by_entry["other"][0].id, "other-person")

    def test_home_and_selected_station_cleanup_use_scoped_lookup(self):
        self.devices.by_entry["own"] = [
            Device("home", "home_weather"),
            Device("station", "1474"),
        ]
        self.devices.by_entry["other"] = [
            Device("other-home", "home_weather"),
            Device("other-station", "1474"),
        ]
        _async_remove_home_weather_registry_entries(
            self.entities, self.devices, self.entry
        )
        _async_remove_selected_station_registry_entries(
            self.entities, self.devices, self.entry, {"1474"}
        )
        self.assertCountEqual(self.devices.removed, ["home", "station"])
        self.assertEqual(len(self.devices.by_entry["other"]), 2)

    def test_options_station_removal_is_scoped_to_config_entry(self):
        self.devices.by_entry["own"] = [Device("station", "1474")]
        self.devices.by_entry["other"] = [Device("other-station", "1474")]
        _async_remove_station_registry_entries(self.hass, [1474], config_entry_id="own")
        self.assertEqual(self.devices.removed, ["station"])

    def test_options_tracker_removal_is_scoped_to_config_entry(self):
        self.devices.by_entry["own"] = [
            Device("tracker", "device_tracker_weather:device_tracker.route")
        ]
        self.devices.by_entry["other"] = [
            Device("other-tracker", "device_tracker_weather:device_tracker.route")
        ]
        _async_remove_device_tracker_registry_entries(
            self.hass, ["device_tracker.route"], config_entry_id="own"
        )
        self.assertEqual(self.devices.removed, ["tracker"])

    def test_scoped_lookup_does_not_return_another_entries_device(self):
        self.devices.by_entry["other"] = [Device("other-home", "home_weather")]
        self.assertIsNone(
            async_get_entry_device(self.devices, "own", (DOMAIN, "home_weather"))
        )

    def test_enable_diagnostics_preserves_user_disabled_sensors(self):
        def entity(entity_id, disabled_by):
            return SimpleNamespace(
                entity_id=entity_id,
                config_entry_id="own",
                domain="sensor",
                platform=DOMAIN,
                entity_category=EntityCategory.DIAGNOSTIC,
                disabled_by=disabled_by,
            )

        self.entities.by_entry["own"] = [
            entity("sensor.default_disabled", er.RegistryEntryDisabler.INTEGRATION),
            entity("sensor.user_disabled", er.RegistryEntryDisabler.USER),
        ]
        _async_enable_diagnostic_registry_entries(self.hass, self.entry)
        self.assertEqual(
            self.entities.updates, [("sensor.default_disabled", {"disabled_by": None})]
        )

    def test_options_flow_passes_the_owning_config_entry_id(self):
        flow = VedurIsOptionsFlow(self.entry)
        flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda *args, **kwargs: None
            )
        )
        flow._stations = {}
        with (
            patch(
                "custom_components.vedur_is.config_flow._async_remove_station_registry_entries"
            ) as stations,
            patch(
                "custom_components.vedur_is.config_flow._async_remove_device_tracker_registry_entries"
            ) as trackers,
        ):
            flow._async_update_entry([])
        self.assertEqual(stations.call_args.kwargs["config_entry_id"], "own")
        self.assertEqual(trackers.call_args.kwargs["config_entry_id"], "own")

    def test_wind_direction_uses_angular_statistics_and_degrees(self):
        direction = next(item for item in SENSOR_DESCRIPTIONS if item.key == "d")
        self.assertEqual(direction.state_class, SensorStateClass.MEASUREMENT_ANGLE)
        self.assertEqual(direction.native_unit_of_measurement, DEGREE)


if __name__ == "__main__":
    unittest.main()
