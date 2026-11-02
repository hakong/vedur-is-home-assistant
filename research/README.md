# API research plan

Collect evidence before rebuilding `vedur_is`: establish what the providers
actually report, what the values mean, and when they become available. Use the
findings to choose sources, units and freshness rules for the new integration.

## Questions to answer

- Which measurements does each station and provider supply, and how consistently?
- What are their units and measurement periods, especially precipitation amounts,
  precipitation intensity, mean wind and gusts?
- What do timestamps represent? How often are records published, how late do they
  arrive, and can values change without their timestamps changing?
- How do missing values, intermittent errors, API caches and station aliases
  affect availability and apparent data age?
- Where do the APIs disagree, and can their definitions explain the differences?
- For forecasts, what models, issue times, valid periods and condition codes are
  supplied? How are precipitation intervals defined?
- For CAP alerts, how do updates, cancellations, expiry, languages and affected
  areas appear in actual messages?

## Collection

The campaign runs from **November 1 to November 15, 2026**, with a first review
after at least a week of useful coverage. It samples **36 physical stations and
four virtual forecast locations** across regions and operators, including IMO,
Vegagerdin, utilities, aviation and private stations. Sparse stations, stations
with the same name, and stations without forecasts are deliberately included.
The selected locations and their seed metadata are in [stations.json](stations.json).

| Data | Nominal polling interval |
| --- | --- |
| Official JSON observations: ten-minute, hourly, raw, cloud and SYNOP products | 5 minutes |
| Legacy XML observations, English and Icelandic | 5 minutes |
| Gottvedur individual and batch observations | 5 minutes |
| XML and Gottvedur forecasts, including batch forecasts | 15 minutes |
| Gottvedur hourly details for eight precipitation research stations | Current hour: 15 minutes; previous hour: 60 minutes |
| Station catalogues, parameter definitions and API schemas | Daily; Gottvedur map catalogues every 6 hours |
| CAP active alerts and detailed active feed | 5 minutes |
| CAP history covering the previous two UTC dates through today | Hourly |
| Complete CAP messages in JSON and XML, including referenced predecessors | Once per discovered version |

Individual and batch products are sampled to compare their coverage, timestamps
and values. Hourly details allow comparison with reported precipitation totals.

CAP collection also includes an initial two-week historical sample, so research
does not depend on warnings occurring during the campaign. Detail discovery is
limited to the 100 newest versions in that initial index and 2,000 versions across
the campaign, including referenced predecessors. Any truncation is recorded.
The detailed active feed includes all CAP categories; the active index and history
queries select meteorological alerts.

## Evidence and limitations

Original response bodies are saved locally, with a SQLite index of request times,
source timestamps, cache headers, errors and station records. Every request is
recorded, including unchanged responses and failures. Client caching is disabled.
Values and timestamps are preserved without unit conversion, correction, station
substitution or weather inference. Observations and forecasts are analysed
separately.

Polling can only bound publication time to the polling interval. Timestamp strings
without an offset are indexed assuming UTC; their originals remain available.
Neither that assumption nor a timestamp alone establishes the measurement period.
A fortnight of data cannot establish year-round behaviour.

Requests are serial and spaced at least two seconds apart. Rate limits and service
failures trigger backoff. Known Gottvedur "no observations found" responses retain
the normal polling cadence. Collection gaps and backoff must be included when
assessing coverage.

**Known sampling gap:** until November 2 at 11:30 UTC, Gottvedur's "No observations
found for station" responses caused six-hour delays for affected jobs. The fix
restored 16 jobs. Its activation time and affected requests are recorded in the
`observation_sampling_policy` event. Earlier intervals for those jobs are
undersampled; the saved responses remain valid evidence of what was fetched.

For CAP, an empty active list is distinct from a failed request. A message
disappearing from a list is not evidence of cancellation; retain explicit message
types and references. Historical sampling improves lifecycle coverage but does
not guarantee that every possible message variant will be seen.

## Result

Produce a station-and-source capability table, documented field and timestamp
semantics, observed reporting cadence and publication delays, and an inventory of
failure cases. Turn representative responses into regression fixtures. Use this
evidence to decide which data the first rebuilt version can expose reliably and
which questions still need provider clarification or longer observation.
