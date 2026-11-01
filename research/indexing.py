"""Lossless-source summaries, not meteorological interpretation or HA mapping."""

from collections import Counter
from datetime import datetime, timezone
import json
import xml.etree.ElementTree as ET


class ProviderError(ValueError):
    """A problem-details response delivered inside an HTTP-success envelope."""

    def __init__(self, payload):
        self.status = payload["status"]
        super().__init__(str(payload.get("detail") or payload.get("title")))


def check_problem(payload):
    if (
        isinstance(payload, dict)
        and type(payload.get("status")) is int
        and 400 <= payload["status"] <= 599
        and isinstance(payload.get("title"), str)
        and ("detail" in payload or "type" in payload)
    ):
        raise ProviderError(payload)


def normalize_time(value):
    """Index provider dates as UTC; retain the untouched value separately."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def field_state(value):
    if value is None:
        return "null"
    if value == "":
        return "empty"
    if value == "?":
        return "question_mark"
    return "present"


def summary(station, records, *, timestamp_key, extra=None):
    fields = {}
    times = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Expected a record object")
        raw_time = record.get(timestamp_key)
        if raw_time is not None:
            times.append(raw_time)
        for key, value in record.items():
            fields.setdefault(key, Counter())[field_state(value)] += 1
    normalized = sorted(value for raw in times if (value := normalize_time(raw)))
    result = {
        "record_count": len(records),
        "fields": fields,
        "first_time_utc": normalized[0] if normalized else None,
        "last_time_utc": normalized[-1] if normalized else None,
        "invalid_time_count": sum(normalize_time(raw) is None for raw in times),
        "utc_assumption_for_naive_times": True,
    }
    result.update(extra or {})
    if len(records) == 1:
        result["record"] = records[0]
    return (
        str(station),
        times[0] if len(times) == 1 else None,
        normalized[0] if len(times) == 1 and normalized else None,
        result,
    )


def extract(job, body):
    """Index lightweight summaries; raw bodies always remain the authority."""
    kind = job["kind"]
    if kind == "metadata":
        check_problem(json.loads(body))
        return []
    if kind.startswith("xml_"):
        root = ET.fromstring(body)
        expected_root = "forecasts" if kind == "xml_forec" else "observations"
        if root.tag != expected_root:
            raise ValueError("Unexpected XML root: " + root.tag)
        results = []
        for station in root.findall("station"):
            error = station.findtext("err")
            if kind == "xml_forec":
                records = [
                    {item.tag: item.text for item in point}
                    for point in station.findall("forecast")
                ]
                key = "ftime"
            else:
                records = [
                    {item.tag: item.text for item in station if item.tag != "forecast"}
                ]
                key = "time"
            results.append(
                summary(
                    station.get("id", "unknown"),
                    records,
                    timestamp_key=key,
                    extra={
                        "provider_error": error,
                        "issued_raw": station.findtext("atime"),
                    },
                )
            )
        return results
    payload = json.loads(body)
    check_problem(payload)
    if kind == "official_observation":
        if not isinstance(payload, list):
            raise ValueError("Official observations are not a list")
        return [summary(row["station"], [row], timestamp_key="time") for row in payload]
    if kind == "gott_observation":
        if not isinstance(payload, dict) or "station" not in payload:
            raise ValueError("Missing latest observation station")
        return [summary(payload["station"], [payload], timestamp_key="observationTime")]
    if kind == "gott_forecast":
        if not isinstance(payload, dict) or not isinstance(
            payload.get("nextDays"), list
        ):
            raise ValueError("Missing forecast nextDays")
        extra = {
            "top_level_fields": sorted(payload),
            "issued_raw": payload.get("forecastTime"),
        }
        return [
            summary(
                job["station_ids"][0],
                payload[key],
                timestamp_key="time",
                extra={**extra, "provider_product": key},
            )
            for key in ("today", "nextDays")
            if isinstance(payload.get(key), list)
        ]
    if kind in {"gott_batch_observation", "gott_batch_forecast"}:
        if not isinstance(payload, list):
            raise ValueError("Gottvedur batch is not a list")
        results = []
        for row in payload:
            if kind == "gott_batch_observation":
                results.append(
                    summary(
                        row["stationId"], [row], timestamp_key="lastObservationTime"
                    )
                )
            else:
                if not isinstance(row.get("hourlyForecasts"), list):
                    raise ValueError("Missing batch hourlyForecasts")
                for key in ("hourlyForecasts", "nextDaysForecasts"):
                    if isinstance(row.get(key), list):
                        results.append(
                            summary(
                                row["stationId"],
                                row[key],
                                timestamp_key="time",
                                extra={
                                    "provider_product": key,
                                    "top_level_fields": sorted(row),
                                },
                            )
                        )
        return results
    if kind == "detail":
        records = payload if isinstance(payload, list) else [payload]
        return [
            summary(job["station_ids"][0], records, timestamp_key="observationTime")
        ]
    raise ValueError("Unknown product kind: " + kind)
