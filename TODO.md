# TODO

Future ideas and follow-up work for the Vedur.is Home Assistant integration.

## Near-Term Polish

- Add a Home Assistant brand/icon asset so the integration page does not show
  the placeholder icon.
- Add clearer station forecast metadata in the UI/docs, especially when a
  station uses a nearby forecast-capable station for forecasts.
- Add a configurable warning threshold for forecast station distance, so users
  can see when forecasts may be less local than observations.
- Add observation completeness diagnostics, such as which current observation
  fields came from the official API, the gottvedur.is fallback, or are still
  unavailable.
- Add more current weather attributes when they are reliable, such as
  visibility and cloud cover.
- Improve condition mapping from Vedur.is forecast text, including more
  Icelandic Met Office wording variants.
- Add better documentation examples for dashboard badges, person-following
  weather, station weather, and `weather.get_forecasts`.
- Add a Home Assistant diagnostics download for config entry debug data without
  exposing secrets.

## Configuration Ideas

- Add options for whether Home weather, person-following weather, and selected
  station weather entities are enabled.
- Add a configurable maximum distance for Home/person observation stations.
- Add a configurable maximum distance for Home/person forecast stations.
- Add a station selection hint or attribute that indicates whether a selected
  station has direct XML forecast support.
- Consider optional grouping or labels for station weather entities when the
  nearest forecast station differs from the selected observation station.

## Weather Data Sources

- Add Textaspá text forecasts. The Icelandic Met Office recommends that these
  handwritten meteorologist forecasts take priority over automatic forecasts
  when there is a large difference between them.
- Add weather warnings.
- Add earthquake data.
- Investigate whether gottvedur.is exposes stable public APIs for additional
  current or historical observation values.
- Investigate historical observations for trend sensors or recent-condition
  context.

## Forecast Improvements

- Review whether daily and twice-daily forecasts should be derived differently
  from the hourly XML data.
- Add tests around stations without direct XML forecasts using a nearby
  forecast-capable station.
- Add tests for forecast distance and fallback metadata.
- Explore whether forecasts can be requested or interpolated by latitude and
  longitude. Current known public sources appear station-based.

## Release Polish

- Add HACS release notes/changelog.
- Add repository topics and a concise public project description.
- Add more screenshots to the README once the UI settles.
- Add CI for linting and tests.
