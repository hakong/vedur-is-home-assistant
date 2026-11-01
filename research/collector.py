"""Bounded, restartable API collector using only the Python standard library."""

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import fcntl
import gzip
import hashlib
import json
import logging
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zlib

from .indexing import ProviderError, extract

LOG = logging.getLogger("vedur-research")
HOSTS = {"api.vedur.is", "xmlweather.vedur.is", "gottvedur.is", "www.gottvedur.is"}
HEADERS = {
    "date",
    "age",
    "cache-control",
    "etag",
    "last-modified",
    "retry-after",
    "content-type",
    "content-encoding",
    "content-length",
    "server",
    "via",
    "x-cache",
    "x-vi-api-version",
    "location",
}
STOP = False


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def valid_url(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in HOSTS
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
    )


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        if not valid_url(newurl):
            raise ValueError("Redirect outside configured HTTPS weather providers")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def retry_after(value, now):
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0.0


def next_slot(due, interval, now):
    """Keep a job's phase without replaying missed requests after downtime."""
    return due + max(1, int((now - due) // interval) + 1) * interval


def request_url(job, now):
    if job.get("hour_offset") is None:
        return job["url"]
    hour = datetime.fromtimestamp(now, timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    hour += timedelta(hours=job["hour_offset"])
    return job["url"] + "&" + urlencode({"hour": hour.strftime("%Y-%m-%dT%H:%M:%S")})


def download(job, config):
    started = time.time()
    monotonic_start = time.monotonic()
    url = request_url(job, started)
    result = dict(
        started=started,
        finished=started,
        elapsed=0,
        url=url,
        final_url=url,
        status=None,
        headers={},
        body=b"",
        error=None,
        truncated=False,
    )
    headers = {"User-Agent": config["user_agent"], "Accept-Encoding": "identity"}
    payload = None
    if job.get("body") is not None:
        payload = json.dumps(job["body"]).encode()
        headers["Content-Type"] = "application/json"
    request = Request(url, data=payload, headers=headers)
    limit = config["maximum_response_bytes"]
    try:
        try:
            response = build_opener(SafeRedirect()).open(
                request, timeout=config["timeout_seconds"]
            )
        except HTTPError as error:
            response = error
        with response:
            result.update(
                status=response.code,
                final_url=response.url,
                headers={
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in HEADERS
                },
            )
            chunks = bytearray()
            size = 0
            while size <= limit:
                chunk = response.read(min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.extend(chunk)
                size += len(chunk)
                result["body"] = bytes(chunks)
                if time.monotonic() - monotonic_start > 45:
                    raise TimeoutError("Response exceeded total read budget")
            result["body"] = bytes(chunks[:limit])
            result["truncated"] = size > limit
            if result["truncated"]:
                result["error"] = "Response exceeds configured size limit"
    except (URLError, OSError, ValueError, TimeoutError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    result.update(finished=time.time(), elapsed=time.monotonic() - monotonic_start)
    return result


def parse_body(result, limit):
    body = result["body"]
    encoding = result["headers"].get("content-encoding", "").lower()
    if encoding in {"gzip", "deflate"}:
        decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
        body = decoder.decompress(body, limit + 1)
        if len(body) > limit or not decoder.eof:
            raise ValueError("Encoded response exceeds limit or is incomplete")
    elif encoding not in {"", "identity"}:
        raise ValueError("Unsupported content encoding: " + encoding)
    return body


class Archive:
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / "collector.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another collector already owns this archive") from None
        self.db = sqlite3.connect(self.directory / "index.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, definition TEXT NOT NULL, next_due REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY, blocked_until REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0, factor INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS blobs(sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, raw_bytes INTEGER NOT NULL, stored_bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), scheduled_at REAL NOT NULL, started REAL NOT NULL, finished REAL, elapsed REAL, url TEXT NOT NULL, final_url TEXT, status INTEGER, headers TEXT, blob_sha256 TEXT REFERENCES blobs(sha256), body_bytes INTEGER, error TEXT, parser_error TEXT, truncated INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS requests_job_time ON requests(job_id, started);
            CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id), station_id TEXT NOT NULL, time_raw TEXT, time_utc TEXT, summary TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_station_time ON samples(station_id,time_utc);
            CREATE INDEX IF NOT EXISTS samples_request ON samples(request_id);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(requests)")}
        for name, kind in (("provider_status", "INTEGER"), ("provider_error", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE requests ADD COLUMN {name} {kind}")
        self.config = config
        encoded = json.dumps(config, sort_keys=True).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        previous = self.db.execute(
            "SELECT value FROM meta WHERE key='config_sha256'"
        ).fetchone()
        if previous and previous[0] != digest:
            self.close()
            raise ValueError("Configuration changed: use a new campaign directory")
        if not previous:
            start = time.time()
            self.db.executemany(
                "INSERT INTO meta VALUES (?,?)",
                [
                    ("schema_version", "1"),
                    ("config_sha256", digest),
                    ("config", encoded.decode()),
                    ("started", str(start)),
                    ("stop_at", str(start + config["duration_days"] * 86400)),
                ],
            )
            # Cold start gathers one pass, spaced globally; subsequent slots
            # retain each job's phase. Restarts reuse these persisted deadlines.
            for index, job in enumerate(config["jobs"]):
                self.db.execute(
                    "INSERT INTO jobs(id,definition,next_due) VALUES (?,?,?)",
                    (
                        job["id"],
                        json.dumps(job),
                        start + index * config["minimum_request_gap_seconds"],
                    ),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO hosts(host) VALUES (?)",
                    (urlsplit(job["url"]).hostname,),
                )
        self.db.execute(
            "UPDATE requests SET error='Collector interrupted before completion',finished=? WHERE finished IS NULL",
            (time.time(),),
        )
        self.event(
            "start",
            {
                "code_sha256": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in Path(__file__).parent.glob("*.py")
                }
            },
        )
        self.db.commit()

    def event(self, kind, detail):
        self.db.execute(
            "INSERT INTO events(at,kind,detail) VALUES (?,?,?)",
            (time.time(), kind, json.dumps(detail)),
        )

    def close(self):
        self.db.close()
        self.lock.close()

    def check_space(self):
        blobs = self.db.execute(
            "SELECT COALESCE(SUM(stored_bytes),0) FROM blobs"
        ).fetchone()[0]
        database = sum(p.stat().st_size for p in self.directory.glob("index.sqlite3*"))
        if blobs + database >= self.config["maximum_archive_bytes"]:
            raise RuntimeError("Archive size limit reached; existing data retained")
        if shutil.disk_usage(self.directory).free < self.config["minimum_free_bytes"]:
            raise RuntimeError("Free-space floor reached; existing data retained")

    def store_blob(self, body):
        digest = hashlib.sha256(body).hexdigest()
        if not self.db.execute(
            "SELECT 1 FROM blobs WHERE sha256=?", (digest,)
        ).fetchone():
            relative = Path("responses") / digest[:2] / (digest + ".bin.gz")
            path = self.directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            compressed = gzip.compress(body, mtime=0)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(compressed)
            temporary.replace(path)
            self.db.execute(
                "INSERT INTO blobs VALUES (?,?,?,?)",
                (digest, str(relative), len(body), len(compressed)),
            )
        return digest

    def record(self, request_id, job, result):
        digest = self.store_blob(result["body"])
        parser_error = None
        result["provider_status"] = None
        result["provider_error"] = None
        samples = []
        if result["status"] == 200 and not result["error"]:
            try:
                samples = extract(
                    job, parse_body(result, self.config["maximum_response_bytes"])
                )
            except ProviderError as error:
                result["provider_status"] = error.status
                result["provider_error"] = str(error)
            except Exception as error:
                parser_error = f"{type(error).__name__}: {error}"
        self.db.execute(
            """UPDATE requests SET started=?,finished=?,elapsed=?,url=?,final_url=?,status=?,headers=?,blob_sha256=?,body_bytes=?,error=?,parser_error=?,truncated=? WHERE id=?""",
            (
                result["started"],
                result["finished"],
                result["elapsed"],
                result["url"],
                result["final_url"],
                result["status"],
                json.dumps(result["headers"]),
                digest,
                len(result["body"]),
                result["error"],
                parser_error,
                result["truncated"],
                request_id,
            ),
        )
        for sid, raw_time, normalized, details in samples:
            self.db.execute(
                "INSERT INTO samples(request_id,station_id,time_raw,time_utc,summary) VALUES (?,?,?,?,?)",
                (
                    request_id,
                    sid,
                    None if raw_time is None else str(raw_time),
                    normalized,
                    json.dumps(details, ensure_ascii=False),
                ),
            )
        self.db.execute(
            "UPDATE requests SET provider_status=?,provider_error=? WHERE id=?",
            (result["provider_status"], result["provider_error"], request_id),
        )
        self.db.commit()
        return parser_error

    def reschedule(self, row, job, result, parser_error):
        now = result["finished"]
        host = urlsplit(job["url"]).hostname
        host_row = self.db.execute(
            "SELECT * FROM hosts WHERE host=?", (host,)
        ).fetchone()
        status = result.get("provider_status") or result["status"]
        failed = status != 200 or result["error"] or parser_error
        failures = row["failures"] + 1 if failed else 0
        factor = host_row["factor"]
        pause = 0
        host_failure = (
            status in {403, 429}
            or (status is not None and status >= 500)
            or result["error"]
        )
        if host_failure:
            count = host_row["failures"] + 1
            pause = max(
                retry_after(result["headers"].get("retry-after"), now),
                min(3600, 60 * 2 ** min(count - 1, 6)),
            )
            if status == 429:
                factor = min(16, factor * 2)
                pause = max(pause, 900)
            if status == 403:
                pause = max(pause, 86400)
            self.db.execute(
                "UPDATE hosts SET blocked_until=?,failures=?,factor=? WHERE host=?",
                (now + pause, count, factor, host),
            )
            self.event(
                "host_backoff",
                {
                    "host": host,
                    "seconds": pause,
                    "status": status,
                    "interval_multiplier": factor,
                },
            )
        elif status == 200 and not result["error"]:
            self.db.execute("UPDATE hosts SET failures=0 WHERE host=?", (host,))
        due = next_slot(row["next_due"], job["interval"] * factor, now)
        if status is not None and 400 <= status < 500 and status not in {403, 408, 429}:
            due = max(due, now + 21600)
        elif failed:
            due = max(due, now + min(3600, 300 * 2 ** min(failures - 1, 4)))
        self.db.execute(
            "UPDATE jobs SET next_due=?,failures=? WHERE id=?",
            (due, failures, row["id"]),
        )
        self.db.commit()

    def run(self, max_requests=None):
        stop_at = float(
            self.db.execute("SELECT value FROM meta WHERE key='stop_at'").fetchone()[0]
        )
        completed = 0
        last_finish = 0.0
        last_heartbeat = 0.0
        while not STOP and time.time() < stop_at:
            now = time.time()
            if now - last_heartbeat >= 60:
                self.check_space()
                self.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('heartbeat',?)", (str(now),)
                )
                self.db.commit()
                last_heartbeat = now
            rows = self.db.execute(
                "SELECT * FROM jobs WHERE next_due<=? ORDER BY next_due,id", (now,)
            ).fetchall()
            ready = None
            for row in rows:
                job = json.loads(row["definition"])
                host = self.db.execute(
                    "SELECT blocked_until FROM hosts WHERE host=?",
                    (urlsplit(job["url"]).hostname,),
                ).fetchone()
                if host[0] <= now:
                    ready = (row, job)
                    break
            if (
                ready is None
                or time.monotonic() - last_finish
                < self.config["minimum_request_gap_seconds"]
            ):
                time.sleep(0.5)
                continue
            row, job = ready
            with self.db:
                cursor = self.db.execute(
                    "INSERT INTO requests(job_id,scheduled_at,started,url) VALUES (?,?,?,?)",
                    (job["id"], row["next_due"], now, request_url(job, now)),
                )
            result = download(job, self.config)
            parser_error = self.record(cursor.lastrowid, job, result)
            self.reschedule(row, job, result, parser_error)
            last_finish = time.monotonic()
            completed += 1
            LOG.info(
                "%s status=%s bytes=%s elapsed=%.2fs error=%s parser=%s provider=%s",
                job["id"],
                result["status"],
                len(result["body"]),
                result["elapsed"],
                result["error"],
                parser_error,
                result.get("provider_status"),
            )
            if max_requests is not None and completed >= max_requests:
                break
        self.event(
            "stop",
            {
                "reason": "signal"
                if STOP
                else "campaign_completed"
                if time.time() >= stop_at
                else "request_limit"
            },
        )
        self.db.commit()


def load_config(path):
    config = json.loads(Path(path).read_text())
    if config.get("schema_version") != 1 or not 1 <= config["duration_days"] <= 30:
        raise ValueError("Unsupported campaign configuration")
    if not config["jobs"] or len({job["id"] for job in config["jobs"]}) != len(
        config["jobs"]
    ):
        raise ValueError("Missing or duplicate jobs")
    if (
        config["minimum_request_gap_seconds"] < 1
        or config["maximum_response_bytes"] > 16 * 1024 * 1024
    ):
        raise ValueError("Unsafe request limits")
    for job in config["jobs"]:
        if not valid_url(job["url"]) or job["interval"] < 60:
            raise ValueError("Unsupported URL or polling interval: " + job["id"])
    return config


def handle_stop(*_):
    global STOP
    STOP = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--max-requests", type=int)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    archive = None
    try:
        archive = Archive(args.data_dir, load_config(args.config))
        archive.run(args.max_requests)
    except (ValueError, RuntimeError) as error:
        LOG.error("Collection stopped: %s", error)
        return 78
    finally:
        if archive is not None:
            archive.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
