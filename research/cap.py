"""CAP research: archive provider messages without interpreting warning state."""

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

from .indexing import check_problem, normalize_time

BASE = "https://api.vedur.is/cap"
NS = "urn:oasis:names:tc:emergency:cap:1.2"
LIST_KINDS = {"cap_index", "cap_detailed"}


def identity(value):
    if not isinstance(value, dict):
        raise ValueError("CAP reference is not an object")
    parts = tuple(value.get(key) for key in ("sender", "identifier", "sent"))
    if not all(isinstance(part, str) and 0 < len(part) <= 512 for part in parts):
        raise ValueError("Incomplete CAP message identity")
    stamp = normalize_time(parts[2])
    if (
        stamp is None
        or datetime.fromisoformat(parts[2].replace("Z", "+00:00")).tzinfo is None
    ):
        raise ValueError("CAP sent time must include a timezone")
    return parts[0], parts[1], stamp


def references(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        result = []
        for item in value.split():
            fields = item.split(",")
            if len(fields) != 3:
                raise ValueError("Invalid CAP references triple")
            result.append(dict(zip(("sender", "identifier", "sent"), fields)))
    else:
        result = value if isinstance(value, list) else [value]
    for item in result:
        identity(item)
    return result


def xml_object(node):
    result = {}
    for child in node:
        key = child.tag.removeprefix("{" + NS + "}")
        value = xml_object(child) if len(child) else child.text
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def parse(job, body, status=200):
    kind = job["kind"]
    if status == 204 and kind in LIST_KINDS and not body.strip():
        return [], []
    if status != 200:
        raise ValueError("Unexpected CAP response status")
    if kind == "cap_xml":
        root = ET.fromstring(body)
        if root.tag != "{" + NS + "}alert":
            raise ValueError("Expected CAP 1.2 alert XML")
        records = [xml_object(root)]
    else:
        payload = json.loads(body)
        check_problem(payload)
        if kind in LIST_KINDS:
            if not isinstance(payload, list):
                raise ValueError(
                    "Expected CAP message list (null is not an empty list)"
                )
            records = payload
        elif (
            kind == "cap_json"
            and isinstance(payload, dict)
            and isinstance(payload.get("alert"), dict)
        ):
            records = [payload["alert"]]
        else:
            raise ValueError("Expected CAP alert object")
    discovered = []
    for record in records:
        key = identity(record)
        if "message" in job and key != identity(job["message"]):
            raise ValueError("CAP detail identity differs from requested message")
        if kind in {"cap_json", "cap_xml"}:
            for field in ("msgType", "status", "scope"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise ValueError("Missing CAP " + field)
        else:
            discovered.append(record)
        discovered.extend(references(record.get("references")))
    return records, discovered


def history_url(start, end):
    return (
        BASE + f"/capbroker/sent/from/{start.year}/{start.month}/{start.day}"
        f"/to/{end.year}/{end.month}/{end.day}/category/Met"
    )


def job(name, kind, url, interval=300, **extra):
    return dict(
        id=name,
        source="cap",
        kind=kind,
        url=url,
        interval=interval,
        station_ids=[],
        body=None,
        hour_offset=None,
        **extra,
    )


def initial_jobs(settings):
    start, end = (
        date.fromisoformat(settings[key]) for key in ("seed_start", "seed_end")
    )
    return [
        job("cap_active", "cap_index", BASE + "/capbroker/active/category/Met"),
        job(
            "cap_active_detailed",
            "cap_detailed",
            BASE + "/capbroker/active/detailed/all",
        ),
        job(
            "cap_recent",
            "cap_index",
            history_url(end - timedelta(days=2), end),
            3600,
            cap_lookback_days=2,
        ),
        job(
            "cap_seed",
            "cap_index",
            history_url(start, end),
            86400,
            once=True,
            discovery_limit=settings["seed_limit"],
        ),
        job("cap_schema", "metadata", BASE + "/openapi.json", 86400),
        job(
            "cap_regions",
            "metadata",
            BASE + "/capcreator/forecast_regions?region_type=all",
            86400,
        ),
    ]


def install(archive, settings):
    start, end = (
        date.fromisoformat(settings[key]) for key in ("seed_start", "seed_end")
    )
    if (
        settings.get("schema_version") != 1
        or not 0 <= (end - start).days <= 31
        or not 1 <= settings["seed_limit"] <= 100
        or not 100 <= settings["maximum_versions"] <= 2000
    ):
        raise ValueError("Unsafe CAP research limits")
    encoded = json.dumps(settings, sort_keys=True)
    previous = archive.db.execute(
        "SELECT value FROM meta WHERE key='cap_plan'"
    ).fetchone()
    if previous:
        if previous[0] != encoded:
            raise ValueError("CAP plan changed: retain the frozen extension")
        return
    jobs = initial_jobs(settings)
    now = time.time()
    with archive.db:
        archive.db.execute("INSERT INTO meta VALUES ('cap_plan',?)", (encoded,))
        archive.db.execute("INSERT OR IGNORE INTO hosts(host) VALUES ('api.vedur.is')")
        for index, item in enumerate(jobs):
            archive.db.execute(
                "INSERT INTO jobs(id,definition,next_due) VALUES (?,?,?)",
                (item["id"], json.dumps(item), now + index * 2),
            )
        archive.event("cap_enabled", settings)


def queue_messages(archive, records, request_id):
    settings = json.loads(
        archive.db.execute("SELECT value FROM meta WHERE key='cap_plan'").fetchone()[0]
    )
    count = archive.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE json_extract(definition,'$.kind')='cap_json'"
    ).fetchone()[0]
    limited = 0
    for record in records:
        key = identity(record)
        digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        if archive.db.execute(
            "SELECT 1 FROM jobs WHERE id=?", ("cap_json_" + digest,)
        ).fetchone():
            continue
        if count >= settings["maximum_versions"]:
            limited += 1
            continue
        message = {field: record[field] for field in ("sender", "identifier", "sent")}
        path = "/capbroker/sender/{}/identifier/{}/sent/{}".format(
            *(
                quote(message[field], safe="")
                for field in ("sender", "identifier", "sent")
            )
        )
        for fmt in ("json", "xml"):
            item = job(
                "cap_" + fmt + "_" + digest,
                "cap_" + fmt,
                BASE + path + "/" + fmt,
                once=True,
                message=message,
                discovered_by_request=request_id,
            )
            archive.db.execute(
                "INSERT INTO jobs(id,definition,next_due) VALUES (?,?,?)",
                (item["id"], json.dumps(item), time.time()),
            )
        count += 1
    if limited:
        archive.event(
            "cap_discovery_limit", {"request_id": request_id, "omitted": limited}
        )


def record_response(archive, request_id, item, body, status):
    records, discovered = parse(item, body, status)
    # Validate the whole payload before indexing; never infer cancellations from
    # disappearing list entries or drop expired, Test or Exercise messages.
    if "discovery_limit" in item:
        discovered.sort(key=lambda value: identity(value)[2], reverse=True)
        omitted = max(0, len(discovered) - item["discovery_limit"])
        discovered = discovered[: item["discovery_limit"]]
        if omitted:
            archive.event(
                "cap_seed_limit", {"request_id": request_id, "omitted": omitted}
            )
    queue_messages(archive, discovered, request_id)
    archive.db.execute(
        "INSERT INTO cap_snapshots VALUES (?,?)", (request_id, len(records))
    )
    for record in records:
        archive.db.execute(
            "INSERT INTO cap_records(request_id,sender,identifier,sent,record) VALUES (?,?,?,?,?)",
            (
                request_id,
                record["sender"],
                record["identifier"],
                record["sent"],
                json.dumps(record),
            ),
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    end = datetime.now(timezone.utc).date()
    settings = dict(
        schema_version=1,
        seed_start=str(end - timedelta(days=14)),
        seed_end=str(end),
        seed_limit=100,
        maximum_versions=2000,
    )
    with args.output.open("x") as handle:
        json.dump(settings, handle, indent=2)
        handle.write("\n")
