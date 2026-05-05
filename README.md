# Vedur.is Home Assistant Integration

A Home Assistant custom integration that exposes current weather observation
and forecast data from the Icelandic Met Office.

## Features

- UI config flow.
- Multiple selected stations in one config entry.
- Weather entities for selected stations.
- Weather entity for Home Assistant's home zone.
- Optional weather entities that follow Home Assistant `person` entities.
- Hourly, daily, and twice-daily forecasts through `weather.get_forecasts`.
- Hourly polling through a `DataUpdateCoordinator`.
- Diagnostic sensors for temperature, humidity, dew point, wind speed, wind
  gust, wind direction, pressure, and precipitation. These are disabled by
  default because the `weather.*` entities are the primary interface.
- Station metadata is used for Home Assistant device registry entries.
- No API key required.

## Installation

### HACS Custom Repository

1. Add this repository to HACS as an integration custom repository.
2. Install `Vedur.is`.
3. Restart Home Assistant.
4. Go to **Settings > Devices & services > Add integration** and search for
   `Vedur.is`.

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
