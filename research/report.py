"""Read-only campaign health and observed station cadence reports."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3
from statistics import median
import time


def report(directory):
    db = sqlite3.connect(
        f"file:{Path(directory).resolve() / 'index.sqlite3'}?mode=ro", uri=True
    )
    db.row_factory = sqlite3.Row
    meta = dict(db.execute("SELECT key,value FROM meta"))
    columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
    provider = "provider_status" if "provider_status" in columns else "NULL"
    outcomes = [
        dict(row)
        for row in db.execute(f"""SELECT job_id,COUNT(*) AS requests,SUM(status BETWEEN 200 AND 299 AND error IS NULL) AS http_ok,
        SUM({provider} IS NOT NULL) AS provider_errors,
        SUM(parser_error IS NOT NULL) AS parser_errors,MAX(finished) AS last_finished,
        MAX(started-scheduled_at) AS maximum_schedule_lag_seconds
        FROM requests GROUP BY job_id ORDER BY job_id""")
    ]
    statuses = [
        dict(row)
        for row in db.execute(
            f"SELECT status,{provider} AS provider_status,error,COUNT(*) AS count FROM requests GROUP BY status,{provider},error"
        )
    ]
    cadence = defaultdict(list)
    for row in db.execute("""SELECT r.job_id,s.station_id,s.time_utc,MIN(r.finished) AS first_seen
        FROM samples s JOIN requests r ON r.id=s.request_id JOIN jobs j ON j.id=r.job_id WHERE s.time_utc IS NOT NULL
        AND json_extract(j.definition,'$.kind') IN ('official_observation','xml_obs','gott_observation','gott_batch_observation')
        GROUP BY r.job_id,s.station_id,s.time_utc ORDER BY r.job_id,s.station_id,s.time_utc"""):
        cadence[(row["job_id"], row["station_id"])].append(
            (datetime.fromisoformat(row["time_utc"]).timestamp(), row["first_seen"])
        )
    observed = []
    for (job, station), values in cadence.items():
        steps = [(b[0] - a[0]) / 60 for a, b in zip(values, values[1:])]
        ages = [(seen - stamp) / 60 for stamp, seen in values]
        observed.append(
            dict(
                job=job,
                station=station,
                distinct_timestamps=len(values),
                median_timestamp_step_minutes=median(steps) if steps else None,
                median_first_seen_age_minutes=median(ages),
                maximum_first_seen_age_minutes=max(ages),
            )
        )
    output = dict(
        started=utc(float(meta["started"])),
        stop_at=utc(float(meta["stop_at"])),
        heartbeat_age_seconds=time.time()
        - float(meta.get("heartbeat", meta["started"])),
        requests=db.execute("SELECT COUNT(*) FROM requests").fetchone()[0],
        distinct_bodies=db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0],
        compressed_body_bytes=db.execute(
            "SELECT COALESCE(SUM(stored_bytes),0) FROM blobs"
        ).fetchone()[0],
        stations_indexed=db.execute(
            "SELECT COUNT(DISTINCT station_id) FROM samples"
        ).fetchone()[0],
        host_controls=[dict(row) for row in db.execute("SELECT * FROM hosts")],
        http_outcomes=statuses,
        jobs=outcomes,
        observed_cadence=observed,
    )
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='cap_snapshots'").fetchone():
        output["cap"] = dict(
            successful_snapshots=db.execute(
                "SELECT COUNT(*) FROM cap_snapshots"
            ).fetchone()[0],
            empty_snapshots=db.execute(
                "SELECT COUNT(*) FROM cap_snapshots WHERE record_count=0"
            ).fetchone()[0],
            indexed_messages=db.execute("SELECT COUNT(*) FROM cap_records").fetchone()[
                0
            ],
            message_types=[
                dict(row)
                for row in db.execute(
                    "SELECT json_extract(record,'$.msgType') AS message_type, COUNT(*) AS records FROM cap_records JOIN requests ON requests.id=cap_records.request_id JOIN jobs ON jobs.id=requests.job_id WHERE json_extract(jobs.definition,'$.kind')='cap_json' GROUP BY message_type"
                )
            ],
            discovery_limit_events=db.execute(
                "SELECT COUNT(*) FROM events WHERE kind IN ('cap_seed_limit','cap_discovery_limit')"
            ).fetchone()[0],
            latest_active_fetch=[
                dict(row)
                for row in db.execute(
                    "SELECT r.id,r.finished,r.status,r.error,r.parser_error,r.provider_status,s.record_count FROM requests r LEFT JOIN cap_snapshots s ON s.request_id=r.id WHERE r.job_id='cap_active' ORDER BY r.id DESC LIMIT 1"
                )
            ],
        )
    db.close()
    return output


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def export_body(directory, request_id):
    root = Path(directory).resolve()
    db = sqlite3.connect(f"file:{root / 'index.sqlite3'}?mode=ro", uri=True)
    row = db.execute(
        "SELECT b.path FROM requests r JOIN blobs b ON b.sha256=r.blob_sha256 WHERE r.id=?",
        (request_id,),
    ).fetchone()
    db.close()
    if row is None:
        raise ValueError("No archived body for that request")
    return gzip.decompress((root / row[0]).read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir")
    parser.add_argument(
        "--body", type=int, help="Print the original response body for a request ID"
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Omit per-station cadence and successful job details",
    )
    args = parser.parse_args()
    if args.body is not None:
        import sys

        sys.stdout.buffer.write(export_body(args.data_dir, args.body))
    else:
        result = report(args.data_dir)
        if args.summary:
            result.pop("observed_cadence")
            result["jobs_with_errors"] = [
                job
                for job in result.pop("jobs")
                if job["http_ok"] != job["requests"]
                or job["parser_errors"]
                or job["provider_errors"]
            ]
        print(json.dumps(result, indent=2))
