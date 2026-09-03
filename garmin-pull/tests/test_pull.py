"""Pure-function tests for garmin_pull.py: no network, no account."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import garmin_pull as gp  # noqa: E402


class TestParseLast(unittest.TestCase):
    def test_units(self):
        self.assertEqual(gp.parse_last("30d"), 30)
        self.assertEqual(gp.parse_last("12w"), 84)
        self.assertEqual(gp.parse_last("6m"), 183)
        self.assertEqual(gp.parse_last("2y"), 730)
        self.assertEqual(gp.parse_last("45"), 45)
        self.assertEqual(gp.parse_last(" 1Y "), 365)

    def test_garbage_raises(self):
        for bad in ("", "x", "30 days", "d", "1.5y"):
            with self.assertRaises(ValueError):
                gp.parse_last(bad)


class TestShapeSummaryExtra(unittest.TestCase):
    def test_fields_and_missing(self):
        out = gp.shape_summary_extra({"bodyBatteryHighestValue": 91, "activeKilocalories": 739})
        self.assertEqual(out["body_battery_high"], 91)
        self.assertEqual(out["active_kcal"], 739)
        self.assertIsNone(out["floors_ascended"])
        self.assertEqual(gp.shape_summary_extra(None), {})


class TestRowsFor(unittest.TestCase):
    def test_rows_use_date_and_drop_calendar_date(self):
        days = [
            {"date": "2026-01-01", "hrv": {"calendar_date": "2026-01-01", "status": "BALANCED"}},
            {"date": "2026-01-02", "error": "boom"},
        ]
        rows = gp.rows_for("hrv", days)
        self.assertEqual(rows, [{"date": "2026-01-01", "status": "BALANCED"}])


class TestMissingSummaries(unittest.TestCase):
    def test_error_does_not_discard_categories_on_disk(self):
        existing = {"date": "2026-01-01", "sleep": {"x": 1}, "error": "boom"}
        self.assertEqual(gp.missing_summaries(existing, {"sleep", "hrv"}), {"hrv"})
        self.assertEqual(gp.missing_summaries(None, {"sleep", "hrv"}), {"sleep", "hrv"})


class _StubSession:
    def __init__(self, script):
        self.script = list(script)  # per fetch: dict to return, or exception
        self.fetched = []

    def fetch_hrv(self, day):
        self.fetched.append(day)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestPullDays(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raw = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_short_retry_after_waits_once_and_continues(self):
        from garmin_client import GarminAPIError
        session = _StubSession([GarminAPIError("HTTP 429", status=429, retry_after=30),
                                {"hrvSummary": {"weeklyAvg": 50}}, {"hrvSummary": {"weeklyAvg": 51}}])
        slept = []
        pulled, skipped, limited, wait = gp.pull_days(session, ["2026-01-01", "2026-01-02"], {"hrv"}, self.raw,
                                                      pause=0, refresh=False, sleep=slept.append)
        self.assertEqual((pulled, skipped, limited, wait), (2, 0, False, None))
        self.assertEqual(slept, [31])
        self.assertEqual(session.fetched, ["2026-01-01", "2026-01-01", "2026-01-02"])

    def test_long_retry_after_stops_and_reports_the_wait(self):
        from garmin_client import GarminAPIError
        session = _StubSession([GarminAPIError("HTTP 429", status=429, retry_after=3600)])
        pulled, skipped, limited, wait = gp.pull_days(session, ["2026-01-01"], {"hrv"}, self.raw,
                                                      pause=0, refresh=False, sleep=lambda _s: None)
        self.assertEqual((pulled, limited, wait), (0, True, 3600))
        self.assertFalse((self.raw / "2026-01-01.json").exists())

    def test_non_429_error_keeps_what_was_on_disk(self):
        from garmin_client import GarminAPIError
        (self.raw / "2026-01-01.json").write_text(json.dumps({"date": "2026-01-01", "sleep": {"x": 1}}))
        session = _StubSession([GarminAPIError("HTTP 500", status=500)])
        gp.pull_days(session, ["2026-01-01"], {"sleep", "hrv"}, self.raw, pause=0, refresh=False,
                     sleep=lambda _s: None)
        data = json.loads((self.raw / "2026-01-01.json").read_text())
        self.assertEqual(data["sleep"], {"x": 1})
        self.assertIn("error", data)
        self.assertEqual(gp.day_state(self.raw, "2026-01-01", {"sleep", "hrv"}, False), "error")
        self.assertEqual(gp.day_state(self.raw, "2026-01-01", {"sleep"}, False), "complete")


class TestDayState(unittest.TestCase):
    def test_detail_only_pull_is_judged_on_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            self.assertEqual(gp.day_state(raw, "2026-01-01", set(), True), "missing")
            day = raw / "detail" / "2026-01-01"
            day.mkdir(parents=True)
            (day / "_index.json").write_text(json.dumps({name: "ok" for name in gp.garmin_detail.ENDPOINTS}))
            self.assertEqual(gp.day_state(raw, "2026-01-01", set(), True), "complete")
            (day / "_index.json").write_text(json.dumps({"heart_rate": "error: HTTP 500"}))
            self.assertEqual(gp.day_state(raw, "2026-01-01", set(), True), "error")


class TestWriteDetailStreams(unittest.TestCase):
    def test_two_days_land_in_sqlite_and_csv_with_counts(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for day, bpm in (("2026-01-01", 50), ("2026-01-02", 55)):
                d = out / "raw" / "detail" / day
                d.mkdir(parents=True)
                (d / "heart_rate.json").write_text(json.dumps({
                    "minHeartRate": bpm, "heartRateValueDescriptors": [{"index": 0, "key": "timestamp"},
                                                                       {"index": 1, "key": "heartrate"}],
                    "heartRateValues": [[1788300000000, bpm], [1788300120000, bpm + 1]]}))
                (d / "stress.json").write_text(json.dumps({"stressValuesArray": [[1788300000000, 1, 2]]}))
            counts, series, drift = gp.write_detail(out, {"csv", "sqlite"})
            self.assertEqual(counts, {"timeline": 4, "snapshots": 2})
            self.assertEqual(series, {"heart_rate_bpm": 4})
            self.assertEqual(len(drift["unmapped"]), 1)
            self.assertIn("stress.stressValuesArray", drift["unmapped"][0])
            db = sqlite3.connect(out / "garmin.db")
            self.assertEqual(db.execute("select count(*) from timeline").fetchone()[0], 4)
            self.assertEqual(db.execute("select value from snapshots where date='2026-01-02'").fetchone()[0], 55)
            db.close()
            lines = (out / "timeline.csv").read_text().splitlines()
            self.assertEqual(lines[0], "date,series,time_gmt,value,value2")
            self.assertEqual(len(lines), 5)
            # a second run replaces rather than duplicates
            counts, _, _ = gp.write_detail(out, {"sqlite"})
            db = sqlite3.connect(out / "garmin.db")
            self.assertEqual(db.execute("select count(*) from timeline").fetchone()[0], 4)
            db.close()


class TestEstimate(unittest.TestCase):
    def test_scales_with_days(self):
        self.assertAlmostEqual(gp.estimate_minutes(60, 4, 1.0), 2.6)
        self.assertEqual(gp.estimate_minutes(0, 4, 1.0), 0)


if __name__ == "__main__":
    unittest.main()
