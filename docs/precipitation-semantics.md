# Precipitation: Amount, Time Window, and Intensity

This explains the investigation and fix for issue #4. Current precipitation now
uses only Gottvedur's preceding-hour amount, exposed as a one-hour-average
intensity in mm/h. The official API's ten-minute `r` is no longer used for this
sensor. Other observation fields and forecast precipitation are unchanged.

## 1. An Amount Needs a Time Window

Millimeters describe an amount of precipitation, expressed as water depth.
"1.2 mm" alone does not say whether it accumulated over ten minutes, an hour,
or a day. A rate adds a time denominator, for example mm/h.

An average rate is the amount divided by the duration. It describes what
already happened over that window, not what will happen in the next hour.

## 2. Official r Is a Ten-Minute Amount

The integration fetches:

```text
https://api.vedur.is/weather/observations/aws/10min/latest?parameters=basic&station_id=1474
```

The [official parameter metadata](https://api.vedur.is/weather/parameters?url=%2Fobservations%2Faws%2F10min%2Flatest&locale=en)
identifies r as a quality-checked/corrected total over ten minutes, measured
in mm. This is not r_raw, which is a cumulative gauge counter since reset.
The separate hourly endpoint has a different accumulation window.

If this ten-minute r were 1.2, it would mean 1.2 mm fell during that ten-minute
measurement interval. The normalized average rate is:

```text
1.2 mm / (10/60 hours) = 7.2 mm/h
```

That does NOT mean 7.2 mm actually fell during the preceding whole hour, or
that another 7.2 mm is forecast. It is a ten-minute average expressed per hour.

During the earlier bulk API check, processed r was null at all 290 AWS stations
in that response. The definition comes from the metadata, not a successful
positive processed-r sample. Null is unknown, never proof of dry weather.

## 3. Gottvedur Latest Is a Different Window

The [latest endpoint](https://www.gottvedur.is/api/observations/latest/?stationId=6459)
has a field named precipitation. Its website label alone did not establish
the window, so we compared it to detailed measurements.

Captured latest response for Lonakvisl, station 6459, on 2026-10-23:

```json
{
  "observationTime": "2026-10-23T11:10:00",
  "station": 6459,
  "precipitation": 2.7
}
```

This is an excerpt of the saved response, not a promise about the value a live
latest link returns later. The observed behavior is a rolling preceding-hour
amount, not a new amount to add to the previous latest response.
The [captured excerpts](../tests/fixtures/precipitation-6459-2026-10-23.json)
preserve the exact numbers used below when the live latest response changes.

## 4. How the Six-Sample Check Worked

The [details for the hour ending 11:00](https://www.gottvedur.is/api/observations/detailsByHour/?stationId=6459&hour=2026-10-23T11%3A00%3A00)
and [the following hour](https://www.gottvedur.is/api/observations/detailsByHour/?stationId=6459&hour=2026-10-23T12%3A00%3A00)
returned these ten-minute amounts:

| Interval ending at UTC | Amount, mm |
| --- | --- |
| 10:20 | 0.2 |
| 10:30 | 0.0 |
| 10:40 | 0.6 |
| 10:50 | 0.0 |
| 11:00 | 0.7 |
| 11:10 | 1.2 |
| Sum | 2.7 |

Those are six adjacent, non-overlapping ten-minute intervals spanning
10:10-11:10. Their sum is exactly the captured latest value, 2.7 mm.
As another check, the cumulative gauge rose from 634.5 at 10:10 to 637.2 at
11:10: also 2.7 mm, with no reset in that interval.

The latest value was therefore neither just the last ten minutes (1.2 mm)
nor the partial 11:00 clock hour. Similar complete-window comparisons at
stations 6472 and 2693 matched 0.1 and 0.3 mm respectively. This is empirical
evidence of the rolling-hour interpretation, not a published API guarantee
for every station type and endpoint.

The same precipitation field name means different things in latest and
detailsByHour. Do not assume every Gottvedur endpoint uses the same window.

The six-sample sum was a research cross-check. The integration does not
fetch and sum these detailed samples in production.

## 5. What the Old Implementation Did Wrong

In the old implementation, the Gottvedur field map said:

```python
"precipitation": "r",
```

The integration reads this object as pageProps.latestObservation
from the site's Next.js page data. The public latest API above is the simpler
endpoint used for the investigation.

The fallback merge filled a null/missing official r with that value unchanged.
The resulting sensor used PRECIPITATION with native unit mm. Source diagnostics
identified which provider supplied it, but did not preserve the accumulation
window.

Consequently, one entity could mean ten-minute amount when official data was
available and rolling-hour amount when the fallback was used. Both numbers
really were mm, but they were not the same measurement. Changing only the name
or unit would not have fixed this.

## 6. What Normalizing to Intensity Would Do

```text
average intensity (mm/h) = amount (mm) / duration (hours)
```

| Input | Window | Average intensity | Numerical change |
| --- | --- | --- | --- |
| Official r = 1.2 mm | 10 min | 7.2 mm/h | Multiply by 6 |
| Gottvedur latest = 2.7 mm | 60 min | 2.7 mm/h | Number unchanged |

There is no conversion that both retains every original number and gives the
same physical meaning. What we must preserve is meaning, with the original
amount and window available as metadata or separate sensors.

The example's 7.2 and 2.7 do not contradict each other: the last ten minutes
were wetter than the preceding-hour average. Even after using the same mm/h
unit, switching sources changes the smoothing window. An attribute such as
averaging_period_minutes must make that explicit.

Getting a genuinely consistent one-hour average from ten-minute amounts would
require all six valid, non-overlapping amounts and their timestamps. Missing
any one must leave the result unknown; no missing amount can be filled with
zero. Summing overlapping rolling-hour latest values would double-count rain.

## 7. Implemented Fix: One Source, One Window

[api.py](../custom_components/vedur_is/api.py) no longer maps Gottvedur
`precipitation` to official `r`. It stores the valid non-negative hourly amount
separately as `ObservationPrecipitation`, retaining its own window end. Official
`r` remains raw API data internally, but is never a fallback for this sensor.
Manned observations are excluded because the one-hour interpretation has only
been checked for automatic weather stations.

[observation_utils.py](../custom_components/vedur_is/observation_utils.py)
exposes the amount divided by one hour, preserving the original numerical value
as mm/h, and checks the source timestamp against UTC now. Missing, non-numeric,
negative, non-finite, older-than-30-minute, or more-than-five-minute-future
values remain unknown. A zero is a real, valid zero. The accumulation window
stays 60 minutes regardless of how often observations are fetched.

[sensor.py](../custom_components/vedur_is/sensor.py) keeps the existing sensor
unique IDs (legacy key `r`) but uses Home Assistant's `PRECIPITATION_INTENSITY`
class and `mm/h`. Both sensors and weather entities expose the original amount
in mm, intensity in mm/h, source, and window start/end. The ambiguous custom
weather `precipitation` attribute is replaced by `precipitation_amount` and
`precipitation_intensity`, each with an explicit unit. Forecast amounts remain
in mm and continue using Home Assistant's separate forecast unit conversion.

No additional calls to the historical/detail APIs are made. The existing
Gottvedur observation cache and ten-minute refresh cadence are reused. A failed
Gottvedur fetch cannot silently substitute official `r` or manufacture a zero.

Changing an existing entity from mm to mm/h changes its quantity and can conflict
with historical HA statistics. The [README upgrade notice](../README.md#current-precipitation)
identifies that change. Existing historical samples are not converted,
relabelled, or deleted; any statistics repair should be reviewed separately.

## Separate: Forecast Precipitation

The forecast parser already handles one-, three-, and six-hour amounts as
interval totals in mm. Those stay amounts: a 3 mm total over six hours is not
3 mm/h, and must not be multiplied by six or duplicated into six hourly rows.
Derived daily totals sum non-overlapping, complete intervals. Missing coverage
remains unknown. Precipitation probability is a different quantity; it has
not been confirmed and must not be inferred from these amounts or humidity.
