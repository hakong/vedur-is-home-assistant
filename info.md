# Icelandic Met Office Weather

Home Assistant custom integration for current weather observations and forecasts
from the Icelandic Met Office through vedur.is.

Weather data is provided by the Icelandic Met Office (vedur.is). This project is
not affiliated with or endorsed by the Icelandic Met Office.

The integration can create weather entities for Home, people, and selected
stations. These are enabled by default and can be toggled in integration
options. Selected station diagnostics are disabled by default.

Weather entities expose metadata showing which station is used for forecasts and
which current observation fields came from the official API, gottvedur.is
fallback data, or remain unavailable.
