# Icelandic Met Office Weather for Home Assistant

A Home Assistant custom integration that exposes current weather observation
and forecast data from the Icelandic Met Office through vedur.is.

This project is not affiliated with or endorsed by the Icelandic Met Office.
Weather data is provided by the Icelandic Met Office (vedur.is).

## Features

- UI config flow.
- Optional weather for Home, using Home Assistant's home zone. Enabled by
  default.
- Optional station selection for additional station weather entities.
- Station selector labels include the station owner when provided by the API.
- Multiple optional stations in one config entry.
- Optional weather and diagnostic entities for selected stations. Enabled by
  default.
- Optional weather entities that follow Home Assistant `person` entities.
  Enabled by default.
- Hourly, daily, and twice-daily forecasts through `weather.get_forecasts`.
  Daily and twice-daily forecasts are derived from future XML time points using
  period high/low temperatures, max wind, and the most significant mapped
  condition in the period.
- Hourly polling through a `DataUpdateCoordinator`.
- Diagnostic sensors for temperature, humidity, dew point, wind speed, wind
  gust, wind direction, pressure, and precipitation. These are disabled by
  default because the `weather.*` entities are the primary interface.
- Station metadata is used for Home Assistant device registry entries.
- Weather entities expose observation source diagnostics and forecast station
  metadata.
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

## API Endpoints Used

- `GET https://api.vedur.is/weather/stations?active=true&station_type=sj`
- `GET https://api.vedur.is/weather/observations/aws/hour/latest?parameters=basic&station_id=<id>`
- `GET https://xmlweather.vedur.is/?op_w=xml&type=forec&lang=en&view=xml&ids=<ids>`
- `GET https://gottvedur.is/_next/data/.../en/vedur/athuganir/<station>.json`
  as a fallback for missing current observation values.

## Diagnostic Sensors

| API key | Sensor | Unit |
| --- | --- | --- |
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
