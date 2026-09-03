"""Verdict logic of garmin_canary.py. No network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import garmin_canary as gc  # noqa: E402

OK_STATUS = {name: "ok" for name in ("heart_rate", "stress", "sleep", "hrv")}
SUMMARY = {"date": "2026-09-02", "sleep": {"x": 1}, "hrv": {"x": 1}, "summary": {"x": 1}, "vo2max": {}}


def rows(n_series: int, per_series: int) -> list[dict]:
    return [{"series": f"s{i}", "value": 1} for i in range(n_series) for _ in range(per_series)]


class TestAssess(unittest.TestCase):
    def test_healthy(self):
        code, text = gc.assess(SUMMARY, OK_STATUS, rows(20, 200), {})
        self.assertEqual(code, gc.HEALTHY)
        self.assertIn("healthy", text)

    def test_missing_summary_is_a_note_not_a_failure(self):
        code, text = gc.assess({**SUMMARY, "sleep": {}}, OK_STATUS, rows(20, 200), {})
        self.assertEqual(code, gc.HEALTHY)
        self.assertIn("no data for sleep", text)

    def test_failing_endpoint_is_broken(self):
        code, text = gc.assess(SUMMARY, {**OK_STATUS, "spo2": "error: HTTP 500"}, rows(20, 200), {})
        self.assertEqual(code, gc.BROKEN)
        self.assertIn("spo2", text)

    def test_thin_flattening_is_broken(self):
        code, text = gc.assess(SUMMARY, OK_STATUS, rows(3, 10), {})
        self.assertEqual(code, gc.BROKEN)
        self.assertIn("thin", text)

    def test_drift_is_changed_not_broken(self):
        drift = {"heart_rate": {"added": ["newField"], "missing": []}}
        code, text = gc.assess(SUMMARY, OK_STATUS, rows(20, 200), drift)
        self.assertEqual(code, gc.CHANGED)
        self.assertIn("heart_rate +newField", text)


if __name__ == "__main__":
    unittest.main()
