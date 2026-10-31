# Icelandic Met Office Weather

Home Assistant custom integration for current weather observations and forecasts
from the Icelandic Met Office through vedur.is.

**Unofficial community integration.** Independently developed; not created,
maintained, approved, or endorsed by the Icelandic Meteorological Office
(Veðurstofa Íslands / IMO), and not affiliated with it. The IMO provides the
weather data, not this integration. Integration support is provided through
[this project's issue tracker](https://github.com/hakong/vedur-is-home-assistant/issues),
not the IMO. The bundled artwork identifies this project and is not the IMO logo.

The integration can create weather entities for Home, people, and selected
stations. These are enabled by default and can be toggled in integration
options. Current-weather diagnostic sensors are enabled by default and can be
disabled individually from Home Assistant's entity settings.

Weather entities expose metadata showing which station is used for forecasts and
which current observation fields came from the official API, gottvedur.is
fallback data, or remain unavailable.
