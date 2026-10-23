# Condition Sources and Mapping

## Source Order

1. An explicit icon from the selected station's current observation, when
   supplied. Many automatic stations have no such icon.
2. The Gottvedur forecast icon for the nearest valid forecast instant.
3. Recognized XML W text at that same instant, when available. If the primary
   product does not cover the current time, a valid XML point can supply it.
4. Unknown, not a numerical guess or an exceptional-weather placeholder.

The existing chart request/cache supplies Gottvedur conditions and numeric
forecast values together. This adds no second chart request just for icons.
XML remains available during Gottvedur errors or schema changes. Current
numeric measurements remain observations, independent of the forecast source.

Unknown provider codes are logged with a bounded, deduplicated warning cache.
The original code is retained in diagnostics. No substring classification,
rain/snow inference from temperature, or wind-speed classification is applied.

## Mapping Decisions

These are translations into [HA's fixed condition vocabulary](https://contributors.home-assistant.io/core/entities/weather/),
not new meteorological predictions.

| Gottvedur icon(s) | HA condition |
| --- | --- |
| ClearSky | sunny |
| ClearSkyNight | clear-night |
| PartlyCloudy, PartlyCloudyNight | partlycloudy |
| Cloudy, CloudyNight, Overcast | cloudy |
| LightRain, Rain, LightRainShowers, LightRainShowersNight, RainShowers, RainShowersNight | rainy |
| LightDrizzle, Drizzle, FreezingRain | rainy |
| LightSleet, Sleet, SleetShowers, SleetShowersNight | snowy-rainy |
| LightSnow, Snow, SnowShowers, SnowShowersNight, BlowingSnow | snowy |
| Storm | windy |
| Fog, FogDrizzle, FogMist | fog |
| Hail, HailNight | hail |
| LightThunder, Thunder | lightning |
| DustDevil, DustStorm | exceptional |

All 35 weather cases handled by the inspected Gottvedur renderer are covered.
This is not a promise that the provider cannot introduce additional codes.

The XML fallback explicitly covers all 23 bilingual descriptions listed in
the provider's 2018 guide, plus the existing exact English Heavy rain alias.
Clear sky / Heidskirt require solar classification. Other supported XML text
maps to the corresponding categories above. Unrecognized compound phrases
are not guessed from keywords.

### What Loses Detail

- Storm is not being assumed to mean thunder: Gottvedur translates it as
  Icelandic Rok and renders a wind symbol. It maps to windy without choosing
  a local wind-speed threshold.
- FreezingRain maps to rainy because HA has no freezing-rain condition. That
  broad condition does not preserve the icing hazard; the raw code does.
- BlowingSnow maps to snowy. It does not claim that new snow is falling.
- FogDrizzle and FogMist map to fog. HA cannot show simultaneous fog and drizzle
  in a single condition.
- DustDevil and DustStorm use exceptional because HA has no dust condition.
  Exceptional is used for these known events, never for absent/unknown data.
- LightThunder and Thunder map to lightning. Rain is not added unless the
  provider actually identifies rain as part of the condition.
- Light/ordinary precipitation and shower distinctions collapse to HA's broader
  conditions. Non-clear day/night distinctions may share one HA state; the
  original icon remains available in `condition_raw` / `forecast_icon`.

These are reasonable defaults, not unresolved blockers. Different display
preferences are possible, particularly for blowing snow, freezing rain, and
fog with drizzle, but no additional numerical weather inference is needed.

XML wind direction Calm / Logn is represented as no bearing, while wind speed
remains zero. Calm is not a compass direction and must not become north.
Other future fields still need unit checks: cloud-cover octas are not percent,
UV irradiance is not UV index, and precipitation probability is not humidity.

## XML Solar Fallback

Gottvedur's explicit day/night decision is not overwritten with local logic.
For XML clear skies, Astral computes sunrise/sunset at the forecast station's
latitude/longitude for the requested UTC date. It uses the library's standard
unobstructed-horizon convention, not local terrain or a fixed 07-22 schedule.
Polar days/nights are handled using solar elevation when an event is absent.
No Home-only sun entity is used for remote people, stations, or destinations.

Hourly forecasts use the forecast valid time. Current XML-derived conditions
use now for solar classification, independently of the selected point's time.
The current-point selector does not use points more than one hour outside the
product's time coverage.

## Provenance

- `condition_source`: gottvedur_observation, official_observation,
  gottvedur_forecast, or xml_forecast.
- `condition_raw`: the selected original provider icon/text.
- `condition_time`: the underlying observation or forecast valid time, not
  the last HTTP fetch time.
- `condition_station_id`: the station supplying that condition.
- `forecast_icon`: the original icon for forecast-based conditions, including
  an unsupported icon when XML text was used as fallback.

Daily and twice-daily summaries remain optional integration-derived products.
Their grouping is separate from translating an individual provider condition.

## Primary Sources

- [Official XML guide, pages 5-6](https://www.vedur.is/media/vedurstofan/XMLthjonusta.pdf#page=5).
- [Gottvedur chart response](https://www.gottvedur.is/api/forecasts/highChart/?stationId=1470).
- [Gottvedur observation history showing populated icons](https://www.gottvedur.is/api/observations/station/?stationId=1).
- [Gottvedur observation page, including its published translations](https://www.gottvedur.is/en/vedur/athuganir/1474/).
- [Inspected provider frontend bundle](https://static.vedur.is/production-assets/vedur/0905c7952d15a21a2fb32689cf573e4ef55360f5/vedur/_next/static/chunks/1apxzk4gcic-h.js):
  search for `case"ClearSky"`; Storm renders the wind glyph.

Research checked 2026-10-23. Website endpoints are structured provider data,
but not a versioned public API contract; future changes must fail safely.
