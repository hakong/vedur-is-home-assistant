# Icelandic Met Office Weather for Home Assistant

A Home Assistant custom integration that exposes current weather observation
and forecast data from the Icelandic Met Office through vedur.is.

This project is not affiliated with or endorsed by the Icelandic Met Office.
Weather data is provided by the Icelandic Met Office (vedur.is).

## Features

- UI config flow with full station browsing, map-based nearby station
  suggestions, or place search that opens the map near the matched location.
- Optional weather for Home, using Home Assistant's home zone. Enabled by
  default.
- Optional station selection for additional station weather entities.
- Station selector labels include station distance in nearby mode and the
  station owner when provided by the API.
- Multiple optional stations in one config entry.
- Optional weather and diagnostic entities for selected stations. Enabled by
  default.
- Optional weather entities that follow Home Assistant `person` entities.
  Enabled by default.
- Optional weather entities that follow selected `device_tracker` entities,
  such as route or destination trackers. Configure the trackers from the
  integration options.
- Hourly forecasts through `weather.get_forecasts`, using Vedur.is XML forecast
  time points.
- Optional daily and twice-daily forecasts. These are disabled by default
  because Vedur.is does not expose native daily or twice-daily forecast products
  through the APIs used by this integration. When enabled, they are derived from
  future XML time points using period high/low temperatures, max wind, and the
  most common mapped condition in the relevant daytime or day/night period.
- Processed 10-minute observations, polled every 10 minutes through a
  `DataUpdateCoordinator`. After the immediate startup refresh, successful
  polls are aligned to 75 seconds after each UTC 10-minute boundary to allow
  the Met Office processing pipeline time to publish new observations.
- Transient API failures retry after approximately 2, 5, 10, 20, and 40
  minutes before progressively backing off to a maximum of six hours.
- Diagnostic sensors for condition, temperature, humidity, dew point, wind
  speed, wind gust, wind direction, pressure, and precipitation. These are
  created for Home, person-following, device-tracker, and selected-station
  weather devices, and are disabled by default because the `weather.*`
  entities are the primary interface.
- Station metadata is used for Home Assistant device registry entries.
- Weather entities expose observation source diagnostics and forecast station
  metadata.
- Weather alerts sensor for active Icelandic Met Office CAP weather warnings.
- `vedur_is.get_forecast_for_location` action for one-off nearest-station
  forecast lookups by latitude/longitude without creating an entity.
- No API key required.

## Installation

### HACS Custom Repository

1. Add this repository to HACS as an integration custom repository.
2. Install `Icelandic Met Office Weather`.
3. Restart Home Assistant.
4. Go to **Settings > Devices & services > Add integration** and search for
   `Icelandic Met Office Weather`.

### Manual

Copy `custom_components/vedur_is` into your Home Assistant config directory at:

```text
config/custom_components/vedur_is
```

Restart Home Assistant and add the integration from the UI.

## Development

Install the lightweight unit-test dependencies:

```bash
.venv/bin/python -m pip install -e '.[test]'
```

Run tests:

```bash
.venv/bin/python -m pytest -q
```

See [TODO.md](TODO.md) for planned features and follow-up work.

## Weather Metadata

Current observations come from the official vedur.is API. When an official API
field is unavailable, the integration can fill that field from gottvedur.is page
data. Weather entities expose:

- `observation_value_sources`: maps observation keys such as `t`, `f`, `fg`, and
  `r` to `official_api`, `gottvedur_is`, or `unavailable`.
- `observation_unavailable_fields`: lists observation keys that are still
  unavailable after fallback data is applied.

Station weather entities also expose forecast metadata:

- `station_has_direct_forecast`: whether the selected observation station has
  its own XML forecast.
- `forecast_uses_nearby_station`: whether forecasts are coming from a nearby
  forecast-capable station instead of the selected observation station.
- `forecast_station_id`, `forecast_station_name`, and
  `forecast_station_distance_km`: the forecast station actually used by
  `weather.get_forecasts`.

Device tracker weather entities expose `device_tracker_entity_id` along with
the same observation and forecast station metadata. They use the tracker's
standard `latitude` and `longitude` attributes only; if those attributes are
missing, the weather entity is unavailable.

Hourly forecasts come directly from Vedur.is XML forecast time points. Daily
and twice-daily forecasts are optional derived summaries made by this
integration; when that option is enabled, weather entities add a note to their
Home Assistant attribution text.

## Station Selection

The integration can help find stations near a place in two ways:

- Search for an Icelandic place name, such as `Þórsmörk`, then review the map
  centered on the matched location before selecting nearby stations.
- Pick a location directly on the map and then choose from the nearest stations.

Place search uses the public OpenStreetMap Nominatim search service. Searches
are only sent when you submit the form, not while typing, and repeated searches
are cached in memory during the Home Assistant process. Forecasts remain
station-based; the integration uses the nearest suitable Vedur station rather
than a gridded point forecast.

## Current Conditions

Weather entity numeric values such as temperature, humidity, dew point, wind,
pressure, and precipitation come from current automatic station observations.
The Home Assistant condition state, such as `sunny`, `partlycloudy`, `rainy`,
or `clear-night`, comes from the nearest XML forecast time point rather than
from the observation API. The observation API does not currently provide a
single current-condition text field equivalent to Home Assistant's weather
condition state.

## One-Off Location Forecasts

Use the `vedur_is.get_forecast_for_location` action to get the nearest Vedur
observation station and XML forecast station for a coordinate without adding a
station to the integration.

Example:

```yaml
action: vedur_is.get_forecast_for_location
data:
  latitude: 64.1466
  longitude: -21.9426
  type: hourly
response_variable: vedur_forecast
```

Supported forecast types are `hourly`, `daily`, and `twice_daily`. Daily and
twice-daily responses are derived summaries, matching the integration's weather
entity behavior when derived forecasts are enabled.

The action response includes `observation_station`, `observation`,
`forecast_station`, and `forecast`. Forecasts are still station-based; the
integration selects the nearest forecast-capable Vedur station for the
coordinate.

## API Endpoints Used

- `GET https://api.vedur.is/weather/stations?active=true&station_type=sj`
- `GET https://api.vedur.is/weather/observations/aws/10min/latest?parameters=basic&station_id=<id>`
- `GET https://xmlweather.vedur.is/?op_w=xml&type=forec&lang=en&view=xml&ids=<ids>`
- `GET https://gottvedur.is/_next/data/.../en/vedur/athuganir/<station>.json`
  as a fallback for missing current observation values.
- `GET https://api.vedur.is/cap/v1/capbroker/active/category/Met` plus alert
  detail payloads for active weather warnings.

## API Examples

Fetch the latest processed 10-minute automatic weather observation for station `1470`
(`Reykjavík`):

```bash
curl -L "https://api.vedur.is/weather/observations/aws/10min/latest?parameters=basic&station_id=1470"
```

Pretty-print the same response:

```bash
curl -L "https://api.vedur.is/weather/observations/aws/10min/latest?parameters=basic&station_id=1470" \
  | python3 -m json.tool
```

Fetch parameter metadata for the latest 10-minute observation endpoint:

```bash
curl -L "https://api.vedur.is/weather/parameters?url=/observations/aws/10min/latest&locale=en" \
  | python3 -m json.tool
```

In the `basic` response, useful current-weather fields include `t`
temperature, `rh` humidity, `td` dew point, `f` wind speed, `fg` wind gust, `d`
wind direction, `p` pressure, and `r` precipitation.

## Weather Alerts

The integration creates a `sensor.weather_alerts` entity. Its state is the
number of active Icelandic Met Office weather warning areas. Attributes include
the highest warning color and severity, affected areas, and a list of alert
details with headline, description, timing, links, and CAP polygon data.

## Diagnostic Sensors

Every weather device/location has disabled-by-default diagnostic sensors for
its current weather values. Enable only the entities needed by dashboards,
automations, or displays that cannot read attributes from a `weather.*` entity.
For example, enabling **Wind speed** under the Home weather device provides a
normal sensor entity suitable for NSPanel displays without a template sensor.

| API key | Sensor | Unit |
| --- | --- | --- |
| forecast/observation | Condition | Home Assistant weather condition |
| `t` | Temperature | Celsius |
| `rh` | Humidity | percent |
| `td` | Dew point | Celsius |
| `f` | Wind speed | m/s |
| `fg` | Wind gust | m/s |
| `d` | Wind direction | degrees |
| `p` | Sea-level pressure | hPa |
| `r` | Precipitation, last hour | mm |

Null values from the API are represented as unavailable states unless the
gottvedur.is fallback provides a value.
