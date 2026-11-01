# API research campaign

The collector runs separately from Home Assistant. It uses system Python 3.12+
and its standard library only. Production HA does not need this branch installed.
This is an unofficial research project, not an IMO service.

## Scope and schedule

The frozen seed in `stations.json` selects 36 physical stations (32 `sj`, three
`sk`, one `ur`) plus four virtual forecast locations from the October 24 audit.
It includes IMO, avalanche-monitoring, Vegagerdin, Landsnet, Landsvirkjun,
coastal, ISAVIA, private and research operators, across multiple regions.
Sparse stations, same-name/different-ID stations, and missing forecast coverage
are intentional. Seed capability flags are historical observations, not promises.
Fresh provider catalogues are archived during the campaign.

| Product | Nominal interval | Scope |
| --- | --- | --- |
| Official AWS ten-minute and hourly observations, all fields | 5 min | Physical station IDs in batch requests |
| Official raw ten-minute, raw cloud, and SYNOP latest | 5 min | Physical station IDs in batch requests |
| XML observations, English and Icelandic | 5 min | Physical stations, batched |
| Gottvedur latest observations | 5 min | Each physical station |
| Gottvedur batch observations | 5 min | All selected IDs |
| XML forecasts, English and Icelandic | 15 min | All selected IDs, batched |
| Gottvedur chart and batch forecasts | 15 min | All selected IDs |
| Gottvedur current-hour details | 15 min | Eight precipitation research stations |
| Gottvedur previous-hour details | 60 min | Same eight stations |
| OpenAPI, station catalogues, capabilities, bilingual parameter definitions | 24 h | Official API |
| Gottvedur observation/forecast map catalogues | 6 h | Iceland boundary |

The official weather OpenAPI schema inspected on November 1 lists observation
products, not a weather forecast endpoint. Forecast research therefore uses XML
and Gottvedur. The collector does not invent a corresponding official endpoint.
Unsupported station/product combinations are recorded, not silently substituted.

There are 119 jobs, nominally about 741 HTTP requests/hour (12.4/minute) before
backoff. Only one request runs at a time, with at least two seconds between
completion and the next start. Job phases are retained; missed slots are not
replayed after downtime. The initial pass gradually visits every job.

The campaign stops after 14 calendar days, preserving the data. Restarts retain
the original stop time. The workstation must remain powered and connected;
offline time is missing research coverage, not evidence of an upstream failure.
At least seven days of useful coverage is the first review target, not a claim
that one week proves every station's permanent schedule or seasonal behaviour.

## Storage and interpretation

Use a local filesystem, not the production HA share. The data directory contains:

```text
index.sqlite3                 request ledger, samples, schedules, backoff, events
index.sqlite3-wal / -shm       SQLite's active write-ahead log files
responses/ab/<sha256>.bin.gz   compressed response-body bytes
collector.lock                exclusive writer lock
```

Every attempt gets a request row, including unchanged responses, HTTP errors,
transport failures, malformed payloads and interrupted requests. Successful and
error response bodies are stored before parsing. Bodies are addressed by SHA-256;
identical bodies share storage, but their separate fetch timestamps remain.
No client response cache or conditional GET suppresses measurements of publication
behaviour. Provider cache headers are retained, including Date, Age, Cache-Control,
ETag, Last-Modified and Retry-After. Cookies are excluded.

The ledger retains the exact requested URL, method/body through its immutable job
definition, final URL, scheduled/start/end times, monotonic request duration, HTTP
status, selected response headers, errors, body size/hash and truncation flag.
It requests identity encoding. Archived bytes are the body after HTTP transfer
decoding; any provider content encoding is retained in headers and decoded only
for indexing. A response is bounded at 8 MiB; oversized bodies are explicitly
marked partial, never treated as complete successful data.

`samples` contains per-station observation records and compact forecast summaries:
field presence/null/empty counts, source timestamps, coverage and provider errors.
All forecast points, raw values, model labels, details and station names remain
in the original bodies. Future timestamps, values above nominal ranges, empty
fields and provider errors are retained. There is no unit conversion, correction,
station fallback, freshness rejection, or meteorological inference.

Timestamp normalization is a query aid. Original strings remain available;
naive timestamp strings are indexed under an explicit UTC assumption. A timestamp
does not by itself establish whether it marks an interval's start or end, or
whether a partial hourly aggregate is final. Those are research questions.
Forecast valid times and observation times must be analysed separately. The report
excludes forecasts from observation-cadence calculations.

Observed first-seen times only bound publication latency to polling resolution;
they do not establish the exact upstream release instant. Source records may
be corrected without their timestamps changing. Raw archives permit later
reinterpretation if an index parser or our understanding changes.

SQLite uses WAL and full synchronous writes. It is the query index and request
manifest; no external database server is needed. Backups of a running database
should use SQLite's online backup API, not copy just the main file while ignoring
its WAL. The raw archive and its index should be backed up together.

The default limits are 4 GiB for compressed bodies plus SQLite files and at least
2 GiB free filesystem space. They are checked every minute, so a small overshoot
is possible. On reaching a limit collection stops visibly; it never deletes old
evidence. Raw responses, databases and local deployment settings are not committed
to Git. Small reviewed provider excerpts are used as parser test fixtures.

## Failure handling

- HTTP 429 honours Retry-After, pauses that host for at least 15 minutes, and
  persistently doubles its job intervals, up to 16 times the base interval.
- HTTP 403 pauses the host for at least 24 hours. No identity/IP rotation.
- Server/transport errors apply increasing host backoff, bounded at one hour
  unless Retry-After requests longer, plus per-job error spacing.
- Missing/invalid endpoint responses such as HTTP 404/422 are retried no more
  frequently than every six hours. They do not block healthy endpoints.
- Parser failures keep the complete body and apply per-job backoff. HTTP success
  and successful indexing are reported separately.
- Backoff, slowdown factors, schedules and run events survive process restarts.

## Running and inspecting

From the branch checkout, generate a new configuration outside the repository:

```sh
python3 -m research.plan --output /path/to/campaign.json
python3 -m research.collector --config /path/to/campaign.json --data-dir /path/to/data
python3 -m research.report /path/to/data --summary
python3 -m research.report /path/to/data
python3 -m research.report /path/to/data --body 123
python3 -m unittest discover -s tests -v
```

The full report includes HTTP outcomes, parser errors, coverage, distinct source
timestamps, observed timestamp steps and first-seen ages. Those are measurements
of this sample, not automatic station classifications. Structured query examples:

```sql
SELECT job_id, status, error, parser_error, COUNT(*)
FROM requests GROUP BY job_id, status, error, parser_error;

SELECT r.id, datetime(r.finished, 'unixepoch') AS fetched_utc,
       s.station_id, s.time_raw, s.summary
FROM samples s JOIN requests r ON r.id = s.request_id
WHERE r.job_id = 'gott_latest_1474' ORDER BY r.id;

SELECT id, url, status, blob_sha256 FROM requests WHERE parser_error IS NOT NULL;
```

For unattended operation, render `vedur-api-research.service.in` with an existing
unprivileged account and an absolute research root. Copy the tested `research/`
package to `<root>/runner/research/`, place the frozen config at
`<root>/campaign.json`, and create `<root>/data/` writable by that account.
The service reads the copied code, so editing the development branch cannot
silently change an active experiment. The archive records code hashes at startup.

Install the rendered unit as `vedur-api-research.service`, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now vedur-api-research.service
systemctl status vedur-api-research.service
journalctl -u vedur-api-research.service --since today
sudo systemctl stop vedur-api-research.service
```

The service resumes after reboot, stops cleanly on request, and does not restart
after campaign completion or a configuration/storage-limit stop. Process crashes
have a bounded restart policy. There is no automatic HA deployment or release.

## Questions to answer before integration features

1. Which fields does each source/station actually provide, and which disappear?
2. What do timestamps, accumulation periods, mean winds and gust periods mean?
3. How often do values/timestamps change, and how late are they first observed?
4. Are hourly products partial, revised, or differently timestamped from details?
5. How do API caches, errors and station aliases affect the apparent freshness?
6. Which forecast models, intervals, issue times and icon codes occur?
7. Where do different products disagree, and is that explained by their definitions?

Only after these questions are supported by evidence should we specify sensors,
units, expiry policies, source selection or any derived values.
