"""Pure-function tests for garmin_pull.py: no network, no account."""
from __future__ import annotations

import sys
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


class TestEstimate(unittest.TestCase):
    def test_scales_with_days(self):
        self.assertAlmostEqual(gp.estimate_minutes(60, 4, 1.0), 2.6)
        self.assertEqual(gp.estimate_minutes(0, 4, 1.0), 0)


if __name__ == "__main__":
    unittest.main()
