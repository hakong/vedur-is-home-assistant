"""Archive, scheduling, missing-data and recovery contracts."""

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from research.collector import (
    Archive,
    download,
    load_config,
    next_slot,
    parse_body,
    request_url,
    retry_after,
    valid_url,
)
from research.indexing import extract, normalize_time
from research.plan import make_config
from research.report import export_body, report


def config():
    result = make_config([{"station_id": 1474, "type": "sj"}])
    result["jobs"] = [
        next(job for job in result["jobs"] if job["id"] == "gott_latest_1474")
    ]
    result["minimum_free_bytes"] = 0
    return result


class TimingTests(unittest.TestCase):
    def test_next_slot_keeps_phase_without_catchup(self):
        self.assertEqual(next_slot(100, 300, 102), 400)
        self.assertEqual(next_slot(100, 300, 1001), 1300)
        self.assertEqual(next_slot(100, 300, 400), 700)

    def test_retry_after_seconds_and_http_date(self):
        self.assertEqual(retry_after("120", 0), 120)
        self.assertEqual(retry_after("Thu, 01 Jan 1970 00:02:00 GMT", 0), 120)
        self.assertEqual(retry_after("nonsense", 0), 0)

    def test_provider_url_allowlist(self):
        self.assertTrue(valid_url("https://api.vedur.is/weather/stations"))
        for bad in (
            "http://api.vedur.is/",
            "https://example.org/",
            "https://user:pass@api.vedur.is/",
            "https://api.vedur.is:8443/",
        ):
            self.assertFalse(valid_url(bad))

    def test_detail_hour_rollover(self):
        job = {
            "url": "https://www.gottvedur.is/api/observations/detailsByHour/?stationId=1474",
            "hour_offset": 1,
        }
        epoch = datetime(2026, 11, 1, 23, 50, tzinfo=timezone.utc).timestamp()
        self.assertIn("2026-11-02T00%3A00%3A00", request_url(job, epoch))

    def test_time_normalization_preserves_offsets(self):
        self.assertEqual(
            normalize_time("2026-11-01T12:00:00+02:00"), "2026-11-01T10:00:00+00:00"
        )
        self.assertIsNone(normalize_time("?"))


class ExtractionTests(unittest.TestCase):
    def test_captured_provider_shapes(self):
        fixtures = json.loads(
            (
                Path(__file__).parent / "fixtures/provider-excerpts-20261101.json"
            ).read_text()
        )
        for kind, payload in fixtures.items():
            with self.subTest(kind=kind):
                body = (
                    payload.encode()
                    if isinstance(payload, str)
                    else json.dumps(payload).encode()
                )
                records = extract({"kind": kind, "station_ids": [1474]}, body)
                self.assertTrue(records)
                self.assertTrue(all(row[3]["first_time_utc"] for row in records))

    def test_batch_observation_uses_its_own_timestamp_name(self):
        payload = [
            {
                "stationId": 1474,
                "lastObservationTime": "2026-11-01T22:00:00",
                "precipitation": None,
            }
        ]
        row = extract({"kind": "gott_batch_observation"}, json.dumps(payload).encode())[
            0
        ]
        self.assertEqual(row[2], "2026-11-01T22:00:00+00:00")
        self.assertIsNone(row[3]["record"]["precipitation"])

    def test_zero_null_empty_missing_are_different(self):
        job = config()["jobs"][0]
        body = b'{"station":1474,"observationTime":"2026-11-01T10:00:00","temperature":0,"humidity":null,"precipitation":""}'
        _, raw, stamp, result = extract(job, body)[0]
        self.assertEqual(raw, "2026-11-01T10:00:00")
        self.assertEqual(stamp, raw + "+00:00")
        self.assertEqual(result["record"]["temperature"], 0)
        self.assertEqual(result["fields"]["humidity"], {"null": 1})
        self.assertEqual(result["fields"]["precipitation"], {"empty": 1})
        self.assertNotIn("windSpeed", result["fields"])

    def test_forecast_interval_values_are_not_converted(self):
        job = {"kind": "gott_forecast", "station_ids": [1474]}
        body = b'{"nextDays":[{"time":"2026-11-01T12:00:00","precipitation":6,"model":"ecm-is"}]}'
        result = extract(job, body)[0][3]
        self.assertEqual(result["record"]["precipitation"], 6)
        self.assertEqual(result["record"]["model"], "ecm-is")

    def test_xml_error_and_empty_field_are_retained(self):
        body = b'<observations><station id="1474"><time>2026-11-01 10:00:00</time><T>0</T><R/></station><station id="3470"><err>No data</err></station></observations>'
        records = extract({"kind": "xml_obs"}, body)
        self.assertEqual(records[0][3]["record"]["T"], "0")
        self.assertIsNone(records[0][3]["record"]["R"])
        self.assertEqual(records[1][3]["provider_error"], "No data")

    def test_unknown_schema_fails_without_guessing(self):
        with self.assertRaises(ValueError):
            extract(config()["jobs"][0], b'{"new_payload":{}}')

    def test_encoded_body_size_is_bounded(self):
        packed = gzip.compress(b"x" * 10000)
        result = {"headers": {"content-encoding": "gzip"}, "body": packed}
        with self.assertRaises(ValueError):
            parse_body(result, 100)
        self.assertEqual(parse_body(result, 20000), b"x" * 10000)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = config()
        self.archive = Archive(self.root, self.config)
        self.job = self.config["jobs"][0]

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def record(
        self,
        body=b'{"station":1474,"observationTime":"2026-11-01T10:00:00","temperature":0}',
        status=200,
        headers=None,
    ):
        now = time.time()
        cursor = self.archive.db.execute(
            "INSERT INTO requests(job_id,scheduled_at,started,url) VALUES (?,?,?,?)",
            (self.job["id"], now, now, self.job["url"]),
        )
        result = dict(
            started=now,
            finished=now + 1,
            elapsed=1,
            url=self.job["url"],
            final_url=self.job["url"],
            status=status,
            headers=headers or {},
            body=body,
            error=None,
            truncated=False,
        )
        error = self.archive.record(cursor.lastrowid, self.job, result)
        return cursor.lastrowid, result, error

    def test_identical_bodies_keep_every_request_but_one_blob(self):
        first, _, _ = self.record()
        self.record()
        self.assertEqual(
            self.archive.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 2
        )
        self.assertEqual(
            self.archive.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0], 1
        )
        self.assertIn(b'"temperature":0', export_body(self.root, first))
        self.assertEqual(report(self.root)["requests"], 2)

    def test_unknown_and_http_error_bodies_survive(self):
        request, _, error = self.record(b'{"schema_changed":true}')
        self.assertIsNotNone(error)
        self.assertEqual(export_body(self.root, request), b'{"schema_changed":true}')
        request, _, error = self.record(b"<html>Bad gateway</html>", 502)
        self.assertIsNone(error)
        self.assertEqual(export_body(self.root, request), b"<html>Bad gateway</html>")

    def test_rate_limit_blocks_host_and_reduces_frequency(self):
        _, result, error = self.record(status=429, headers={"retry-after": "7200"})
        row = self.archive.db.execute("SELECT * FROM jobs").fetchone()
        self.archive.reschedule(row, self.job, result, error)
        host = self.archive.db.execute("SELECT * FROM hosts").fetchone()
        self.assertEqual(host["factor"], 2)
        self.assertEqual(host["blocked_until"], result["finished"] + 7200)

    def test_missing_endpoint_backs_off_only_that_job(self):
        _, result, error = self.record(status=404)
        row = self.archive.db.execute("SELECT * FROM jobs").fetchone()
        self.archive.reschedule(row, self.job, result, error)
        self.assertGreaterEqual(
            self.archive.db.execute("SELECT next_due FROM jobs").fetchone()[0],
            result["finished"] + 21600,
        )
        self.assertEqual(
            self.archive.db.execute("SELECT blocked_until FROM hosts").fetchone()[0], 0
        )

    def test_http_200_problem_is_distinct_from_parser_failure(self):
        body = b'{"type":"https://tools.ietf.org/html/rfc9110#section-15.5.5","title":"Not Found","status":404,"detail":"No observations found for station","Context":{"stationId":3470}}'
        request, result, error = self.record(body)
        self.assertIsNone(error)
        row = self.archive.db.execute(
            "SELECT * FROM requests WHERE id=?", (request,)
        ).fetchone()
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["provider_status"], 404)
        self.assertEqual(row["provider_error"], "No observations found for station")
        self.assertEqual(export_body(self.root, request), body)
        job_row = self.archive.db.execute("SELECT * FROM jobs").fetchone()
        self.archive.reschedule(job_row, self.job, result, error)
        self.assertLessEqual(
            self.archive.db.execute("SELECT next_due FROM jobs").fetchone()[0],
            result["finished"] + self.job["interval"],
        )

    def test_repeated_missing_observations_keep_cadence_and_all_evidence(self):
        self.job["kind"] = "detail"
        self.job["interval"] = 900
        body = b'{"title":"Not Found","type":"about:blank","status":404,"detail":"No observations found for station"}'
        for _ in range(5):
            row = self.archive.db.execute("SELECT * FROM jobs").fetchone()
            with patch("time.time", return_value=max(time.time(), row["next_due"])):
                request, result, error = self.record(body)
            self.archive.reschedule(row, self.job, result, error)
            next_due = self.archive.db.execute("SELECT next_due FROM jobs").fetchone()[
                0
            ]
            self.assertLessEqual(next_due, result["finished"] + 900)
            self.assertEqual(export_body(self.root, request), body)
        self.assertEqual(
            self.archive.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 5
        )
        self.assertEqual(
            self.archive.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0
        )
        self.assertEqual(
            self.archive.db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='observation_missing'"
            ).fetchone()[0],
            5,
        )
        self.assertEqual(
            tuple(
                self.archive.db.execute(
                    "SELECT factor,blocked_until FROM hosts"
                ).fetchone()
            ),
            (1, 0),
        )

    def test_no_data_respects_explicit_retry_after(self):
        _, result, error = self.record(
            b'{"title":"Not Found","type":"about:blank","status":404,"detail":"No observations found for station"}',
            headers={"retry-after": "7200"},
        )
        self.archive.reschedule(
            self.archive.db.execute("SELECT * FROM jobs").fetchone(),
            self.job,
            result,
            error,
        )
        self.assertGreaterEqual(
            self.archive.db.execute("SELECT next_due FROM jobs").fetchone()[0],
            result["finished"] + 7200,
        )

    def test_other_problem_404_still_backs_off(self):
        _, result, error = self.record(
            b'{"title":"Not Found","type":"about:blank","status":404,"detail":"Unknown endpoint"}'
        )
        self.archive.reschedule(
            self.archive.db.execute("SELECT * FROM jobs").fetchone(),
            self.job,
            result,
            error,
        )
        self.assertGreaterEqual(
            self.archive.db.execute("SELECT next_due FROM jobs").fetchone()[0],
            result["finished"] + 21600,
        )

    def test_legacy_no_data_delay_is_released_once_with_audit_event(self):
        request, result, _ = self.record(
            b'{"title":"Not Found","type":"about:blank","status":404,"detail":"No observations found for station"}'
        )
        original = dict(self.archive.db.execute("SELECT key,value FROM meta"))
        old_due = time.time() + 21600
        self.archive.db.execute("UPDATE jobs SET next_due=?,failures=2", (old_due,))
        self.archive.db.execute(
            "DELETE FROM meta WHERE key='observation_no_data_normal_cadence_v1'"
        )
        self.archive.db.commit()
        before = dict(
            self.archive.db.execute(
                "SELECT * FROM requests WHERE id=?", (request,)
            ).fetchone()
        )
        self.archive.close()
        self.archive = Archive(self.root, self.config)
        row = self.archive.db.execute("SELECT * FROM jobs").fetchone()
        self.assertLess(row["next_due"], time.time() + 10)
        event = json.loads(
            self.archive.db.execute(
                "SELECT detail FROM events WHERE kind='observation_sampling_policy' ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        self.assertEqual(event["affected_jobs"][0]["previous_next_due"], old_due)
        self.assertEqual(
            before,
            dict(
                self.archive.db.execute(
                    "SELECT * FROM requests WHERE id=?", (request,)
                ).fetchone()
            ),
        )
        self.assertEqual(
            dict(self.archive.db.execute("SELECT key,value FROM meta"))["stop_at"],
            original["stop_at"],
        )
        self.archive.close()
        self.archive = Archive(self.root, self.config)
        self.assertEqual(
            self.archive.db.execute("SELECT next_due FROM jobs").fetchone()[0],
            row["next_due"],
        )

    def test_embedded_rate_limit_also_slows_host(self):
        _, result, error = self.record(
            b'{"title":"Too Many Requests","status":429,"detail":"Retry later"}'
        )
        self.archive.reschedule(
            self.archive.db.execute("SELECT * FROM jobs").fetchone(),
            self.job,
            result,
            error,
        )
        self.assertEqual(
            self.archive.db.execute("SELECT factor FROM hosts").fetchone()[0], 2
        )

    def test_additive_index_migration_preserves_existing_archive(self):
        self.record()
        self.archive.db.execute("ALTER TABLE requests DROP COLUMN provider_status")
        self.archive.db.execute("ALTER TABLE requests DROP COLUMN provider_error")
        self.archive.db.commit()
        self.archive.close()
        self.archive = Archive(self.root, self.config)
        row = self.archive.db.execute(
            "SELECT status,provider_status FROM requests"
        ).fetchone()
        self.assertEqual(tuple(row), (200, None))

    def test_request_failures_do_not_change_measurement_timestamp(self):
        self.record()
        self.record(b"", status=503)
        rows = self.archive.db.execute("SELECT time_raw FROM samples").fetchall()
        self.assertEqual([row[0] for row in rows], ["2026-11-01T10:00:00"])

    def test_restarts_preserve_campaign_and_deadlines(self):
        original = dict(self.archive.db.execute("SELECT key,value FROM meta"))
        due = self.archive.db.execute("SELECT next_due FROM jobs").fetchone()[0]
        self.archive.close()
        self.archive = Archive(self.root, self.config)
        meta = dict(self.archive.db.execute("SELECT key,value FROM meta"))
        self.assertEqual(meta["stop_at"], original["stop_at"])
        self.assertEqual(
            self.archive.db.execute("SELECT next_due FROM jobs").fetchone()[0], due
        )

    def test_incomplete_request_is_recorded_on_restart(self):
        self.archive.db.execute(
            "INSERT INTO requests(job_id,scheduled_at,started,url) VALUES (?,?,?,?)",
            (self.job["id"], 0, 0, self.job["url"]),
        )
        self.archive.db.commit()
        self.archive.close()
        self.archive = Archive(self.root, self.config)
        self.assertIn(
            "interrupted",
            self.archive.db.execute("SELECT error FROM requests").fetchone()[0],
        )

    def test_lock_prevents_two_collectors(self):
        with self.assertRaises(RuntimeError):
            Archive(self.root, self.config)

    def test_space_guard_keeps_existing_data(self):
        self.record()
        self.config["maximum_archive_bytes"] = 1
        with self.assertRaises(RuntimeError):
            self.archive.check_space()
        self.assertEqual(
            self.archive.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1
        )

    def test_future_timestamps_and_out_of_range_values_remain_raw(self):
        self.record(
            b'{"station":1474,"observationTime":"2140-01-01T00:00:00","humidity":101}'
        )
        row = self.archive.db.execute("SELECT time_raw,summary FROM samples").fetchone()
        self.assertEqual(row["time_raw"], "2140-01-01T00:00:00")
        self.assertEqual(json.loads(row["summary"])["record"]["humidity"], 101)

    def test_server_backoff_survives_restart(self):
        _, result, error = self.record(status=503, headers={"retry-after": "600"})
        row = self.archive.db.execute("SELECT * FROM jobs").fetchone()
        self.archive.reschedule(row, self.job, result, error)
        expected = self.archive.db.execute(
            "SELECT blocked_until FROM hosts"
        ).fetchone()[0]
        self.archive.close()
        self.archive = Archive(self.root, self.config)
        self.assertEqual(
            self.archive.db.execute("SELECT blocked_until FROM hosts").fetchone()[0],
            expected,
        )

    def test_completed_campaign_makes_no_more_requests(self):
        self.archive.db.execute("UPDATE meta SET value='0' WHERE key='stop_at'")
        self.archive.db.commit()
        with patch("research.collector.download") as fetch:
            self.archive.run()
            fetch.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_download_preserves_body_and_honours_bound(self):
        import io

        response = io.BytesIO(b"123456")
        response.code = 200
        response.url = "https://www.gottvedur.is/"
        response.headers = {"Content-Type": "application/json", "Set-Cookie": "private"}
        options = config()
        options["maximum_response_bytes"] = 5
        with patch("research.collector.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = download(options["jobs"][0], options)
        self.assertEqual(result["body"], b"12345")
        self.assertTrue(result["truncated"])
        self.assertNotIn("set-cookie", result["headers"])

    def test_connection_error_is_not_a_zero_observation(self):
        from urllib.error import URLError

        options = config()
        with patch("research.collector.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("offline")
            result = download(options["jobs"][0], options)
        self.assertIsNone(result["status"])
        self.assertEqual(result["body"], b"")
        self.assertIn("offline", result["error"])


class PlanTests(unittest.TestCase):
    def test_selected_station_coverage_and_request_limits(self):
        stations = json.loads(
            (Path(__file__).parents[1] / "research/stations.json").read_text()
        )
        self.assertEqual(len(stations), 40)
        self.assertEqual(sum(s["type"] != "virtual" for s in stations), 36)
        planned = make_config(stations)
        self.assertEqual(
            len({job["id"] for job in planned["jobs"]}), len(planned["jobs"])
        )
        self.assertEqual(
            len([j for j in planned["jobs"] if j["kind"] == "gott_observation"]), 36
        )
        self.assertEqual(
            len([j for j in planned["jobs"] if j["kind"] == "gott_forecast"]), 40
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(planned))
            self.assertEqual(load_config(path), planned)


if __name__ == "__main__":
    unittest.main()
