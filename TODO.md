# TODO

Future ideas and follow-up work for the Vedur.is Home Assistant integration.

## Near-Term Polish

- Add a Home Assistant brand/icon asset so the integration page does not show
  the placeholder icon.
- Add a configurable warning threshold for forecast station distance, so users
  can see when forecasts may be less local than observations.
- Add more current weather attributes only after confirming which source fields
  are stable and consistently populated. Candidate fields include visibility
  (`v`) and cloud cover (`n`), but these should not be exposed as first-class
  weather attributes until their units, meanings, and fallback behavior are
  verified across several station types.
- Keep condition mapping current as new Vedur.is/XML forecast text variants
  appear. The English and Icelandic XML `W` phrases observed on May 6, 2026 are
  mapped; future work should focus on newly observed `exceptional` phrases and
  on SYNOP/current-condition text if the integration starts consuming that data.
- Add better documentation examples for dashboard badges, person-following
  weather, station weather, and `weather.get_forecasts`.
- Add a Home Assistant diagnostics download for config entry debug data without
  exposing secrets.

## Configuration Ideas

- Add a configurable maximum distance for Home/person observation stations.
- Add a configurable maximum distance for Home/person forecast stations.
- Consider optional grouping or labels for station weather entities when the
  nearest forecast station differs from the selected observation station.

## Weather Data Sources

- Add Textaspá text forecasts. The Icelandic Met Office recommends that these
  handwritten meteorologist forecasts take priority over automatic forecasts
  when there is a large difference between them.
- Improve weather warnings after the first CAP alert sensor pass. Useful next
  steps include location-aware Home/person alert matching, optional per-area or
  per-person alert sensors, and richer dashboard examples.
- Add earthquake data.
- Investigate whether gottvedur.is exposes stable public APIs for additional
  current or historical observation values.
- Investigate historical observations for trend sensors or recent-condition
  context.

## Forecast Improvements

- Add tests around stations without direct XML forecasts using a nearby
  forecast-capable station.
- Add tests for forecast distance and fallback metadata.
- Explore whether forecasts can be requested or interpolated by latitude and
  longitude. Current known public sources appear station-based.

## Release Polish

- Add HACS release notes/changelog.
- Add more screenshots to the README once the UI settles.
- Add CI for linting and tests.
