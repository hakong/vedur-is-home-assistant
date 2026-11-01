"""CAP research lifecycle, archive isolation and request budget contracts."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from research import cap
from research.collector import Archive, request_url, successful_status
from research.plan import make_config
from research.report import export_body, report

PLAN = dict(
    schema_version=1,
    seed_start="2026-10-18",
    seed_end="2026-11-01",
    seed_limit=100,
    maximum_versions=2000,
)
MESSAGE = dict(
    sender="IMO-Icelandic_Met_Office",
    identifier="test-update",
    sent="2026-11-01T12:00:00Z",
    status="Actual",
    msgType="Update",
    scope="Public",
    references="IMO-Icelandic_Met_Office,test-original,2026-11-01T10:00:00Z",
    info=[
        dict(
            language="is-IS",
            onset="2026-11-01T15:00:00Z",
            expires="2026-11-02T01:00:00Z",
            severity="Severe",
            area=[dict(polygon="64,-22 65,-22 65,-21 64,-22")],
        ),
        dict(language="en-US", description="Example warning"),
    ],
)


class CapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = make_config([{"station_id": 1474, "type": "sj"}])
        self.config["jobs"] = self.config["jobs"][:1]
        self.archive = Archive(self.root, self.config)
        self.before = tuple(self.archive.db.execute("SELECT * FROM jobs").fetchone())
        self.meta_before = dict(self.archive.db.execute("SELECT key,value FROM meta"))
        cap.install(self.archive, PLAN)

    def tearDown(self):
        self.archive.close()
        self.temp.cleanup()

    def attempt(self, job_id, payload, status=200):
        row = self.archive.db.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        job = json.loads(row["definition"])
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        now = time.time()
        cursor = self.archive.db.execute(
            "INSERT INTO requests(job_id,scheduled_at,started,url) VALUES (?,?,?,?)",
            (job_id, now, now, job["url"]),
        )
        result = dict(
            body=body,
            status=status,
            started=now,
            finished=now + 1,
            elapsed=1,
            url=job["url"],
            final_url=job["url"],
            headers={},
            error=None,
            truncated=False,
        )
        error = self.archive.record(cursor.lastrowid, job, result)
        self.archive.reschedule(row, job, result, error)
        return cursor.lastrowid, error

    def message_jobs(self):
        return self.archive.db.execute(
            "SELECT * FROM jobs WHERE json_extract(definition,'$.kind') IN ('cap_json','cap_xml')"
        ).fetchall()

    def test_additive_install_is_idempotent_and_preserves_weather(self):
        cap.install(self.archive, PLAN)
        self.assertEqual(
            len(self.archive.db.execute("SELECT * FROM jobs").fetchall()), 7
        )
        after = tuple(
            self.archive.db.execute(
                "SELECT * FROM jobs WHERE id=?", (self.config["jobs"][0]["id"],)
            ).fetchone()
        )
        self.assertEqual(self.before, after)
        meta = dict(self.archive.db.execute("SELECT key,value FROM meta"))
        for key in ("config", "config_sha256", "started", "stop_at"):
            self.assertEqual(meta[key], self.meta_before[key])
        with self.assertRaises(ValueError):
            cap.install(self.archive, {**PLAN, "seed_limit": 99})

    def test_empty_204_and_empty_array_are_success_not_failure(self):
        for payload, status in ((b"", 204), ([], 200)):
            _, error = self.attempt("cap_active", payload, status)
            self.assertIsNone(error)
        result = report(self.root)
        self.assertEqual(result["cap"]["empty_snapshots"], 2)
        self.assertEqual(result["stations_indexed"], 0)
        self.assertFalse(successful_status({"kind": "cap_json"}, 204))
        self.assertFalse(successful_status({"kind": "official_observation"}, 204))

    def test_null_malformed_or_error_is_not_empty_snapshot(self):
        for payload, status in (
            (None, 200),
            (b"<html>broken</html>", 200),
            (b"", 503),
            (b"nonempty", 204),
        ):
            self.attempt("cap_active", payload, status)
        self.assertEqual(report(self.root)["cap"]["successful_snapshots"], 0)

    def test_both_formats_once_and_duplicate_indexes_do_not_reset_jobs(self):
        self.attempt("cap_active", [MESSAGE])
        rows = self.message_jobs()
        self.assertEqual(
            len(rows), 4
        )  # Current message and its referenced predecessor.
        first = rows[0]
        self.attempt("cap_active", [MESSAGE])
        self.assertEqual(len(self.message_jobs()), 4)
        self.assertEqual(
            self.archive.db.execute(
                "SELECT next_due FROM jobs WHERE id=?", (first["id"],)
            ).fetchone()[0],
            first["next_due"],
        )

    def test_full_json_preserves_languages_areas_times_and_references(self):
        self.attempt("cap_active", [MESSAGE])
        item = next(
            row
            for row in self.message_jobs()
            if json.loads(row["definition"])["kind"] == "cap_json"
            and json.loads(row["definition"])["message"]["identifier"]
            == MESSAGE["identifier"]
        )
        request, error = self.attempt(item["id"], {"alert": MESSAGE})
        self.assertIsNone(error)
        body = json.loads(export_body(self.root, request))
        self.assertEqual(body["alert"], MESSAGE)
        due = self.archive.db.execute(
            "SELECT next_due FROM jobs WHERE id=?", (item["id"],)
        ).fetchone()[0]
        self.assertGreater(due, float(self.meta_before["stop_at"]))
        self.archive.close()
        self.archive = Archive(self.root, self.config)
        cap.install(self.archive, PLAN)
        self.assertEqual(
            self.archive.db.execute(
                "SELECT next_due FROM jobs WHERE id=?", (item["id"],)
            ).fetchone()[0],
            due,
        )

    def test_failed_detail_is_retried_and_identity_mismatch_rejected(self):
        self.attempt("cap_active", [MESSAGE])
        item = next(
            row
            for row in self.message_jobs()
            if json.loads(row["definition"])["kind"] == "cap_json"
        )
        _, error = self.attempt(
            item["id"], {"alert": {**MESSAGE, "identifier": "wrong"}}
        )
        self.assertIsNotNone(error)
        self.assertLess(
            self.archive.db.execute(
                "SELECT next_due FROM jobs WHERE id=?", (item["id"],)
            ).fetchone()[0],
            float(self.meta_before["stop_at"]),
        )

    def test_cancel_test_expired_and_unknown_fields_are_retained(self):
        message = {
            **MESSAGE,
            "msgType": "Cancel",
            "status": "Test",
            "info": [],
            "futureField": "keep",
        }
        records, refs = cap.parse(
            {"kind": "cap_json"}, json.dumps({"alert": message}).encode()
        )
        self.assertEqual(records[0], message)
        self.assertEqual(refs[0]["identifier"], "test-original")

    def test_cap_xml_namespace_repeated_info_and_cancel(self):
        body = b'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><sender>IMO</sender><identifier>cancel</identifier><sent>2026-11-01T10:00:00Z</sent><status>Actual</status><msgType>Cancel</msgType><scope>Public</scope><info><language>is-IS</language></info><info><language>en-US</language></info></alert>'
        records, _ = cap.parse({"kind": "cap_xml"}, body)
        self.assertEqual(len(records[0]["info"]), 2)
        self.assertEqual(records[0]["msgType"], "Cancel")
        with self.assertRaises(ValueError):
            cap.parse({"kind": "cap_xml"}, b"<alert/>")

    def test_rolling_history_changes_date_at_utc_midnight(self):
        item = next(j for j in cap.initial_jobs(PLAN) if j["id"] == "cap_recent")
        now = datetime(2026, 11, 2, 0, 1, tzinfo=timezone.utc).timestamp()
        self.assertIn("from/2026/10/31/to/2026/11/2", request_url(item, now))

    def test_seed_is_bounded_but_raw_index_retained(self):
        item = next(j for j in cap.initial_jobs(PLAN) if j["id"] == "cap_seed")
        item["discovery_limit"] = 1
        self.archive.db.execute(
            "UPDATE jobs SET definition=? WHERE id='cap_seed'", (json.dumps(item),)
        )
        older = {key: MESSAGE[key] for key in ("sender", "identifier", "sent")}
        newer = {**older, "identifier": "newer", "sent": "2026-11-02T00:00:00Z"}
        request, error = self.attempt("cap_seed", [older, newer])
        self.assertIsNone(error)
        self.assertEqual(len(self.message_jobs()), 2)
        self.assertEqual(json.loads(export_body(self.root, request)), [older, newer])
        self.assertEqual(report(self.root)["cap"]["discovery_limit_events"], 1)

    def test_malformed_reference_does_not_partially_index(self):
        _, error = self.attempt("cap_active", [MESSAGE, {"identifier": "incomplete"}])
        self.assertIsNotNone(error)
        self.assertEqual(len(self.message_jobs()), 0)
        self.assertEqual(report(self.root)["cap"]["successful_snapshots"], 0)

    def test_equivalent_sent_time_deduplicates_and_paths_are_escaped(self):
        item = {
            "sender": "IMO / sender",
            "identifier": "a/b?x=y",
            "sent": MESSAGE["sent"],
        }
        self.attempt(
            "cap_active", [item, {**item, "sent": "2026-11-01T12:00:00+00:00"}]
        )
        self.assertEqual(len(self.message_jobs()), 2)
        for row in self.message_jobs():
            self.assertIn("a%2Fb%3Fx%3Dy", json.loads(row["definition"])["url"])

    def test_version_limit_is_visible_and_does_not_discard_index(self):
        self.archive.db.execute(
            "UPDATE meta SET value=? WHERE key='cap_plan'",
            (json.dumps({**PLAN, "maximum_versions": 1}),),
        )
        self.attempt("cap_active", [MESSAGE])
        self.assertEqual(len(self.message_jobs()), 2)
        self.assertEqual(report(self.root)["cap"]["discovery_limit_events"], 1)
        self.assertEqual(report(self.root)["cap"]["indexed_messages"], 1)

    def test_cap_rate_limit_uses_the_shared_official_host_backoff(self):
        self.attempt("cap_active", b"", 429)
        host = self.archive.db.execute(
            "SELECT * FROM hosts WHERE host='api.vedur.is'"
        ).fetchone()
        self.assertEqual(host["factor"], 2)
        self.assertGreater(host["blocked_until"], time.time() + 800)
